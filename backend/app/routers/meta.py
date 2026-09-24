"""Health, platform status, scenarios, rulebook and compiled guardrails."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from agents import __version__
from agents.guardrails.arbiter import LegalArbiter
from agents.guardrails.rego import compile_rego
from agents.guardrails.rules import load_rulebook_data
from agents.scenarios import get_scenario, load_scenarios

from ..deps import get_service
from ..services import RunService

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/status")
def status(service: RunService = Depends(get_service)) -> dict[str, Any]:
    store = service.store
    return {
        **service.platform.status(),
        "counts": {"runs": len(store.runs), "contracts": len(store.contracts), "active_tasks": len(service.tasks)},
        "version": __version__,
    }


@router.get("/scenarios")
def scenarios() -> list[dict[str, Any]]:
    out = []
    for sc in load_scenarios().values():
        out.append({
            "id": sc.id, "title": sc.title, "summary": sc.summary, "mode": sc.mode, "tags": sc.tags,
            "max_rounds": sc.max_rounds, "jurisdiction": sc.context.jurisdiction, "currency": sc.context.currency,
            "buyer": sc.context.buyer.name, "suppliers": [{"id": s.id, "name": s.party.name} for s in sc.suppliers],
            "issues": [i.model_dump(mode="json") for i in sc.issues],
        })
    return out


@router.get("/scenarios/{scenario_id}")
def scenario(scenario_id: str) -> dict[str, Any]:
    """Full scenario including sealed envelopes - the operator's Policy Setup view."""
    try:
        return get_scenario(scenario_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/scenarios/{scenario_id}/rego", response_class=PlainTextResponse)
def scenario_rego(scenario_id: str, role: Literal["buyer", "supplier"] = Query("buyer"),
                  supplier_id: str | None = None) -> str:
    try:
        sc = get_scenario(scenario_id)
        supplier = sc.supplier(supplier_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    arbiter = LegalArbiter(sc, supplier)
    org = sc.context.buyer.name if role == "buyer" else supplier.party.name
    return compile_rego(arbiter.utils[role], arbiter.rulebook, org, arbiter.commitments()[role])


@router.get("/rulebook")
def rulebook() -> dict[str, Any]:
    return load_rulebook_data()
