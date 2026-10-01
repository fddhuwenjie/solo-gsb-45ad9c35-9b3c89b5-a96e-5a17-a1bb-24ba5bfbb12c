"""POST /demand-charge：最大需量结算的端到端测试。"""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app.demand import settle  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import DemandChargeRequest  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _startup():
    with client:
        yield


def _req(**over):
    base = dict(
        timezone="UTC",
        window_minutes=60,
        unit_price="10",
        intervals=[{
            "start": "2024-06-01T00:00:00Z",
            "end": "2024-06-01T03:00:00Z",
            "timezone": "UTC",
            "energy": "30",
        }],
    )
    base.update(over)
    return base


# --------------------------------------------------------------- basic settle

def test_basic_flat_three_hours():
    r = client.post("/demand-charge", json=_req())
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["timezone"] == "UTC"
    assert b["window_minutes"] == 60
    assert b["unit_price"] == "10"
    assert b["eligible_window_count"] == 3
    sw = b["selected_window"]
    # Constant load -> all windows tie at 10 kWh/10 kW; earliest UTC wins.
    assert sw["start"] == "2024-06-01T00:00:00Z"
    assert sw["end"] == "2024-06-01T01:00:00Z"
    assert sw["duration_seconds"] == 3600
    assert sw["energy"] == "10.000000"
    assert sw["average_power_kw"] == "10.000000"
    assert b["demand_kw"] == "10.000000"
    assert b["amount"] == "100.000000"
    assert b["reason_code"] is None
    assert len(sw["allocations"]) == 1
    a = sw["allocations"][0]
    assert a["interval_index"] == 0
    assert a["energy"] == "10.000000"
    assert a["demand_kw"] == "10.000000"
    assert a["amount"] == "100.000000"


def test_winner_is_highest_average_power():
    # One hour with a big load followed by a long light-load hour.
    r = client.post("/demand-charge", json=_req(intervals=[
        {"start": "2024-06-01T00:00:00Z", "end": "2024-06-01T01:00:00Z",
         "timezone": "UTC", "energy": "100"},
        {"start": "2024-06-01T01:00:00Z", "end": "2024-06-01T02:00:00Z",
         "timezone": "UTC", "energy": "5"},
    ]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["selected_window"]["start"] == "2024-06-01T00:00:00Z"
    assert b["selected_window"]["energy"] == "100.000000"
    assert b["demand_kw"] == "100.000000"
    assert b["amount"] == "1000.000000"


def test_demand_and_amount_billed_at_six_decimals_half_up():
    # 1 kWh over 15 min = 4 kW; choose a price producing >6 dp products.
    r = client.post("/demand-charge", json=_req(
        window_minutes=15, unit_price="0.333333",
        intervals=[{
            "start": "2024-06-01T00:00:00Z",
            "end": "2024-06-01T00:15:00Z",
            "timezone": "UTC", "energy": "1",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["demand_kw"] == "4.000000"
    # 4 * 0.333333 = 1.333332 exactly
    assert b["amount"] == "1.333332"


def test_30_and_15_minute_grids():
    for minutes, energy in ((30, "1"), (15, "0.25")):
        r = client.post("/demand-charge", json=_req(
            window_minutes=minutes,
            intervals=[{
                "start": "2024-06-01T00:00:00Z",
                "end": f"2024-06-01T00:{minutes:02d}:00Z",
                "timezone": "UTC", "energy": energy,
            }]))
        assert r.status_code == 200, r.text
        assert r.json()["selected_window"]["duration_seconds"] == minutes * 60


def test_window_aligns_to_local_midnight_not_first_input():
    # Input 23:00 local -> 02:00 local in New York (3 whole hours); the
    # windows are local-midnight anchored at 23:00, 00:00 and 01:00 starts,
    # not a grid starting at the first input (no 3-hour grid phase).
    r = client.post("/demand-charge", json=_req(
        timezone="America/New_York",
        intervals=[{
            "start": "2024-06-01T23:00:00-04:00",
            "end": "2024-06-02T02:00:00-04:00",
            "timezone": "America/New_York", "energy": "3",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["eligible_window_count"] == 3
    assert b["window_count"] == 3
    # Earliest tied window wins (flat 1 kWh/h load): local 23:00.
    sw = b["selected_window"]
    assert sw["start"] == "2024-06-02T03:00:00Z"
    assert sw["start_local"] == "2024-06-01T23:00:00-04:00"
    # The half-open end means there is no window starting at local 02:00.
    assert b["excluded_windows"] == []


# ----------------------------------------------------- unequal-length prorating

def test_cross_window_proration_exact_by_elapsed_duration():
    # 1500-second interval with 10 kWh: [00:05, 00:30). Only [00:15,00:30)
    # is fully covered (900 s); [00:00,00:15) only intersects for 600 s.
    start = datetime(2024, 6, 1, 0, 5, tzinfo=timezone.utc)
    end = datetime(2024, 6, 1, 0, 30, tzinfo=timezone.utc)
    r = client.post("/demand-charge", json=_req(
        window_minutes=15,
        intervals=[{
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timezone": "UTC", "energy": "10",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    sw = b["selected_window"]
    assert sw["start"] == "2024-06-01T00:15:00Z"
    assert sw["end"] == "2024-06-01T00:30:00Z"
    # 900/1500 * 10 = 6 kWh exactly; power = 6 kWh / 0.25 h = 24 kW.
    assert sw["energy"] == "6.000000"
    assert b["demand_kw"] == "24.000000"
    a = sw["allocations"][0]
    assert a["start"] == "2024-06-01T00:15:00Z"
    assert a["end"] == "2024-06-01T00:30:00Z"
    assert a["duration_seconds"] == 900
    reasons = {(e["start"], tuple(e["reasons"]))
               for e in b["excluded_windows"]}
    assert ("2024-06-01T00:00:00Z", ("partial_coverage",)) in reasons


def test_per_input_energy_conservation_engine():
    """Every input's slices across ALL windows sum exactly to its energy,
    including slices inside non-eligible (partial) windows."""
    req = DemandChargeRequest(
        timezone="UTC", window_minutes=15, unit_price="2.75",
        intervals=[
            {"start": "2024-06-01T00:07:00Z",
             "end": "2024-06-01T01:23:00Z",
             "timezone": "UTC", "energy": "10.123456"},
            {"start": "2024-06-01T01:23:00Z",
             "end": "2024-06-01T03:00:00Z",
             "timezone": "UTC", "energy": "42.7"},
        ])
    res = settle(req)
    for idx, total in ((0, Decimal("10.123456")), (1, Decimal("42.7"))):
        got = sum((s.energy for w in res.windows for s in w.slices
                   if s.interval_index == idx), Decimal())
        assert got == total
    # Winner allocations conserve both energy and billed amount.
    alloc = res.allocations
    assert sum((a.energy for a in alloc), Decimal()) == res.winner.energy
    assert sum((a.amount for a in alloc), Decimal()) == res.amount
    # Per-slice microseconds match the actual UTC overlap.
    for w in res.windows:
        for s in w.slices:
            assert s.microseconds == int(
                (s.end - s.start).total_seconds() * 1_000_000)


def test_abutting_inputs_count_as_continuous_coverage():
    # Two intervals meeting exactly on a window boundary: windows spanning
    # the seam are eligible (no gap), and the higher-power second interval
    # wins with its energy intact.
    r = client.post("/demand-charge", json=_req(
        window_minutes=30,
        intervals=[
            {"start": "2024-06-01T00:00:00Z",
             "end": "2024-06-01T00:30:00Z",
             "timezone": "UTC", "energy": "3"},
            {"start": "2024-06-01T00:30:00Z",
             "end": "2024-06-01T01:00:00Z",
             "timezone": "UTC", "energy": "9"},
        ]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["eligible_window_count"] == 2
    assert b["window_count"] == 2
    sw = b["selected_window"]
    assert sw["start"] == "2024-06-01T00:30:00Z"
    assert sw["energy"] == "9.000000"
    assert sw["allocations"][0]["interval_index"] == 1
    # No partial/edge windows at all.
    assert b["excluded_windows"] == []


# ------------------------------------------------------- per-input shares

def test_interval_shares_cover_all_overlapping_windows():
    """The reported case: abutting inputs 00:00-00:10 (1 kWh) and
    00:10-00:30 (4 kWh) on a 15-minute grid. The winner [00:15,00:30)
    holds only 3 kWh of input 1; interval_shares must still return input 0
    and input 1's 1 kWh slice in the previous window, each group summing
    exactly to its declared energy."""
    r = client.post("/demand-charge", json=_req(
        window_minutes=15,
        intervals=[
            {"start": "2024-06-01T00:00:00Z",
             "end": "2024-06-01T00:10:00Z",
             "timezone": "UTC", "energy": "1"},
            {"start": "2024-06-01T00:10:00Z",
             "end": "2024-06-01T00:30:00Z",
             "timezone": "UTC", "energy": "4"},
        ]))
    assert r.status_code == 200, r.text
    b = r.json()
    sw = b["selected_window"]
    assert sw["start"] == "2024-06-01T00:15:00Z"
    # Winner-window fee split stays scoped to the winner: only input 1's
    # 3 kWh slice participates there.
    assert [(a["interval_index"], a["energy"]) for a in sw["allocations"]] == \
        [(1, "3.000000")]
    # The independent per-input view covers every overlapping window.
    shares = {s["interval_index"]: s for s in b["interval_shares"]}
    assert set(shares) == {0, 1}
    assert shares[0]["energy"] == "1.000000"
    assert [(s["window_index"], s["start"], s["end"], s["energy"])
            for s in shares[0]["slices"]] == [
        (0, "2024-06-01T00:00:00Z", "2024-06-01T00:10:00Z", "1.000000"),
    ]
    assert shares[1]["energy"] == "4.000000"
    assert [(s["window_index"], s["start"], s["end"], s["energy"])
            for s in shares[1]["slices"]] == [
        (0, "2024-06-01T00:10:00Z", "2024-06-01T00:15:00Z", "1.000000"),
        (1, "2024-06-01T00:15:00Z", "2024-06-01T00:30:00Z", "3.000000"),
    ]
    # Group sums strictly equal the declared inputs.
    for s in b["interval_shares"]:
        assert sum(Decimal(x["energy"]) for x in s["slices"]) == \
            Decimal(s["energy"])
    # Slice window_index matches the enumerated windows.
    assert shares[1]["slices"][1]["window_index"] == sw["window_index"]


def test_interval_shares_cross_window_proration():
    # 1500-second interval with 10 kWh: [00:05, 00:30) -> 4 kWh in the
    # partial window [00:00,00:15) and 6 kWh in the winner [00:15,00:30).
    r = client.post("/demand-charge", json=_req(
        window_minutes=15,
        intervals=[{
            "start": "2024-06-01T00:05:00Z",
            "end": "2024-06-01T00:30:00Z",
            "timezone": "UTC", "energy": "10",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["interval_shares"]) == 1
    share = b["interval_shares"][0]
    assert share["interval_index"] == 0
    assert share["energy"] == "10.000000"
    assert [(s["window_index"], s["duration_seconds"], s["energy"])
            for s in share["slices"]] == [
        (0, 600, "4.000000"),
        (1, 900, "6.000000"),
    ]
    # The slice in the partial (non-eligible) window is reported too, even
    # though that window is excluded from selection.
    excluded = {e["window_index"] for e in b["excluded_windows"]}
    assert share["slices"][0]["window_index"] in excluded


def test_interval_shares_present_without_complete_window():
    # No window is fully covered: selected_window is null, but the per-input
    # shares still list every slice and conserve the input energy.
    r = client.post("/demand-charge", json=_req(
        window_minutes=60,
        intervals=[{
            "start": "2024-06-01T00:10:00Z",
            "end": "2024-06-01T00:50:00Z",
            "timezone": "UTC", "energy": "1",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["selected_window"] is None
    assert b["reason_code"] == "no_complete_window"
    assert len(b["interval_shares"]) == 1
    share = b["interval_shares"][0]
    assert share["energy"] == "1.000000"
    assert sum(Decimal(s["energy"]) for s in share["slices"]) == \
        Decimal("1.000000")
    assert [s["window_index"] for s in share["slices"]] == [0]
    assert share["slices"][0]["duration_seconds"] == 2400


def test_interval_shares_engine_conservation_over_dst_fallback():
    """Fall-back day in New York: the single 25-hour input is prorated over
    100 fifteen-minute windows; the engine's per-input view must return all
    100 slices in UTC order, summing exactly to the declared energy."""
    req = DemandChargeRequest(
        timezone="America/New_York", window_minutes=15, unit_price="1",
        intervals=[{
            "start": "2024-11-03T00:00:00-04:00",
            "end": "2024-11-04T00:00:00-05:00",
            "timezone": "America/New_York", "energy": "25",
        }])
    res = settle(req)
    assert len(res.interval_shares) == 1
    share = res.interval_shares[0]
    assert share.interval_index == 0
    assert len(share.slices) == 100
    assert share.energy == Decimal("25")
    assert sum((s.energy for s in share.slices), Decimal()) == Decimal("25")
    assert all(s.energy == Decimal("0.25") for s in share.slices)
    # Chronological along the UTC timeline, one slice per window.
    assert [s.window_index for s in share.slices] == list(range(100))
    starts = [s.start for s in share.slices]
    assert all(b > a for a, b in zip(starts, starts[1:]))


def test_interval_shares_response_over_spring_forward():
    # 23-hour spring-forward day: response-level check that every one of
    # the 92 windows appears exactly once in the single input's shares.
    r = client.post("/demand-charge", json=_req(
        timezone="America/New_York", window_minutes=15, unit_price="5",
        intervals=[{
            "start": "2024-03-10T00:00:00-05:00",
            "end": "2024-03-11T00:00:00-04:00",
            "timezone": "America/New_York", "energy": "23",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["eligible_window_count"] == 92
    share = b["interval_shares"][0]
    assert share["energy"] == "23.000000"
    assert len(share["slices"]) == 92
    assert sum(Decimal(s["energy"]) for s in share["slices"]) == \
        Decimal("23.000000")
    assert [s["window_index"] for s in share["slices"]] == list(range(92))
    # Local wall labels are echoed with their real offsets (EDT throughout
    # the day after 02:00; the first slices carry -05:00).
    assert share["slices"][0]["start_local"] == "2024-03-10T00:00:00-05:00"
    assert share["slices"][-1]["end_local"] == "2024-03-11T00:00:00-04:00"


def test_fee_split_across_inputs_proportional_to_energy():
    # Two abutting inputs with different power share one window: input 0
    # [00:00,00:30) = 3 kWh, input 1 [00:30,01:00) = 9 kWh. The window
    # [00:00,01:00) (60-minute grid) holds both slices (3 and 9); the fee
    # is split 1:3 and reconciles exactly to the billed amount.
    r = client.post("/demand-charge", json=_req(
        window_minutes=60, unit_price="10",
        intervals=[
            {"start": "2024-06-01T00:00:00Z",
             "end": "2024-06-01T00:30:00Z",
             "timezone": "UTC", "energy": "3"},
            {"start": "2024-06-01T00:30:00Z",
             "end": "2024-06-01T01:00:00Z",
             "timezone": "UTC", "energy": "9"},
        ]))
    assert r.status_code == 200, r.text
    b = r.json()
    sw = b["selected_window"]
    assert sw["start"] == "2024-06-01T00:00:00Z"
    assert sw["energy"] == "12.000000"
    assert b["demand_kw"] == "12.000000"
    assert b["amount"] == "120.000000"
    alloc = {a["interval_index"]: a for a in sw["allocations"]}
    assert set(alloc) == {0, 1}
    assert alloc[0]["energy"] == "3.000000"
    assert alloc[1]["energy"] == "9.000000"
    assert alloc[0]["amount"] == "30.000000"
    assert alloc[1]["amount"] == "90.000000"
    # Allocation windows stay inside their owning input.
    assert alloc[0]["end"] == "2024-06-01T00:30:00Z"
    assert alloc[1]["start"] == "2024-06-01T00:30:00Z"
    # Per-input demand shares: 3 kWh / 1 h and 9 kWh / 1 h.
    assert alloc[0]["demand_kw"] == "3.000000"
    assert alloc[1]["demand_kw"] == "9.000000"


def test_fee_residual_lands_on_last_allocation():
    # Three equal-energy contributors in one window with a price whose
    # thirds do not terminate at 6 dp: shares must still sum exactly.
    r = client.post("/demand-charge", json=_req(
        window_minutes=30, unit_price="1.000001",
        intervals=[
            {"start": "2024-06-01T00:00:00Z",
             "end": "2024-06-01T00:10:00Z",
             "timezone": "UTC", "energy": "1"},
            {"start": "2024-06-01T00:10:00Z",
             "end": "2024-06-01T00:20:00Z",
             "timezone": "UTC", "energy": "1"},
            {"start": "2024-06-01T00:20:00Z",
             "end": "2024-06-01T00:30:00Z",
             "timezone": "UTC", "energy": "1"},
        ]))
    assert r.status_code == 200, r.text
    b = r.json()
    alloc = b["selected_window"]["allocations"]
    assert len(alloc) == 3
    assert sum(Decimal(a["amount"]) for a in alloc) == Decimal(b["amount"])
    assert sum(Decimal(a["energy"]) for a in alloc) == Decimal(
        b["selected_window"]["energy"])


# --------------------------------------------------------- window eligibility

def test_partial_edge_windows_excluded_with_reasons():
    # 00:30-02:30 with 60-minute grid: only [01:00, 02:00) is complete.
    r = client.post("/demand-charge", json=_req(
        window_minutes=60,
        intervals=[{
            "start": "2024-06-01T00:30:00Z",
            "end": "2024-06-01T02:30:00Z",
            "timezone": "UTC", "energy": "2",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["eligible_window_count"] == 1
    assert b["selected_window"]["start"] == "2024-06-01T01:00:00Z"
    reasons = {e["start"]: e["reasons"] for e in b["excluded_windows"]}
    assert reasons["2024-06-01T00:00:00Z"] == ["partial_coverage"]
    assert reasons["2024-06-01T02:00:00Z"] == ["partial_coverage"]


def test_internal_gap_window_not_covered():
    # 00:00-01:00 covered, 01:00-02:00 gap, 02:00-03:00 covered; 30-min grid.
    r = client.post("/demand-charge", json=_req(
        window_minutes=30,
        intervals=[
            {"start": "2024-06-01T00:00:00Z",
             "end": "2024-06-01T01:00:00Z",
             "timezone": "UTC", "energy": "2"},
            {"start": "2024-06-01T02:00:00Z",
             "end": "2024-06-01T03:00:00Z",
             "timezone": "UTC", "energy": "2"},
        ]))
    assert r.status_code == 200, r.text
    b = r.json()
    reasons = {e["start"]: e["reasons"] for e in b["excluded_windows"]}
    assert reasons["2024-06-01T01:00:00Z"] == ["not_covered"]
    assert reasons["2024-06-01T01:30:00Z"] == ["not_covered"]
    # Edges of the covered blocks are fully covered themselves.
    assert "2024-06-01T00:30:00Z" not in reasons
    assert "2024-06-01T02:00:00Z" not in reasons
    # The four half-hour windows inside the blocks are eligible.
    assert b["eligible_window_count"] == 4


def test_no_complete_window_returns_200_with_reason():
    r = client.post("/demand-charge", json=_req(
        window_minutes=60,
        intervals=[{
            "start": "2024-06-01T00:10:00Z",
            "end": "2024-06-01T00:50:00Z",
            "timezone": "UTC", "energy": "1",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["selected_window"] is None
    assert b["demand_kw"] == "0.000000"
    assert b["amount"] == "0.000000"
    assert b["reason_code"] == "no_complete_window"
    assert b["eligible_window_count"] == 0
    assert b["excluded_windows"][0]["reasons"] == ["partial_coverage"]


# ------------------------------------------------------------------ DST cases

def test_fall_back_repeated_local_boundaries_are_separate_windows():
    """2024-11-03 NY falls back 02:00 -> 01:00: the 25-hour local day must
    produce 100 fifteen-minute windows (25 UTC hours), and the repeated
    01:00/01:15/01:30/01:45 wall labels each appear twice with distinct
    offsets (-04:00 then -05:00)."""
    req = DemandChargeRequest(
        timezone="America/New_York", window_minutes=15, unit_price="1",
        intervals=[{
            "start": "2024-11-03T00:00:00-04:00",
            "end": "2024-11-04T00:00:00-05:00",
            "timezone": "America/New_York", "energy": "25",
        }])
    res = settle(req)
    ny = ZoneInfo("America/New_York")
    assert len(res.windows) == 100
    assert sum((w.end - w.start).total_seconds() for w in res.windows) == \
        25 * 3600
    labels = {}
    for w in res.windows:
        key = w.start.astimezone(ny).strftime("%Y-%m-%dT%H:%M")
        labels.setdefault(key, []).append(
            w.start.astimezone(ny).utcoffset())
    for hm in ("01:00", "01:15", "01:30", "01:45"):
        key = f"2024-11-03T{hm}"
        assert labels[key] == [timedelta(hours=-4), timedelta(hours=-5)]
    # Every window in the fully covered day is eligible.
    assert all(w.eligible for w in res.windows)


def test_spring_forward_day_has_23_hours_of_windows():
    """2024-03-10 NY springs 02:00 -> 03:00: 92 fifteen-minute windows
    (23 UTC hours); no fabricated nonexistent local boundaries, and every
    window is a real fixed-size UTC window."""
    req = DemandChargeRequest(
        timezone="America/New_York", window_minutes=15, unit_price="1",
        intervals=[{
            "start": "2024-03-10T00:00:00-05:00",
            "end": "2024-03-11T00:00:00-04:00",
            "timezone": "America/New_York", "energy": "23",
        }])
    res = settle(req)
    assert len(res.windows) == 92
    assert all((w.end - w.start) == timedelta(minutes=15)
               for w in res.windows)
    assert sum((w.end - w.start).total_seconds() for w in res.windows) == \
        23 * 3600
    assert all(w.eligible for w in res.windows)
    # UTC timeline strictly increasing across the gap.
    starts = [w.start for w in res.windows]
    assert starts == sorted(starts)
    assert all(b > a for a, b in zip(starts, starts[1:]))


def test_energy_prorated_over_fall_back_windows_along_utc():
    """Flat power across the 25-hour fall-back day: each of 100 15-minute
    windows gets exactly 0.025 of 2.5 kWh... use 25 kWh -> 0.25 kWh each."""
    r = client.post("/demand-charge", json=_req(
        timezone="America/New_York", window_minutes=15, unit_price="5",
        intervals=[{
            "start": "2024-11-03T00:00:00-04:00",
            "end": "2024-11-04T00:00:00-05:00",
            "timezone": "America/New_York", "energy": "25",
        }]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["eligible_window_count"] == 100
    assert b["selected_window"]["energy"] == "0.250000"
    # 0.25 kWh / 0.25 h = 1 kW * 5 = 5
    assert b["demand_kw"] == "1.000000"
    assert b["amount"] == "5.000000"


def test_cross_local_midnight_reanchors_grid():
    """An interval spanning local midnight in a non-UTC zone: after midnight
    the grid must restart from the new day's local 00:00 UTC instant (which
    is itself a window edge, not an arbitrary offset from day one)."""
    ny = ZoneInfo("America/New_York")
    req = DemandChargeRequest(
        timezone="America/New_York", window_minutes=30, unit_price="1",
        intervals=[{
            "start": "2024-06-01T23:00:00-04:00",
            "end": "2024-06-02T01:00:00-04:00",
            "timezone": "America/New_York", "energy": "2",
        }])
    res = settle(req)
    mid = datetime.combine(date(2024, 6, 2), time.min,
                           tzinfo=ny).astimezone(timezone.utc)
    starts = [w.start for w in res.windows]
    # Midnight is a window start (re-anchored), and 23:00/23:30 before it.
    assert starts == [
        mid - timedelta(hours=1), mid - timedelta(minutes=30),
        mid, mid + timedelta(minutes=30),
    ]
    assert res.winner is not None


# ----------------------------------------------------------------- validation

def test_invalid_window_minutes_rejected():
    for minutes in (10, 45, 0, -15):
        r = client.post("/demand-charge", json=_req(window_minutes=minutes))
        assert r.status_code == 422, minutes


def test_non_positive_price_rejected():
    for price in ("0", "-1.5"):
        r = client.post("/demand-charge", json=_req(unit_price=price))
        assert r.status_code == 422, price


def test_unknown_timezone_rejected():
    r = client.post("/demand-charge", json=_req(timezone="Moon/Base"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_timezone"


def test_overlapping_inputs_rejected():
    r = client.post("/demand-charge", json=_req(intervals=[
        {"start": "2024-06-01T00:00:00Z", "end": "2024-06-01T02:00:00Z",
         "timezone": "UTC", "energy": "1"},
        {"start": "2024-06-01T01:00:00Z", "end": "2024-06-01T03:00:00Z",
         "timezone": "UTC", "energy": "1"},
    ]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "interval_overlap"


def test_ambiguous_naive_input_rejected():
    r = client.post("/demand-charge", json=_req(
        timezone="America/New_York",
        intervals=[{
            "start": "2024-11-03T01:30:00",
            "end": "2024-11-03T03:00:00",
            "timezone": "America/New_York", "energy": "1",
        }]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ambiguous_local_time"


def test_extra_field_rejected():
    r = client.post("/demand-charge", json=_req(currency="USD"))
    assert r.status_code == 422


def test_empty_intervals_rejected():
    body = _req()
    body["intervals"] = []
    r = client.post("/demand-charge", json=body)
    assert r.status_code == 422
