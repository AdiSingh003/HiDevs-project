"""Hash-chained audit ledgers (mirrored to Lyzr AIMS when configured)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agents.audit.ledger import AuditLedger
from agents.lyzr.client import LyzrError

from ..deps import get_service
from ..services import RunService

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _ledger(service: RunService, stream_id: str) -> AuditLedger:
    ledger = service.ledgers.get(stream_id)
    if ledger is None:
        if stream_id not in service.store.audit_streams():
            raise HTTPException(404, f"no audit ledger for '{stream_id}'")
        ledger = service.ledger(stream_id)
    return ledger


@router.get("")
def streams(service: RunService = Depends(get_service)) -> list[dict[str, Any]]:
    out = []
    for stream_id in service.store.audit_streams():
        ledger = service.ledger(stream_id)
        out.append({"stream": stream_id, "entries": len(ledger.entries), "head": ledger.head,
                    "valid": ledger.verify()["valid"], "aims": ledger.sync_summary()})
    return out


@router.get("/{stream_id}")
def entries(stream_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    ledger = _ledger(service, stream_id)
    return {"stream": stream_id, "head": ledger.head, "verification": ledger.verify(), "aims": ledger.sync_summary(),
            "aims_mode": service.platform.aims.mode if service.platform.aims else "local",
            "entries": [{**e.model_dump(mode="json"), "aims": ledger.sync_status.get(e.seq, "local")}
                        for e in ledger.entries]}


@router.get("/{stream_id}/aims")
async def aims(stream_id: str, rewrite_seq: int | None = Query(None, ge=1),
               service: RunService = Depends(get_service)) -> dict[str, Any]:
    """Reconcile the local ledger with the chain-head anchors stored in Lyzr AIMS."""
    _ledger(service, stream_id)
    try:
        return await service.aims_reconcile(stream_id, rewrite_seq)
    except LyzrError as exc:
        raise HTTPException(502, f"Lyzr AIMS unavailable: {exc}") from exc


@router.get("/{stream_id}/verify")
def verify(stream_id: str, tamper_seq: int | None = Query(None, ge=1),
           service: RunService = Depends(get_service)) -> dict[str, Any]:
    """Verify the chain. ``tamper_seq`` verifies a copy with that entry altered (tamper-detection demo)."""
    ledger = _ledger(service, stream_id)
    if tamper_seq is None:
        return {"stream": stream_id, "simulated_tamper": None, **ledger.verify()}
    return {"stream": stream_id, "simulated_tamper": tamper_seq,
            **AuditLedger.verify_entries(ledger.tampered_copy(tamper_seq))}
