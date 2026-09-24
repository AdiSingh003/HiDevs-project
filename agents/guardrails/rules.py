"""Public legal rulebook: jurisdiction-aware numeric rules and mandatory clauses."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..core.models import DealContext, Envelope, IssueSpec, Party, RuleViolation, Terms

RULEBOOK_PATH = Path(__file__).resolve().parents[1] / "config" / "legal_rulebook.json"


@dataclass(frozen=True)
class LegalRule:
    id: str
    issue: str
    title: str
    citation: str
    message: str
    min: float | None = None
    max: float | None = None
    when: str | None = None

    def check(self, value: float) -> str | None:
        if self.min is not None and value < self.min - 1e-9:
            return f"{self.message} (got {value:g}, minimum {self.min:g})"
        if self.max is not None and value > self.max + 1e-9:
            return f"{self.message} (got {value:g}, maximum {self.max:g})"
        return None


@lru_cache(maxsize=4)
def load_rulebook_data(path: str = str(RULEBOOK_PATH)) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Rulebook:
    def __init__(self, context: DealContext, supplier: Party, data: dict[str, Any] | None = None):
        self.data = data or load_rulebook_data()
        self.context = context
        self.supplier = supplier
        self.version: str = self.data["version"]
        juris = self.data["jurisdictions"][context.jurisdiction]
        self.jurisdiction_name: str = juris["name"]
        self.governing_law: str = context.governing_law or juris["governing_law"]
        self.dispute_resolution: str = context.dispute_resolution or juris["dispute_resolution"]
        self.rules: list[LegalRule] = []
        for raw in [*self.data["global"], *juris["rules"]]:
            rule = LegalRule(**{k: raw.get(k) for k in LegalRule.__dataclass_fields__})
            if self._applies(rule):
                self.rules.append(rule)
        self.mandatory_clauses: list[dict[str, str]] = self.data["mandatory_clauses"]
        self.force_majeure_events: list[str] = self.data["force_majeure_events"]

    def _applies(self, rule: LegalRule) -> bool:
        if rule.when is None:
            return True
        if rule.when == "supplier_is_msme":
            return self.supplier.is_msme
        return False

    def rule_ids(self) -> list[str]:
        return [r.id for r in self.rules]

    def rules_for(self, issue: str) -> list[LegalRule]:
        return [r for r in self.rules if r.issue == issue]

    def bounds(self, issue: str) -> tuple[float, float]:
        lo, hi = -math.inf, math.inf
        for r in self.rules_for(issue):
            if r.min is not None:
                lo = max(lo, r.min)
            if r.max is not None:
                hi = min(hi, r.max)
        return lo, hi

    def check_terms(self, terms: Terms) -> list[RuleViolation]:
        out: list[RuleViolation] = []
        for rule in self.rules:
            if rule.issue not in terms:
                continue
            problem = rule.check(terms[rule.issue])
            if problem:
                out.append(RuleViolation(rule_id=rule.id, category="legal", severity="block", message=problem,
                                         issue=rule.issue, citation=rule.citation))
        return out

    # ------------------------------------------------------------------ policy setup

    def sanitize_envelope(self, envelope: Envelope, issues: list[IssueSpec]) -> tuple[Envelope, list[RuleViolation]]:
        """Clamp a mandate into the legal envelope so agents can never be *instructed* to break the law."""
        findings: list[RuleViolation] = []
        mandate = {}
        for spec in issues:
            m = envelope.mandate[spec.key]
            lo, hi = self.bounds(spec.key)
            ideal = min(max(m.ideal, lo), hi)
            limit = min(max(m.limit, lo), hi)
            if ideal == limit:
                raise ValueError(f"{envelope.role} mandate for '{spec.key}' lies entirely outside the legal range "
                                 f"[{lo:g}, {hi:g}] - fix the policy envelope")
            if ideal != m.ideal or limit != m.limit:
                rule_ids = ",".join(r.id for r in self.rules_for(spec.key)) or "LEGAL"
                findings.append(RuleViolation(
                    rule_id=rule_ids, category="legal", severity="warn", issue=spec.key, private=True,
                    message=(f"Mandate for {spec.label} adjusted to the legal range at policy setup "
                             f"(ideal {m.ideal:g}->{ideal:g}, limit {m.limit:g}->{limit:g})"),
                ))
            mandate[spec.key] = m.model_copy(update={"ideal": ideal, "limit": limit})
        return envelope.model_copy(update={"mandate": mandate}), findings
