"""Tests for persistent incremental billing-cycle settlement.

The settlement endpoint must survive restarts and serialize concurrent
same-revision writers, so tests run against a temporary *file-backed* SQLite
database (WAL, real write locks) rather than the shared in-memory engine used
by the rest of the API tests.
"""
from __future__ import annotations

import os
import threading
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from app import settlement  # noqa: E402
from app.database import (  # noqa: E402
    Base,
    BillingCycle,
    CycleInterval,
    UsageBatch,
    configure_sqlite_engine,
    seed,
)
from app.main import app  # noqa: E402
from app.pricing import PricingError  # noqa: E402
from app.schemas import QuoteIntervalIn  # noqa: E402

V = "us-ny-demo-v2024"
# 2024-07-06 is a Saturday in NY -> only the `leisure` rule
# (tiers: 100 @0.15, 300 @0.18, open @0.22).
DAY1 = {
    "start": "2024-07-06T00:00:00-04:00",
    "end": "2024-07-06T01:00:00-04:00",
    "timezone": "America/New_York",
    "energy": "60",
}
DAY2 = {
    "start": "2024-07-06T01:00:00-04:00",
    "end": "2024-07-06T02:00:00-04:00",
    "timezone": "America/New_York",
    "energy": "60",
}
DAY3_LATER = {
    "start": "2024-07-06T02:00:00-04:00",
    "end": "2024-07-06T03:00:00-04:00",
    "timezone": "America/New_York",
    "energy": "60",
}


@pytest.fixture()
def tmp_engine(tmp_path):
    path = tmp_path / "settle.db"
    eng = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(eng)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        seed(s)
        s.commit()
    yield eng, path
    eng.dispose()


def _submit(eng, cycle, key, rev, intervals, version=V):
    class _Req:  # lightweight stand-in for the pydantic request model
        def __init__(self):
            self.version = version
            self.idempotency_key = key
            self.expected_revision = rev
            self.intervals = [QuoteIntervalIn(**iv) for iv in intervals]

    return settlement.submit_batch(cycle, _Req(), engine=eng)


def _interval(start, end, energy, tz="America/New_York"):
    return {"start": start, "end": end, "timezone": tz, "energy": energy}


# ------------------------------------------------------------- happy paths


def test_first_batch_must_have_revision_zero(tmp_engine):
    eng, _ = tmp_engine
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k1", 1, [DAY1])
    assert ei.value.code == "revision_conflict"
    assert ei.value.details == {
        "cycle_id": "c1", "expected_revision": 1, "current_revision": 0}
    # nothing was written
    with Session(eng) as s:
        assert s.scalar(select(BillingCycle)) is None
        assert s.scalar(select(UsageBatch)) is None


def test_first_batch_locks_version_and_sets_revision_one(tmp_engine):
    eng, _ = tmp_engine
    res = _submit(eng, "c1", "k1", 0, [DAY1])
    assert res.replayed is False
    body = res.body
    assert body["revision"] == 1
    assert body["version"] == V
    assert Decimal(body["total_energy"]) == Decimal("60")
    assert Decimal(body["total_amount"]) == Decimal("9.00")
    assert Decimal(body["batch_amount"]) == Decimal("9.00")
    assert body["quote"]["matched_rules"] == ["leisure"]
    assert Decimal(body["quote"]["total_amount"]) == Decimal("9.00")


def test_cumulative_tier_spans_batches(tmp_engine):
    eng, _ = tmp_engine
    b1 = _submit(eng, "c1", "k1", 0, [DAY1]).body
    assert b1["revision"] == 1
    assert Decimal(b1["batch_amount"]) == Decimal("9.00")  # 60 @0.15

    # Second batch is back-to-back (allowed). Cumulative tier now spans
    # batches: 40 left in tier 1 @0.15 + 20 in tier 2 @0.18.
    b2 = _submit(eng, "c1", "k2", 1, [DAY2]).body
    assert b2["revision"] == 2
    assert Decimal(b2["total_energy"]) == Decimal("120")
    # merged whole: 100*0.15 + 20*0.18 = 18.60
    assert Decimal(b2["total_amount"]) == Decimal("18.60")
    assert Decimal(b2["batch_amount"]) == Decimal("18.60") - Decimal("9.00")
    # Quote covers the whole cycle across both batches.
    seg_energy = sum(Decimal(s["energy"]) for s in b2["quote"]["segments"])
    assert seg_energy == Decimal("120.000000")
    ordinals = {s["tier_ordinal"] for s in b2["quote"]["segments"]}
    assert ordinals == {1, 2}

    # Third batch pushes into the open tier.
    b3 = _submit(eng, "c1", "k3", 2, [DAY3_LATER]).body
    assert Decimal(b3["total_energy"]) == Decimal("180")
    # 100*0.15 + 80*0.18 = 29.40
    assert Decimal(b3["total_amount"]) == Decimal("29.40")
    assert Decimal(b3["batch_amount"]) == Decimal("29.40") - Decimal("18.60")


# ------------------------------------------------------------- idempotency


def test_same_key_same_content_replays_first_response(tmp_engine):
    eng, _ = tmp_engine
    first = _submit(eng, "c1", "k1", 0, [DAY1]).body
    # replay with a stale expected_revision is still a replay (idempotency
    # is checked before expected_revision)
    replay = _submit(eng, "c1", "k1", 5, [DAY1])
    assert replay.replayed is True
    assert replay.body == first
    with Session(eng) as s:
        assert len(s.scalars(select(UsageBatch)).all()) == 1
        assert s.get(BillingCycle, "c1").revision == 1


def test_same_key_different_content_conflicts(tmp_engine):
    eng, _ = tmp_engine
    _submit(eng, "c1", "k1", 0, [DAY1])
    changed = dict(DAY1, energy="61")
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k1", 1, [changed])
    assert ei.value.code == "idempotency_conflict"
    assert ei.value.details["idempotency_key"] == "k1"
    with Session(eng) as s:
        # no extra batch, revision untouched
        assert len(s.scalars(select(UsageBatch)).all()) == 1
        assert s.get(BillingCycle, "c1").revision == 1


def test_replay_order_insensitive(tmp_engine):
    """区间在批内重排不改变幂等摘要（摘要按 UTC 排序后计算）。"""
    eng, _ = tmp_engine
    iv_other = _interval("2024-07-07T00:00:00-04:00",
                         "2024-07-07T01:00:00-04:00", "10")
    first = _submit(eng, "c1", "k1", 0, [DAY1, iv_other]).body
    replay = _submit(eng, "c1", "k1", 0, [iv_other, DAY1])
    assert replay.replayed is True
    assert replay.body == first


# ------------------------------------------------------------- conflicts


def test_stale_revision_conflict(tmp_engine):
    eng, _ = tmp_engine
    _submit(eng, "c1", "k1", 0, [DAY1])
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k2", 0, [DAY2])
    assert ei.value.code == "revision_conflict"
    assert ei.value.details["current_revision"] == 1
    with Session(eng) as s:
        assert len(s.scalars(select(UsageBatch)).all()) == 1


def test_cross_batch_overlap_rejected_and_not_persisted(tmp_engine):
    eng, _ = tmp_engine
    _submit(eng, "c1", "k1", 0, [DAY1])
    overlap = _interval("2024-07-06T00:30:00-04:00",
                        "2024-07-06T01:30:00-04:00", "5")
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k2", 1, [overlap])
    assert ei.value.code == "interval_overlap"
    det = ei.value.details
    assert det["interval_a"]["start"].endswith("Z")
    assert det["interval_b"]["start"].endswith("Z")
    with Session(eng) as s:
        assert len(s.scalars(select(UsageBatch)).all()) == 1
        assert len(s.scalars(select(CycleInterval)).all()) == 1
        assert s.get(BillingCycle, "c1").revision == 1


def test_version_lock_after_first_batch(tmp_engine):
    eng, _ = tmp_engine
    _submit(eng, "c1", "k1", 0, [DAY1], version=V)
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k2", 1, [DAY2],
                version="cn-shanghai-demo-v2024")
    assert ei.value.code == "version_locked"
    assert ei.value.details["locked_version"] == V
    with Session(eng) as s:
        assert s.get(BillingCycle, "c1").revision == 1


def test_unknown_version_404(tmp_engine):
    eng, _ = tmp_engine
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k1", 0, [DAY1], version="nope")
    assert ei.value.status_code == 404
    assert ei.value.code == "version_not_found"
    with Session(eng) as s:
        assert s.scalar(select(BillingCycle)) is None


def test_rule_gap_writes_nothing(tmp_engine):
    eng, _ = tmp_engine
    # 2026 is outside every seeded rule's effective window -> rule_gap.
    gap = _interval("2026-01-03T00:00:00-05:00",
                    "2026-01-03T01:00:00-05:00", "10")
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k1", 0, [gap])
    assert ei.value.code == "rule_gap"
    with Session(eng) as s:
        assert s.scalar(select(BillingCycle)) is None
        assert s.scalar(select(UsageBatch)) is None
        assert s.scalar(select(CycleInterval)) is None


def test_invalid_interval_within_batch_writes_nothing(tmp_engine):
    eng, _ = tmp_engine
    bad = _interval("2024-07-06T02:00:00-04:00",
                    "2024-07-06T01:00:00-04:00", "10")
    with pytest.raises(PricingError) as ei:
        _submit(eng, "c1", "k1", 0, [bad])
    assert ei.value.code == "invalid_interval"
    with Session(eng) as s:
        assert s.scalar(select(BillingCycle)) is None


# ------------------------------------------------------------- concurrency


def test_concurrent_same_revision_only_one_wins(tmp_engine):
    eng, path = tmp_engine
    results: dict[str, object] = {}

    def worker(name, key, interval):
        try:
            results[name] = _submit(eng, "race", key, 1, [interval]).body
        except PricingError as e:
            results[name] = e

    # revision 1 must exist first
    _submit(eng, "race", "base", 0, [DAY1])
    t1 = threading.Thread(
        target=worker, args=("A", "ka", DAY2))
    t2 = threading.Thread(
        target=worker, args=("B", "kb", DAY3_LATER))
    t1.start(); t2.start(); t1.join(); t2.join()

    ok = [n for n, r in results.items() if not isinstance(r, PricingError)]
    err = [r for r in results.values() if isinstance(r, PricingError)]
    assert len(ok) == 1, results
    assert len(err) == 1
    assert err[0].code == "revision_conflict"
    winner = results[ok[0]]
    assert winner["revision"] == 2
    with Session(eng) as s:
        assert s.get(BillingCycle, "race").revision == 2
        # only the winner's intervals were added (base + winner = 1 batch of
        # intervals each; the loser wrote nothing)
        assert len(s.scalars(select(CycleInterval)).all()) == 2
        assert len(s.scalars(select(UsageBatch)).all()) == 2


def test_concurrent_first_submit_only_one_wins(tmp_engine):
    eng, _ = tmp_engine
    results: dict[str, object] = {}

    def worker(name, key):
        try:
            results[name] = _submit(eng, "fresh", key, 0, [DAY1]).body
        except PricingError as e:
            results[name] = e

    t1 = threading.Thread(target=worker, args=("A", "ka"))
    t2 = threading.Thread(target=worker, args=("B", "kb"))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok = [r for r in results.values() if not isinstance(r, PricingError)]
    err = [r for r in results.values() if isinstance(r, PricingError)]
    assert len(ok) == 1
    assert len(err) == 1
    assert err[0].code == "revision_conflict"
    assert ok[0]["revision"] == 1


def test_concurrent_same_key_same_content_both_replay(tmp_engine):
    eng, _ = tmp_engine

    def worker(out):
        try:
            out.append(_submit(eng, "idem", "k1", 0, [DAY1]))
        except PricingError as e:
            out.append(e)

    out: list = []
    threads = [threading.Thread(target=worker, args=(out,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # At least one must be the successful create; both responses must agree.
    assert all(not isinstance(r, PricingError) for r in out), out
    assert {r.body["revision"] for r in out} == {1}
    assert out[0].body == out[1].body
    with Session(eng) as s:
        assert len(s.scalars(select(UsageBatch)).all()) == 1


# ------------------------------------------------------------- persistence


def test_state_survives_engine_restart(tmp_engine):
    eng, path = tmp_engine
    _submit(eng, "c1", "k1", 0, [DAY1])
    _submit(eng, "c1", "k2", 1, [DAY2])
    eng.dispose()

    # Reopen the same file with a brand-new engine (simulated restart).
    eng2 = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(eng2)
    try:
        state = settlement.get_cycle("c1", engine=eng2)
        assert state["revision"] == 2
        assert Decimal(state["total_energy"]) == Decimal("120")
        assert Decimal(state["total_amount"]) == Decimal("18.60")
        assert [b["idempotency_key"] for b in state["batches"]] == ["k1", "k2"]
        assert len(state["intervals"]) == 2

        # Idempotent replay still returns the original body after restart.
        replay = _submit(eng2, "c1", "k1", 9, [DAY1])
        assert replay.replayed is True
        assert replay.body["revision"] == 1
        assert Decimal(replay.body["batch_amount"]) == Decimal("9.00")

        # Continue with the next batch on top of the recovered state.
        b3 = _submit(eng2, "c1", "k3", 2, [DAY3_LATER]).body
        assert b3["revision"] == 3
        assert Decimal(b3["total_amount"]) == Decimal("29.40")
    finally:
        eng2.dispose()


def test_get_cycle_404(tmp_engine):
    eng, _ = tmp_engine
    with pytest.raises(PricingError) as ei:
        settlement.get_cycle("missing", engine=eng)
    assert ei.value.status_code == 404
    assert ei.value.code == "cycle_not_found"


# ------------------------------------------------------------- HTTP surface


def test_http_endpoint_first_and_replay():
    client = TestClient(app)
    with client:
        cid = "http-demo-cycle"
        r = client.post(f"/cycles/{cid}/batches", json={
            "version": V,
            "idempotency_key": "http-k1",
            "expected_revision": 0,
            "intervals": [{
                "start": "2024-07-06T00:00:00-04:00",
                "end": "2024-07-06T01:00:00-04:00",
                "timezone": "America/New_York",
                "energy": "60",
            }],
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["revision"] == 1
        assert body["batch_amount"] == "9.00"
        assert body["quote"]["version"] == V

        # identical replay -> 200 with the same body
        r2 = client.post(f"/cycles/{cid}/batches", json={
            "version": V,
            "idempotency_key": "http-k1",
            "expected_revision": 0,
            "intervals": [{
                "start": "2024-07-06T00:00:00-04:00",
                "end": "2024-07-06T01:00:00-04:00",
                "timezone": "America/New_York",
                "energy": "60",
            }],
        })
        assert r2.status_code == 200, r2.text
        assert r2.json() == body

        # GET state
        rg = client.get(f"/cycles/{cid}")
        assert rg.status_code == 200
        assert rg.json()["revision"] == 1

        # stale revision over HTTP -> 409 envelope
        r3 = client.post(f"/cycles/{cid}/batches", json={
            "version": V,
            "idempotency_key": "http-k2",
            "expected_revision": 0,
            "intervals": [{
                "start": "2024-07-06T01:00:00-04:00",
                "end": "2024-07-06T02:00:00-04:00",
                "timezone": "America/New_York",
                "energy": "60",
            }],
        })
        assert r3.status_code == 409
        assert r3.json()["error"]["code"] == "revision_conflict"


def test_http_unknown_cycle_404():
    client = TestClient(app)
    with client:
        r = client.get("/cycles/does-not-exist")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "cycle_not_found"
