"""The operator tools: ``agents.lyzr.doctor`` (live health check) and ``agents.lyzr.provision`` (set-up CLI)."""

from __future__ import annotations

import httpx

from agents.lyzr import doctor, provision
from agents.lyzr.client import LyzrAgentClient
from agents.lyzr.rai import LyzrRAIClient

from .fakes import FakeLyzr, opa_decision, settings


def clients(handler):
    s = settings(lyzr_max_retries=0)
    t = httpx.MockTransport(handler)
    return s, LyzrAgentClient(s, transport=t, sync_transport=t), LyzrRAIClient(s, transport=t)


class TestDoctor:
    async def test_every_check_passes_against_a_healthy_lyzr(self):
        fake = FakeLyzr(provisioned=True)
        s, client, rai = clients(fake)
        results = await doctor.run_checks(s, full=True, client=client, rai=rai)
        assert [(name, ok) for name, ok, _ in results] == [
            ("Agent API / provisioned agents", True), ("Negotiator brain (buyer agent)", True),
            ("Safe AI RAI (per-party policies)", True), ("Safe AI OPA tool-call guardrail", True),
            ("AIMS event log + anchor read-back", True), ("Lyzr Automata drafting pipeline", True)]
        details = {name: detail for name, _, detail in results}
        assert "11 envelope guardrails" in details["Safe AI OPA tool-call guardrail"]
        assert "15/15 LLM clauses kept" in details["Lyzr Automata drafting pipeline"]

    async def test_unprovisioned_agents_are_reported_missing(self):
        s, client, rai = clients(FakeLyzr())  # nothing provisioned
        results = {name: (ok, detail) for name, ok, detail in await doctor.run_checks(s, client=client, rai=rai)}
        assert results["Agent API / provisioned agents"][0] is False
        assert "missing" in results["Agent API / provisioned agents"][1]
        assert "Lyzr Automata drafting pipeline" not in results  # only with --full

    async def test_an_outage_is_reported_per_check_not_raised(self):
        s, client, rai = clients(lambda r: httpx.Response(503, json={"detail": "down"}))
        results = await doctor.run_checks(s, client=client, rai=rai)
        assert len(results) == 5 and not any(ok for _, ok, _ in results)
        assert all("LyzrError" in detail or "no parseable" in detail or "missing" in detail for _, _, detail in results)

    def test_main_without_a_key_explains_and_exits_2(self, monkeypatch, capsys):
        monkeypatch.setenv("LYZR_API_KEY", "")
        assert doctor.main([]) == 2
        assert "LYZR_API_KEY is not set" in capsys.readouterr().err

    def test_main_prints_a_pass_fail_table(self, monkeypatch, capsys):
        monkeypatch.setenv("LYZR_API_KEY", "test-key")

        async def fake_checks(settings, full):
            return [("Agent API", True, "all present"), ("Safe AI OPA", False, "breach allowed")]

        monkeypatch.setattr(doctor, "run_checks", fake_checks)
        assert doctor.main(["--full"]) == 1
        out = capsys.readouterr().out
        assert "PASS  Agent API" in out and "FAIL  Safe AI OPA" in out and "1/2 checks passed" in out


class TestProvisionCli:
    def test_dry_run_prints_every_payload_without_calling_lyzr(self, capsys):
        assert provision.main(["--dry-run", "--scenario", "cold_chain_sla"]) == 0
        out = capsys.readouterr().out
        assert out.count("# POST /v3/agents/") == 1 and out.count("# POST /v1/rai/policies") == 3
        assert out.count("# POST /v1/opa-policies") == 2 and "b2b-guardrail-buyer-" in out

    def test_without_a_key_it_explains_and_exits_2(self, monkeypatch, capsys):
        monkeypatch.setenv("LYZR_API_KEY", "")
        assert provision.main([]) == 2
        assert "--dry-run" in capsys.readouterr().err

    def test_write_env_updates_only_the_given_keys(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("LYZR_API_KEY", "test-key")

        async def fake_provision(settings, scenarios):
            return {"LYZR_BUYER_AGENT_ID": "agent-1", "LYZR_OPA_GUARDRAILS": "true"}

        monkeypatch.setattr(provision, "provision", fake_provision)
        env = tmp_path / ".env"
        env.write_text("# keep me\nLYZR_API_KEY=secret\nLYZR_BUYER_AGENT_ID=old\n", encoding="utf-8")
        assert provision.main(["--write-env", str(env), "--aims-mode", "agent_session"]) == 0
        lines = env.read_text(encoding="utf-8").splitlines()
        assert lines[:3] == ["# keep me", "LYZR_API_KEY=secret", "LYZR_BUYER_AGENT_ID=agent-1"]
        assert "LYZR_OPA_GUARDRAILS=true" in lines and "AIMS_MODE=agent_session" in lines
        assert "Updated" in capsys.readouterr().out

    def test_without_write_env_it_prints_the_ids(self, monkeypatch, capsys):
        monkeypatch.setenv("LYZR_API_KEY", "test-key")

        async def fake_provision(settings, scenarios):
            return {"LYZR_BUYER_AGENT_ID": "agent-1"}

        monkeypatch.setattr(provision, "provision", fake_provision)
        assert provision.main([]) == 0
        assert "LYZR_BUYER_AGENT_ID=agent-1" in capsys.readouterr().out


def test_fake_opa_applies_the_compiled_rules():
    """The fake OPA must deny exactly what the compiled guardrail denies, or the doctor test proves nothing."""
    from agents.guardrails.arbiter import LegalArbiter
    from agents.scenarios import get_scenario
    sc = get_scenario("semiconductor_spot_po")
    arb = LegalArbiter(sc, sc.suppliers[0])
    buyer, supplier = arb.guardrail("buyer").rego, arb.guardrail("supplier").rego
    ideal_b, ideal_s = arb.utils["buyer"].ideal_terms(), arb.utils["supplier"].ideal_terms()
    assert opa_decision(buyer, ideal_b) is None and opa_decision(supplier, ideal_s) is None
    assert opa_decision(buyer, {**ideal_b, "unit_price": 4.15}).startswith(("POLICY-MANDATE", "POLICY-BUDGET"))
    assert opa_decision(supplier, {**ideal_s, "unit_price": 3.0}).startswith("POLICY-MANDATE")
    assert opa_decision(buyer, {k: v for k, v in ideal_b.items() if k != "warranty_months"}).startswith("SCHEMA")
