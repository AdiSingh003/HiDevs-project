"""Legal Arbiter / Safe AI - the trusted third party of the negotiation.

The arbiter holds both sealed envelopes (like an escrow agent) and validates *every* move before
the counterpart sees it:

1. **Schema**   - every issue present, finite numbers, nothing extra.
2. **Legal**    - the public, jurisdiction-aware rulebook (MSMED Act s.15, EU Late Payment Directive, LD caps...).
3. **Policy**   - the sender's private CFO/Legal guardrails: mandate limits, budget cap, cost floor,
                  BATNA floor and the authorised concession pace (anti over-concession).
4. **Safety**   - outbound message screening: mandate leaks, prompt injection, PII, toxicity
                  (+ Lyzr RAI policy and Lyzr OPA tool-call guardrail when configured).

Private findings are flagged ``private=True`` so they are only ever shown to the sender.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import dataclass, field

from ..core.models import (Action, Decision, Envelope, Redaction, Role, RuleViolation, Scenario, SupplierProfile,
                           Terms, Verdict)
from ..core.pareto import BargainingSpace, FrontierPoint
from ..core.strategy import ConcessionSchedule
from ..core.utility import PRICE_KEY, UtilityModel
from ..lyzr.rai import SafeAIGateway
from .rego import Guardrail, guardrail_spec
from .rules import Rulebook
from .safety import extract_numbers, sanitise_message

POLICY_RULES = ["SCHEMA", "POLICY-MANDATE", "POLICY-BUDGET", "POLICY-MARGIN", "POLICY-BATNA", "POLICY-PACE",
                "POLICY-WALK", "SAFE-LEAK", "SAFE-INJECTION", "SAFE-PII", "SAFE-TOXICITY"]


@dataclass
class ReviewContext:
    role: Role
    own_turn: int
    rounds_remaining: int
    standing_offer: Terms | None
    public_numbers: list[float] = field(default_factory=list)
    session_id: str = ""


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def schedule_for(util: UtilityModel, max_rounds: int) -> ConcessionSchedule:
    strat = util.envelope.strategy
    opening = max(strat.opening_utility, util.reservation)
    return ConcessionSchedule(opening, util.reservation, strat.effective_beta, max_rounds)


class LegalArbiter:
    def __init__(self, scenario: Scenario, supplier: SupplierProfile, max_rounds: int | None = None,
                 gateway: SafeAIGateway | None = None, buyer_envelope: Envelope | None = None,
                 supplier_envelope: Envelope | None = None):
        self.scenario = scenario
        self.supplier = supplier
        self.max_rounds = max_rounds or scenario.max_rounds
        self.gateway = gateway
        self.rulebook = Rulebook(scenario.context, supplier.party)
        self.setup_findings: dict[str, list[RuleViolation]] = {}
        envelopes = {"buyer": buyer_envelope or scenario.buyer, "supplier": supplier_envelope or supplier.envelope}
        self.envelopes: dict[str, Envelope] = {}
        for role, env in envelopes.items():
            clean, findings = self.rulebook.sanitize_envelope(env, scenario.issues)
            self.envelopes[role] = clean
            self.setup_findings[role] = findings
        q = scenario.context.quantity
        self.utils: dict[str, UtilityModel] = {r: UtilityModel(scenario.issues, e, q) for r, e in self.envelopes.items()}
        self.schedules = {r: schedule_for(u, self.max_rounds) for r, u in self.utils.items()}
        self.space = BargainingSpace(scenario.issues, self.utils["buyer"], self.utils["supplier"])
        self._salts = {r: secrets.token_hex(16) for r in self.envelopes}
        self.rules_catalogue = self.rulebook.rule_ids() + POLICY_RULES
        self._guardrails: dict[str, Guardrail] = {}

    def guardrail(self, role: Role) -> Guardrail:
        """OPA guardrail compiled from the envelope this arbiter actually sealed (after legal clamping)."""
        if role not in self._guardrails:
            org = self.scenario.context.buyer.name if role == "buyer" else self.supplier.party.name
            self._guardrails[role] = guardrail_spec(self.utils[role], self.rulebook, org)
        return self._guardrails[role]

    # ------------------------------------------------------------------ sealing

    def commitments(self) -> dict[str, str]:
        """Hash commitments of each sealed envelope (logged publicly; salts revealed only to auditors)."""
        return {r: "sha256:" + hashlib.sha256((canonical(e.model_dump(mode="json")) + self._salts[r]).encode()).hexdigest()
                for r, e in self.envelopes.items()}

    def reveal_salt(self, role: str) -> str:
        return self._salts[role]

    # ------------------------------------------------------------------ rule families

    def schema_violations(self, terms: Terms | None) -> list[RuleViolation]:
        expected = {s.key for s in self.scenario.issues}
        if not isinstance(terms, dict):
            return [RuleViolation(rule_id="SCHEMA", category="schema", severity="block", message="offer is missing")]
        out = []
        missing = expected - set(terms)
        extra = set(terms) - expected
        if missing:
            out.append(RuleViolation(rule_id="SCHEMA", category="schema", severity="block",
                                     message=f"offer is missing issues: {', '.join(sorted(missing))}"))
        if extra:
            out.append(RuleViolation(rule_id="SCHEMA", category="schema", severity="block",
                                     message=f"offer contains unknown issues: {', '.join(sorted(extra))}"))
        for k, v in terms.items():
            if k in expected and (not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)):
                out.append(RuleViolation(rule_id="SCHEMA", category="schema", severity="block", issue=k,
                                         message=f"{k} must be a finite number"))
        return out

    def policy_violations(self, role: Role, terms: Terms, own_turn: int | None) -> list[RuleViolation]:
        util = self.utils[role]
        env = self.envelopes[role]
        out: list[RuleViolation] = []
        q = self.scenario.context.quantity
        for key in util.violations(terms):
            spec = util.specs[key]
            if key == PRICE_KEY and role == "buyer" and env.budget_cap and terms[key] * q > env.budget_cap + 1e-6:
                out.append(RuleViolation(rule_id="POLICY-BUDGET", category="policy", severity="block", issue=key, private=True,
                                         message=f"Contract value {terms[key] * q:,.2f} exceeds the CFO budget cap"))
                if terms[key] <= env.mandate[key].limit + 1e-9:
                    continue
            if key == PRICE_KEY and role == "supplier" and env.unit_cost and terms[key] < util.limit[key] - 1e-9 \
                    and terms[key] >= env.mandate[key].limit - 1e-9:
                out.append(RuleViolation(rule_id="POLICY-MARGIN", category="policy", severity="block", issue=key, private=True,
                                         message="Price is below unit cost plus the minimum margin set by Finance"))
                continue
            out.append(RuleViolation(rule_id="POLICY-MANDATE", category="policy", severity="block", issue=key, private=True,
                                     message=f"{spec.label} {spec.fmt(terms[key])} breaches the sealed mandate"))
        u = util.utility(terms)
        if not out and u < util.reservation - 1e-9:
            out.append(RuleViolation(rule_id="POLICY-BATNA", category="policy", severity="block", private=True,
                                     message=f"Offer utility {u:.3f} is worse than walking away (BATNA {util.reservation:.3f})"))
        if not out and own_turn is not None:
            floor = self.schedules[role].floor(own_turn, env.strategy.pace_tolerance)
            if u < floor - 1e-9:
                out.append(RuleViolation(rule_id="POLICY-PACE", category="policy", severity="block", private=True,
                                         message=(f"Concession pace exceeds the CFO-authorised schedule "
                                                  f"(utility {u:.3f} < floor {floor:.3f} at move {own_turn + 1})")))
        return out

    def approval_findings(self, terms: Terms) -> list[RuleViolation]:
        env = self.envelopes["buyer"]
        value = terms.get(PRICE_KEY, 0.0) * self.scenario.context.quantity
        if env.auto_approve_limit and value > env.auto_approve_limit:
            return [RuleViolation(rule_id="POLICY-APPROVAL", category="policy", severity="warn", private=True,
                                  message="Contract value exceeds the agent's delegated authority - CFO co-signature required")]
        return []

    # ------------------------------------------------------------------ move review

    async def review(self, decision: Decision, ctx: ReviewContext) -> Verdict:
        role = ctx.role
        violations: list[RuleViolation] = []
        checked: list[str] = ["SCHEMA"]
        terms_checked: Terms | None = None
        external: dict = {}

        if decision.action is Action.COUNTER:
            schema = self.schema_violations(decision.offer)
            violations += schema
            if not schema:
                terms_checked = decision.offer
                violations += self.rulebook.check_terms(decision.offer)
                violations += self.policy_violations(role, decision.offer, ctx.own_turn)
        elif decision.action is Action.ACCEPT:
            if ctx.standing_offer is None:
                violations.append(RuleViolation(rule_id="PROTOCOL", category="schema", severity="block",
                                                message="there is no counterpart offer to accept"))
            else:
                terms_checked = ctx.standing_offer
                violations += self.rulebook.check_terms(ctx.standing_offer)
                violations += self.policy_violations(role, ctx.standing_offer, ctx.own_turn)
        elif decision.action is Action.WALK_AWAY:
            standing_ok = ctx.standing_offer is not None and self.utils[role].acceptable(ctx.standing_offer)
            if standing_ok:
                violations.append(RuleViolation(rule_id="POLICY-WALK", category="policy", severity="block", private=True,
                                                message="Walking away from an offer that beats the BATNA is not authorised"))
            elif ctx.rounds_remaining > 0 and self.space.zopa_exists():
                violations.append(RuleViolation(rule_id="POLICY-WALK", category="policy", severity="block", private=True,
                                                message="Premature walk-away: rounds remain and the mandate still has room"))
        checked += self.rules_catalogue

        if terms_checked is not None and self.gateway is not None and not any(v.severity == "block" for v in violations):
            allowed, details = await self.gateway.screen_offer(role, terms_checked, f"{role}-agent", self.guardrail(role))
            external.update(details)
            if not allowed:
                violations.append(RuleViolation(rule_id="LYZR-OPA", category="policy", severity="block", private=True,
                                                 message=f"Lyzr Safe AI OPA guardrail denied the offer: {details.get('reason')}"))

        clean, findings = sanitise_message(decision.message, self.utils[role].private_numbers(), ctx.public_numbers)
        redactions = [Redaction(rule_id=f.rule_id, original=f.original, replacement=f.replacement, reason=f.reason)
                      for f in findings]
        if self.gateway is not None:
            screened, details = await self.gateway.screen_message(clean, role, ctx.session_id)
            external.update(details)
            if screened != clean:
                redactions.append(Redaction(rule_id="LYZR-RAI", original=clean, replacement=screened,
                                            reason=str(details.get("reason") or "Lyzr RAI policy transformation")))
                clean = screened
        if redactions:
            for r in redactions:
                violations.append(RuleViolation(rule_id=r.rule_id, category="safety", severity="redact",
                                                message=r.reason, private=r.rule_id == "SAFE-LEAK"))
        blocked = any(v.severity == "block" for v in violations)
        status = "blocked" if blocked else ("redacted" if redactions else "approved")
        return Verdict(status=status, violations=violations, redactions=redactions, rules_checked=checked,
                       sanitized_message=clean, external=external)

    # ------------------------------------------------------------------ agreement & mediation

    def review_agreement(self, terms: Terms) -> Verdict:
        violations = self.schema_violations(terms)
        if not violations:
            violations += self.rulebook.check_terms(terms)
            for role in ("buyer", "supplier"):
                violations += self.policy_violations(role, terms, own_turn=None)  # type: ignore[arg-type]
            violations += self.approval_findings(terms)
        blocked = any(v.severity == "block" for v in violations)
        return Verdict(status="blocked" if blocked else "approved", violations=violations,
                       rules_checked=self.rules_catalogue)

    def mediate(self, buyer_reservation: float | None = None) -> FrontierPoint | None:
        """Nash bargaining solution over the mutually acceptable region (None when no ZOPA exists).

        ``buyer_reservation`` lets the disagreement point reflect the buyer's live outside option
        (e.g. competing quotes in a multi-vendor RFQ), as Nash bargaining theory prescribes.
        """
        space = self.space
        if buyer_reservation is not None and buyer_reservation > space.d_buyer + 1e-9:
            space = BargainingSpace(self.scenario.issues, self.utils["buyer"], self.utils["supplier"],
                                    buyer_reservation=buyer_reservation)
        return space.nash_point()

    def utilities(self, terms: Terms) -> tuple[float, float]:
        return self.utils["buyer"].utility(terms), self.utils["supplier"].utility(terms)

    def public_numbers(self) -> list[float]:
        """Numbers anyone may quote: quantity, legal bounds and every number in the public RFQ text."""
        nums: list[float] = [self.scenario.context.quantity]
        for rule in self.rulebook.rules:
            nums += [x for x in (rule.min, rule.max) if x is not None]
        ctx = self.scenario.context
        public_text = " ".join([ctx.reference, ctx.title, ctx.item, ctx.item_description, ctx.incoterm,
                                ctx.delivery_location, ctx.buyer.address, self.supplier.party.address])
        nums += extract_numbers(public_text.replace("-", " "))
        return nums
