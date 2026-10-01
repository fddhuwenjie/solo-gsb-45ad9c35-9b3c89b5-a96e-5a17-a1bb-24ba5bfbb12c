"""账期分批用量的持久化增量结算。

账期用量会分批到达，本模块在 SQLite 上提供"提交即对账期整体重算"的
增量结算语义：

1. **幂等优先**：每次提交带 ``idempotency_key``；先查幂等记录——同键同
   内容直接返回首次响应（不写任何数据、revision 不变），同键不同内容
   拒绝（409 ``idempotency_conflict``）。
2. **乐观 revision**：幂等检查之后再比 ``expected_revision``——首次必须
   为 0，之后必须等于账期当前 revision，否则 409 ``revision_conflict``。
   两个并发请求持同一 revision 时，通过条件写（``WHERE revision = :exp``）
   与 SQLite 写锁保证只有一个生效；落败方在重试时看到新 revision，得到
   409。
3. **UTC 合并、禁止重叠**：历史区间与本批区间全部换算成 UTC 后合并且
   不得重叠（首尾相接允许），随后调用现有计价引擎对账期整体重算——
   累计阶梯因此自然贯穿所有批次。
4. **本批金额**：重算后的未舍入累计金额 − 重算前的未舍入累计金额，再
   四舍五入到分；持久化未舍入累计金额保证重启后本批金额与首次响应一致。

任何区间冲突、规则版本锁定冲突、规则缺口或计价失败都在事务提交前抛出，
不会写入批次、幂等记录，也不会推进 revision。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import pricing
from .database import (
    BillingCycle,
    CycleInterval,
    UsageBatch,
    engine as default_engine,
)
from .importer import canonical_json
from .pricing import (
    Q2,
    Q6,
    ZERO,
    InputInterval,
    LoadedVersion,
    PricingError,
    load_version,
)
from .schemas import QuoteResponse, SegmentOut, SettlementBatchRequest

# Same-revision write races surface as SQLITE_BUSY (a deferred transaction
# upgrading to a write lock); retry the whole serialized transaction a
# bounded number of times before giving up.
_MAX_LOCK_RETRIES = 20


# --------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class SubmitResult:
    body: dict
    replayed: bool


class _StoredIntervalPayload:
    """Minimal interval payload accepted by ``pricing.quote``.

    Times are already UTC instants rendered with a ``Z`` suffix; the engine
    re-parses them and uses ``timezone`` only for local-time rendering.
    """

    def __init__(self, start_utc: datetime, end_utc: datetime,
                 tz_name: str, energy: Decimal):
        self.start = _utc_z(start_utc)
        self.end = _utc_z(end_utc)
        self.timezone = tz_name
        self._energy = energy

    def energy_decimal(self) -> Decimal:
        return self._energy


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- digest


def _content_digest(version_code: str,
                    intervals: list[InputInterval]) -> str:
    """幂等内容摘要：规则版本 + 本批区间（UTC 端点/时区/能量，按 UTC 起点
    排序）。``expected_revision`` 是并发控制令牌，刻意不参与摘要。"""
    doc = {
        "version": version_code,
        "intervals": [
            {
                "start": _utc_z(i.start),
                "end": _utc_z(i.end),
                "timezone": str(i.tz),
                "energy": format(i.energy.normalize(), "f"),
            }
            for i in sorted(intervals,
                            key=lambda i: (i.start, i.end, str(i.tz)))
        ],
    }
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- quote body


def build_quote_response(version: LoadedVersion,
                         result: pricing.QuoteResult | None) -> QuoteResponse:
    """构造与 ``POST /quote`` 完全一致的报价响应体。

    集中在此处，供 ``/quote``、``/usage-estimate`` 与增量结算共用。
    """
    if result is None:
        return QuoteResponse(
            version=version.code,
            currency=version.currency,
            energy_unit=version.energy_unit,
            total_energy=str(ZERO.quantize(Q6)),
            total_amount=str(ZERO.quantize(Q2)),
            matched_rules=[],
            segments=[],
        )
    return QuoteResponse(
        version=version.code,
        currency=version.currency,
        energy_unit=version.energy_unit,
        total_energy=str(result.total_energy),
        total_amount=str(result.total_amount.quantize(
            Q2, rounding=ROUND_HALF_UP)),
        matched_rules=result.matched_rules,
        segments=[
            SegmentOut(**s.to_dict(version.energy_unit, version.currency))
            for s in result.segments
        ],
    )


# ------------------------------------------------------------------- errors


def _revision_conflict(cycle_id: str, expected: int,
                       current: int) -> PricingError:
    return PricingError(
        f"账期 {cycle_id} 的 expected_revision 过期：期望 {expected}，"
        f"当前 revision 为 {current}",
        code="revision_conflict", status_code=409,
        details={"cycle_id": cycle_id,
                 "expected_revision": expected,
                 "current_revision": current},
    )


def _version_locked(cycle_id: str, locked: str,
                    requested: str) -> PricingError:
    return PricingError(
        f"账期 {cycle_id} 已锁定规则版本 {locked}，不能改用 {requested}；"
        "同一账期的所有批次必须使用相同规则版本",
        code="version_locked", status_code=409,
        details={"cycle_id": cycle_id,
                 "locked_version": locked,
                 "requested_version": requested},
    )


def _idempotency_conflict(cycle_id: str, key: str, existing_digest: str,
                          received_digest: str) -> PricingError:
    return PricingError(
        f"账期 {cycle_id} 的幂等键 {key!r} 已用于不同的提交内容"
        "（同键重放必须与首次提交完全一致）",
        code="idempotency_conflict", status_code=409,
        details={"cycle_id": cycle_id, "idempotency_key": key,
                 "existing_digest": existing_digest,
                 "received_digest": received_digest},
    )


def _overlap_error(cycle_id: str, a, b) -> PricingError:
    """合并去重后发现任意两段重叠时的 400；``a``/``b`` 为 CycleInterval
    行或本批 :class:`InputInterval`。"""
    def part(it):
        if isinstance(it, CycleInterval):
            return {"start": it.start_utc, "end": it.end_utc,
                    "timezone": it.timezone, "revision": it.revision}
        return {"start": _utc_z(it.start), "end": _utc_z(it.end),
                "timezone": str(it.tz)}

    pa, pb = part(a), part(b)
    return PricingError(
        f"账期 {cycle_id} 的用量区间在 UTC 时间线上重叠（{pa['start']} ~ "
        f"{pa['end']} 与 {pb['start']} ~ {pb['end']}）；账期内所有批次的"
        "区间必须互不重叠（首尾相接允许）",
        code="interval_overlap", status_code=400,
        details={"cycle_id": cycle_id,
                 "interval_a": pa, "interval_b": pb},
    )


def _find_overlap(stored_rows: list[CycleInterval],
                  new_intervals: list[InputInterval]):
    """合并历史行与本批区间后返回第一对重叠项，无重叠返回 None。"""
    items: list = list(stored_rows) + list(new_intervals)

    def span(it):
        if isinstance(it, CycleInterval):
            return _parse_z(it.start_utc), _parse_z(it.end_utc)
        return it.start, it.end

    items.sort(key=lambda it: span(it))
    for k in range(1, len(items)):
        a, b = items[k - 1], items[k]
        if span(b)[0] < span(a)[1]:
            return a, b
    return None


# ----------------------------------------------------------------- main flow


def submit_batch(cycle_id: str, req: SettlementBatchRequest, *,
                 engine=None) -> SubmitResult:
    """提交一批账期用量，返回 ``(响应体, 是否幂等重放)``。

    任何失败都抛出 :class:`PricingError`，且不会写入批次、幂等记录或推进
    revision。``engine`` 可注入（测试用临时文件库），默认应用全局引擎。
    """
    eng = engine or default_engine

    # 先用现有引擎语义解析本批区间（时区/DST/区间非法/批内重叠在此暴露），
    # 只有合法区间才进入事务、才计算幂等摘要。
    new_intervals = pricing.parse_usage_intervals(req.intervals)
    digest = _content_digest(req.version, new_intervals)

    attempt = 0
    while True:
        attempt += 1
        try:
            return _submit_once(eng, cycle_id, req, new_intervals, digest)
        except OperationalError as exc:  # SQLITE_BUSY same-revision race
            if attempt > _MAX_LOCK_RETRIES or not _is_lock_error(exc):
                raise
            # Serialized retry: re-read revision / idempotency record and
            # re-run all checks from scratch on a fresh transaction.


def _is_lock_error(exc: OperationalError) -> bool:
    msg = str(exc.orig).lower() if exc.orig is not None else ""
    return ("database is locked" in msg
            or "database table is locked" in msg)


def _submit_once(eng, cycle_id: str, req: SettlementBatchRequest,
                 new_intervals: list[InputInterval],
                 digest: str) -> SubmitResult:
    with Session(eng) as session:
        try:
            with session.begin():
                # 1) 幂等优先：同键记录先于 expected_revision 判定。
                existing_batch = session.scalar(
                    select(UsageBatch).where(
                        UsageBatch.cycle_id == cycle_id,
                        UsageBatch.idempotency_key == req.idempotency_key,
                    )
                )
                if existing_batch is not None:
                    if existing_batch.content_digest != digest:
                        raise _idempotency_conflict(
                            cycle_id, req.idempotency_key,
                            existing_batch.content_digest, digest)
                    # 同键同内容：无论 expected_revision 为何，原样返回
                    # 首次响应，不写任何数据。
                    return SubmitResult(json.loads(existing_batch.response_json),
                                        True)

                cycle = session.get(BillingCycle, cycle_id)

                # 2) expected_revision 乐观锁检查（在幂等检查之后）。
                current_revision = cycle.revision if cycle else 0
                if req.expected_revision != current_revision:
                    raise _revision_conflict(
                        cycle_id, req.expected_revision, current_revision)

                # 3) 规则版本：不存在 -> 404；首次提交锁定版本；后续批次
                #    换版本 -> 409 version_locked。结构问题/规则缺口由下面
                #    的 quote 抛 409（rule_conflict / rule_gap），全部发生
                #    在写批次、区间与推进 revision 之前。
                version = load_version(session, req.version)
                if cycle is not None and cycle.version_code != req.version:
                    raise _version_locked(
                        cycle_id, cycle.version_code, req.version)

                # 4) 新旧区间按 UTC 合并且禁止重叠（首尾相接允许）。
                stored_rows = (
                    session.scalars(
                        select(CycleInterval)
                        .where(CycleInterval.cycle_id == cycle_id)
                        .order_by(CycleInterval.start_utc, CycleInterval.seq)
                    ).all()
                    if cycle is not None else []
                )
                hit = _find_overlap(list(stored_rows), new_intervals)
                if hit is not None:
                    raise _overlap_error(cycle_id, hit[0], hit[1])

                merged_payload = [
                    _StoredIntervalPayload(
                        _parse_z(r.start_utc), _parse_z(r.end_utc),
                        r.timezone, Decimal(r.energy))
                    for r in stored_rows
                ]
                merged_payload.extend(
                    _StoredIntervalPayload(i.start, i.end, str(i.tz), i.energy)
                    for i in new_intervals
                )

                # 5) 用现有计价引擎对账期整体重算：累计阶梯贯穿所有批次。
                result = pricing.quote(version, merged_payload)

                # 6) 本批金额 = 重算后未舍入累计 − 重算前未舍入累计，
                #    差值再四舍五入到分（账期 total_amount 同样到分）。
                prev_total_amount = (
                    Decimal(cycle.total_amount) if cycle is not None else ZERO)
                new_total_amount = result.total_amount
                batch_amount = (new_total_amount - prev_total_amount).quantize(
                    Q2, rounding=ROUND_HALF_UP)

                new_revision = current_revision + 1
                batch_energy = sum((i.energy for i in new_intervals), ZERO)
                now = _now()

                body = {
                    "cycle_id": cycle_id,
                    "version": version.code,
                    "currency": version.currency,
                    "energy_unit": version.energy_unit,
                    "revision": new_revision,
                    "batch_amount": str(batch_amount),
                    "total_energy": str(result.total_energy),
                    "total_amount": str(new_total_amount.quantize(
                        Q2, rounding=ROUND_HALF_UP)),
                    "quote": build_quote_response(version, result).model_dump(),
                }

                if cycle is None:
                    # 首次提交：INSERT 主键冲突意味着并发请求已抢先创建账期。
                    session.add(BillingCycle(
                        cycle_id=cycle_id,
                        version_code=version.code,
                        revision=new_revision,
                        total_energy=str(result.total_energy),
                        total_amount=str(new_total_amount),
                        created_at=now, updated_at=now,
                    ))
                    session.flush()
                else:
                    # 条件推进 revision：仅当 revision 仍等于 expected 时
                    # 生效；rowcount==0 即被并发同 revision 请求抢先。
                    guarded = session.execute(
                        update(BillingCycle)
                        .where(BillingCycle.cycle_id == cycle_id,
                               BillingCycle.revision == req.expected_revision)
                        .values(revision=new_revision,
                                total_energy=str(result.total_energy),
                                total_amount=str(new_total_amount),
                                updated_at=now)
                    )
                    if guarded.rowcount != 1:
                        raise _revision_conflict(
                            cycle_id, req.expected_revision, current_revision)

                batch_row = UsageBatch(
                    cycle_id=cycle_id,
                    revision=new_revision,
                    idempotency_key=req.idempotency_key,
                    version_code=version.code,
                    content_digest=digest,
                    batch_energy=str(batch_energy),
                    batch_amount=str(batch_amount),
                    response_json=json.dumps(body, ensure_ascii=False),
                    created_at=now,
                )
                session.add(batch_row)
                session.flush()  # 取 batch_row.id，同时暴露幂等键唯一冲突

                next_seq = (
                    max((r.seq for r in stored_rows), default=-1) + 1
                )
                for i in sorted(new_intervals,
                                key=lambda i: (i.start, i.end)):
                    session.add(CycleInterval(
                        cycle_id=cycle_id,
                        revision=new_revision,
                        batch_id=batch_row.id,
                        start_utc=_utc_z(i.start),
                        end_utc=_utc_z(i.end),
                        timezone=str(i.tz),
                        energy=str(i.energy),
                        seq=next_seq,
                    ))
                    next_seq += 1

                return SubmitResult(body, False)

        except IntegrityError as exc:
            # 落库唯一/主键冲突（并发竞态）：只读重查归类为重放 /
            # 幂等冲突 / revision 冲突，绝不写任何数据。
            return _classify_integrity_error(
                eng, cycle_id, req, digest, exc)


def _classify_integrity_error(eng, cycle_id, req, digest, exc):
    with Session(eng) as session:
        batch = session.scalar(
            select(UsageBatch).where(
                UsageBatch.cycle_id == cycle_id,
                UsageBatch.idempotency_key == req.idempotency_key,
            )
        )
        if batch is not None:
            if batch.content_digest == digest:
                return SubmitResult(json.loads(batch.response_json), True)
            raise _idempotency_conflict(
                cycle_id, req.idempotency_key, batch.content_digest, digest)
        cycle = session.get(BillingCycle, cycle_id)
        if cycle is not None:
            raise _revision_conflict(
                cycle_id, req.expected_revision, cycle.revision) from exc
    raise exc


# --------------------------------------------------------------- read model


def get_cycle(cycle_id: str, *, engine=None) -> dict:
    """返回账期当前状态（含批次摘要与全部区间）；不存在抛 404。"""
    eng = engine or default_engine
    with Session(eng) as session:
        cycle = session.get(BillingCycle, cycle_id)
        if cycle is None:
            raise PricingError(
                f"账期不存在: {cycle_id}",
                code="cycle_not_found", status_code=404,
                details={"cycle_id": cycle_id})
        version = load_version(session, cycle.version_code)
        batches = session.scalars(
            select(UsageBatch)
            .where(UsageBatch.cycle_id == cycle_id)
            .order_by(UsageBatch.revision)
        ).all()
        intervals = session.scalars(
            select(CycleInterval)
            .where(CycleInterval.cycle_id == cycle_id)
            .order_by(CycleInterval.seq)
        ).all()
        return {
            "cycle_id": cycle.cycle_id,
            "version": cycle.version_code,
            "currency": version.currency,
            "energy_unit": version.energy_unit,
            "revision": cycle.revision,
            "total_energy": cycle.total_energy,
            "total_amount": str(Decimal(cycle.total_amount).quantize(Q2)),
            "created_at": cycle.created_at,
            "updated_at": cycle.updated_at,
            "batches": [
                {
                    "revision": b.revision,
                    "idempotency_key": b.idempotency_key,
                    "content_digest": b.content_digest,
                    "batch_energy": b.batch_energy,
                    "batch_amount": str(Decimal(b.batch_amount).quantize(Q2)),
                    "created_at": b.created_at,
                }
                for b in batches
            ],
            "intervals": [
                {
                    "seq": r.seq,
                    "revision": r.revision,
                    "start": r.start_utc,
                    "end": r.end_utc,
                    "timezone": r.timezone,
                    "energy": r.energy,
                }
                for r in intervals
            ],
        }
