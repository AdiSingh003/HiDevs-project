from __future__ import annotations

import pytest

from agents.core.utility import UtilityModel
from agents.guardrails.arbiter import LegalArbiter
from agents.scenarios import get_scenario


@pytest.fixture
def semi():
    return get_scenario("semiconductor_spot_po")


@pytest.fixture
def arbiter(semi):
    return LegalArbiter(semi, semi.suppliers[0])


@pytest.fixture
def buyer_util(arbiter) -> UtilityModel:
    return arbiter.utils["buyer"]


@pytest.fixture
def supplier_util(arbiter) -> UtilityModel:
    return arbiter.utils["supplier"]


@pytest.fixture
def compliant_offer() -> dict[str, float]:
    return {"unit_price": 3.9, "delivery_days": 25, "payment_terms_days": 30, "sla_on_time_pct": 97.0,
            "late_penalty_pct_per_day": 0.4, "penalty_cap_pct": 8.0, "warranty_months": 18}
