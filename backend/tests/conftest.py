from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.lyzr.settings import LyzrSettings
from backend.app.main import create_app
from backend.app.settings import AppSettings

SECRET = "test-webhook-secret"


def make_app(tmp_path, **overrides):
    values = {"data_dir": tmp_path / "data", "frontend_dist": tmp_path / "no-ui", "default_speed_ms": 0,
              "telemetry_webhook_secret": SECRET, **overrides}
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
