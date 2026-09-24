"""Command-line entry point: ``python -m agents.cli <command>``.

Commands:
  list                         list preset scenarios
  run <scenario> [--red-team]  bilateral negotiation -> signed contract (JSON + PDF) + audit ledger
  rfq <scenario>               multi-vendor RFQ -> Pareto award -> contract for the winner
  rego <scenario> [--role r]   print the OPA/Rego guardrail compiled from a sealed envelope
  verify <contract.json>       verify a contract's hash and Ed25519 signatures
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from .contract.compiler import verify_contract
from .contract.pdf import render_contract_pdf
from .core.models import Action
from .guardrails.arbiter import LegalArbiter
from .guardrails.rego import compile_rego
from .platform import NegotiationPlatform
from .scenarios import get_scenario, load_scenarios

C = {"buyer": "\033[96m", "supplier": "\033[93m", "arbiter": "\033[95m", "ok": "\033[92m", "bad": "\033[91m",
     "dim": "\033[2m", "end": "\033[0m"}


def _c(key: str, text: str) -> str:
    return f"{C[key]}{text}{C['end']}" if sys.stdout.isatty() else text


def _save_contract(contract: dict, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"{contract['contract_id']}_v{contract['version']}"
    json_path = base.with_suffix(".json")
    pdf_path = base.with_suffix(".pdf")
    json_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    pdf_path.write_bytes(render_contract_pdf(contract))
    return json_path, pdf_path


def _print_transcript(session) -> None:
    for t in session.turns:
        who = _c(t.actor, f"{t.actor.upper():8}")
        flag = "" if t.verdict_status == "approved" else _c("arbiter", f" [{t.verdict_status}]")
        src = "" if t.source == "engine" else _c("dim", f" ({t.source})")
        print(f"R{t.round:>2} {who} {t.action.value.upper():7}{flag}{src}  "
              f"{_c('dim', f'u_b={t.u_buyer} u_s={t.u_supplier}')}")
        print(f"      {t.message}")
    for e in session.events:
        if e.type in ("guardrail_block", "deadlock_detected", "mediation_proposal", "mediation_failed"):
            print(_c("arbiter", f"  >> {e.type}: ") + json.dumps({k: v for k, v in e.data.items()
                                                                 if k not in ("negotiation_id", "supplier_id")})[:300])


async def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    platform = NegotiationPlatform(data_dir=out)
    scenario = get_scenario(args.scenario)
    session = platform.new_session(scenario, negotiation_id=f"NEG-{uuid.uuid4().hex[:8].upper()}",
                                   llm_mode=args.llm, red_team=args.red_team, max_rounds=args.rounds)
    result = await session.run()
    _print_transcript(session)
    print(_c("ok" if result.agreed_terms else "bad", f"\nOUTCOME: {result.status.value} - {result.reason}"))
    if result.agreed_terms:
        print("Agreed terms:", json.dumps(result.agreed_terms))
        print(f"Utilities: buyer={result.u_buyer} supplier={result.u_supplier}  "
              f"Pareto gap={result.efficiency.get('pareto_gap')}  joint gain vs split="
              f"{result.efficiency.get('joint_gain_vs_split')}")
        contract = await platform.compile_contract(session)
        json_path, pdf_path = _save_contract(contract, out / "contracts")
        print(f"Contract {contract['contract_id']} ({contract['status']}) -> {json_path}, {pdf_path}")
    print(f"Guardrail interventions: {result.interventions} (blocked {result.blocked_moves}, "
          f"redactions {result.redactions})")
    if session.ledger is not None:
        await session.ledger.flush()
    verification = session.ledger.verify() if session.ledger else {}
    print(f"Audit ledger: {len(session.ledger.entries) if session.ledger else 0} entries, "
          f"chain {'intact' if verification.get('valid') else 'BROKEN'}")
    await platform.aclose()
    return 0


async def cmd_rfq(args: argparse.Namespace) -> int:
    out = Path(args.out)
    platform = NegotiationPlatform(data_dir=out)
    scenario = get_scenario(args.scenario)
    orch = platform.new_rfq(scenario, rfq_id=f"RFQ-{uuid.uuid4().hex[:6].upper()}", llm_mode=args.llm,
                            leverage=not args.no_leverage)
    result = await orch.run()
    for c in result.candidates:
        mark = _c("ok", "WIN ") if c.supplier_id == result.winner else "    "
        print(f"{mark}{c.supplier_name:32} {c.status.value:18} u_b={c.u_buyer} u_s={c.u_supplier} "
              f"value={c.total_value} efficient={c.pareto_efficient} dominated_by={c.dominated_by}")
    print("\n" + result.rationale)
    if result.winner:
        contract = await platform.compile_contract(orch.sessions[result.winner])
        json_path, pdf_path = _save_contract(contract, out / "contracts")
        print(f"Contract {contract['contract_id']} ({contract['status']}) -> {json_path}")
    await platform.aclose()
    return 0


def cmd_rego(args: argparse.Namespace) -> int:
    scenario = get_scenario(args.scenario)
    supplier = scenario.supplier(args.supplier)
    arbiter = LegalArbiter(scenario, supplier)
    org = scenario.context.buyer.name if args.role == "buyer" else supplier.party.name
    print(compile_rego(arbiter.utils[args.role], arbiter.rulebook, org, arbiter.commitments()[args.role]))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    contract = json.loads(Path(args.path).read_text(encoding="utf-8"))
    report = verify_contract(contract)
    print(json.dumps(report, indent=2))
    return 0 if report["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.cli", description="Autonomous B2B negotiation platform")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    p = sub.add_parser("run")
    p.add_argument("scenario")
    p.add_argument("--red-team", action="store_true")
    p.add_argument("--llm", default="auto", choices=["auto", "offline", "lyzr"])
    p.add_argument("--rounds", type=int, default=None)
    p.add_argument("--out", default="data/cli")
    p = sub.add_parser("rfq")
    p.add_argument("scenario", nargs="?", default="steel_rfq")
    p.add_argument("--llm", default="auto", choices=["auto", "offline", "lyzr"])
    p.add_argument("--no-leverage", action="store_true")
    p.add_argument("--out", default="data/cli")
    p = sub.add_parser("rego")
    p.add_argument("scenario")
    p.add_argument("--role", default="buyer", choices=["buyer", "supplier"])
    p.add_argument("--supplier", default=None)
    p = sub.add_parser("verify")
    p.add_argument("path")
    args = parser.parse_args(argv)

    if args.command == "list":
        for sc in load_scenarios().values():
            print(f"{sc.id:24} {sc.mode:9} {sc.title}")
        return 0
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "rfq":
        return asyncio.run(cmd_rfq(args))
    if args.command == "rego":
        return cmd_rego(args)
    if args.command == "verify":
        return cmd_verify(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
