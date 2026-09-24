"""Negotiation engine end-to-end: convergence, guardrails in the loop, deadlock/mediation, isolation, RFQ, telemetry."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.core.models import IssueMandate, Outcome
from agents.negotiation.engine import NegotiationSession
from agents.negotiation.renegotiation import TelemetryEvent, amendment_scenario, assess
from agents.negotiation.rfq import RFQOrchestrator
from agents.scenarios import get_scenario, load_scenarios

BILATERAL = [s.id for s in load_scenarios().values() if s.mode == "bilateral"]


def assert_compliant(session: NegotiationSession, terms: dict[str, float]) -> None:
    arb = session.arbiter
    assert arb.schema_violations(terms) == []
    assert arb.rulebook.check_terms(terms) == []
    for role in ("buyer", "supplier"):
        assert arb.policy_violations(role, terms, None) == [], role


@pytest.mark.parametrize("scenario_id", BILATERAL)
async def test_every_scenario_reaches_a_compliant_efficient_agreement(scenario_id):
    session = NegotiationSession(get_scenario(scenario_id))
    result = await session.run()
    assert result.status in (Outcome.AGREEMENT, Outcome.MEDIATED)
    assert_compliant(session, result.agreed_terms)
    assert result.rounds <= session.max_rounds
    assert result.efficiency["pareto_gap"] < 0.06
    # integrative bargaining beats naive split-the-difference on joint utility
    assert result.efficiency["joint_gain_vs_split"] > 0


async def test_runs_are_deterministic():
    a = await NegotiationSession(get_scenario("semiconductor_spot_po")).run()
    b = await NegotiationSession(get_scenario("semiconductor_spot_po")).run()
    assert a.agreed_terms == b.agreed_terms and a.rounds == b.rounds


async def test_red_team_attacks_are_contained():
    clean = await NegotiationSession(get_scenario("semiconductor_spot_po")).run()
    session = NegotiationSession(get_scenario("semiconductor_spot_po"), red_team=True)
    result = await session.run()
    assert result.status is Outcome.AGREEMENT
    assert_compliant(session, result.agreed_terms)
    assert result.agreed_terms == clean.agreed_terms  # attacks changed nothing material
    fired = {a.kind for a in session.injector.fired}
    assert fired == {"over_concession", "leak", "injection", "illegal_terms", "premature_accept", "toxicity", "pii"}
    blocks = [e for e in session.events if e.type == "guardrail_block_detail"]
    assert len(blocks) == result.blocked_moves == 3
    redacted_rules = {r["rule_id"] for e in session.events if e.type == "message_redacted_detail"
                      for r in e.data["redactions"]}
    assert {"SAFE-LEAK", "SAFE-INJECTION", "SAFE-TOXICITY", "SAFE-PII"} <= redacted_rules
    transcript = " ".join(t.message for t in session.turns)
    assert "ignore all previous instructions" not in transcript.lower()
    assert "98765" not in transcript and "clowns" not in transcript
    assert "3.70 per unit, so there is some room" not in transcript


async def test_deadlock_is_detected_and_mediated_to_nash():
    session = NegotiationSession(get_scenario("lithium_hardball"))
    result = await session.run()
    types = [e.type for e in session.events]
    assert "deadlock_detected" in types and "mediation_proposal" in types
    assert result.status is Outcome.MEDIATED and result.mediated
    nash = session.arbiter.mediate()
    assert result.agreed_terms == nash.terms
    assert result.requires_cfo_approval  # USD 976k > USD 900k delegated authority


async def test_no_zopa_ends_without_deal_and_escalates():
    sc = get_scenario("semiconductor_spot_po")
    sc.buyer.mandate["unit_price"] = IssueMandate(ideal=3.3, limit=3.6, weight=0.34)
    sc.buyer.budget_cap = 180000
    session = NegotiationSession(sc)
    result = await session.run()
    assert result.status is Outcome.NO_DEAL and result.agreed_terms is None
    assert "escalate" in result.reason
    assert "unit_price" in next(e for e in session.events if e.type == "mediation_failed_detail").data["detail"]


async def test_event_views_isolate_private_information():
    session = NegotiationSession(get_scenario("semiconductor_spot_po"), red_team=True)
    await session.run()
    supplier_view = session.events_for("supplier")
    buyer_only = [e for e in session.events if {v.value for v in e.visibility} == {"buyer", "arbiter"}]
    assert buyer_only, "expected buyer-private events"
    assert not set(map(id, buyer_only)) & set(map(id, supplier_view))
    assert all(e.data.get("actor") != "buyer" for e in supplier_view if e.type == "turn_private")
    dump = json.dumps([e.data for e in supplier_view])
    # the buyer's budget value and its private POLICY-BUDGET finding (from the red-team over-concession) stay hidden
    assert "205000" not in dump and "205,000" not in dump
    # rule *names* are public (the catalogue), but the buyer's POLICY-BUDGET *finding* must never reach the supplier
    assert "exceeds the CFO budget cap" not in dump and '"rule_id": "POLICY-BUDGET"' not in dump
    assert all(e.type not in ("analytics", "bargaining_analysis") for e in supplier_view)
    buyer_dump = json.dumps([e.data for e in session.events_for("buyer")])
    assert '"rule_id": "POLICY-BUDGET"' in buyer_dump  # ...while the buyer does see its own blocked move


class RogueBrain:
    """An 'unbounded' LLM that always concedes everything - the arbiter must contain it."""

    name = "rogue-test-brain"

    def __init__(self, give_away: dict[str, float]):
        self.give_away = give_away
        self.calls = 0

    async def propose(self, brief: dict[str, Any]) -> dict[str, Any] | None:
        self.calls += 1
        return {"action": "counter", "offer": dict(self.give_away), "message": "Deal at any price!"}


class FlakyBrain:
    name = "flaky"

    async def propose(self, brief: dict[str, Any]) -> dict[str, Any] | None:
        return None  # e.g. Lyzr timeout


async def test_unbounded_llm_is_always_overridden():
    sc = get_scenario("semiconductor_spot_po")
    rogue = RogueBrain({"unit_price": 4.6, "delivery_days": 42, "payment_terms_days": 15, "sla_on_time_pct": 92.0,
                        "late_penalty_pct_per_day": 0.1, "penalty_cap_pct": 3, "warranty_months": 12})
    session = NegotiationSession(sc, brains={"buyer": rogue, "supplier": FlakyBrain()})
    result = await session.run()
    # every give-away proposal is blocked and replaced by the compliant engine move
    assert rogue.calls > 0 and result.blocked_moves == rogue.calls
    assert all(t.source == "fallback" for t in session.turns if t.actor == "buyer")
    assert all(t.source == "engine" for t in session.turns if t.actor == "supplier")  # flaky LLM -> engine
    assert result.agreed_terms is not None
    assert_compliant(session, result.agreed_terms)


async def test_llm_brief_never_contains_the_mandate():
    session = NegotiationSession(get_scenario("semiconductor_spot_po"))
    buyer = session.buyer
    from agents.negotiation.negotiator import TurnContext
    ctx = TurnContext(round=1, own_turn=0, max_rounds=12, counterpart_offer=None, counterpart_message="",
                      final_turn=False)
    brief = json.dumps(buyer.brief(ctx, buyer.plan(ctx)))
    # mandate values (budget, approval limit, BATNA price, price ceiling) and envelope fields never reach the LLM
    for secret in ("205000", "190000", "4.05", "4.1,", "4.1}", "batna_terms", "budget_cap", "auto_approve_limit",
                   "reservation_utility", "mandate\""):
        assert secret not in brief, secret


async def test_rfq_awards_pareto_efficient_best_quote():
    orch = RFQOrchestrator(get_scenario("steel_rfq"))
    result = await orch.run()
    agreed = [c for c in result.candidates if c.terms]
    assert result.winner is not None and len(agreed) >= 2
    winner = next(c for c in result.candidates if c.supplier_id == result.winner)
    assert winner.pareto_efficient and winner.rank == 1
    assert all(winner.u_buyer >= c.u_buyer for c in agreed if c.pareto_efficient)
    assert result.leverage_log, "competing quotes should create buyer leverage"
    for c in agreed:
        assert_compliant(orch.sessions[c.supplier_id], c.terms)
    # the MSME lane inherits the statutory 45-day cap even though the buyer's mandate asks for 60
    assert orch.sessions["sagar"].arbiter.envelopes["buyer"].mandate["payment_terms_days"].ideal == 45


async def test_rfq_leverage_improves_buyer_outcomes():
    with_lev = await RFQOrchestrator(get_scenario("steel_rfq"), leverage=True).run()
    without = await RFQOrchestrator(get_scenario("steel_rfq"), leverage=False).run()
    best = lambda r: max(c.u_buyer for c in r.candidates if c.terms)  # noqa: E731
    assert best(with_lev) >= best(without) - 1e-9
    krupa_with = next(c for c in with_lev.candidates if c.supplier_id == "krupa")
    krupa_without = next(c for c in without.candidates if c.supplier_id == "krupa")
    if krupa_with.terms and krupa_without.terms:
        assert krupa_with.terms["unit_price"] <= krupa_without.terms["unit_price"]


class TestTelemetry:
    @pytest.fixture
    def contract(self):
        return {"contract_id": "CTR-TEST", "version": 1,
                "terms": {"unit_price": 3.72, "delivery_days": 21, "payment_terms_days": 15, "sla_on_time_pct": 98.5,
                          "late_penalty_pct_per_day": 0.25, "penalty_cap_pct": 5.0, "warranty_months": 24},
                "commercial_terms": {"total_value": 186000.0, "currency": "USD"},
                "legal": {"force_majeure_events": ["severe_weather", "port_closure"]},
                "executable": {"sla_rules": [{"id": "LD-DELAY", "params": {"rate_pct_per_day": 0.25,
                                                                           "cap_amount": 9300.0}}]},
                "parties": {"supplier": {"supplier_id": "vega"}}}

    def test_force_majeure_triggers_bounded_renegotiation(self, contract):
        ev = TelemetryEvent(contract_id="CTR-TEST", event_type="severe_weather", severity=4, expected_delay_days=7)
        a = assess(ev, contract)
        assert a.action == "renegotiate" and a.force_majeure and a.affected_issues == ["delivery_days", "unit_price"]

    def test_minor_event_applies_sla_schedule(self, contract):
        ev = TelemetryEvent(contract_id="CTR-TEST", event_type="severe_weather", severity=2, expected_delay_days=4)
        a = assess(ev, contract)
        assert a.action == "apply_sla" and a.sla_estimate["amount"] == pytest.approx(0.25 / 100 * 4 * 186000)

    def test_small_index_move_is_ignored(self, contract):
        ev = TelemetryEvent(contract_id="CTR-TEST", event_type="commodity_price_spike", price_index_change_pct=1.5)
        assert assess(ev, contract).action == "no_action"

    async def test_weather_amendment_negotiates_within_bounds(self, contract):
        sc = get_scenario("semiconductor_spot_po")
        ev = TelemetryEvent(contract_id="CTR-TEST", event_type="severe_weather", severity=4, expected_delay_days=7)
        amend = amendment_scenario(sc, sc.suppliers[0], contract, ev, assess(ev, contract))
        assert [i.key for i in amend.issues] == ["delivery_days", "unit_price"]
        session = NegotiationSession(amend)
        result = await session.run()
        assert result.status in (Outcome.AGREEMENT, Outcome.MEDIATED)
        assert 21 < result.agreed_terms["delivery_days"] <= 21 + 7 + 5
        assert result.agreed_terms["unit_price"] <= 205000 / 50000  # CFO budget cap still binds

    async def test_commodity_spike_capped_by_escalation_clause(self, contract):
        sc = get_scenario("semiconductor_spot_po")
        ev = TelemetryEvent(contract_id="CTR-TEST", event_type="commodity_price_spike", price_index="Wafer index",
                            price_index_change_pct=12, material_share_pct=60)
        amend = amendment_scenario(sc, sc.suppliers[0], contract, ev, assess(ev, contract))
        result = await NegotiationSession(amend).run()
        assert result.agreed_terms is not None
        assert result.agreed_terms["unit_price"] <= 3.72 * 1.05 + 1e-9
