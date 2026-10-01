"""End-to-end tests for POST /cost-allocation (Shapley split of a shared
meter's tiered bill)."""
import os
from decimal import Decimal

import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _startup():
    with client:
        yield


V = "us-ny-demo-v2024"
# Leisure (weekend/holiday) tier prices in v2024: 100 @0.15, 300 @0.18, inf @0.22
# DST workday prices: 100 @0.22, 300 @0.27, inf @0.32


def _iv(start, end, energy, tz="UTC"):
    return {"start": start, "end": end, "timezone": tz, "energy": energy}


def _allocate(members, version=V):
    return client.post("/cost-allocation",
                       json={"version": version, "members": members})


def _member(member_id, *intervals):
    return {"member_id": member_id, "intervals": list(intervals)}


# --------------------------------------------------------------- basic shapes


def test_two_overlapping_members_symmetric_shapley():
    # Both draw 60 kWh over the identical Saturday 2024-07-06 10:00-12:00 UTC.
    # Singleton: 60 @0.15 = 9.00. Merged 120: 100 @0.15 + 20 @0.18 = 18.60.
    # Symmetric Shapley split: (9.00 + (18.60-9.00)) / 2 = 9.30 each.
    r = _allocate([
        _member("A", _iv("2024-07-06T10:00:00Z", "2024-07-06T12:00:00Z", 60)),
        _member("B", _iv("2024-07-06T10:00:00Z", "2024-07-06T12:00:00Z", 60)),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == V
    assert body["currency"] == "USD"
    assert body["energy_unit"] == "kWh"
    assert body["total_energy"] == "120"
    assert body["total_amount"] == "18.600000"
    assert body["coalitions_evaluated"] == 4  # 2^2 incl. the empty coalition

    a, b = body["members"]
    assert a["member_index"] == 0 and a["member_id"] == "A"
    assert b["member_index"] == 1 and b["member_id"] == "B"
    for m in (a, b):
        assert m["energy"] == "60"
        assert m["standalone_amount"] == "9.000000"
        assert m["allocated_amount"] == "9.300000"
        assert m["difference"] == "-0.300000"

    # Allocated shares sum EXACTLY to the merged quote.
    assert sum(Decimal(m["allocated_amount"]) for m in body["members"]) == (
        Decimal(body["total_amount"]))


def test_three_members_energy_only_game():
    # Same leisure rule the whole time, so a coalition's bill is purely a
    # function of its total energy; equal players share the merged bill evenly.
    # 180 kWh: 100 @0.15 + 80 @0.18 = 29.40; 29.40 / 3 = 9.80.
    r = _allocate([
        _member("A", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T12:00:00Z", 60)),
        _member("B", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T11:00:00Z", 60)),
        _member("C", _iv("2024-07-06T11:00:00Z",
                         "2024-07-06T12:00:00Z", 60)),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coalitions_evaluated"] == 8
    assert body["total_energy"] == "180"
    assert body["total_amount"] == "29.400000"
    for m in body["members"]:
        assert m["standalone_amount"] == "9.000000"
        assert m["allocated_amount"] == "9.800000"
        assert m["difference"] == "-0.800000"
    assert sum(Decimal(m["allocated_amount"]) for m in body["members"]) == (
        Decimal("29.400000"))


def test_disjoint_members_below_cap_have_zero_difference():
    # Disjoint windows, combined energy stays in tier 1 -> no subsidy at all.
    r = _allocate([
        _member("A", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T11:00:00Z", 40)),
        _member("B", _iv("2024-07-06T12:00:00Z",
                         "2024-07-06T13:00:00Z", 40)),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_amount"] == "12.000000"
    for m in body["members"]:
        assert m["standalone_amount"] == "6.000000"
        assert m["allocated_amount"] == "6.000000"
        assert m["difference"] == "0.000000"


def test_cross_rule_distribution_is_priced_correctly():
    # Fri 2024-07-05 22:00 -> Sat 02:00 (EDT): 2 workday hours (@0.22) then
    # 2 leisure hours (@0.15). A draws uniformly across all four (20+20 kWh),
    # B draws 80 kWh in the workday half. Merged, the workday block lands on
    # exactly 100 kWh of the quote-wide cumulative counter, so A's later 20
    # leisure kWh fall in leisure tier 2 (@0.18): 100*.22 + 20*.18 = 25.60.
    # v(A)=20*.22+20*.15=7.40; v(B)=80*.22=17.60. Shapley:
    # phi(A)=(7.40+(25.60-17.60))/2=7.70, phi(B)=17.90.
    r = _allocate([
        _member("A", _iv("2024-07-05T22:00:00-04:00",
                         "2024-07-06T02:00:00-04:00", 40,
                         "America/New_York")),
        _member("B", _iv("2024-07-05T22:00:00-04:00",
                         "2024-07-06T00:00:00-04:00", 80,
                         "America/New_York")),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_amount"] == "25.600000"
    a, b = body["members"]
    assert a["standalone_amount"] == "7.400000"
    assert b["standalone_amount"] == "17.600000"
    assert a["allocated_amount"] == "7.700000"
    assert b["allocated_amount"] == "17.900000"
    assert a["difference"] == "-0.300000"
    assert b["difference"] == "-0.300000"


def test_shapley_values_match_independent_enumeration():
    # Independently recompute the Shapley value for three asymmetric players
    # from coalition totals obtained by merging members ourselves.
    members = [
        _member("shop",  _iv("2024-07-06T10:00:00Z",
                             "2024-07-06T12:00:00Z", "33.333")),
        _member("cafe",  _iv("2024-07-06T10:00:00Z",
                             "2024-07-06T11:00:00Z", "47.777")),
        _member("flat",  _iv("2024-07-06T11:00:00Z",
                             "2024-07-06T12:00:00Z", "25.5")),
    ]
    r = _allocate(members)
    assert r.status_code == 200, r.text
    body = r.json()
    n = 3

    # Coalition costs via plain /quote over the merged atomic windows.
    endpoints = ["2024-07-06T10:00:00Z", "2024-07-06T11:00:00Z",
                 "2024-07-06T12:00:00Z"]
    # Atomic-window loads: window 0 (first hour) = shop 16.6665 + cafe 47.777;
    # window 1 (second hour) = shop 16.6665 + flat 25.5.
    loads = {
        0: ["16.6665", "47.777", "0"],
        1: ["16.6665", "0", "25.5"],
    }

    def coalition_total(mask):
        if mask == 0:
            return Decimal("0")
        ivs = []
        for wi, (s, e) in enumerate(zip(endpoints, endpoints[1:])):
            energy = sum((Decimal(loads[wi][i]) for i in range(n)
                          if mask & (1 << i)), Decimal(0))
            if energy > 0:
                ivs.append(_iv(s, e, str(energy)))
        q = client.post("/quote", json={"version": V, "intervals": ivs})
        assert q.status_code == 200, q.text
        return sum((Decimal(s["amount"]) for s in q.json()["segments"]),
                   Decimal(0))

    from itertools import permutations
    totals = {mask: coalition_total(mask) for mask in range(1 << n)}
    expected = []
    for i in range(n):
        acc = Decimal(0)
        for order in permutations(range(n)):
            mask = 0
            for j in order:
                new_mask = mask | (1 << j)
                if j == i:
                    acc += totals[new_mask] - totals[mask]
                    break
                mask = new_mask
        expected.append((acc / 6).quantize(Decimal("0.000001")))

    actual = [Decimal(m["allocated_amount"]) for m in body["members"]]
    # Players 0..n-2 are independently rounded; the last absorbs the residual.
    for i in range(n - 1):
        assert actual[i] == expected[i]
    assert actual[-1] == (
        totals[(1 << n) - 1] - sum(actual[:-1], Decimal(0)))
    assert sum(actual, Decimal(0)) == totals[(1 << n) - 1]
    assert Decimal(body["total_amount"]) == totals[(1 << n) - 1]


# ------------------------------------------------- duration proration & DST


def test_member_energy_conserved_over_awkward_atomic_splits():
    # A draws 10 kWh over 3 hours; B and C carve the hour endpoints inside it,
    # so A's interval is cut into three equal atomic windows.
    r = _allocate([
        _member("A", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T13:00:00Z", 10)),
        _member("B", _iv("2024-07-06T11:00:00Z",
                         "2024-07-06T11:30:00Z", 1)),
        _member("C", _iv("2024-07-06T12:00:00Z",
                         "2024-07-06T12:15:00Z", 1)),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    a = body["members"][0]
    assert Decimal(a["energy"]) == Decimal("10")
    assert Decimal(body["total_energy"]) == Decimal("12")


def test_dst_spring_forward_actual_duration_split():
    # Sat 2024-03-09 23:30 -> Sun 05:30 NY wall time spans only 5 UTC hours
    # (04:30-09:30Z), all leisure. A draws 10 over the whole span; B draws 5
    # in the final UTC hour; merged total 15 @0.15 = 2.25.
    r = _allocate([
        _member("A", _iv("2024-03-09T23:30:00-05:00",
                         "2024-03-10T05:30:00-04:00", 10,
                         "America/New_York")),
        _member("B", _iv("2024-03-10T08:30:00Z",
                         "2024-03-10T09:30:00Z", 5)),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["total_energy"]) == Decimal("15")
    assert body["total_amount"] == "2.250000"
    a, b = body["members"]
    assert a["standalone_amount"] == "1.500000"
    assert b["standalone_amount"] == "0.750000"
    assert sum(Decimal(m["allocated_amount"]) for m in body["members"]) == (
        Decimal("2.250000"))


def test_members_may_use_different_member_timezones():
    # Same physical UTC window expressed once in NY local time and once in Z;
    # cross-member overlap is explicitly allowed.
    r = _allocate([
        _member("ny", _iv("2024-07-06T06:00:00-04:00",
                          "2024-07-06T08:00:00-04:00", 60,
                          "America/New_York")),
        _member("utc", _iv("2024-07-06T10:00:00Z",
                           "2024-07-06T12:00:00Z", 60)),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_energy"] == "120"
    assert body["total_amount"] == "18.600000"


# ---------------------------------------------------------- standalone parity


def test_standalone_amount_matches_quote_segments():
    intervals = [
        _iv("2024-07-05T22:00:00-04:00", "2024-07-06T02:00:00-04:00", 150,
            "America/New_York"),
        _iv("2024-07-06T10:00:00Z", "2024-07-06T11:00:00Z", 20),
    ]
    q = client.post("/quote", json={"version": V, "intervals": intervals})
    assert q.status_code == 200, q.text
    quote_total = sum((Decimal(s["amount"]) for s in q.json()["segments"]),
                      Decimal(0))

    r = _allocate([
        _member("solo", *intervals),
        _member("other", _iv("2024-07-07T00:00:00Z",
                             "2024-07-07T01:00:00Z", 1)),
    ])
    assert r.status_code == 200, r.text
    solo = r.json()["members"][0]
    assert Decimal(solo["standalone_amount"]) == quote_total


# -------------------------------------------------------------- last residual


def test_last_member_absorbs_rounding_residual_in_input_order():
    # Awkward decimals so exact Shapley shares need not land on 6 dp; whatever
    # the rounding, shares must sum exactly to the merged bill and the last
    # member's share is defined as total minus the others.
    members = [
        _member("a", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T12:00:00Z", "33.333333")),
        _member("b", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T11:30:00Z", "41.111111")),
        _member("c", _iv("2024-07-06T10:30:00Z",
                         "2024-07-06T12:00:00Z", "27.777777")),
        _member("d", _iv("2024-07-06T11:00:00Z",
                         "2024-07-06T12:00:00Z", "19.999999")),
    ]
    r = _allocate(members)
    assert r.status_code == 200, r.text
    body = r.json()
    shares = [Decimal(m["allocated_amount"]) for m in body["members"]]
    total = Decimal(body["total_amount"])
    assert sum(shares, Decimal(0)) == total
    assert shares[-1] == total - sum(shares[:-1], Decimal(0))
    assert body["coalitions_evaluated"] == 16

    # Energy conservation holds on the awkward prorations as well.
    assert sum((Decimal(m["energy"]) for m in body["members"]), Decimal(0)) == (
        Decimal(body["total_energy"]))


def test_six_members_accepted_and_seven_rejected():
    members = [
        _member(f"m{i}", _iv("2024-07-06T10:00:00Z",
                             "2024-07-06T11:00:00Z", 1))
        for i in range(6)
    ]
    r = _allocate(members)
    assert r.status_code == 200, r.text
    assert r.json()["coalitions_evaluated"] == 64

    members.append(_member("m6", _iv("2024-07-06T10:00:00Z",
                                     "2024-07-06T11:00:00Z", 1)))
    r = _allocate(members)
    assert r.status_code == 422


# ------------------------------------------------------------------ errors


def test_duplicate_member_id_rejected():
    r = _allocate([
        _member("dup", _iv("2024-07-06T10:00:00Z",
                           "2024-07-06T11:00:00Z", 10)),
        _member("dup", _iv("2024-07-06T11:00:00Z",
                           "2024-07-06T12:00:00Z", 10)),
    ])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "duplicate_member"
    assert err["details"]["member_id"] == "dup"


def test_within_member_overlap_rejected_but_cross_member_allowed():
    r = _allocate([
        _member("A",
                _iv("2024-07-06T10:00:00Z", "2024-07-06T12:00:00Z", 10),
                _iv("2024-07-06T11:00:00Z", "2024-07-06T13:00:00Z", 10)),
        _member("B", _iv("2024-07-06T10:30:00Z",
                         "2024-07-06T11:30:00Z", 5)),
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "interval_overlap"


def test_single_member_rejected():
    r = _allocate([
        _member("alone", _iv("2024-07-06T10:00:00Z",
                             "2024-07-06T11:00:00Z", 10)),
    ])
    assert r.status_code == 422


def test_member_requires_at_least_one_interval():
    r = _allocate([
        {"member_id": "A", "intervals": []},
        {"member_id": "B", "intervals": [
            _iv("2024-07-06T10:00:00Z", "2024-07-06T11:00:00Z", 10)]},
    ])
    assert r.status_code == 422


def test_blank_member_id_rejected():
    r = _allocate([
        _member("", _iv("2024-07-06T10:00:00Z",
                        "2024-07-06T11:00:00Z", 10)),
        _member("B", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T11:00:00Z", 10)),
    ])
    assert r.status_code == 422


def test_unknown_version_is_404():
    r = _allocate([
        _member("A", _iv("2024-07-06T10:00:00Z",
                         "2024-07-06T11:00:00Z", 10)),
        _member("B", _iv("2024-07-06T11:00:00Z",
                         "2024-07-06T12:00:00Z", 10)),
    ], version="nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"


def test_rule_gap_propagates_as_409():
    # v2025's earliest rule starts 2024-07-01; January 2024 is uncovered.
    r = _allocate([
        _member("A", _iv("2024-01-15T10:00:00Z",
                         "2024-01-15T11:00:00Z", 10)),
        _member("B", _iv("2024-01-15T10:00:00Z",
                         "2024-01-15T11:00:00Z", 10)),
    ], version="us-ny-demo-v2025")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "rule_gap"


def test_extra_fields_rejected():
    r = client.post("/cost-allocation", json={
        "version": V,
        "members": [
            {"member_id": "A", "note": "x",
             "intervals": [_iv("2024-07-06T10:00:00Z",
                               "2024-07-06T11:00:00Z", 10)]},
            {"member_id": "B",
             "intervals": [_iv("2024-07-06T11:00:00Z",
                               "2024-07-06T12:00:00Z", 10)]},
        ],
    })
    assert r.status_code == 422
