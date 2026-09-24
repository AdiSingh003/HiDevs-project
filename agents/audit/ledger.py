"""Append-only, hash-chained audit ledger (tamper-evident).

Each entry commits to its predecessor: ``hash = sha256(canonical(entry_without_hash))`` where the
entry includes ``prev_hash``. Editing, deleting or re-ordering any entry breaks every later hash,
which :meth:`AuditLedger.verify` pinpoints. Entries are mirrored to Lyzr AIMS when a sink is attached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..core.models import utcnow

log = logging.getLogger(__name__)
GENESIS = "0" * 64


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class AuditEntry(BaseModel):
    seq: int
    ts: str = Field(default_factory=utcnow)
    stream: str
    event_type: str
    actor: str
    visibility: list[str] = Field(default_factory=lambda: ["arbiter"])
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str = ""

    def digest(self) -> str:
        body = self.model_dump(mode="json", exclude={"hash"})
        return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


class AIMSSink(Protocol):
    name: str

    async def push(self, entry: AuditEntry) -> bool: ...


class AuditLedger:
    def __init__(self, stream: str, path: Path | None = None, sink: AIMSSink | None = None):
        self.stream = stream
        self.path = path
        self.sink = sink
        self.entries: list[AuditEntry] = []
        self.sync_status: dict[int, str] = {}
        self.anchors: list[dict[str, Any]] = []
        self._pending: set[asyncio.Task[Any]] = set()
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.entries.append(AuditEntry.model_validate_json(line))

    @property
    def head(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    def append(self, event_type: str, actor: str, payload: dict[str, Any] | None = None,
               visibility: list[str] | None = None) -> AuditEntry:
        entry = AuditEntry(seq=len(self.entries) + 1, stream=self.stream, event_type=event_type, actor=actor,
                           visibility=visibility or ["arbiter"], payload=json.loads(canonical(payload or {})),
                           prev_hash=self.head)
        entry.hash = entry.digest()
        self.entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.model_dump_json() + "\n")
        self._mirror(entry)
        return entry

    # ------------------------------------------------------------------ AIMS mirroring

    def _mirror(self, entry: AuditEntry) -> None:
        if self.sink is None:
            self.sync_status[entry.seq] = "local"
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.sync_status[entry.seq] = "pending"
            return
        self.sync_status[entry.seq] = "pending"
        task = loop.create_task(self._push(entry))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _push(self, entry: AuditEntry) -> None:
        assert self.sink is not None
        try:
            ok = await self.sink.push(entry)
            self.sync_status[entry.seq] = "synced" if ok else "failed"
        except Exception as exc:  # the local ledger stays authoritative
            log.warning("AIMS sync failed for %s#%s: %s", entry.stream, entry.seq, exc)
            self.sync_status[entry.seq] = "failed"

    def request_anchor(self, reason: str) -> None:
        """Anchor the current head in Lyzr AIMS in the background (no-op without an anchoring sink)."""
        if self.sink is None or not getattr(self.sink, "can_anchor", False) or not self.entries:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        entries, head = len(self.entries), self.head  # captured now: later appends don't change this anchor

        async def _anchor() -> None:
            try:
                anchor = await self.sink.anchor(self.stream, entries, head, reason)  # type: ignore[union-attr]
                if anchor:
                    self.anchors.append(anchor)
            except Exception as exc:  # the local chain stays authoritative
                log.warning("AIMS anchor failed for %s: %s", self.stream, exc)

        task = loop.create_task(_anchor())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def sync_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for status in self.sync_status.values():
            out[status] = out.get(status, 0) + 1
        return out

    # ------------------------------------------------------------------ verification

    @staticmethod
    def verify_entries(entries: list[AuditEntry]) -> dict[str, Any]:
        prev = GENESIS
        for i, entry in enumerate(entries):
            if entry.seq != i + 1:
                return {"valid": False, "broken_at": entry.seq, "reason": f"sequence gap: expected {i + 1}", "checked": i}
            if entry.prev_hash != prev:
                return {"valid": False, "broken_at": entry.seq, "reason": "prev_hash does not match predecessor",
                        "checked": i}
            if entry.digest() != entry.hash:
                return {"valid": False, "broken_at": entry.seq, "reason": "entry content was modified", "checked": i}
            prev = entry.hash
        return {"valid": True, "broken_at": None, "reason": "chain intact", "checked": len(entries), "head": prev}

    def verify(self) -> dict[str, Any]:
        return self.verify_entries(self.entries)

    def tampered_copy(self, seq: int | None = None) -> list[AuditEntry]:
        """A copy with one entry's payload altered - used to demonstrate tamper detection."""
        copy = [e.model_copy(deep=True) for e in self.entries]
        if not copy:
            return copy
        idx = (seq - 1) if seq else len(copy) // 2
        idx = max(0, min(idx, len(copy) - 1))
        copy[idx].payload = {**copy[idx].payload, "tampered": True}
        return copy

    def rewritten_copy(self, seq: int | None = None) -> list[AuditEntry]:
        """A *fully re-hashed* forgery: alter one entry, then recompute every later hash so the local chain
        verifies again. Only an external anchor (Lyzr AIMS) can expose this."""
        copy = self.tampered_copy(seq)
        prev = GENESIS
        for entry in copy:
            entry.prev_hash = prev
            entry.hash = entry.digest()
            prev = entry.hash
        return copy
