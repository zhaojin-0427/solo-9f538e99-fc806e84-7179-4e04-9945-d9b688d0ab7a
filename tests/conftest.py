"""Pytest fixtures: isolated temp data dir and fresh FastAPI app per test."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the project root importable when tests run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app instance backed by a brand-new temporary data directory."""
    monkeypatch.setenv("PM25_DATA_DIR", str(tmp_path / "data"))

    # Drop cached app modules so config picks up the env var on import.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app.main import app  # imported after env var is set

    with TestClient(app) as test_client:
        yield test_client


def make_csv(rows: list[tuple[int, float]], header: str = "hour,pm25") -> bytes:
    lines = [header] + [f"{h},{v}" for h, v in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def full_csv(values: dict[int, float] | None = None) -> bytes:
    values = values or {h: float(h) + 1.0 for h in range(24)}
    return make_csv([(h, values[h]) for h in range(24)])
