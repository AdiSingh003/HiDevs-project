"""REST + SSE API tests (offline mode, speed 0)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app import security

from .conftest import SECRET, make_app


def wait_for(client, run_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = client.get(f"/api/runs/{run_id}").json()
        if rec["status"] not in ("running",):
            return rec
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def signed_post(client, payload: dict, secret: str = SECRET, ts: int | None = None):
    body = json.dumps(payload).encode()
    stamp, sig = security.sign(secret, body, ts)
    return client.post("/api/webhooks/telemetry", content=body,
                       headers={"Content-Type": "application/json", security.TIMESTAMP_HEADER: stamp,
                                security.SIGNATURE_HEADER: sig})


class TestMeta:
    def test_health_status_scenarios(self, client):
        assert client.get("/api/health").json()["status"] == "ok"
        status = client.get("/api/status").json()
        assert status["llm_modes"] == ["offline"] and status["safe_ai"]["local_rules"]
        assert status["automata"]["engine"] == "lyzr-automata LinearSyncPipeline"
        ids = {s["id"] for s in client.get("/api/scenarios").json()}
        assert ids == {"semiconductor_spot_po", "cold_chain_sla", "lithium_hardball", "steel_rfq"}
        full = client.get("/api/scenarios/steel_rfq").json()
        assert len(full["suppliers"]) == 3 and "mandate" in full["buyer"]
        assert client.get("/api/scenarios/nope").status_code == 404
        rego = client.get("/api/scenarios/semiconductor_spot_po/rego", params={"role": "buyer"})
        assert rego.status_code == 200 and "package lyzr.procurement." in rego.text
        assert "IN" in client.get("/api/rulebook").json()["jurisdictions"]


class TestNegotiations:
    def test_negotiation_produces_contract(self, client, negotiated):
        assert negotiated["status"] == "agreement" and negotiated["contract_id"]
        assert negotiated["result"]["agreed_terms"]
        runs = client.get("/api/runs").json()
        assert runs[0]["id"] == negotiated["id"] and runs[0]["contract_id"] == negotiated["contract_id"]

    def test_async_start_then_poll(self, client):
        r = client.post("/api/negotiations", json={"scenario_id": "cold_chain_sla", "speed_ms": 0})
        assert r.status_code == 201 and r.json()["status"] == "running"
        rec = wait_for(client, r.json()["id"])
        assert rec["status"] in ("agreement", "mediated_agreement") and rec["contract_id"]

    def test_validation_errors(self, client):
        assert client.post("/api/negotiations", json={"scenario_id": "steel_rfq"}).status_code == 400
        assert client.post("/api/negotiations", json={"scenario_id": "unknown"}).status_code == 404
        assert client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po",
                                                      "llm_mode": "lyzr"}).status_code == 400
        bad = client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po",
                                                     "buyer": {"mandate": {"unit_price": {"weight": -1}}}})
        assert bad.status_code == 422
        inconsistent = client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po",
                                                              "buyer": {"budget_cap": 170000}})
        assert inconsistent.status_code == 400 and "ideal" in inconsistent.json()["detail"]

    def test_envelope_overrides_change_the_outcome(self, client):
        r = client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po", "wait": True,
                                                   "buyer": {"mandate": {"unit_price": {"limit": 3.6}},
                                                             "budget_cap": 180000}})
        assert r.json()["status"] == "no_deal" and r.json()["contract_id"] is None

    def test_red_team_run_reports_interventions(self, client):
        r = client.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po", "red_team": True,
                                                   "wait": True})
        res = r.json()["result"]
        assert res["blocked_moves"] == 3 and res["redactions"] >= 4


class TestEvents:
    def test_json_views_isolate_private_events(self, client, negotiated):
        rid = negotiated["id"]
        god = client.get(f"/api/runs/{rid}/events", params={"format": "json"}).json()
        pub = client.get(f"/api/runs/{rid}/events", params={"format": "json", "view": "public"}).json()
        sup = client.get(f"/api/runs/{rid}/events", params={"format": "json", "view": "supplier"}).json()
        assert len(pub) < len(sup) < len(god)
        assert all("public" in e["visibility"] or "supplier" in e["visibility"] for e in sup)
        assert not any(e["type"] in ("analytics", "bargaining_analysis") for e in sup)
        assert {e["type"] for e in god} >= {"negotiation_started", "turn", "analytics", "agreement",
                                            "contract_compiled", "run_finished"}

    def test_sse_replays_finished_run(self, client, negotiated):
        with client.stream("GET", f"/api/runs/{negotiated['id']}/events", params={"view": "buyer"}) as s:
            assert s.headers["content-type"].startswith("text/event-stream")
            text = "".join(s.iter_text())
        events = [line[len("event: "):] for line in text.splitlines() if line.startswith("event: ")]
        assert events[0] == "negotiation_started" and events[-1] == "end"
        datas = [json.loads(line[len("data: "):]) for line in text.splitlines()
                 if line.startswith("data: ") and line != "data: {}"]
        ids = [d["id"] for d in datas]  # stream ids: ascending, with gaps where supplier-private events were filtered
        assert ids == sorted(set(ids)) and len(ids) < ids[-1]
        assert not any(d["type"] == "turn_private" and d["data"]["actor"] == "supplier" for d in datas)

    def test_sse_resume_after_last_event_id(self, client, negotiated):
        with client.stream("GET", f"/api/runs/{negotiated['id']}/events",
                           headers={"Last-Event-ID": "10"}) as s:
            text = "".join(s.iter_text())
        ids = [int(line[4:]) for line in text.splitlines() if line.startswith("id: ")]
        assert ids and min(ids) == 11


class TestContracts:
    def test_contract_endpoints(self, client, negotiated):
        cid = negotiated["contract_id"]
        listing = client.get("/api/contracts").json()
        assert listing[0]["contract_id"] == cid and listing[0]["status"] == "executed"
        doc = client.get(f"/api/contracts/{cid}").json()
        assert doc["terms"] == negotiated["result"]["agreed_terms"]
        pdf = client.get(f"/api/contracts/{cid}/pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert client.post(f"/api/contracts/{cid}/verify").json()["valid"]
        doc["commercial_terms"]["total_value"] = 1
        assert not client.post("/api/contracts/verify-document", json=doc).json()["valid"]
        sla = client.post(f"/api/contracts/{cid}/sla", json={"days_late": 3, "monthly_otif_pct": 90}).json()
        assert sla["delay"]["amount"] > 0 and sla["otif"]["breach"]
        assert client.get("/api/contracts/CTR-NOPE").status_code == 404

    def test_cfo_approval_flow(self, client):
        rec = client.post("/api/negotiations", json={"scenario_id": "lithium_hardball", "wait": True}).json()
        cid = rec["contract_id"]
        assert client.get(f"/api/contracts/{cid}").json()["status"] == "pending_cfo_approval"
        r = client.post(f"/api/contracts/{cid}/approve", json={"approver_name": "Jane CFO"})
        assert r.status_code == 200 and r.json()["status"] == "executed"
        assert r.json()["verification"]["fully_executed"]
        assert client.post(f"/api/contracts/{cid}/approve", json={}).status_code == 409
        audit = client.get(f"/api/audit/{rec['id']}").json()
        assert audit["entries"][-1]["event_type"] == "cfo_approved" and audit["verification"]["valid"]


class TestRFQ:
    def test_rfq_awards_and_compiles_winner_contract(self, client):
        r = client.post("/api/rfq", json={"scenario_id": "steel_rfq", "wait": True})
        assert r.status_code == 201
        rec = r.json()
        assert rec["status"] == "awarded" and rec["contract_id"]
        winner = rec["result"]["winner"]
        contract = client.get(f"/api/contracts/{rec['contract_id']}").json()
        assert contract["parties"]["supplier"]["supplier_id"] == winner
        events = client.get(f"/api/runs/{rec['id']}/events", params={"format": "json"}).json()
        lanes = {e["data"].get("supplier_id") for e in events if e["type"] == "turn"}
        assert lanes == {"ferro", "krupa", "sagar"}
        assert any(e["type"] == "rfq_award" for e in events)
        assert client.post("/api/rfq", json={"scenario_id": "semiconductor_spot_po"}).status_code == 400


class TestTelemetryWebhook:
    def test_signed_weather_event_triggers_amendment(self, client, negotiated):
        cid = negotiated["contract_id"]
        r = signed_post(client, {"contract_id": cid, "event_type": "severe_weather", "severity": 4,
                                 "expected_delay_days": 6, "source": "imd", "region": "Chennai",
                                 "description": "Cyclone - port closed", "event_id": "EVT-1"})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["assessment"]["action"] == "renegotiate" and body["assessment"]["force_majeure"]
        rec = wait_for(client, body["renegotiation_id"])
        assert rec["status"] in ("agreement", "mediated_agreement") and rec["contract_version"] == 2
        versions = client.get(f"/api/contracts/{cid}/versions").json()
        assert [v["version"] for v in versions] == [1, 2]
        v2 = client.get(f"/api/contracts/{cid}").json()
        assert v2["parent_hash"] == versions[0]["content_hash"]
        assert v2["amendment"]["ld_waiver"] and "delivery_days" in v2["amendment"]["changed_terms"]
        assert client.post(f"/api/contracts/{cid}/verify").json()["valid"]
        # replaying the same event id is idempotent
        again = signed_post(client, {"contract_id": cid, "event_type": "severe_weather", "severity": 4,
                                     "expected_delay_days": 6, "event_id": "EVT-1"})
        assert again.json()["status"] == "duplicate"
        audit_types = [e["event_type"] for e in client.get(f"/api/audit/{negotiated['id']}").json()["entries"]]
        assert {"telemetry_received", "telemetry_assessed", "contract_amended"} <= set(audit_types)

    def test_below_threshold_event_applies_sla(self, client, negotiated):
        r = signed_post(client, {"contract_id": negotiated["contract_id"], "event_type": "carrier_delay",
                                 "severity": 2, "expected_delay_days": 3})
        body = r.json()
        assert body["assessment"]["action"] == "apply_sla" and body["renegotiation_id"] is None
        assert body["assessment"]["sla_estimate"]["amount"] > 0

    def test_signature_is_enforced(self, client, negotiated):
        payload = {"contract_id": negotiated["contract_id"], "event_type": "severe_weather"}
        assert signed_post(client, payload, secret="wrong").status_code == 401
        assert signed_post(client, payload, ts=int(time.time()) - 3600).status_code == 401  # replay window
        assert client.post("/api/webhooks/telemetry", json=payload).status_code == 401
        assert signed_post(client, {"contract_id": "CTR-NOPE", "event_type": "severe_weather"}).status_code == 404
        assert signed_post(client, {"contract_id": "x", "event_type": "alien_invasion"}).status_code == 422

    def test_simulator_signs_and_dispatches(self, client, negotiated):
        r = client.post("/api/telemetry/simulate", json={"contract_id": negotiated["contract_id"],
                                                          "event_type": "commodity_price_spike",
                                                          "price_index": "LME Copper", "price_index_change_pct": 12})
        assert r.status_code == 202
        assert r.json()["signed_headers"][security.SIGNATURE_HEADER].startswith("sha256=")
        rec = wait_for(client, r.json()["renegotiation_id"])
        assert rec["status"] in ("agreement", "mediated_agreement", "no_deal")


class TestAudit:
    def test_audit_endpoints(self, client, negotiated):
        rid = negotiated["id"]
        streams = client.get("/api/audit").json()
        assert any(s["stream"] == rid and s["valid"] for s in streams)
        entries = client.get(f"/api/audit/{rid}").json()
        assert entries["verification"]["valid"] and entries["entries"][0]["prev_hash"] == "0" * 64
        tampered = client.get(f"/api/audit/{rid}/verify", params={"tamper_seq": 7}).json()
        assert not tampered["valid"] and tampered["broken_at"] == 7
        assert client.get("/api/audit/NOPE").status_code == 404

    def test_aims_reconcile_reports_when_lyzr_is_off(self, client, negotiated):
        report = client.get(f"/api/audit/{negotiated['id']}/aims").json()
        assert report["available"] is False and "AIMS" in report["reason"]
        assert client.get("/api/audit/NOPE/aims").status_code == 404


def test_state_survives_restart(tmp_path):
    with TestClient(make_app(tmp_path)) as c:
        rec = c.post("/api/negotiations", json={"scenario_id": "semiconductor_spot_po", "wait": True}).json()
    with TestClient(make_app(tmp_path)) as c:
        assert c.get(f"/api/runs/{rec['id']}").json()["status"] == "agreement"
        assert c.post(f"/api/contracts/{rec['contract_id']}/verify").json()["valid"]
        events = c.get(f"/api/runs/{rec['id']}/events", params={"format": "json"}).json()
        assert events[-1]["type"] == "run_finished"
        assert c.get(f"/api/audit/{rec['id']}/verify").json()["valid"]


@pytest.mark.parametrize("path", ["/", "/arena", "/contracts/CTR-1"])
def test_spa_fallback_serves_index(tmp_path, path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>arena</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    with TestClient(make_app(tmp_path, frontend_dist=dist)) as c:
        assert c.get(path).text == "<html>arena</html>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/nothing-here").status_code == 404
