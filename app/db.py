"""SQLite persistence and optimistic version control.

Concurrency model
-----------------
Every publish opens one connection with ``BEGIN IMMEDIATE``, so competing
backfills are serialized at the database level. The current version is
checked *inside* that write transaction: only the request whose expected
version matches can commit, every concurrent loser gets a conflict error and
the winner's whole batch replaces the previous one.

Durability model
----------------
The immutable CSV of each version is written (via a temp file + atomic
``os.replace``) *before* the database transaction starts. If the transaction
fails or the version check conflicts, the previous version row still points at
the previous file, which is never overwritten or deleted; nothing already
published is ever lost or partially visible.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from . import config
from .validation import ParsedBatch

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_versions (
    station_id     TEXT NOT NULL,
    data_date      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    file_id        TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (station_id, data_date, version),
    UNIQUE (file_id)
);

CREATE TABLE IF NOT EXISTS hourly_records (
    station_id     TEXT NOT NULL,
    data_date      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    hour           INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    pm25           REAL NOT NULL CHECK (pm25 >= 0.0),
    PRIMARY KEY (station_id, data_date, version, hour),
    FOREIGN KEY (station_id, data_date, version)
        REFERENCES upload_versions (station_id, data_date, version)
);

CREATE INDEX IF NOT EXISTS idx_versions_lookup
    ON upload_versions (station_id, data_date, version);
"""


class VersionConflictError(Exception):
    """The supplied expected version no longer matches the current version."""

    def __init__(self, expected: int, current: int | None):
        self.expected = expected
        self.current = current
        super().__init__(
            f"version conflict: expected {expected}, current is {current}"
        )


class PublishError(Exception):
    """A storage or database failure during publish; old version stays intact."""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH, timeout=30.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    # Wake lock waiters periodically instead of deadlocking, and enforce
    # foreign keys on every connection.
    conn.execute(f"PRAGMA busy_timeout = {30_000}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db() -> None:
    config.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_current_version(
    conn: sqlite3.Connection, station_id: str, data_date: str
) -> int | None:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM upload_versions "
        "WHERE station_id = ? AND data_date = ?",
        (station_id, data_date),
    ).fetchone()
    return None if row is None or row["v"] is None else int(row["v"])


def _version_row(
    conn: sqlite3.Connection, station_id: str, data_date: str, version: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM upload_versions "
        "WHERE station_id = ? AND data_date = ? AND version = ?",
        (station_id, data_date, version),
    ).fetchone()


def persist_csv_file(
    station_id: str, data_date: str, version: int, batch: ParsedBatch
) -> tuple[str, str]:
    """Write the raw CSV to an immutable path atomically.

    Returns ``(file_id, relative_path)``. Raises :class:`PublishError` on any
    filesystem failure; no previously published file is touched.
    """
    file_id = uuid.uuid4().hex
    # Immutable per-version path: backfills never overwrite a stored CSV.
    rel_path = Path(station_id) / data_date / f"v{version}_{file_id}.csv"
    final_path = config.FILES_DIR / rel_path
    tmp_path = config.TMP_DIR / f".{file_id}.tmp.csv"
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as fh:
            fh.write(batch.raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
    except OSError as exc:
        # Best-effort cleanup of the temp file; the published tree is untouched.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise PublishError(f"failed to store CSV file: {exc}") from exc
    return file_id, str(rel_path)


def _insert_records(conn, station_id: str, date_str: str,
                    version: int, rows: list[tuple[int, float]]) -> None:
    """Indirection point so failure tests can simulate a database write error."""
    conn.executemany(
        "INSERT INTO hourly_records "
        "(station_id, data_date, version, hour, pm25) "
        "VALUES (?, ?, ?, ?, ?)",
        [(station_id, date_str, version, hour, pm25) for hour, pm25 in rows],
    )


def publish_batch(
    station_id: str,
    data_date: date,
    expected_version: int,
    batch: ParsedBatch,
) -> dict[str, Any]:
    """Validate optimistic version and commit a full batch replacement.

    Runs in a single ``BEGIN IMMEDIATE`` transaction so concurrent backfills
    serialize and exactly one wins.
    """
    date_str = data_date.isoformat()
    conn = connect()
    try:
        # Acquire the write lock up front, serializing concurrent publishes.
        conn.execute("BEGIN IMMEDIATE")

        current = get_current_version(conn, station_id, date_str)
        if expected_version != (current or 0):
            conn.execute("ROLLBACK")
            raise VersionConflictError(expected=expected_version, current=current)

        new_version = (current or 0) + 1

        # File first; if this fails, no transaction content exists yet and the
        # previous version remains fully queryable. Referenced via module
        # attribute so the storage layer can be swapped in failure tests.
        try:
            file_id, rel_path = sys.modules[__name__].persist_csv_file(
                station_id, date_str, new_version, batch
            )
        except (PublishError, OSError) as exc:
            conn.execute("ROLLBACK")
            if isinstance(exc, PublishError):
                raise
            raise PublishError(f"failed to store CSV file: {exc}") from exc

        try:
            conn.execute(
                "INSERT INTO upload_versions "
                "(station_id, data_date, version, file_id, file_path, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    station_id,
                    date_str,
                    new_version,
                    file_id,
                    rel_path,
                    batch.checksum,
                ),
            )
            sys.modules[__name__]._insert_records(
                conn, station_id, date_str, new_version, batch.rows
            )
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            # The orphaned new file is harmless (nothing references it), but
            # remove it to keep the storage tree tidy.
            try:
                (config.FILES_DIR / rel_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise PublishError(f"database write failed: {exc}") from exc

        conn.execute("COMMIT")
    except VersionConflictError:
        raise
    except PublishError:
        raise
    except sqlite3.Error as exc:
        # Lock/timeout or any unexpected SQLite failure: old version survives.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise PublishError(f"database transaction failed: {exc}") from exc
    finally:
        conn.close()

    return {
        "station_id": station_id,
        "data_date": date_str,
        "version": new_version,
        "file_id": file_id,
        "checksum": batch.checksum,
    }


def get_current_data(
    station_id: str, data_date: date
) -> dict[str, Any] | None:
    """Return the current version's metadata and 24 hourly records."""
    date_str = data_date.isoformat()
    conn = connect()
    try:
        version = get_current_version(conn, station_id, date_str)
        if version is None:
            return None
        meta = _version_row(conn, station_id, date_str, version)
        records = conn.execute(
            "SELECT hour, pm25 FROM hourly_records "
            "WHERE station_id = ? AND data_date = ? AND version = ? "
            "ORDER BY hour",
            (station_id, date_str, version),
        ).fetchall()
        return {
            "station_id": station_id,
            "data_date": date_str,
            "version": version,
            "file_id": meta["file_id"],
            "file_path": meta["file_path"],
            "checksum": meta["checksum"],
            "created_at": meta["created_at"],
            "records": [
                {"hour": row["hour"], "pm25": row["pm25"]} for row in records
            ],
        }
    finally:
        conn.close()


def list_versions(station_id: str, data_date: date) -> list[dict[str, Any]]:
    """Return every published version (newest first), including file ids."""
    date_str = data_date.isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT version, file_id, file_path, checksum, created_at "
            "FROM upload_versions "
            "WHERE station_id = ? AND data_date = ? ORDER BY version DESC",
            (station_id, date_str),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_version_file(
    station_id: str, data_date: date, version: int
) -> dict[str, Any] | None:
    date_str = data_date.isoformat()
    conn = connect()
    try:
        row = _version_row(conn, station_id, date_str, version)
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


def reset_for_tests() -> None:
    """Drop all data; used by the test suite against a temp data dir."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(
            "DELETE FROM hourly_records; DELETE FROM upload_versions;"
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


# Kept here so callers can render structured failure details uniformly.
def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)
