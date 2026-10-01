"""服务运行期间的可迁移 SQLite 快照（SQLite Online Backup API）。

调用方提交 ``backup_id``，本模块在**独立连接**上通过 ``sqlite3`` 的
Online Backup API（``Connection.backup``）把在线数据库增量复制到备份目录
里的临时文件。复制期间源库的报价 / 结算请求照常工作：WAL 让读不阻塞
唯一写者，而 backup API 本身以读锁分批拉取页，写事务可以在批次之间提交。

副本完成后：

1. 用**另一条独立连接**打开临时文件执行 ``PRAGMA integrity_check``，
   结果必须恰为 ``ok``（打开即报 malformed 同样视为失败）；
2. 计算文件的 **SHA-256** 与**字节数**；
3. 用 ``os.link`` 把临时文件**原子发布**为 ``<backup_id>.db``（目标已存在
   必然失败，已有备份不可覆盖），随后登记元数据（主键保证同一 backup_id
   只有一行）。

任何失败（复制异常、integrity_check 不为 ok、发布冲突……）都删除临时
文件，备份目录不留下半成品；登记行与文件由启动时的
:func:`reconcile_backups` 相互对齐（清理崩溃残留）。同一 backup_id 的
并发请求在进程内由每 id 锁串行化，跨进程由文件原子链接 + 主键兜底，
最终只有一个文件、一份首次元数据。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import DatabaseBackup, engine as default_engine
from .pricing import PricingError

# backup_id 同时是文件名，只允许安全字符且长度受限，杜绝路径穿越。
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TMP_PREFIX = ".tmp-"
# Online Backup 每批拷贝的页数与批次间休眠：分批让出写锁，使报价/结算在
# 备份期间持续提交（测试中可 monkeypatch 以放大并发窗口）。
_PAGES_PER_STEP = 64
_STEP_SLEEP_SECONDS = 0.01
_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class BackupResult:
    metadata: dict
    created: bool


# 同一 backup_id 在本进程内串行化，使并发请求中只有一个执行复制/发布；
# 不同 backup_id 互不阻塞。跨进程的重复发布由主键 + 文件存在检查兜底。
_locks_guard = threading.Lock()
_backup_locks: dict[str, threading.Lock] = {}


def _backup_lock(backup_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _backup_locks.get(backup_id)
        if lock is None:
            lock = threading.Lock()
            _backup_locks[backup_id] = lock
        return lock


# ------------------------------------------------------------ configuration


def source_db_path(eng=None) -> str | None:
    """从引擎 URL 解析文件型 SQLite 的绝对路径；内存库返回 None。"""
    e = eng or default_engine
    if e.url.drivername != "sqlite" or not e.url.database:
        return None
    db = e.url.database
    if db == ":memory:":
        return None
    return os.path.abspath(db)


def backup_dir(eng=None) -> str:
    """备份目录：``BACKUP_DIR`` 环境变量优先，否则取数据库文件同级的
    ``backups/``（Docker 卷布局即 ``/data/backups``）。"""
    override = os.environ.get("BACKUP_DIR")
    if override:
        return os.path.abspath(override)
    path = source_db_path(eng)
    if path is None:
        raise PricingError(
            "当前数据库为内存 SQLite，没有数据卷可发布备份；请设置 "
            "BACKUP_DIR 或使用文件型 DATABASE_URL",
            code="backup_unavailable", status_code=409,
        )
    return os.path.join(os.path.dirname(path), "backups")


def validate_backup_id(backup_id: str) -> None:
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise PricingError(
            f"非法 backup_id: {backup_id!r}；只允许字母/数字开头，后跟至多 "
            "127 个字母、数字、下划线或连字符",
            code="invalid_backup_id", status_code=400,
            details={"backup_id": backup_id})


# --------------------------------------------------------------- primitives


def _borrow_raw(eng) -> tuple[object, sqlite3.Connection]:
    """从连接池借一条底层 sqlite3 连接，返回 ``(代理, 原始连接)``。

    仅用于内存库这种"所有连接共享同一实例"的场景；文件库一律另开独立
    sqlite3 连接，绝不占用服务连接池。代理用于归还连接。
    """
    proxy = eng.raw_connection()
    return proxy, proxy.dbapi_connection


def _open_file_uri(path: str) -> sqlite3.Connection:
    from urllib.parse import quote

    uri = "file://" + quote(os.path.abspath(path), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30,
                           check_same_thread=False)


def online_copy(src: sqlite3.Connection, dst_path: str,
                *, pages_per_step: int = _PAGES_PER_STEP,
                step_sleep: float = _STEP_SLEEP_SECONDS) -> None:
    """在独立连接上用 Online Backup API 把源库复制到 ``dst_path``。

    目标文件必须是一个全新的空数据库；``pages`` 限制每批拷贝的页数，
    ``progress`` 回调在每批之间休眠，让其它连接的写事务有机会获取写锁
    并提交（Python 的 sqlite3 不暴露分步句柄：批间让步通过
    ``sleep`` 参数与 progress 回调实现），备份期间报价/结算无需暂停。
    副本使用 rollback journal 单文件（journal_mode=DELETE），拷贝结束时
    不携带 WAL/SHM，得到自包含、可离线核对的文件。
    """
    import time

    dst = sqlite3.connect(dst_path, timeout=30, check_same_thread=False)
    try:
        dst.execute("PRAGMA busy_timeout=30000")
        dst.execute("PRAGMA journal_mode=DELETE")

        def _between_batches(status, remaining, total):
            if remaining:
                time.sleep(step_sleep)

        # sleep= 是某批次遇到写锁冲突时 sqlite3 自身的重试等待；批间让步
        # 由 progress 回调完成。
        src.backup(dst, pages=pages_per_step, progress=_between_batches,
                   sleep=0.25)
    finally:
        dst.close()


def verify_copy(path: str) -> None:
    """用独立连接对副本执行 integrity_check，非 ``ok``（含打开即报告
    database is malformed）一律抛 ``backup_integrity_failed``。"""
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as e:
            raise PricingError(
                f"备份副本完整性校验失败（SQLite 报告数据库损坏）: {path}: {e}",
                code="backup_integrity_failed", status_code=500,
                details={"path": path, "error": str(e)}) from e
    finally:
        conn.close()
    values = [r[0] for r in row]
    if values != ["ok"]:
        raise PricingError(
            f"备份副本完整性校验未通过（integrity_check 未返回 ok）: {path}",
            code="backup_integrity_failed", status_code=500,
            details={"path": path, "integrity_check": values[:10]})


def hash_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


# ---------------------------------------------------------------- main flow


def get_backup(backup_id: str, *, engine=None) -> dict | None:
    """返回已发布备份的元数据（含下载地址）；不存在返回 None。"""
    validate_backup_id(backup_id)
    eng = engine or default_engine
    with Session(eng) as session:
        row = session.get(DatabaseBackup, backup_id)
        if row is None:
            return None
        return _metadata(row)


def create_backup(backup_id: str, *, engine=None, directory: str | None = None,
                  pages_per_step: int = _PAGES_PER_STEP,
                  step_sleep: float = _STEP_SLEEP_SECONDS) -> BackupResult:
    """提交一次在线备份，返回 ``(元数据, 是否首次创建)``。

    同一 backup_id 的重放返回首次元数据（``created=False``）；任何失败都
    清理临时文件，不发布半成品。
    """
    validate_backup_id(backup_id)
    eng = engine or default_engine
    target_dir = directory or backup_dir(eng)
    final_name = f"{backup_id}.db"
    final_path = os.path.join(target_dir, final_name)

    with _backup_lock(backup_id):
        existing = get_backup(backup_id, engine=eng)
        if existing is not None:
            return BackupResult(existing, False)
        if os.path.exists(final_path):
            # 无登记行却有最终文件：历史残留 / 外部同名文件，拒绝覆盖。
            raise PricingError(
                f"备份文件已存在且未登记，拒绝覆盖: {final_name}",
                code="backup_exists", status_code=409,
                details={"backup_id": backup_id, "filename": final_name})

        os.makedirs(target_dir, exist_ok=True)
        tmp_name = (f"{_TMP_PREFIX}{backup_id}-{os.getpid()}-"
                    f"{id(threading.current_thread())}.db")
        tmp_path = os.path.join(target_dir, tmp_name)
        published = False
        try:
            try:
                _do_copy(eng, tmp_path, pages_per_step, step_sleep)
                verify_copy(tmp_path)
            except PricingError:
                raise
            except sqlite3.Error as e:
                raise PricingError(
                    f"在线备份失败（SQLite Online Backup）: {e}",
                    code="backup_failed", status_code=500,
                    details={"backup_id": backup_id, "error": str(e)}) from e
            digest, size = hash_file(tmp_path)

            # 原子发布先于登记：os.link 在同一文件系统上是原子的，且目标
            # 已存在时必然失败（FileExistsError），因此已有备份不可被覆盖。
            try:
                os.link(tmp_path, final_path)
                published = True
            except FileExistsError:
                # 并发请求（另一线程/进程）已抢先发布同名最终文件：同一
                # backup_id 的重放返回首次元数据，本次副本删除，绝不覆盖。
                meta = get_backup(backup_id, engine=eng)
                if meta is None:
                    # 胜者已发布文件但尚未登记（极窄窗口）：稍后重试即可。
                    raise PricingError(
                        f"备份 {backup_id} 正在被并发请求发布，请稍后重试",
                        code="backup_in_progress", status_code=409,
                        details={"backup_id": backup_id}) from None
                return BackupResult(meta, False)

            # 登记元数据（主键唯一）。同一 backup_id 下最终文件名也是唯一
            # 的：能 link 成功说明此前无人发布，因此这里的 PK 冲突只可能
            # 来自极端的跨进程交错，按重放处理并移除自己多余的链接。
            now = datetime.now(timezone.utc).isoformat()
            row = DatabaseBackup(
                backup_id=backup_id, filename=final_name,
                size_bytes=size, sha256=digest, created_at=now)
            meta: dict | None = None
            with Session(eng) as session:
                try:
                    with session.begin():
                        session.add(row)
                        session.flush()
                        meta = _metadata(row)
                except IntegrityError:
                    meta = get_backup(backup_id, engine=eng)
                    if meta is None:
                        # 极端交错：最终文件是本次刚 link 的同一 inode，安全
                        # 移除，避免留下无登记行的孤儿文件。
                        if published:
                            _safe_unlink(final_path)
                        raise PricingError(
                            f"备份登记失败: {backup_id}", code="backup_failed",
                            status_code=500,
                            details={"backup_id": backup_id}) from None
                    return BackupResult(meta, False)
            return BackupResult(meta, True)
        finally:
            # link 发布后临时文件是同一 inode 的多余名字，删除不影响最终
            # 文件；异常路径下它就是要清理的半成品。
            _safe_unlink(tmp_path)


def _do_copy(eng, tmp_path: str, pages_per_step: int,
             step_sleep: float) -> None:
    """选择独立文件连接（文件库）或共享内存连接（内存库）执行在线复制。"""
    path = source_db_path(eng)
    if path is not None:
        # 独立 URI 连接打开源库，不复用服务连接池；backup 只读拉取页。
        src = _open_file_uri(path)
        try:
            online_copy(src, tmp_path, pages_per_step=pages_per_step,
                        step_sleep=step_sleep)
        finally:
            src.close()
    else:
        proxy, src = _borrow_raw(eng)
        try:
            online_copy(src, tmp_path, pages_per_step=pages_per_step,
                        step_sleep=step_sleep)
        finally:
            proxy.close()


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _metadata(row: DatabaseBackup) -> dict:
    return {
        "backup_id": row.backup_id,
        "filename": row.filename,
        "download_url": f"/backups/{row.backup_id}/download",
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "created_at": row.created_at,
    }


# ------------------------------------------------------------- startup sweep


def reconcile_backups(*, engine=None, directory: str | None = None) -> None:
    """启动时对齐登记行与备份目录，清除崩溃残留：

    * 目录里的临时文件（``.tmp-*``）一律删除（无归属、绝不会是发布件）；
    * 有登记行但最终文件缺失：删除孤儿登记行（该备份从未完成发布）；
    * 有最终文件但无登记行：不删除用户文件，仅跳过（保守处理）。
    """
    eng = engine or default_engine
    try:
        target_dir = directory or backup_dir(eng)
    except PricingError:
        return  # 内存库且未配置 BACKUP_DIR：无目录可清理
    if not os.path.isdir(target_dir):
        return

    for name in os.listdir(target_dir):
        if name.startswith(_TMP_PREFIX):
            _safe_unlink(os.path.join(target_dir, name))

    with Session(eng) as session:
        with session.begin():
            rows = session.scalars(select(DatabaseBackup)).all()
            for row in rows:
                if not os.path.exists(os.path.join(target_dir, row.filename)):
                    session.delete(row)
