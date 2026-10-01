"""End-to-end tests for POST /usage-estimate (in-memory SQLite)."""
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


GRID = Decimal("0.000001")

# Saturday 2024-07-06 00:00 -> 04:00 local: leisure rule, flat 0.15 up to the
# 100 kWh cumulative cap, then 0.18.
SAT_INTERVAL = {
    "start": "2024-07-06T00:00:00-04:00",
    "end": "2024-07-06T04:00:00-04:00",
    "timezone": "America/New_York",
}


def _quote_amount(energy, interval=SAT_INTERVAL, version="us-ny-demo-v2024"):
    """Bill amount (2 dp) an independent /quote returns for ``energy``."""
    r = client.post("/quote", json={
        "version": version,
        "intervals": [dict(interval, energy=str(energy))],
    })
    assert r.status_code == 200, r.text
    return Decimal(r.json()["total_amount"])


def _estimate(**over):
    payload = {
        "version": "us-ny-demo-v2024",
        "interval": SAT_INTERVAL,
        "billed_amount": "1.50",
        "max_energy": "100",
    }
    payload.update(over)
    return client.post("/usage-estimate", json=payload)


# ------------------------------------------------------------------ solvable

def test_estimate_plateau_boundaries_match_quote():
    r = _estimate()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is True
    assert body["reason_code"] is None and body["reason"] is None
    assert body["lower_neighbor"] is None and body["upper_neighbor"] is None
    lo = Decimal(body["min_energy"])
    hi = Decimal(body["max_energy"])
    billed = Decimal("1.50")
    # 10 kWh @0.15 -> exactly 1.50, so the plateau contains it.
    assert lo <= Decimal("10") <= hi
    # Defining property of the plateau edges: both ends bill the target and
    # the adjacent grid points just outside step off it.
    assert _quote_amount(lo) == billed
    assert _quote_amount(lo - GRID) < billed
    assert _quote_amount(hi) == billed
    assert _quote_amount(hi + GRID) > billed
    # End quotes carry the target bill and agree with independent /quote calls.
    assert body["min_quote"]["total_amount"] == "1.50"
    assert body["max_quote"]["total_amount"] == "1.50"
    direct = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [dict(SAT_INTERVAL, energy=str(lo))],
    }).json()
    assert body["min_quote"]["segments"] == direct["segments"]
    assert body["min_quote"]["total_energy"] == direct["total_energy"]
    assert body["matched_rules"] == ["leisure"]
    # Binary search over 1e8 grid points stays logarithmic.
    assert 0 < body["quotes_evaluated"] <= 100


def test_estimate_plateau_spans_tier_boundary():
    # leisure tiers: 100 -> 0.15, then 0.18; bill 15.00 sits at the 100 kWh
    # cumulative cap, so the plateau straddles the tier boundary.
    r = _estimate(billed_amount="15.00", max_energy="200")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is True
    lo = Decimal(body["min_energy"])
    hi = Decimal(body["max_energy"])
    assert lo < Decimal("100") < hi
    tiers_min = {s["tier_ordinal"] for s in body["min_quote"]["segments"]}
    tiers_max = {s["tier_ordinal"] for s in body["max_quote"]["segments"]}
    assert tiers_min == {1}
    assert tiers_max == {1, 2}
    billed = Decimal("15.00")
    assert _quote_amount(lo) == billed
    assert _quote_amount(lo - GRID) < billed
    assert _quote_amount(hi) == billed
    assert _quote_amount(hi + GRID) > billed


def test_estimate_zero_bill_includes_zero_usage():
    # Zero usage is treated as zero fee, so 0 is always the left edge of the
    # zero-bill plateau; the right edge is where the bill rounds up to 0.01.
    r = _estimate(billed_amount="0.00", max_energy="1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is True
    assert Decimal(body["min_energy"]) == 0
    # The zero-usage end is a synthetic zero-fee quote without segments.
    assert body["min_quote"]["total_energy"] == "0.000000"
    assert body["min_quote"]["total_amount"] == "0.00"
    assert body["min_quote"]["segments"] == []
    assert body["min_quote"]["matched_rules"] == []
    hi = Decimal(body["max_energy"])
    assert hi > 0
    assert _quote_amount(hi) == Decimal("0.00")
    assert _quote_amount(hi + GRID) == Decimal("0.01")
    # Matched rules come from the positive end of the plateau.
    assert body["matched_rules"] == ["leisure"]


def test_estimate_cross_rule_interval_matched_rules():
    # 2024-07-03 22:00 (workday) -> 2024-07-04 06:00 (Independence Day):
    # /quote with 150 kWh bills 26.63 across two rules.
    interval = {
        "start": "2024-07-03T22:00:00-04:00",
        "end": "2024-07-04T06:00:00-04:00",
        "timezone": "America/New_York",
    }
    r = _estimate(interval=interval, billed_amount="26.63", max_energy="200")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is True
    assert body["matched_rules"] == ["dst-workday", "leisure"]
    lo = Decimal(body["min_energy"])
    hi = Decimal(body["max_energy"])
    assert lo <= Decimal("150") <= hi
    assert _quote_amount(lo, interval) == Decimal("26.63")
    assert _quote_amount(hi, interval) == Decimal("26.63")


def test_estimate_dst_spring_forward_consistent_with_quote():
    # Spring-forward night: 5 UTC hours; 10 kWh bills exactly 1.50 (leisure).
    interval = {
        "start": "2024-03-09T23:30:00-05:00",
        "end": "2024-03-10T05:30:00-04:00",
        "timezone": "America/New_York",
    }
    r = _estimate(interval=interval, billed_amount="1.50", max_energy="20")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is True
    lo = Decimal(body["min_energy"])
    hi = Decimal(body["max_energy"])
    assert lo <= Decimal("10") <= hi
    # The DST-aware split is reused: end quotes match independent /quote calls.
    direct = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": [dict(interval, energy=str(hi))],
    }).json()
    assert body["max_quote"]["total_amount"] == direct["total_amount"]
    assert body["max_quote"]["segments"] == direct["segments"]
    durations = sum(s["duration_seconds"]
                    for s in body["max_quote"]["segments"])
    assert durations == 18000  # 5 UTC hours, not 6 wall-clock hours


# ---------------------------------------------------------------- unsolvable

def test_estimate_amount_above_reachable():
    r = _estimate(billed_amount="999.00", max_energy="1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is False
    assert body["reason_code"] == "amount_above_reachable"
    assert body["min_energy"] is None and body["max_energy"] is None
    assert body["min_quote"] is None and body["max_quote"] is None
    assert body["matched_rules"] == []
    # The reachable ceiling is the bill at max_energy (1 kWh @0.15).
    assert body["lower_neighbor"] == {"energy": "1.000000", "amount": "0.15"}
    assert body["upper_neighbor"] is None


# A synthetic version whose unit price is so high that one 1e-6 kWh grid step
# moves the bill by exactly 0.02, making odd cent amounts unreachable.
HIGH_PRICE_VERSION = {
    "code": "high-price-demo-v1",
    "currency": "USD",
    "energy_unit": "kWh",
    "description": "单价极高的演示版本：一个网格步进超过 1 分，制造舍入跳跃",
    "rules": [{
        "code": "flat", "name": "全天",
        "effective_from": "2024-01-01T00:00:00",
        "effective_to": "2025-01-01T00:00:00",
        "timezone": "UTC", "weekdays": "", "day_scope": "all",
        "time_on": "00:00", "time_off": "24:00",
        "tiers": [{"ordinal": 1, "cumulative_cap": None,
                   "unit_price": "20000"}],
    }],
}

JUMP_INTERVAL = {
    "start": "2024-06-01T00:00:00Z",
    "end": "2024-06-01T01:00:00Z",
    "timezone": "UTC",
}


@pytest.fixture(scope="module", autouse=True)
def _import_high_price_version():
    r = client.post("/versions/import", json=HIGH_PRICE_VERSION)
    assert r.status_code in (200, 201), r.text


def _estimate_jump(billed):
    return client.post("/usage-estimate", json={
        "version": "high-price-demo-v1",
        "interval": JUMP_INTERVAL,
        "billed_amount": billed,
        "max_energy": "0.001",
    })


def test_estimate_rounding_jump_from_zero():
    r = _estimate_jump("0.01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is False
    assert body["reason_code"] == "rounding_jump"
    # Zero usage bills zero; the first positive grid point already bills 0.02.
    assert body["lower_neighbor"] == {"energy": "0.000000", "amount": "0.00"}
    assert body["upper_neighbor"] == {"energy": "0.000001", "amount": "0.02"}


def test_estimate_rounding_jump_mid_grid():
    r = _estimate_jump("0.03")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is False
    assert body["reason_code"] == "rounding_jump"
    assert body["lower_neighbor"] == {"energy": "0.000001", "amount": "0.02"}
    assert body["upper_neighbor"] == {"energy": "0.000002", "amount": "0.04"}


def test_estimate_single_grid_point_plateau():
    r = _estimate_jump("0.02")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solvable"] is True
    assert body["min_energy"] == "0.000001"
    assert body["max_energy"] == "0.000001"
    assert body["min_quote"]["total_amount"] == "0.02"
    assert body["max_quote"]["total_amount"] == "0.02"
    assert body["matched_rules"] == ["flat"]


# ------------------------------------------------------------------ validation

def test_billed_amount_validation():
    # More than two decimal places.
    assert _estimate(billed_amount="1.505").status_code == 422
    # Negative.
    assert _estimate(billed_amount="-0.01").status_code == 422
    # NaN / Infinity are not acceptable amounts.
    assert _estimate(billed_amount="NaN").status_code == 422
    assert _estimate(billed_amount="Infinity").status_code == 422
    # Zero and exact-cent values are fine.
    assert _estimate(billed_amount="0").status_code == 200
    assert _estimate(billed_amount="1.5").status_code == 200


def test_max_energy_validation():
    for bad in ("0", "-1", "1000000.000001", "0.0000001", "NaN"):
        r = _estimate(max_energy=bad)
        assert r.status_code == 422, bad
    # Boundary values are accepted (1e6 kWh ceiling, single grid step).
    assert _estimate(max_energy="1000000").status_code == 200
    assert _estimate(max_energy="0.000001").status_code == 200


def test_interval_and_version_validation():
    # end <= start.
    r = _estimate(interval={
        "start": "2024-07-06T04:00:00-04:00",
        "end": "2024-07-06T00:00:00-04:00",
        "timezone": "America/New_York",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_interval"
    # Unknown version.
    r = _estimate(version="nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"
    # Naive time inside the DST fall-back ambiguity.
    r = _estimate(interval={
        "start": "2024-11-03T01:30:00",
        "end": "2024-11-03T03:00:00",
        "timezone": "America/New_York",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ambiguous_local_time"
    # Unknown timezone.
    r = _estimate(interval={
        "start": "2024-07-06T00:00:00",
        "end": "2024-07-06T04:00:00",
        "timezone": "Mars/Olympus",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_timezone"
    # Extra fields are forbidden (including a stray energy on the interval).
    r = client.post("/usage-estimate", json={
        "version": "us-ny-demo-v2024",
        "interval": dict(SAT_INTERVAL, energy="5"),
        "billed_amount": "1.50",
        "max_energy": "100",
    })
    assert r.status_code == 422


def test_estimate_rule_gap_propagates_409():
    # June 2025 is outside every rule's effective window of us-ny-demo-v2024.
    r = _estimate(interval={
        "start": "2025-06-01T00:00:00-04:00",
        "end": "2025-06-01T01:00:00-04:00",
        "timezone": "America/New_York",
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "rule_gap"
