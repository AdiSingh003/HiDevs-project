"""Contracts: JSON, PDF, versions/amendments, signature verification, CFO approval, executable SLA."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from agents.contract.compiler import verify_contract
from agents.contract.pdf import render_contract_pdf
from agents.contract.sla import evaluate_delay, evaluate_otif

from ..deps import get_service
from ..services import RunService

router = APIRouter(prefix="/api/contracts", tags=["contracts"])


class Approval(BaseModel):
    approver_name: str | None = None


class SLAQuery(BaseModel):
    days_late: float = Field(0, ge=0, le=365)
    shipment_value: float | None = Field(None, gt=0)
    force_majeure: bool = False
    ld_already_charged: float = Field(0, ge=0)
    monthly_otif_pct: float | None = Field(None, ge=0, le=100)
    monthly_invoice_value: float | None = Field(None, gt=0)


def _contract(service: RunService, contract_id: str, version: int | None = None) -> dict[str, Any]:
    contract = service.store.contract(contract_id, version)
    if contract is None:
        raise HTTPException(404, f"unknown contract '{contract_id}'" + (f" v{version}" if version else ""))
    return contract


@router.get("")
def list_contracts(service: RunService = Depends(get_service)) -> list[dict[str, Any]]:
    return service.store.list_contracts()


@router.post("/verify-document")
def verify_document(contract: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Verify an arbitrary contract JSON (e.g. an edited copy) - demonstrates tamper evidence."""
    try:
        return verify_contract(contract)
    except (KeyError, TypeError) as exc:
        raise HTTPException(422, f"not a compiled contract: {exc}") from exc


@router.get("/{contract_id}")
def get_contract(contract_id: str, version: int | None = None, service: RunService = Depends(get_service)) -> dict:
    return _contract(service, contract_id, version)


@router.get("/{contract_id}/versions")
def versions(contract_id: str, service: RunService = Depends(get_service)) -> list[dict[str, Any]]:
    items = service.store.contract_versions(contract_id)
    if not items:
        raise HTTPException(404, f"unknown contract '{contract_id}'")
    return [{"version": c["version"], "status": c["status"], "created_at": c["created_at"],
             "content_hash": c["integrity"]["content_hash"], "parent_hash": c.get("parent_hash"),
             "terms": c["terms"], "total_value": c["commercial_terms"]["total_value"],
             "amendment": c.get("amendment")} for c in items]


@router.get("/{contract_id}/pdf")
def pdf(contract_id: str, version: int | None = None, service: RunService = Depends(get_service)) -> Response:
    contract = _contract(service, contract_id, version)
    data = render_contract_pdf(contract)
    filename = f"{contract['contract_id']}_v{contract['version']}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.post("/{contract_id}/verify")
def verify(contract_id: str, version: int | None = None, service: RunService = Depends(get_service)) -> dict:
    return verify_contract(_contract(service, contract_id, version))


@router.post("/{contract_id}/approve")
async def approve(contract_id: str, body: Approval | None = None,
                  service: RunService = Depends(get_service)) -> dict[str, Any]:
    _contract(service, contract_id)
    try:
        contract = await service.approve_contract(contract_id, body.approver_name if body else None)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"contract_id": contract_id, "status": contract["status"], "verification": verify_contract(contract)}


@router.post("/{contract_id}/sla")
def sla(contract_id: str, query: SLAQuery, version: int | None = None,
        service: RunService = Depends(get_service)) -> dict[str, Any]:
    contract = _contract(service, contract_id, version)
    out: dict[str, Any] = {"contract_id": contract_id, "version": contract["version"],
                           "delay": evaluate_delay(contract, query.days_late, query.shipment_value, query.force_majeure,
                                                   query.ld_already_charged)}
    if query.monthly_otif_pct is not None:
        invoice = query.monthly_invoice_value or contract["commercial_terms"]["total_value"]
        out["otif"] = evaluate_otif(contract, query.monthly_otif_pct, invoice)
    return out
