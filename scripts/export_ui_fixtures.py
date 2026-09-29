"""Record real event streams from the backend (offline, deterministic) as fixtures for the frontend unit tests.

The Vitest suite feeds these to the UI's event reducer, so the tests also pin the contract between the backend's
event stream and the frontend. Re-run after changing an event's shape:

  python scripts/export_ui_fixtures.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.lyzr.settings import LyzrSettings  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.settings import AppSettings  # noqa: E402

OUT = ROOT / "frontend" / "src" / "test-fixtures"
RUNS = {  # fixture name -> (request, views to record)
    "red-team": ({"scenario_id": "semiconductor_spot_po", "red_team": True}, ("god", "supplier")),
    "deadlock": ({"scenario_id": "lithium_hardball"}, ("god",)),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        settings = AppSettings(data_dir=Path(tmp), frontend_dist=Path(tmp) / "no-ui", default_speed_ms=0,
                               database_url=f"sqlite:///{(Path(tmp) / 'fixtures.db').as_posix()}")
        with TestClient(create_app(settings, LyzrSettings(_env_file=None))) as client:
            for name, (request, views) in RUNS.items():
                run = client.post("/api/negotiations", json={**request, "llm_mode": "offline", "wait": True}).json()
                for view in views:
                    events = client.get(f"/api/runs/{run['id']}/events", params={"format": "json", "view": view}).json()
                    path = OUT / f"{name}.{view}.json"
                    path.write_text(json.dumps(events, separators=(",", ":")), encoding="utf-8")
                    print(f"{path.relative_to(ROOT)}: {len(events)} events, {path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
