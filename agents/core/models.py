"""Domain models shared by every layer.

Everything a party keeps private lives in :class:`Envelope`; everything both parties may see
(the RFQ, the issue catalogue, offers and sanitised messages) lives in the other models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Role = Literal["buyer", "supplier"]
Terms = dict[str, float]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def other_role(role: Role) -> Role:
    return "supplier" if role == "buyer" else "buyer"


class Direction(str, Enum):
    LOWER = "lower"
    HIGHER = "higher"

    def flip(self) -> "Direction":
        return Direction.HIGHER if self is Direction.LOWER else Direction.LOWER


class IssueSpec(BaseModel):
    """A negotiable contract term. Public: both parties know the issue catalogue."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    unit: str = ""
    buyer_prefers: Direction
    step: float = Field(1.0, gt=0)
    decimals: int = Field(0, ge=0, le=4)
    description: str = ""

    def prefers(self, role: Role) -> Direction:
        return self.buyer_prefers if role == "buyer" else self.buyer_prefers.flip()

    def round(self, value: float) -> float:
        stepped = round(round(value / self.step) * self.step, self.decimals)
        return float(stepped)

    def fmt(self, value: float) -> str:
        number = f"{value:,.{self.decimals}f}"
        if self.unit in ("USD", "EUR", "INR", "GBP"):
            return f"{self.unit} {number}"
        if self.unit == "%":
            return f"{number}%"
        return f"{number} {self.unit}".strip()


class IssueMandate(BaseModel):
    """Private bounds for one issue: the aspiration (ideal) and the walk-away value (limit)."""

    model_config = ConfigDict(extra="forbid")

    ideal: float
    limit: float
    weight: float = Field(gt=0)

    @model_validator(mode="after")
    def _distinct(self) -> "IssueMandate":
        if self.ideal == self.limit:
            raise ValueError("ideal and limit must differ")
        return self


TACTIC_BETA = {"boulware": 0.35, "linear": 1.0, "conceder": 2.5}


class StrategyProfile(BaseModel):
    """Concession behaviour (Faratin, Sierra & Jennings time-dependent tactics + tit-for-tat)."""

    model_config = ConfigDict(extra="forbid")

    tactic: Literal["boulware", "linear", "conceder"] = "linear"
    beta: Optional[float] = Field(None, gt=0)
    reciprocity: float = Field(0.3, ge=0, le=1)
    tradeoff_sharpness: float = Field(0.2, gt=0, le=5)
    opening_utility: float = Field(1.0, ge=0.5, le=1.0)
    pace_tolerance: float = Field(0.03, ge=0, le=0.5)
    # Posturing: utility the agent refuses to concede below in open bargaining. Mediated proposals
    # are still judged against the true BATNA, which is how a neutral mediator breaks such standoffs.
    aspiration_floor: float = Field(0.0, ge=0, le=0.95)

    @property
    def effective_beta(self) -> float:
        return self.beta if self.beta is not None else TACTIC_BETA[self.tactic]


class Envelope(BaseModel):
    """A party's sealed policy envelope (mandate). Never leaves the owner + Legal Arbiter."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    mandate: dict[str, IssueMandate]
    batna_description: str = ""
    batna_terms: Optional[Terms] = None
    batna_utility: Optional[float] = Field(None, ge=0, le=1)
    min_utility: float = Field(0.0, ge=0, le=0.95)
    budget_cap: Optional[float] = Field(None, gt=0)
    auto_approve_limit: Optional[float] = Field(None, gt=0)
    unit_cost: Optional[float] = Field(None, gt=0)
    min_margin_pct: Optional[float] = Field(None, ge=0)
    strategy: StrategyProfile = Field(default_factory=StrategyProfile)
    notes: str = ""


class Party(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    address: str = ""
    signatory_name: str
    signatory_title: str
    is_msme: bool = False
    persona: str = ""


class DealContext(BaseModel):
    """The public RFQ that both sides negotiate over."""

    model_config = ConfigDict(extra="forbid")

    reference: str
    title: str
    item: str
    item_description: str = ""
    quantity: float = Field(gt=0)
    quantity_unit: str = "units"
    currency: str = "USD"
    incoterm: str = "DAP"
    delivery_location: str = ""
    jurisdiction: Literal["IN", "EU", "US", "UK"] = "IN"
    governing_law: str = ""
    dispute_resolution: str = ""
    buyer: Party
    # Buyer's standard T&C values for terms that are not negotiated in this RFQ (agreed terms always win).
    standard_terms: dict[str, float] = Field(default_factory=dict)


class SupplierProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    party: Party
    envelope: Envelope

    @field_validator("envelope")
    @classmethod
    def _supplier_role(cls, v: Envelope) -> Envelope:
        if v.role != "supplier":
            raise ValueError("supplier envelope must have role=supplier")
        return v


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    summary: str
    mode: Literal["bilateral", "rfq"] = "bilateral"
    tags: list[str] = Field(default_factory=list)
    context: DealContext
    issues: list[IssueSpec]
    buyer: Envelope
    suppliers: list[SupplierProfile]
    max_rounds: int = Field(12, ge=2, le=40)
    seed: int = 7

    @model_validator(mode="after")
    def _consistent(self) -> "Scenario":
        keys = {i.key for i in self.issues}
        if len(keys) != len(self.issues):
            raise ValueError("duplicate issue keys")
        if self.buyer.role != "buyer":
            raise ValueError("buyer envelope must have role=buyer")
        for env in [self.buyer, *[s.envelope for s in self.suppliers]]:
            if set(env.mandate) != keys:
                raise ValueError(f"{env.role} mandate must cover exactly the issues {sorted(keys)}")
        if self.mode == "bilateral" and len(self.suppliers) != 1:
            raise ValueError("bilateral scenarios need exactly one supplier")
        if self.mode == "rfq" and len(self.suppliers) < 2:
            raise ValueError("rfq scenarios need at least two suppliers")
        return self

    def issue(self, key: str) -> IssueSpec:
        for spec in self.issues:
            if spec.key == key:
                return spec
        raise KeyError(key)

    def supplier(self, supplier_id: str | None = None) -> SupplierProfile:
        if supplier_id is None:
            return self.suppliers[0]
        for s in self.suppliers:
            if s.id == supplier_id:
                return s
        raise KeyError(supplier_id)


# --------------------------------------------------------------------------- runtime


class Action(str, Enum):
    COUNTER = "counter"
    ACCEPT = "accept"
    WALK_AWAY = "walk_away"


class RuleViolation(BaseModel):
    rule_id: str
    category: Literal["schema", "legal", "policy", "safety"]
    severity: Literal["block", "redact", "warn"]
    message: str
    issue: Optional[str] = None
    citation: Optional[str] = None
    private: bool = False  # private-policy findings are only shown to the sender


class Redaction(BaseModel):
    rule_id: str
    original: str
    replacement: str
    reason: str


class Verdict(BaseModel):
    status: Literal["approved", "redacted", "blocked"]
    violations: list[RuleViolation] = Field(default_factory=list)
    redactions: list[Redaction] = Field(default_factory=list)
    rules_checked: list[str] = Field(default_factory=list)
    sanitized_message: str = ""
    external: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def public_violations(self) -> list[RuleViolation]:
        return [v for v in self.violations if not v.private]


class Decision(BaseModel):
    action: Action
    offer: Optional[Terms] = None
    message: str = ""
    source: Literal["engine", "llm", "rogue", "fallback", "mediator"] = "engine"
    target_utility: Optional[float] = None
    rationale: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(BaseModel):
    """Public record of one move, plus arbiter-only analytics (u_buyer/u_supplier/gap)."""

    index: int
    round: int
    actor: Role
    action: Action
    offer: Optional[Terms] = None
    message: str = ""
    source: str = "engine"
    verdict_status: str = "approved"
    public_violations: list[RuleViolation] = Field(default_factory=list)
    interventions: int = 0
    u_buyer: Optional[float] = None
    u_supplier: Optional[float] = None
    gap: Optional[float] = None
    ts: str = Field(default_factory=utcnow)


class Outcome(str, Enum):
    RUNNING = "running"
    AGREEMENT = "agreement"
    MEDIATED = "mediated_agreement"
    NO_DEAL = "no_deal"
    FAILED = "failed"


class EventVisibility(str, Enum):
    PUBLIC = "public"
    BUYER = "buyer"
    SUPPLIER = "supplier"
    ARBITER = "arbiter"


class NegotiationEvent(BaseModel):
    seq: int
    type: str
    ts: str = Field(default_factory=utcnow)
    visibility: list[EventVisibility] = Field(default_factory=lambda: [EventVisibility.PUBLIC])
    data: dict[str, Any] = Field(default_factory=dict)

    def visible_to(self, view: str) -> bool:
        if view == "god":
            return True
        vis = {v.value for v in self.visibility}
        if view == "public":
            return "public" in vis
        return "public" in vis or view in vis
