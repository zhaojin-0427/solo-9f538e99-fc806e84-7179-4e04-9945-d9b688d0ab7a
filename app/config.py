"""Runtime configuration.

All state (SQLite database + published CSV files) lives under a single data
directory so that it can be mounted as a Docker volume.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.getenv("PM25_DATA_DIR", "/data")).resolve()
DB_PATH = DATA_DIR / "pm25.db"
FILES_DIR = DATA_DIR / "files"
TMP_DIR = DATA_DIR / "tmp"


def ensure_dirs() -> None:
    """Create the storage directories on startup."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
