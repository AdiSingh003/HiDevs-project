"""Provision Lyzr Studio artefacts for the platform (idempotent: artefacts matched by name are updated in place).

Creates (via the published Lyzr APIs):
  * 6 agents   - POST {agent_base}/v3/agents/     (buyer, supplier, drafter, reviewer, CFO summary, audit scribe)
  * 3 policies - POST {rai_base}/v1/rai/policies   (shared + per-party Safe AI: each side knows only its own leak rules)
  * guardrails - POST {rai_base}/v1/opa-policies   (Rego compiled from every preset party's sealed envelope; named by
                                                    content hash, so envelopes seen later are registered on first use)

Usage:
  python -m agents.lyzr.provision --dry-run                 # print every payload, no network
  python -m agents.lyzr.provision --write-env .env          # create/reuse everything and update .env in place
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

from ..guardrails.arbiter import LegalArbiter
from ..guardrails.rego import Guardrail
from ..scenarios import get_scenario, load_scenarios
from .client import LyzrAgentClient, agent_id_from_response
from .rai import LyzrRAIClient, artefact_id, ids_by_name
from .settings import LyzrSettings

AGENTS_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = AGENTS_DIR / "config"
REQUIRED_AGENT_FIELDS = ("name", "provider_id", "model", "top_p", "temperature")


def build_agent_payloads(settings: LyzrSettings | None = None) -> list[dict[str, Any]]:
    config = json.loads((CONFIG_DIR / "lyzr_agents.json").read_text(encoding="utf-8"))
    defaults = dict(config["defaults"])
    if settings is not None:
        for attr, key in (("lyzr_provider_id", "provider_id"), ("lyzr_model", "model"),
                          ("lyzr_llm_credential_id", "llm_credential_id")):
            if getattr(settings, attr):
                defaults[key] = getattr(settings, attr)
    out = []
    for spec in config["agents"]:
        payload = {**defaults, **{k: v for k, v in spec.items() if k not in ("key", "env", "instructions_file")}}
        payload["agent_instructions"] = (AGENTS_DIR / spec["instructions_file"]).read_text(encoding="utf-8")
        missing = [f for f in REQUIRED_AGENT_FIELDS if payload.get(f) in (None, "")]
        if missing:
            raise ValueError(f"agent '{spec['key']}' is missing required fields {missing}")
        out.append({"key": spec["key"], "env": spec["env"], "payload": payload})
    return out


def build_rai_policy(role: str | None = None) -> dict[str, Any]:
    """Shared Safe AI policy, or a per-party one with that party's own leak keywords layered on top."""
    policy = json.loads((CONFIG_DIR / "rai_policy.json").read_text(encoding="utf-8"))
    if role is None:
        return policy
    extra = json.loads((CONFIG_DIR / "rai_party_keywords.json").read_text(encoding="utf-8"))[role]
    policy = copy.deepcopy(policy)
    policy["name"] = f"{policy['name']}-{role}"
    policy["description"] = f"{policy['description']} Party policy for the {role} negotiator."
    policy["keywords"]["keywords"] = policy["keywords"]["keywords"] + extra
    return policy


def build_guardrails(scenario_ids: list[str] | None = None) -> list[Guardrail]:
    """One guardrail per distinct sealed envelope (RFQ lanes share the buyer's), exactly as the arbiter seals it."""
    scenarios = [get_scenario(s) for s in scenario_ids] if scenario_ids else list(load_scenarios().values())
    out: dict[str, Guardrail] = {}
    for scenario in scenarios:
        for supplier in scenario.suppliers:
            arbiter = LegalArbiter(scenario, supplier)
            for role in ("buyer", "supplier"):
                g = arbiter.guardrail(role)
                out.setdefault(g.name, g)
    return list(out.values())


async def provision(settings: LyzrSettings, scenario_ids: list[str] | None = None,
                    agent_client: LyzrAgentClient | None = None,
                    rai_client: LyzrRAIClient | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    agent_client = agent_client or LyzrAgentClient(settings)
    rai_client = rai_client or LyzrRAIClient(settings)
    try:
        existing_agents = ids_by_name(await agent_client.list_agents())
        for item in build_agent_payloads(settings):
            name = item["payload"]["name"]
            if name in existing_agents:
                env[item["env"]] = existing_agents[name]
                await agent_client.update_agent(existing_agents[name], item["payload"])
                print(f"  synced agent {name!r}: {existing_agents[name]}")
                continue
            data = await agent_client.create_agent(item["payload"])
            agent_id = agent_id_from_response(data)
            if not agent_id:
                raise RuntimeError(f"could not read agent id from response: {json.dumps(data)[:300]}")
            env[item["env"]] = agent_id
            print(f"  created agent {name!r}: {agent_id}")

        existing_rai = ids_by_name(await rai_client.list_policies())
        for role, key in ((None, "LYZR_RAI_POLICY_ID"), ("buyer", "LYZR_RAI_BUYER_POLICY_ID"),
                          ("supplier", "LYZR_RAI_SUPPLIER_POLICY_ID")):
            payload = build_rai_policy(role)
            if payload["name"] in existing_rai:
                env[key] = existing_rai[payload["name"]]
                await rai_client.update_policy(env[key], payload)
                print(f"  synced RAI policy {payload['name']!r}: {env[key]}")
                continue
            created = await rai_client.create_policy(payload)
            env[key] = artefact_id(created) or ""
            print(f"  created RAI policy {payload['name']!r}: {env[key]}")

        # content-addressed: a guardrail with this name has exactly these rules, so an existing one is reused as is
        existing_opa = ids_by_name(await rai_client.list_opa_policies())
        for g in build_guardrails(scenario_ids):
            if g.name in existing_opa:
                print(f"  reused OPA guardrail {g.name!r}: {existing_opa[g.name]}")
                continue
            created = await rai_client.create_opa_policy(g.name, g.rego, g.description)
            print(f"  created OPA guardrail {g.name!r}: {artefact_id(created)}  ({g.description})")
        env["LYZR_OPA_GUARDRAILS"] = "true"
    finally:
        await agent_client.aclose()
        await rai_client.aclose()
    return env


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Update KEY=VALUE lines in place (appending missing keys) without touching anything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.lyzr.provision", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print payloads without calling Lyzr")
    parser.add_argument("--scenario", action="append", default=None,
                        help="only pre-register guardrails for this scenario (repeatable; default: every preset)")
    parser.add_argument("--write-env", default=None, help="update these KEY=VALUE lines in an env file (e.g. .env)")
    parser.add_argument("--aims-mode", default="event_log", choices=["local", "event_log", "agent_session"],
                        help="AIMS_MODE to write alongside the IDs (with --write-env)")
    args = parser.parse_args(argv)

    if args.dry_run:
        print("# POST /v3/agents/ payloads")
        for item in build_agent_payloads(LyzrSettings()):
            shown = {**item["payload"], "agent_instructions": item["payload"]["agent_instructions"][:160] + "..."}
            print(json.dumps({"env": item["env"], "payload": shown}, indent=2))
        for role in (None, "buyer", "supplier"):
            print(f"# POST /v1/rai/policies payload ({role or 'shared'})")
            print(json.dumps(build_rai_policy(role), indent=2))
        for g in build_guardrails(args.scenario):
            print(f"# POST /v1/opa-policies - {g.name} ({g.description})")
            print(g.rego)
        return 0

    settings = LyzrSettings()
    if not settings.configured:
        print("LYZR_API_KEY is not set (see .env.example). Use --dry-run to inspect payloads.", file=sys.stderr)
        return 2
    env = asyncio.run(provision(settings, args.scenario))
    if args.write_env:
        write_env(Path(args.write_env), {**env, "AIMS_MODE": args.aims_mode})
        print(f"\nUpdated {args.write_env} with {len(env)} IDs (+ AIMS_MODE={args.aims_mode}).")
    else:
        print("\nAdd these to your .env:\n" + "\n".join(f"{k}={v}" for k, v in env.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
