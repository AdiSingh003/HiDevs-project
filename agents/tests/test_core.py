"""Utility model, bargaining-space maths and strategy primitives."""

from __future__ import annotations

import pytest

from agents.core.models import Envelope, IssueMandate
from agents.core.pareto import BargainingSpace, buyer_dominates
from agents.core.strategy import ConcessionSchedule, OpponentModel, accept_decision, tradeoff_offer
from agents.core.utility import EnvelopeError, UtilityModel


class TestUtility:
    def test_ideal_scores_one_and_limit_scores_zero(self, buyer_util):
        assert buyer_util.utility(buyer_util.ideal_terms()) == pytest.approx(1.0)
        assert buyer_util.utility(dict(buyer_util.limit)) == pytest.approx(0.0)

    def test_weights_are_normalised(self, buyer_util, supplier_util):
        assert sum(buyer_util.weights.values()) == pytest.approx(1.0)
        assert sum(supplier_util.weights.values()) == pytest.approx(1.0)

    def test_budget_cap_tightens_price_ceiling(self, buyer_util):
        # mandate limit 4.20 but budget 205,000 / 50,000 units = 4.10
        assert buyer_util.limit["unit_price"] == pytest.approx(4.10)
        assert any("budget cap" in a for a in buyer_util.adjustments)

    def test_cost_plus_margin_raises_supplier_floor(self, supplier_util):
        assert supplier_util.limit["unit_price"] == pytest.approx(3.30 * 1.12)

    def test_reservation_from_batna_terms(self, buyer_util):
        batna = buyer_util.envelope.batna_terms
        assert buyer_util.reservation == pytest.approx(buyer_util.utility(batna))
        assert 0 < buyer_util.reservation < 0.3

    def test_violations_detect_breaches_on_the_limit_side_only(self, buyer_util, compliant_offer):
        assert buyer_util.violations(compliant_offer) == []
        assert buyer_util.violations({**compliant_offer, "unit_price": 4.2}) == ["unit_price"]
        # better than ideal is never a breach
        assert buyer_util.violations({**compliant_offer, "unit_price": 3.0}) == []

    def test_direction_errors_are_rejected(self, semi):
        env = semi.buyer.model_copy(deep=True)
        env.mandate["unit_price"] = IssueMandate(ideal=4.5, limit=3.5, weight=0.3)
        with pytest.raises(EnvelopeError):
            UtilityModel(semi.issues, env, semi.context.quantity)

    def test_private_numbers_include_limits_budget_and_batna(self, buyer_util):
        values = {round(p.value, 4) for p in buyer_util.private_numbers()}
        assert {4.1, 205000.0, 4.05} <= values

    def test_snap_rounds_towards_own_ideal(self, buyer_util, supplier_util):
        assert buyer_util.snap_own_favour("unit_price", 3.8777) == pytest.approx(3.87)
        assert supplier_util.snap_own_favour("unit_price", 3.8711) == pytest.approx(3.88)


class TestBargainingSpace:
    def test_frontier_is_monotone_trade_off(self, arbiter):
        pts = arbiter.space.frontier(4)
        assert len(pts) > 2
        for a, b in zip(pts, pts[1:]):
            assert b.u_buyer <= a.u_buyer + 1e-9
            assert b.u_supplier >= a.u_supplier - 1e-9

    def test_nash_point_is_mutually_acceptable_and_efficient(self, arbiter):
        nash = arbiter.space.nash_point()
        assert nash is not None
        b, s = arbiter.utils["buyer"], arbiter.utils["supplier"]
        assert b.acceptable(nash.terms) and s.acceptable(nash.terms)
        gap = arbiter.space.efficiency(nash.terms)["pareto_gap"]
        assert gap is not None and gap < 0.02

    def test_nash_beats_random_feasible_points_on_nash_product(self, arbiter):
        space = arbiter.space
        nash = space.nash_point(round_terms=False)
        best = (nash.u_buyer - space.d_buyer) * (nash.u_supplier - space.d_supplier)
        for p in space.frontier(10):
            if p.u_buyer >= space.d_buyer and p.u_supplier >= space.d_supplier:
                assert (p.u_buyer - space.d_buyer) * (p.u_supplier - space.d_supplier) <= best + 1e-9

    def test_no_zopa_when_hard_limits_do_not_overlap(self, semi):
        env = semi.buyer.model_copy(deep=True)
        env.mandate["unit_price"] = IssueMandate(ideal=3.2, limit=3.5, weight=0.34)
        env.budget_cap = None
        b = UtilityModel(semi.issues, env, semi.context.quantity)
        s = UtilityModel(semi.issues, semi.suppliers[0].envelope, semi.context.quantity)
        space = BargainingSpace(semi.issues, b, s)
        assert space.infeasible_issues == ["unit_price"]
        assert space.nash_point() is None and not space.zopa_exists()

    def test_buyer_dominance(self, semi, compliant_offer):
        better = {**compliant_offer, "unit_price": 3.8, "delivery_days": 20}
        assert buyer_dominates(better, compliant_offer, semi.issues)
        assert not buyer_dominates(compliant_offer, better, semi.issues)
        mixed = {**better, "warranty_months": 12}
        assert not buyer_dominates(mixed, compliant_offer, semi.issues)


class TestStrategy:
    @pytest.mark.parametrize("beta", [0.35, 1.0, 2.5])
    def test_schedule_runs_from_opening_to_reservation(self, beta):
        sch = ConcessionSchedule(opening=1.0, reservation=0.2, beta=beta, turns=10)
        targets = [sch.target(k) for k in range(10)]
        assert targets[0] == pytest.approx(1.0)
        assert targets[-1] == pytest.approx(0.2)
        assert all(a >= b - 1e-12 for a, b in zip(targets, targets[1:]))

    def test_boulware_holds_firmer_than_conceder(self):
        boulware = ConcessionSchedule(1.0, 0.2, 0.35, 10).target(5)
        linear = ConcessionSchedule(1.0, 0.2, 1.0, 10).target(5)
        conceder = ConcessionSchedule(1.0, 0.2, 2.5, 10).target(5)
        assert boulware > linear > conceder

    def test_tradeoff_offer_meets_target_and_stays_in_mandate(self, buyer_util):
        model = OpponentModel(buyer_util)
        for target in (0.95, 0.8, 0.6, 0.4, 0.2):
            offer = tradeoff_offer(buyer_util, target, model, sharpness=0.2)
            assert buyer_util.utility(offer) >= target - 1e-9
            assert buyer_util.violations(offer) == []

    def test_tradeoff_concedes_first_where_opponent_cares(self, buyer_util):
        model = OpponentModel(buyer_util)
        opp = {"unit_price": 4.6, "delivery_days": 42, "payment_terms_days": 15, "sla_on_time_pct": 92.0,
               "late_penalty_pct_per_day": 0.1, "penalty_cap_pct": 3, "warranty_months": 12}
        model.observe(opp)
        for _ in range(3):  # opponent never moves on payment terms -> looks important to them
            opp = {**opp, "unit_price": opp["unit_price"] - 0.1, "delivery_days": opp["delivery_days"] - 3}
            model.observe(opp)
        assert max(model.weights, key=model.weights.get) == "payment_terms_days"
        offer = tradeoff_offer(buyer_util, 0.85, model, sharpness=0.2)
        conceded = {k: buyer_util.concession_of(k, offer[k]) for k in offer}
        assert conceded["payment_terms_days"] > conceded["unit_price"]

    def test_never_concedes_beyond_opponent_demand(self, buyer_util):
        model = OpponentModel(buyer_util)
        model.observe({"unit_price": 4.0, "delivery_days": 30, "payment_terms_days": 30, "sla_on_time_pct": 96.0,
                       "late_penalty_pct_per_day": 0.5, "penalty_cap_pct": 8, "warranty_months": 18})
        offer = tradeoff_offer(buyer_util, 0.0, model, sharpness=0.2)
        assert offer["payment_terms_days"] >= 30  # they only asked for 30

    def test_acceptance_rules(self, buyer_util, compliant_offer):
        u = buyer_util.utility(compliant_offer)
        assert accept_decision(buyer_util, compliant_offer, u - 0.01, buyer_util.reservation, False)[0]
        assert not accept_decision(buyer_util, compliant_offer, u + 0.1, buyer_util.reservation, False)[0]
        assert accept_decision(buyer_util, compliant_offer, u + 0.1, buyer_util.reservation, True)[0]  # AC_time
        over = {**compliant_offer, "unit_price": 4.2}
        ok, why = accept_decision(buyer_util, over, 0.0, 0.0, True)
        assert not ok and "outside mandate" in why


def test_envelope_requires_distinct_ideal_and_limit():
    with pytest.raises(ValueError):
        IssueMandate(ideal=1, limit=1, weight=0.5)
    Envelope(role="buyer", mandate={"x": IssueMandate(ideal=1, limit=2, weight=1)})
