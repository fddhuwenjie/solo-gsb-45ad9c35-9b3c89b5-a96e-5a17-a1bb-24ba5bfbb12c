"""End-to-end tests against the FastAPI app with in-memory SQLite."""
import os
from decimal import Decimal

import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="session", autouse=True)
def _startup():
    with client:
        yield


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_versions_listed():
    r = client.get("/versions")
    assert r.status_code == 200
    codes = {v["code"] for v in r.json()}
    assert codes == {"us-ny-demo-v2024", "us-ny-demo-v2025",
                     "cn-shanghai-demo-v2024"}


def test_seed_versions_have_no_issues():
    for code in ("us-ny-demo-v2024", "us-ny-demo-v2025",
                 "cn-shanghai-demo-v2024"):
        r = client.get(f"/versions/{code}/issues")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body["issues"]


# ---------------------------------------------------- cross-day + DST spring

def test_quote_cross_day_dst_spring_forward():
    # 2024-03-09 Sat 23:30 -> 2024-03-10 Sun 05:30, NY local time.
    # Spring forward at 02:00 -> 03:00 on 2024-03-10, so the UTC duration is
    # 5 hours (18000 s) rather than the naive 6 hours (21600 s).
    r = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [{
            "start": "2024-03-09T23:30:00-05:00",
            "end": "2024-03-10T05:30:00-04:00",
            "timezone": "America/New_York",
            "energy": "10",
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["total_energy"]) == Decimal("10.000000")
    durations = sum(s["duration_seconds"] for s in body["segments"])
    assert durations == 18000
    # Both weekend days -> only the leisure rule is matched.
    assert body["matched_rules"] == ["leisure"]
    assert Decimal(body["total_amount"]) == Decimal("1.50")


# ----------------------------------------------------- cross-rule boundaries

def test_quote_independence_day_crosses_rules_and_tiers():
    # 2024-07-03 22:00 (workday, tier1) -> 2024-07-04 06:00 (holiday).
    r = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [{
            "start": "2024-07-03T22:00:00-04:00",
            "end": "2024-07-04T06:00:00-04:00",
            "timezone": "America/New_York",
            "energy": "150",
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_rules"] == ["dst-workday", "leisure"]
    # 2h/8h of 150 = 37.5 workday tier1 @0.22; 112.5 holiday: 62.5 @0.15
    # (to cap 100), 50 @0.18 (tier2).
    assert Decimal(body["total_amount"]) == Decimal("26.63")
    kinds = [(s["rule_code"], s["tier_ordinal"]) for s in body["segments"]]
    assert ("dst-workday", 1) in kinds
    assert ("leisure", 1) in kinds
    assert ("leisure", 2) in kinds
    # half-open partition: adjacent segments share exactly one boundary
    segs = body["segments"]
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"]
    assert segs[0]["start_local"].startswith("2024-07-03T22:00:00-04:00")
    assert segs[-1]["end_local"].startswith("2024-07-04T06:00:00-04:00")


def test_dst_fall_back_duration_is_nine_hours():
    # 2024-11-03 00:00 (-04:00 DST) -> 08:00 (-05:00 standard), Sunday.
    # Wall clocks repeat 01:00-02:00, so this spans 9 UTC hours (32400 s).
    r = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [{
            "start": "2024-11-03T00:00:00-04:00",
            "end": "2024-11-03T08:00:00-05:00",
            "timezone": "America/New_York",
            "energy": "9",
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert sum(s["duration_seconds"] for s in body["segments"]) == 32400
    # DST->standard rule change is irrelevant on Sunday (leisure all day);
    # but the transition still splits the segments.
    assert body["matched_rules"] == ["leisure"]


# ------------------------------------------------------------------ errors

def test_ambiguous_naive_time_rejected():
    r = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [{
            "start": "2024-11-03T01:30:00",
            "end": "2024-11-03T03:00:00",
            "timezone": "America/New_York",
            "energy": "1",
        }],
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ambiguous_local_time"


def test_nonexistent_naive_time_rejected():
    r = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [{
            "start": "2024-03-10T02:30:00",
            "end": "2024-03-10T03:30:00",
            "timezone": "America/New_York",
            "energy": "1",
        }],
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "nonexistent_local_time"


def test_overlapping_input_intervals_rejected():
    r = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [
            {"start": "2024-07-01T00:00:00-04:00",
             "end": "2024-07-01T02:00:00-04:00",
             "timezone": "America/New_York", "energy": "1"},
            {"start": "2024-07-01T01:00:00-04:00",
             "end": "2024-07-01T03:00:00-04:00",
             "timezone": "America/New_York", "energy": "1"},
        ],
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "interval_overlap"


def test_unknown_version_404():
    r = client.post("/quote", json={
        "version": "nope",
        "intervals": [{
            "start": "2024-07-01T00:00:00-04:00",
            "end": "2024-07-01T02:00:00-04:00",
            "timezone": "America/New_York", "energy": "1",
        }],
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"


# ----------------------------------------------------- rule conflict engine

def _rule(**over):
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app.pricing import LoadedRule, LoadedTier

    base = dict(
        code="r", name="r",
        effective_from=dt.datetime(2024, 1, 1),
        effective_to=dt.datetime(2025, 1, 1),
        tz=ZoneInfo("UTC"), weekdays=frozenset(), day_scope="all",
        time_on=dt.time(0, 0),
        time_off=dt.time(0, 0),
        tiers=(LoadedTier(1, Decimal("100"), Decimal("0.2")),
               LoadedTier(2, None, Decimal("0.3"))),
    )
    base.update(over)
    return LoadedRule(**base)


def _version(rules):
    from app.pricing import LoadedVersion
    return LoadedVersion(
        code="t", currency="USD", energy_unit="kWh", description="",
        rules=tuple(rules), holidays={}, holiday_names={},
    )


def test_tier_gap_detected():
    from app.pricing import validate_tiers, LoadedTier
    bad = _rule(tiers=(LoadedTier(1, Decimal("100"), Decimal("0.2")),
                       LoadedTier(2, Decimal("100"), Decimal("0.3")),
                       LoadedTier(3, None, Decimal("0.4"))))
    issue = validate_tiers(bad)
    assert issue is not None and issue["type"] == "tier_gap"


def test_closed_last_tier_detected():
    from app.pricing import validate_tiers, LoadedTier
    bad = _rule(tiers=(LoadedTier(1, Decimal("100"), Decimal("0.2")),))
    issue = validate_tiers(bad)
    assert issue["type"] == "tier_gap"
    assert "最后一档" in issue["message"]


def test_rule_overlap_detected_even_when_named_differently():
    from app.pricing import validate_version
    a = _rule(code="a")
    b = _rule(code="b")
    issues = validate_version(_version([a, b]))
    overlaps = [i for i in issues if i["type"] == "rule_overlap"]
    assert overlaps and overlaps[0]["rules"] == ["a", "b"]


def test_conditionally_disjoint_rules_do_not_overlap():
    from app.pricing import validate_version
    from zoneinfo import ZoneInfo
    import datetime as dt
    common = dict(
        effective_from=dt.datetime(2024, 1, 1),
        effective_to=dt.datetime(2025, 1, 1),
        tz=ZoneInfo("UTC"),
        time_on=dt.time(0, 0), time_off=dt.time(0, 0),
    )
    work = _rule(code="work", day_scope="workdays",
                 weekdays=frozenset({1, 2, 3, 4, 5}), **common)
    rest = _rule(code="rest", day_scope="non_workdays",
                 weekdays=frozenset(), **common)
    assert validate_version(_version([work, rest])) == []


def test_cn_makeup_workday_classified_as_workday():
    # 2024-02-04 is Sunday but a 调休 workday in China -> workday rule.
    r = client.post("/quote", json={
        "version": "cn-shanghai-demo-v2024",
        "intervals": [{
            "start": "2024-02-04T08:00:00+08:00",
            "end": "2024-02-04T10:00:00+08:00",
            "timezone": "Asia/Shanghai", "energy": "10",
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_rules"] == ["workday"]
    assert body["segments"][0]["day_kind"] == "workday"


# ---------------------------------------------------------------- quote-diff

# 2024-07-03 22:00 (workday) -> 2024-07-04 06:00 (Independence Day), 8h/150 kWh.
DIFF_INTERVAL = {
    "start": "2024-07-03T22:00:00-04:00",
    "end": "2024-07-04T06:00:00-04:00",
    "timezone": "America/New_York",
    "energy": "150",
}


def test_quote_diff_incompatible_currency_rejected():
    r = client.post("/quote-diff", json={
        "before_version": "us-ny-demo-v2024",
        "after_version": "cn-shanghai-demo-v2024",
        "intervals": [DIFF_INTERVAL],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incompatible_versions"


def test_quote_diff_unknown_version_404():
    r = client.post("/quote-diff", json={
        "before_version": "nope",
        "after_version": "us-ny-demo-v2024",
        "intervals": [DIFF_INTERVAL],
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"


def test_quote_diff_same_version_keeps_segments_all_unchanged():
    r = client.post("/quote-diff", json={
        "before_version": "us-ny-demo-v2024",
        "after_version": "us-ny-demo-v2024",
        "intervals": [DIFF_INTERVAL],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Unchanged atomic segments are kept, with empty reasons and zero diffs.
    assert body["segments"]
    assert all(s["reasons"] == [] for s in body["segments"])
    assert all(Decimal(s["amount_diff"]) == 0 for s in body["segments"])
    assert all(Decimal(s["energy_diff"]) == 0 for s in body["segments"])
    assert body["before_total_amount"] == body["after_total_amount"] == "26.63"
    assert Decimal(body["total_amount_diff"]) == 0


def test_quote_diff_reconciles_with_independent_quotes():
    r = client.post("/quote-diff", json={
        "before_version": "us-ny-demo-v2024",
        "after_version": "us-ny-demo-v2025",
        "intervals": [DIFF_INTERVAL],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    qb = client.post("/quote", json={
        "version": "us-ny-demo-v2024", "intervals": [DIFF_INTERVAL],
    }).json()
    qa = client.post("/quote", json={
        "version": "us-ny-demo-v2025", "intervals": [DIFF_INTERVAL],
    }).json()
    # Totals match the standalone quotes exactly.
    assert body["before_total_amount"] == qb["total_amount"] == "26.63"
    assert body["after_total_amount"] == qa["total_amount"] == "28.13"
    assert body["total_amount_diff"] == "1.50"
    assert body["total_energy"] == qb["total_energy"] == qa["total_energy"]
    segs = body["segments"]
    # Atomic segments form a half-open partition of the quoted span.
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"]
    # Per-version atomic sums reconcile with the independent quotes.
    assert (sum(Decimal(s["before"]["energy"]) for s in segs)
            == sum(Decimal(s["energy"]) for s in qb["segments"]))
    assert (sum(Decimal(s["after"]["energy"]) for s in segs)
            == sum(Decimal(s["energy"]) for s in qa["segments"]))
    assert (sum(Decimal(s["before"]["amount"]) for s in segs)
            == sum(Decimal(s["amount"]) for s in qb["segments"]))
    assert (sum(Decimal(s["after"]["amount"]) for s in segs)
            == sum(Decimal(s["amount"]) for s in qa["segments"]))
    # Same rules/tiers on both versions, prices rose across the board.
    assert all(s["reasons"] == ["unit_price_changed"] for s in segs)


def test_quote_diff_rule_changed_when_calendars_differ():
    # 2024-10-14 (Columbus Day) is a holiday in v2024 but a plain workday
    # in v2025, so the matched rule changes on every atomic segment.
    r = client.post("/quote-diff", json={
        "before_version": "us-ny-demo-v2024",
        "after_version": "us-ny-demo-v2025",
        "intervals": [{
            "start": "2024-10-14T09:00:00-04:00",
            "end": "2024-10-14T17:00:00-04:00",
            "timezone": "America/New_York",
            "energy": "80",
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segments"]
    rules = {(s["before"]["rule_code"], s["after"]["rule_code"])
             for s in body["segments"]}
    assert rules == {("leisure", "dst-workday")}
    assert all("rule_changed" in s["reasons"] for s in body["segments"])
    assert all("unit_price_changed" in s["reasons"] for s in body["segments"])
    assert all("tier_changed" not in s["reasons"] for s in body["segments"])
    assert body["before_total_amount"] == "12.00"
    assert body["after_total_amount"] == "18.40"
    assert body["total_amount_diff"] == "6.40"


def test_quote_diff_atomic_split_reconciles_synthetic():
    # Two synthetic versions whose rule edges differ (08:00 vs none) force a
    # real bisection of one side's segment at the union endpoints.
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app.diff import quote_diff
    from app.pricing import LoadedRule, LoadedTier, LoadedVersion, quote
    from app.schemas import QuoteIntervalIn

    tz = ZoneInfo("UTC")

    def flat_rule(code, price, on, off):
        return LoadedRule(
            code=code, name=code,
            effective_from=dt.datetime(2024, 1, 1),
            effective_to=dt.datetime(2025, 1, 1),
            tz=tz, weekdays=frozenset(), day_scope="all",
            time_on=on, time_off=off,
            tiers=(LoadedTier(1, None, Decimal(price)),),
        )

    vx = LoadedVersion(
        code="x", currency="USD", energy_unit="kWh", description="",
        rules=(flat_rule("x-night", "0.05", dt.time(20, 0), dt.time(8, 0)),
               flat_rule("x-day", "0.10", dt.time(8, 0), dt.time(20, 0))),
        holidays={}, holiday_names={},
    )
    vy = LoadedVersion(
        code="y", currency="USD", energy_unit="kWh", description="",
        rules=(flat_rule("y-flat", "0.07", dt.time(0, 0), dt.time(0, 0)),),
        holidays={}, holiday_names={},
    )
    intervals = [QuoteIntervalIn(
        start="2024-06-01T06:00:00Z", end="2024-06-01T12:00:00Z",
        timezone="UTC", energy=Decimal("6"),
    )]
    result = quote_diff(vx, vy, intervals)

    # Union endpoints 06:00 / 08:00 / 12:00 -> two atomic segments.
    assert [(s.start.hour, s.end.hour) for s in result.segments] == [(6, 8),
                                                                     (8, 12)]
    first, second = result.segments
    assert first.before.rule_code == "x-night"
    assert first.after.rule_code == "y-flat"
    assert first.reasons == ["rule_changed", "unit_price_changed"]
    assert second.before.rule_code == "x-day"
    # The y side's single 6h/0.42 segment is bisected at 08:00 by duration.
    assert first.after.amount == Decimal("0.140000")
    assert second.after.amount == Decimal("0.280000")
    assert first.before.amount == Decimal("0.100000")
    assert second.before.amount == Decimal("0.400000")
    # Per-version sums reconcile exactly with the standalone quotes.
    qx, qy = quote(vx, intervals), quote(vy, intervals)
    zero = Decimal("0")
    assert sum((s.before.energy for s in result.segments),
               zero) == qx.total_energy
    assert sum((s.before.amount for s in result.segments),
               zero) == qx.total_amount
    assert sum((s.after.energy for s in result.segments),
               zero) == qy.total_energy
    assert sum((s.after.amount for s in result.segments),
               zero) == qy.total_amount
