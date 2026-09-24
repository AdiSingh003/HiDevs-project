"""Start negotiations / RFQs and stream their events (Server-Sent Events)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from ..bus import visible
from ..deps import get_service
from ..services import RunService, StartNegotiation, StartRFQ

router = APIRouter(prefix="/api", tags=["runs"])
View = Literal["god", "public", "buyer", "supplier", "arbiter"]
KEEPALIVE_S = 15.0


def _bad_request(exc: Exception) -> HTTPException:
    if isinstance(exc, ValidationError):
        return HTTPException(422, json.loads(exc.json()))
    return HTTPException(400 if isinstance(exc, ValueError) else 404, str(exc))


@router.post("/negotiations", status_code=201)
async def start_negotiation(req: StartNegotiation, service: RunService = Depends(get_service)) -> dict[str, Any]:
    try:
        record = await service.start_negotiation(req)
    except (KeyError, ValueError, ValidationError) as exc:
        raise _bad_request(exc) from exc
    return record.model_dump(mode="json") if req.wait else record.summary()


@router.post("/rfq", status_code=201)
async def start_rfq(req: StartRFQ, service: RunService = Depends(get_service)) -> dict[str, Any]:
    try:
        record = await service.start_rfq(req)
    except (KeyError, ValueError, ValidationError) as exc:
        raise _bad_request(exc) from exc
    return record.model_dump(mode="json") if req.wait else record.summary()


@router.get("/runs")
def list_runs(kind: str | None = None, service: RunService = Depends(get_service)) -> list[dict[str, Any]]:
    return [r.summary() for r in service.store.list_runs(kind)]


@router.get("/runs/{run_id}")
def get_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    record = service.store.runs.get(run_id)
    if record is None:
        raise HTTPException(404, f"unknown run '{run_id}'")
    return record.model_dump(mode="json")


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, view: View = "god", after: int = Query(0, ge=0),
                     format: Literal["sse", "json"] = "sse", service: RunService = Depends(get_service)) -> Any:
    stream = service.bus.streams.get(run_id)
    if stream is None:
        raise HTTPException(404, f"unknown run '{run_id}'")
    if format == "json":
        return [r for r in stream.events[after:] if visible(r, view)]
    last_id = request.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        after = max(after, int(last_id))

    async def gen() -> AsyncIterator[str]:
        history, queue, last = stream.open(after, view)
        try:
            yield "retry: 3000\n\n"
            for record in history:
                yield f"id: {record['id']}\nevent: {record['type']}\ndata: {json.dumps(record, default=str)}\n\n"
            while queue is not None:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
                    continue
                if record is None:
                    break
                if record["id"] > last and visible(record, view):
                    yield f"id: {record['id']}\nevent: {record['type']}\ndata: {json.dumps(record, default=str)}\n\n"
            yield "event: end\ndata: {}\n\n"
        finally:
            stream.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
