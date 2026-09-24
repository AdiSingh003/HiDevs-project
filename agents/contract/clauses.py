"""Canonical clause templates and the numeric-consistency check for (LLM-)drafted clauses.

The structured terms are the source of truth. A drafted clause is only kept if every agreed number
for that clause appears in its text and it introduces no conflicting percentages; otherwise the
canonical template is substituted and the finding is recorded in the contract.
"""

from __future__ import annotations

import re
from typing import Any

FM_LABELS = {
    "severe_weather": "severe weather events (cyclones, floods, blizzards)",
    "natural_disaster": "earthquakes and other natural disasters",
    "port_closure": "port or border closures ordered by a public authority",
    "epidemic": "epidemics and pandemics",
    "war_or_civil_unrest": "war, terrorism or civil unrest",
    "government_embargo": "embargoes, sanctions or changes in law",
}

ANTI_BRIBERY_LAW = {
    "IN": "the Prevention of Corruption Act, 1988",
    "EU": "applicable EU and national anti-corruption laws",
    "UK": "the Bribery Act 2010",
    "US": "the Foreign Corrupt Practices Act",
}

CLAUSE_KEYS = {
    "price_payment": ["unit_price", "payment_terms_days"],
    "delivery": ["delivery_days"],
    "service_levels": ["sla_on_time_pct"],
    "liquidated_damages": ["late_penalty_pct_per_day", "penalty_cap_pct"],
    "warranty": ["warranty_months"],
}


def fmt_num(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


def number_variants(value: float, decimals: int) -> set[str]:
    base = f"{value:.{decimals}f}"
    out = {base, fmt_num(value, decimals)}
    if "." in base:
        stripped = base.rstrip("0").rstrip(".")
        out |= {stripped, f"{float(stripped):,}" if "." in stripped else f"{int(float(stripped)):,}"}
    return out


def render_clauses(p: dict[str, Any]) -> list[dict[str, str]]:
    """Render every mandatory clause from the drafting payload (terms are pre-formatted strings)."""
    t = p["terms_fmt"]
    c = p["context"]
    cur = c["currency"]
    unit = p["per_unit"]
    juris = c["jurisdiction"]
    pay_extra = ""
    if juris == "IN" and p.get("supplier_is_msme"):
        pay_extra = (" As the Supplier is registered as a micro or small enterprise, payment shall in no event be made "
                     "later than forty-five (45) days from the day of acceptance, in accordance with Section 15 of the "
                     "MSMED Act, 2006.")
    elif juris == "EU":
        pay_extra = " The payment period complies with Article 3(5) of Directive 2011/7/EU."
    fm_list = "; ".join(FM_LABELS.get(e, e) for e in p["force_majeure_events"])
    pay_days = t.get("payment_terms_days", "30")
    lead = t.get("delivery_days")
    delivery_window = f"within {lead} days of the Effective Date" if lead else "by the Delivery Date"
    sla = (f"an OTIF service level of at least {t['sla_on_time_pct']}% measured monthly" if "sla_on_time_pct" in t
           else "the service levels specified in the RFQ, measured monthly")
    rate, cap = t.get("late_penalty_pct_per_day"), t.get("penalty_cap_pct")
    ld_rate = (f"liquidated damages of {rate}% of the value of the delayed Goods for each day of delay" if rate
               else "liquidated damages reflecting a genuine pre-estimate of the Buyer's loss for each day of delay")
    ld_cap = (f", capped in aggregate at {cap}% of the Contract Value ({cur} {p['cap_amount_fmt']})" if cap
              else ", subject to the limitation of liability")
    warranty = (
        f"The Supplier warrants that the Goods will conform to the agreed specification and be free from defects in "
        f"materials and workmanship for {t['warranty_months']} months from delivery. Non-conforming Goods shall be "
        f"repaired or replaced at the Supplier's cost within 15 Business Days of notification."
        if "warranty_months" in t else
        "The Supplier warrants that the services will be performed with reasonable skill and care, in accordance with "
        "good industry practice and applicable regulatory guidelines, and shall re-perform any non-conforming service "
        "at its own cost."
    )
    clauses = {
        "definitions": (
            f"In this Agreement: \"Goods\" means {p['quantity_fmt']} {c['quantity_unit']} of {c['item']}; \"Contract "
            f"Value\" means {cur} {p['total_value_fmt']} exclusive of taxes; \"Effective Date\" means {p['effective_date']}; "
            f"\"Delivery Date\" means {p['delivery_due']}; \"OTIF\" means the percentage of order lines delivered on time "
            f"and in full in a calendar month; \"Force Majeure Event\" has the meaning given in the Force Majeure clause."
        ),
        "scope": (
            f"The Supplier shall manufacture (or procure), supply and deliver the Goods to {c['delivery_location']} on "
            f"{c['incoterm']} terms (Incoterms 2020) in accordance with the specifications in {c['reference']}."
        ),
        "price_payment": (
            f"The price is {cur} {t['unit_price']} per {unit}, fixed for the term of this Agreement, giving a Contract "
            f"Value of {cur} {p['total_value_fmt']}. The Buyer shall pay each undisputed invoice within "
            f"{pay_days} days of acceptance of the corresponding delivery.{pay_extra}"
        ),
        "delivery": (
            f"The Supplier shall deliver the Goods {delivery_window}, being no later than {p['delivery_due']}. Title and "
            f"risk pass in accordance with {c['incoterm']}. The Buyer shall accept or reject each delivery within 5 "
            f"Business Days of receipt."
        ),
        "service_levels": (
            f"The Supplier shall achieve {sla} and reported with supporting logistics data. If performance falls below "
            f"this level in any month the Supplier shall deliver a corrective action plan within 10 Business Days and "
            f"grant the service credit defined in the executable SLA schedule."
        ),
        "liquidated_damages": (
            f"If the Supplier fails to deliver by the Delivery Date it shall pay {ld_rate}{ld_cap}. The parties agree "
            f"these sums are a genuine pre-estimate of loss and not a penalty. No liquidated damages accrue for delay "
            f"caused by a Force Majeure Event."
        ),
        "warranty": warranty,
        "force_majeure": (
            f"Neither party is liable for delay or failure to perform caused by events beyond its reasonable control, "
            f"including {fm_list} (each a \"Force Majeure Event\"), provided it notifies the other party within 48 hours "
            f"with supporting evidence, which may include independent third-party telemetry. Affected obligations are "
            f"suspended for the duration of the event and the parties shall renegotiate affected milestones in good "
            f"faith under the Change Control clause."
        ),
        "change_control": (
            "Any change to price, delivery milestones or service levels requires a written amendment signed by both "
            "parties. Telemetry events notified through the agreed signed webhook trigger a bounded renegotiation "
            "limited to the affected terms; each amendment references the content hash of the version it amends. "
            "Price adjustments for commodity index movements are capped at 5% per contract year."
        ),
        "limitation_of_liability": (
            "Save for liability that cannot be limited by law, fraud, wilful misconduct or breach of confidentiality, "
            "each party's aggregate liability under this Agreement is limited to 100% of the Contract Value, and neither "
            "party is liable for indirect or consequential loss."
        ),
        "confidentiality": (
            "Each party shall keep confidential all non-public information received from the other, including pricing, "
            "policy envelopes and negotiation records, during the term and for three years after termination."
        ),
        "anti_bribery": (
            f"Each party shall comply with {ANTI_BRIBERY_LAW.get(juris, 'applicable anti-bribery laws')} and maintain "
            f"adequate procedures to prevent bribery by persons acting on its behalf."
        ),
        "termination": (
            "Either party may terminate this Agreement for material breach not remedied within 30 days of written "
            "notice. The Buyer may terminate with immediate effect if accrued liquidated damages reach the aggregate cap."
        ),
        "governing_law": (
            f"This Agreement is governed by {p['governing_law']}. Any dispute shall be finally resolved by "
            f"{p['dispute_resolution']}."
        ),
        "entire_agreement": (
            f"This Agreement, together with {c['reference']} and the negotiation record identified by audit hash "
            f"{p['audit_head'][:16]}..., constitutes the entire agreement between the parties and supersedes all prior "
            f"negotiations, representations and understandings."
        ),
    }
    return [{"id": m["id"], "title": m["title"], "text": clauses[m["id"]]} for m in p["mandatory_clauses"]]


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?%")


def verify_clause(clause_id: str, text: str, terms: dict[str, float], decimals: dict[str, int],
                  extra_allowed_pct: set[str] | None = None) -> list[str]:
    """Return problems if a drafted clause does not faithfully carry the agreed numbers."""
    problems = []
    keys = [k for k in CLAUSE_KEYS.get(clause_id, []) if k in terms]
    compact = text.replace(",", "")
    for key in keys:
        variants = {v.replace(",", "") for v in number_variants(terms[key], decimals.get(key, 2))}
        if not any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", compact) for v in variants):
            problems.append(f"agreed {key} ({terms[key]:g}) is missing")
    if keys:
        allowed = set(extra_allowed_pct or set())
        for key in keys:
            allowed |= {v.replace(",", "") for v in number_variants(terms[key], decimals.get(key, 2))}
        for m in _PCT.finditer(compact):
            if m.group(1) not in allowed and f"{float(m.group(1)):g}" not in allowed:
                problems.append(f"unexpected percentage {m.group(1)}% conflicts with agreed terms")
    return problems
