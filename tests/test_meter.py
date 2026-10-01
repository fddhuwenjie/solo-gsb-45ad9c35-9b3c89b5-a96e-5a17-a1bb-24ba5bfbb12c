"""End-to-end tests for POST /meter-readings/reconcile (in-memory SQLite)."""
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


def _reading(at, value):
    return {"at": at, "value": value}


def _payload(readings, modulus="1000", max_interval_seconds=3600, resets=None):
    body = {
        "readings": readings,
        "modulus": modulus,
        "max_interval_seconds": max_interval_seconds,
    }
    if resets is not None:
        body["resets"] = resets
    return body


# --------------------------------------------------------------- happy paths

def test_basic_reconcile_sorts_by_utc_and_keeps_original_indices():
    # Readings arrive out of order and with mixed offset notations.
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T02:00:00Z", "150"),
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00+00:00", "120"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reading_count"] == 3
    assert body["duplicate_count"] == 0
    assert body["gaps"] == []
    intervals = body["intervals"]
    assert [(i["start"], i["end"]) for i in intervals] == [
        ("2024-07-01T00:00:00Z", "2024-07-01T01:00:00Z"),
        ("2024-07-01T01:00:00Z", "2024-07-01T02:00:00Z"),
    ]
    # from/to indices point at the *original* input positions.
    assert [(i["from_index"], i["to_index"]) for i in intervals] == [(1, 2),
                                                                     (2, 0)]
    assert [i["energy"] for i in intervals] == ["20", "30"]
    assert all(i["wrapped"] is False for i in intervals)
    assert all(i["duration_seconds"] == 3600 for i in intervals)
    assert body["total_energy"] == "50"
    # Half-open chain: adjacent intervals share exactly one boundary.
    assert intervals[0]["end"] == intervals[1]["start"]


def test_same_instant_same_value_is_deduped():
    # 08:00 at +08:00 is the same UTC instant as 00:00Z.
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T08:00:00+08:00", "100"),
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "110"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reading_count"] == 2
    assert body["duplicate_count"] == 1
    # The first occurrence (input index 0) is kept as the representative.
    assert body["intervals"] == [
        {"start": "2024-07-01T00:00:00Z", "end": "2024-07-01T01:00:00Z",
         "duration_seconds": 3600, "from_index": 0, "to_index": 2,
         "wrapped": False, "energy": "10"},
    ]
    assert body["total_energy"] == "10"


def test_same_instant_different_value_rejected():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T08:00:00+08:00", "100"),
        _reading("2024-07-01T00:00:00Z", "101"),
        _reading("2024-07-01T01:00:00Z", "110"),
    ]))
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "conflicting_readings"
    assert body["details"]["at"] == "2024-07-01T00:00:00Z"
    assert body["details"]["indices"] == [0, 1]
    assert body["details"]["values"] == ["100", "101"]


def test_drop_is_computed_as_single_modulus_wrap():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "900"),
        _reading("2024-07-01T01:00:00Z", "100"),
    ], modulus="1000"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gaps"] == []
    assert body["intervals"][0]["wrapped"] is True
    # (100 - 900) + 1000 = 200
    assert body["intervals"][0]["energy"] == "200"
    assert body["total_energy"] == "200"


def test_reset_breaks_sequence_and_only_records_gap():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
        _reading("2024-07-01T02:00:00Z", "10"),
        _reading("2024-07-01T03:00:00Z", "40"),
    ], resets=["2024-07-01T02:00:00Z"]))
    assert r.status_code == 200, r.text
    body = r.json()
    # The pair leading into the reset reading is a gap, not a wraparound,
    # even though 10 < 150; the reset reading starts a new sequence.
    assert [(i["start"], i["energy"]) for i in body["intervals"]] == [
        ("2024-07-01T00:00:00Z", "50"),
        ("2024-07-01T02:00:00Z", "30"),
    ]
    assert body["gaps"] == [
        {"start": "2024-07-01T01:00:00Z", "end": "2024-07-01T02:00:00Z",
         "duration_seconds": 3600, "from_index": 1, "to_index": 2,
         "reason": "reset"},
    ]
    assert body["total_energy"] == "80"


def test_reset_at_first_reading_is_a_noop():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ], resets=["2024-07-01T00:00:00Z", "2024-07-01T00:00:00Z"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gaps"] == []
    assert len(body["intervals"]) == 1
    assert body["total_energy"] == "50"


def test_reset_must_hit_a_reading():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ], resets=["2024-07-01T00:30:00Z"]))
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "reset_not_on_reading"
    assert body["details"]["at"] == "2024-07-01T00:30:00Z"


def test_sampling_interval_gap_only_records_gap():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
        _reading("2024-07-01T05:00:00Z", "400"),
        _reading("2024-07-01T06:00:00Z", "450"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["energy"] for i in body["intervals"]] == ["50", "50"]
    assert body["gaps"] == [
        {"start": "2024-07-01T01:00:00Z", "end": "2024-07-01T05:00:00Z",
         "duration_seconds": 14400, "from_index": 1, "to_index": 2,
         "reason": "max_interval_exceeded"},
    ]
    # The usage across the gap (250) is unknown and must not appear.
    assert body["total_energy"] == "100"


def test_interval_exactly_at_max_is_not_a_gap():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ], max_interval_seconds=3600))
    assert r.status_code == 200, r.text
    assert r.json()["gaps"] == []
    assert len(r.json()["intervals"]) == 1


def test_stationary_readings_produce_no_interval_and_no_gap():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "100"),
        _reading("2024-07-01T02:00:00Z", "130"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gaps"] == []
    # The zero-delta pair yields no interval (/quote requires energy > 0).
    assert [(i["start"], i["energy"]) for i in body["intervals"]] == [
        ("2024-07-01T01:00:00Z", "30"),
    ]
    assert body["total_energy"] == "30"


def test_reconciliation_invariant_holds_across_wraps_and_gaps():
    # Two continuous runs separated by a gap; the first run wraps once.
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "90"),
        _reading("2024-07-01T01:00:00Z", "10"),
        _reading("2024-07-01T02:00:00Z", "95"),
        _reading("2024-07-01T10:00:00Z", "50"),
        _reading("2024-07-01T11:00:00Z", "70"),
    ], modulus="100"))
    assert r.status_code == 200, r.text
    body = r.json()
    intervals = body["intervals"]
    assert [i["energy"] for i in intervals] == ["20", "85", "20"]
    assert [i["wrapped"] for i in intervals] == [True, False, False]
    # Run 1: 90 -> 10 -> 95 sums to (95 - 90) + 1 * modulus = 105.
    # Run 2: 50 -> 70 sums to 20. Total = 125.
    assert body["total_energy"] == "125"
    assert (sum(Decimal(i["energy"]) for i in intervals)
            == Decimal(body["total_energy"]))
    assert len(body["gaps"]) == 1
    assert body["gaps"][0]["reason"] == "max_interval_exceeded"


def test_intervals_feed_directly_into_quote():
    # 04:00Z-06:00Z == 2024-07-06 00:00-02:00 in New York (a Saturday).
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-06T04:00:00Z", "100"),
        _reading("2024-07-06T05:00:00Z", "140"),
        _reading("2024-07-06T06:00:00Z", "175"),
    ]))
    assert r.status_code == 200, r.text
    intervals = [
        {"start": i["start"], "end": i["end"], "timezone": "UTC",
         "energy": i["energy"]}
        for i in r.json()["intervals"]
    ]
    q = client.post("/quote", json={
        "version": "us-ny-demo-v2024",
        "intervals": intervals,
    })
    assert q.status_code == 200, q.text
    # 2024-07-06 is a Saturday -> leisure tier 1 @0.15 for all 75 kWh.
    assert Decimal(q.json()["total_energy"]) == Decimal("75")
    assert q.json()["total_amount"] == "11.25"


# -------------------------------------------------------------------- errors

def test_reading_value_out_of_range_rejected():
    for bad in ("1000", "-1"):
        r = client.post("/meter-readings/reconcile", json=_payload([
            _reading("2024-07-01T00:00:00Z", "100"),
            _reading("2024-07-01T01:00:00Z", bad),
        ], modulus="1000"))
        assert r.status_code == 400, bad
        body = r.json()["error"]
        assert body["code"] == "reading_out_of_range"
        assert body["details"]["index"] == 1


def test_reading_value_equal_to_modulus_rejected():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "0"),
        _reading("2024-07-01T01:00:00Z", "1000"),
    ], modulus="1000"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "reading_out_of_range"


def test_naive_reading_time_rejected():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "offset_required"


def test_bad_reading_time_format_rejected():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("not-a-time", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_datetime"


def test_naive_reset_time_rejected():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ], resets=["2024-07-01T01:00:00"]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "offset_required"


def test_reset_matches_reading_across_offset_notations():
    # Reset given with +01:00 offset hits the 00:00Z reading at 01:00+01:00.
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "100"),
        _reading("2024-07-01T01:00:00Z", "150"),
    ], resets=["2024-07-01T02:00:00+01:00"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gaps"][0]["reason"] == "reset"
    assert body["intervals"] == []


# ------------------------------------------------------------- schema (422)

def test_readings_count_bounds():
    one = _payload([_reading("2024-07-01T00:00:00Z", "1")])
    assert client.post("/meter-readings/reconcile", json=one).status_code == 422
    too_many = _payload([
        _reading("2024-07-01T00:00:00Z", "1"),
        *[_reading("2024-07-01T01:00:00Z", "2")] * 500,
    ])
    assert client.post(
        "/meter-readings/reconcile", json=too_many).status_code == 422


def test_modulus_must_be_positive_finite_decimal():
    base = [_reading("2024-07-01T00:00:00Z", "1"),
            _reading("2024-07-01T01:00:00Z", "2")]
    for bad in ("0", "-5", "NaN", "Infinity"):
        assert client.post("/meter-readings/reconcile", json=_payload(
            base, modulus=bad)).status_code == 422, bad


def test_max_interval_seconds_must_be_positive_integer():
    base = [_reading("2024-07-01T00:00:00Z", "1"),
            _reading("2024-07-01T01:00:00Z", "2")]
    for bad in (0, -10, 1.5, True, "3600"):
        assert client.post("/meter-readings/reconcile", json=_payload(
            base, max_interval_seconds=bad)).status_code == 422, bad


def test_extra_fields_rejected():
    body = _payload([_reading("2024-07-01T00:00:00Z", "1"),
                     _reading("2024-07-01T01:00:00Z", "2")])
    body["version"] = "nope"
    assert client.post("/meter-readings/reconcile", json=body).status_code == 422
    body = _payload([dict(_reading("2024-07-01T00:00:00Z", "1"), unit="kWh"),
                     _reading("2024-07-01T01:00:00Z", "2")])
    assert client.post("/meter-readings/reconcile", json=body).status_code == 422


def test_non_finite_reading_value_rejected():
    r = client.post("/meter-readings/reconcile", json=_payload([
        _reading("2024-07-01T00:00:00Z", "NaN"),
        _reading("2024-07-01T01:00:00Z", "2"),
    ]))
    assert r.status_code == 422
