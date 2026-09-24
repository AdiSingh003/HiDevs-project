"""High-level façade used by the FastAPI backend and the CLI.

Wires Lyzr (Agent API brains, Safe AI gateway, AIMS sink) when credentials exist and falls back to the
deterministic offline stack otherwise, then exposes the three product flows:

* bilateral negotiation  -> contract (JSON + PDF, signed)
* multi-vendor RFQ       -> Pareto award -> contract for the winner
* telemetry event        -> assessment -> bounded renegotiation -> hash-chained amendment
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from .audit.aims import LyzrAIMSSink
from .audit.ledger import AuditLedger
from .contract.automata_pipeline import DraftingPipeline
from .contract.compiler import ContractCompiler
from .contract.signing import KeyStore
from .core.models import NegotiationEvent, Outcome, Scenario
from .lyzr.client import LyzrAgentClient
from .lyzr.rai import LyzrRAIClient, SafeAIGateway
from .lyzr.settings import LyzrSettings, get_lyzr_settings
from .negotiation.engine import NegotiationSession
from .negotiation.llm import LyzrBrain, NegotiatorBrain
from .negotiation.renegotiation import Assessment, TelemetryEvent, amendment_record, amendment_scenario, assess
from .negotiation.rfq import RFQOrchestrator

log = logging.getLogger(__name__)
Listener = Callable[[NegotiationEvent], Any]


class NegotiationPlatform:
    def __init__(self, data_dir: Path | None = None, settings: LyzrSettings | None = None):
        self.settings = settings or get_lyzr_settings()
        self.data_dir = data_dir
        self.keystore = KeyStore(data_dir / "keys" if data_dir else None)
        self.client: LyzrAgentClient | None = None
        self.gateway: SafeAIGateway | None = None
        self.aims: LyzrAIMSSink | None = None
        if self.settings.configured:
            self.client = LyzrAgentClient(self.settings)
            if self.settings.any_rai_policy or self.settings.lyzr_opa_guardrails:
                self.gateway = SafeAIGateway(LyzrRAIClient(self.settings), self.settings)
            if self.settings.aims_mode != "local":
                self.aims = LyzrAIMSSink(self.client, self.settings)
        self.compiler = ContractCompiler(self.keystore, DraftingPipeline(self.settings, self.client))

    # ------------------------------------------------------------------ wiring helpers

    def status(self) -> dict[str, Any]:
        return {
            "lyzr": self.settings.status(),
            "llm_modes": ["offline"] + (["lyzr"] if self.settings.negotiators_ready else []),
            "safe_ai": {"local_rules": True, "lyzr_rai": bool(self.gateway and self.gateway.screens_text),
                        "lyzr_opa": bool(self.gateway and self.settings.lyzr_opa_guardrails)},
            "automata": {"engine": "lyzr-automata LinearSyncPipeline",
                         "model": "lyzr-agent-api" if self.compiler.drafting.online else "offline-template"},
            "aims": {"mode": self.aims.mode if self.aims else "local", "local_ledger": True},
        }

    def resolve_llm_mode(self, requested: str = "auto") -> str:
        if requested == "auto":
            return "lyzr" if self.settings.negotiators_ready else "offline"
        if requested == "lyzr" and not self.settings.negotiators_ready:
            raise ValueError("LLM mode 'lyzr' needs LYZR_API_KEY plus LYZR_BUYER_AGENT_ID and LYZR_SUPPLIER_AGENT_ID")
        if requested not in ("lyzr", "offline"):
            raise ValueError(f"unknown llm_mode '{requested}'")
        return requested

    def brains(self, negotiation_id: str, llm_mode: str) -> dict[str, NegotiatorBrain] | None:
        if llm_mode != "lyzr" or self.client is None:
            return None
        return {
            "buyer": LyzrBrain(self.client, self.settings.lyzr_buyer_agent_id or "", f"{negotiation_id}-buyer"),
            "supplier": LyzrBrain(self.client, self.settings.lyzr_supplier_agent_id or "", f"{negotiation_id}-supplier"),
        }

    def ledger(self, stream_id: str) -> AuditLedger:
        path = self.data_dir / "audit" / f"{stream_id}.jsonl" if self.data_dir else None
        return AuditLedger(stream_id, path=path, sink=self.aims)

    # ------------------------------------------------------------------ flows

    def new_session(self, scenario: Scenario, *, negotiation_id: str, supplier_id: str | None = None,
                    llm_mode: str = "auto", red_team: bool = False, listener: Listener | None = None,
                    speed_ms: int = 0, max_rounds: int | None = None, ledger: AuditLedger | None = None,
                    rfq_mode: bool = False) -> NegotiationSession:
        mode = self.resolve_llm_mode(llm_mode)
        return NegotiationSession(scenario, supplier_id=supplier_id, negotiation_id=negotiation_id,
                                  max_rounds=max_rounds, brains=self.brains(negotiation_id, mode), red_team=red_team,
                                  gateway=self.gateway, listener=listener,
                                  ledger=ledger or self.ledger(negotiation_id), speed_ms=speed_ms, llm_mode=mode,
                                  rfq_mode=rfq_mode)

    def negotiation_record(self, session: NegotiationSession) -> dict[str, Any]:
        res = session.result()
        return {
            "negotiation_id": session.id, "outcome": res.status.value, "rounds": res.rounds,
            "accepted_by": res.accepted_by, "interventions": res.interventions, "blocked_moves": res.blocked_moves,
            "redactions": res.redactions, "mediated": res.mediated, "llm_mode": session.llm_mode,
            "audit_head": session.ledger.head if session.ledger else None,
            "envelope_commitments": session.arbiter.commitments(),
        }

    async def compile_contract(self, session: NegotiationSession, *, version: int = 1,
                               parent: dict[str, Any] | None = None, amendment: dict[str, Any] | None = None,
                               base_scenario: Scenario | None = None, terms: dict[str, float] | None = None,
                               ) -> dict[str, Any]:
        res = session.result()
        if res.status not in (Outcome.AGREEMENT, Outcome.MEDIATED) or not res.agreed_terms:
            raise ValueError(f"negotiation {session.id} has no agreement to compile")
        scenario = base_scenario or session.scenario
        supplier = scenario.supplier(session.supplier_profile.id)
        contract = await asyncio.to_thread(
            self.compiler.compile, scenario=scenario, supplier=supplier, terms=terms or res.agreed_terms,
            negotiation=self.negotiation_record(session), cfo_required=res.requires_cfo_approval,
            version=version, parent=parent, amendment=amendment)
        if session.ledger is not None:
            session.ledger.append("contract_compiled", "legal_arbiter", {
                "contract_id": contract["contract_id"], "version": contract["version"],
                "content_hash": contract["integrity"]["content_hash"], "status": contract["status"],
                "signatures": [s["role"] for s in contract["integrity"]["signatures"]],
                "drafting_engine": contract["drafting"]["engine"], "drafting_model": contract["drafting"]["model"],
            })
            session.ledger.request_anchor("contract_compiled")
            await session.ledger.flush()
        return contract

    def new_rfq(self, scenario: Scenario, *, rfq_id: str, llm_mode: str = "auto", red_team: bool = False,
                listener: Listener | None = None, speed_ms: int = 0, leverage: bool = True,
                ledger_factory: Callable[[str], AuditLedger] | None = None) -> RFQOrchestrator:
        mode = self.resolve_llm_mode(llm_mode)
        return RFQOrchestrator(scenario, rfq_id=rfq_id, brains_factory=lambda nid: self.brains(nid, mode) or {},
                               red_team=red_team, gateway=self.gateway, listener=listener,
                               ledger_factory=ledger_factory or self.ledger, speed_ms=speed_ms, leverage=leverage,
                               llm_mode=mode)

    def assess_event(self, event: TelemetryEvent, contract: dict[str, Any]) -> Assessment:
        return assess(event, contract)

    def renegotiation_session(self, scenario: Scenario, contract: dict[str, Any], event: TelemetryEvent,
                              assessment: Assessment, *, negotiation_id: str, listener: Listener | None = None,
                              speed_ms: int = 0, llm_mode: str = "auto",
                              ledger: AuditLedger | None = None) -> NegotiationSession:
        supplier = scenario.supplier(contract["parties"]["supplier"]["supplier_id"])
        amend = amendment_scenario(scenario, supplier, contract, event, assessment)
        return self.new_session(amend, negotiation_id=negotiation_id, llm_mode=llm_mode, listener=listener,
                                speed_ms=speed_ms, ledger=ledger)

    async def compile_amendment(self, session: NegotiationSession, scenario: Scenario, contract: dict[str, Any],
                                event: TelemetryEvent, assessment: Assessment) -> dict[str, Any]:
        res = session.result()
        assert res.agreed_terms is not None
        new_terms = {**contract["terms"], **res.agreed_terms}
        record = amendment_record(event, assessment, contract["terms"], res.agreed_terms, session.id)
        return await self.compile_contract(session, version=contract["version"] + 1, parent=contract,
                                           amendment=record, base_scenario=scenario, terms=new_terms)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        if self.gateway is not None:
            await self.gateway.rai.aclose()
