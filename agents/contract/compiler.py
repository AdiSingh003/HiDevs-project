"""Compile agreed terms into an executable, signed contract (structured JSON; PDF via :mod:`.pdf`).

Integrity model: ``content_hash = sha256(canonical JSON of the contract without integrity/status)``.
Each signer signs ``"{contract_id}|v{version}|{content_hash}"`` with Ed25519. Amendments are new
versions whose ``parent_hash`` is the content hash of the version they amend (a hash chain).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..core.models import Scenario, SupplierProfile, Terms, utcnow
from ..core.utility import PRICE_KEY
from ..guardrails.rules import Rulebook
from .automata_pipeline import DraftingPipeline
from .clauses import fmt_num
from .signing import KeyStore, fingerprint_of, verify_signature

SCHEMA = "hidevs.contract/v1"
HASH_EXCLUDE = {"integrity", "status", "approvals_log"}


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(contract: dict[str, Any]) -> str:
    body = {k: v for k, v in contract.items() if k not in HASH_EXCLUDE}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def signing_message(contract: dict[str, Any], digest: str) -> bytes:
    return f"{contract['contract_id']}|v{contract['version']}|{digest}".encode()


def _signer_id(role: str, org: str) -> str:
    return f"{role}:{org}"


class ContractCompiler:
    def __init__(self, keystore: KeyStore, drafting: DraftingPipeline | None = None):
        self.keystore = keystore
        self.drafting = drafting or DraftingPipeline()

    # ------------------------------------------------------------------ build

    def compile(self, *, scenario: Scenario, supplier: SupplierProfile, terms: Terms, negotiation: dict[str, Any],
                cfo_required: bool, version: int = 1, parent: dict[str, Any] | None = None,
                amendment: dict[str, Any] | None = None, effective: date | None = None) -> dict[str, Any]:
        ctx = scenario.context
        rulebook = Rulebook(ctx, supplier.party)
        specs = {s.key: s for s in scenario.issues}
        standard = {k: v for k, v in ctx.standard_terms.items() if k not in terms}
        effective_terms = {**standard, **terms}
        quantity = ctx.quantity
        total = terms[PRICE_KEY] * quantity
        cap_pct = effective_terms.get("penalty_cap_pct")
        cap_amount = total * cap_pct / 100 if cap_pct is not None else None
        effective = effective or datetime.now(timezone.utc).date()
        due = effective + timedelta(days=int(terms.get("delivery_days", 0)))
        per_unit = ctx.quantity_unit[:-1] if ctx.quantity_unit.endswith("s") else ctx.quantity_unit
        contract_id = parent["contract_id"] if parent else f"CTR-{effective:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        terms_fmt = {k: fmt_num(v, specs[k].decimals if k in specs else 2) for k, v in effective_terms.items()}

        payload = {
            "context": ctx.model_dump(mode="json"), "terms": terms, "terms_fmt": terms_fmt, "standard_terms": standard,
            "decimals": {k: s.decimals for k, s in specs.items()},
            "per_unit": per_unit, "quantity_fmt": fmt_num(quantity, 0), "total_value_fmt": fmt_num(total, 2),
            "cap_amount_fmt": fmt_num(cap_amount or 0, 2), "effective_date": effective.isoformat(),
            "delivery_due": due.isoformat(), "force_majeure_events": rulebook.force_majeure_events,
            "governing_law": rulebook.governing_law, "dispute_resolution": rulebook.dispute_resolution,
            "jurisdiction_name": rulebook.jurisdiction_name, "audit_head": negotiation.get("audit_head", "0" * 64),
            "mandatory_clauses": rulebook.mandatory_clauses, "supplier_is_msme": supplier.party.is_msme,
            "supplier_name": supplier.party.name, "cfo_required": cfo_required,
            "negotiation": {"rounds": negotiation.get("rounds", 0), "outcome": negotiation.get("outcome", "agreement"),
                            "interventions": negotiation.get("interventions", 0)},
        }
        drafted = self.drafting.run(payload, session_id=f"{contract_id}-v{version}-drafting")

        contract: dict[str, Any] = {
            "schema": SCHEMA,
            "contract_id": contract_id,
            "version": version,
            "parent_hash": parent["integrity"]["content_hash"] if parent else None,
            "title": f"Supply Agreement - {ctx.title}",
            "created_at": utcnow(),
            "effective_date": effective.isoformat(),
            "parties": {
                "buyer": {**ctx.buyer.model_dump(mode="json"), "role": "buyer"},
                "supplier": {**supplier.party.model_dump(mode="json"), "role": "supplier", "supplier_id": supplier.id},
            },
            "rfq": {"reference": ctx.reference, "title": ctx.title, "scenario_id": scenario.id},
            "commercial_terms": {
                "item": ctx.item, "item_description": ctx.item_description, "quantity": quantity,
                "quantity_unit": ctx.quantity_unit, "currency": ctx.currency, "unit_price": terms[PRICE_KEY],
                "total_value": round(total, 2), "incoterm": ctx.incoterm, "delivery_location": ctx.delivery_location,
                "payment_terms_days": terms.get("payment_terms_days"),
            },
            "delivery": {"lead_time_days": terms.get("delivery_days"), "delivery_due_date": due.isoformat()},
            "service_levels": {"otif_pct": effective_terms.get("sla_on_time_pct"), "measurement": "monthly OTIF"},
            "liquidated_damages": {
                "rate_pct_per_day": effective_terms.get("late_penalty_pct_per_day"), "cap_pct": cap_pct,
                "cap_amount": None if cap_amount is None else round(cap_amount, 2),
                "excused_by_force_majeure": True,
            },
            "warranty": {"months": effective_terms.get("warranty_months")},
            "terms": terms,
            "standard_terms": standard,
            "issues": [{"key": s.key, "label": s.label, "unit": s.unit, "decimals": s.decimals} for s in scenario.issues],
            "legal": {
                "jurisdiction": ctx.jurisdiction, "jurisdiction_name": rulebook.jurisdiction_name,
                "governing_law": rulebook.governing_law, "dispute_resolution": rulebook.dispute_resolution,
                "rulebook_version": rulebook.version, "rules_applied": rulebook.rule_ids(),
                "force_majeure_events": rulebook.force_majeure_events,
            },
            "clauses": drafted.clauses,
            "executable": self._executable(effective_terms, total, cap_amount, rulebook),
            "negotiation": negotiation,
            "drafting": {"engine": drafted.engine, "model": drafted.model, "review": drafted.review,
                         "substitutions": drafted.substitutions, "executive_summary": drafted.summary},
            "approvals": {
                "required": ["legal_arbiter", "supplier", "buyer"] + (["buyer_cfo"] if cfo_required else []),
                "cfo_required": cfo_required,
            },
            "amendment": amendment,
        }
        digest = content_hash(contract)
        contract["integrity"] = {"hash_algorithm": "sha256", "content_hash": digest, "signatures": []}
        self._sign(contract, "legal_arbiter", "Legal Arbiter (Safe AI)", "Compliance attestation", "legal_arbiter")
        self._sign(contract, "supplier", supplier.party.signatory_name, supplier.party.signatory_title,
                   _signer_id("supplier", supplier.party.name))
        self._sign(contract, "buyer", ctx.buyer.signatory_name, ctx.buyer.signatory_title,
                   _signer_id("buyer", ctx.buyer.name))
        contract["status"] = "pending_cfo_approval" if cfo_required else "executed"
        contract["approvals_log"] = [{"at": utcnow(), "event": "compiled", "by": "legal_arbiter"}]
        return contract

    def _executable(self, terms: Terms, total: float, cap_amount: float | None, rulebook: Rulebook) -> dict[str, Any]:
        rules = []
        if "late_penalty_pct_per_day" in terms:
            rules.append({
                "id": "LD-DELAY", "trigger": "days_late > 0 and not force_majeure",
                "formula": "min(rate_pct_per_day / 100 * days_late * shipment_value, cap_amount - ld_already_charged)",
                "params": {"rate_pct_per_day": terms["late_penalty_pct_per_day"],
                           "cap_amount": None if cap_amount is None else round(cap_amount, 2)},
            })
        if "sla_on_time_pct" in terms:
            rules.append({
                "id": "OTIF-CREDIT", "trigger": "monthly_otif_pct < otif_target",
                "formula": "min(0.10, (otif_target - monthly_otif_pct) * 0.01) * monthly_invoice_value",
                "params": {"otif_target": terms["sla_on_time_pct"]},
            })
        return {
            "sla_rules": rules,
            "payment_schedule": [{"milestone": "delivery acceptance", "due_days_after": terms.get("payment_terms_days"),
                                  "amount": round(total, 2)}],
            "force_majeure_events": rulebook.force_majeure_events,
            "renegotiation_webhook": {"path": "/api/webhooks/telemetry", "signature": "HMAC-SHA256 (X-Telemetry-Signature)"},
        }

    def _sign(self, contract: dict[str, Any], role: str, name: str, title: str, signer_id: str) -> None:
        digest = contract["integrity"]["content_hash"]
        contract["integrity"]["signatures"].append({
            "role": role, "name": name, "title": title, "signer_id": signer_id, "algorithm": "Ed25519",
            "public_key": self.keystore.public_key_b64(signer_id),
            "fingerprint": self.keystore.fingerprint(signer_id),
            "signature": self.keystore.sign(signer_id, signing_message(contract, digest)),
            "signed_at": utcnow(),
        })

    # ------------------------------------------------------------------ lifecycle

    def approve_cfo(self, contract: dict[str, Any], approver_name: str | None = None) -> dict[str, Any]:
        if contract.get("status") != "pending_cfo_approval":
            raise ValueError(f"contract {contract['contract_id']} is not awaiting CFO approval")
        buyer = contract["parties"]["buyer"]
        self._sign(contract, "buyer_cfo", approver_name or f"CFO, {buyer['name']}", "Chief Financial Officer",
                   _signer_id("cfo", buyer["name"]))
        contract["status"] = "executed"
        contract.setdefault("approvals_log", []).append({"at": utcnow(), "event": "cfo_approved",
                                                         "by": approver_name or "CFO"})
        return contract


def verify_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Recompute the content hash and check every signature (independent of the key store)."""
    digest = content_hash(contract)
    integrity = contract.get("integrity", {})
    hash_ok = digest == integrity.get("content_hash")
    results = []
    for sig in integrity.get("signatures", []):
        ok = verify_signature(sig["public_key"], signing_message(contract, integrity.get("content_hash", "")),
                              sig["signature"])
        results.append({"role": sig["role"], "name": sig["name"], "valid": ok,
                        "fingerprint_matches": fingerprint_of(sig["public_key"]) == sig.get("fingerprint")})
    required = set(contract.get("approvals", {}).get("required", []))
    signed_roles = {r["role"] for r in results if r["valid"]}
    authentic = hash_ok and bool(results) and all(r["valid"] for r in results)
    missing = sorted(required - signed_roles)
    return {
        "contract_id": contract.get("contract_id"), "version": contract.get("version"),
        "hash_valid": hash_ok, "computed_hash": digest, "recorded_hash": integrity.get("content_hash"),
        "signatures": results, "all_signatures_valid": bool(results) and all(r["valid"] for r in results),
        "missing_signatures": missing, "valid": authentic, "fully_executed": authentic and not missing,
    }
