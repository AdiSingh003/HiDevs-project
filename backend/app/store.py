"""Persistence for runs, events, contracts, telemetry idempotency and audit ledgers (see ``db.py`` for the schema).

The database is the source of truth. The app is a single process, so the store loads everything at start-up and
keeps in-memory indexes for fast reads, writing every change straight through to the database. Signing keys
stay under ``DATA_DIR/keys``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Engine, delete, insert, select, update

from agents.core.models import utcnow

from . import db

log = logging.getLogger(__name__)
RunKind = Literal["negotiation", "rfq", "renegotiation"]


class RunRecord(BaseModel):
    id: str
    kind: RunKind
    scenario_id: str
    title: str = ""
    status: str = "running"
    created_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    contract_id: str | None = None
    contract_version: int | None = None
    parent_contract_id: str | None = None
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        res = self.result or {}
        return {
            "id": self.id, "kind": self.kind, "scenario_id": self.scenario_id, "title": self.title,
            "status": self.status, "created_at": self.created_at, "finished_at": self.finished_at,
            "contract_id": self.contract_id, "contract_version": self.contract_version,
            "parent_contract_id": self.parent_contract_id,
            "rounds": res.get("rounds"), "winner": res.get("winner"),
            "llm_mode": self.options.get("llm_mode"), "red_team": self.options.get("red_team"),
        }


class SqlLedgerBackend:
    """Where ``AuditLedger`` keeps its entries: one row per entry, keyed by (stream, seq)."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def load(self, stream: str) -> list[dict[str, Any]]:
        q = select(db.audit_entries.c.entry).where(db.audit_entries.c.stream == stream).order_by(db.audit_entries.c.seq)
        with self.engine.connect() as conn:
            return [row.entry for row in conn.execute(q)]

    def append(self, entry: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(db.audit_entries).values(stream=entry["stream"], seq=entry["seq"], hash=entry["hash"],
                                                         entry=entry))

    def streams(self) -> list[str]:
        q = select(db.audit_entries.c.stream).distinct().order_by(db.audit_entries.c.stream)
        with self.engine.connect() as conn:
            return [row.stream for row in conn.execute(q)]


class Store:
    def __init__(self, database_url: str):
        self.engine = db.make_engine(database_url)
        db.metadata.create_all(self.engine)
        self.ledger_backend = SqlLedgerBackend(self.engine)
        self.runs: dict[str, RunRecord] = {}
        self.run_events: dict[str, list[dict[str, Any]]] = {}
        self.contracts: dict[str, dict[int, dict[str, Any]]] = {}
        self.seen_events: set[str] = set()
        self._load()

    def _load(self) -> None:
        with self.engine.begin() as conn:
            for row in conn.execute(select(db.runs.c.id, db.runs.c.record)):
                record = RunRecord.model_validate(row.record)
                if record.status == "running":  # the process stopped mid-run
                    record.status = "interrupted"
                    conn.execute(update(db.runs).where(db.runs.c.id == record.id)
                                 .values(status=record.status, record=record.model_dump(mode="json")))
                self.runs[record.id] = record
                self.run_events[record.id] = []
            for row in conn.execute(select(db.run_events).order_by(db.run_events.c.run_id, db.run_events.c.seq)):
                self.run_events.setdefault(row.run_id, []).append(row.event)
            for row in conn.execute(select(db.contracts.c.document)):
                contract = row.document
                self.contracts.setdefault(contract["contract_id"], {})[int(contract["version"])] = contract
            self.seen_events.update(row.event_id for row in conn.execute(select(db.telemetry_events.c.event_id)))

    # ------------------------------------------------------------------ runs

    def _upsert_run(self, conn: Any, record: RunRecord) -> None:
        values = {"kind": record.kind, "status": record.status, "created_at": record.created_at,
                  "record": record.model_dump(mode="json")}
        if conn.execute(update(db.runs).where(db.runs.c.id == record.id).values(**values)).rowcount == 0:
            conn.execute(insert(db.runs).values(id=record.id, **values))

    def put_run(self, record: RunRecord) -> None:
        self.runs[record.id] = record
        with self.engine.begin() as conn:
            self._upsert_run(conn, record)

    def save_run(self, record: RunRecord, events: list[dict[str, Any]]) -> None:
        self.runs[record.id] = record
        self.run_events[record.id] = events
        rows = [{"run_id": record.id, "seq": i, "event": e} for i, e in enumerate(events, 1)]
        with self.engine.begin() as conn:
            self._upsert_run(conn, record)
            conn.execute(delete(db.run_events).where(db.run_events.c.run_id == record.id))
            if rows:
                conn.execute(insert(db.run_events), rows)

    def list_runs(self, kind: str | None = None) -> list[RunRecord]:
        runs = [r for r in self.runs.values() if kind is None or r.kind == kind]
        return sorted(runs, key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------------ contracts

    def save_contract(self, contract: dict[str, Any]) -> None:
        cid, version = contract["contract_id"], int(contract["version"])
        self.contracts.setdefault(cid, {})[version] = contract
        key = (db.contracts.c.contract_id == cid) & (db.contracts.c.version == version)
        with self.engine.begin() as conn:
            if conn.execute(update(db.contracts).where(key).values(document=contract)).rowcount == 0:
                conn.execute(insert(db.contracts).values(contract_id=cid, version=version,
                                                         created_at=contract["created_at"], document=contract))

    def contract(self, contract_id: str, version: int | None = None) -> dict[str, Any] | None:
        versions = self.contracts.get(contract_id)
        if not versions:
            return None
        return versions.get(version) if version is not None else versions[max(versions)]

    def contract_versions(self, contract_id: str) -> list[dict[str, Any]]:
        return [self.contracts[contract_id][v] for v in sorted(self.contracts.get(contract_id, {}))]

    def list_contracts(self) -> list[dict[str, Any]]:
        out = []
        for cid in self.contracts:
            c = self.contract(cid)
            assert c is not None
            ct = c["commercial_terms"]
            out.append({
                "contract_id": cid, "version": c["version"], "versions": len(self.contracts[cid]),
                "status": c["status"], "title": c["title"], "buyer": c["parties"]["buyer"]["name"],
                "supplier": c["parties"]["supplier"]["name"], "currency": ct["currency"],
                "total_value": ct["total_value"], "created_at": c["created_at"],
                "negotiation_id": c["negotiation"].get("negotiation_id"), "scenario_id": c["rfq"]["scenario_id"],
            })
        return sorted(out, key=lambda x: x["created_at"], reverse=True)

    # ------------------------------------------------------------------ telemetry idempotency

    def mark_event_seen(self, event_id: str, contract_id: str | None = None) -> bool:
        """Record a processed webhook event. Returns False when it was already seen."""
        if event_id in self.seen_events:
            return False
        self.seen_events.add(event_id)
        with self.engine.begin() as conn:
            conn.execute(insert(db.telemetry_events).values(event_id=event_id, contract_id=contract_id,
                                                            received_at=utcnow()))
        return True

    # ------------------------------------------------------------------ audit

    def audit_streams(self) -> list[str]:
        return self.ledger_backend.streams()

    def close(self) -> None:
        self.engine.dispose()
