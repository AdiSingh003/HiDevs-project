"""Property-style stress test (100% policy adherence) and targeted deadlock / edge-case tests."""

from __future__ import annotations

import random

import pytest

from agents.audit.ledger import AuditLedger
from agents.core.models import Action, Decision, IssueMandate, Outcome, Scenario, StrategyProfile
from agents.core.pareto import BargainingSpace
from agents.core.utility import PRICE_KEY, UtilityModel
from agents.guardrails.arbiter import ReviewContext
from agents.negotiation.engine import NegotiationSession
from agents.scenarios import get_scenario

# (issue, buyer ideal range, buyer limit offset range, supplier ideal range, supplier limit range)
# offsets move the limit away from the ideal in the direction the party dislikes
RANGES = {
    "unit_price": ((3.0, 3.6), (0.4, 1.3), (4.0, 5.0), (3.2, 3.9)),
    "delivery_days": ((7, 20), (10, 30), (30, 60), (10, 28)),
    "payment_terms_days": ((30, 45), (10, 30), (0, 20), (25, 45)),
    "sla_on_time_pct": ((97.5, 99.8), (2.0, 8.0), (88.0, 94.0), (96.0, 99.5)),
    "late_penalty_pct_per_day": ((0.6, 1.0), (0.3, 0.5), (0.05, 0.2), (0.4, 0.9)),
    "penalty_cap_pct": ((10, 20), (4, 8), (1, 4), (8, 15)),
    "warranty_months": ((20, 36), (6, 14), (6, 12), (18, 30)),
}


def random_scenario(rng: random.Random) -> Scenario:
    base = get_scenario("semiconductor_spot_po")
    data = base.model_dump(mode="json")
    for key, (b_ideal, b_off, s_ideal, s_limit) in RANGES.items():
        spec = base.issue(key)
        lower = spec.buyer_prefers.value == "lower"
        bi = spec.round(rng.uniform(*b_ideal))
        bl = spec.round(bi + (1 if lower else -1) * rng.uniform(*b_off))
        si = spec.round(rng.uniform(*s_ideal))
        sl = spec.round(rng.uniform(*s_limit))
        if lower and not sl < si:
            sl = spec.round(si - spec.step * 3)
        if not lower and not sl > si:
            sl = spec.round(si + spec.step * 3)
        data["buyer"]["mandate"][key] = {"ideal": bi, "limit": bl, "weight": round(rng.uniform(0.02, 0.4), 3)}
        data["suppliers"][0]["envelope"]["mandate"][key] = {"ideal": si, "limit": sl,
                                                             "weight": round(rng.uniform(0.02, 0.4), 3)}
    for env in (data["buyer"], data["suppliers"][0]["envelope"]):
        env["strategy"] = {"tactic": rng.choice(["boulware", "linear", "conceder"]),
                           "beta": round(rng.uniform(0.25, 3.0), 2), "reciprocity": round(rng.uniform(0, 0.9), 2),
                           "tradeoff_sharpness": round(rng.uniform(0.1, 1.0), 2),
                           "aspiration_floor": rng.choice([0.0, 0.0, 0.0, round(rng.uniform(0.2, 0.6), 2)])}
        env["batna_terms"] = None
        env["batna_utility"] = round(rng.uniform(0.0, 0.25), 2)
    data["buyer"]["budget_cap"] = rng.choice([None, round(rng.uniform(170000, 260000), -3)])
    data["suppliers"][0]["envelope"]["unit_cost"] = rng.choice([None, round(rng.uniform(2.8, 3.8), 2)])
    data["max_rounds"] = rng.randint(2, 16)
    # mandates are biased towards overlap so agreements, mediations and no-deals are all exercised
    data["seed"] = rng.randint(1, 10_000)
    return Scenario.model_validate(data)


def assert_move_compliant(session: NegotiationSession, role: str, offer: dict[str, float]) -> None:
    arb = session.arbiter
    util = arb.utils[role]
    assert arb.rulebook.check_terms(offer) == [], (role, offer)
    assert util.violations(offer) == [], (role, offer)
    assert util.utility(offer) >= util.reservation - 1e-9, (role, offer)
    env = arb.envelopes[role]
    if role == "buyer" and env.budget_cap:
        assert offer[PRICE_KEY] * session.scenario.context.quantity <= env.budget_cap + 1e-6


@pytest.mark.parametrize("batch", range(4))
async def test_random_negotiations_never_breach_policy(batch):
    """400 random mandates/strategies/deadlines (+ red team): every committed move and every agreement is
    inside both sealed mandates, the law and the BATNA floors; runs always terminate; ledgers verify."""
    rng = random.Random(20260924 + batch)
    outcomes: dict[str, int] = {}
    for i in range(100):
        try:
            scenario = random_scenario(rng)
        except ValueError:  # random mandate inconsistent with the budget/cost floor - rejected at policy setup
            outcomes["rejected_at_setup"] = outcomes.get("rejected_at_setup", 0) + 1
            continue
        ledger = AuditLedger(f"STRESS-{batch}-{i}")
        try:
            session = NegotiationSession(scenario, red_team=rng.random() < 0.3, ledger=ledger)
        except ValueError:
            outcomes["rejected_at_setup"] = outcomes.get("rejected_at_setup", 0) + 1
            continue
        result = await session.run()
        outcomes[result.status.value] = outcomes.get(result.status.value, 0) + 1
        assert result.status in (Outcome.AGREEMENT, Outcome.MEDIATED, Outcome.NO_DEAL), result.reason
        assert result.rounds <= scenario.max_rounds
        for turn in session.turns:
            if turn.action is Action.COUNTER and turn.offer:
                assert_move_compliant(session, turn.actor, turn.offer)
        if result.agreed_terms:
            for role in ("buyer", "supplier"):
                assert_move_compliant(session, role, result.agreed_terms)
        else:
            # bilateral no-deal only happens when no mutually acceptable (grid) agreement exists
            assert session.arbiter.mediate() is None
        assert ledger.verify()["valid"]
    assert sum(v for k, v in outcomes.items() if k != "rejected_at_setup") >= 60, outcomes
    assert outcomes.get("agreement", 0) + outcomes.get("mediated_agreement", 0) > 0, outcomes


# ---------------------------------------------------------------------------------------------- deadlock detectors


def primed(scenario_id: str = "semiconductor_spot_po") -> NegotiationSession:
    return NegotiationSession(get_scenario(scenario_id))


def test_no_deadlock_before_round_three():
    s = primed()
    s.round = 2
    s.buyer.my_offers = [s.buyer.util.ideal_terms()] * 3
    s.supplier.my_offers = [s.supplier.util.ideal_terms()] * 3
    assert s._deadlock_reason() is None


def test_repetition_detector():
    s = primed()
    s.round = 3
    b, sup = s.buyer.util.ideal_terms(), s.supplier.util.ideal_terms()
    s.buyer.my_offers, s.supplier.my_offers = [b, b, b], [sup, sup, sup]
    assert s._deadlock_reason() == "Offers are repeating on both sides"


def test_floor_detector():
    s = primed("lithium_hardball")
    s.round = 5
    floor_b = s.buyer.bargaining_floor
    # offers whose own utility sits at each side's floor, far apart from each other
    b_offer = dict(s.buyer.util.ideal_terms())
    b_offer[PRICE_KEY] = s.buyer.util.at_concession(PRICE_KEY, 1.0)  # concede price fully...
    s.buyer.my_offers = [s.buyer.util.ideal_terms(), s.buyer.util.ideal_terms(), b_offer]
    s.supplier.my_offers = [s.supplier.util.ideal_terms()] * 2 + [dict(s.supplier.util.limit)]
    s.buyer.reservation_override = s.buyer.util.utility(b_offer) - 0.005  # ...so it sits at its floor
    assert s.buyer.util.utility(b_offer) <= max(floor_b, s.buyer.reservation) + 0.01
    assert s._deadlock_reason() == "Both agents are holding at their bargaining floors without converging"


def test_stalemate_detector():
    s = primed()
    s.round = 8
    b, sup = s.buyer.util.ideal_terms(), s.supplier.util.ideal_terms()
    nudged = {**b, "warranty_months": b["warranty_months"] - 1}  # tiny moves, not exact repeats
    s.buyer.my_offers = [b, b, nudged, b, nudged]
    s.supplier.my_offers = [sup, sup, {**sup, "warranty_months": sup["warranty_months"] + 1}, sup, sup]
    s.gap_history = [0.9, 0.9, 0.9, 0.9, 0.9]
    assert s._deadlock_reason().startswith("Stalemate")


async def test_deadlock_leads_to_mediation_then_contractable_terms():
    s = primed("lithium_hardball")
    result = await s.run()
    reasons = [e.data["reason"] for e in s.events if e.type == "deadlock_detected"]
    assert reasons and result.status is Outcome.MEDIATED
    assert s.arbiter.review_agreement(result.agreed_terms).status == "approved"


# ---------------------------------------------------------------------------------------------- edge cases


async def test_two_round_deadline():
    s = NegotiationSession(get_scenario("semiconductor_spot_po"), max_rounds=2)
    result = await s.run()
    assert result.rounds <= 2 and result.status in (Outcome.AGREEMENT, Outcome.MEDIATED, Outcome.NO_DEAL)
    if result.agreed_terms:
        assert_move_compliant(s, "buyer", result.agreed_terms)
        assert_move_compliant(s, "supplier", result.agreed_terms)


async def test_single_issue_negotiation():
    base = get_scenario("semiconductor_spot_po")
    data = base.model_dump(mode="json")
    data["issues"] = [i for i in data["issues"] if i["key"] == PRICE_KEY]
    data["buyer"]["mandate"] = {PRICE_KEY: {"ideal": 3.4, "limit": 4.1, "weight": 1}}
    data["buyer"]["batna_terms"] = None
    data["suppliers"][0]["envelope"]["mandate"] = {PRICE_KEY: {"ideal": 4.6, "limit": 3.7, "weight": 1}}
    result = await NegotiationSession(Scenario.model_validate(data)).run()
    assert result.agreed_terms and 3.7 <= result.agreed_terms[PRICE_KEY] <= 4.1
    assert result.efficiency["pareto_gap"] == pytest.approx(0.0, abs=1e-6)  # one issue: every deal is efficient


async def test_overlapping_ideals_collapse_to_a_point():
    base = get_scenario("semiconductor_spot_po")
    data = base.model_dump(mode="json")
    data["buyer"]["mandate"][PRICE_KEY] = {"ideal": 4.0, "limit": 4.5, "weight": 0.34}   # happy at <= 4.0
    data["buyer"]["budget_cap"] = None
    data["suppliers"][0]["envelope"]["mandate"][PRICE_KEY] = {"ideal": 3.8, "limit": 3.7, "weight": 0.3}  # >= 3.8
    sc = Scenario.model_validate(data)
    b = UtilityModel(sc.issues, sc.buyer, sc.context.quantity)
    s = UtilityModel(sc.issues, sc.suppliers[0].envelope, sc.context.quantity)
    iv = BargainingSpace(sc.issues, b, s).intervals[PRICE_KEY]
    assert iv.buyer_best == iv.supplier_best == pytest.approx(3.9)  # any price in [3.8, 4.0] satisfies both fully
    result = await NegotiationSession(sc).run()
    assert result.agreed_terms and 3.7 <= result.agreed_terms[PRICE_KEY] <= 4.5


async def test_llm_accepting_nothing_is_blocked():
    """An ACCEPT before any counterpart offer exists is a protocol error, not an agreement."""
    s = primed()
    v = await s.arbiter.review(Decision(action=Action.ACCEPT),
                               ReviewContext(role="buyer", own_turn=0, rounds_remaining=10, standing_offer=None))
    assert v.blocked and v.violations[0].rule_id == "PROTOCOL"


def test_invalid_mandate_directions_rejected_at_setup():
    base = get_scenario("semiconductor_spot_po")
    data = base.model_dump(mode="json")
    data["suppliers"][0]["envelope"]["mandate"][PRICE_KEY] = {"ideal": 3.5, "limit": 4.5, "weight": 0.3}
    with pytest.raises(ValueError):
        NegotiationSession(Scenario.model_validate(data))


def test_strategy_profile_bounds():
    with pytest.raises(ValueError):
        StrategyProfile(reciprocity=1.5)
    with pytest.raises(ValueError):
        IssueMandate(ideal=1, limit=2, weight=0)
