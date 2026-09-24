"""Bounded negotiator agent = deterministic policy engine + optional LLM brain + voice.

The policy engine (concession schedule, tit-for-tat, opponent model, trade-off offers, acceptance
criteria) always computes a compliant recommended move. An LLM brain may deviate from it, but the
Legal Arbiter validates the result and the agent falls back to the recommendation when blocked.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Action, DealContext, Decision, Envelope, IssueSpec, Party, Role, Terms, TurnRecord
from ..core.strategy import OpponentModel, accept_decision, tradeoff_offer
from ..core.utility import UtilityModel
from ..guardrails.arbiter import schedule_for
from .llm import BRIEF_RULES, RESPONSE_SCHEMA, NegotiatorBrain
from .voice import LocalVoice


@dataclass
class TurnContext:
    round: int
    own_turn: int
    max_rounds: int
    counterpart_offer: Terms | None
    counterpart_message: str
    final_turn: bool
    history: list[TurnRecord] = field(default_factory=list)
    competition: bool = False


class NegotiatorAgent:
    def __init__(self, role: Role, party: Party, counterparty: Party, context: DealContext,
                 issues: list[IssueSpec], envelope: Envelope, max_rounds: int, rng: random.Random,
                 brain: NegotiatorBrain | None = None):
        self.role = role
        self.party = party
        self.counterparty = counterparty
        self.context = context
        self.issues = issues
        self.util = UtilityModel(issues, envelope, context.quantity)
        self.strategy = envelope.strategy
        self.schedule = schedule_for(self.util, max_rounds)
        self.model = OpponentModel(self.util)
        self.voice = LocalVoice(role, party.name, counterparty.name, issues, context, self.util.weights, rng)
        self.brain = brain
        self.my_offers: list[Terms] = []
        self.turns_taken = 0
        self.reservation_override: float | None = None
        self.last_plan: Decision | None = None
        self.targets: list[float] = []

    # ------------------------------------------------------------------ state

    @property
    def reservation(self) -> float:
        base = self.util.reservation
        return base if self.reservation_override is None else max(base, self.reservation_override)

    def observe(self, offer: Terms) -> None:
        self.model.observe(offer)

    def commit(self, decision: Decision) -> None:
        if decision.action is Action.COUNTER and decision.offer is not None:
            self.my_offers.append(dict(decision.offer))
        if decision.target_utility is not None:
            self.targets.append(decision.target_utility)
        self.turns_taken += 1

    # ------------------------------------------------------------------ policy engine

    def target(self, k: int) -> float:
        t_time = max(self.schedule.target(k), self.reservation)
        tgt = t_time
        opp = self.model.offers
        if len(opp) >= 2 and self.my_offers:
            delta = self.util.utility(opp[-1]) - self.util.utility(opp[-2])
            tft = self.util.utility(self.my_offers[-1]) - max(0.0, delta)
            r = self.strategy.reciprocity
            tgt = max(t_time, (1 - r) * t_time + r * tft)
        return min(1.0, max(tgt, self.reservation, self.strategy.aspiration_floor))

    @property
    def bargaining_floor(self) -> float:
        return max(self.reservation, self.strategy.aspiration_floor)

    def plan(self, ctx: TurnContext) -> Decision:
        target = self.target(ctx.own_turn)
        if ctx.counterpart_offer is not None:
            ok, why = accept_decision(self.util, ctx.counterpart_offer, target, self.reservation, ctx.final_turn)
            if ok:
                return Decision(action=Action.ACCEPT, target_utility=target, source="engine",
                                message=self.voice.compose(Action.ACCEPT, None, None, ctx.counterpart_offer, []),
                                rationale={"acceptance": why, "offer_utility": self.util.utility(ctx.counterpart_offer)})
        prev = self.my_offers[-1] if self.my_offers else None
        allow_retraction = prev is not None and target > self.util.utility(prev) + 1e-6
        offer = tradeoff_offer(self.util, target, self.model, self.strategy.tradeoff_sharpness,
                               previous=prev, allow_retraction=allow_retraction)
        conceded = []
        if prev is not None:
            moves = {k: self.util.concession_of(k, offer[k]) - self.util.concession_of(k, prev[k]) for k in offer}
            conceded = [k for k, d in sorted(moves.items(), key=lambda kv: -kv[1]) if d > 1e-9]
        message = self.voice.compose(Action.COUNTER, offer, prev, ctx.counterpart_offer, conceded,
                                     competition=ctx.competition, final_turn=ctx.final_turn)
        return Decision(action=Action.COUNTER, offer=offer, message=message, target_utility=target, source="engine",
                        rationale={"target_utility": round(target, 4), "offer_utility": round(self.util.utility(offer), 4),
                                   "time_target": round(self.schedule.target(ctx.own_turn), 4),
                                   "opponent_weights": {k: round(v, 3) for k, v in self.model.weights.items()},
                                   "conceded": conceded})

    # ------------------------------------------------------------------ LLM layer

    def brief(self, ctx: TurnContext, plan: Decision) -> dict[str, Any]:
        c = self.context
        return {
            "your_role": self.role,
            "your_organisation": self.party.name,
            "counterparty": self.counterparty.name,
            "round": ctx.round,
            "max_rounds": ctx.max_rounds,
            "rfq": {"reference": c.reference, "item": c.item, "quantity": c.quantity, "unit": c.quantity_unit,
                    "currency": c.currency, "incoterm": c.incoterm, "jurisdiction": c.jurisdiction},
            "issues": [{"key": s.key, "label": s.label, "unit": s.unit,
                        "you_prefer": s.prefers(self.role).value} for s in self.issues],
            "your_previous_offer": self.my_offers[-1] if self.my_offers else None,
            "counterparty_offer": ctx.counterpart_offer,
            "counterparty_message": ctx.counterpart_message,
            "recommended_move": {"action": plan.action.value, "offer": plan.offer},
            "rules": BRIEF_RULES,
            "response_schema": RESPONSE_SCHEMA,
        }

    async def decide(self, ctx: TurnContext) -> Decision:
        plan = self.plan(ctx)
        self.last_plan = plan
        if self.brain is None:
            return plan
        proposal = await self.brain.propose(self.brief(ctx, plan))
        if proposal is None:
            return plan.model_copy(update={"rationale": {**plan.rationale, "llm": "unavailable - engine move used"}})
        action = Action(proposal["action"])
        base = plan.offer or (self.my_offers[-1] if self.my_offers else self.util.ideal_terms())
        offer = {**base, **proposal["offer"]} if action is Action.COUNTER else None
        return Decision(action=action, offer=offer, message=proposal["message"] or plan.message, source="llm",
                        target_utility=plan.target_utility,
                        rationale={**plan.rationale, "recommended_action": plan.action.value,
                                   "recommended_offer": plan.offer, "llm_latency_ms": proposal.get("latency_ms")})

    def fallback(self) -> Decision:
        """The compliant engine recommendation, used when the arbiter blocks a proposed move."""
        assert self.last_plan is not None
        return self.last_plan.model_copy(update={"source": "fallback"})

    # ------------------------------------------------------------------ mediation

    def evaluate_mediation(self, terms: Terms) -> tuple[bool, str]:
        if self.util.violations(terms):
            return False, "outside mandate"
        u = self.util.utility(terms)
        if u < self.reservation - 1e-9:
            return False, "worse than our best alternative"
        return True, "beats our best alternative"
