"""In-process event bus: per-run event history + live subscribers (feeds the SSE endpoints)."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from agents.core.models import NegotiationEvent, utcnow


def visible(record: dict[str, Any], view: str) -> bool:
    if view == "god":
        return True
    vis = set(record.get("visibility", []))
    if view == "public":
        return "public" in vis
    return "public" in vis or view in vis


class EventStream:
    def __init__(self, run_id: str, history: list[dict[str, Any]] | None = None, closed: bool = False):
        self.run_id = run_id
        self.events: list[dict[str, Any]] = list(history or [])
        self.closed = closed
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()

    def publish(self, event: NegotiationEvent | dict[str, Any]) -> dict[str, Any]:
        if isinstance(event, NegotiationEvent):
            record = {"type": event.type, "ts": event.ts, "visibility": [v.value for v in event.visibility],
                      "data": event.data}
        else:
            record = dict(event)
            record.setdefault("visibility", ["public"])
            record.setdefault("ts", utcnow())
            record.setdefault("data", {})
        record["id"] = len(self.events) + 1
        self.events.append(record)
        for queue in list(self._subscribers):
            queue.put_nowait(record)
        return record

    def close(self) -> None:
        self.closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    def open(self, after: int = 0, view: str = "god"
             ) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any] | None] | None, int]:
        """Snapshot history and (if still live) register a queue - atomically, so no event is missed."""
        queue: asyncio.Queue[dict[str, Any] | None] | None = None
        if not self.closed:
            queue = asyncio.Queue()
            self._subscribers.add(queue)
        history = [r for r in self.events[after:] if visible(r, view)]
        return history, queue, len(self.events)

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None] | None) -> None:
        if queue is not None:
            self._subscribers.discard(queue)

    async def subscribe(self, after: int = 0, view: str = "god") -> AsyncIterator[dict[str, Any]]:
        history, queue, last = self.open(after, view)
        try:
            for record in history:
                yield record
            while queue is not None:
                record = await queue.get()
                if record is None:
                    return
                if record["id"] > last and visible(record, view):
                    yield record
        finally:
            self.unsubscribe(queue)


class EventBus:
    def __init__(self) -> None:
        self.streams: dict[str, EventStream] = {}

    def stream(self, run_id: str) -> EventStream:
        if run_id not in self.streams:
            self.streams[run_id] = EventStream(run_id)
        return self.streams[run_id]

    def restore(self, run_id: str, history: list[dict[str, Any]]) -> EventStream:
        self.streams[run_id] = EventStream(run_id, history, closed=True)
        return self.streams[run_id]
