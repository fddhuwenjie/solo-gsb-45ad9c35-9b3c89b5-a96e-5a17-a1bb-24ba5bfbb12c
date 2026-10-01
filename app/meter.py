"""累计表计读数对账：把计数器读数整理成可直接提交 /quote 的用量区间。

输入是一组带 UTC 偏移的累计计数器读数（计数器按 ``modulus`` 回卷）、
正整数秒的最大采样间隔，以及可选的计数器 reset 时刻。处理流程：

1. **解析与范围校验**：每条读数的时间必须携带 UTC 偏移量（``Z`` 或
   ``±HH:MM``），统一换算成 UTC；读数值必须落在 ``[0, modulus)`` 内。
2. **排序与去重**：按 UTC 时刻升序排序（稳定排序，保留输入顺序）；同一
   时刻出现多条读数时，值相同去重（保留输入顺序最靠前的一条及其原始
   下标），值不同直接报错（``conflicting_readings``）。
3. **reset 断列**：每个 reset 时刻必须命中某条读数；命中处序列断开，
   即“通向 reset 读数”的相邻读数对只记为 gap（``reset``），不产生用量；
   reset 读数本身是新一段连续序列的起点。
4. **采样间隔**：相邻读数的间隔超过 ``max_interval_seconds`` 时同样只记
   为 gap（``max_interval_exceeded``）——期间用量不可知，不得估算。
5. **用量区间**：其余相邻读数对生成 UTC 半开区间 ``[start, end)``，用量
   为计数器差值；出现下降时按**一次** modulus 回卷计算
   （``delta = cur - prev + modulus``，读数都在 ``[0, modulus)`` 内，一次
   回卷必然足够）并打上 ``wrapped`` 标记。差值为 0 的静止读数对不产生
   区间（``/quote`` 要求能量为正），也不算 gap。

对账不变量：任一连续段（不被 reset / 采样间隔打断的最大读数序列）内，
所有区间用量之和 == 该段末读数 − 首读数 + modulus × 回卷次数。所有用量
都是精确 ``Decimal``（不做舍入），因此 ``total_energy`` 与各区间用量之
和严格相等。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .pricing import ZERO, PricingError

GAP_RESET = "reset"
GAP_MAX_INTERVAL = "max_interval_exceeded"


def to_utc_z(dt: datetime) -> str:
    """Format an aware UTC datetime with a trailing ``Z``."""
    return dt.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Reading:
    index: int                 # 原始输入下标（0 起）
    at: datetime               # UTC
    value: Decimal


@dataclass(frozen=True)
class UsageInterval:
    start: datetime            # UTC，半开区间 [start, end)
    end: datetime
    from_index: int            # 起点读数的原始下标
    to_index: int              # 终点读数的原始下标
    wrapped: bool              # 是否按一次 modulus 回卷计算
    energy: Decimal            # 精确用量（未舍入）


@dataclass(frozen=True)
class ReadingGap:
    start: datetime            # UTC，半开区间 [start, end)
    end: datetime
    from_index: int
    to_index: int
    reason: str                # reset | max_interval_exceeded


@dataclass
class ReconcileResult:
    modulus: Decimal
    reading_count: int         # 去重后的读数条数
    duplicate_count: int       # 被去重丢弃的重复读数条数
    intervals: list[UsageInterval] = field(default_factory=list)
    gaps: list[ReadingGap] = field(default_factory=list)

    @property
    def total_energy(self) -> Decimal:
        return sum((i.energy for i in self.intervals), ZERO)


def _parse_instant(value: str, *, field: str, index: int) -> datetime:
    """Parse an ISO-8601 datetime that *must* carry a UTC offset -> UTC."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingError(
            f"{field}[{index}] 的时间格式无效: {value}",
            code="invalid_datetime", status_code=400,
            details={"field": field, "index": index, "value": value},
        ) from exc
    if dt.tzinfo is None:
        raise PricingError(
            f"{field}[{index}] 必须携带 UTC 偏移量（如 Z / +08:00）",
            code="offset_required", status_code=400,
            details={"field": field, "index": index, "value": value},
        )
    return dt.astimezone(timezone.utc)


def reconcile(req) -> ReconcileResult:
    """Reconcile cumulative counter readings into usage intervals + gaps."""
    modulus: Decimal = req.modulus
    max_interval = timedelta(seconds=req.max_interval_seconds)

    readings: list[Reading] = []
    for idx, item in enumerate(req.readings):
        at = _parse_instant(item.at, field="readings", index=idx)
        value = item.value
        if not ZERO <= value < modulus:
            raise PricingError(
                f"readings[{idx}] 的读数值 {value} 不在 [0, modulus) 范围内"
                f"（modulus={modulus}）",
                code="reading_out_of_range", status_code=400,
                details={"index": idx, "value": str(value),
                         "modulus": str(modulus)},
            )
        readings.append(Reading(index=idx, at=at, value=value))

    # 稳定排序：同一时刻保留输入顺序，去重时自然留下最靠前的原始下标。
    readings.sort(key=lambda r: r.at)

    deduped: list[Reading] = []
    duplicate_count = 0
    for r in readings:
        if deduped and deduped[-1].at == r.at:
            prev = deduped[-1]
            if prev.value != r.value:
                raise PricingError(
                    f"下标 {prev.index} 与 {r.index} 的读数处于同一时刻 "
                    f"{to_utc_z(r.at)}，但数值不一致"
                    f"（{prev.value} vs {r.value}）",
                    code="conflicting_readings", status_code=400,
                    details={
                        "at": to_utc_z(r.at),
                        "indices": [prev.index, r.index],
                        "values": [str(prev.value), str(r.value)],
                    },
                )
            duplicate_count += 1  # 同一时刻同一数值：重复上报，去重
            continue
        deduped.append(r)

    instants = {r.at for r in deduped}
    resets: set[datetime] = set()
    for i, raw in enumerate(req.resets):
        inst = _parse_instant(raw, field="resets", index=i)
        if inst not in instants:
            raise PricingError(
                f"resets[{i}] 的时刻 {to_utc_z(inst)} 没有命中任何读数",
                code="reset_not_on_reading", status_code=400,
                details={"index": i, "at": to_utc_z(inst)},
            )
        resets.add(inst)

    result = ReconcileResult(
        modulus=modulus,
        reading_count=len(deduped),
        duplicate_count=duplicate_count,
    )
    for prev, cur in zip(deduped, deduped[1:]):
        if cur.at in resets:
            # reset 处序列断开：跨越 reset 的区间用量不可知，只记 gap；
            # reset 读数是新一段连续序列的起点。
            result.gaps.append(ReadingGap(
                start=prev.at, end=cur.at,
                from_index=prev.index, to_index=cur.index,
                reason=GAP_RESET,
            ))
            continue
        if cur.at - prev.at > max_interval:
            # 采样间隔超限：期间用量不可知，只记 gap，不得估算。
            result.gaps.append(ReadingGap(
                start=prev.at, end=cur.at,
                from_index=prev.index, to_index=cur.index,
                reason=GAP_MAX_INTERVAL,
            ))
            continue
        delta = cur.value - prev.value
        wrapped = delta < 0
        if wrapped:
            delta += modulus
        if delta <= 0:
            # 读数静止（差值为 0）：无用量，不产生区间，也不算 gap。
            continue
        result.intervals.append(UsageInterval(
            start=prev.at, end=cur.at,
            from_index=prev.index, to_index=cur.index,
            wrapped=wrapped, energy=delta,
        ))
    return result
