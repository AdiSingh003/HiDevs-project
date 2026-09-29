from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agents.lyzr.settings import LyzrSettings
from backend.app.db import make_engine, metadata
from backend.app.main import create_app
from backend.app.settings import AppSettings

SECRET = "test-webhook-secret"
# Set to run the API and store tests against a real PostgreSQL (CI does); otherwise each test gets its own SQLite file.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or None


def reset_database(url: str) -> None:
    """A shared PostgreSQL keeps rows between tests, so each fresh app starts from an empty schema."""
    engine = make_engine(url)
    metadata.drop_all(engine)
    engine.dispose()


def make_app(tmp_path, fresh: bool = True, **overrides):
    values = {"data_dir": tmp_path / "data", "frontend_dist": tmp_path / "no-ui", "default_speed_ms": 0,
              "telemetry_webhook_secret": SECRET, "database_url": TEST_DATABASE_URL, **overrides}
    if TEST_DATABASE_URL and fresh:
        reset_database(TEST_DATABASE_URL)
    return create_app(AppSettings(**values), LyzrSettings(_env_file=None))


@pytest.fixture
def client(tmp_path):
    with TestClient(make_app(tmp_path)) as c:
        yield c


@pytest.fixture
def negotiated(client):
    r = client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po", "wait": True})
    assert r.status_code == 201, r.text
    return r.json()
