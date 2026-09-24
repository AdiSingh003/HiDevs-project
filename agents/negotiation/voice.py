"""Deterministic natural-language voice for negotiator agents (offline mode / LLM fallback).

The voice only ever sees *public* information (offers, issue specs, RFQ) plus which issues the
strategy moved - never mandate numbers - so it cannot leak a reservation value by construction.
"""

from __future__ import annotations

import random

from ..core.models import Action, DealContext, IssueSpec, Role, Terms

JUSTIFY: dict[str, dict[str, list[str]]] = {
    "unit_price": {
        "buyer": ["current market benchmarks for comparable parts", "our program-level cost targets",
                  "the volume we are committing to"],
        "supplier": ["input-cost inflation this quarter", "the capacity we are reserving for you",
                     "the quality grade this specification requires"],
    },
    "delivery_days": {
        "buyer": ["our production ramp schedule", "line-side inventory targets"],
        "supplier": ["current production cycle times", "capacity allocation across our customer base"],
    },
    "payment_terms_days": {
        "buyer": ["our standard treasury cycle", "group working-capital policy"],
        "supplier": ["our working-capital position", "the financing cost of receivables"],
    },
    "sla_on_time_pct": {
        "buyer": ["the cost of a line stoppage on our side", "our downstream service commitments"],
        "supplier": ["logistics variability outside our control", "carrier performance risk"],
    },
    "late_penalty_pct_per_day": {
        "buyer": ["the real cost of delay to our operations", "the need for a meaningful delivery incentive"],
        "supplier": ["keeping liquidated damages proportionate", "the insurance cost of aggressive LDs"],
    },
    "penalty_cap_pct": {
        "buyer": ["meaningful protection if delays compound", "fair risk-sharing on critical shipments"],
        "supplier": ["keeping total exposure insurable", "a proportionate allocation of risk"],
    },
    "warranty_months": {
        "buyer": ["the service life of our end products", "field-failure exposure"],
        "supplier": ["our reliability data for this part family", "warranty reserve costs"],
    },
}


def per_unit(context: DealContext) -> str:
    unit = context.quantity_unit.strip()
    return unit[:-1] if unit.endswith("s") else unit


def phrase(spec: IssueSpec, value: float, context: DealContext) -> str:
    v = spec.fmt(value)
    n = f"{value:,.{spec.decimals}f}"
    return {
        "unit_price": f"{v} per {per_unit(context)}",
        "delivery_days": f"delivery in {n} days",
        "payment_terms_days": f"Net {n} payment terms",
        "sla_on_time_pct": f"{n}% on-time-in-full",
        "late_penalty_pct_per_day": f"LDs of {n}% per day of delay",
        "penalty_cap_pct": f"an LD cap of {n}% of contract value",
        "warranty_months": f"a {n}-month warranty",
    }.get(spec.key, f"{spec.label.lower()} of {v}")


def terms_sentence(offer: Terms, issues: list[IssueSpec], context: DealContext) -> str:
    parts = [phrase(spec, offer[spec.key], context) for spec in issues if spec.key in offer]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class LocalVoice:
    def __init__(self, role: Role, organisation: str, counterparty: str, issues: list[IssueSpec],
                 context: DealContext, weights: dict[str, float], rng: random.Random):
        self.role = role
        self.org = organisation
        self.counterparty = counterparty
        self.issues = issues
        self.specs = {s.key: s for s in issues}
        self.context = context
        self.weights = weights
        self.rng = rng

    def _why(self, key: str) -> str:
        options = JUSTIFY.get(key, {}).get(self.role) or ["our internal requirements"]
        return self.rng.choice(options)

    def compose(self, action: Action, offer: Terms | None, previous: Terms | None, counterpart: Terms | None,
                conceded: list[str], competition: bool = False, final_turn: bool = False) -> str:
        ctx = self.context
        if action is Action.ACCEPT:
            opener = self.rng.choice(["Agreed.", "We have a deal.", "That works for us."])
            return (f"{opener} {self.org} accepts {self.counterparty}'s proposal as tabled. "
                    "We will ask the Legal Arbiter to compile the contract for signature.")
        if action is Action.WALK_AWAY:
            return (f"Thank you for your time. {self.org} cannot proceed on these terms and will pursue "
                    "its alternatives. We remain open to future opportunities.")
        assert offer is not None
        if previous is None:
            if self.role == "supplier":
                return (f"Thank you for the opportunity under {ctx.reference}. For {ctx.quantity:,.0f} "
                        f"{ctx.quantity_unit} of {ctx.item} "
                        f"we can offer {terms_sentence(offer, self.issues, ctx)}. "
                        f"This reflects {self._why('unit_price')}.")
            top = sorted(self.weights, key=self.weights.get, reverse=True)[:2]  # type: ignore[arg-type]
            return (f"Thank you for the quote. To make this work for {self.org} we need "
                    f"{terms_sentence(offer, self.issues, ctx)}. Our priorities are "
                    f"{_join([self.specs[k].label.lower() for k in top])}.")

        moved = [k for k in conceded if abs(offer[k] - previous[k]) > 1e-9]
        held = [k for k in sorted(self.weights, key=self.weights.get, reverse=True)  # type: ignore[arg-type]
                if abs(offer[k] - previous[k]) <= 1e-9]
        sentences: list[str] = []
        if moved:
            moves = [f"{self.specs[k].label.lower()} to {self.specs[k].fmt(offer[k])}" for k in moved[:3]]
            lead = self.rng.choice(["In the spirit of closing,", "To keep momentum,", "Meeting you part-way,",
                                    "We have listened to your priorities:"])
            sentences.append(f"{lead} we are moving {_join(moves)}.")
        else:
            sentences.append(self.rng.choice([
                "We have reviewed your proposal carefully but need to hold our position this round.",
                "Our position is unchanged for now - the gap is on terms that matter most to us.",
            ]))
        if held:
            k = held[0]
            sentences.append(f"We need to hold {self.specs[k].label.lower()} at {self.specs[k].fmt(offer[k])} "
                             f"given {self._why(k)}.")
        if counterpart is not None:
            gaps = sorted(self.issues, key=lambda s: self.weights[s.key] * abs(offer[s.key] - counterpart[s.key])
                          / (abs(offer[s.key]) + 1e-9), reverse=True)
            ask = gaps[0]
            if abs(offer[ask.key] - counterpart[ask.key]) > 1e-9:
                sentences.append(f"If you can move on {ask.label.lower()}, we can close quickly.")
        if competition and self.role == "buyer":
            sentences.append("For transparency, we are evaluating competitive offers under this RFQ.")
        if final_turn:
            sentences.append("This is our final offer within this negotiation window.")
        return " ".join(sentences)

    def mediation_reply(self, accepted: bool) -> str:
        if accepted:
            return f"{self.org} accepts the Legal Arbiter's mediated proposal."
        return f"{self.org} cannot accept the mediated proposal on these terms."
