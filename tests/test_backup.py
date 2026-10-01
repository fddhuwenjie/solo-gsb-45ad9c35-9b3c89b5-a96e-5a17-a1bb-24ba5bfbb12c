"""Tests for online SQLite snapshots (Online Backup API).

Like the settlement tests, these run against a temporary **file-backed**
SQLite database (WAL + real write locks): the Online Backup API opens an
independent connection, and we need real concurrent writers to prove that
``/quote``-style reads and settlement-style commits keep working while a
backup is being streamed.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import backup, settlement
from app.database import (
    Base,
    BillingCycle,
    DatabaseBackup,
    configure_sqlite_engine,
    seed,
)
from app.pricing import PricingError
from app.schemas import QuoteIntervalIn

V = "us-ny-demo-v2024"


def _interval_at(hour, energy="60"):
    """Back-to-back hourly intervals starting 2024-07-06 00:00 NY (a Saturday),
    so every batch hits the single ``leisure`` rule at 9.00 per 60 kWh."""
    base = datetime(2024, 7, 6, tzinfo=timezone(timedelta(hours=-4)))
    start = base + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    fmt = "%Y-%m-%dT%H:%M:%S-04:00"
    return {
        "start": start.strftime(fmt),
        "end": end.strftime(fmt),
        "timezone": "America/New_York",
        "energy": energy,
    }


@pytest.fixture()
def live_db(tmp_path):
    db_path = tmp_path / "live.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(eng)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        seed(s)
        s.commit()
    directory = str(tmp_path / "backups")
    yield eng, db_path, directory
    eng.dispose()


def _submit(eng, cycle, key, rev, hour):
    class _Req:
        def __init__(self):
            self.version = V
            self.idempotency_key = key
            self.expected_revision = rev
            self.intervals = [QuoteIntervalIn(**_interval_at(hour))]

    return settlement.submit_batch(cycle, _Req(), engine=eng)


def _file_path(directory, backup_id):
    return os.path.join(directory, f"{backup_id}.db")


# ------------------------------------------------------------- happy path


def test_backup_published_verified_and_self_contained(live_db):
    eng, _, directory = live_db
    result = backup.create_backup("snap-1", engine=eng, directory=directory)
    assert result.created is True
    meta = result.metadata

    path = _file_path(directory, "snap-1")
    assert os.path.isfile(path)
    # no temp leftovers, no WAL/SHM sidecars for the published snapshot
    leftovers = [n for n in os.listdir(directory) if n.startswith(".tmp-")]
    assert leftovers == []
    assert not os.path.exists(path + "-wal")
    assert not os.path.exists(path + "-shm")

    # digest and byte count match the actual file
    raw = open(path, "rb").read()
    assert len(raw) == meta["size_bytes"]
    assert hashlib.sha256(raw).hexdigest() == meta["sha256"]
    assert meta == {
        "backup_id": "snap-1",
        "filename": "snap-1.db",
        "download_url": "/backups/snap-1/download",
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "created_at": meta["created_at"],
    }

    # the copy opens on its own and passes integrity_check with seed data
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute(
            "select count(*) from tariff_versions").fetchone()[0] == 3
        assert conn.execute(
            "select count(*) from rate_rules").fetchone()[0] > 0
        # the snapshot predates its own registry row (copy happens before the
        # metadata insert); it only needs the application data, which is seeded.
        assert conn.execute(
            "select count(*) from database_backups").fetchone()[0] == 0
    finally:
        conn.close()


def test_replay_returns_first_metadata_and_no_second_file(live_db):
    eng, _, directory = live_db
    first = backup.create_backup("snap-1", engine=eng, directory=directory)
    replay = backup.create_backup("snap-1", engine=eng, directory=directory)
    assert replay.created is False
    assert replay.metadata == first.metadata
    with Session(eng) as s:
        rows = s.scalars(select(DatabaseBackup)).all()
        assert len(rows) == 1
    assert os.listdir(directory) == ["snap-1.db"]


def test_snapshot_reflects_committed_state_and_is_immutable(live_db):
    eng, _, directory = live_db
    _submit(eng, "c1", "k1", 0, 0)
    result = backup.create_backup("snap-1", engine=eng, directory=directory)

    conn = sqlite3.connect(_file_path(directory, "snap-1"))
    try:
        assert conn.execute(
            "select revision, total_amount from billing_cycles"
        ).fetchone() == (1, "9.000000")
        assert conn.execute(
            "select count(*) from cycle_intervals").fetchone()[0] == 1
    finally:
        conn.close()

    # keep mutating the live DB; the published snapshot is a fixed copy
    _submit(eng, "c1", "k2", 1, 1)
    conn = sqlite3.connect(_file_path(directory, "snap-1"))
    try:
        assert conn.execute(
            "select revision from billing_cycles").fetchone()[0] == 1
    finally:
        conn.close()
    # replaying the same id still reports the original digest/size
    replay = backup.create_backup("snap-1", engine=eng, directory=directory)
    assert replay.metadata == result.metadata


# ------------------------------------------------------------- concurrency


def test_concurrent_same_id_publishes_exactly_one_file(live_db):
    eng, _, directory = live_db
    outcomes: list[backup.BackupResult] = []

    def worker():
        outcomes.append(backup.create_backup(
            "race", engine=eng, directory=directory,
            pages_per_step=1, step_sleep=0.01))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 8
    created = [o for o in outcomes if o.created]
    replays = [o for o in outcomes if not o.created]
    assert len(created) == 1
    assert len(replays) == 7
    # every request returns the *first* metadata
    assert {o.metadata["sha256"] for o in outcomes} == {
        created[0].metadata["sha256"]}
    assert os.listdir(directory) == ["race.db"]
    assert [n for n in os.listdir(directory) if n.startswith(".tmp-")] == []
    with Session(eng) as s:
        assert s.scalars(select(DatabaseBackup)).all() and len(
            s.scalars(select(DatabaseBackup)).all()) == 1


def test_service_keeps_working_during_backup(live_db):
    """备份进行中，报价读与结算写都必须持续成功。"""
    eng, db_path, directory = live_db
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def do_backup():
        try:
            barrier.wait()
            # one page at a time with sleeps keeps the backup window wide
            r = backup.create_backup(
                "live", engine=eng, directory=directory,
                pages_per_step=1, step_sleep=0.005)
            assert r.created
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    # writers commit settlement batches (non-overlapping hours, unique keys);
    # revision races are serialized by the settlement layer itself.
    def writer_revision():
        try:
            barrier.wait()
            rev = 0
            for hour in range(0, 12):
                res = _submit(
                    eng, "busy", f"k-{hour}", rev, hour)
                if not res.replayed:
                    rev = res.body["revision"]
        except Exception as e:  # pragma: no cover
            errors.append(e)

    # readers hit the pricing path on independent sessions like POST /quote
    def reader_quotes():
        try:
            barrier.wait()
            from app.pricing import load_version, quote
            payload = [QuoteIntervalIn(**_interval_at(13))]
            for _ in range(30):
                with Session(eng) as s:
                    version = load_version(s, V)
                    total = quote(version, payload).total_amount
                    assert total.quantize(Decimal("0.01")) == Decimal("9.00")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [
        threading.Thread(target=do_backup),
        threading.Thread(target=writer_revision),
        threading.Thread(target=reader_quotes),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    # all 12 batches survived and the backup file is valid
    with Session(eng) as s:
        assert s.get(BillingCycle, "busy").revision == 12
    conn = sqlite3.connect(_file_path(directory, "live"))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        n_batches = conn.execute(
            "select count(*) from usage_batches").fetchone()[0]
        # backup may have landed at any point; it is a consistent prefix
        assert 0 <= n_batches <= 12
    finally:
        conn.close()


# ------------------------------------------------------------- failure paths


def test_integrity_failure_deletes_temp_and_publishes_nothing(
        live_db, monkeypatch):
    eng, _, directory = live_db
    real_verify = backup.verify_copy

    def corrupt_then_verify(path):
        # Corrupt bytes inside a b-tree page (past the 100-byte db header):
        # SQLite then reports "database disk image is malformed" while running
        # integrity_check, which must surface as backup_integrity_failed.
        with open(path, "r+b") as f:
            f.seek(4096 + 8)
            f.write(b"\x00" * 40)
        real_verify(path)

    monkeypatch.setattr(backup, "verify_copy", corrupt_then_verify)
    with pytest.raises(PricingError) as ei:
        backup.create_backup("bad", engine=eng, directory=directory)
    assert ei.value.code == "backup_integrity_failed"

    # no final file, no metadata row, no temp leftovers
    assert not os.path.exists(_file_path(directory, "bad"))
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "bad") is None
    assert [n for n in os.listdir(directory) if n.startswith(".tmp-")] == []


def test_copy_error_leaves_no_artifact(live_db, monkeypatch):
    eng, _, directory = live_db

    def boom(src, dst_path, **kwargs):
        raise sqlite3.OperationalError("simulated backup failure")

    monkeypatch.setattr(backup, "online_copy", boom)
    with pytest.raises(PricingError):
        backup.create_backup("boom", engine=eng, directory=directory)
    assert os.listdir(directory) == []
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "boom") is None


def test_existing_file_is_never_overwritten(live_db):
    eng, _, directory = live_db
    target = _file_path(directory, "taken")
    os.makedirs(directory, exist_ok=True)
    with open(target, "wb") as f:
        f.write(b"not-a-backup")
    with pytest.raises(PricingError) as ei:
        backup.create_backup("taken", engine=eng, directory=directory)
    assert ei.value.code == "backup_exists"
    assert open(target, "rb").read() == b"not-a-backup"
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "taken") is None
    assert [n for n in os.listdir(directory) if n.startswith(".tmp-")] == []


@pytest.mark.parametrize("bad", [
    "", "a" * 129, "../x", "a/b", "x y", "abc.def", "-abc", "_abc", "中文",
])
def test_invalid_backup_id(live_db, bad):
    eng, _, directory = live_db
    with pytest.raises(PricingError) as ei:
        backup.create_backup(bad, engine=eng, directory=directory)
    assert ei.value.code == "invalid_backup_id"
    assert ei.value.status_code == 400
    with pytest.raises(PricingError) as ei:
        backup.get_backup(bad, engine=eng)
    assert ei.value.code == "invalid_backup_id"


# ------------------------------------------------------------- restart


def test_metadata_and_file_survive_restart(live_db):
    eng, db_path, directory = live_db
    _submit(eng, "c1", "k1", 0, 0)
    first = backup.create_backup(
        "persist", engine=eng, directory=directory).metadata
    eng.dispose()

    # reopen the same files with brand-new engines (simulated restart)
    eng2 = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30})
    configure_sqlite_engine(eng2)
    try:
        # startup sweep must not remove a complete, registered backup
        backup.reconcile_backups(engine=eng2, directory=directory)
        meta = backup.get_backup("persist", engine=eng2)
        assert meta == first

        # replay after restart returns first metadata, writes nothing new
        replay = backup.create_backup(
            "persist", engine=eng2, directory=directory)
        assert replay.created is False
        assert replay.metadata == first

        # the file itself is still downloadable/verifiable on its own
        conn = sqlite3.connect(_file_path(directory, "persist"))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert conn.execute(
                "select total_amount from billing_cycles"
            ).fetchone()[0] == "9.000000"
        finally:
            conn.close()
    finally:
        eng2.dispose()


def test_reconcile_clears_temp_and_orphan_row(live_db):
    eng, db_path, directory = live_db
    os.makedirs(directory, exist_ok=True)
    # crash residue 1: stray temp copy
    stray = os.path.join(directory, ".tmp-stray-1.db")
    open(stray, "wb").write(b"x")
    # crash residue 2: committed metadata row whose file never got published
    with Session(eng) as s:
        with s.begin():
            s.add(DatabaseBackup(
                backup_id="orphan", filename="orphan.db", size_bytes=1,
                sha256="0" * 64, created_at="2026-01-01T00:00:00+00:00"))

    backup.reconcile_backups(engine=eng, directory=directory)
    assert not os.path.exists(stray)
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "orphan") is None
    # a healthy backup survives subsequent sweeps
    backup.create_backup("ok", engine=eng, directory=directory)
    backup.reconcile_backups(engine=eng, directory=directory)
    assert os.path.isfile(_file_path(directory, "ok"))
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "ok") is not None
