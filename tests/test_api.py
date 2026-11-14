"""End-to-end API tests: upload, query, backfill, files, history."""
from __future__ import annotations

import io

from conftest import full_csv, make_csv


def _upload(client, station, day, raw, expected_version=0):
    return client.post(
        f"/stations/{station}/dates/{day}/uploads",
        data={"expected_version": str(expected_version)},
        files={"file": ("data.csv", io.BytesIO(raw), "text/csv")},
    )


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_first_upload_creates_version_one(client):
    resp = _upload(client, "ST-001", "2026-11-14", full_csv())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["replaced"] is False
    assert len(body["file_id"]) == 32


def test_records_endpoint_returns_current_version(client):
    _upload(client, "ST-001", "2026-11-14", full_csv())
    resp = client.get("/stations/ST-001/dates/2026-11-14/records")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert len(body["records"]) == 24
    assert body["records"][0] == {"hour": 0, "pm25": 1.0}
    assert body["records"][23]["hour"] == 23
    assert body["file_id"]


def test_records_404_when_nothing_published(client):
    resp = client.get("/stations/ST-999/dates/2026-11-14/records")
    assert resp.status_code == 404


def test_invalid_batch_is_not_published_at_all(client):
    # Missing hours 3 and 11; make one of the remaining rows negative too.
    rows = [(h, 2.5) for h in range(24) if h not in (3, 11)]
    rows = [(h, -0.1 if h == 7 else v) for h, v in rows]
    resp = _upload(client, "ST-001", "2026-11-14", make_csv(rows))
    assert resp.status_code == 422
    reasons = resp.json()["reasons"]
    missing = {r["hour"] for r in reasons if r["reason"] == "missing hour"}
    assert {3, 11} <= missing  # the rejected negative row's hour is also absent
    assert any("non-negative" in r["reason"] for r in reasons)

    # Nothing was published.
    assert client.get("/stations/ST-001/dates/2026-11-14/records").status_code == 404


def test_backfill_requires_current_version_and_replaces_batch(client):
    resp1 = _upload(client, "ST-001", "2026-11-14", full_csv())
    v1 = resp1.json()["version"]
    file_id_v1 = resp1.json()["file_id"]

    # Stale expected version -> conflict, old data remains.
    stale = _upload(client, "ST-001", "2026-11-14",
                    full_csv({h: 99.0 for h in range(24)}), expected_version=99)
    assert stale.status_code == 409
    assert stale.json()["error"] == "version_conflict"
    assert stale.json()["current_version"] == 1

    current = client.get("/stations/ST-001/dates/2026-11-14/records").json()
    assert current["version"] == 1 and current["file_id"] == file_id_v1

    # Correct expected version -> whole-batch replacement at version 2.
    new_values = {h: 50.0 + h for h in range(24)}
    resp2 = _upload(client, "ST-001", "2026-11-14",
                    full_csv(new_values), expected_version=v1)
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["version"] == 2
    assert resp2.json()["replaced"] is True

    current = client.get("/stations/ST-001/dates/2026-11-14/records").json()
    assert current["version"] == 2
    assert current["records"][0]["pm25"] == 50.0
    assert len(current["records"]) == 24


def test_backfill_validation_failure_keeps_old_version(client):
    _upload(client, "ST-001", "2026-11-14", full_csv())
    bad_rows = [(h, 1.0) for h in range(23)]  # hour 23 missing
    resp = _upload(client, "ST-001", "2026-11-14", make_csv(bad_rows),
                   expected_version=1)
    assert resp.status_code == 422
    current = client.get("/stations/ST-001/dates/2026-11-14/records").json()
    assert current["version"] == 1


def test_version_history_lists_all_versions(client):
    _upload(client, "ST-001", "2026-11-14", full_csv())
    _upload(client, "ST-001", "2026-11-14",
            full_csv({h: 7.0 for h in range(24)}), expected_version=1)
    _upload(client, "ST-001", "2026-11-14",
            full_csv({h: 8.0 for h in range(24)}), expected_version=2)

    resp = client.get("/stations/ST-001/dates/2026-11-14/versions")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert all(v["file_id"] and v["file_path"].endswith(".csv") for v in versions)
    # Every retained file has its own immutable identity.
    assert len({v["file_id"] for v in versions}) == 3


def test_original_csv_retained_and_downloadable_per_version(client):
    csv_v1 = full_csv({h: 1.0 for h in range(24)})
    csv_v2 = full_csv({h: 2.0 for h in range(24)})
    r1 = _upload(client, "ST-001", "2026-11-14", csv_v1)
    id1 = r1.json()["file_id"]
    _upload(client, "ST-001", "2026-11-14", csv_v2, expected_version=1)

    d1 = client.get("/stations/ST-001/dates/2026-11-14/versions/1/file")
    assert d1.status_code == 200
    assert d1.content == csv_v1
    assert d1.headers["X-File-Id"] == id1

    d2 = client.get("/stations/ST-001/dates/2026-11-14/versions/2/file")
    assert d2.status_code == 200
    assert d2.content == csv_v2
    assert d1.content != d2.content

    assert client.get(
        "/stations/ST-001/dates/2026-11-14/versions/99/file"
    ).status_code == 404


def test_station_isolation_and_date_isolation(client):
    _upload(client, "ST-A", "2026-11-14", full_csv({h: 1.0 for h in range(24)}))
    _upload(client, "ST-B", "2026-11-14", full_csv({h: 2.0 for h in range(24)}))
    _upload(client, "ST-A", "2026-11-15", full_csv({h: 3.0 for h in range(24)}))

    a = client.get("/stations/ST-A/dates/2026-11-14/records").json()
    b = client.get("/stations/ST-B/dates/2026-11-14/records").json()
    a2 = client.get("/stations/ST-A/dates/2026-11-15/records").json()
    assert a["records"][0]["pm25"] == 1.0
    assert b["records"][0]["pm25"] == 2.0
    assert a2["records"][0]["pm25"] == 3.0


def test_empty_upload_returns_400(client):
    resp = _upload(client, "ST-001", "2026-11-14", b"")
    assert resp.status_code == 400


def test_invalid_station_id_rejected(client):
    resp = _upload(client, "bad station!", "2026-11-14", full_csv())
    assert resp.status_code == 400


def test_invalid_date_format_returns_422(client):
    resp = client.get("/stations/ST-001/dates/2026-13-40/records")
    assert resp.status_code == 422
