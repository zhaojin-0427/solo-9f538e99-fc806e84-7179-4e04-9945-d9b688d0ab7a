"""Archive containment and legacy-archive recovery.

Two concerns are covered:

* new uploads must refuse a station id (``.`` / ``..``) whose archive would
  escape the retention directory;
* versions published *before* that check existed must still be listable and
  downloadable through the original endpoint, with their bytes recovered
  inside the retention directory -- but only from an original file that
  matches station/date/version and the registered checksum.
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import full_csv


def _upload(client, station, day, raw, expected_version=0):
    return client.post(
        f"/stations/{station}/dates/{day}/uploads",
        data={"expected_version": str(expected_version)},
        files={"file": ("data.csv", io.BytesIO(raw), "text/csv")},
    )


def _app_modules():
    from app import archive, config, db

    return archive, config, db


def _legacy_publish(station: str, day: str, raw: bytes, version: int = 1) -> dict:
    """Recreate what the pre-fix code left on disk and in SQLite.

    The archive is written exactly where the old code put it
    (``FILES_DIR / station / day / name``), which for ``..`` is outside
    ``FILES_DIR``. Returns the registered row.
    """
    _archive, config, db = _app_modules()
    file_id = hashlib.sha256(f"{station}|{day}|{version}".encode()).hexdigest()[:32]
    name = f"v{version}_{file_id}.csv"
    legacy_path = config.FILES_DIR / station / day / name
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(raw)

    from app.validation import parse_and_validate

    batch = parse_and_validate(raw)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO upload_versions "
            "(station_id, data_date, version, file_id, file_path, checksum) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                station,
                day,
                version,
                file_id,
                str(Path(station) / day / name),
                batch.checksum,
            ),
        )
        conn.executemany(
            "INSERT INTO hourly_records "
            "(station_id, data_date, version, hour, pm25) VALUES (?, ?, ?, ?, ?)",
            [(station, day, version, h, v) for h, v in batch.rows],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"file_id": file_id, "name": name, "path": legacy_path,
            "checksum": batch.checksum}


def _registered_row(station: str, day: str, version: int) -> dict:
    _archive, _config, db = _app_modules()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM upload_versions WHERE station_id = ? AND data_date = ? "
            "AND version = ?",
            (station, day, version),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _csv_files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.csv") if p.is_file())


# ---------------------------------------------------------------------------
# New uploads must stay inside the retention directory.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "station,encoded", [("..", "%2E%2E"), (".", "%2E")]
)
def test_new_upload_rejects_escaping_station_id(
    client, tmp_path, station, encoded
):
    data_dir = tmp_path / "data"
    resp = client.post(
        f"/stations/{encoded}/dates/2026-11-14/uploads",
        data={"expected_version": "0"},
        files={"file": ("data.csv", io.BytesIO(full_csv()), "text/csv")},
    )
    assert resp.status_code == 400, resp.text
    assert "留存目录" in resp.json()["detail"]

    # Nothing was published and nothing was written anywhere in the data dir
    # except the database itself.
    assert client.get(
        f"/stations/{encoded}/dates/2026-11-14/records"
    ).status_code == 404
    stray = [
        p for p in data_dir.rglob("*")
        if p.is_file() and p.name != "pm25.db" and not p.name.startswith("pm25.db-")
    ]
    assert stray == []


def test_upload_rejects_encoded_dot_dot(client, tmp_path):
    resp = client.post(
        "/stations/%2E%2E/dates/2026-11-14/uploads",
        data={"expected_version": "0"},
        files={"file": ("data.csv", io.BytesIO(full_csv()), "text/csv")},
    )
    assert resp.status_code == 400
    assert not list((tmp_path / "data").glob("2026-11-14/*.csv"))


def test_storage_layer_itself_refuses_unsafe_station(tmp_path, client):
    _archive, config, db = _app_modules()
    from app.validation import parse_and_validate

    batch = parse_and_validate(full_csv())
    with pytest.raises(db.PublishError):
        db.persist_csv_file("..", "2026-11-14", 1, batch)
    assert not (config.DATA_DIR / "2026-11-14").exists()


# ---------------------------------------------------------------------------
# Legacy versions: history still visible, original bytes recoverable.
# ---------------------------------------------------------------------------


def test_legacy_escaping_version_remains_listable_and_downloadable(client, tmp_path):
    raw = full_csv({h: 3.0 for h in range(24)})
    legacy = _legacy_publish("..", "2026-11-14", raw)
    # The original file really is outside the retention directory.
    files_dir = tmp_path / "data" / "files"
    assert legacy["path"].is_file()
    assert files_dir.resolve() not in legacy["path"].resolve().parents

    history = client.get("/stations/../dates/2026-11-14/versions")
    # ``..`` in the raw URL is normalised by the client; use the encoded form
    # the API actually receives in production.
    history = client.get("/stations/%2E%2E/dates/2026-11-14/versions")
    assert history.status_code == 200, history.text
    assert history.json()["count"] == 1

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code == 200, resp.text
    assert resp.content == raw
    assert resp.headers["X-File-Id"] == legacy["file_id"]

    # Archive was restored inside the retention directory...
    row = _registered_row("..", "2026-11-14", 1)
    restored = (files_dir / row["file_path"]).resolve()
    assert restored.is_file()
    assert restored.read_bytes() == raw
    assert files_dir.resolve() in restored.parents

    # ...the out-of-tree copy is gone, and a second download still works.
    assert not legacy["path"].exists()
    again = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert again.status_code == 200 and again.content == raw


def test_legacy_file_missing_reports_reason_and_keeps_registration(client, tmp_path):
    raw = full_csv()
    legacy = _legacy_publish("..", "2026-11-14", raw)
    before = _registered_row("..", "2026-11-14", 1)
    legacy["path"].unlink()

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code == 410, resp.text
    body = resp.json()
    assert body["error"] == "archive_missing"
    assert body["reason"]
    assert "缺失" in body["reason"]

    # Version record itself is untouched and still queryable.
    assert _registered_row("..", "2026-11-14", 1) == before
    history = client.get("/stations/%2E%2E/dates/2026-11-14/versions")
    assert history.status_code == 200 and history.json()["count"] == 1
    assert not list((tmp_path / "data" / "files").rglob("*.csv"))


def test_legacy_file_corrupt_is_rejected_without_rebinding(client, tmp_path):
    raw = full_csv()
    legacy = _legacy_publish("..", "2026-11-14", raw)
    before = _registered_row("..", "2026-11-14", 1)
    # Same name/version/station, different bytes -> checksum mismatch.
    legacy["path"].write_bytes(full_csv({h: 99.0 for h in range(24)}))

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "archive_corrupt"
    assert "校验和" in body["reason"]

    # Corrupt original is left alone, registration unchanged, nothing restored.
    assert _registered_row("..", "2026-11-14", 1) == before
    assert legacy["path"].is_file()
    assert not list((tmp_path / "data" / "files").rglob("*.csv"))


def test_database_file_path_is_not_used_to_read_other_files(client, tmp_path):
    """A doctored ``file_path`` must not become an arbitrary file read."""
    raw = full_csv()
    legacy = _legacy_publish("..", "2026-11-14", raw)

    # A perfectly valid, in-tree archive belonging to a *different* version.
    other_raw = full_csv({h: 7.0 for h in range(24)})
    other = _legacy_publish("ST-OTHER", "2026-11-15", other_raw, version=1)
    files_dir = tmp_path / "data" / "files"
    other_row = _registered_row("ST-OTHER", "2026-11-15", 1)

    _archive, _config, db = _app_modules()
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE upload_versions SET file_path = ? "
            "WHERE station_id = '..' AND data_date = '2026-11-14' AND version = 1",
            (other_row["file_path"],),
        )
    finally:
        conn.close()

    legacy["path"].unlink()  # the genuine original is gone

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code in (410, 422), resp.text
    assert resp.status_code != 200
    assert resp.content != other_raw
    # The other version's archive was not copied or modified.
    assert (files_dir / other_row["file_path"]).read_bytes() == other_raw


def test_wrong_station_version_or_file_id_does_not_restore(client, tmp_path):
    raw = full_csv()
    legacy = _legacy_publish("..", "2026-11-14", raw)
    # A file for version 2 exists at the legacy location, but version 1's
    # registered file_id points elsewhere -> must not be borrowed.
    bogus = full_csv({h: 5.0 for h in range(24)})
    (legacy["path"].parent / "v2_deadbeefdeadbeefdeadbeefdeadbeef.csv").write_bytes(
        bogus
    )
    legacy["path"].unlink()

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code == 410
    assert not list((tmp_path / "data" / "files").rglob("*.csv"))


# ---------------------------------------------------------------------------
# Failure safety and retryability.
# ---------------------------------------------------------------------------


def test_copy_failure_keeps_original_and_registration_retryable(
    client, tmp_path, monkeypatch
):
    raw = full_csv()
    legacy = _legacy_publish("..", "2026-11-14", raw)
    before = _registered_row("..", "2026-11-14", 1)

    _archive, _config, _db = _app_modules()

    def broken_replace(*_args, **_kwargs):
        raise OSError("simulated cross-device copy failure")

    monkeypatch.setattr(_archive.os, "replace", broken_replace)

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["error"] == "archive_recovery_failed"
    assert body["retryable"] is True
    assert "可重试" in body["reason"] or "重试" in body["reason"]

    # Original still there, registration unchanged, nothing restored yet.
    assert legacy["path"].is_file()
    assert legacy["path"].read_bytes() == raw
    assert _registered_row("..", "2026-11-14", 1) == before
    assert not list((tmp_path / "data" / "files").rglob("*.csv"))

    monkeypatch.undo()
    retry = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert retry.status_code == 200 and retry.content == raw
    row = _registered_row("..", "2026-11-14", 1)
    assert (tmp_path / "data" / "files" / row["file_path"]).read_bytes() == raw


def test_database_update_failure_keeps_original_and_registration_retryable(
    client, tmp_path, monkeypatch
):
    raw = full_csv()
    legacy = _legacy_publish("..", "2026-11-14", raw)
    before = _registered_row("..", "2026-11-14", 1)

    _archive, _config, db = _app_modules()

    def broken_update(*_args, **_kwargs):
        raise db.sqlite3.OperationalError("simulated database write error")

    monkeypatch.setattr(db, "update_version_file_path", broken_update)

    resp = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert resp.status_code == 500, resp.text
    assert resp.json()["retryable"] is True

    # Original and registration are intact; the verified in-tree copy may
    # already exist, which makes the retry cheap and idempotent.
    assert legacy["path"].is_file() and legacy["path"].read_bytes() == raw
    assert _registered_row("..", "2026-11-14", 1) == before

    monkeypatch.undo()
    retry = client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")
    assert retry.status_code == 200 and retry.content == raw
    row = _registered_row("..", "2026-11-14", 1)
    assert row["file_path"] != before["file_path"]
    assert (tmp_path / "data" / "files" / row["file_path"]).read_bytes() == raw


def test_concurrent_restore_binds_no_wrong_file(client, tmp_path):
    raw = full_csv({h: float(h) for h in range(24)})
    legacy = _legacy_publish("..", "2026-11-14", raw)

    # Another version of the same station, also legacy/out-of-tree.
    raw2 = full_csv({h: 100.0 + h for h in range(24)})
    legacy2 = _legacy_publish("..", "2026-11-14", raw2, version=2)

    def download(_i):
        return client.get("/stations/%2E%2E/dates/2026-11-14/versions/1/file")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(download, range(8)))

    assert all(r.status_code == 200 for r in responses)
    assert {r.content for r in responses} == {raw}

    row = _registered_row("..", "2026-11-14", 1)
    row2 = _registered_row("..", "2026-11-14", 2)
    files_dir = tmp_path / "data" / "files"
    assert (files_dir / row["file_path"]).read_bytes() == raw
    assert (files_dir / row2["file_path"]).read_bytes() == raw2
    assert row["file_id"] != row2["file_id"]

    # Version 2 is unaffected and still downloadable with its own bytes.
    d2 = client.get("/stations/%2E%2E/dates/2026-11-14/versions/2/file")
    assert d2.status_code == 200 and d2.content == raw2


# ---------------------------------------------------------------------------
# Normal operation is unaffected.
# ---------------------------------------------------------------------------


def test_normal_publish_backfill_and_download_still_work(client, tmp_path):
    csv_v1 = full_csv({h: 1.0 for h in range(24)})
    csv_v2 = full_csv({h: 2.0 for h in range(24)})

    r1 = _upload(client, "ST-001", "2026-11-14", csv_v1)
    assert r1.status_code == 201
    r2 = _upload(client, "ST-001", "2026-11-14", csv_v2, expected_version=1)
    assert r2.status_code == 201 and r2.json()["version"] == 2

    d1 = client.get("/stations/ST-001/dates/2026-11-14/versions/1/file")
    d2 = client.get("/stations/ST-001/dates/2026-11-14/versions/2/file")
    assert d1.status_code == 200 and d1.content == csv_v1
    assert d2.status_code == 200 and d2.content == csv_v2

    # Normal archives live directly under files/<station>/<date>/.
    files_dir = tmp_path / "data" / "files"
    row = _registered_row("ST-001", "2026-11-14", 1)
    assert row["file_path"].startswith("ST-001/2026-11-14/")
    assert (files_dir / row["file_path"]).read_bytes() == csv_v1


def test_safe_station_with_dots_and_colons_still_accepted(client):
    for station in ["ST.001", "A:B", "-_."]:
        resp = _upload(client, station, "2026-11-14", full_csv())
        assert resp.status_code == 201, (station, resp.text)
