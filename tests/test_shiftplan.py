"""End-to-end tests for POST /shift-plan (in-memory SQLite)."""
import datetime as dt
import os
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import shiftplan  # noqa: E402
from app.database import RateRule, TariffVersion, Tier, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.pricing import (  # noqa: E402
    LoadedRule,
    LoadedTier,
    LoadedVersion,
    PricingError,
)
from app.schemas import ShiftPlanRequest  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="session", autouse=True)
def _startup():
    with client:
        yield


def _task(earliest, latest, minutes, energy):
    return {
        "earliest_start": earliest,
        "latest_end": latest,
        "duration_minutes": minutes,
        "energy": energy,
    }


# Two identical 2h tasks, Friday 20:00 -> Saturday 04:00 New York time.
WEEKEND_PAYLOAD = {
    "version": "us-ny-demo-v2024",
    "timezone": "America/New_York",
    "tasks": [
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00", 120, "5"),
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00", 120, "7"),
    ],
}


def test_shift_plan_prefers_cheaper_weekend_window():
    r = client.post("/shift-plan", json=WEEKEND_PAYLOAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["reason_code"] is None
    # Workday tier1 is 0.22, weekend tier1 is 0.15: both tasks must land in
    # the Saturday leisure window; ties break to the earliest starts.
    t0, t1 = body["tasks"]
    assert (t0["task_index"], t0["start"], t0["end"]) == (
        0, "2024-07-06T04:00:00Z", "2024-07-06T06:00:00Z")
    assert (t1["task_index"], t1["start"], t1["end"]) == (
        1, "2024-07-06T06:00:00Z", "2024-07-06T08:00:00Z")
    assert t0["start_local"] == "2024-07-06T00:00:00-04:00"
    assert t1["end_local"] == "2024-07-06T04:00:00-04:00"
    assert body["total_amount"] == "1.80"  # 5*0.15 + 7*0.15
    assert Decimal(body["total_energy"]) == Decimal("12")
    assert {s["rule_code"] for s in body["segments"]} == {"leisure"}
    # Every segment is labelled with its owning task.
    assert {s["task_index"] for s in body["segments"]} == {0, 1}
    for s in body["segments"]:
        assert s["start"] >= body["tasks"][s["task_index"]]["start"]
        assert s["end"] <= body["tasks"][s["task_index"]]["end"]
    # 13 candidates per task -> 169 pairs, 90 of them non-overlapping.
    assert body["candidates_evaluated"] == 169
    assert body["non_overlapping_pairs"] == 90
    assert body["rule_gap_pairs"] == 0


def test_shift_plan_reconciles_with_quote():
    body = client.post("/shift-plan", json=WEEKEND_PAYLOAD).json()
    t0, t1 = body["tasks"]
    q = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [
            {"start": t0["start"], "end": t0["end"],
             "timezone": "America/New_York", "energy": "5"},
            {"start": t1["start"], "end": t1["end"],
             "timezone": "America/New_York", "energy": "7"},
        ],
    }).json()
    assert body["total_amount"] == q["total_amount"]
    assert Decimal(body["total_energy"]) == Decimal(q["total_energy"])
    assert (sum(Decimal(s["amount"]) for s in body["segments"])
            == sum(Decimal(s["amount"]) for s in q["segments"]))


def test_shift_plan_shares_cumulative_tiers_across_tasks():
    # 60 + 50 kWh on a Saturday: jointly the second task spills into tier 2
    # (cap 100); quoted separately both would stay in tier 1.
    r = client.post("/shift-plan", json={
        "version": "us-ny-demo-v2024",
        "timezone": "America/New_York",
        "tasks": [
            _task("2024-07-06T00:00:00-04:00", "2024-07-06T08:00:00-04:00",
                  60, "60"),
            _task("2024-07-06T00:00:00-04:00", "2024-07-06T08:00:00-04:00",
                  60, "50"),
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    # Shared cumulative: 60*0.15 + 40*0.15 + 10*0.18 = 16.80
    assert body["total_amount"] == "16.80"
    tiers = {(s["task_index"], s["tier_ordinal"]) for s in body["segments"]}
    assert (1, 2) in tiers  # second task reaches tier 2 via shared cumulative
    separate = sum(
        Decimal(client.post("/quote", json={
            "version": "us-ny-demo-v2024",
            "intervals": [{
                "start": "2024-07-06T00:00:00-04:00",
                "end": "2024-07-06T01:00:00-04:00",
                "timezone": "America/New_York", "energy": e,
            }],
        }).json()["total_amount"])
        for e in ("60", "50")
    )
    # Not the naive sum of two independent quotes (9.00 + 7.50 = 16.50):
    # the joint engine prices both tasks against one cumulative ladder.
    assert separate == Decimal("16.50")
    assert Decimal(body["total_amount"]) != separate


# ------------------------------------------------------------ synthetic rules

UTC = ZoneInfo("UTC")


def _flat_rule(code, price, on, off, end_of_day=False, tiers=None):
    return LoadedRule(
        code=code, name=code,
        effective_from=dt.datetime(2024, 1, 1),
        effective_to=dt.datetime(2025, 1, 1),
        tz=UTC, weekdays=frozenset(), day_scope="all",
        time_on=on, time_off=off, end_of_day=end_of_day,
        tiers=tiers or (LoadedTier(1, None, Decimal(price)),),
    )


def _req(tasks, timezone="UTC"):
    return ShiftPlanRequest(version="synthetic", timezone=timezone, tasks=tasks)


GAP_VERSION = LoadedVersion(
    code="gap", currency="USD", energy_unit="kWh", description="",
    rules=(
        _flat_rule("night", "0.05", dt.time(0, 0), dt.time(12, 0)),
        # 12:00-13:00 uncovered on purpose.
        _flat_rule("day", "0.10", dt.time(13, 0), dt.time(0, 0),
                   end_of_day=True),
    ),
    holidays={}, holiday_names={},
)


def test_rule_gap_only_eliminates_affected_candidates():
    # Window 11:00-19:00 with 2h tasks: pairs starting 11:00/12:00 hit the
    # 12:00-13:00 coverage gap and must be skipped, not fatal.
    req = _req([
        _task("2024-06-01T11:00:00Z", "2024-06-01T19:00:00Z", 120, "2"),
        _task("2024-06-01T11:00:00Z", "2024-06-01T19:00:00Z", 120, "3"),
    ])
    res = shiftplan.plan_shift(GAP_VERSION, req)
    assert res.feasible is True
    assert res.rule_gap_pairs > 0
    assert res.gaps and res.gaps[0]["at"] == "2024-06-01T12:00:00+00:00"
    # Only day-rule pairs survive; lexicographically earliest is (13:00, 15:00).
    assert res.best_starts == (
        dt.datetime(2024, 6, 1, 13, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2024, 6, 1, 15, 0, tzinfo=dt.timezone.utc),
    )
    assert res.result.total_amount == Decimal("0.5")


def test_all_pairs_rule_gap_reports_reason():
    req = _req([
        _task("2024-06-01T12:00:00Z", "2024-06-01T13:00:00Z", 30, "1"),
        _task("2024-06-01T12:00:00Z", "2024-06-01T13:00:00Z", 30, "1"),
    ])
    res = shiftplan.plan_shift(GAP_VERSION, req)
    assert res.feasible is False
    assert res.reason_code == "all_pairs_rule_gap"
    assert res.non_overlapping_pairs == 2
    assert res.rule_gap_pairs == 2
    assert "规则缺口" in res.reason


def test_no_non_overlapping_pair_reports_reason():
    # Two 2h tasks inside a shared 3h window can never run back to back.
    flat = LoadedVersion(
        code="flat", currency="USD", energy_unit="kWh", description="",
        rules=(_flat_rule("flat", "0.10", dt.time(0, 0), dt.time(0, 0),
                          end_of_day=True),),
        holidays={}, holiday_names={},
    )
    req = _req([
        _task("2024-06-01T00:00:00Z", "2024-06-01T03:00:00Z", 120, "2"),
        _task("2024-06-01T00:00:00Z", "2024-06-01T03:00:00Z", 120, "3"),
    ])
    res = shiftplan.plan_shift(flat, req)
    assert res.feasible is False
    assert res.reason_code == "no_non_overlapping_pair"
    assert res.non_overlapping_pairs == 0


def test_tie_break_prefers_lexicographically_earliest_starts():
    # Flat price: every feasible pair costs the same, so the winner is the
    # lexicographically earliest (task0, task1) UTC start tuple.
    flat = LoadedVersion(
        code="flat", currency="USD", energy_unit="kWh", description="",
        rules=(_flat_rule("flat", "0.10", dt.time(0, 0), dt.time(0, 0),
                          end_of_day=True),),
        holidays={}, holiday_names={},
    )
    req = _req([
        _task("2024-06-01T00:00:00Z", "2024-06-01T08:00:00Z", 60, "1"),
        _task("2024-06-01T00:00:00Z", "2024-06-01T08:00:00Z", 60, "1"),
    ])
    res = shiftplan.plan_shift(flat, req)
    assert res.feasible is True
    assert res.best_starts == (
        dt.datetime(2024, 6, 1, 0, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2024, 6, 1, 1, 0, tzinfo=dt.timezone.utc),
    )


def test_structural_rule_errors_raise_immediately():
    overlapping = LoadedVersion(
        code="ov", currency="USD", energy_unit="kWh", description="",
        rules=(
            _flat_rule("a", "0.05", dt.time(0, 0), dt.time(23, 0)),
            _flat_rule("b", "0.10", dt.time(0, 0), dt.time(23, 0)),
        ),
        holidays={}, holiday_names={},
    )
    req = _req([
        _task("2024-06-01T00:00:00Z", "2024-06-01T04:00:00Z", 60, "1"),
        _task("2024-06-01T00:00:00Z", "2024-06-01T04:00:00Z", 60, "1"),
    ])
    with pytest.raises(PricingError) as excinfo:
        shiftplan.plan_shift(overlapping, req)
    assert excinfo.value.code == "rule_conflict"
    assert excinfo.value.status_code == 409


def test_structural_tier_gap_raises_immediately():
    bad_tiers = (LoadedTier(1, Decimal("100"), Decimal("0.2")),)  # closed last
    broken = LoadedVersion(
        code="tg", currency="USD", energy_unit="kWh", description="",
        rules=(_flat_rule("flat", "0.10", dt.time(0, 0), dt.time(0, 0),
                          end_of_day=True, tiers=bad_tiers),),
        holidays={}, holiday_names={},
    )
    req = _req([
        _task("2024-06-01T00:00:00Z", "2024-06-01T04:00:00Z", 60, "1"),
        _task("2024-06-01T00:00:00Z", "2024-06-01T04:00:00Z", 60, "1"),
    ])
    with pytest.raises(PricingError) as excinfo:
        shiftplan.plan_shift(broken, req)
    assert excinfo.value.code == "rule_conflict"


# ----------------------------------------------------------------- API errors

def test_shift_plan_structural_error_via_api_409():
    with Session(engine) as session:
        if session.get(TariffVersion, "broken-shift") is None:
            v = TariffVersion(code="broken-shift", currency="USD",
                              energy_unit="kWh", description="")
            for code in ("a", "b"):
                rule = RateRule(
                    code=code, name=code,
                    effective_from="2024-01-01T00:00:00",
                    effective_to="2025-01-01T00:00:00",
                    timezone="UTC", weekdays="", day_scope="all",
                    time_on="00:00", time_off="24:00",
                )
                rule.tiers.append(
                    Tier(ordinal=1, cumulative_cap=None, unit_price="0.1"))
                v.rules.append(rule)
            session.add(v)
            session.commit()
    r = client.post("/shift-plan", json={
        "version": "broken-shift",
        "timezone": "UTC",
        "tasks": [
            _task("2024-06-01T00:00:00Z", "2024-06-01T04:00:00Z", 60, "1"),
            _task("2024-06-01T00:00:00Z", "2024-06-01T04:00:00Z", 60, "1"),
        ],
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "rule_conflict"


def test_shift_plan_naive_time_rejected():
    payload = dict(WEEKEND_PAYLOAD)
    payload["tasks"] = [
        _task("2024-07-05T20:00:00", "2024-07-06T04:00:00-04:00", 120, "5"),
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00", 120, "7"),
    ]
    r = client.post("/shift-plan", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "offset_required"


def test_shift_plan_window_over_eight_hours_rejected():
    payload = dict(WEEKEND_PAYLOAD)
    payload["tasks"] = [
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T06:00:00-04:00", 120, "5"),
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00", 120, "7"),
    ]
    r = client.post("/shift-plan", json=payload)
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "window_too_large"
    assert body["details"]["task_index"] == 0


def test_shift_plan_duration_exceeds_window_rejected():
    payload = dict(WEEKEND_PAYLOAD)
    payload["tasks"] = [
        _task("2024-07-05T20:00:00-04:00", "2024-07-05T21:00:00-04:00", 120, "5"),
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00", 120, "7"),
    ]
    r = client.post("/shift-plan", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "duration_exceeds_window"


def test_shift_plan_reversed_window_rejected():
    payload = dict(WEEKEND_PAYLOAD)
    payload["tasks"] = [
        _task("2024-07-06T04:00:00-04:00", "2024-07-05T20:00:00-04:00", 120, "5"),
        _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00", 120, "7"),
    ]
    r = client.post("/shift-plan", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_interval"


def test_shift_plan_bad_timezone_rejected():
    payload = dict(WEEKEND_PAYLOAD, timezone="Not/AZone")
    r = client.post("/shift-plan", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_timezone"


def test_shift_plan_unknown_version_404():
    payload = dict(WEEKEND_PAYLOAD, version="nope")
    r = client.post("/shift-plan", json=payload)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"


def test_shift_plan_requires_exactly_two_tasks():
    payload = dict(WEEKEND_PAYLOAD)
    payload["tasks"] = WEEKEND_PAYLOAD["tasks"][:1]
    assert client.post("/shift-plan", json=payload).status_code == 422
    payload["tasks"] = WEEKEND_PAYLOAD["tasks"] * 2
    assert client.post("/shift-plan", json=payload).status_code == 422


def test_shift_plan_duration_must_be_integer_minutes():
    for bad in (True, 60.5, "abc"):
        payload = dict(WEEKEND_PAYLOAD)
        payload["tasks"] = [
            _task("2024-07-05T20:00:00-04:00", "2024-07-06T04:00:00-04:00",
                  bad, "5"),
            WEEKEND_PAYLOAD["tasks"][1],
        ]
        assert client.post("/shift-plan", json=payload).status_code == 422, bad
