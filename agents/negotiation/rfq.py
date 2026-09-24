"""Multi-vendor RFQ: one buyer negotiates with N suppliers in lockstep, then awards a Pareto-efficient deal.

Competitive dynamics: after every round the buyer's live outside option in each lane becomes the best
*acceptable* standing quote from the other lanes (minus a switching margin). This raises the buyer's
reservation in weaker lanes - exactly the leverage a real sourcing event creates - without ever
revealing competitors' numbers. Deals agreed in RFQ lanes are binding best-and-final quotes; the
award goes to the Pareto-efficient quote (buyer-side dominance over every term) with the highest
buyer utility, and the contract is compiled only for the winner.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..audit.ledger import AuditLedger
from ..core.models import EventVisibility, NegotiationEvent, Outcome, Scenario, Terms, utcnow
from ..core.pareto import buyer_dominates
from ..core.utility import PRICE_KEY, UtilityModel
from ..lyzr.rai import SafeAIGateway
from .engine import NegotiationResult, NegotiationSession
from .llm import NegotiatorBrain

Listener = Callable[[NegotiationEvent], Any]


class RFQCandidate(BaseModel):
    supplier_id: str
    supplier_name: str
    negotiation_id: str
    status: Outcome
    terms: Terms | None = None
    u_buyer: float | None = None
    u_supplier: float | None = None
    total_value: float | None = None
    pareto_efficient: bool = False
    dominated_by: list[str] = Field(default_factory=list)
    rank: int | None = None
    note: str = ""


class RFQResult(BaseModel):
    rfq_id: str
    scenario_id: str
    winner: str | None = None
    winner_negotiation_id: str | None = None
    rationale: str = ""
    candidates: list[RFQCandidate] = Field(default_factory=list)
    negotiations: dict[str, NegotiationResult] = Field(default_factory=dict)
    leverage_log: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


class RFQOrchestrator:
    def __init__(self, scenario: Scenario, *, rfq_id: str | None = None,
                 brains_factory: Callable[[str], dict[str, NegotiatorBrain]] | None = None,
                 red_team: bool = False, gateway: SafeAIGateway | None = None, listener: Listener | None = None,
                 ledger_factory: Callable[[str], AuditLedger | None] | None = None, speed_ms: int = 0,
                 leverage: bool = True, switching_margin: float = 0.03, llm_mode: str = "offline"):
        if scenario.mode != "rfq":
            raise ValueError("RFQOrchestrator needs an rfq-mode scenario")
        self.scenario = scenario
        self.id = rfq_id or f"RFQ-{uuid.uuid4().hex[:8].upper()}"
        self.listener = listener
        self.speed_ms = speed_ms
        self.leverage = leverage
        self.switching_margin = switching_margin
        self.ledger = ledger_factory(self.id) if ledger_factory else None
        self.events: list[NegotiationEvent] = []
        # One reference utility (the buyer's true preferences) to compare quotes across lanes.
        self.reference = UtilityModel(scenario.issues, scenario.buyer, scenario.context.quantity)
        self.sessions: dict[str, NegotiationSession] = {}
        for sup in scenario.suppliers:
            nid = f"{self.id}-{sup.id.upper()}"
            self.sessions[sup.id] = NegotiationSession(
                scenario, supplier_id=sup.id, negotiation_id=nid, rfq_mode=True, listener=listener,
                ledger=ledger_factory(nid) if ledger_factory else None,
                brains=brains_factory(nid) if brains_factory else None,
                red_team=red_team, gateway=gateway, llm_mode=llm_mode)
        self.leverage_log: list[dict[str, Any]] = []
        self.result_obj: RFQResult | None = None
        self.started_at = ""

    def emit(self, type_: str, data: dict[str, Any], visibility: list[str] | None = None) -> None:
        vis = [EventVisibility(v) for v in (visibility or ["buyer", "arbiter"])]
        ev = NegotiationEvent(seq=len(self.events) + 1, type=type_, visibility=vis,
                              data={"rfq_id": self.id, "negotiation_id": self.id, "supplier_id": None, **data})
        self.events.append(ev)
        if self.ledger is not None:
            self.ledger.append(type_, "buyer-sourcing", ev.data, [v.value for v in vis])
        if self.listener is not None:
            res = self.listener(ev)
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)

    # ------------------------------------------------------------------ run

    async def run(self) -> RFQResult:
        self.started_at = utcnow()
        self.emit("rfq_started", {
            "scenario_id": self.scenario.id, "title": self.scenario.title,
            "lanes": [{"supplier_id": sid, "name": s.supplier_profile.party.name, "negotiation_id": s.id}
                      for sid, s in self.sessions.items()],
            "leverage": self.leverage, "switching_margin": self.switching_margin,
        }, visibility=["public"])
        for s in self.sessions.values():
            await s.start()
        rnd = 0
        while any(s.running for s in self.sessions.values()):
            rnd += 1
            await asyncio.gather(*(s.step_round() for s in self.sessions.values() if s.running))
            if self.leverage:
                self._update_leverage(rnd)
            self.emit("rfq_round", {"round": rnd, "lanes": self._standings()})
            if self.speed_ms:
                await asyncio.sleep(self.speed_ms / 1000)
        result = self._select()
        if self.ledger is not None:
            self.ledger.request_anchor("rfq_awarded")
        for s in self.sessions.values():
            if s.ledger is not None:
                await s.ledger.flush()
        if self.ledger is not None:
            await self.ledger.flush()
        self.result_obj = result
        return result

    def _standing_terms(self, s: NegotiationSession) -> Terms | None:
        if s.agreed_terms:
            return s.agreed_terms
        return s.supplier.my_offers[-1] if s.supplier.my_offers else None

    def _standings(self) -> list[dict[str, Any]]:
        out = []
        for sid, s in self.sessions.items():
            terms = self._standing_terms(s)
            acceptable = terms is not None and s.buyer.util.acceptable(terms, reservation=s.buyer.util.reservation)
            out.append({"supplier_id": sid, "status": s.status.value, "round": s.round,
                        "standing_offer": terms, "acceptable": acceptable,
                        "u_buyer": None if terms is None else round(self.reference.utility(terms), 4)})
        return out

    def _update_leverage(self, rnd: int) -> None:
        quotes: dict[str, float] = {}
        for sid, s in self.sessions.items():
            terms = self._standing_terms(s)
            if terms is not None and s.buyer.util.acceptable(terms, reservation=s.buyer.util.reservation):
                quotes[sid] = self.reference.utility(terms)
        for sid, s in self.sessions.items():
            if not s.running:
                continue
            alternatives = [u for other, u in quotes.items() if other != sid]
            if not alternatives:
                continue
            outside = max(alternatives) - self.switching_margin
            if outside > s.buyer.util.reservation:
                previous = s.buyer.reservation_override
                s.buyer.reservation_override = outside
                if previous is None or abs(previous - outside) > 1e-4:
                    entry = {"round": rnd, "supplier_id": sid, "buyer_outside_option": round(outside, 4)}
                    self.leverage_log.append(entry)
                    self.emit("rfq_leverage", entry, visibility=["buyer", "arbiter"])

    # ------------------------------------------------------------------ award

    def _select(self) -> RFQResult:
        issues = self.scenario.issues
        q = self.scenario.context.quantity
        cands: list[RFQCandidate] = []
        for sid, s in self.sessions.items():
            res = s.result()
            terms = res.agreed_terms
            cands.append(RFQCandidate(
                supplier_id=sid, supplier_name=s.supplier_profile.party.name, negotiation_id=s.id, status=res.status,
                terms=terms, u_buyer=None if terms is None else round(self.reference.utility(terms), 4),
                u_supplier=res.u_supplier, total_value=None if terms is None else round(terms[PRICE_KEY] * q, 2),
                note=res.reason))
        agreed = [c for c in cands if c.terms]
        for c in agreed:
            c.dominated_by = [o.supplier_id for o in agreed
                              if o is not c and buyer_dominates(o.terms, c.terms, issues)]  # type: ignore[arg-type]
            c.pareto_efficient = not c.dominated_by
        ranked = sorted(agreed, key=lambda c: (c.pareto_efficient, c.u_buyer or 0, c.u_supplier or 0), reverse=True)
        for i, c in enumerate(ranked, start=1):
            c.rank = i
        winner = ranked[0] if ranked and ranked[0].pareto_efficient else None
        if winner is None:
            rationale = "No lane produced a compliant best-and-final quote; the buyer keeps its BATNA."
        else:
            others = [c for c in ranked[1:]]
            parts = [f"{winner.supplier_name} offers the highest buyer utility ({winner.u_buyer:.3f}) among "
                     f"Pareto-efficient quotes"]
            for c in others:
                if c.dominated_by:
                    parts.append(f"{c.supplier_name} is dominated by {', '.join(c.dominated_by)} on every term")
                else:
                    parts.append(f"{c.supplier_name} is Pareto-efficient but scores lower ({c.u_buyer:.3f})")
            for c in cands:
                if not c.terms:
                    parts.append(f"{c.supplier_name}: no binding quote ({c.note})")
            rationale = "; ".join(parts) + "."
        result = RFQResult(
            rfq_id=self.id, scenario_id=self.scenario.id, winner=winner.supplier_id if winner else None,
            winner_negotiation_id=winner.negotiation_id if winner else None, rationale=rationale,
            candidates=cands, negotiations={sid: s.result() for sid, s in self.sessions.items()},
            leverage_log=self.leverage_log, started_at=self.started_at, finished_at=utcnow())
        self.emit("rfq_award", {"winner": result.winner, "winner_negotiation_id": result.winner_negotiation_id,
                                "rationale": rationale,
                                "candidates": [c.model_dump(mode="json") for c in cands]}, visibility=["public"])
        return result
