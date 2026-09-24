"""Alternating-offers negotiation engine (Rubinstein protocol) with a Legal Arbiter in the loop.

Protocol per round: the supplier moves (quote / counter / accept), then the buyer. Every move is
reviewed by the arbiter *before* the counterpart sees it; blocked moves are replaced by the agent's
compliant engine recommendation. After each round the engine measures convergence and detects
deadlock (both at floor, stalemate, or repetition); deadlocks and the deadline trigger mediation by
the arbiter using the Nash bargaining solution over both sealed envelopes.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import uuid
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..audit.ledger import AuditLedger
from ..core.models import (Action, Decision, Envelope, EventVisibility, NegotiationEvent, Outcome, Role, Scenario,
                           Terms, TurnRecord, Verdict, other_role, utcnow)
from ..core.utility import PRICE_KEY
from ..guardrails.arbiter import LegalArbiter, ReviewContext
from ..lyzr.rai import SafeAIGateway
from .llm import NegotiatorBrain
from .negotiator import NegotiatorAgent, TurnContext
from .redteam import RedTeamInjector

Listener = Callable[[NegotiationEvent], Any]


class NegotiationResult(BaseModel):
    negotiation_id: str
    scenario_id: str
    supplier_id: str
    status: Outcome
    reason: str = ""
    rounds: int = 0
    turns: list[TurnRecord] = Field(default_factory=list)
    agreed_terms: Terms | None = None
    accepted_by: str | None = None
    u_buyer: float | None = None
    u_supplier: float | None = None
    efficiency: dict[str, Any] = Field(default_factory=dict)
    mediated: bool = False
    interventions: int = 0
    blocked_moves: int = 0
    redactions: int = 0
    requires_cfo_approval: bool = False
    started_at: str = ""
    finished_at: str = ""


class NegotiationSession:
    def __init__(self, scenario: Scenario, *, supplier_id: str | None = None, negotiation_id: str | None = None,
                 max_rounds: int | None = None, brains: dict[str, NegotiatorBrain] | None = None,
                 red_team: bool = False, gateway: SafeAIGateway | None = None, listener: Listener | None = None,
                 ledger: AuditLedger | None = None, seed: int | None = None, speed_ms: int = 0,
                 rfq_mode: bool = False, buyer_envelope: Envelope | None = None,
                 supplier_envelope: Envelope | None = None, llm_mode: str = "offline"):
        self.scenario = scenario
        self.supplier_profile = scenario.supplier(supplier_id)
        self.id = negotiation_id or f"NEG-{uuid.uuid4().hex[:10].upper()}"
        self.max_rounds = max_rounds or scenario.max_rounds
        self.rfq_mode = rfq_mode
        self.listener = listener
        self.ledger = ledger
        self.speed_ms = speed_ms
        self.llm_mode = llm_mode
        self.rng = random.Random(scenario.seed if seed is None else seed)
        self.arbiter = LegalArbiter(scenario, self.supplier_profile, self.max_rounds, gateway,
                                    buyer_envelope=buyer_envelope, supplier_envelope=supplier_envelope)
        brains = brains or {}
        ctx = scenario.context
        self.agents: dict[str, NegotiatorAgent] = {
            "buyer": NegotiatorAgent("buyer", ctx.buyer, self.supplier_profile.party, ctx, scenario.issues,
                                     self.arbiter.envelopes["buyer"], self.max_rounds, random.Random(self.rng.random()),
                                     brains.get("buyer")),
            "supplier": NegotiatorAgent("supplier", self.supplier_profile.party, ctx.buyer, ctx, scenario.issues,
                                        self.arbiter.envelopes["supplier"], self.max_rounds,
                                        random.Random(self.rng.random()), brains.get("supplier")),
        }
        self.injector = RedTeamInjector() if red_team else None
        self.status = Outcome.RUNNING
        self.reason = ""
        self.round = 1
        self.turns: list[TurnRecord] = []
        self.events: list[NegotiationEvent] = []
        self.last_message: dict[str, str] = {}
        self.gap_history: list[float] = []
        self.agreed_terms: Terms | None = None
        self.accepted_by: str | None = None
        self.mediated = False
        self.mediations = 0
        self.stats = {"interventions": 0, "blocked": 0, "redactions": 0}
        self.started_at = ""
        self.finished_at = ""
        self._requires_cfo = False
        self._public_base = self.arbiter.public_numbers()
        self._started = False

    # ------------------------------------------------------------------ plumbing

    @property
    def buyer(self) -> NegotiatorAgent:
        return self.agents["buyer"]

    @property
    def supplier(self) -> NegotiatorAgent:
        return self.agents["supplier"]

    @property
    def running(self) -> bool:
        return self.status is Outcome.RUNNING

    def emit(self, type_: str, data: dict[str, Any], visibility: list[str] | None = None,
             actor: str = "arbiter") -> NegotiationEvent:
        vis = [EventVisibility(v) for v in (visibility or ["public"])]
        ev = NegotiationEvent(seq=len(self.events) + 1, type=type_, visibility=vis,
                              data={"negotiation_id": self.id, "supplier_id": self.supplier_profile.id, **data})
        self.events.append(ev)
        if self.ledger is not None:
            self.ledger.append(type_, actor, ev.data, [v.value for v in vis])
        if self.listener is not None:
            res = self.listener(ev)
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)
        return ev

    def _public_numbers(self, extra: list[Terms | None]) -> list[float]:
        nums = list(self._public_base)
        q = self.scenario.context.quantity
        for offer in [*(t.offer for t in self.turns), *extra]:
            if offer:
                nums += list(offer.values())
                if PRICE_KEY in offer:
                    nums.append(offer[PRICE_KEY] * q)
        return nums

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.started_at = utcnow()
        sc, ctx = self.scenario, self.scenario.context
        self.emit("negotiation_started", {
            "scenario_id": sc.id, "title": sc.title, "rfq": ctx.model_dump(mode="json"),
            "supplier": self.supplier_profile.party.model_dump(mode="json"),
            "issues": [s.model_dump(mode="json") for s in sc.issues], "max_rounds": self.max_rounds,
            "rfq_mode": self.rfq_mode, "llm_mode": self.llm_mode, "red_team": self.injector is not None,
            "rulebook": {"version": self.arbiter.rulebook.version, "jurisdiction": self.arbiter.rulebook.jurisdiction_name,
                         "rules": self.arbiter.rulebook.rule_ids()},
            "rules_catalogue": self.arbiter.rules_catalogue,
        })
        self.emit("envelopes_sealed", {"commitments": self.arbiter.commitments(),
                                       "note": "Envelopes are sealed; only hash commitments are public."})
        for role, agent in self.agents.items():
            findings = [f.message for f in self.arbiter.setup_findings[role]] + agent.util.adjustments
            self.emit("policy_setup", {
                "role": role, "reservation_utility": round(agent.util.reservation, 4),
                "tactic": agent.strategy.tactic, "beta": agent.strategy.effective_beta,
                "reciprocity": agent.strategy.reciprocity, "adjustments": findings,
                "brain": getattr(agent.brain, "name", "policy-engine"),
            }, visibility=[role, "arbiter"], actor=role)
        space = self.arbiter.space
        nash = space.nash_point()
        self.emit("bargaining_analysis", {
            "zopa_exists": nash is not None, "infeasible_issues": space.infeasible_issues,
            "reservations": {"buyer": round(space.d_buyer, 4), "supplier": round(space.d_supplier, 4)},
            "frontier": [{"u_buyer": round(p.u_buyer, 4), "u_supplier": round(p.u_supplier, 4)} for p in space.frontier(6)],
            "nash": None if nash is None else {"u_buyer": round(nash.u_buyer, 4), "u_supplier": round(nash.u_supplier, 4),
                                               "terms": nash.terms},
            # true issue weights - arbiter-only, lets the dashboard score each agent's opponent model
            "weights": {r: {k: round(w, 4) for k, w in a.util.weights.items()} for r, a in self.agents.items()},
        }, visibility=["arbiter"])

    async def run(self) -> NegotiationResult:
        await self.start()
        while self.running:
            await self.step_round()
        if self.ledger is not None:
            self.ledger.request_anchor("negotiation_finished")  # background; callers flush when needed
        return self.result()

    async def step_round(self) -> None:
        if not self.running:
            return
        for role in ("supplier", "buyer"):
            await self.take_turn(role)  # type: ignore[arg-type]
            if not self.running:
                return
        if self.buyer.my_offers and self.supplier.my_offers:
            gap = self.arbiter.space.normalised_gap(self.buyer.my_offers[-1], self.supplier.my_offers[-1])
            self.gap_history.append(gap)
            self.emit("round_summary", {"round": self.round, "gap": round(gap, 4),
                                        "gap_history": [round(g, 4) for g in self.gap_history]}, visibility=["arbiter"])
        reason = self._deadlock_reason()
        if reason and self.mediations == 0:
            if await self._mediate(reason):
                return
            if not self.running:
                return
        if self.round >= self.max_rounds:
            if not await self._mediate("Deadline reached without a bilateral agreement"):
                if self.running:
                    self._end(Outcome.NO_DEAL, "Deadline reached; no mutually acceptable agreement - both parties "
                                               "revert to their BATNA")
            return
        self.round += 1

    # ------------------------------------------------------------------ a single move

    async def take_turn(self, role: Role) -> None:
        agent, other = self.agents[role], self.agents[other_role(role)]
        k = agent.turns_taken
        standing = other.my_offers[-1] if other.my_offers else None
        ctx = TurnContext(round=self.round, own_turn=k, max_rounds=self.max_rounds, counterpart_offer=standing,
                          counterpart_message=self.last_message.get(other.role, ""),
                          final_turn=self.round >= self.max_rounds, history=self.turns,
                          competition=role == "buyer" and agent.reservation_override is not None)
        decision = await agent.decide(ctx)
        if self.injector is not None:
            decision = self.injector.apply(agent, ctx, decision)
        rctx = ReviewContext(role=role, own_turn=k, rounds_remaining=self.max_rounds - self.round,
                             standing_offer=standing, session_id=f"{self.id}-{role}",
                             public_numbers=self._public_numbers([decision.offer, standing]))
        verdict = await self.arbiter.review(decision, rctx)
        interventions = 0
        if verdict.blocked:
            interventions += 1
            self._emit_block(role, decision, verdict)
            decision = agent.fallback()
            rctx.public_numbers = self._public_numbers([decision.offer, standing])
            verdict = await self.arbiter.review(decision, rctx)
            if verdict.blocked and agent.my_offers:  # defensive: re-table the last compliant offer
                self._emit_block(role, decision, verdict)
                decision = Decision(action=Action.COUNTER, offer=dict(agent.my_offers[-1]), source="fallback",
                                    message="We maintain our previous proposal.", target_utility=decision.target_utility)
                verdict = await self.arbiter.review(decision, rctx)
            if verdict.blocked:
                self._end(Outcome.FAILED, f"{role} agent could not produce a compliant move")
                return
        if verdict.redactions:
            self.stats["redactions"] += len(verdict.redactions)
            interventions += 1
            self.emit("message_redacted", {
                "round": self.round, "actor": role,
                "rules": sorted({r.rule_id for r in verdict.redactions}),
                "public_note": "Legal Arbiter sanitised this message before delivery",
            }, visibility=["public"], actor="arbiter")
            self.emit("message_redacted_detail", {
                "round": self.round, "actor": role,
                "redactions": [r.model_dump() for r in verdict.redactions],
            }, visibility=[role, "arbiter"], actor="arbiter")
        self.stats["interventions"] += interventions

        agent.commit(decision)
        if decision.action is Action.COUNTER and decision.offer is not None:
            other.observe(decision.offer)
        self.last_message[role] = verdict.sanitized_message
        terms_for_analytics = decision.offer if decision.action is Action.COUNTER else standing
        ub = us = gap = None
        if terms_for_analytics is not None:
            ub, us = self.arbiter.utilities(terms_for_analytics)
        if decision.action is Action.COUNTER and standing is not None and decision.offer is not None:
            gap = self.arbiter.space.normalised_gap(decision.offer, standing)
        record = TurnRecord(index=len(self.turns) + 1, round=self.round, actor=role, action=decision.action,
                            offer=decision.offer if decision.action is Action.COUNTER else None,
                            message=verdict.sanitized_message, source=decision.source, verdict_status=verdict.status,
                            public_violations=verdict.public_violations(), interventions=interventions,
                            u_buyer=None if ub is None else round(ub, 4),
                            u_supplier=None if us is None else round(us, 4),
                            gap=None if gap is None else round(gap, 4))
        self.turns.append(record)
        public = record.model_dump(mode="json", exclude={"u_buyer", "u_supplier", "gap"})
        self.emit("turn", public, visibility=["public"], actor=role)
        self.emit("turn_private", {
            "index": record.index, "round": self.round, "actor": role,
            "target_utility": None if decision.target_utility is None else round(decision.target_utility, 4),
            "own_utility": None if terms_for_analytics is None else round(agent.util.utility(terms_for_analytics), 4),
            "reservation_utility": round(agent.reservation, 4),
            "rationale": decision.rationale,
            "safe_ai": verdict.external or None,  # Lyzr OPA guardrail + RAI verdicts on the delivered move
        }, visibility=[role, "arbiter"], actor=role)
        self.emit("analytics", {"index": record.index, "round": self.round, "actor": role,
                                "u_buyer": record.u_buyer, "u_supplier": record.u_supplier, "gap": record.gap,
                                "offer": decision.offer if decision.action is Action.COUNTER else standing},
                  visibility=["arbiter"])

        if decision.action is Action.ACCEPT and standing is not None:
            await self._agree(standing, Outcome.AGREEMENT, accepted_by=role)
        elif decision.action is Action.WALK_AWAY:
            self._end(Outcome.NO_DEAL, f"{agent.party.name} walked away")
        if self.speed_ms:
            await asyncio.sleep(self.speed_ms / 1000)

    def _emit_block(self, role: Role, decision: Decision, verdict: Verdict) -> None:
        self.stats["blocked"] += 1
        public = verdict.public_violations()
        self.emit("guardrail_block", {
            "round": self.round, "actor": role, "attempted_action": decision.action.value,
            "public_violations": [v.model_dump() for v in public if v.severity == "block"],
            "note": "Legal Arbiter blocked a non-compliant move; the agent re-planned within its mandate.",
        }, visibility=["public"], actor="arbiter")
        self.emit("guardrail_block_detail", {
            "round": self.round, "actor": role, "source": decision.source,
            "attempted": {"action": decision.action.value, "offer": decision.offer, "message": decision.message},
            "violations": [v.model_dump() for v in verdict.violations if v.severity == "block"],
            "external": verdict.external,
        }, visibility=[role, "arbiter"], actor="arbiter")

    # ------------------------------------------------------------------ deadlock & mediation

    def _deadlock_reason(self) -> str | None:
        b, s = self.buyer.my_offers, self.supplier.my_offers
        if self.round < 3 or len(b) < 3 or len(s) < 3:
            return None
        ub, us = self.buyer.util, self.supplier.util
        if (ub.utility(b[-1]) <= self.buyer.bargaining_floor + 0.01
                and us.utility(s[-1]) <= self.supplier.bargaining_floor + 0.01):
            return "Both agents are holding at their bargaining floors without converging"
        w = 3
        if len(self.gap_history) > w and self.round >= max(4, self.max_rounds // 2) and len(b) > w and len(s) > w:
            gap_drop = self.gap_history[-w - 1] - self.gap_history[-1]
            b_conc = ub.utility(b[-w - 1]) - ub.utility(b[-1])
            s_conc = us.utility(s[-w - 1]) - us.utility(s[-1])
            if gap_drop < 0.02 * max(self.gap_history[0], 1e-6) and b_conc < 0.015 and s_conc < 0.015:
                return f"Stalemate: positions moved less than 2% over the last {w} rounds"
        if b[-1] == b[-2] == b[-3] and s[-1] == s[-2] == s[-3]:
            return "Offers are repeating on both sides"
        return None

    async def _mediate(self, reason: str) -> bool:
        self.mediations += 1
        self.emit("deadlock_detected", {"round": self.round, "reason": reason,
                                        "action": "Legal Arbiter invoked as mediator"}, visibility=["public"])
        point = self.arbiter.mediate(buyer_reservation=self.buyer.reservation)
        if point is None:
            space = self.arbiter.space
            outbid = self.buyer.reservation_override is not None and space.zopa_exists()
            if outbid:
                public_reason = "The buyer holds a better competing quote than any outcome available in this lane"
                end_reason = "Lane closed: competing quotes beat anything this supplier can offer"
                detail = f"buyer outside option {self.buyer.reservation:.3f} exceeds this lane's Nash frontier"
            else:
                public_reason = "No zone of possible agreement exists under the current mandates"
                end_reason = "No ZOPA under current mandates - escalate to human approvers to revisit limits"
                detail = (f"incompatible hard limits on {', '.join(space.infeasible_issues)}" if space.infeasible_issues
                          else "no outcome satisfies both parties' BATNA floors")
            self.emit("mediation_failed", {"round": self.round, "reason": public_reason}, visibility=["public"])
            self.emit("mediation_failed_detail", {"detail": detail}, visibility=["arbiter"])
            self._end(Outcome.NO_DEAL, end_reason)
            return False
        terms = point.terms
        self.emit("mediation_proposal", {"round": self.round, "terms": terms, "method": "Nash bargaining solution",
                                         "message": "Single-text proposal from the Legal Arbiter maximising the joint "
                                                    "surplus over both parties' walk-away positions."},
                  visibility=["public"])
        self.emit("mediation_analysis", {"u_buyer": round(point.u_buyer, 4), "u_supplier": round(point.u_supplier, 4)},
                  visibility=["arbiter"])
        verdicts = {}
        for role in ("supplier", "buyer"):
            agent = self.agents[role]
            ok, why = agent.evaluate_mediation(terms)
            verdicts[role] = ok
            self.emit("mediation_response", {"round": self.round, "actor": role, "accepted": ok,
                                             "message": agent.voice.mediation_reply(ok)}, visibility=["public"], actor=role)
            self.emit("mediation_response_detail", {"actor": role, "why": why}, visibility=[role, "arbiter"], actor=role)
        if all(verdicts.values()):
            await self._agree(terms, Outcome.MEDIATED, accepted_by="both (mediated)")
            return self.status in (Outcome.AGREEMENT, Outcome.MEDIATED)
        return False

    # ------------------------------------------------------------------ endings

    async def _agree(self, terms: Terms, outcome: Outcome, accepted_by: str) -> None:
        verdict = self.arbiter.review_agreement(terms)
        if verdict.blocked:
            self.emit("agreement_rejected", {"violations": [v.model_dump() for v in verdict.violations]},
                      visibility=["arbiter"])
            self._end(Outcome.FAILED, "Final compliance review rejected the agreement")
            return
        self.agreed_terms = dict(terms)
        self.accepted_by = accepted_by
        self.mediated = outcome is Outcome.MEDIATED
        ub, us = self.arbiter.utilities(terms)
        cfo = any(v.rule_id == "POLICY-APPROVAL" for v in verdict.violations)
        self.emit("agreement", {"round": self.round, "terms": terms, "outcome": outcome.value, "accepted_by": accepted_by,
                                "rfq_mode": self.rfq_mode}, visibility=["public"])
        if cfo:
            self.emit("approval_required", {"reason": "Contract value exceeds the agent's delegated authority",
                                            "approver": "Buyer CFO"}, visibility=["buyer", "arbiter"])
        self.emit("agreement_analysis", {"u_buyer": round(ub, 4), "u_supplier": round(us, 4),
                                         **self._efficiency(terms)}, visibility=["arbiter"])
        self._end(outcome, "Agreement reached" if outcome is Outcome.AGREEMENT else "Agreement reached via mediation",
                  requires_cfo=cfo)

    def _efficiency(self, terms: Terms) -> dict[str, Any]:
        space = self.arbiter.space
        eff = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in space.efficiency(terms).items()}
        b0 = next((t.offer for t in self.turns if t.actor == "buyer" and t.offer), None)
        s0 = next((t.offer for t in self.turns if t.actor == "supplier" and t.offer), None)
        if b0 and s0:
            split = {k: self.scenario.issue(k).round((b0[k] + s0[k]) / 2) for k in terms}
            sb, ss = self.arbiter.utilities(split)
            eff["split_difference"] = {"terms": split, "u_buyer": round(sb, 4), "u_supplier": round(ss, 4),
                                       "acceptable_to_both": self.arbiter.utils["buyer"].acceptable(split)
                                       and self.arbiter.utils["supplier"].acceptable(split)}
            eff["joint_gain_vs_split"] = round(eff["joint_utility"] - (sb + ss), 4)
        return eff

    def _end(self, outcome: Outcome, reason: str, requires_cfo: bool = False) -> None:
        if not self.running and outcome is not self.status:
            return
        self.status = outcome
        self.reason = reason
        self.finished_at = utcnow()
        self._requires_cfo = requires_cfo
        self.emit("negotiation_finished", {"status": outcome.value, "reason": reason, "rounds": self.round,
                                           "turns": len(self.turns), "stats": dict(self.stats)}, visibility=["public"])

    def result(self) -> NegotiationResult:
        ub = us = None
        eff: dict[str, Any] = {}
        if self.agreed_terms:
            ub, us = self.arbiter.utilities(self.agreed_terms)
            eff = self._efficiency(self.agreed_terms)
        return NegotiationResult(
            negotiation_id=self.id, scenario_id=self.scenario.id, supplier_id=self.supplier_profile.id,
            status=self.status, reason=self.reason, rounds=self.round, turns=self.turns,
            agreed_terms=self.agreed_terms, accepted_by=self.accepted_by,
            u_buyer=None if ub is None else round(ub, 4), u_supplier=None if us is None else round(us, 4),
            efficiency=eff, mediated=self.mediated, interventions=self.stats["interventions"],
            blocked_moves=self.stats["blocked"], redactions=self.stats["redactions"],
            requires_cfo_approval=self._requires_cfo,
            started_at=self.started_at, finished_at=self.finished_at,
        )

    def events_for(self, view: str) -> list[NegotiationEvent]:
        return [e for e in self.events if e.visible_to(view)]
