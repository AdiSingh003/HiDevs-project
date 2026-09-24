"""Run orchestration: negotiations, RFQs and telemetry renegotiations as background tasks with live streams."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.audit.aims import reconcile_anchors
from agents.audit.ledger import AuditLedger
from agents.core.models import Outcome, Scenario, utcnow
from agents.negotiation.renegotiation import Assessment, TelemetryEvent
from agents.platform import NegotiationPlatform
from agents.scenarios import get_scenario

from .bus import EventBus, EventStream
from .settings import AppSettings
from .store import RunRecord, Store

log = logging.getLogger(__name__)
LLMMode = Literal["auto", "offline", "lyzr"]


class StartNegotiation(BaseModel):
    scenario_id: str
    supplier_id: str | None = None
    llm_mode: LLMMode = "auto"
    red_team: bool = False
    speed_ms: int | None = Field(None, ge=0, le=5000)
    max_rounds: int | None = Field(None, ge=2, le=40)
    buyer: dict[str, Any] | None = Field(None, description="partial override of the buyer envelope")
    supplier: dict[str, Any] | None = Field(None, description="partial override of the supplier envelope")
    wait: bool = False


class StartRFQ(BaseModel):
    scenario_id: str = "steel_rfq"
    llm_mode: LLMMode = "auto"
    red_team: bool = False
    leverage: bool = True
    speed_ms: int | None = Field(None, ge=0, le=5000)
    wait: bool = False


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def apply_overrides(scenario: Scenario, buyer: dict[str, Any] | None, supplier: dict[str, Any] | None,
                    supplier_id: str | None) -> Scenario:
    if not buyer and not supplier:
        return scenario
    data = scenario.model_dump(mode="json")
    if buyer:
        data["buyer"] = deep_merge(data["buyer"], buyer)
    if supplier:
        idx = next((i for i, s in enumerate(data["suppliers"]) if supplier_id in (None, s["id"])), 0)
        data["suppliers"][idx]["envelope"] = deep_merge(data["suppliers"][idx]["envelope"], supplier)
    return Scenario.model_validate(data)


class RunService:
    def __init__(self, platform: NegotiationPlatform, store: Store, bus: EventBus, settings: AppSettings):
        self.platform = platform
        self.store = store
        self.bus = bus
        self.settings = settings
        self.tasks: set[asyncio.Task[Any]] = set()
        self.ledgers: dict[str, AuditLedger] = {}
        for run_id, events in store.run_events.items():
            bus.restore(run_id, events)

    # ------------------------------------------------------------------ helpers

    def ledger(self, stream_id: str) -> AuditLedger:
        if stream_id not in self.ledgers:
            self.ledgers[stream_id] = self.platform.ledger(stream_id)
        return self.ledgers[stream_id]

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _speed(self, requested: int | None) -> int:
        return self.settings.default_speed_ms if requested is None else requested

    def _finish(self, record: RunRecord, stream: EventStream) -> None:
        if not record.finished_at:
            record.finished_at = stream.events[-1]["ts"] if stream.events else utcnow()
        stream.publish({"type": "run_finished", "visibility": ["public"],
                        "data": {"run_id": record.id, "status": record.status, "contract_id": record.contract_id}})
        self.store.save_run(record, stream.events)
        stream.close()

    def _contract_event(self, stream: EventStream, contract: dict[str, Any], type_: str = "contract_compiled") -> None:
        stream.publish({"type": type_, "visibility": ["public"], "data": {
            "contract_id": contract["contract_id"], "version": contract["version"], "status": contract["status"],
            "content_hash": contract["integrity"]["content_hash"], "title": contract["title"],
            "cfo_required": contract["approvals"]["cfo_required"],
            "drafting_model": contract["drafting"]["model"],
        }})

    # ------------------------------------------------------------------ bilateral negotiation

    async def start_negotiation(self, req: StartNegotiation) -> RunRecord:
        scenario = apply_overrides(get_scenario(req.scenario_id), req.buyer, req.supplier, req.supplier_id)
        if scenario.mode != "bilateral":
            raise ValueError(f"scenario '{scenario.id}' is an RFQ - use POST /api/rfq")
        mode = self.platform.resolve_llm_mode(req.llm_mode)
        run_id = f"NEG-{uuid.uuid4().hex[:10].upper()}"
        record = RunRecord(id=run_id, kind="negotiation", scenario_id=scenario.id, title=scenario.title,
                           options={"llm_mode": mode, "red_team": req.red_team, "speed_ms": self._speed(req.speed_ms),
                                    "max_rounds": req.max_rounds or scenario.max_rounds,
                                    "overrides": bool(req.buyer or req.supplier)})
        self.store.put_run(record)
        stream = self.bus.stream(run_id)
        session = self.platform.new_session(scenario, negotiation_id=run_id, supplier_id=req.supplier_id,
                                            llm_mode=mode, red_team=req.red_team, listener=stream.publish,
                                            speed_ms=self._speed(req.speed_ms), max_rounds=req.max_rounds,
                                            ledger=self.ledger(run_id))
        task = self._spawn(self._negotiate(record, session, stream))
        if req.wait:
            await task
        return record

    async def _negotiate(self, record: RunRecord, session: Any, stream: EventStream) -> None:
        try:
            result = await session.run()
            record.result = result.model_dump(mode="json")
            if result.status in (Outcome.AGREEMENT, Outcome.MEDIATED):
                contract = await self.platform.compile_contract(session)
                self.store.save_contract(contract)
                record.contract_id, record.contract_version = contract["contract_id"], contract["version"]
                self._contract_event(stream, contract)
            record.status = result.status.value  # only final once the contract exists
        except Exception as exc:  # surface failures on the stream instead of dying silently
            log.exception("negotiation %s failed", record.id)
            record.status, record.error = "error", str(exc)
            stream.publish({"type": "error", "visibility": ["public"], "data": {"message": str(exc)}})
        finally:
            record.finished_at = session.finished_at or None
            self._finish(record, stream)

    # ------------------------------------------------------------------ multi-vendor RFQ

    async def start_rfq(self, req: StartRFQ) -> RunRecord:
        scenario = get_scenario(req.scenario_id)
        if scenario.mode != "rfq":
            raise ValueError(f"scenario '{scenario.id}' is bilateral - use POST /api/negotiations")
        mode = self.platform.resolve_llm_mode(req.llm_mode)
        run_id = f"RFQ-{uuid.uuid4().hex[:8].upper()}"
        record = RunRecord(id=run_id, kind="rfq", scenario_id=scenario.id, title=scenario.title,
                           options={"llm_mode": mode, "red_team": req.red_team, "leverage": req.leverage,
                                    "speed_ms": self._speed(req.speed_ms),
                                    "lanes": [s.id for s in scenario.suppliers]})
        self.store.put_run(record)
        stream = self.bus.stream(run_id)
        orch = self.platform.new_rfq(scenario, rfq_id=run_id, llm_mode=mode, red_team=req.red_team,
                                     listener=stream.publish, speed_ms=self._speed(req.speed_ms),
                                     leverage=req.leverage, ledger_factory=self.ledger)
        task = self._spawn(self._rfq(record, orch, stream))
        if req.wait:
            await task
        return record

    async def _rfq(self, record: RunRecord, orch: Any, stream: EventStream) -> None:
        try:
            result = await orch.run()
            record.result = result.model_dump(mode="json")
            if result.winner:
                contract = await self.platform.compile_contract(orch.sessions[result.winner])
                self.store.save_contract(contract)
                record.contract_id, record.contract_version = contract["contract_id"], contract["version"]
                self._contract_event(stream, contract)
            record.status = "awarded" if result.winner else "no_award"
        except Exception as exc:
            log.exception("rfq %s failed", record.id)
            record.status, record.error = "error", str(exc)
            stream.publish({"type": "error", "visibility": ["public"], "data": {"message": str(exc)}})
        finally:
            self._finish(record, stream)

    # ------------------------------------------------------------------ telemetry -> renegotiation

    async def handle_telemetry(self, event: TelemetryEvent, speed_ms: int | None = None) -> dict[str, Any]:
        if event.event_id in self.store.seen_events:
            return {"status": "duplicate", "event_id": event.event_id}
        contract = self.store.contract(event.contract_id)
        if contract is None:
            raise KeyError(f"unknown contract '{event.contract_id}'")
        self.store.seen_events.add(event.event_id)
        assessment = self.platform.assess_event(event, contract)
        ledger = self.ledger(contract["negotiation"]["negotiation_id"])
        ledger.append("telemetry_received", event.source, event.model_dump(mode="json"), ["public"])
        ledger.append("telemetry_assessed", "legal_arbiter", assessment.model_dump(mode="json"), ["public"])
        await ledger.flush()
        run_id = None
        if assessment.action == "renegotiate":
            record = await self.start_renegotiation(contract, event, assessment, speed_ms)
            run_id = record.id
        return {"status": "accepted", "event_id": event.event_id, "assessment": assessment.model_dump(mode="json"),
                "renegotiation_id": run_id}

    async def start_renegotiation(self, contract: dict[str, Any], event: TelemetryEvent, assessment: Assessment,
                                  speed_ms: int | None = None) -> RunRecord:
        scenario = get_scenario(contract["rfq"]["scenario_id"])
        run_id = f"RNG-{uuid.uuid4().hex[:8].upper()}"
        record = RunRecord(id=run_id, kind="renegotiation", scenario_id=scenario.id,
                           title=f"Amendment for {contract['contract_id']}: {event.event_type.replace('_', ' ')}",
                           parent_contract_id=contract["contract_id"],
                           options={"event_id": event.event_id, "speed_ms": self._speed(speed_ms),
                                    "llm_mode": self.platform.resolve_llm_mode("auto")})
        self.store.put_run(record)
        stream = self.bus.stream(run_id)
        stream.publish({"type": "telemetry_event", "visibility": ["public"], "data": {
            "contract_id": contract["contract_id"], "version": contract["version"],
            "event": event.model_dump(mode="json"), "assessment": assessment.model_dump(mode="json")}})
        session = self.platform.renegotiation_session(scenario, contract, event, assessment, negotiation_id=run_id,
                                                      listener=stream.publish, speed_ms=self._speed(speed_ms),
                                                      ledger=self.ledger(run_id))
        self._spawn(self._renegotiate(record, session, stream, scenario, contract, event, assessment))
        return record

    async def _renegotiate(self, record: RunRecord, session: Any, stream: EventStream, scenario: Scenario,
                           contract: dict[str, Any], event: TelemetryEvent, assessment: Assessment) -> None:
        parent_ledger = self.ledger(contract["negotiation"]["negotiation_id"])
        try:
            result = await session.run()
            record.result = result.model_dump(mode="json")
            if result.status in (Outcome.AGREEMENT, Outcome.MEDIATED):
                amendment = await self.platform.compile_amendment(session, scenario, contract, event, assessment)
                self.store.save_contract(amendment)
                record.contract_id, record.contract_version = amendment["contract_id"], amendment["version"]
                self._contract_event(stream, amendment, "amendment_compiled")
                parent_ledger.append("contract_amended", "legal_arbiter", {
                    "contract_id": amendment["contract_id"], "new_version": amendment["version"],
                    "parent_hash": amendment["parent_hash"], "content_hash": amendment["integrity"]["content_hash"],
                    "renegotiation_id": record.id, "changed_terms": amendment["amendment"]["changed_terms"]})
                parent_ledger.request_anchor("contract_amended")
            else:
                stream.publish({"type": "renegotiation_failed", "visibility": ["public"], "data": {
                    "reason": result.reason,
                    "fallback": "The contract stands as written; force-majeure relief applies where the event qualifies."}})
                parent_ledger.append("renegotiation_failed", "legal_arbiter", {"renegotiation_id": record.id,
                                                                               "reason": result.reason})
            await parent_ledger.flush()
            record.status = result.status.value
        except Exception as exc:
            log.exception("renegotiation %s failed", record.id)
            record.status, record.error = "error", str(exc)
            stream.publish({"type": "error", "visibility": ["public"], "data": {"message": str(exc)}})
        finally:
            record.finished_at = session.finished_at or None
            self._finish(record, stream)

    # ------------------------------------------------------------------ approvals

    async def approve_contract(self, contract_id: str, approver: str | None) -> dict[str, Any]:
        contract = self.store.contract(contract_id)
        if contract is None:
            raise KeyError(f"unknown contract '{contract_id}'")
        self.platform.compiler.approve_cfo(contract, approver)
        self.store.save_contract(contract)
        ledger = self.ledger(contract["negotiation"]["negotiation_id"])
        ledger.append("cfo_approved", "buyer_cfo", {"contract_id": contract_id, "version": contract["version"],
                                                    "content_hash": contract["integrity"]["content_hash"],
                                                    "approver": approver or "CFO"}, ["buyer", "arbiter"])
        ledger.request_anchor("cfo_approved")
        await ledger.flush()
        return contract

    # ------------------------------------------------------------------ AIMS governance

    async def aims_reconcile(self, stream_id: str, rewrite_seq: int | None = None) -> dict[str, Any]:
        """Compare the local chain with the anchors Lyzr AIMS holds. ``rewrite_seq`` reconciles a fully
        re-hashed forgery instead (it passes local verification - only the AIMS anchors expose it)."""
        sink = self.platform.aims
        if sink is None or not sink.can_anchor:
            return {"available": False, "stream": stream_id,
                    "reason": "AIMS anchoring is off - set LYZR_API_KEY, AIMS_MODE=event_log and LYZR_AUDIT_AGENT_ID"}
        ledger = self.ledger(stream_id)
        await ledger.flush()
        anchors = await sink.fetch_anchors(stream_id)
        entries = ledger.rewritten_copy(rewrite_seq) if rewrite_seq else ledger.entries
        return {"available": True, "stream": stream_id, "simulated_rewrite": rewrite_seq,
                "local_chain": AuditLedger.verify_entries(entries), **reconcile_anchors(entries, anchors)}

    async def shutdown(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.platform.aclose()
