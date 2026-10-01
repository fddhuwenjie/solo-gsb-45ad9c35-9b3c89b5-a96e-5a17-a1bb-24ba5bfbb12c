"""服务运行期间的可迁移 SQLite 快照备份。

调用方提交一个 ``backup_id``，本模块通过 **SQLite Online Backup API**
（``sqlite3.Connection.backup``）在**独立连接**上把当前数据库复制到备份
目录下的临时文件：复制按页分批（``sleep`` 让出写锁），因此写入期间
``/quote``、``/cycles/{id}/batches`` 等请求无需暂停。

副本完成后再用**另一条独立连接**打开临时文件执行 ``PRAGMA
integrity_check``，并流式计算副本的 SHA-256 与字节数；全部通过后用
``os.link``（同卷原子硬链接，目标已存在即失败）**原子发布**最终文件
``{backup_id}.db`` 并在 ``database_backups`` 表登记首次元数据。

语义：

* 同一 ``backup_id`` 的重放（含并发的第二个请求）返回**首次元数据**；
  进程内以 ``backup_id`` 串行，配合登记行的原子 INSERT，保证并发请求
  只有一个最终文件被发布；
* **已有备份不可覆盖**：最终文件已存在且校验通过时直接采用，校验失败
  返回 409；
* 备份失败或 integrity_check 不通过时删除临时文件，不留下半成品；
  登记行只在发布成功后写入（单事务），失败不留任何状态。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import DatabaseBackup, engine as default_engine
from .pricing import PricingError

# backup_id 同时用作最终文件名，只接受不跨越目录的安全字符。
_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Online backup 每批复制的页数与批间让出时间：小库（演示数据）通常一批
# 即完成，大库会在批次之间把写锁交还给正常请求。
_BACKUP_PAGES_PER_STEP = 256
_BACKUP_SLEEP_SECONDS = 0.01

# 并发的第二个请求等待首个请求完成的上限（正常备份远小于该值；超时按
# backup_in_flight 拒绝而不是自行发布第二个文件）。
_INFLIGHT_WAIT_SECONDS = 60.0

_HASH_CHUNK = 1024 * 1024

# 进程内每个 backup_id 只有一个"主请求"执行真正的复制；其余请求等待后
# 读取首次登记的元数据。服务为单进程部署（docker compose 单容器）。
_locks_guard = threading.Lock()
_inflight_locks: dict[str, threading.Lock] = {}


# --------------------------------------------------------------------- paths


def validate_backup_id(backup_id: str) -> None:
    if not isinstance(backup_id, str) or not _BACKUP_ID_RE.match(backup_id):
        raise PricingError(
            f"非法 backup_id: {backup_id!r}；只允许 1~128 个字母、数字、"
            "点、下划线或短横线",
            code="invalid_backup_id", status_code=400,
            details={"backup_id": backup_id})


def _sqlite_file_path(eng) -> str | None:
    """从 SQLAlchemy 引擎 URL 解析 SQLite 文件路径；``:memory:`` 返回
    ``None``（内存库改用借出的活动连接做源）。"""
    url = eng.url
    if url.get_backend_name() != "sqlite":
        raise PricingError(
            "备份仅支持 SQLite 数据库",
            code="backup_unsupported_database", status_code=409)
    database = url.database
    if database in (None, "", ":memory:"):
        return None
    return database


def default_backup_dir() -> Path:
    """备份目录：``BACKUP_DIR`` 环境变量优先；否则落在数据库文件旁的
    ``backups/``（Docker 卷内即 ``/data/backups``）。"""
    raw = os.environ.get("BACKUP_DIR")
    if raw:
        return Path(raw)
    db_file = _sqlite_file_path(default_engine)
    base = Path(db_file).resolve().parent if db_file else Path("data")
    return base / "backups"


@contextmanager
def _source_connection(eng):
    """提供备份**源**连接（独立于引擎连接池的新建连接；内存库例外——
    内存数据只存在于 StaticPool 唯一的活动连接里，只能借出该连接）。

    源连接设置 busy_timeout，避免与正常写请求互相立即报 locked。
    """
    db_file = _sqlite_file_path(eng)
    if db_file is not None:
        conn = sqlite3.connect(
            db_file, timeout=30,
            detect_types=0, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={30_000}")
            yield conn
        finally:
            conn.close()
        return

    # :memory:：StaticPool 下所有会话共享同一条底层连接。
    pooled = eng.raw_connection()
    try:
        conn = pooled.driver_connection
        yield conn
    finally:
        pooled.close()


@contextmanager
def _destination_connection(path: Path):
    """打开/创建备份**目标**临时文件的连接（独立连接，保持默认
    journal 模式，不为临时副本产生 -wal/-shm 侧车文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout={30_000}")
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- verification


def verify_copy(path: Path) -> None:
    """用独立连接对副本执行 integrity_check；结果不为 ``ok`` 即失败。"""
    uri = path.as_uri()  # file:// URI，自动转义路径中的特殊字符
    conn = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise PricingError(
            f"备份副本无法通过完整性检查: {exc}",
            code="backup_integrity_failed", status_code=500,
            details={"backup_path": str(path)})
    finally:
        conn.close()
    if not row or row[0] != "ok":
        raise PricingError(
            f"备份副本 integrity_check 结果非 ok: {row[0] if row else None}",
            code="backup_integrity_failed", status_code=500,
            details={"backup_path": str(path),
                     "integrity_check": row[0] if row else None})


def hash_file(path: Path) -> tuple[str, int]:
    """流式计算 SHA-256（hex）与字节数。"""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _online_copy(eng, destination: Path) -> None:
    """SQLite Online Backup API：独立源连接 → 独立目标连接，分批复制，
    批间 sleep 把锁让给在线写请求。

    Online Backup 会把源库头部（含 WAL 标志位）原样复制过来，使目标也以
    WAL 模式运行并产生 ``-wal/-shm`` 侧车文件——那样单独发布 ``.db`` 并非
    完整快照。复制完成后在同一连接上把目标切回 ``DELETE`` 日志模式，强制
    检查点并使副本成为自包含的单文件。
    """
    # 崩溃残留的同名临时文件不能作为备份起点（backup 虽会重置页面大小，
    # 仍先显式删除以保证是全新文件）。
    with suppress(FileNotFoundError):
        destination.unlink()
    with suppress(FileNotFoundError):
        Path(str(destination) + "-wal").unlink()
        Path(str(destination) + "-shm").unlink()
    with _destination_connection(destination) as dst, _source_connection(eng) as src:
        src.backup(dst, pages=_BACKUP_PAGES_PER_STEP,
                   sleep=_BACKUP_SLEEP_SECONDS)
        mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise PricingError(
                f"备份副本无法切换到 DELETE 日志模式（结果: {mode}）；"
                "不能发布带 -wal 侧车文件的副本",
                code="backup_journal_mode_failed", status_code=500,
                details={"backup_path": str(destination), "journal_mode": mode})
        # 强制把可能残留的 WAL 页全部并入主库文件。
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ------------------------------------------------------------------ main flow


def _inflight_lock(backup_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _inflight_locks.get(backup_id)
        if lock is None:
            lock = threading.Lock()
            _inflight_locks[backup_id] = lock
        return lock


def _body(row: DatabaseBackup) -> dict:
    return {
        "backup_id": row.backup_id,
        "filename": row.filename,
        "download_url": f"/backups/{row.backup_id}/download",
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "created_at": row.created_at,
    }


def _existing_record(eng, backup_id: str) -> DatabaseBackup | None:
    with Session(eng) as session:
        row = session.get(DatabaseBackup, backup_id)
        if row is None:
            return None
        session.expunge(row)
        return row


def _adopt_existing_file(backup_id: str, final_path: Path) -> tuple[str, int]:
    """最终文件已存在（上一进程发布后崩溃在登记前，或外部预置）：备份
    不可覆盖，只有在文件仍通过 integrity_check 时才采用并补登记。"""
    try:
        verify_copy(final_path)
        return hash_file(final_path)
    except PricingError:
        raise PricingError(
            f"backup_id {backup_id} 的备份文件已存在且无法通过完整性"
            "检查；已有备份不可覆盖，请更换 backup_id",
            code="backup_exists", status_code=409,
            details={"backup_id": backup_id, "path": str(final_path)})


def _publish_and_record(eng, backup_id: str, backup_dir: Path) -> dict:
    """执行一次完整备份并发布；调用方持有该 backup_id 的进程内锁。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    final_path = backup_dir / f"{backup_id}.db"

    with Session(eng) as session:
        row = session.get(DatabaseBackup, backup_id)
        if row is not None:  # 等待期间首个请求已完成登记
            return _body(row)

    if final_path.exists():
        sha, size = _adopt_existing_file(backup_id, final_path)
        return _record(eng, backup_id, final_path.name, sha, size)

    tmp_path = backup_dir / f".{backup_id}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        _online_copy(eng, tmp_path)
        verify_copy(tmp_path)
        sha, size = hash_file(tmp_path)
        # 原子发布：同卷硬链接，目标已存在时失败而绝不覆盖；链接成功后
        # 源临时文件即多余副本，删除之。
        try:
            os.link(tmp_path, final_path)
        except FileExistsError:
            # 并发/残留：不覆盖已有文件，按已有文件校验后采用。
            sha, size = _adopt_existing_file(backup_id, final_path)
        return _record(eng, backup_id, final_path.name, sha, size)
    finally:
        # 成功路径下临时文件已被硬链接发布（链接计数仍为 1），删除该
        # 目录项；失败路径删除半成品。无论成败不留 .tmp 文件。
        with suppress(FileNotFoundError):
            tmp_path.unlink()


def _record(eng, backup_id: str, filename: str, sha256: str,
            size_bytes: int) -> dict:
    """原子登记首次元数据；并发下只有一个 INSERT 成功，落败方读取并
    返回首个请求登记的元数据（摘要不一致按冲突拒绝）。"""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    row = DatabaseBackup(
        backup_id=backup_id, filename=filename, sha256=sha256,
        size_bytes=size_bytes, created_at=now)
    try:
        with Session(eng) as session, session.begin():
            session.add(row)
            session.flush()  # 立即暴露主键唯一冲突
        # 事务提交后再读取，确保对象与会话解耦。
        with Session(eng) as session:
            stored = session.get(DatabaseBackup, backup_id)
            session.expunge(stored)
        return _body(stored)
    except IntegrityError:
        # 并发下首个请求已登记：以登记为准，摘要不一致则拒绝。
        with Session(eng) as session:
            existing = session.get(DatabaseBackup, backup_id)
            if existing is None:  # 理论上不会发生
                raise
            if (existing.sha256 != sha256
                    or existing.size_bytes != size_bytes
                    or existing.filename != filename):
                raise PricingError(
                    f"backup_id {backup_id} 已存在且对应不同的备份内容；"
                    "已有备份不可覆盖",
                    code="backup_conflict", status_code=409,
                    details={"backup_id": backup_id,
                             "existing_sha256": existing.sha256,
                             "incoming_sha256": sha256})
            session.expunge(existing)
            return _body(existing)


def create_backup(backup_id: str, *, engine=None,
                  backup_dir: str | os.PathLike | None = None) -> tuple[dict, bool]:
    """提交一次在线备份，返回 ``(元数据, 是否本次新建)``。

    同一 ``backup_id`` 并发时只执行一次实际复制，其余请求阻塞等待后
    返回首次元数据（``created=False``，HTTP 200）；首次成功为
    ``created=True``（HTTP 201）。任何失败都会清理临时文件。
    """
    validate_backup_id(backup_id)
    eng = engine or default_engine
    target_dir = Path(backup_dir) if backup_dir is not None else default_backup_dir()

    with Session(eng) as session:
        existing = session.get(DatabaseBackup, backup_id)
        if existing is not None:
            return _body(existing), False

    lock = _inflight_lock(backup_id)
    while True:
        if lock.acquire(blocking=False):
            # 成为本 backup_id 的"主请求"：唯一执行实际复制与发布。
            try:
                return _publish_and_record(eng, backup_id, target_dir), True
            finally:
                lock.release()

        # 并发的后到请求：等待主请求完成后读取首次登记的元数据。
        if not lock.acquire(timeout=_INFLIGHT_WAIT_SECONDS):
            raise PricingError(
                f"backup_id {backup_id} 的另一个备份仍在进行中",
                code="backup_in_flight", status_code=409,
                details={"backup_id": backup_id})
        try:
            row = _existing_record(eng, backup_id)
        finally:
            lock.release()
        if row is not None:
            return _body(row), False
        # 主请求失败（不会留下登记/临时文件）：本请求成为新的主请求重试。


def get_backup(backup_id: str, *, engine=None) -> dict:
    """返回已登记备份的元数据；未登记返回 404。"""
    validate_backup_id(backup_id)
    eng = engine or default_engine
    with Session(eng) as session:
        row = session.get(DatabaseBackup, backup_id)
        if row is None:
            raise PricingError(
                f"备份不存在: {backup_id}",
                code="backup_not_found", status_code=404,
                details={"backup_id": backup_id})
        return _body(row)


def backup_file_path(backup_id: str, *, engine=None,
                     backup_dir: str | os.PathLike | None = None) -> Path:
    """已登记备份文件的磁盘路径（用于下载）；登记或文件缺失均 404。"""
    validate_backup_id(backup_id)
    eng = engine or default_engine
    target_dir = Path(backup_dir) if backup_dir is not None else default_backup_dir()
    with Session(eng) as session:
        row = session.get(DatabaseBackup, backup_id)
        if row is None:
            raise PricingError(
                f"备份不存在: {backup_id}",
                code="backup_not_found", status_code=404,
                details={"backup_id": backup_id})
        path = target_dir / row.filename
    if not path.is_file():
        raise PricingError(
            f"备份文件已不在备份目录: {backup_id}",
            code="backup_file_missing", status_code=404,
            details={"backup_id": backup_id, "path": str(path)})
    return path
