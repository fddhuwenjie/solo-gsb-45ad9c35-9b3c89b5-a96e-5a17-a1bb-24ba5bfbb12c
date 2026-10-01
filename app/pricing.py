"""Core time-of-use tiered pricing engine.

Conventions
-----------
* All wall-clock time windows are half-open ``[start, end)``.
* All internal instants are timezone-aware UTC datetimes.
* Money and energy use :class:`decimal.Decimal` end to end.
* Each rule's tiers are cumulative: tier 1 applies to the first
  ``cumulative_cap`` units *of cumulative usage across the whole quote*,
  tier 2 from there up to its (larger) cap, and so on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .database import Holiday, RateRule, TariffVersion, Tier

ZERO = Decimal("0")
DAY = timedelta(days=1)
Q6 = Decimal("0.000001")  # quantize energy and line amounts to 6 dp
Q2 = Decimal("0.01")      # quantize totals to cents


class PricingError(Exception):
    """Error carrying an HTTP-ish status code and a machine-readable code."""

    def __init__(self, message: str, *, code: str = "pricing_error",
                 status_code: int = 422, details: dict | list | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details


# --------------------------------------------------------------------- models


@dataclass(frozen=True)
class LoadedTier:
    ordinal: int
    cumulative_cap: Decimal | None  # None == unbounded (final tier)
    unit_price: Decimal


@dataclass(frozen=True)
class LoadedRule:
    code: str
    name: str
    effective_from: datetime          # naive, in `tz`
    effective_to: datetime            # naive, in `tz`
    tz: ZoneInfo
    weekdays: frozenset[int]          # ISO: 1=Mon..7=Sun; empty == any
    day_scope: str                    # all|workdays|holidays|non_workdays
    time_on: time
    time_off: time
    tiers: tuple[LoadedTier, ...]
    end_of_day: bool = False  # True when time_off == 24:00 (end of day)

    @property
    def overnight(self) -> bool:
        """True when the daily window wraps past local midnight."""
        if self.end_of_day:
            return False
        return self.time_off <= self.time_on


@dataclass(frozen=True)
class LoadedVersion:
    code: str
    currency: str
    energy_unit: str
    description: str
    rules: tuple[LoadedRule, ...]
    holidays: dict[date, str]          # date -> kind (holiday|workday)
    holiday_names: dict[date, str]


# ---------------------------------------------------------------- repository


def _parse_rule(row: RateRule) -> LoadedRule:
    try:
        tz = ZoneInfo(row.timezone)
    except ZoneInfoNotFoundError as exc:
        raise PricingError(
            f"规则 {row.code} 的时区无效: {row.timezone}",
            code="invalid_rule",
            details={"rule": row.code, "timezone": row.timezone},
        ) from exc
    try:
        eff_from = datetime.fromisoformat(row.effective_from)
        eff_to = datetime.fromisoformat(row.effective_to)
        on = time.fromisoformat(row.time_on)
    except ValueError as exc:
        raise PricingError(
            f"规则 {row.code} 的时间字段格式无效",
            code="invalid_rule",
            details={"rule": row.code, "error": str(exc)},
        ) from exc
    # "24:00" denotes end of day (right edge of a whole-day half-open window);
    # datetime.time cannot represent hour 24, so keep a flag and 00:00.
    end_of_day = row.time_off == "24:00"
    try:
        off = time(0, 0) if end_of_day else time.fromisoformat(row.time_off)
    except ValueError as exc:
        raise PricingError(
            f"规则 {row.code} 的 time_off 格式无效",
            code="invalid_rule",
            details={"rule": row.code, "error": str(exc)},
        ) from exc
    if eff_from.tzinfo is not None or eff_to.tzinfo is not None:
        raise PricingError(
            f"规则 {row.code} 的生效时间必须是无时区的本地时间",
            code="invalid_rule", details={"rule": row.code},
        )
    if eff_to <= eff_from:
        raise PricingError(
            f"规则 {row.code} 的生效区间无效：结束必须晚于开始（半开区间）",
            code="invalid_rule", details={"rule": row.code},
        )
    weekdays = frozenset(
        int(d) for d in row.weekdays.split(",") if d.strip()
    )
    if not weekdays <= set(range(1, 8)):
        raise PricingError(
            f"规则 {row.code} 的 weekdays 非法（只允许 1-7）",
            code="invalid_rule", details={"rule": row.code},
        )
    if row.day_scope not in {"all", "workdays", "holidays",
                             "non_workdays", "weekends"}:
        raise PricingError(
            f"规则 {row.code} 的 day_scope 非法: {row.day_scope}",
            code="invalid_rule", details={"rule": row.code},
        )
    tiers: list[LoadedTier] = []
    for t in row.tiers:
        cap = Decimal(t.cumulative_cap) if t.cumulative_cap is not None else None
        if cap is not None and cap <= 0:
            raise PricingError(
                f"规则 {row.code} 第 {t.ordinal} 档上限必须为正",
                code="invalid_rule", details={"rule": row.code},
            )
        price = Decimal(t.unit_price)
        if price < 0:
            raise PricingError(
                f"规则 {row.code} 第 {t.ordinal} 档单价不能为负",
                code="invalid_rule", details={"rule": row.code},
            )
        tiers.append(LoadedTier(t.ordinal, cap, price))
    return LoadedRule(
        code=row.code, name=row.name,
        effective_from=eff_from, effective_to=eff_to, tz=tz,
        weekdays=weekdays, day_scope=row.day_scope,
        time_on=on, time_off=off, tiers=tuple(tiers),
        end_of_day=end_of_day,
    )


def load_version(session: Session, version_code: str) -> LoadedVersion:
    row = session.scalar(
        select(TariffVersion)
        .where(TariffVersion.code == version_code)
        .options(
            selectinload(TariffVersion.rules).selectinload(RateRule.tiers),
            selectinload(TariffVersion.holidays),
        )
    )
    if row is None:
        raise PricingError(
            f"规则版本不存在: {version_code}",
            code="version_not_found", status_code=404,
        )
    rules = tuple(_parse_rule(r) for r in sorted(row.rules, key=lambda r: r.id))
    holidays = {h.day: h.kind for h in row.holidays}
    names = {h.day: h.name for h in row.holidays}
    return LoadedVersion(
        code=row.code, currency=row.currency, energy_unit=row.energy_unit,
        description=row.description or "",
        rules=rules, holidays=holidays, holiday_names=names,
    )


def load_all_versions(session: Session) -> list[LoadedVersion]:
    rows = session.scalars(
        select(TariffVersion).options(
            selectinload(TariffVersion.rules).selectinload(RateRule.tiers),
            selectinload(TariffVersion.holidays),
        ).order_by(TariffVersion.code)
    ).all()
    return [
        LoadedVersion(
            code=row.code, currency=row.currency, energy_unit=row.energy_unit,
            description=row.description or "",
            rules=tuple(_parse_rule(r) for r in
                        sorted(row.rules, key=lambda r: r.id)),
            holidays={h.day: h.kind for h in row.holidays},
            holiday_names={h.day: h.name for h in row.holidays},
        )
        for row in rows
    ]


# ------------------------------------------------------------- time helpers


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise PricingError(
            f"无效时区: {name}", code="invalid_timezone", status_code=400,
            details={"timezone": name},
        ) from exc


def parse_local_datetime(value: str, tz: ZoneInfo) -> datetime:
    """Parse an ISO-8601 datetime and return an aware UTC instant.

    * Aware input (with offset or ``Z``): the offset is honoured as given;
      the value is normalised to UTC.
    * Naive input: interpreted in ``tz``. Spring-forward gap times and
      fall-back ambiguous times are rejected so the caller can disambiguate
      by sending an explicit UTC offset.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingError(
            f"时间格式无效: {value}", code="invalid_datetime", status_code=400,
            details={"value": value},
        ) from exc

    if dt.tzinfo is None:
        dt0 = dt.replace(tzinfo=tz, fold=0)
        dt1 = dt.replace(tzinfo=tz, fold=1)
        # PEP 495: in both a spring-forward gap and a fall-back ambiguity the
        # two folds carry different offsets. Distinguish by round-tripping:
        # a gap wall time (e.g. 02:30 during US spring forward) maps forward
        # and never maps back to itself.
        if dt0.utcoffset() != dt1.utcoffset():
            if dt0.astimezone(timezone.utc).astimezone(tz).replace(
                    tzinfo=None) != dt:
                raise PricingError(
                    f"本地时间 {value} 在时区 {tz} 的夏令时跳转中"
                    "不存在（春季跳时缺口），请携带明确的 UTC 偏移量",
                    code="nonexistent_local_time", status_code=400,
                    details={"value": value, "timezone": str(tz)},
                )
            raise PricingError(
                f"本地时间 {value} 在时区 {tz} 的夏令时回拨中含义不明确"
                "（重复的一小时），请携带明确的 UTC 偏移量",
                code="ambiguous_local_time", status_code=400,
                details={"value": value, "timezone": str(tz)},
            )
        dt = dt0
    return dt.astimezone(timezone.utc)


def _date_range(d0: date, d1: date):
    """Yield dates from d0 (inclusive) up to d1 (inclusive)."""
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def local_midnights(tz: ZoneInfo, lo: datetime, hi: datetime) -> list[datetime]:
    """UTC instants of local midnights strictly inside (lo, hi)."""
    out: list[datetime] = []
    first = lo.astimezone(tz).date()
    last = hi.astimezone(tz).date()
    for d in _date_range(first, last):
        mid = datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)
        if lo < mid < hi:
            out.append(mid)
    return out


def rule_time_edges(rule: LoadedRule, lo: datetime, hi: datetime) -> list[datetime]:
    """UTC instants of the rule's daily ``time_on``/``time_off`` edges inside
    (lo, hi) — handles overnight windows and DST days where a stated local
    edge may be missing (spring-forward gap)."""
    out: set[datetime] = set()
    first = lo.astimezone(rule.tz).date()
    last = hi.astimezone(rule.tz).date()
    # (wall time, day advance) edges of the daily window.
    edges = [(rule.time_on, 0)]
    if rule.end_of_day:
        edges.append((time.min, 1))
    else:
        edges.append((rule.time_off, 1 if rule.overnight else 0))
    for d in _date_range(first, last + timedelta(days=1)):
        for t, advance in edges:
            wall = datetime.combine(d + timedelta(days=advance), t)
            local0 = wall.replace(tzinfo=rule.tz, fold=0)
            local1 = wall.replace(tzinfo=rule.tz, fold=1)
            # Spring-forward gap wall time never round-trips to itself;
            # the DST transition instant is already a split point.
            if (local0.utcoffset() != local1.utcoffset()
                    and local0.astimezone(timezone.utc)
                    .astimezone(rule.tz).replace(tzinfo=None) != wall):
                continue
            inst = local0.astimezone(timezone.utc)
            if lo < inst < hi:
                out.add(inst)
    return sorted(out)


def dst_transitions(tz: ZoneInfo, lo: datetime, hi: datetime) -> list[datetime]:
    """Find DST/UTC-offset transition instants strictly inside (lo, hi).

    For each local calendar day whose start/end offsets differ, the exact
    transition second is located by integer binary search on UTC offsets.
    """
    found: set[datetime] = set()
    first = lo.astimezone(tz).date()
    last = (hi - timedelta(seconds=1)).astimezone(tz).date()
    for d in _date_range(first, last):
        day_start = datetime.combine(d, time.min, tzinfo=tz)
        day_end = day_start + DAY
        if day_start.utcoffset() == day_end.utcoffset():
            continue
        a = day_start.astimezone(timezone.utc)
        b = day_end.astimezone(timezone.utc)
        lo_i = int((a - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
        hi_i = int((b - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
        start_off = day_start.utcoffset()
        # Find first second whose offset differs from the day-start offset.
        lo2, hi2 = lo_i, hi_i
        while lo2 < hi2:
            mid = (lo2 + hi2) // 2
            inst = datetime.fromtimestamp(mid, timezone.utc).astimezone(tz)
            if inst.utcoffset() != start_off:
                hi2 = mid
            else:
                lo2 = mid + 1
        trans = datetime.fromtimestamp(lo2, timezone.utc)
        if lo < trans < hi:
            found.add(trans)
    return sorted(found)


# --------------------------------------------------------- calendar logic


def classify_day(d: date, version: LoadedVersion) -> str:
    """Return ``holiday``, ``workday`` (holiday-calendar override) or
    ``weekday``/``weekend`` from the ISO weekday."""
    override = version.holidays.get(d)
    if override is not None:
        return override  # holiday | workday
    return "weekday" if d.isoweekday() <= 5 else "weekend"


def rule_applies_on(rule: LoadedRule, d: date, version: LoadedVersion) -> bool:
    kind = classify_day(d, version)
    if rule.day_scope == "all":
        # weekday mask still narrows an "all" scope rule
        return not rule.weekdays or d.isoweekday() in rule.weekdays
    if rule.day_scope == "workdays":
        if kind == "workday":
            return True  # explicit 调休上班 day overrides the weekday mask
        if kind == "weekday":
            return not rule.weekdays or d.isoweekday() in rule.weekdays
        return False
    if rule.day_scope == "holidays":
        return kind == "holiday"
    if rule.day_scope == "non_workdays":
        return kind == "holiday" or kind == "weekend"
    if rule.day_scope == "weekends":
        return kind == "weekend"
    return False


def rule_local_window(rule: LoadedRule, d: date) -> tuple[datetime, datetime]:
    """UTC [start, end) of the rule's daily window on local date ``d``."""
    start = datetime.combine(d, rule.time_on, tzinfo=rule.tz)
    if rule.end_of_day:
        end = datetime.combine(d + timedelta(days=1), time.min,
                               tzinfo=rule.tz)
    elif rule.overnight:
        # [time_on on day d, time_off on day d+1); a pre-midnight window that
        # belongs to the *following* calendar day's rule applicability.
        end = datetime.combine(d + timedelta(days=1), rule.time_off,
                               tzinfo=rule.tz)
    else:
        end = datetime.combine(d, rule.time_off, tzinfo=rule.tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def rule_intervals(rule: LoadedRule, version: LoadedVersion,
                   lo: datetime | None = None,
                   hi: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """Sorted, merged UTC half-open intervals where the rule is in effect."""
    raw: list[tuple[datetime, datetime]] = []
    start_day = rule.effective_from.date()
    end_day = (rule.effective_to - timedelta(microseconds=1)).date()
    for d in _date_range(start_day, end_day):
        if not rule_applies_on(rule, d, version):
            continue
        ws, we = rule_local_window(rule, d)
        eff_start = rule.effective_from.replace(tzinfo=rule.tz).astimezone(
            timezone.utc)
        eff_end = rule.effective_to.replace(tzinfo=rule.tz).astimezone(
            timezone.utc)
        ws = max(ws, eff_start)
        we = min(we, eff_end)
        if ws < we:
            raw.append((ws, we))
    if lo is not None:
        raw = [(max(s, lo), e) for s, e in raw if e > lo]
    if hi is not None:
        raw = [(s, min(e, hi)) for s, e in raw if s < hi]
    raw.sort()
    merged: list[tuple[datetime, datetime]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def intervals_overlap(a: list[tuple[datetime, datetime]],
                      b: list[tuple[datetime, datetime]]) -> tuple | None:
    """First overlapping ``(instant, interval_a, interval_b)`` or None."""
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            return s, a[i], b[j]
        if a[i][1] <= b[j][0]:
            i += 1
        else:
            j += 1
    return None


# ------------------------------------------------------------- validation


def validate_tiers(rule: LoadedRule) -> dict | None:
    if not rule.tiers:
        return {
            "type": "tier_gap", "rule": rule.code,
            "message": f"规则 {rule.code} 没有任何价格阶梯",
        }
    prev_cap: Decimal | None = ZERO
    for idx, t in enumerate(rule.tiers):
        if t.ordinal != idx + 1:
            return {
                "type": "tier_gap", "rule": rule.code,
                "message": f"规则 {rule.code} 阶梯序号不连续",
            }
        if idx < len(rule.tiers) - 1:
            if t.cumulative_cap is None:
                return {
                    "type": "tier_gap", "rule": rule.code,
                    "message": f"规则 {rule.code} 第 {t.ordinal} 档之后"
                    "仍有阶梯，但该档未设累计上限（中间档位不得开放）",
                }
            if t.cumulative_cap <= prev_cap:
                return {
                    "type": "tier_gap", "rule": rule.code,
                    "message": f"规则 {rule.code} 第 {t.ordinal} 档累计上限"
                    f" {t.cumulative_cap} 必须大于前一档上限 {prev_cap}",
                }
        else:
            if t.cumulative_cap is not None:
                return {
                    "type": "tier_gap", "rule": rule.code,
                    "message": f"规则 {rule.code} 的最后一档必须开放"
                    "（cumulative_cap 为空），否则超出上限的用量无价格可依",
                }
        prev_cap = t.cumulative_cap
    return None


def validate_version(version: LoadedVersion) -> list[dict]:
    """Return a list of structural issues (tier gaps / time overlaps)."""
    issues: list[dict] = []
    if not version.rules:
        return [{
            "type": "no_rules",
            "message": f"版本 {version.code} 没有任何规则",
        }]

    intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for rule in version.rules:
        issue = validate_tiers(rule)
        if issue:
            issues.append(issue)
        # Compute applicability intervals even when tiers are bad, so both
        # classes of structural problem are reported in one response.
        intervals[rule.code] = rule_intervals(rule, version)

    codes = [r.code for r in version.rules]
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            a_code, b_code = codes[i], codes[j]
            hit = intervals_overlap(intervals[a_code], intervals[b_code])
            if hit:
                instant, ia, ib = hit
                issues.append({
                    "type": "rule_overlap",
                    "rules": [a_code, b_code],
                    "at": instant.isoformat(),
                    "interval_a": [ia[0].isoformat(), ia[1].isoformat()],
                    "interval_b": [ib[0].isoformat(), ib[1].isoformat()],
                    "message": f"规则 {a_code} 与 {b_code} 在 "
                    f"{instant.isoformat()} 起时间段重叠；同一时刻至多命中一条规则",
                })
    return issues


def ensure_valid(version: LoadedVersion) -> None:
    issues = validate_version(version)
    if issues:
        raise PricingError(
            f"规则版本 {version.code} 存在 {len(issues)} 处结构问题"
            "（时间重叠或阶梯缺口）",
            code="rule_conflict", status_code=409, details=issues,
        )


# ------------------------------------------------------------- quote engine


@dataclass
class InputInterval:
    start: datetime          # UTC
    end: datetime            # UTC
    energy: Decimal
    tz: ZoneInfo
    index: int


@dataclass
class Segment:
    interval_index: int
    start: datetime
    end: datetime
    duration_seconds: int
    tz: ZoneInfo
    energy: Decimal
    rule_code: str
    rule_name: str
    tier_ordinal: int
    cumulative_from: Decimal
    cumulative_to: Decimal
    unit_price: Decimal
    amount: Decimal
    day_kind: str

    def to_dict(self, energy_unit: str, currency: str) -> dict:
        return {
            "interval_index": self.interval_index,
            "start": self.start.isoformat().replace("+00:00", "Z"),
            "end": self.end.isoformat().replace("+00:00", "Z"),
            "start_local": self.start.astimezone(self.tz).isoformat(),
            "end_local": self.end.astimezone(self.tz).isoformat(),
            "timezone": str(self.tz),
            "duration_seconds": self.duration_seconds,
            "day_kind": self.day_kind,
            "energy": str(self.energy.quantize(Q6)),
            "energy_unit": energy_unit,
            "rule_code": self.rule_code,
            "rule_name": self.rule_name,
            "tier_ordinal": self.tier_ordinal,
            "cumulative_from": str(self.cumulative_from.quantize(Q6)),
            "cumulative_to": str(self.cumulative_to.quantize(Q6)),
            "unit_price": str(self.unit_price),
            "currency": currency,
            "amount": str(self.amount.quantize(Q6)),
        }


@dataclass
class QuoteResult:
    version: LoadedVersion
    segments: list[Segment] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)

    @property
    def total_energy(self) -> Decimal:
        return sum((s.energy for s in self.segments), ZERO)

    @property
    def total_amount(self) -> Decimal:
        return sum((s.amount for s in self.segments), ZERO)


def parse_usage_intervals(payload_intervals,
                          version: LoadedVersion | None = None
                          ) -> list[InputInterval]:
    """Validate shared ``QuoteIntervalIn``-style payloads into UTC intervals.

    Intervals are sorted by start; overlapping inputs are rejected. The
    ``version`` argument is unused by the interval grammar itself (it is
    accepted for call sites that historically passed it) and may be omitted,
    e.g. by endpoints that do not price against a stored tariff version.
    """
    parsed: list[InputInterval] = []
    for idx, item in enumerate(payload_intervals):
        tz = get_zone(item.timezone)
        start = parse_local_datetime(item.start, tz)
        end = parse_local_datetime(item.end, tz)
        if end <= start:
            raise PricingError(
                f"第 {idx} 段用量区间必须满足 end > start（半开区间）",
                code="invalid_interval", status_code=400,
                details={"interval_index": idx},
            )
        energy = item.energy_decimal()
        if energy <= 0:
            raise PricingError(
                f"第 {idx} 段用量必须为正数",
                code="invalid_interval", status_code=400,
                details={"interval_index": idx},
            )
        parsed.append(InputInterval(start, end, energy, tz, idx))

    parsed.sort(key=lambda x: (x.start, x.end))
    for k in range(1, len(parsed)):
        if parsed[k].start < parsed[k - 1].end:
            raise PricingError(
                f"第 {parsed[k - 1].index} 段与第 {parsed[k].index} 段"
                "用量区间重叠；输入区间必须互不重叠",
                code="interval_overlap", status_code=400,
                details={
                    "intervals": [parsed[k - 1].index, parsed[k].index],
                },
            )
    return parsed


def _split_instant_inside(lo: datetime, hi: datetime,
                          intervals_: list[InputInterval],
                          version: LoadedVersion) -> list[datetime]:
    """All instants at which the rule in force could possibly change."""
    points: set[datetime] = set()
    for iv in intervals_:
        # Local-day boundaries in both the interval timezone and every rule tz
        zones = {iv.tz} | {r.tz for r in version.rules}
        for tz in zones:
            points.update(local_midnights(tz, lo, hi))
            points.update(dst_transitions(tz, lo, hi))
        for r in version.rules:
            points.update(rule_time_edges(r, lo, hi))
            eff_start = r.effective_from.replace(tzinfo=r.tz).astimezone(
                timezone.utc)
            eff_end = r.effective_to.replace(tzinfo=r.tz).astimezone(
                timezone.utc)
            if lo < eff_start < hi:
                points.add(eff_start)
            if lo < eff_end < hi:
                points.add(eff_end)
    return sorted(points)


def _rule_at(instant: datetime, version: LoadedVersion) -> LoadedRule | None:
    """Find the unique rule covering ``instant``. Assumes pre-validated."""
    match: LoadedRule | None = None
    for r in version.rules:
        eff_start = r.effective_from.replace(tzinfo=r.tz).astimezone(timezone.utc)
        eff_end = r.effective_to.replace(tzinfo=r.tz).astimezone(timezone.utc)
        if not (eff_start <= instant < eff_end):
            continue
        local = instant.astimezone(r.tz)
        d = local.date()
        if not rule_applies_on(r, d, version):
            continue
        t = local.time()
        if r.end_of_day:
            in_window = t >= r.time_on
        elif r.overnight:
            in_window = t >= r.time_on or t < r.time_off
        else:
            in_window = r.time_on <= t < r.time_off
        if in_window:
            if match is not None:
                # Should be impossible after validation; guard regardless.
                raise PricingError(
                    f"时刻 {instant.isoformat()} 同时命中规则 "
                    f"{match.code} 与 {r.code}",
                    code="rule_overlap", status_code=409,
                    details={"rules": [match.code, r.code],
                             "at": instant.isoformat()},
                )
            match = r
    return match


def _tier_at(rule: LoadedRule, cumulative: Decimal) -> LoadedTier:
    for t in rule.tiers:
        if t.cumulative_cap is None or cumulative < t.cumulative_cap:
            return t
    # Unreachable: final tier is open by validation.
    raise PricingError(
        f"累计用量 {cumulative} 超出规则 {rule.code} 的最高阶梯上限",
        code="tier_exhausted",
    )


def quote(version: LoadedVersion, payload_intervals,
          *, validate: bool = True) -> QuoteResult:
    if validate:
        ensure_valid(version)
    inputs = parse_usage_intervals(payload_intervals, version)

    lo = min(i.start for i in inputs)
    hi = max(i.end for i in inputs)

    # Slice every input interval at all boundary candidates.
    boundaries = _split_instant_inside(lo, hi, inputs, version)
    raw_slices: list[tuple[InputInterval, datetime, datetime]] = []
    for iv in inputs:
        cuts = [iv.start]
        cuts.extend(p for p in boundaries if iv.start < p < iv.end)
        cuts.append(iv.end)
        for a, b in zip(cuts, cuts[1:]):
            raw_slices.append((iv, a, b))
    raw_slices.sort(key=lambda x: (x[1], x[2]))

    result = QuoteResult(version=version)
    cumulative = ZERO
    matched: list[str] = []

    for iv, a, b in raw_slices:
        duration = int((b - a).total_seconds())
        total_dur = int((iv.end - iv.start).total_seconds())
        # Distribute energy proportionally to elapsed UTC seconds; residual
        # stays on the last slice of this interval so totals reconcile.
        if b == iv.end:
            used = iv.energy - sum(
                (s.energy for s in result.segments
                 if s.interval_index == iv.index), ZERO)
        else:
            used = (iv.energy * Decimal(duration)
                    / Decimal(total_dur)).quantize(Q6)
        if used <= 0:
            continue

        rule = _rule_at(a, version)
        if rule is None:
            local = a.astimezone(iv.tz).isoformat()
            raise PricingError(
                f"时刻 {local} 没有任何规则覆盖（规则缺口）；请检查版本的"
                "生效时间段、工作日/节假日条件与日内时段",
                code="rule_gap", status_code=409,
                details={"at": a.isoformat(), "at_local": local},
            )

        day_kind = classify_day(a.astimezone(rule.tz).date(), version)
        slice_start_cum = cumulative
        # One wall-clock slice can straddle a cumulative tier boundary: walk
        # the tiers and split the window proportionally to elapsed seconds,
        # keeping exact half-open sub-windows and reconciling energy onto the
        # final sub-segment so per-interval totals equal the input energy.
        # part = (tier, energy, window_start, window_end, cum_from, cum_to)
        parts: list[tuple] = []
        remaining_energy = used
        cursor = a
        while True:
            part_from = cumulative
            tier = _tier_at(rule, cumulative)
            room = (None if tier.cumulative_cap is None
                    else tier.cumulative_cap - cumulative)
            if room is None or remaining_energy <= room:
                parts.append((tier, remaining_energy, cursor, b,
                              part_from, part_from + remaining_energy))
                cumulative += remaining_energy
                break
            frac = room / used
            sub_seconds = int((frac * Decimal(duration)).to_integral_value())
            sub_end = cursor + timedelta(seconds=sub_seconds)
            parts.append((tier, room, cursor, sub_end,
                          part_from, part_from + room))
            cumulative += room
            remaining_energy -= room
            cursor = sub_end

        # Reconcile rounding residuals onto the last part so the energies of
        # all parts of this slice sum exactly to the slice energy.
        tier, _, ps, pe, cum_from, _ = parts[-1]
        accounted = sum((p[1] for p in parts[:-1]), ZERO)
        last_energy = used - accounted
        parts[-1] = (tier, last_energy, ps, pe, cum_from,
                     cum_from + last_energy)
        cumulative = slice_start_cum + used

        for tier, sub_energy, part_start, part_end, cum_from, cum_to in parts:
            if sub_energy <= 0:
                continue
            amount = (sub_energy * tier.unit_price).quantize(Q6)
            result.segments.append(Segment(
                interval_index=iv.index, start=part_start, end=part_end,
                duration_seconds=int((part_end - part_start).total_seconds()),
                tz=iv.tz,
                energy=sub_energy,
                rule_code=rule.code, rule_name=rule.name,
                tier_ordinal=tier.ordinal,
                cumulative_from=cum_from,
                cumulative_to=cum_to,
                unit_price=tier.unit_price,
                amount=amount, day_kind=day_kind,
            ))
            if rule.code not in matched:
                matched.append(rule.code)

    result.matched_rules = matched
    return result
