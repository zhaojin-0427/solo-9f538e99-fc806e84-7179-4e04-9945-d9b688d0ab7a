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


def is_safe_component(value: str) -> bool:
    """True when *value* can be used as exactly one path component.

    ``.`` and ``..`` are rejected because the former is a no-op and the
    latter walks out of the retention directory; path separators and NUL
    bytes are rejected as well.
    """
    if not value or "\x00" in value:
        return False
    if value in (".", ".."):
        return False
    separators = {"/", "\\", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    if any(sep in value for sep in separators):
        return False
    return True
