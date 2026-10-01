"""Tests for online SQLite snapshot backups.

The interesting guarantees — backup while the service keeps serving
reads/writes, one final file under concurrent duplicate submissions,
no half-finished artifacts on failure, and metadata surviving restart —
require a real *file-backed* SQLite database (WAL, real locks), like the
settlement tests. The HTTP surface is exercised against the app's normal
in-memory engine.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from app import backup, settlement  # noqa: E402
from app.database import (  # noqa: E402
    Base,
    DatabaseBackup,
    configure_sqlite_engine,
    seed,
)
from app.main import app  # noqa: E402
from app.pricing import PricingError, load_version  # noqa: E402
from app.schemas import QuoteIntervalIn  # noqa: E402

V = "us-ny-demo-v2024"
DAY1 = {
    "start": "2024-07-06T00:00:00-04:00",
    "end": "2024-07-06T01:00:00-04:00",
    "timezone": "America/New_York",
    "energy": "60",
}


@pytest.fixture()
def tmp_backup(tmp_path, monkeypatch):
    db_path = tmp_path / "svc.db"
    backup_dir = tmp_path / "backups"
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(eng)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        seed(s)
        s.commit()
    yield eng, db_path, backup_dir
    eng.dispose()


def _pad_database(eng, rows: int = 2000, blob: int = 1024):
    """Inflate the file so the online copy spans multiple backup steps."""
    with Session(eng) as s:
        s.execute(text("CREATE TABLE IF NOT EXISTS pad (k INTEGER PRIMARY KEY, b BLOB)"))
        s.commit()
        chunk = b"x" * blob
        for k in range(rows):
            s.execute(text("INSERT INTO pad(k, b) VALUES (:k, :b)"),
                      {"k": k, "b": chunk})
            if k % 200 == 199:
                s.commit()
        s.commit()


def _only_files(backup_dir: Path):
    return sorted(p.name for p in backup_dir.iterdir())


def _is_sqlite_file(path: Path) -> bool:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        versions = conn.execute(
            "SELECT count(*) FROM tariff_versions").fetchone()[0]
    finally:
        conn.close()
    return ok and versions == 3


# ---------------------------------------------------------------- happy path


def test_backup_publishes_verified_snapshot(tmp_backup):
    eng, _, backup_dir = tmp_backup
    body, created = backup.create_backup("snap-1", engine=eng,
                                         backup_dir=backup_dir)
    assert created is True
    assert body == {
        "backup_id": "snap-1",
        "filename": "snap-1.db",
        "download_url": "/backups/snap-1/download",
        "sha256": body["sha256"],
        "size_bytes": body["size_bytes"],
        "created_at": body["created_at"],
    }
    final = backup_dir / "snap-1.db"
    assert final.is_file()
    assert _is_sqlite_file(final)
    # Published file is a self-contained snapshot: no -wal/-shm/.tmp artifacts.
    assert _only_files(backup_dir) == ["snap-1.db"]
    data = final.read_bytes()
    assert hashlib.sha256(data).hexdigest() == body["sha256"]
    assert len(data) == body["size_bytes"]

    # GET metadata matches.
    meta = backup.get_backup("snap-1", engine=eng)
    assert meta == body
    assert backup.backup_file_path("snap-1", engine=eng,
                                   backup_dir=backup_dir) == final


def test_replay_returns_first_metadata(tmp_backup):
    eng, _, backup_dir = tmp_backup
    first, created1 = backup.create_backup("dup", engine=eng,
                                           backup_dir=backup_dir)
    assert created1 is True
    second, created2 = backup.create_backup("dup", engine=eng,
                                            backup_dir=backup_dir)
    assert created2 is False
    assert second == first
    # Replay must not republish or touch the file.
    assert _only_files(backup_dir) == ["dup.db"]
    assert (backup_dir / "dup.db").stat().st_size == first["size_bytes"]


# --------------------------------------------------------- online concurrency


def test_service_keeps_working_during_online_backup(tmp_backup, monkeypatch):
    eng, _, backup_dir = tmp_backup
    _pad_database(eng)

    # Force many small copy steps so writer traffic provably overlaps the
    # backup (the production default is tuned for large production DBs).
    monkeypatch.setattr(backup, "_BACKUP_PAGES_PER_STEP", 8)

    errors: list[Exception] = []
    quote_hits = 0
    batches: list[int] = []

    def traffic():
        nonlocal quote_hits
        idx = 0
        while backup_thread.is_alive():
            # Read-only pricing request against a fresh session (like /quote).
            try:
                with Session(eng) as s:
                    version = load_version(s, V)
                    req_intervals = [QuoteIntervalIn(**{
                        "start": f"2024-07-06T{idx % 20:02d}:00:00-04:00",
                        "end": f"2024-07-06T{idx % 20:02d}:30:00-04:00",
                        "timezone": "America/New_York",
                        "energy": "0.01",
                    })]
                    from app import pricing
                    pricing.quote(version, req_intervals)
                quote_hits += 1
            except Exception as exc:  # pragma: no cover - must not happen
                errors.append(exc)

            # Persistent writer exactly like POST /cycles/{id}/batches:
            # distinct cycles so each submission is its own revision-0 create.
            try:
                class _Req:
                    version = V
                    idempotency_key = f"k-{idx}"
                    expected_revision = 0
                    intervals = [QuoteIntervalIn(**DAY1)]

                result = settlement.submit_batch(
                    f"live-cycle-{idx}", _Req(), engine=eng)
                batches.append(result.body["revision"])
            except Exception as exc:  # pragma: no cover - must not happen
                errors.append(exc)
            idx += 1

    backup_thread = threading.Thread(
        target=lambda: backup.create_backup(
            "live", engine=eng, backup_dir=backup_dir))
    backup_thread.start()
    traffic()
    backup_thread.join()

    assert not errors, errors
    assert quote_hits > 0, "reader traffic did not overlap the backup"
    assert batches, "writer traffic did not overlap the backup"
    assert all(r == 1 for r in batches)

    body, created = backup.create_backup("live", engine=eng,
                                         backup_dir=backup_dir)
    # The thread already published it; this call must be a pure replay.
    assert created is False
    final = backup_dir / body["filename"]
    assert _only_files(backup_dir) == ["live.db"]
    assert _is_sqlite_file(final)
    # The snapshot captured a consistent mid-flight database. Metadata is
    # recorded only after the snapshot is published, so the copy contains
    # the table but not its own row.
    conn = sqlite3.connect(f"file:{final}?mode=ro", uri=True)
    try:
        n_cycles = conn.execute(
            "SELECT count(*) FROM billing_cycles").fetchone()[0]
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert n_cycles >= 1
    assert "database_backups" in tables
    # The live DB carries the record published by the backup thread.
    with Session(eng) as s:
        row = s.get(DatabaseBackup, "live")
        assert row is not None and row.filename == "live.db"


def test_concurrent_same_backup_id_publishes_one_file(tmp_backup,
                                                      monkeypatch):
    eng, _, backup_dir = tmp_backup
    _pad_database(eng)
    monkeypatch.setattr(backup, "_BACKUP_PAGES_PER_STEP", 8)

    outcomes: list[tuple[dict, bool] | Exception] = []

    def worker():
        try:
            outcomes.append(backup.create_backup(
                "once", engine=eng, backup_dir=backup_dir))
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(not isinstance(o, Exception) for o in outcomes), outcomes
    created = sorted(created for _, created in outcomes)
    assert created == [False, False, False, False, True]
    bodies = {b["sha256"] for b, _ in outcomes}
    assert bodies == {outcomes[0][0]["sha256"]}
    assert _only_files(backup_dir) == ["once.db"]
    with Session(eng) as s:
        assert s.scalars(select(DatabaseBackup)).all() and len(
            s.scalars(select(DatabaseBackup)).all()) == 1


# ------------------------------------------------------------- failure paths


def test_failed_verification_removes_temp_and_can_retry(tmp_backup,
                                                       monkeypatch):
    eng, _, backup_dir = tmp_backup

    def broken_verify(path):
        raise PricingError("forced integrity failure",
                           code="backup_integrity_failed", status_code=500)

    monkeypatch.setattr(backup, "verify_copy", broken_verify)
    with pytest.raises(PricingError) as ei:
        backup.create_backup("fail", engine=eng, backup_dir=backup_dir)
    assert ei.value.code == "backup_integrity_failed"

    # Nothing left behind: no temp file, no final file, no metadata row.
    assert not backup_dir.exists() or list(backup_dir.iterdir()) == []
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "fail") is None

    # The same backup_id can be retried once the problem is gone.
    monkeypatch.undo()
    body, created = backup.create_backup("fail", engine=eng,
                                         backup_dir=backup_dir)
    assert created is True
    assert _only_files(backup_dir) == ["fail.db"]
    assert _is_sqlite_file(backup_dir / "fail.db")


def test_existing_backup_file_is_never_overwritten(tmp_backup):
    eng, _, backup_dir = tmp_backup
    backup_dir.mkdir(parents=True)

    # A corrupt file already occupies the target name: backup must refuse
    # rather than replace it, and leave no temp files.
    target = backup_dir / "locked-id.db"
    target.write_bytes(b"this is not a sqlite database")
    with pytest.raises(PricingError) as ei:
        backup.create_backup("locked-id", engine=eng, backup_dir=backup_dir)
    assert ei.value.status_code == 409
    assert ei.value.code == "backup_exists"
    assert target.read_bytes() == b"this is not a sqlite database"
    assert _only_files(backup_dir) == ["locked-id.db"]
    with Session(eng) as s:
        assert s.get(DatabaseBackup, "locked-id") is None

    # A pre-existing *valid* snapshot is adopted (content-addressed by its
    # own checksum, still never overwritten).
    valid = backup_dir / "external-id.db"
    src_conn = sqlite3.connect(str(eng.url.database))
    try:
        dst_conn = sqlite3.connect(str(valid))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    expected_sha = hashlib.sha256(valid.read_bytes()).hexdigest()
    body, created = backup.create_backup("external-id", engine=eng,
                                         backup_dir=backup_dir)
    assert created is True
    assert body["sha256"] == expected_sha
    assert hashlib.sha256(valid.read_bytes()).hexdigest() == expected_sha


def test_unknown_and_invalid_ids(tmp_backup):
    eng, _, backup_dir = tmp_backup
    with pytest.raises(PricingError) as ei:
        backup.get_backup("nope", engine=eng)
    assert ei.value.status_code == 404
    assert ei.value.code == "backup_not_found"
    for bad in ("../x", "a/b", "", "x" * 129):
        with pytest.raises(PricingError) as ei:
            backup.create_backup(bad, engine=eng, backup_dir=backup_dir)
        assert ei.value.status_code == 400
        assert ei.value.code == "invalid_backup_id"


# ------------------------------------------------------------------ restart


def test_backup_metadata_survives_restart(tmp_backup):
    eng, db_path, backup_dir = tmp_backup
    first, created = backup.create_backup("restart", engine=eng,
                                          backup_dir=backup_dir)
    assert created is True
    eng.dispose()

    eng2 = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(eng2)
    try:
        # Metadata is persisted: replay after restart returns first result.
        second, created2 = backup.create_backup("restart", engine=eng2,
                                                backup_dir=backup_dir)
        assert created2 is False
        assert second == first
        meta = backup.get_backup("restart", engine=eng2)
        assert meta["sha256"] == first["sha256"]
        path = backup.backup_file_path("restart", engine=eng2,
                                       backup_dir=backup_dir)
        assert _is_sqlite_file(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == first["sha256"]
    finally:
        eng2.dispose()


# --------------------------------------------------------------- HTTP surface


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "http-backups"))
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        yield client


def test_http_backup_create_replay_get_download(http_client, monkeypatch):
    import os as _os
    backup_dir = Path(_os.environ["BACKUP_DIR"])

    r = http_client.post("/backups/http-1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["backup_id"] == "http-1"
    assert body["filename"] == "http-1.db"
    assert body["download_url"] == "/backups/http-1/download"
    assert len(body["sha256"]) == 64
    assert body["size_bytes"] > 0

    # Concurrent duplicate -> 200 with identical first metadata.
    result: dict = {}

    def call():
        result["r"] = http_client.post("/backups/http-1")

    t = threading.Thread(target=call)
    t.start(); t.join()
    assert result["r"].status_code == 200
    assert result["r"].json() == body

    rg = http_client.get("/backups/http-1")
    assert rg.status_code == 200
    assert rg.json() == body

    rd = http_client.get("/backups/http-1/download")
    assert rd.status_code == 200
    assert rd.headers["content-type"] == "application/vnd.sqlite3"
    assert hashlib.sha256(rd.content).hexdigest() == body["sha256"]
    assert len(rd.content) == body["size_bytes"]
    assert _only_files(backup_dir) == ["http-1.db"]


def test_http_backup_errors_and_traffic_during_backup(http_client):
    r404 = http_client.get("/backups/missing")
    assert r404.status_code == 404
    assert r404.json()["error"]["code"] == "backup_not_found"

    # An encoded space decodes to an ID that still hits the route but fails
    # the safe-character check. (An encoded slash collapses to a sub-path and
    # never reaches the handler.)
    rbad = http_client.post("/backups/bad%20id")
    assert rbad.status_code == 400
    assert rbad.json()["error"]["code"] == "invalid_backup_id"

    # /quote keeps working while a backup is in flight on another thread.
    quote_payload = {
        "version": V,
        "intervals": [{
            "start": "2024-07-06T00:00:00-04:00",
            "end": "2024-07-06T01:00:00-04:00",
            "timezone": "America/New_York",
            "energy": "10",
        }],
    }
    quotes: list[int] = []

    def quote_loop():
        for _ in range(20):
            quotes.append(http_client.post("/quote", json=quote_payload)
                          .status_code)

    t = threading.Thread(target=quote_loop)
    t.start()
    rb = http_client.post("/backups/with-traffic")
    t.join()
    assert rb.status_code == 201, rb.text
    assert quotes and all(code == 200 for code in quotes), quotes
    assert len(rb.json()["sha256"]) == 64
