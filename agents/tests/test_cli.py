"""The command-line entry point, end to end: every command, offline and deterministic."""

from __future__ import annotations

import json

import pytest

from agents import cli
from agents.lyzr.settings import LyzrSettings


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """The CLI reads .env; keep these runs offline whatever the developer's machine has configured."""
    monkeypatch.delenv("LYZR_API_KEY", raising=False)
    monkeypatch.setattr("agents.platform.get_lyzr_settings", lambda: LyzrSettings(_env_file=None))


def test_list_shows_every_preset(capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    for sid in ("semiconductor_spot_po", "cold_chain_sla", "lithium_hardball", "steel_rfq"):
        assert sid in out


def test_run_writes_a_signed_contract_pdf_and_an_intact_ledger(tmp_path, capsys):
    assert cli.main(["run", "semiconductor_spot_po", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "OUTCOME: agreement" in out and "chain intact" in out and "Pareto gap=" in out
    [contract_path] = (tmp_path / "contracts").glob("*.json")
    [pdf_path] = (tmp_path / "contracts").glob("*.pdf")
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    assert len(list((tmp_path / "audit").glob("*.jsonl"))) == 1  # the CLI keeps its ledger as a JSONL file

    assert cli.main(["verify", str(contract_path)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    forged = json.loads(contract_path.read_text(encoding="utf-8"))
    forged["commercial_terms"]["total_value"] += 1
    forged_path = tmp_path / "forged.json"
    forged_path.write_text(json.dumps(forged), encoding="utf-8")
    assert cli.main(["verify", str(forged_path)]) == 1
    assert json.loads(capsys.readouterr().out)["hash_valid"] is False


def test_red_team_run_shows_the_arbiter_intervening(tmp_path, capsys):
    assert cli.main(["run", "semiconductor_spot_po", "--red-team", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert ">> guardrail_block" in out and "OUTCOME: agreement" in out
    blocked = int(out.split("(blocked ")[1].split(",")[0])
    assert blocked > 0


def test_deadlock_is_mediated_and_needs_the_cfo(tmp_path, capsys):
    assert cli.main(["run", "lithium_hardball", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert ">> deadlock_detected" in out and "OUTCOME: mediated_agreement" in out and "pending_cfo_approval" in out


def test_rfq_awards_the_msme_lane_and_writes_its_contract(tmp_path, capsys):
    assert cli.main(["rfq", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "WIN Sagar Alloys (MSME)" in out and "Contract CTR-" in out
    assert len(list((tmp_path / "contracts").glob("*.json"))) == 1


def test_rfq_runs_without_competitive_leverage(tmp_path, capsys):
    assert cli.main(["rfq", "steel_rfq", "--no-leverage", "--out", str(tmp_path)]) == 0
    assert "Ferro Steel Works" in capsys.readouterr().out


def test_rego_prints_the_compiled_guardrail(capsys):
    assert cli.main(["rego", "cold_chain_sla", "--role", "supplier"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("package lyzr.procurement.supplier_") and "mandate_min :=" in out and "allow if" in out


def test_colour_is_only_used_on_a_terminal(monkeypatch):
    assert cli._c("ok", "done") == "done"
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert cli._c("ok", "done") == "\033[92mdone\033[0m"
