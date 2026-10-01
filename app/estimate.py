"""用量反推：账单金额已知、电表用量缺失时的用量区间估计。

场景：电表用量缺失，但账单金额已知。假定区间内**功率恒定**——能量按
实际 UTC 时长比例分布，与 ``/quote`` 对单段区间的分摊方式完全一致——
在 0.000001 能量单位的离散网格上，反推哪些用量会使 ``/quote`` 的账单
金额（分段六位小数合计后再四舍五入到分）恰好等于目标金额。

金额函数 f(E) 关于用量 E 单调不减（各档单价非负），目标金额对应的解集
因此是一段连续的"平台"。用 Decimal 二分分别定位平台的左右边界：

* 左边界：第一个使 f(E) >= billed 的网格点；
* 右边界：最后一个使 f(E) <= billed 的网格点（平台上必有 f == billed）。

零用量视为零费用（``/quote`` 不接受 0 能量，这里特判 f(0) = 0）。

无解的两种形态（响应用 ``reason_code`` 区分）：

* ``amount_above_reachable``：f(max_energy) < billed，目标金额超过
  可达上界；返回上界网格点及其账单金额；
* ``rounding_jump``：f 在相邻两个网格点之间跳过目标值（单价极高时
  一个网格步进就会越过 1 分）；返回夹住目标的相邻网格点及其金额。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from .demand import to_utc_z
from .pricing import (
    Q2,
    Q6,
    ZERO,
    LoadedVersion,
    PricingError,
    QuoteResult,
    ensure_valid,
    get_zone,
    parse_local_datetime,
    quote,
)
from .schemas import QuoteIntervalIn

GRID = Q6  # 0.000001 能量单位：用量搜索的离散网格

REASON_ABOVE_REACHABLE = "amount_above_reachable"
REASON_ROUNDING_JUMP = "rounding_jump"


@dataclass(frozen=True)
class GridPoint:
    """一个网格用量及其对应的账单金额（分）。"""
    energy: Decimal
    amount: Decimal


@dataclass
class EstimateResult:
    billed_amount: Decimal
    solvable: bool
    min_energy: Decimal | None = None      # 平台左边界（最小用量）
    max_energy: Decimal | None = None      # 平台右边界（最大用量，≤ 请求上界）
    min_quote: QuoteResult | None = None   # None 表示该端为零用量
    max_quote: QuoteResult | None = None
    matched_rules: list[str] = field(default_factory=list)
    reason_code: str | None = None
    reason: str | None = None
    lower: GridPoint | None = None         # 无解时夹住目标的下侧网格点
    upper: GridPoint | None = None         # 无解时夹住目标的上侧网格点
    quotes_evaluated: int = 0


class _BillFunction:
    """账单金额函数 f(E)（带记忆化）：零用量视为零费用，正用量复用 /quote。

    返回值是 ``(账单金额, QuoteResult)`` 二元组；零用量没有计价结果，
    以 ``(0.00, None)`` 表示。网格点求值结果会被缓存，左右边界的两次
    二分共享同一份缓存，端点不会重复报价。
    """

    def __init__(self, version: LoadedVersion, start, end, tz_name: str):
        self._version = version
        self._start = to_utc_z(start)
        self._end = to_utc_z(end)
        self._tz_name = tz_name
        self._cache: dict[Decimal, tuple[Decimal, QuoteResult]] = {}
        self.evaluations = 0

    def __call__(self, energy: Decimal) -> tuple[Decimal, QuoteResult | None]:
        if energy == 0:
            return ZERO.quantize(Q2), None
        hit = self._cache.get(energy)
        if hit is not None:
            return hit
        payload = [QuoteIntervalIn(
            start=self._start, end=self._end,
            timezone=self._tz_name, energy=energy,
        )]
        result = quote(self._version, payload, validate=False)
        amount = result.total_amount.quantize(Q2, rounding=ROUND_HALF_UP)
        self._cache[energy] = (amount, result)
        self.evaluations += 1
        return amount, result


def _grid_mid(lo: Decimal, hi: Decimal) -> Decimal:
    """(lo, hi) 内按下取整对齐到网格的中点；调用前需 ``hi - lo > GRID``，
    此时返回值严格大于 ``lo``（保证二分收敛）。"""
    return ((lo + hi) / 2 / GRID).to_integral_value(
        rounding=ROUND_FLOOR) * GRID


def _bisect_first_ge(bill: _BillFunction, billed: Decimal,
                     ceiling: Decimal) -> Decimal:
    """第一个使 f(E) >= billed 的网格点。前置：f(0) < billed <= f(ceiling)。"""
    lo, hi = ZERO, ceiling
    while hi - lo > GRID:
        mid = _grid_mid(lo, hi)
        amount, _ = bill(mid)
        if amount >= billed:
            hi = mid
        else:
            lo = mid
    return hi


def _bisect_last_le(bill: _BillFunction, billed: Decimal, left: Decimal,
                    ceiling: Decimal) -> Decimal:
    """最后一个使 f(E) <= billed 的网格点。前置：f(left) <= billed < f(ceiling)。"""
    lo, hi = left, ceiling
    while hi - lo > GRID:
        mid = _grid_mid(lo, hi)
        amount, _ = bill(mid)
        if amount <= billed:
            lo = mid
        else:
            hi = mid
    return lo


def estimate(version: LoadedVersion, req) -> EstimateResult:
    """二分反推使账单金额等于 ``req.billed_amount`` 的用量平台。"""
    # 结构性规则错误（重叠/阶梯缺口）与 /quote 一样直接 409。
    ensure_valid(version)
    tz = get_zone(req.interval.timezone)
    start = parse_local_datetime(req.interval.start, tz)
    end = parse_local_datetime(req.interval.end, tz)
    if end <= start:
        raise PricingError(
            "用量估计区间必须满足 end > start（半开区间）",
            code="invalid_interval", status_code=400,
        )
    billed = req.billed_amount
    ceiling = req.max_energy

    bill = _BillFunction(version, start, end, str(tz))
    top_amount, _ = bill(ceiling)
    if top_amount < billed:
        return EstimateResult(
            billed_amount=billed, solvable=False,
            reason_code=REASON_ABOVE_REACHABLE,
            reason=(f"用量达到上界 {ceiling} 时账单金额仅为 {top_amount}，"
                    f"仍低于目标金额 {billed}：目标超过可达上界"),
            lower=GridPoint(ceiling, top_amount),
            quotes_evaluated=bill.evaluations,
        )

    # 左边界：第一个 f(E) >= billed 的网格点（billed 为 0 时即零用量）。
    left = ZERO if billed == 0 else _bisect_first_ge(bill, billed, ceiling)
    left_amount, left_quote = bill(left)
    if left_amount != billed:
        below = left - GRID
        below_amount, _ = bill(below)
        return EstimateResult(
            billed_amount=billed, solvable=False,
            reason_code=REASON_ROUNDING_JUMP,
            reason=(f"账单金额在相邻网格点 {below}（{below_amount}）与 "
                    f"{left}（{left_amount}）之间跳过目标金额 {billed}："
                    "没有任何网格用量能使账单金额等于目标"),
            lower=GridPoint(below, below_amount),
            upper=GridPoint(left, left_amount),
            quotes_evaluated=bill.evaluations,
        )

    # 右边界：最后一个 f(E) <= billed 的网格点；平台上必有 f == billed。
    right = (ceiling if top_amount == billed
             else _bisect_last_le(bill, billed, left, ceiling))
    _, right_quote = bill(right)

    matched: list[str] = []
    for result in (left_quote, right_quote):
        if result is None:
            continue
        for code in result.matched_rules:
            if code not in matched:
                matched.append(code)

    return EstimateResult(
        billed_amount=billed, solvable=True,
        min_energy=left, max_energy=right,
        min_quote=left_quote, max_quote=right_quote,
        matched_rules=matched,
        quotes_evaluated=bill.evaluations,
    )
