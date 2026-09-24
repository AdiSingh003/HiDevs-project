"""Preset negotiation scenarios (JSON) - each one is a public RFQ plus the parties' sealed envelopes."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..core.models import Scenario

SCENARIO_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load_scenarios() -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        scenario = Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))
        out[scenario.id] = scenario
    return out


def get_scenario(scenario_id: str) -> Scenario:
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        raise KeyError(f"unknown scenario '{scenario_id}' (available: {', '.join(scenarios)})")
    return scenarios[scenario_id].model_copy(deep=True)
