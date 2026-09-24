"""Contract compiler (Lyzr Automata pipeline, signatures, PDF, executable SLA) and the hash-chained audit ledger."""

from __future__ import annotations

import copy
import json

import pytest

from agents.audit.ledger import GENESIS, AuditLedger
from agents.contract.automata_pipeline import DraftingPipeline, TemplateDraftingModel
from agents.contract.clauses import verify_clause
from agents.contract.compiler import ContractCompiler, content_hash, verify_contract
from agents.contract.pdf import render_contract_pdf
from agents.contract.signing import KeyStore
from agents.contract.sla import evaluate_delay, evaluate_otif
from agents.negotiation.engine import NegotiationSession
from agents.platform import NegotiationPlatform
from agents.lyzr.settings import LyzrSettings
from agents.scenarios import get_scenario


@pytest.fixture
def platform(tmp_path):
    return NegotiationPlatform(tmp_path, LyzrSettings(_env_file=None))


async def negotiated_contract(platform, scenario_id="semiconductor_spot_po"):
    session = platform.new_session(get_scenario(scenario_id), negotiation_id=f"NEG-{scenario_id[:6].upper()}")
    await session.run()
    return session, await platform.compile_contract(session)


class TestContract:
    async def test_contract_structure_and_signatures(self, platform):
        session, c = await negotiated_contract(platform)
        assert c["schema"] == "hidevs.contract/v1" and c["status"] == "executed"
        assert c["terms"] == session.agreed_terms
        assert c["commercial_terms"]["total_value"] == pytest.approx(c["terms"]["unit_price"] * 50000)
        assert [cl["id"] for cl in c["clauses"]][:3] == ["definitions", "scope", "price_payment"]
        assert len(c["clauses"]) == 15
        assert {s["role"] for s in c["integrity"]["signatures"]} == {"legal_arbiter", "supplier", "buyer"}
        assert c["drafting"]["engine"] == "lyzr-automata LinearSyncPipeline"
        assert c["drafting"]["review"]["approved"] is True
        assert c["negotiation"]["audit_head"] == session.ledger.entries[-2].hash  # head before contract entry
        report = verify_contract(c)
        assert report["valid"] and report["fully_executed"]

    async def test_msme_statutory_clause_is_drafted(self, platform):
        _, c = await negotiated_contract(platform)
        payment = next(cl for cl in c["clauses"] if cl["id"] == "price_payment")["text"]
        assert "Section 15 of the MSMED Act, 2006" in payment

    async def test_tampering_is_detected(self, platform):
        _, c = await negotiated_contract(platform)
        forged = copy.deepcopy(c)
        forged["terms"]["unit_price"] = 1.0
        report = verify_contract(forged)
        assert not report["hash_valid"] and not report["valid"]
        # re-hashing the forgery does not help: signatures no longer match
        forged["integrity"]["content_hash"] = content_hash(forged)
        report = verify_contract(forged)
        assert report["hash_valid"] and not report["all_signatures_valid"]

    async def test_cfo_co_signature_flow(self, platform):
        _, c = await negotiated_contract(platform, "lithium_hardball")
        assert c["status"] == "pending_cfo_approval"
        report = verify_contract(c)
        assert report["valid"] and not report["fully_executed"] and report["missing_signatures"] == ["buyer_cfo"]
        platform.compiler.approve_cfo(c, "Jane Doe")
        assert c["status"] == "executed" and verify_contract(c)["fully_executed"]
        with pytest.raises(ValueError):
            platform.compiler.approve_cfo(c)

    async def test_standard_terms_fill_non_negotiated_issues(self, platform):
        _, c = await negotiated_contract(platform, "lithium_hardball")
        assert "late_penalty_pct_per_day" not in c["terms"]
        assert c["standard_terms"] == {"late_penalty_pct_per_day": 0.5, "warranty_months": 12}
        ld = next(cl for cl in c["clauses"] if cl["id"] == "liquidated_damages")["text"]
        assert "0.50% of the value of the delayed Goods" in ld
        assert c["drafting"]["review"]["approved"], c["drafting"]["review"]["findings"]  # standard terms are legitimate

    @pytest.mark.parametrize("scenario_id", ["semiconductor_spot_po", "cold_chain_sla", "lithium_hardball"])
    async def test_every_scenario_contract_passes_legal_review(self, platform, scenario_id):
        _, c = await negotiated_contract(platform, scenario_id)
        assert c["drafting"]["review"] == {**c["drafting"]["review"], "approved": True, "findings": []}
        assert verify_contract(c)["valid"]

    async def test_pdf_renders(self, platform):
        _, c = await negotiated_contract(platform)
        pdf = render_contract_pdf(c)
        assert pdf.startswith(b"%PDF-") and len(pdf) > 5000 and b"%%EOF" in pdf[-1024:]

    async def test_signing_keys_persist(self, tmp_path):
        a = KeyStore(tmp_path)
        b = KeyStore(tmp_path)
        assert a.public_key_b64("buyer:X") == b.public_key_b64("buyer:X")
        assert a.public_key_b64("buyer:X") != a.public_key_b64("supplier:Y")


class TestDrafting:
    def test_clause_number_drift_is_caught(self):
        terms = {"late_penalty_pct_per_day": 0.35, "penalty_cap_pct": 10.0}
        dec = {"late_penalty_pct_per_day": 2, "penalty_cap_pct": 1}
        ok = "LDs of 0.35% per day capped at 10.0% of the Contract Value."
        assert verify_clause("liquidated_damages", ok, terms, dec) == []
        drift = "LDs of 2% per day capped at 10% of the Contract Value."
        problems = verify_clause("liquidated_damages", drift, terms, dec)
        assert any("missing" in p for p in problems) and any("2%" in p for p in problems)

    def test_llm_drafts_with_wrong_numbers_are_replaced(self, platform):
        """Simulate an online drafter that invents numbers: canonical clauses must be substituted."""
        pipeline = platform.compiler.drafting
        good_payload = self._payload(platform)

        class DriftingModel(TemplateDraftingModel):
            def generate_text(self, task_id=None, system_persona=None, prompt=None, messages=None):
                out = super().generate_text(task_id, system_persona, prompt, messages)
                if "[TASK:draft_clauses]" in (prompt or ""):
                    data = json.loads(out)
                    for cl in data["clauses"]:
                        if cl["id"] == "liquidated_damages":
                            cl["text"] = "The Supplier pays LDs of 2% per day, uncapped."
                    return json.dumps(data)
                return out

        pipeline.online = True  # treat as an LLM drafter so substitutions are recorded
        pipeline._models = lambda session_id: (DriftingModel(), TemplateDraftingModel(), TemplateDraftingModel())
        result = pipeline.run(good_payload, "test-session")
        ld = next(c for c in result.clauses if c["id"] == "liquidated_damages")
        assert ld["source"] == "canonical-template" and "0.25%" in ld["text"]
        assert result.substitutions and result.substitutions[0]["clause"] == "liquidated_damages"
        pipeline.online = False

    @staticmethod
    def _payload(platform):
        sc = get_scenario("semiconductor_spot_po")
        captured = {}
        original = platform.compiler.drafting.run

        def capture(payload, session_id):
            captured["payload"] = payload
            return original(payload, session_id)

        platform.compiler.drafting.run = capture
        terms = {"unit_price": 3.72, "delivery_days": 21.0, "payment_terms_days": 15.0, "sla_on_time_pct": 98.5,
                 "late_penalty_pct_per_day": 0.25, "penalty_cap_pct": 5.0, "warranty_months": 24.0}
        platform.compiler.compile(scenario=sc, supplier=sc.suppliers[0], terms=terms,
                                  negotiation={"rounds": 10, "outcome": "agreement", "interventions": 0},
                                  cfo_required=False)
        platform.compiler.drafting.run = original
        return captured["payload"]


class TestExecutableSLA:
    @pytest.fixture
    def contract(self):
        return {"commercial_terms": {"total_value": 186000.0, "currency": "USD"},
                "executable": {"sla_rules": [
                    {"id": "LD-DELAY", "params": {"rate_pct_per_day": 0.25, "cap_amount": 9300.0}},
                    {"id": "OTIF-CREDIT", "params": {"otif_target": 98.5}}]}}

    def test_delay_ld(self, contract):
        assert evaluate_delay(contract, 0)["amount"] == 0
        r = evaluate_delay(contract, 5)
        assert r["amount"] == pytest.approx(2325.0) and not r["capped"]

    def test_ld_cap_and_force_majeure(self, contract):
        r = evaluate_delay(contract, 40)
        assert r["capped"] and r["amount"] == pytest.approx(9300.0)
        assert evaluate_delay(contract, 40, ld_already_charged=9000)["amount"] == pytest.approx(300.0)
        assert evaluate_delay(contract, 10, force_majeure=True)["excused"]

    def test_otif_credit(self, contract):
        assert evaluate_otif(contract, 99.0, 100000)["credit"] == 0
        r = evaluate_otif(contract, 95.0, 100000)
        assert r["breach"] and r["credit"] == pytest.approx(3500.0)
        assert evaluate_otif(contract, 50.0, 100000)["credit"] == pytest.approx(10000.0)  # capped at 10%


class TestAuditLedger:
    def test_chain_and_tamper_detection(self, tmp_path):
        ledger = AuditLedger("S1", path=tmp_path / "S1.jsonl")
        for i in range(5):
            ledger.append("event", "tester", {"i": i})
        assert ledger.entries[0].prev_hash == GENESIS
        assert ledger.verify()["valid"]
        reloaded = AuditLedger("S1", path=tmp_path / "S1.jsonl")
        assert reloaded.head == ledger.head and reloaded.verify()["valid"]
        bad = ledger.tampered_copy(3)
        report = AuditLedger.verify_entries(bad)
        assert not report["valid"] and report["broken_at"] == 3
        dropped = ledger.entries[:2] + ledger.entries[3:]
        assert AuditLedger.verify_entries(dropped)["broken_at"] == 4

    async def test_negotiation_is_fully_audited(self, platform):
        session, contract = await negotiated_contract(platform)
        types = [e.event_type for e in session.ledger.entries]
        assert types[0] == "negotiation_started" and "envelopes_sealed" in types
        assert types.count("turn") == len(session.turns)
        assert types[-1] == "contract_compiled"
        assert session.ledger.verify()["valid"]
        assert (platform.data_dir / "audit" / f"{session.id}.jsonl").exists()

    async def test_aims_sink_receives_every_entry(self, tmp_path):
        pushed = []

        class Sink:
            name = "test-aims"

            async def push(self, entry):
                pushed.append(entry.seq)
                return True

        ledger = AuditLedger("S2", sink=Sink())
        for i in range(3):
            ledger.append("event", "tester", {"i": i})
        await ledger.flush()
        assert pushed == [1, 2, 3] and ledger.sync_summary() == {"synced": 3}


async def test_compiled_contract_matches_negotiated_terms_exactly():
    session = NegotiationSession(get_scenario("cold_chain_sla"))
    result = await session.run()
    compiler = ContractCompiler(KeyStore(), DraftingPipeline())
    sc = get_scenario("cold_chain_sla")
    c = compiler.compile(scenario=sc, supplier=sc.suppliers[0], terms=result.agreed_terms,
                         negotiation={"rounds": result.rounds, "outcome": result.status.value, "interventions": 0},
                         cfo_required=False)
    payment = next(cl for cl in c["clauses"] if cl["id"] == "price_payment")["text"]
    assert "Directive 2011/7/EU" in payment
    warranty = next(cl for cl in c["clauses"] if cl["id"] == "warranty")["text"]
    assert "services" in warranty  # logistics contract: no goods warranty issue negotiated
