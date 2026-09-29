"""The in-process event bus behind the live SSE stream, and streaming a run while it is still in progress."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from agents.core.models import EventVisibility, NegotiationEvent
from backend.app.bus import EventBus, visible
from backend.app.routers import runs as runs_router

from .conftest import make_app


def test_visibility_rules_for_each_view():
    buyer_only = {"visibility": ["buyer", "arbiter"]}
    public = {"visibility": ["public"]}
    assert visible(buyer_only, "god") and visible(public, "god")
    assert visible(buyer_only, "buyer") and not visible(buyer_only, "supplier") and not visible(buyer_only, "public")
    assert visible(public, "supplier") and visible(public, "public")


async def test_live_subscribers_get_new_events_in_order_and_stop_when_the_run_closes():
    stream = EventBus().stream("NEG-1")
    stream.publish({"type": "negotiation_started"})  # before anyone subscribed: arrives as history

    async def collect(view: str) -> list[str]:
        return [r["type"] async for r in stream.subscribe(view=view)]

    buyer, public = asyncio.create_task(collect("buyer")), asyncio.create_task(collect("public"))
    await asyncio.sleep(0)  # let both subscribe
    stream.publish(NegotiationEvent(seq=2, type="turn", visibility=[EventVisibility.PUBLIC], data={"round": 1}))
    stream.publish({"type": "turn_private", "visibility": ["buyer", "arbiter"]})
    stream.publish({"type": "run_finished"})
    stream.close()
    assert await buyer == ["negotiation_started", "turn", "turn_private", "run_finished"]
    assert await public == ["negotiation_started", "turn", "run_finished"]
    assert not stream._subscribers  # every queue was released


async def test_a_closed_or_restored_stream_replays_history_only():
    bus = EventBus()
    restored = bus.restore("NEG-2", [{"id": 1, "type": "turn", "visibility": ["public"]},
                                     {"id": 2, "type": "run_finished", "visibility": ["public"]}])
    assert [r["type"] async for r in restored.subscribe(after=1)] == ["run_finished"]
    history, queue, last = restored.open()
    assert queue is None and last == 2 and len(history) == 2
    restored.unsubscribe(None)  # harmless


def test_streaming_a_run_in_progress_with_keep_alives(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_router, "KEEPALIVE_S", 0.02)  # moves are slower than this, so keep-alives appear
    with TestClient(make_app(tmp_path)) as client:
        run = client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po", "speed_ms": 60}).json()
        assert run["status"] == "running"
        with client.stream("GET", f"/api/runs/{run['id']}/events", params={"view": "supplier"}) as s:
            text = "".join(s.iter_text())
    lines = text.splitlines()
    assert ": keep-alive" in lines
    types = [line[len("event: "):] for line in lines if line.startswith("event: ")]
    assert types[0] == "negotiation_started" and types[-2:] == ["run_finished", "end"]
    datas = [json.loads(line[6:]) for line in lines if line.startswith("data: ") and line != "data: {}"]
    ids = [d["id"] for d in datas]
    assert ids == sorted(set(ids))  # live and in order, with gaps where buyer-private events were filtered out
    assert not any(d["type"] == "turn_private" and d["data"]["actor"] == "buyer" for d in datas)
