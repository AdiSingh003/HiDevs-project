"""Negotiation strategy primitives (private to each agent).

* :class:`ConcessionSchedule` - Faratin-style time-dependent tactic. ``beta < 1`` is Boulware (hold
  firm, concede late), ``beta = 1`` linear, ``beta > 1`` Conceder.
* :class:`OpponentModel` - frequency model (HardHeaded-style): issues the opponent refuses to move
  on are assumed to matter more to them.
* :func:`tradeoff_offer` - builds an offer at a target utility that concedes preferentially on the
  issues the opponent values most relative to what they cost us (a smoothed fractional knapsack
  solved by water-filling), never conceding beyond what the opponent is currently asking for.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Direction, IssueSpec, Terms
from .utility import EPS, UtilityModel


@dataclass
class ConcessionSchedule:
    opening: float
    reservation: float
    beta: float
    turns: int

    def target(self, k: int) -> float:
        """Target utility for this agent's k-th own move (0-based)."""
        if self.turns <= 1:
            return self.reservation
        x = min(1.0, max(0.0, k / (self.turns - 1)))
        alpha = x ** (1.0 / self.beta)
        return self.opening - alpha * (self.opening - self.reservation)

    def floor(self, k: int, tolerance: float) -> float:
        """Most generous utility the CFO-authorised schedule allows at move k."""
        return max(self.reservation, self.target(k) - tolerance)


class OpponentModel:
    def __init__(self, own: UtilityModel, learning_rate: float = 0.3, stay_threshold: float = 0.02):
        self.own = own
        self.keys = own.keys
        self.learning_rate = learning_rate
        self.stay_threshold = stay_threshold
        self.weights = {k: 1.0 / len(self.keys) for k in self.keys}
        self.offers: list[Terms] = []

    def observe(self, offer: Terms) -> None:
        if self.offers:
            prev = self.offers[-1]
            eta = self.learning_rate / (1 + 0.15 * len(self.offers))
            for k in self.keys:
                moved = abs(offer[k] - prev[k]) / (self.own.span(k) or 1.0)
                if moved < self.stay_threshold:
                    self.weights[k] += eta
            total = sum(self.weights.values())
            self.weights = {k: w / total for k, w in self.weights.items()}
        self.offers.append(dict(offer))

    @property
    def latest(self) -> Terms | None:
        return self.offers[-1] if self.offers else None

    def demand_cap(self, key: str) -> float:
        """Max fraction of our range worth conceding on ``key``: never give more than they ask."""
        if not self.offers:
            return 1.0
        return self.own.concession_of(key, self.offers[-1][key])

    def gain_rate(self, key: str) -> float:
        """Estimated opponent utility gained per unit of our concession fraction on ``key``."""
        own_span = self.own.span(key) or 1.0
        if self.offers:
            opp_span = abs(self.offers[0][key] - self.own.ideal[key])
        else:
            opp_span = own_span
        opp_span = max(opp_span, 0.05 * own_span)
        return self.weights[key] * min(3.0, own_span / opp_span)


def tradeoff_offer(own: UtilityModel, target: float, model: OpponentModel, sharpness: float,
                   previous: Terms | None = None, allow_retraction: bool = False) -> Terms:
    """Offer whose own utility is >= ``target`` while concentrating concessions where they buy most."""
    keys = own.keys
    w = own.weights
    budget = max(0.0, 1.0 - target)  # own utility we may give away (weights sum to 1)

    rho = {k: model.gain_rate(k) / max(w[k], EPS) for k in keys}
    top = max(rho.values()) or 1.0
    rho = {k: v / top for k, v in rho.items()}

    c_max = {k: model.demand_cap(k) for k in keys}
    c_min = {k: 0.0 for k in keys}
    if previous is not None and not allow_retraction:
        c_min = {k: min(own.concession_of(k, previous[k]), c_max[k]) for k in keys}

    lam = max(sharpness, 1e-3)

    def alloc(mu: float) -> dict[str, float]:
        return {k: min(c_max[k], max(c_min[k], (rho[k] - mu) / lam)) for k in keys}

    def spent(c: dict[str, float]) -> float:
        return sum(w[k] * c[k] for k in keys)

    lo_mu, hi_mu = min(rho.values()) - lam - 1.0, max(rho.values()) + 1.0
    if spent(alloc(lo_mu)) <= budget:
        conc = alloc(lo_mu)  # even maximal concession keeps us above target
    elif spent(alloc(hi_mu)) >= budget:
        conc = alloc(hi_mu)  # previous concessions already exhaust the budget
    else:
        for _ in range(80):
            mid = (lo_mu + hi_mu) / 2
            if spent(alloc(mid)) > budget:
                lo_mu = mid
            else:
                hi_mu = mid
        conc = alloc(hi_mu)

    offer = {k: own.snap_own_favour(k, own.at_concession(k, conc[k])) for k in keys}
    return offer


def accept_decision(own: UtilityModel, offer: Terms, next_target: float, reservation: float,
                    final_turn: bool) -> tuple[bool, str]:
    """AC_next with an AC_time fallback, both bounded by hard limits and the reservation utility."""
    breaches = own.violations(offer)
    if breaches:
        return False, f"outside mandate on {', '.join(breaches)}"
    u = own.utility(offer)
    if u < reservation - 1e-9:
        return False, "below reservation utility"
    if u >= next_target - 1e-9:
        return True, "AC_next: offer is at least as good as our next planned offer"
    if final_turn:
        return True, "AC_time: final round and offer beats our BATNA"
    return False, "our next offer would be better for us"


def issue_direction_word(spec: IssueSpec, role: str) -> str:
    return "lower" if spec.prefers(role) is Direction.LOWER else "higher"  # type: ignore[arg-type]
