"""Shift-planning engine for two time-shiftable power tasks.

Each task supplies an earliest start and a latest finish (both with explicit
UTC offsets), an integer-minute duration and a positive energy amount. Start
candidates are enumerated on a 30-minute UTC grid anchored at each task's
earliest start; a task must finish strictly before its latest finish, and the
two tasks must not overlap.

Every non-overlapping candidate pair is priced **together** with the regular
:func:`pricing.quote` engine — the two intervals are handed over in one call
(the engine itself processes them in start order), so cumulative tiers are
shared across the tasks instead of each task being quoted independently.
A rule *gap* only invalidates the candidate pair that hits it
(``rule_gap``), whereas structural rule errors (overlapping rules / tier
gaps) propagate immediately from ``quote``.

The cheapest pair is chosen on the unrounded (6 dp per-segment) total
amount; ties break by the lexicographically earliest UTC start tuple **in
input order** (task 0, then task 1).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .pricing import (
    PricingError,
    ensure_valid,
    get_zone,
    parse_local_datetime,
    quote,
)
from .schemas import QuoteIntervalIn

STEP = timedelta(minutes=30)
MAX_WINDOW = timedelta(hours=8)


@dataclass(frozen=True)
class ShiftTask:
    index: int
    earliest_start: datetime          # UTC
    latest_end: datetime              # UTC
    duration: timedelta
    energy: Decimal
    tz: object                         # ZoneInfo


@dataclass
class PlanResult:
    tasks: tuple[ShiftTask, ShiftTask]
    feasible: bool
    best_starts: tuple[datetime, datetime] | None
    result: object | None             # pricing.QuoteResult when feasible
    candidates_evaluated: int = 0
    non_overlapping_pairs: int = 0
    rule_gap_pairs: int = 0
    reason_code: str | None = None
    reason: str | None = None
    gaps: list[dict] | None = None


def to_utc_z(dt: datetime) -> str:
    """Format an aware UTC datetime with a trailing ``Z``."""
    return dt.isoformat().replace("+00:00", "Z")


def _parse_zoned(value: str, field: str, tz, index: int) -> datetime:
    """Parse an ISO-8601 datetime that *must* carry a UTC offset."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingError(
            f"任务 {index} 的 {field} 时间格式无效: {value}",
            code="invalid_datetime", status_code=400,
            details={"task_index": index, "field": field, "value": value},
        ) from exc
    if dt.tzinfo is None:
        raise PricingError(
            f"任务 {index} 的 {field} 必须携带 UTC 偏移量（如 -04:00 / Z）",
            code="offset_required", status_code=400,
            details={"task_index": index, "field": field, "value": value},
        )
    # Normalise through the shared helper so the same conversion semantics apply.
    return parse_local_datetime(value, tz)


def parse_tasks(req, tz) -> tuple[ShiftTask, ShiftTask]:
    """Validate the request payload into two :class:`ShiftTask` values."""
    tasks: list[ShiftTask] = []
    for idx, item in enumerate(req.tasks):
        earliest = _parse_zoned(item.earliest_start, "earliest_start", tz, idx)
        latest = _parse_zoned(item.latest_end, "latest_end", tz, idx)
        if latest <= earliest:
            raise PricingError(
                f"任务 {idx} 的 latest_end 必须晚于 earliest_start（半开区间）",
                code="invalid_interval", status_code=400,
                details={"task_index": idx},
            )
        if latest - earliest > MAX_WINDOW:
            raise PricingError(
                f"任务 {idx} 的可用窗口超过 8 小时上限",
                code="window_too_large", status_code=400,
                details={
                    "task_index": idx,
                    "window_minutes": int(
                        (latest - earliest).total_seconds() // 60),
                },
            )
        duration = timedelta(minutes=item.duration_minutes)
        if duration > latest - earliest:
            raise PricingError(
                f"任务 {idx} 的持续时长 {item.duration_minutes} 分钟超过其"
                "可用窗口，任何开机时刻都无法在最晚完工前结束",
                code="duration_exceeds_window", status_code=400,
                details={"task_index": idx},
            )
        tasks.append(ShiftTask(
            index=idx, earliest_start=earliest, latest_end=latest,
            duration=duration, energy=item.energy, tz=tz,
        ))
    return tuple(tasks)  # type: ignore[return-value]


def candidate_starts(task: ShiftTask) -> list[datetime]:
    """30-minute UTC grid anchored at ``earliest_start``; the run must end
    strictly before ``latest_end`` (half-open)."""
    out: list[datetime] = []
    deadline = task.latest_end - task.duration
    cursor = task.earliest_start
    while cursor <= deadline:
        out.append(cursor)
        cursor += STEP
    return out


def _overlap(a_start: datetime, a_end: datetime,
             b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _payload(tz, s0: datetime, e0: datetime, energy0: Decimal,
             s1: datetime, e1: datetime, energy1: Decimal
             ) -> list[QuoteIntervalIn]:
    """Build the engine payload in *input* order (task 0 then task 1); the
    engine sorts intervals by start itself, and the payload position is the
    ``interval_index`` stamped on every priced segment."""
    tz_name = str(tz)
    return [
        QuoteIntervalIn(start=to_utc_z(s0), end=to_utc_z(e0),
                        timezone=tz_name, energy=energy0),
        QuoteIntervalIn(start=to_utc_z(s1), end=to_utc_z(e1),
                        timezone=tz_name, energy=energy1),
    ]


def plan_shift(version, req) -> PlanResult:
    """Enumerate every legal pair and return the cheapest feasible plan."""
    # Structural rule errors (overlap / tier gap / invalid rule) are a hard
    # failure, independent of any particular candidate.
    ensure_valid(version)
    tz = get_zone(req.timezone)
    tasks = parse_tasks(req, tz)

    starts = [candidate_starts(t) for t in tasks]
    if not starts[0] or not starts[1]:
        return PlanResult(
            tasks=tasks, feasible=False, best_starts=None, result=None,
            reason_code="no_candidate_start",
            reason="至少一项任务在其可用窗口内没有满足网格约束的开机时刻"
            "（最早开机 + 30 分钟整数倍后无法在最晚完工前结束）",
            gaps=[],
        )

    best_amount: Decimal | None = None
    best_pair: tuple[datetime, datetime] | None = None
    best_result = None
    evaluated = 0
    non_overlapping = 0
    gap_pairs = 0
    gaps: list[dict] = []
    for s0 in starts[0]:
        e0 = s0 + tasks[0].duration
        for s1 in starts[1]:
            e1 = s1 + tasks[1].duration
            evaluated += 1
            if _overlap(s0, e0, s1, e1):
                continue
            non_overlapping += 1
            pair = (s0, s1)

            result = None
            try:
                # One joint quote: cumulative tiers are shared across tasks.
                # Structural validation already ran once in plan_shift, so
                # skip the per-call full-year check for the 256+ candidates.
                result = quote(version, _payload(
                    tz, s0, e0, tasks[0].energy, s1, e1, tasks[1].energy),
                    validate=False)
            except PricingError as exc:
                if exc.code != "rule_gap":
                    raise
                gap_pairs += 1
                if len(gaps) < 5:
                    detail = exc.details or {}
                    gaps.append({
                        "starts": [to_utc_z(s0), to_utc_z(s1)],
                        "at": detail.get("at"),
                    })
                continue

            amount = result.total_amount
            if best_amount is None or (amount, pair) < (best_amount, best_pair):
                best_amount, best_pair, best_result = amount, pair, result

    if best_result is None:
        if non_overlapping == 0:
            reason_code = "no_non_overlapping_pair"
            reason = ("两项任务的所有候选开机时刻组合都发生重叠，"
                      "不存在互不重叠的排程")
        else:
            reason_code = "all_pairs_rule_gap"
            reason = (f"共评估 {evaluated} 组候选，其中 {non_overlapping} 组"
                      "互不重叠，但每组都在计价区间内遇到规则缺口"
                      "（存在任何规则都不覆盖的时刻），无可行组合")
        return PlanResult(
            tasks=tasks, feasible=False, best_starts=None, result=None,
            candidates_evaluated=evaluated,
            non_overlapping_pairs=non_overlapping,
            rule_gap_pairs=gap_pairs,
            reason_code=reason_code, reason=reason, gaps=gaps,
        )

    return PlanResult(
        tasks=tasks, feasible=True, best_starts=best_pair,
        result=best_result, candidates_evaluated=evaluated,
        non_overlapping_pairs=non_overlapping, rule_gap_pairs=gap_pairs,
        gaps=gaps,
    )
