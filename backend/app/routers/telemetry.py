"""Telemetry webhook (HMAC-signed) that triggers live contract renegotiation."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from agents.negotiation.renegotiation import TelemetryEvent

from .. import security
from ..deps import get_service
from ..services import RunService

router = APIRouter(prefix="/api", tags=["telemetry"])


@router.post("/webhooks/telemetry", status_code=202)
async def telemetry_webhook(request: Request, service: RunService = Depends(get_service)) -> dict[str, Any]:
    body = await request.body()
    try:
        security.verify(service.settings.telemetry_webhook_secret, body,
                        request.headers.get(security.TIMESTAMP_HEADER), request.headers.get(security.SIGNATURE_HEADER),
                        service.settings.webhook_tolerance_s)
    except security.SignatureError as exc:
        raise HTTPException(401, f"invalid webhook signature: {exc}") from exc
    try:
        event = TelemetryEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc
    try:
        return await service.handle_telemetry(event)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/telemetry/simulate", status_code=202)
async def simulate(payload: dict[str, Any], service: RunService = Depends(get_service)) -> dict[str, Any]:
    """Demo helper: sign an event server-side and dispatch it through the same verified path as the webhook."""
    if not service.settings.enable_telemetry_simulator:
        raise HTTPException(403, "telemetry simulator disabled")
    speed_ms = payload.pop("speed_ms", None)
    try:
        event = TelemetryEvent.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc
    body = event.model_dump_json().encode()
    ts, signature = security.sign(service.settings.telemetry_webhook_secret, body)
    security.verify(service.settings.telemetry_webhook_secret, body, ts, signature)
    try:
        result = await service.handle_telemetry(event, speed_ms=speed_ms)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {**result, "signed_headers": {security.TIMESTAMP_HEADER: ts, security.SIGNATURE_HEADER: signature}}
