"""Executable SLA schedule: evaluate delivery events against a compiled contract's own rules."""

from __future__ import annotations

from typing import Any


def _rule(contract: dict[str, Any], rule_id: str) -> dict[str, Any] | None:
    return next((r for r in contract["executable"]["sla_rules"] if r["id"] == rule_id), None)


def evaluate_delay(contract: dict[str, Any], days_late: float, shipment_value: float | None = None,
                   force_majeure: bool = False, ld_already_charged: float = 0.0) -> dict[str, Any]:
    """Liquidated damages owed for a late delivery (rule LD-DELAY)."""
    rule = _rule(contract, "LD-DELAY")
    total = contract["commercial_terms"]["total_value"]
    value = total if shipment_value is None else shipment_value
    if rule is None:
        return {"rule": None, "amount": 0.0, "explanation": "Contract has no liquidated-damages rule"}
    rate = rule["params"]["rate_pct_per_day"]
    cap = rule["params"]["cap_amount"] if rule["params"]["cap_amount"] is not None else float("inf")
    if days_late <= 0:
        return {"rule": "LD-DELAY", "amount": 0.0, "capped": False, "excused": False,
                "explanation": "Delivered on time - no liquidated damages"}
    if force_majeure:
        return {"rule": "LD-DELAY", "amount": 0.0, "capped": False, "excused": True,
                "explanation": "Delay excused: Force Majeure Event (no LDs accrue; renegotiate milestones)"}
    raw = rate / 100 * days_late * value
    remaining = max(0.0, cap - ld_already_charged)
    amount = min(raw, remaining)
    return {
        "rule": "LD-DELAY", "amount": round(amount, 2), "uncapped_amount": round(raw, 2), "capped": raw > remaining,
        "excused": False, "cap_amount": None if cap == float("inf") else round(cap, 2),
        "explanation": (f"{rate}% x {days_late:g} days x {contract['commercial_terms']['currency']} {value:,.2f}"
                        + (" (limited by the aggregate LD cap)" if raw > remaining else "")),
    }


def evaluate_otif(contract: dict[str, Any], monthly_otif_pct: float, monthly_invoice_value: float) -> dict[str, Any]:
    """Service credit for a month whose OTIF fell below target (rule OTIF-CREDIT)."""
    rule = _rule(contract, "OTIF-CREDIT")
    if rule is None:
        return {"rule": None, "credit": 0.0, "explanation": "Contract has no OTIF rule"}
    target = rule["params"]["otif_target"]
    if monthly_otif_pct >= target:
        return {"rule": "OTIF-CREDIT", "credit": 0.0, "breach": False,
                "explanation": f"OTIF {monthly_otif_pct:g}% meets the {target:g}% target"}
    factor = min(0.10, (target - monthly_otif_pct) * 0.01)
    return {"rule": "OTIF-CREDIT", "credit": round(factor * monthly_invoice_value, 2), "breach": True,
            "credit_pct": round(factor * 100, 2),
            "explanation": f"OTIF {monthly_otif_pct:g}% vs target {target:g}% -> {factor * 100:.2f}% service credit"}
