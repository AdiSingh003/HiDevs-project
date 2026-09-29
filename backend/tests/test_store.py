"""The database store: SQLite always, PostgreSQL too when TEST_DATABASE_URL is set (CI runs both)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from agents.audit.ledger import AuditLedger
from backend.app.db import normalise_url
from backend.app.store import RunRecord, Store

from .conftest import TEST_DATABASE_URL, reset_database


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{(tmp_path / 'store.db').as_posix()}"
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run the store against PostgreSQL")
    reset_database(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


def contract(cid: str, version: int, created_at: str) -> dict:
    return {"contract_id": cid, "version": version, "created_at": created_at, "status": "executed", "title": "Spot PO",
            "parties": {"buyer": {"name": "Orion"}, "supplier": {"name": "Vega"}},
            "commercial_terms": {"currency": "USD", "total_value": 186000.0},
            "negotiation": {"negotiation_id": "NEG-1"}, "rfq": {"scenario_id": "semiconductor_spot_po"}}


def test_runs_and_events_survive_a_restart(db_url):
    store = Store(db_url)
    done = RunRecord(id="NEG-1", kind="negotiation", scenario_id="s", status="agreement", created_at="2026-01-01T00:00:00Z")
    store.save_run(done, [{"id": 1, "type": "negotiation_started"}, {"id": 2, "type": "run_finished"}])
    store.put_run(RunRecord(id="NEG-2", kind="negotiation", scenario_id="s", created_at="2026-01-02T00:00:00Z"))
    store.save_run(done, [{"id": 1, "type": "negotiation_started"}, {"id": 2, "type": "turn"}, {"id": 3, "type": "run_finished"}])
    store.close()

    again = Store(db_url)
    assert again.runs["NEG-1"].status == "agreement"
    assert [e["type"] for e in again.run_events["NEG-1"]] == ["negotiation_started", "turn", "run_finished"]
    assert again.runs["NEG-2"].status == "interrupted"  # it was still running when the process stopped
    assert [r.id for r in again.list_runs("negotiation")] == ["NEG-2", "NEG-1"]  # newest first
    again.close()
    assert Store(db_url).runs["NEG-2"].status == "interrupted"  # and that correction was written back


def test_contract_versions_persist_and_the_newest_is_current(db_url):
    store = Store(db_url)
    store.save_contract(contract("CTR-1", 1, "2026-01-01T00:00:00Z"))
    store.save_contract(contract("CTR-1", 2, "2026-01-03T00:00:00Z"))
    store.save_contract(contract("CTR-2", 1, "2026-01-02T00:00:00Z"))
    store.save_contract({**contract("CTR-2", 1, "2026-01-02T00:00:00Z"), "status": "pending_cfo_approval"})  # update
    store.close()

    again = Store(db_url)
    assert again.contract("CTR-1")["version"] == 2 and again.contract("CTR-1", 1)["version"] == 1
    assert [c["version"] for c in again.contract_versions("CTR-1")] == [1, 2]
    assert again.contract("CTR-2")["status"] == "pending_cfo_approval"
    assert [c["contract_id"] for c in again.list_contracts()] == ["CTR-1", "CTR-2"]
    assert again.contract("CTR-404") is None
    again.close()


def test_telemetry_events_are_processed_once_even_across_restarts(db_url):
    store = Store(db_url)
    assert store.mark_event_seen("evt-1", "CTR-1") is True
    assert store.mark_event_seen("evt-1", "CTR-1") is False
    store.close()
    again = Store(db_url)
    assert "evt-1" in again.seen_events and again.mark_event_seen("evt-1") is False
    again.close()


def test_audit_chain_round_trips_through_the_database_and_still_verifies(db_url):
    store = Store(db_url)
    ledger = AuditLedger("NEG-9", backend=store.ledger_backend)
    for i in range(5):
        ledger.append("turn", "buyer", {"round": i, "offer": {"unit_price": 3.9 - i / 100}})
    head = ledger.head

    reloaded = AuditLedger("NEG-9", backend=store.ledger_backend)
    assert [e.hash for e in reloaded.entries] == [e.hash for e in ledger.entries] and reloaded.head == head
    assert reloaded.verify()["valid"]
    assert not AuditLedger.verify_entries(reloaded.tampered_copy(3))["valid"]
    reloaded.append("contract_compiled", "legal_arbiter", {"v": 1})  # continues the same chain
    assert AuditLedger("NEG-9", backend=store.ledger_backend).verify() == {**reloaded.verify()}
    assert store.audit_streams() == ["NEG-9"]

    # append-only at the database level too: an entry number can't be written twice
    with pytest.raises(IntegrityError):
        store.ledger_backend.append(ledger.entries[0].model_dump(mode="json"))
    store.close()


@pytest.mark.parametrize(("url", "expected"), [
    ("postgres://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
    ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ("sqlite:///data/negotiator.db", "sqlite:///data/negotiator.db"),
])
def test_hosted_postgres_urls_use_the_psycopg_driver(url, expected):
    assert normalise_url(url) == expected
