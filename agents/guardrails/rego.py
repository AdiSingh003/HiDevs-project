"""Compile a sealed policy envelope into an OPA/Rego guardrail.

The generated policy is registered with Lyzr Safe AI (``POST /v1/opa-policies``) by the provisioning
CLI and evaluated on every ``submit_offer`` tool call via ``/v1/guardrails/evaluate-tool-call``.
It encodes exactly the same hard limits the local Legal Arbiter enforces, so both layers agree.

Verified against the live Lyzr service: the managed evaluator queries the ``allow`` rule and passes the
call as ``input.request.tool_name`` / ``input.request.arguments``.

Guardrails are content-addressed (``guardrail_spec``): the Lyzr policy is named after a hash of the compiled
rules, so every distinct envelope (preset, operator override, RFQ lane, telemetry amendment) gets its own
guardrail and identical envelopes share one.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

from ..core.models import Direction
from ..core.utility import PRICE_KEY, UtilityModel
from .rules import Rulebook


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _num(x: float) -> str:
    return json.dumps(round(float(x), 6))


def _obj(d: dict[str, float]) -> str:
    if not d:
        return "{}"
    return "{" + ", ".join(f'"{k}": {_num(v)}' for k, v in sorted(d.items())) + "}"


@dataclass(frozen=True)
class Guardrail:
    role: str
    name: str
    description: str
    rego: str
    fingerprint: str


def guardrail_spec(util: UtilityModel, rulebook: Rulebook, organisation: str) -> Guardrail:
    """The envelope's guardrail, named after the SHA-256 of its rules (no salted commitment, so it is stable)."""
    rules = _rules(util, rulebook)
    fingerprint = hashlib.sha256("\n".join([rulebook.version, util.role, organisation, *rules]).encode()).hexdigest()
    return Guardrail(
        role=util.role,
        name=f"b2b-guardrail-{util.role}-{fingerprint[:16]}",
        description=f"Sealed-envelope guardrail for the {util.role} agent ({organisation}), sha256:{fingerprint[:16]}",
        rego=compile_rego(util, rulebook, organisation, fingerprint=fingerprint),
        fingerprint=fingerprint,
    )


def compile_rego(util: UtilityModel, rulebook: Rulebook, organisation: str, commitment: str = "",
                 fingerprint: str = "") -> str:
    package = f"lyzr.procurement.{util.role}_{_slug(organisation)}" + (f"_{fingerprint[:12]}" if fingerprint else "")
    provenance = (f"# Guardrail fingerprint: sha256:{fingerprint}" if fingerprint
                  else f"# Envelope commitment: {commitment or 'n/a'}")
    return "\n".join([
        f"package {package}",
        "",
        "import rego.v1",
        "",
        "# Compiled by the Legal Arbiter from a sealed policy envelope.",
        f"{provenance}  |  rulebook {rulebook.version} ({rulebook.jurisdiction_name})",
        "# Evaluated by Lyzr Safe AI (OPA guardrail) on every `submit_offer` tool call.",
        "",
        *_rules(util, rulebook),
    ])


def _rules(util: UtilityModel, rulebook: Rulebook) -> list[str]:
    role = util.role
    mandate_max: dict[str, float] = {}
    mandate_min: dict[str, float] = {}
    for key in util.keys:
        if util.specs[key].prefers(role) is Direction.LOWER:
            mandate_max[key] = util.limit[key]
        else:
            mandate_min[key] = util.limit[key]
    legal_min: dict[str, float] = {}
    legal_max: dict[str, float] = {}
    for key in util.keys:
        lo, hi = rulebook.bounds(key)
        if not math.isinf(lo):
            legal_min[key] = lo
        if not math.isinf(hi):
            legal_max[key] = hi

    env = util.envelope
    lines = [
        f"required_issues := {json.dumps(sorted(util.keys))}",
        f"mandate_max := {_obj(mandate_max)}",
        f"mandate_min := {_obj(mandate_min)}",
        f"legal_min := {_obj(legal_min)}",
        f"legal_max := {_obj(legal_max)}",
        f"quantity := {_num(util.quantity)}",
    ]
    if role == "buyer" and env.budget_cap:
        lines.append(f"budget_cap := {_num(env.budget_cap)}")
    lines += [
        "",
        "# Lyzr's managed OPA passes the tool call as {\"request\": {\"tool_name\", \"arguments\"}, \"context\": {...}};",
        "# plain OPA callers (and the local cross-check) send {\"tool_args\": ...}. Support both.",
        "request := object.get(input, \"request\", {})",
        "tool_args := object.get(request, \"arguments\", object.get(input, \"tool_args\", {}))",
        "offer := object.get(tool_args, \"offer\", {})",
        "",
        "# object.get keeps a missing key from making this rule silently undefined",
        "deny contains msg if {",
        "\tsome issue in required_issues",
        "\tnot is_number(object.get(offer, issue, null))",
        '\tmsg := sprintf("SCHEMA: %s missing or not numeric", [issue])',
        "}",
        "",
        "deny contains msg if {",
        "\tsome issue, bound in mandate_max",
        "\toffer[issue] > bound",
        '\tmsg := sprintf("POLICY-MANDATE: %s above the sealed mandate", [issue])',
        "}",
        "",
        "deny contains msg if {",
        "\tsome issue, bound in mandate_min",
        "\toffer[issue] < bound",
        '\tmsg := sprintf("POLICY-MANDATE: %s below the sealed mandate", [issue])',
        "}",
        "",
        "deny contains msg if {",
        "\tsome issue, bound in legal_max",
        "\toffer[issue] > bound",
        '\tmsg := sprintf("LEGAL: %s above the legal maximum", [issue])',
        "}",
        "",
        "deny contains msg if {",
        "\tsome issue, bound in legal_min",
        "\toffer[issue] < bound",
        '\tmsg := sprintf("LEGAL: %s below the legal minimum", [issue])',
        "}",
    ]
    if role == "buyer" and env.budget_cap and PRICE_KEY in util.keys:
        lines += [
            "",
            'deny contains "POLICY-BUDGET: contract value exceeds the CFO budget cap" if {',
            f"\toffer.{PRICE_KEY} * quantity > budget_cap",
            "}",
        ]
    lines += [
        "",
        "default allow := false",
        "",
        "allow if count(deny) == 0",
        "",
    ]
    return lines
