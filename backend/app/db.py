"""Database schema and engine (SQLAlchemy Core): SQLite by default, PostgreSQL in production via DATABASE_URL.

Tables:
  runs              one row per negotiation / RFQ / renegotiation (the run record as JSON)
  run_events        the run's event history, one row per event, ordered by seq
  contracts         every contract version (amendments are new versions)
  telemetry_events  webhook event ids already processed (idempotency survives restarts)
  audit_entries     the hash-chained audit ledgers, one row per entry
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import JSON, Column, Engine, ForeignKey, Integer, MetaData, String, Table, create_engine, event

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(20), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("created_at", String(40), nullable=False, index=True),
    Column("record", JSON, nullable=False),
)

run_events = Table(
    "run_events", metadata,
    Column("run_id", String(64), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("event", JSON, nullable=False),
)

contracts = Table(
    "contracts", metadata,
    Column("contract_id", String(64), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("created_at", String(40), nullable=False),
    Column("document", JSON, nullable=False),
)

telemetry_events = Table(
    "telemetry_events", metadata,
    Column("event_id", String(128), primary_key=True),
    Column("contract_id", String(64)),
    Column("received_at", String(40), nullable=False),
)

audit_entries = Table(
    "audit_entries", metadata,
    Column("stream", String(64), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("hash", String(64), nullable=False),
    Column("entry", JSON, nullable=False),
)


def normalise_url(url: str) -> str:
    """Hosted Postgres URLs (Neon, Render, Heroku) come as postgres:// or postgresql://; use the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    url = normalise_url(url)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[1] if "///" in url else ""
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _record) -> None:  # enforce foreign keys; WAL keeps readers off the writer
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_recycle=300)
