"""Version-diff engine: compare two tariff versions over identical usage.

Both versions are priced independently with :func:`pricing.quote`; the two
segment sets are then merged into *atomic* segments cut at the union of all
UTC endpoints. Each original segment's energy and 6-dp amount are distributed
across its atomic sub-segments proportionally to elapsed time, with the
rounding residual absorbed by the original segment's last sub-segment — so
per-version atomic sums reconcile exactly with the standalone quotes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .pricing import (
    Q6,
    ZERO,
    LoadedVersion,
    PricingError,
    QuoteResult,
    Segment,
    quote,
)


@dataclass(frozen=True)
class DiffSide:
    """One version's pricing of an atomic segment."""
    interval_index: int
    rule_code: str
    rule_name: str
    tier_ordinal: int
    unit_price: Decimal
    energy: Decimal
    amount: Decimal


@dataclass(frozen=True)
class DiffSegment:
    """Atomic window holding both versions' pricing (None == no coverage)."""
    start: datetime
    end: datetime
    interval_index: int
    before: DiffSide | None
    after: DiffSide | None

    @property
    def energy_diff(self) -> Decimal:
        return (self.after.energy if self.after else ZERO) - (
            self.before.energy if self.before else ZERO)

    @property
    def amount_diff(self) -> Decimal:
        return (self.after.amount if self.after else ZERO) - (
            self.before.amount if self.before else ZERO)

    @property
    def reasons(self) -> list[str]:
        """Every change dimension that holds on this segment, in fixed order."""
        b, a = self.before, self.after
        out: list[str] = []
        if (b.rule_code if b else None) != (a.rule_code if a else None):
            out.append("rule_changed")
        if (b.tier_ordinal if b else None) != (a.tier_ordinal if a else None):
            out.append("tier_changed")
        if (b.unit_price if b else None) != (a.unit_price if a else None):
            out.append("unit_price_changed")
        return out


@dataclass
class DiffResult:
    before: QuoteResult
    after: QuoteResult
    segments: list[DiffSegment]


def _micros(delta) -> int:
    """Exact length of a timedelta in whole microseconds."""
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _distribute(total: Decimal, weights: list[int]) -> list[Decimal]:
    """Split ``total`` proportionally to ``weights`` at 6 dp; the rounding
    residual lands on the final part so the parts sum to ``total`` exactly."""
    whole = sum(weights)
    if whole <= 0:  # zero-length window: nothing to prorate, keep total on last
        return [ZERO] * (len(weights) - 1) + [total]
    shares = [(total * w / whole).quantize(Q6) for w in weights[:-1]]
    shares.append(total - sum(shares, ZERO))
    return shares


def _atomic_sides(segments: list[Segment], points: list[datetime],
                  ) -> dict[tuple[datetime, datetime], DiffSide]:
    """Re-split every segment at the union ``points`` and index the pieces by
    their exact half-open window."""
    out: dict[tuple[datetime, datetime], DiffSide] = {}
    for seg in segments:
        cuts = [seg.start]
        cuts.extend(p for p in points if seg.start < p < seg.end)
        cuts.append(seg.end)
        windows = list(zip(cuts, cuts[1:]))
        weights = [_micros(b - a) for a, b in windows]
        energies = _distribute(seg.energy, weights)
        amounts = _distribute(seg.amount, weights)
        for (a, b), energy, amount in zip(windows, energies, amounts):
            out[(a, b)] = DiffSide(
                interval_index=seg.interval_index,
                rule_code=seg.rule_code, rule_name=seg.rule_name,
                tier_ordinal=seg.tier_ordinal, unit_price=seg.unit_price,
                energy=energy, amount=amount,
            )
    return out


def quote_diff(before: LoadedVersion, after: LoadedVersion,
               payload_intervals) -> DiffResult:
    """Price the same intervals under both versions and align the results."""
    if before.currency != after.currency:
        raise PricingError(
            f"两版币种不同（{before.currency} vs {after.currency}），无法比较",
            code="incompatible_versions", status_code=422,
            details={"before_currency": before.currency,
                     "after_currency": after.currency},
        )
    if before.energy_unit != after.energy_unit:
        raise PricingError(
            f"两版能量单位不同（{before.energy_unit} vs "
            f"{after.energy_unit}），无法比较",
            code="incompatible_versions", status_code=422,
            details={"before_energy_unit": before.energy_unit,
                     "after_energy_unit": after.energy_unit},
        )

    before_result = quote(before, payload_intervals)
    after_result = quote(after, payload_intervals)

    points = sorted({
        p for s in (*before_result.segments, *after_result.segments)
        for p in (s.start, s.end)
    })
    before_map = _atomic_sides(before_result.segments, points)
    after_map = _atomic_sides(after_result.segments, points)

    segments: list[DiffSegment] = []
    for a, b in zip(points, points[1:]):
        side_before = before_map.get((a, b))
        side_after = after_map.get((a, b))
        if side_before is None and side_after is None:
            # Gap in both versions (zero-energy slice skipped by the engine);
            # carries no energy or amount in either quote.
            continue
        side = side_before if side_before is not None else side_after
        segments.append(DiffSegment(
            start=a, end=b, interval_index=side.interval_index,
            before=side_before, after=side_after,
        ))
    return DiffResult(before=before_result, after=after_result,
                      segments=segments)
