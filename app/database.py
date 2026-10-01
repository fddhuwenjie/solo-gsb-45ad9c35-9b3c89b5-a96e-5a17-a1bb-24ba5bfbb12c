"""SQLAlchemy models, engine/session setup and idempotent versioned seed data."""
from __future__ import annotations

import os
from datetime import date

from sqlalchemy import (
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///./data/tariff.db")


# StaticPool is used automatically for :memory: connections so a single shared
# in-memory DB survives across checkouts.
_is_memory = ":memory:" in _database_url()
_connect_args = {"check_same_thread": False, "timeout": 30}
_engine_kwargs: dict = {}
if _is_memory:
    from sqlalchemy.pool import StaticPool

    _engine_kwargs["poolclass"] = StaticPool

engine = create_engine(_database_url(), connect_args=_connect_args, **_engine_kwargs)


def configure_sqlite_engine(eng, *, is_memory: bool = False,
                            busy_timeout_ms: int = 30000) -> None:
    """Attach the standard per-connection SQLite pragmas to an engine.

    * ``busy_timeout``: wait on a contended write lock instead of failing
      immediately (lets concurrent settlement submissions serialize).
    * ``foreign_keys``: enforce ON DELETE CASCADE/RESTRICT.
    * ``journal_mode=WAL`` (file-backed DBs only): readers don't block the
      single writer; the losing writer of a same-revision race gets
      ``database is locked`` and is retried/reclassified by the settlement.
    """
    from sqlalchemy import event

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute("PRAGMA foreign_keys=ON")
        if not is_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


configure_sqlite_engine(engine, is_memory=_is_memory)


class Base(DeclarativeBase):
    pass


class TariffVersion(Base):
    __tablename__ = "tariff_versions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    currency: Mapped[str] = mapped_column(String(8))
    energy_unit: Mapped[str] = mapped_column(String(16))  # e.g. kWh
    description: Mapped[str] = mapped_column(Text, default="")

    rules: Mapped[list["RateRule"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    holidays: Mapped[list["Holiday"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class RateRule(Base):
    __tablename__ = "rate_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_code: Mapped[str] = mapped_column(
        ForeignKey("tariff_versions.code", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    # Naive local datetimes in the rule's timezone, ISO format.
    effective_from: Mapped[str] = mapped_column(String(32))
    effective_to: Mapped[str] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64))
    # ISO weekday mask, 1=Mon..7=Sun, comma separated; empty means every day.
    weekdays: Mapped[str] = mapped_column(String(32), default="")
    # all | workdays | holidays | non_workdays
    day_scope: Mapped[str] = mapped_column(String(16), default="all")
    # Local HH:MM, half-open [on, off); off < on means overnight (wraps midnight).
    time_on: Mapped[str] = mapped_column(String(5))
    time_off: Mapped[str] = mapped_column(String(5))

    version: Mapped[TariffVersion] = relationship(back_populates="rules")
    tiers: Mapped[list["Tier"]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", order_by="Tier.ordinal"
    )


class Tier(Base):
    __tablename__ = "tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("rate_rules.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    # Cumulative usage cap for this tier in energy units. NULL = unbounded (final).
    cumulative_cap: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unit_price: Mapped[str] = mapped_column(String(32))  # price per energy unit

    rule: Mapped[RateRule] = relationship(back_populates="tiers")


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_code: Mapped[str] = mapped_column(
        ForeignKey("tariff_versions.code", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16), default="holiday")
    name: Mapped[str] = mapped_column(String(255), default="")

    version: Mapped[TariffVersion] = relationship(back_populates="holidays")


class VersionImport(Base):
    """导入登记：版本号 -> 规范内容摘要与首次导入的响应体。

    用于 POST /versions/import 的幂等语义：同 code 同摘要重放原结果，
    同 code 不同摘要拒绝（409）。预置（seed）版本没有登记记录，导入时
    按同一规范化流程现算摘要再比较。
    """

    __tablename__ = "version_imports"

    version_code: Mapped[str] = mapped_column(
        ForeignKey("tariff_versions.code", ondelete="CASCADE"), primary_key=True
    )
    content_digest: Mapped[str] = mapped_column(String(64))  # sha256 hex
    # 首次导入时返回的响应体（JSON 文本），重放时原样返回
    response_json: Mapped[str] = mapped_column(Text)

    version: Mapped[TariffVersion] = relationship()


class BillingCycle(Base):
    """账期（billing cycle）：分批增量结算的累计状态。

    首次成功提交时创建并锁定规则版本（``version_code`` 此后不可变）；
    ``revision`` 从 1 开始，每成功一批加一（不存在的账期当前 revision 为 0，
    因此首次提交的 expected_revision 必须为 0）。``total_amount`` 保存
    **未舍入** 的累计金额（各计价段六位小数金额之和），使"本批金额 =
    重算后累计 − 重算前累计"在重启后仍与首次响应逐分一致。
    """

    __tablename__ = "billing_cycles"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_code: Mapped[str] = mapped_column(
        ForeignKey("tariff_versions.code", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer, default=0)
    total_energy: Mapped[str] = mapped_column(String(64), default="0")
    total_amount: Mapped[str] = mapped_column(Text, default="0")
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))

    batches: Mapped[list["UsageBatch"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )
    intervals: Mapped[list["CycleInterval"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )


class UsageBatch(Base):
    """每个账期内成功提交的一批用量（含幂等键与首次响应体）。

    ``(cycle_id, idempotency_key)`` 唯一：同键重放原样返回首次响应，
    同键不同内容拒绝（409）。
    """

    __tablename__ = "usage_batches"
    __table_args__ = (
        UniqueConstraint("cycle_id", "idempotency_key",
                         name="uq_usage_batch_idem"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("billing_cycles.cycle_id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)  # 该批提交成功后的 revision
    idempotency_key: Mapped[str] = mapped_column(String(128))
    version_code: Mapped[str] = mapped_column(String(64))
    content_digest: Mapped[str] = mapped_column(String(64))  # sha256 hex
    batch_energy: Mapped[str] = mapped_column(String(64))
    batch_amount: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))

    cycle: Mapped[BillingCycle] = relationship(back_populates="batches")


class CycleInterval(Base):
    """账期下已成功合并的全部用量区间（UTC 持久化，重启后可重算）。"""

    __tablename__ = "cycle_intervals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("billing_cycles.cycle_id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, index=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("usage_batches.id", ondelete="SET NULL"), nullable=True
    )
    # UTC 半开区间端点（ISO-8601，带 Z）；timezone 为输入声明的 IANA 时区，
    # 计价结果的本地时间表示按它渲染。
    start_utc: Mapped[str] = mapped_column(String(40))
    end_utc: Mapped[str] = mapped_column(String(40))
    timezone: Mapped[str] = mapped_column(String(64))
    energy: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)  # 账期内全局递增序号

    cycle: Mapped[BillingCycle] = relationship(back_populates="intervals")


class DatabaseBackup(Base):
    """在线备份登记：backup_id -> 已发布备份文件的摘要元数据。

    由 ``POST /backups/{backup_id}`` 在**原子发布文件之前**写入：同一
    backup_id 的重放直接返回本行（HTTP 200），并发请求中只有一个能成功
    插入（主键冲突者重放或冲突），已发布备份因此不可覆盖。备份文件本身
    位于数据卷的备份目录，重启后仍可凭本行定位与核对。
    """

    __tablename__ = "database_backups"

    backup_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40))


def init_db() -> None:
    db_path = _database_url()
    if db_path.startswith("sqlite:///") and ":memory:" not in db_path:
        # sqlite:///./data/tariff.db -> ./data
        prefix = "sqlite:///"
        rel = db_path[len(prefix):]
        directory = os.path.dirname(rel)
        if directory:
            os.makedirs(directory, exist_ok=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.scalar(select(TariffVersion)) is None:
            seed(session)
            session.commit()


# ---------------------------------------------------------------- seed data

SEED_VERSIONS: list[dict] = [
    {
        "code": "us-ny-demo-v2024",
        "currency": "USD",
        "energy_unit": "kWh",
        "description": "演示费率：美国纽约（America/New_York）2024 年，含夏令时切换、"
        "工作日/周末/节假日与三段累计阶梯。价格为演示值。",
        "holidays": [
            ("2024-01-01", "holiday", "New Year's Day"),
            ("2024-01-15", "holiday", "Martin Luther King Jr. Day"),
            ("2024-02-19", "holiday", "Presidents' Day"),
            ("2024-05-27", "holiday", "Memorial Day"),
            ("2024-06-19", "holiday", "Juneteenth"),
            ("2024-07-04", "holiday", "Independence Day"),
            ("2024-09-02", "holiday", "Labor Day"),
            ("2024-10-14", "holiday", "Columbus Day"),
            ("2024-11-11", "holiday", "Veterans Day"),
            ("2024-11-28", "holiday", "Thanksgiving"),
            ("2024-12-25", "holiday", "Christmas"),
        ],
        "rules": [
            {
                "code": "std-workday",
                "name": "标准时 工作日",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2024-03-10T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.20"), ("300", "0.25"), (None, "0.30")],
            },
            {
                "code": "dst-workday",
                "name": "夏令时 工作日",
                "effective_from": "2024-03-10T00:00:00",
                "effective_to": "2024-11-03T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.22"), ("300", "0.27"), (None, "0.32")],
            },
            {
                "code": "std-workday-2",
                "name": "标准时 工作日（入冬后）",
                "effective_from": "2024-11-03T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.20"), ("300", "0.25"), (None, "0.30")],
            },
            {
                "code": "leisure",
                "name": "周末与节假日（全天）",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "",
                "day_scope": "non_workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.15"), ("300", "0.18"), (None, "0.22")],
            },
        ],
    },
    {
        "code": "us-ny-demo-v2025",
        "currency": "USD",
        "energy_unit": "kWh",
        "description": "演示费率：美国纽约 2024-07-01 起生效的新版费率。规则结构与 "
        "us-ny-demo-v2024 相同、单价上调，节假日日历略有差异"
        "（2024-10-14 哥伦布日不再列为节假日），用于 /quote-diff 版本对比演示。"
        "价格为演示值。",
        "holidays": [
            ("2024-07-04", "holiday", "Independence Day"),
            ("2024-09-02", "holiday", "Labor Day"),
            ("2024-11-11", "holiday", "Veterans Day"),
            ("2024-11-28", "holiday", "Thanksgiving"),
            ("2024-12-25", "holiday", "Christmas"),
            ("2025-01-01", "holiday", "New Year's Day"),
            ("2025-01-20", "holiday", "Martin Luther King Jr. Day"),
            ("2025-02-17", "holiday", "Presidents' Day"),
            ("2025-05-26", "holiday", "Memorial Day"),
            ("2025-06-19", "holiday", "Juneteenth"),
        ],
        "rules": [
            {
                "code": "dst-workday",
                "name": "夏令时 工作日",
                "effective_from": "2024-07-01T00:00:00",
                "effective_to": "2024-11-03T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.23"), ("300", "0.28"), (None, "0.33")],
            },
            {
                "code": "std-workday",
                "name": "标准时 工作日",
                "effective_from": "2024-11-03T00:00:00",
                "effective_to": "2025-03-09T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.21"), ("300", "0.26"), (None, "0.31")],
            },
            {
                "code": "dst-workday-2",
                "name": "夏令时 工作日（开春后）",
                "effective_from": "2025-03-09T00:00:00",
                "effective_to": "2025-07-01T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.23"), ("300", "0.28"), (None, "0.33")],
            },
            {
                "code": "leisure",
                "name": "周末与节假日（全天）",
                "effective_from": "2024-07-01T00:00:00",
                "effective_to": "2025-07-01T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "",
                "day_scope": "non_workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("100", "0.16"), ("300", "0.19"), (None, "0.23")],
            },
        ],
    },
    {
        "code": "cn-shanghai-demo-v2024",
        "currency": "CNY",
        "energy_unit": "kWh",
        "description": "演示费率：中国居民阶梯分时（Asia/Shanghai，无时区切换）。"
        "工作日/周末/法定节假日（含调休）不同价，三段累计阶梯。价格为演示值。",
        "holidays": [
            ("2024-01-01", "holiday", "元旦"),
            ("2024-02-04", "workday", "调休上班（春节前）"),
            ("2024-02-10", "holiday", "春节"),
            ("2024-02-11", "holiday", "春节"),
            ("2024-02-12", "holiday", "春节"),
            ("2024-02-13", "holiday", "春节"),
            ("2024-02-14", "holiday", "春节"),
            ("2024-02-15", "holiday", "春节"),
            ("2024-02-16", "holiday", "春节"),
            ("2024-02-17", "holiday", "春节"),
            ("2024-02-18", "holiday", "春节"),
            ("2024-02-25", "workday", "调休上班（春节后）"),
            ("2024-04-04", "holiday", "清明节"),
            ("2024-04-05", "holiday", "清明节"),
            ("2024-04-06", "holiday", "清明节"),
            ("2024-04-07", "workday", "调休上班"),
            ("2024-05-01", "holiday", "劳动节"),
            ("2024-05-02", "holiday", "劳动节"),
            ("2024-05-03", "holiday", "劳动节"),
            ("2024-05-04", "holiday", "劳动节"),
            ("2024-05-05", "holiday", "劳动节"),
            ("2024-04-28", "workday", "调休上班"),
            ("2024-05-11", "workday", "调休上班"),
            ("2024-06-08", "holiday", "端午节"),
            ("2024-06-09", "holiday", "端午节"),
            ("2024-06-10", "holiday", "端午节"),
            ("2024-09-15", "holiday", "中秋节"),
            ("2024-09-16", "holiday", "中秋节"),
            ("2024-09-17", "holiday", "中秋节"),
            ("2024-09-14", "workday", "调休上班"),
            ("2024-10-01", "holiday", "国庆节"),
            ("2024-10-02", "holiday", "国庆节"),
            ("2024-10-03", "holiday", "国庆节"),
            ("2024-10-04", "holiday", "国庆节"),
            ("2024-10-05", "holiday", "国庆节"),
            ("2024-10-06", "holiday", "国庆节"),
            ("2024-10-07", "holiday", "国庆节"),
            ("2024-09-29", "workday", "调休上班"),
            ("2024-10-12", "workday", "调休上班"),
        ],
        "rules": [
            {
                "code": "workday",
                "name": "工作日（调休上班按工作日）",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "Asia/Shanghai",
                "weekdays": "1,2,3,4,5",
                "day_scope": "workdays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("200", "0.617"), ("400", "0.677"), (None, "0.977")],
            },
            {
                "code": "weekend",
                "name": "普通周末（法定节假日除外）",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "Asia/Shanghai",
                "weekdays": "6,7",
                "day_scope": "weekends",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("200", "0.617"), ("400", "0.677"), (None, "0.977")],
            },
            {
                "code": "statutory-holiday",
                "name": "法定节假日",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "Asia/Shanghai",
                "weekdays": "",
                "day_scope": "holidays",
                "time_on": "00:00",
                "time_off": "24:00",
                "tiers": [("200", "0.307"), ("400", "0.337"), (None, "0.487")],
            },
        ],
    },
]


def seed(session: Session) -> None:
    for v in SEED_VERSIONS:
        version = TariffVersion(
            code=v["code"],
            currency=v["currency"],
            energy_unit=v["energy_unit"],
            description=v["description"],
        )
        for day_str, kind, name in v["holidays"]:
            version.holidays.append(
                Holiday(day=date.fromisoformat(day_str), kind=kind, name=name)
            )
        for r in v["rules"]:
            rule = RateRule(
                code=r["code"],
                name=r["name"],
                effective_from=r["effective_from"],
                effective_to=r["effective_to"],
                timezone=r["timezone"],
                weekdays=r["weekdays"],
                day_scope=r["day_scope"],
                time_on=r["time_on"],
                time_off=r["time_off"],
            )
            for ordinal, (cap, price) in enumerate(r["tiers"], start=1):
                rule.tiers.append(
                    Tier(ordinal=ordinal, cumulative_cap=cap, unit_price=price)
                )
            version.rules.append(rule)
        session.add(version)
