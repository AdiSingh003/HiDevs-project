"""Lyzr AIMS (AI Management System) mirroring and anchoring.

Two layers, both verified against the live Lyzr service:

* **Event log** - every hash-chained entry is appended to the negotiation's AIMS event log
  (``POST /log/{session_id}``). Cheap (no LLM tokens), but write-only.
* **Anchors** - at checkpoints (negotiation finished, contract compiled / approved / amended) the
  chain head is sent to the *Audit Scribe* agent in session ``aims-{stream}``. Lyzr stores the
  message with its own server timestamp and returns it via ``GET /v3/sessions/{id}/messages``.

Reconciliation re-reads the anchors from Lyzr and checks that the local ledger still produces the
anchored head at each anchored length. A local hash chain alone can be *completely rewritten*
(re-hashing every entry); an anchor held by an independent system cannot, so AIMS makes tampering
detectable even for an attacker with write access to the local ledger.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..lyzr.client import LyzrAgentClient, LyzrError
from ..lyzr.settings import LyzrSettings
from .ledger import AuditEntry, canonical

MAX_PAYLOAD_CHARS = 2500
ANCHOR_PREFIX = "AUDIT_ANCHOR "
_ANCHOR_RE = re.compile(r"AUDIT_ANCHOR (\{.*\})", re.DOTALL)


def compact(entry: AuditEntry) -> str:
    payload = canonical(entry.payload)
    if len(payload) > MAX_PAYLOAD_CHARS:
        payload = json.dumps({"truncated": True, "preview": payload[:MAX_PAYLOAD_CHARS]})
    return canonical({"seq": entry.seq, "ts": entry.ts, "type": entry.event_type, "actor": entry.actor,
                      "prev_hash": entry.prev_hash, "hash": entry.hash, "payload": json.loads(payload)})


def anchor_session(stream: str) -> str:
    return f"aims-{stream}"


def reconcile_anchors(entries: list[AuditEntry], anchors: list[dict[str, Any]]) -> dict[str, Any]:
    """Check every AIMS anchor against the local chain (pure function - no network)."""
    results = []
    for a in anchors:
        n = int(a.get("entries", 0))
        local = entries[n - 1].hash if 0 < n <= len(entries) else None
        results.append({**a, "local_head": local, "match": local == a.get("head")})
    return {
        "anchors": results,
        "anchored": bool(results),
        "all_match": bool(results) and all(r["match"] for r in results),
        "latest_anchored_seq": max((int(r.get("entries", 0)) for r in results), default=0),
        "local_entries": len(entries),
    }


class LyzrAIMSSink:
    def __init__(self, client: LyzrAgentClient, settings: LyzrSettings):
        self.client = client
        self.settings = settings
        self.mode = settings.aims_mode
        self.name = f"lyzr-aims:{self.mode}"

    @property
    def can_anchor(self) -> bool:
        return bool(self.settings.agent_id("audit"))

    async def push(self, entry: AuditEntry) -> bool:
        if self.mode == "event_log":
            return await self.client.log_event(entry.stream, compact(entry))
        if self.mode == "agent_session":
            agent_id = self.settings.agent_id("audit")
            if not agent_id:
                return False
            await self.client.chat(agent_id, f"audit-{entry.stream}", f"AUDIT_EVENT {compact(entry)}")
            return True
        return False

    async def anchor(self, stream: str, entries: int, head: str, reason: str) -> dict[str, Any] | None:
        """Record the chain head in the Audit Scribe's AIMS session; returns the anchor or None."""
        agent_id = self.settings.agent_id("audit")
        if not agent_id or entries == 0:
            return None
        anchor = {"type": "AUDIT_ANCHOR", "stream": stream, "entries": entries, "head": head, "reason": reason}
        reply = await self.client.chat(agent_id, anchor_session(stream), ANCHOR_PREFIX + canonical(anchor))
        return {**anchor, "ack": reply.text[:120]}

    async def fetch_anchors(self, stream: str) -> list[dict[str, Any]]:
        """Read anchors back from Lyzr (source of truth: Lyzr's stored session messages + timestamps)."""
        try:
            resp = await self.client._request("GET", f"/v3/sessions/{anchor_session(stream)}/messages")
        except LyzrError as exc:
            if "404" in str(exc):
                return []
            raise
        data = resp.json()
        messages = data.get("messages", data) if isinstance(data, dict) else data
        out = []
        for m in messages or []:
            if m.get("role") != "user":
                continue
            match = _ANCHOR_RE.search(str(m.get("content", "")))
            if not match:
                continue
            try:
                anchor = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if anchor.get("stream") == stream:
                out.append({**anchor, "lyzr_created_at": m.get("created_at"), "lyzr_message_id": m.get("_id")})
        return out
