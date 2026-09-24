"""Multi-attribute utility (MAUT) for one party.

Each issue's value is linear between the party's *limit* (value 0) and *ideal* (value 1), clamped
to [-1, 1] so that offers outside the mandate register as negative instead of disappearing (this
lets agents perceive concessions the counterpart makes outside their acceptable range). Utility is
the weight-normalised sum of issue values.

The class is private to its owner: it is built from the sealed :class:`Envelope`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Direction, Envelope, IssueSpec, Role, Terms

PRICE_KEY = "unit_price"
EPS = 1e-9


class EnvelopeError(ValueError):
    """Raised when a policy envelope is internally inconsistent."""


@dataclass(frozen=True)
class PrivateNumber:
    label: str
    value: float
    tolerance: float


class UtilityModel:
    def __init__(self, issues: list[IssueSpec], envelope: Envelope, quantity: float):
        self.role: Role = envelope.role
        self.envelope = envelope
        self.quantity = float(quantity)
        self.specs = {spec.key: spec for spec in issues}
        self.keys = [spec.key for spec in issues]
        total_weight = sum(envelope.mandate[k].weight for k in self.keys)
        self.weights = {k: envelope.mandate[k].weight / total_weight for k in self.keys}
        self.ideal = {k: float(envelope.mandate[k].ideal) for k in self.keys}
        self.limit = {k: float(envelope.mandate[k].limit) for k in self.keys}
        self.adjustments: list[str] = []
        self._apply_financial_constraints()
        self._check_directions()
        self.reservation = self._reservation_utility()

    # ------------------------------------------------------------------ setup

    def _apply_financial_constraints(self) -> None:
        if PRICE_KEY not in self.specs:
            return
        env = self.envelope
        if self.role == "buyer" and env.budget_cap:
            ceiling = env.budget_cap / self.quantity
            if ceiling < self.limit[PRICE_KEY]:
                self.adjustments.append(
                    f"price limit tightened from {self.limit[PRICE_KEY]:.4f} to {ceiling:.4f} by the budget cap"
                )
                self.limit[PRICE_KEY] = ceiling
        if self.role == "supplier" and env.unit_cost:
            floor = env.unit_cost * (1 + (env.min_margin_pct or 0.0) / 100.0)
            if floor > self.limit[PRICE_KEY]:
                self.adjustments.append(
                    f"price floor raised from {self.limit[PRICE_KEY]:.4f} to {floor:.4f} by cost + minimum margin"
                )
                self.limit[PRICE_KEY] = floor

    def _check_directions(self) -> None:
        for k in self.keys:
            prefers = self.specs[k].prefers(self.role)
            ideal, limit = self.ideal[k], self.limit[k]
            if prefers is Direction.LOWER and not ideal < limit:
                raise EnvelopeError(f"{self.role}: '{k}' prefers lower values so ideal ({ideal}) must be below limit ({limit})")
            if prefers is Direction.HIGHER and not ideal > limit:
                raise EnvelopeError(f"{self.role}: '{k}' prefers higher values so ideal ({ideal}) must be above limit ({limit})")

    def _reservation_utility(self) -> float:
        env = self.envelope
        res = env.min_utility
        if env.batna_utility is not None:
            res = max(res, env.batna_utility)
        if env.batna_terms:
            filled = {k: env.batna_terms.get(k, self.limit[k]) for k in self.keys}
            res = max(res, self.utility(filled))
        return min(max(res, 0.0), 0.95)

    # ------------------------------------------------------------------ evaluation

    def value(self, key: str, x: float) -> float:
        ideal, limit = self.ideal[key], self.limit[key]
        v = (x - limit) / (ideal - limit)
        return max(-1.0, min(1.0, v))

    def utility(self, terms: Terms) -> float:
        return sum(self.weights[k] * self.value(k, terms[k]) for k in self.keys)

    def violations(self, terms: Terms) -> list[str]:
        """Issues whose value lies beyond this party's limit (hard-constraint breaches)."""
        out = []
        for k in self.keys:
            spec = self.specs[k]
            tol = spec.step * 1e-6 + EPS
            x, limit = terms[k], self.limit[k]
            if spec.prefers(self.role) is Direction.LOWER and x > limit + tol:
                out.append(k)
            elif spec.prefers(self.role) is Direction.HIGHER and x < limit - tol:
                out.append(k)
        return out

    def acceptable(self, terms: Terms, reservation: float | None = None) -> bool:
        res = self.reservation if reservation is None else reservation
        return not self.violations(terms) and self.utility(terms) >= res - 1e-9

    def contract_value(self, terms: Terms) -> float:
        return terms.get(PRICE_KEY, 0.0) * self.quantity

    # ------------------------------------------------------------------ concession space

    def span(self, key: str) -> float:
        return abs(self.limit[key] - self.ideal[key])

    def concession_of(self, key: str, x: float) -> float:
        """Fraction of this party's range conceded on ``key`` (0 at ideal, 1 at limit)."""
        return min(1.0, max(0.0, 1.0 - self.value(key, x)))

    def at_concession(self, key: str, c: float) -> float:
        return self.ideal[key] + c * (self.limit[key] - self.ideal[key])

    def ideal_terms(self) -> Terms:
        return dict(self.ideal)

    def snap_own_favour(self, key: str, x: float) -> float:
        """Round ``x`` onto the issue grid, rounding towards this party's ideal (never gives extra away)."""
        spec = self.specs[key]
        units = x / spec.step
        lower, upper = math.floor(units + 1e-9), math.ceil(units - 1e-9)
        prefers_lower = spec.prefers(self.role) is Direction.LOWER
        snapped = (lower if prefers_lower else upper) * spec.step
        snapped = round(snapped, spec.decimals)
        # stay inside this party's own mandate
        lo, hi = sorted((self.ideal[key], self.limit[key]))
        if snapped < lo - EPS or snapped > hi + EPS:
            snapped = round(min(max(x, lo), hi), spec.decimals)
        return float(snapped)

    # ------------------------------------------------------------------ privacy

    def private_numbers(self) -> list[PrivateNumber]:
        """Numbers that would leak the mandate if they appeared in a message."""
        env = self.envelope
        out: list[PrivateNumber] = []
        for k in self.keys:
            spec = self.specs[k]
            tol = max(spec.step / 2, 10 ** -(spec.decimals + 1))
            out.append(PrivateNumber(f"{k} limit", self.limit[k], tol))
            if env.mandate[k].limit != self.limit[k]:
                out.append(PrivateNumber(f"{k} mandate limit", env.mandate[k].limit, tol))
        if env.budget_cap:
            out.append(PrivateNumber("budget cap", env.budget_cap, env.budget_cap * 0.005))
        if env.auto_approve_limit:
            out.append(PrivateNumber("approval limit", env.auto_approve_limit, env.auto_approve_limit * 0.005))
        if env.unit_cost:
            out.append(PrivateNumber("unit cost", env.unit_cost, max(env.unit_cost * 0.002, 0.005)))
        if env.batna_terms:
            for k, v in env.batna_terms.items():
                spec = self.specs.get(k)
                tol = max(spec.step / 2, 10 ** -(spec.decimals + 1)) if spec else abs(v) * 0.002
                out.append(PrivateNumber(f"BATNA {k}", v, tol))
        return out
