"""最大需量（maximum demand）结算引擎。

对一组**不等长**、互不重叠的用量区间，按指定 IANA 时区本地午夜起算的
固定分钟边界（15/30/60 分钟）枚举需量窗口，沿 UTC 时间线递增推进，
取平均功率最高的窗口作为最大需量窗口。

边界语义
========

* 窗口按**每个本地日**重新对齐：当日本地午夜（00:00）的 UTC 时刻为起点，
  每隔 ``window_minutes`` 分钟沿 UTC 时间线取一个边界，直到**次日**本地
  午夜的 UTC 时刻为止。因此：

  - 普通日恰好得到 ``1440/N`` 个 N 分钟窗口；
  - 春季跳时日（本地 23 小时）得到较少窗口（如纽约 15 分钟档为 92 个），
    跳过的墙上时间内不产生窗口，也**不会**造出非存在的本地边界；
  - 秋季回拨日（本地 25 小时）得到较多窗口（如纽约 15 分钟档为 100 个），
    重复的本地墙上时间（如两个 ``01:15``）对应不同的 UTC 窗口，
    **分别枚举、分别参评**。

  所有标准夏令时跳变（1 小时 / 30 分钟 / 45 分钟等）下日长都是
  ``window_minutes`` 的整数倍；若某时区某日的 UTC 跨度无法整除（理论上的
  防御性场景），余下不足一个窗口的碎片以 ``incomplete_alignment``
  记为未参评。

参评与分摊
==========

* 只有被输入区间（首尾相接视为连续覆盖）**完整覆盖**的窗口才参评；
  仅部分落在输入并集内的窗口记 ``partial_coverage``，完全不与输入相交的
  记 ``not_covered``（内部缺口/前后边缘窗口）。
* 能量按窗口与输入区间的**实际 UTC 微秒时长**精确比例分摊：每段输入先
  按其各切片权重在六位小数上分摊，舍入残差并入该输入的最后一个切片，
  因此逐输入严格能量守恒；窗口能量为落在其中的各输入切片之和。
* 各参评窗口时长相同，平均功率（kW）与窗口能量同序；以**未舍入**的能量
  选最高值，同值时取 **UTC 起点最早**者。
* 需量（kW）与费用在六位小数上四舍五入计费；费用再按选中窗口内各输入的
  能量占比分摊到逐输入，残差并入最后一项以保持费用守恒。
* 结果提供两种逐输入视图：``allocations`` 只覆盖获选窗口内的费用分摊；
  ``interval_shares`` 按输入分组给出其在**全部**重叠窗口中的切片，
  每组能量之和严格等于该输入的申报能量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from .pricing import (
    Q6,
    ZERO,
    InputInterval,
    get_zone,
    parse_usage_intervals,
)

# All eligible windows share one fixed UTC duration (slivers are ineligible),
# so average power is strictly proportional to window energy: winners are
# picked on exact Decimal energies, never on a rounded quotient.

REASON_PARTIAL = "partial_coverage"
REASON_NOT_COVERED = "not_covered"
REASON_INCOMPLETE = "incomplete_alignment"
NO_WINDOW_CODE = "no_complete_window"


@dataclass(frozen=True)
class WindowSlice:
    """One input interval's exact-duration share inside a demand window."""
    interval_index: int
    start: datetime
    end: datetime
    microseconds: int
    energy: Decimal           # 6-dp prorated share (residual reconciled)


@dataclass
class DemandWindow:
    start: datetime           # UTC, half-open [start, end)
    end: datetime
    index: int = 0            # enumeration order along the UTC timeline
    slices: list[WindowSlice] = field(default_factory=list)
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def energy(self) -> Decimal:
        return sum((s.energy for s in self.slices), ZERO)


@dataclass
class DemandAllocation:
    interval_index: int
    start: datetime
    end: datetime
    microseconds: int
    energy: Decimal
    demand_kw: Decimal
    amount: Decimal


@dataclass(frozen=True)
class IntervalSlice:
    """One input interval's share inside one overlapping demand window."""
    window_index: int
    start: datetime
    end: datetime
    microseconds: int
    energy: Decimal           # 6-dp prorated share (residual reconciled)


@dataclass
class IntervalShare:
    """All of one input interval's slices across every overlapping window."""
    interval_index: int
    energy: Decimal           # exact slice sum == the input's declared energy
    slices: list[IntervalSlice]


@dataclass
class DemandResult:
    timezone: str
    window_minutes: int
    unit_price: Decimal
    windows: list[DemandWindow]
    winner: DemandWindow | None
    reason_code: str | None
    reason: str | None

    @property
    def window_count(self) -> int:
        return len(self.windows)

    @property
    def eligible_count(self) -> int:
        return sum(1 for w in self.windows if w.eligible)

    @property
    def demand_kw(self) -> Decimal:
        if self.winner is None:
            return ZERO
        hours = Decimal(self.window_minutes) / Decimal(60)
        return (self.winner.energy / hours).quantize(Q6,
                                                     rounding=ROUND_HALF_UP)

    @property
    def amount(self) -> Decimal:
        return (self.demand_kw * self.unit_price).quantize(
            Q6, rounding=ROUND_HALF_UP)

    @property
    def allocations(self) -> list[DemandAllocation]:
        """Energy/fee shares of the winning window per input interval.

        Amounts are split in proportion to each input's (unrounded-ratio)
        energy in the window; the rounding residual lands on the last
        contributing input so the shares sum exactly to ``amount``.
        """
        if self.winner is None:
            return []
        total = self.winner.energy
        hours = Decimal(self.window_minutes) / Decimal(60)
        ordered = sorted(self.winner.slices, key=lambda s: s.start)
        out: list[DemandAllocation] = []
        for sl in ordered[:-1]:
            share = (self.amount * sl.energy / total).quantize(
                Q6, rounding=ROUND_HALF_UP)
            out.append(DemandAllocation(
                interval_index=sl.interval_index,
                start=sl.start, end=sl.end, microseconds=sl.microseconds,
                energy=sl.energy,
                demand_kw=(sl.energy / hours).quantize(Q6,
                                                       rounding=ROUND_HALF_UP),
                amount=share,
            ))
        accounted = sum((a.amount for a in out), ZERO)
        last = ordered[-1]
        out.append(DemandAllocation(
            interval_index=last.interval_index,
            start=last.start, end=last.end, microseconds=last.microseconds,
            energy=last.energy,
            demand_kw=(last.energy / hours).quantize(Q6,
                                                     rounding=ROUND_HALF_UP),
            amount=self.amount - accounted,
        ))
        return out

    @property
    def interval_shares(self) -> list[IntervalShare]:
        """Per-input slices across ALL overlapping windows, grouped by
        ``interval_index`` in input order.

        Unlike ``allocations`` (winner window only), this view covers every
        window each input touches — eligible or not — so each group's slice
        energies sum exactly to that input's declared energy. Slices are
        chronological: windows are enumerated along the UTC timeline.
        """
        grouped: dict[int, list[IntervalSlice]] = {}
        for w in self.windows:
            for s in w.slices:
                grouped.setdefault(s.interval_index, []).append(IntervalSlice(
                    window_index=w.index, start=s.start, end=s.end,
                    microseconds=s.microseconds, energy=s.energy,
                ))
        return [IntervalShare(
            interval_index=idx,
            energy=sum((s.energy for s in slices), ZERO),
            slices=slices,
        ) for idx, slices in sorted(grouped.items())]


def to_utc_z(dt: datetime) -> str:
    """Format an aware UTC datetime with a trailing ``Z``."""
    return dt.isoformat().replace("+00:00", "Z")


def _micros(delta: timedelta) -> int:
    """Exact length of a timedelta in whole microseconds."""
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _distribute(total: Decimal, weights: list[int]) -> list[Decimal]:
    """Split ``total`` proportionally to ``weights`` at 6 dp; the residual
    lands on the final part so the parts sum exactly to ``total``."""
    whole = sum(weights)
    if whole <= 0:
        return [ZERO] * (len(weights) - 1) + [total]
    shares = [(total * Decimal(w) / Decimal(whole)).quantize(
        Q6, rounding=ROUND_HALF_UP) for w in weights[:-1]]
    shares.append(total - sum(shares, ZERO))
    return shares


def _aligned_windows(tz, first_day: date, last_day: date,
                     window_minutes: int,
                     envelope: tuple[datetime, datetime] | None = None
                     ) -> list[DemandWindow]:
    """Enumerate fixed-size UTC windows, re-anchored at each local midnight.

    Windows between the first and last covered local days are produced; when
    ``envelope`` is given only windows intersecting the half-open envelope
    are kept (indices are reassigned after filtering). The trailing piece of
    a non-divisible local day is kept as an ineligible ``sliver``
    (defensive: standard DST transition offsets never leave a remainder for
    15/30/60-minute grids)."""
    step = timedelta(minutes=window_minutes)
    windows: list[DemandWindow] = []
    d = first_day
    while d <= last_day:
        day_start = datetime.combine(d, time.min, tzinfo=tz).astimezone(
            timezone.utc)
        day_end = datetime.combine(d + timedelta(days=1), time.min,
                                   tzinfo=tz).astimezone(timezone.utc)
        cursor = day_start
        while cursor < day_end:
            nxt = cursor + step
            if nxt <= day_end:
                w = DemandWindow(start=cursor, end=nxt)
            else:
                w = DemandWindow(start=cursor, end=day_end,
                                 reasons=[REASON_INCOMPLETE])
            if envelope is None or (w.end > envelope[0]
                                    and w.start < envelope[1]):
                w.index = len(windows)
                windows.append(w)
            cursor = nxt
        d += timedelta(days=1)
    return windows


def _merge_components(inputs: list[InputInterval]
                      ) -> list[tuple[datetime, datetime]]:
    """Union of the (sorted, non-overlapping) inputs; abutting intervals join."""
    comps: list[tuple[datetime, datetime]] = []
    for iv in inputs:
        if comps and iv.start <= comps[-1][1]:
            comps[-1] = (comps[-1][0], max(comps[-1][1], iv.end))
        else:
            comps.append((iv.start, iv.end))
    return comps


def settle(req) -> DemandResult:
    """Settle maximum demand over the intervals carried by ``req``."""
    tz = get_zone(req.timezone)
    inputs = parse_usage_intervals(req.intervals)

    lo = min(i.start for i in inputs)
    hi = max(i.end for i in inputs)

    # Enumerate every aligned window touching the input envelope [lo, hi).
    first_day = lo.astimezone(tz).date()
    last_day = (hi - timedelta(microseconds=1)).astimezone(tz).date()
    envelope = (lo, hi)
    windows = _aligned_windows(tz, first_day, last_day, req.window_minutes,
                               envelope)
    components = _merge_components(inputs)

    # Classify eligibility against the merged coverage components. A full
    # window must lie inside a single component: disjoint components have a
    # positive gap between them (abutting ones were merged), so they cannot
    # jointly cover one window. A fresh two-pointer scan per window is fine
    # (component counts are tiny) and avoids stale-pointer misclassification.
    for w in windows:
        if REASON_INCOMPLETE in w.reasons:
            continue
        intersects = False
        fully = False
        for cs, ce in components:
            if cs >= w.end:
                break
            if ce <= w.start:
                continue
            if cs <= w.start and ce >= w.end:
                fully = True
                break
            intersects = True
        if fully:
            w.eligible = True
        elif intersects:
            w.reasons.append(REASON_PARTIAL)
        else:
            w.reasons.append(REASON_NOT_COVERED)

    # Prorate each input interval's energy across the windows it intersects,
    # by exact elapsed UTC microseconds; the per-input residual is absorbed
    # by that input's last slice so per-input energy is conserved exactly.
    for iv in inputs:
        hit = [w for w in windows if w.start < iv.end and w.end > iv.start]
        weights: list[int] = []
        for w in hit:
            a, b = max(w.start, iv.start), min(w.end, iv.end)
            weights.append(_micros(b - a))
        shares = _distribute(iv.energy, weights)
        for w, share in zip(hit, shares):
            a, b = max(w.start, iv.start), min(w.end, iv.end)
            if share > 0:
                w.slices.append(WindowSlice(
                    interval_index=iv.index, start=a, end=b,
                    microseconds=_micros(b - a), energy=share,
                ))

    eligible = [w for w in windows if w.eligible]
    winner: DemandWindow | None = None
    reason_code = reason = None
    if not eligible:
        reason_code = NO_WINDOW_CODE
        reason = ("没有任何需量窗口被输入区间完整覆盖；仅完整窗口参评，"
                  "未参评窗口及其原因见 excluded_windows")
    else:
        # Highest average power == highest window energy (identical fixed
        # durations); exact Decimal compare, tie -> earliest UTC start.
        winner = min(eligible, key=lambda w: (-w.energy, w.start))

    return DemandResult(
        timezone=str(tz),
        window_minutes=req.window_minutes,
        unit_price=req.unit_price,
        windows=windows,
        winner=winner,
        reason_code=reason_code,
        reason=reason,
    )
