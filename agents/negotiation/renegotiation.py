"""Live renegotiation triggered by external telemetry (weather, port closures, commodity indices).

1. :func:`assess` classifies the event against the executed contract (force-majeure list, severity,
   index-adjustment threshold) and decides: renegotiate / apply the SLA schedule / no action.
2. :func:`amendment_scenario` derives a bounded negotiation limited to the affected terms, with
   envelopes computed from the contract and the event (CFO budget cap and cost floors still apply).
3. The regular engine + Legal Arbiter run it; the outcome becomes a hash-chained contract amendment.
"""

from __future__ import annotations

import math
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..contract.sla import evaluate_delay
from ..core.models import Envelope, IssueMandate, Scenario, StrategyProfile, SupplierProfile, utcnow
from ..core.utility import PRICE_KEY

EventType = Literal["severe_weather", "natural_disaster", "port_closure", "epidemic", "war_or_civil_unrest",
                    "government_embargo", "commodity_price_spike", "carrier_delay"]

FM_SEVERITY_THRESHOLD = 3
INDEX_THRESHOLD_PCT = 3.0
INDEX_CAP_PCT = 5.0


class TelemetryEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"EVT-{uuid.uuid4().hex[:10].upper()}")
    contract_id: str
    source: str = "telemetry"
    event_type: EventType
    severity: int = Field(3, ge=1, le=5)
    region: str = ""
    expected_delay_days: int = Field(0, ge=0, le=180)
    price_index: str | None = None
    price_index_change_pct: float = Field(0.0, ge=-100, le=500)
    material_share_pct: float = Field(60.0, ge=0, le=100)
    description: str = ""
    observed_at: str = Field(default_factory=utcnow)


class Assessment(BaseModel):
    event_id: str
    contract_id: str
    action: Literal["renegotiate", "apply_sla", "no_action"]
    force_majeure: bool = False
    reason: str
    affected_issues: list[str] = Field(default_factory=list)
    sla_estimate: dict[str, Any] | None = None


def assess(event: TelemetryEvent, contract: dict[str, Any]) -> Assessment:
    terms = contract["terms"]
    fm_events = contract["legal"]["force_majeure_events"]
    base = {"event_id": event.event_id, "contract_id": contract["contract_id"]}
    if event.event_type == "commodity_price_spike":
        if event.price_index_change_pct >= INDEX_THRESHOLD_PCT and PRICE_KEY in terms:
            affected = [PRICE_KEY] + (["payment_terms_days"] if terms.get("payment_terms_days", 0) >= 5 else [])
            return Assessment(**base, action="renegotiate", affected_issues=affected,
                              reason=(f"{event.price_index or 'Input-cost index'} moved +{event.price_index_change_pct:g}% "
                                      f"(threshold {INDEX_THRESHOLD_PCT:g}%): the Change Control clause allows a bounded "
                                      f"price adjustment capped at {INDEX_CAP_PCT:g}%."))
        return Assessment(**base, action="no_action",
                          reason=f"Index movement {event.price_index_change_pct:+g}% is below the "
                                 f"{INDEX_THRESHOLD_PCT:g}% adjustment threshold; contract price stands.")
    if event.expected_delay_days <= 0:
        return Assessment(**base, action="no_action", reason="Event reports no delivery impact.")
    if event.event_type in fm_events and event.severity >= FM_SEVERITY_THRESHOLD:
        affected = [k for k in ("delivery_days", PRICE_KEY) if k in terms]
        return Assessment(**base, action="renegotiate", force_majeure=True, affected_issues=affected,
                          reason=(f"{event.event_type.replace('_', ' ')} (severity {event.severity}/5) is a listed Force "
                                  f"Majeure Event: LDs are suspended for the {event.expected_delay_days}-day impact and "
                                  f"the delivery milestone is renegotiated under Change Control."))
    estimate = evaluate_delay(contract, event.expected_delay_days)
    why = ("is below the force-majeure severity threshold" if event.event_type in fm_events
           else "is not a force-majeure event")
    return Assessment(**base, action="apply_sla", sla_estimate=estimate,
                      reason=f"{event.event_type.replace('_', ' ')} {why}; the supplier bears the delay and the LD "
                             f"schedule applies (estimated {contract['commercial_terms']['currency']} "
                             f"{estimate.get('amount', 0):,.2f}).")


def _mandate(ideal: float, limit: float, weight: float) -> IssueMandate:
    return IssueMandate(ideal=ideal, limit=limit, weight=weight)


def amendment_scenario(scenario: Scenario, supplier: SupplierProfile, contract: dict[str, Any],
                       event: TelemetryEvent, assessment: Assessment) -> Scenario:
    terms = contract["terms"]
    price = terms[PRICE_KEY]
    buyer_m: dict[str, IssueMandate] = {}
    supplier_m: dict[str, IssueMandate] = {}
    if assessment.force_majeure:
        d = max(1, event.expected_delay_days)
        base_days = terms["delivery_days"]
        if "delivery_days" in assessment.affected_issues:
            buyer_m["delivery_days"] = _mandate(base_days + 1, base_days + d + 5, 0.65)
            supplier_m["delivery_days"] = _mandate(base_days + d + 2, base_days + math.ceil(d / 2), 0.40)
        buyer_m[PRICE_KEY] = _mandate(price * 0.99, price * 1.02, 0.35)
        supplier_m[PRICE_KEY] = _mandate(price * 1.035, price, 0.60)
        story = ("Supplier can recover part of the delay by expediting (air/priority freight) in exchange for a "
                 "price contribution; the buyer trades price for time.")
    else:
        pass_through = event.price_index_change_pct * event.material_share_pct / 100
        buyer_m[PRICE_KEY] = _mandate(price, price * (1 + min(pass_through, INDEX_CAP_PCT) / 100), 0.75)
        supplier_m[PRICE_KEY] = _mandate(price * (1 + pass_through / 100), price * (1 + 0.25 * pass_through / 100), 0.70)
        if "payment_terms_days" in assessment.affected_issues:
            t = terms["payment_terms_days"]
            buyer_m["payment_terms_days"] = _mandate(t, max(0.0, t - 15), 0.25)
            supplier_m["payment_terms_days"] = _mandate(max(0.0, t - 15), t, 0.30)
        story = ("Supplier seeks to pass through input-cost inflation; the buyer offers faster payment instead of "
                 "full price pass-through, within the contractual 5% escalation cap.")
    issues = [scenario.issue(k) for k in buyer_m]
    strategy = StrategyProfile(tactic="linear", reciprocity=0.3, tradeoff_sharpness=0.2)
    buyer_env = Envelope(role="buyer", mandate=buyer_m, batna_utility=0.1, strategy=strategy,
                         batna_description="Fall back to the contract as written (force-majeure relief / fixed price)",
                         budget_cap=scenario.buyer.budget_cap, auto_approve_limit=scenario.buyer.auto_approve_limit)
    supplier_env = Envelope(role="supplier", mandate=supplier_m, batna_utility=0.1, strategy=strategy,
                            batna_description="Perform the contract as written",
                            unit_cost=supplier.envelope.unit_cost, min_margin_pct=supplier.envelope.min_margin_pct)
    return Scenario(
        id=f"{scenario.id}__amend_v{contract['version'] + 1}",
        title=f"Amendment v{contract['version'] + 1}: {event.event_type.replace('_', ' ')}",
        summary=story, mode="bilateral", tags=["renegotiation", event.event_type], context=scenario.context,
        issues=issues, buyer=buyer_env,
        suppliers=[SupplierProfile(id=supplier.id, party=supplier.party, envelope=supplier_env)],
        max_rounds=6, seed=scenario.seed + contract["version"],
    )


def amendment_record(event: TelemetryEvent, assessment: Assessment, old_terms: dict[str, float],
                     new_terms: dict[str, float], renegotiation_id: str) -> dict[str, Any]:
    changed = {k: {"from": old_terms[k], "to": v} for k, v in new_terms.items() if abs(v - old_terms[k]) > 1e-9}
    waiver = None
    if assessment.force_majeure:
        waiver = (f"Liquidated damages are waived for the {event.expected_delay_days}-day force-majeure period "
                  f"reported by {event.source} (event {event.event_id}); the amended delivery milestone governs.")
    return {"event": event.model_dump(mode="json"), "assessment": assessment.model_dump(mode="json"),
            "changed_terms": changed, "renegotiation_id": renegotiation_id, "ld_waiver": waiver}
