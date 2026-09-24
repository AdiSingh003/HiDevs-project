"""File-backed store for runs and contract versions (JSON on disk + in-memory indexes).

Layout under DATA_DIR:
  runs/<run_id>.json                    run record + full event history (written when the run finishes)
  contracts/<contract_id>/v<N>.json     every contract version (amendments are new versions)
  audit/<stream_id>.jsonl               hash-chained audit ledgers (written by agents.audit.ledger)
  keys/<signer>.pem                     Ed25519 signing keys
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.core.models import utcnow

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


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.runs_dir = data_dir / "runs"
        self.contracts_dir = data_dir / "contracts"
        self.audit_dir = data_dir / "audit"
        for d in (self.runs_dir, self.contracts_dir, self.audit_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.runs: dict[str, RunRecord] = {}
        self.run_events: dict[str, list[dict[str, Any]]] = {}
        self.contracts: dict[str, dict[int, dict[str, Any]]] = {}
        self.seen_events: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                record = RunRecord.model_validate(raw["record"])
                if record.status == "running":  # the process died mid-run
                    record.status = "interrupted"
                self.runs[record.id] = record
                self.run_events[record.id] = raw.get("events", [])
            except Exception as exc:  # pragma: no cover - corrupted file
                log.warning("skipping unreadable run file %s: %s", path, exc)
        for path in sorted(self.contracts_dir.glob("*/v*.json")):
            try:
                contract = json.loads(path.read_text(encoding="utf-8"))
                self.contracts.setdefault(contract["contract_id"], {})[int(contract["version"])] = contract
                amendment = contract.get("amendment") or {}
                if amendment.get("event", {}).get("event_id"):
                    self.seen_events.add(amendment["event"]["event_id"])
            except Exception as exc:  # pragma: no cover
                log.warning("skipping unreadable contract file %s: %s", path, exc)

    # ------------------------------------------------------------------ runs

    def put_run(self, record: RunRecord) -> None:
        self.runs[record.id] = record

    def save_run(self, record: RunRecord, events: list[dict[str, Any]]) -> None:
        self.runs[record.id] = record
        self.run_events[record.id] = events
        path = self.runs_dir / f"{record.id}.json"
        with self._lock:
            path.write_text(json.dumps({"record": record.model_dump(mode="json"), "events": events}, default=str),
                            encoding="utf-8")

    def list_runs(self, kind: str | None = None) -> list[RunRecord]:
        runs = [r for r in self.runs.values() if kind is None or r.kind == kind]
        return sorted(runs, key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------------ contracts

    def save_contract(self, contract: dict[str, Any]) -> None:
        cid, version = contract["contract_id"], int(contract["version"])
        self.contracts.setdefault(cid, {})[version] = contract
        folder = self.contracts_dir / cid
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            (folder / f"v{version}.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")

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

    # ------------------------------------------------------------------ audit

    def audit_streams(self) -> list[str]:
        return sorted(p.stem for p in self.audit_dir.glob("*.jsonl"))
