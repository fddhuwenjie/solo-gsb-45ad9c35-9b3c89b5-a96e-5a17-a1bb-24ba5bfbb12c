"""共享电表阶梯电费的公平拆分（Shapley 值）。

一块总表后挂着多个用能方（成员）：各方申报自己的用量区间（与
``/quote`` 相同的区间格式），**成员内部区间不得重叠，成员之间允许重叠**
——重叠时刻的负荷本就该合并计量。费用拆分分三步：

1. **原子段切分**：取全部成员全部区间的 UTC 端点（start/end）并集，沿
   UTC 时间线切成互不重叠的原子半开段 ``[a, b)``。
2. **按实际时长分摊 + 同段合并**：每名成员的每段用量按其与原子段的
   **实际 UTC 微秒时长**比例摊到所属原子段（六位小数，舍入残差并入该
   输入区间的最后一个原子段，逐成员能量严格守恒）；任意成员联盟在某
   原子段的负荷 = 联盟内成员分摊能量之和。
3. **复用计价引擎报价**：把联盟合并后的各原子段（零负荷段跳过）作为
   互不重叠、可首尾相接的区间交给 :func:`pricing.quote`，累计阶梯因此
   **跨成员、跨区间共享**，得到该联盟的六位小数费用 ``v(S)``。

对 2～6 名成员精确枚举全部 ``2^n`` 个联盟（空联盟 ``v({}) = 0``），成员
``i`` 的归属额取其在所有 ``n!`` 种加入顺序中边际增费
``v(S∪{i}) − v(S)`` 的算术平均，即合作博弈论中的 **Shapley 值**
（``n ≤ 6``，``6! = 720``，直接精确枚举）。

归属金额按输入顺序量化到六位小数（四舍五入），**最后一名取全体合并
费用减去前项之和**——因此各成员归属金额严格合计为全体合并报价，分文
不差。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from itertools import permutations
from math import factorial

from .pricing import (
    Q6,
    ZERO,
    InputInterval,
    LoadedVersion,
    PricingError,
    ensure_valid,
    get_zone,
    parse_local_datetime,
    quote,
)
from .schemas import QuoteIntervalIn
from .demand import to_utc_z


@dataclass(frozen=True)
class AllocMember:
    """一名成员在原子段网格上的负荷画像。"""
    index: int
    member_id: str
    energy: Decimal                 # 申报总量（精确值；各原子段之和严格相等）
    grid: tuple[Decimal, ...]       # 与原子段对齐的逐段能量，未覆盖处为 0


@dataclass(frozen=True)
class MemberShare:
    """响应中一名成员的对账行。"""
    index: int
    member_id: str
    energy: Decimal
    standalone_amount: Decimal      # v({i})：单独报价（六位小数）
    allocated_amount: Decimal       # Shapley 归属额（六位小数）

    @property
    def difference(self) -> Decimal:
        """独立报价 − 归属金额（正=拆分后少付，负=为阶梯外溢多承担）。"""
        return self.standalone_amount - self.allocated_amount


@dataclass
class AllocationResult:
    version: LoadedVersion
    members: list[MemberShare]
    total_energy: Decimal
    total_amount: Decimal           # v(N)：全体合并报价（六位小数）
    coalitions_evaluated: int


def _micros(delta) -> int:
    """Exact length of a timedelta in whole microseconds."""
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _distribute(total: Decimal, weights: list[int]) -> list[Decimal]:
    """Split ``total`` proportionally to ``weights`` at 6 dp; the rounding
    residual lands on the final part so the parts sum to ``total`` exactly."""
    whole = sum(weights)
    if whole <= 0:  # zero-length window: nothing to prorate, keep total on last
        return [ZERO] * (len(weights) - 1) + [total]
    shares = [(total * Decimal(w) / Decimal(whole)).quantize(
        Q6, rounding=ROUND_HALF_UP) for w in weights[:-1]]
    shares.append(total - sum(shares, ZERO))
    return shares


def _parse_member(member) -> list[InputInterval]:
    """Parse one member's intervals; overlap is forbidden *within* a member."""
    parsed: list[InputInterval] = []
    for j, item in enumerate(member.intervals):
        tz = get_zone(item.timezone)
        start = parse_local_datetime(item.start, tz)
        end = parse_local_datetime(item.end, tz)
        if end <= start:
            raise PricingError(
                f"成员 {member.member_id} 的第 {j} 段用量区间必须满足 "
                "end > start（半开区间）",
                code="invalid_interval", status_code=400,
                details={"member_id": member.member_id, "interval_index": j},
            )
        parsed.append(InputInterval(start, end, item.energy_decimal(), tz, j))

    parsed.sort(key=lambda x: (x.start, x.end))
    for k in range(1, len(parsed)):
        if parsed[k].start < parsed[k - 1].end:
            raise PricingError(
                f"成员 {member.member_id} 的第 {parsed[k - 1].index} 段与第 "
                f"{parsed[k].index} 段用量区间重叠；同一成员的区间不得重叠"
                "（不同成员之间允许重叠）",
                code="interval_overlap", status_code=400,
                details={
                    "member_id": member.member_id,
                    "intervals": [parsed[k - 1].index, parsed[k].index],
                },
            )
    return parsed


def _member_grid(intervals: list[InputInterval],
                 points: list[datetime],
                 windows: list[tuple[datetime, datetime]]
                 ) -> tuple[Decimal, tuple[Decimal, ...]]:
    """Prorate one member's intervals onto the shared atomic-window grid."""
    load: dict[tuple[datetime, datetime], Decimal] = {}
    total = ZERO
    for iv in intervals:
        cuts = [iv.start]
        cuts.extend(p for p in points if iv.start < p < iv.end)
        cuts.append(iv.end)
        pieces = list(zip(cuts, cuts[1:]))
        weights = [_micros(b - a) for a, b in pieces]
        shares = _distribute(iv.energy, weights)
        total += iv.energy
        for (a, b), energy in zip(pieces, shares):
            # Within one member intervals never overlap (abutting allowed), so
            # every atomic window receives exactly one contribution.
            load[(a, b)] = energy
    grid = tuple(load.get(w, ZERO) for w in windows)
    return total, grid


def _coalition_cost(version: LoadedVersion, grids,
                    windows: list[tuple[datetime, datetime]],
                    mask: int) -> Decimal:
    """Merge the coalition members' loads per atomic window and price once."""
    payload: list[QuoteIntervalIn] = []
    for wi, (a, b) in enumerate(windows):
        energy = ZERO
        for mi, grid in enumerate(grids):
            if mask & (1 << mi):
                energy += grid[wi]
        if energy > 0:
            payload.append(QuoteIntervalIn(
                start=to_utc_z(a), end=to_utc_z(b),
                timezone="UTC", energy=energy,
            ))
    # Windows are disjoint atomic half-open pieces (abutting is allowed); one
    # joint quote keeps cumulative tiers shared across every member/window.
    return quote(version, payload, validate=False).total_amount


def allocate(version: LoadedVersion, req) -> AllocationResult:
    """Enumerate every coalition and settle Shapley cost shares."""
    # Structural rule errors (overlap / tier gap / invalid rule) are a hard
    # failure, independent of any particular coalition.
    ensure_valid(version)

    seen_ids: set[str] = set()
    for m in req.members:
        if m.member_id in seen_ids:
            raise PricingError(
                f"成员 ID 重复: {m.member_id}",
                code="duplicate_member", status_code=400,
                details={"member_id": m.member_id},
            )
        seen_ids.add(m.member_id)

    parsed_members = [
        (m.member_id, _parse_member(m)) for m in req.members
    ]

    # Atomic partition: the union of every member's UTC endpoints.
    points = sorted({
        p for _, intervals in parsed_members
        for iv in intervals
        for p in (iv.start, iv.end)
    })
    windows = list(zip(points, points[1:]))

    members: list[AllocMember] = []
    grids: list[tuple[Decimal, ...]] = []
    total_energy = ZERO
    for i, (member_id, intervals) in enumerate(parsed_members):
        energy, grid = _member_grid(intervals, points, windows)
        grids.append(grid)
        total_energy += energy
        members.append(AllocMember(i, member_id, energy, grid))

    n = len(members)
    full_mask = (1 << n) - 1

    # Exact coalition costs via the reused pricing engine; the empty coalition
    # costs nothing.
    costs: dict[int, Decimal] = {0: ZERO}
    for mask in range(1, full_mask + 1):
        costs[mask] = _coalition_cost(version, grids, windows, mask)
    total_amount = costs[full_mask]

    # Shapley value: average marginal increase over every joining order.
    # Costs are 6-dp Decimals; keep the combinatorial average as an exact
    # Fraction so quantization happens once, at the very end.
    marginals: list[Fraction] = [Fraction(0) for _ in range(n)]
    for order in permutations(range(n)):
        mask = 0
        for i in order:
            new_mask = mask | (1 << i)
            marginals[i] += Fraction(costs[new_mask] - costs[mask])
            mask = new_mask
    orders = factorial(n)
    exact_shares = [s / orders for s in marginals]

    # Quantize in input order; the last member absorbs the rounding residual
    # so allocated shares sum EXACTLY to the merged quote of all members.
    allocated: list[Decimal] = []
    for i in range(n - 1):
        value = Decimal(exact_shares[i].numerator) / Decimal(
            exact_shares[i].denominator)
        allocated.append(value.quantize(Q6, rounding=ROUND_HALF_UP))
    allocated.append(total_amount - sum(allocated, ZERO))

    shares = [
        MemberShare(
            index=m.index, member_id=m.member_id, energy=m.energy,
            standalone_amount=costs[1 << m.index],
            allocated_amount=allocated[m.index],
        )
        for m in members
    ]
    return AllocationResult(
        version=version, members=shares, total_energy=total_energy,
        total_amount=total_amount, coalitions_evaluated=1 << n,
    )
