"""Concurrency and failure-recovery tests.

These verify the key guarantees:

* concurrent backfills carrying the same expected version: exactly one wins;
* database write failure mid-publish leaves the previous version intact;
* filesystem write failure leaves the previous version intact;
* retained files of old versions are never overwritten.
"""
from __future__ import annotations

import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import full_csv


def _raw_upload(client, station, day, raw, expected_version):
    return client.post(
        f"/stations/{station}/dates/{day}/uploads",
        data={"expected_version": str(expected_version)},
        files={"file": ("data.csv", io.BytesIO(raw), "text/csv")},
    )


def test_concurrent_backfills_exactly_one_wins(client):
    # Seed version 1.
    r = _raw_upload(client, "ST-001", "2026-11-14",
                    full_csv({h: 1.0 for h in range(24)}), 0)
    assert r.status_code == 201

    results: list[int] = []
    lock = threading.Lock()

    def backfill(i: int):
        values = {h: float(i + 10) for h in range(24)}
        resp = _raw_upload(client, "ST-001", "2026-11-14",
                           full_csv(values), expected_version=1)
        with lock:
            results.append(resp.status_code)
        return resp

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(backfill, range(8)))

    created = [r for r in responses if r.status_code == 201]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(created) == 1
    assert len(conflicts) == 7
    assert sorted(results).count(201) == 1

    body = client.get("/stations/ST-001/dates/2026-11-14/records").json()
    assert body["version"] == 2  # no lost update, no double increment
    assert len(body["records"]) == 24

    history = client.get("/stations/ST-001/dates/2026-11-14/versions").json()
    assert [v["version"] for v in history["versions"]] == [2, 1]


def test_first_upload_contention_only_one_version_created(client):
    outcomes = []
    lock = threading.Lock()

    def first_upload(i: int):
        resp = _raw_upload(client, "ST-NEW", "2026-11-14",
                           full_csv({h: float(i) for h in range(24)}), 0)
        with lock:
            outcomes.append(resp.status_code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(first_upload, range(4)))

    assert outcomes.count(201) == 1
    assert outcomes.count(409) == 3
    body = client.get("/stations/ST-NEW/dates/2026-11-14/records").json()
    assert body["version"] == 1


def test_database_failure_during_publish_keeps_old_version(client, monkeypatch):
    # Seed a healthy version 1.
    r = _raw_upload(client, "ST-001", "2026-11-14",
                    full_csv({h: 1.0 for h in range(24)}), 0)
    assert r.status_code == 201
    v1_file_id = r.json()["file_id"]

    from app import db as db_module

    def broken_insert(conn, station_id, date_str, version, rows):
        raise db_module.sqlite3.OperationalError("simulated disk I/O error")

    monkeypatch.setattr(db_module, "_insert_records", broken_insert)

    resp = _raw_upload(client, "ST-001", "2026-11-14",
                       full_csv({h: 42.0 for h in range(24)}), 1)
    assert resp.status_code == 500
    assert resp.json()["error"] == "publish_failed"

    monkeypatch.undo()

    # Old version still fully queryable...
    body = client.get("/stations/ST-001/dates/2026-11-14/records").json()
    assert body["version"] == 1
    assert body["file_id"] == v1_file_id
    assert body["records"][0]["pm25"] == 1.0
    assert len(body["records"]) == 24

    # ...and its retained CSV is still downloadable byte-for-byte.
    dl = client.get("/stations/ST-001/dates/2026-11-14/versions/1/file")
    assert dl.status_code == 200
    assert dl.content == full_csv({h: 1.0 for h in range(24)})

    # No orphan version row was committed.
    history = client.get("/stations/ST-001/dates/2026-11-14/versions").json()
    assert [v["version"] for v in history["versions"]] == [1]

    # A subsequent retry with the same expected version succeeds.
    retry = _raw_upload(client, "ST-001", "2026-11-14",
                        full_csv({h: 42.0 for h in range(24)}), 1)
    assert retry.status_code == 201
    assert retry.json()["version"] == 2


def test_filesystem_failure_keeps_old_version(client, monkeypatch):
    _raw_upload(client, "ST-001", "2026-11-14",
                full_csv({h: 1.0 for h in range(24)}), 0)

    from app import db as db_module

    def broken_persist(*_args, **_kwargs):
        raise OSError("simulated filesystem read-only")

    monkeypatch.setattr(db_module, "persist_csv_file", broken_persist)

    resp = _raw_upload(client, "ST-001", "2026-11-14",
                       full_csv({h: 5.0 for h in range(24)}), 1)
    assert resp.status_code == 500

    monkeypatch.undo()
    body = client.get("/stations/ST-001/dates/2026-11-14/records").json()
    assert body["version"] == 1
    history = client.get("/stations/ST-001/dates/2026-11-14/versions").json()
    assert history["count"] == 1


def test_old_csv_files_are_immutable_on_disk(client, tmp_path):
    csv_v1 = full_csv({h: 1.0 for h in range(24)})
    r1 = _raw_upload(client, "ST-001", "2026-11-14", csv_v1, 0)
    path_v1 = r1.json()  # file id captured below via history

    _raw_upload(client, "ST-001", "2026-11-14",
                full_csv({h: 9.0 for h in range(24)}), 1)

    files_dir = tmp_path / "data" / "files"
    csv_files = list(files_dir.rglob("*.csv"))
    assert len(csv_files) == 2  # nothing overwritten

    history = client.get("/stations/ST-001/dates/2026-11-14/versions").json()
    v1 = next(v for v in history["versions"] if v["version"] == 1)
    on_disk_v1 = (files_dir / v1["file_path"]).read_bytes()
    assert on_disk_v1 == csv_v1
