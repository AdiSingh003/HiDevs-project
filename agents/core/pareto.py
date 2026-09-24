"""Joint bargaining-space analysis (arbiter-only: needs both sealed envelopes).

For each issue the *efficient interval* runs from the buyer-best to the supplier-best point inside
both mandates. Inside it both linear value functions are un-clamped, so the Pareto frontier of the
additive utilities is exactly the fractional-knapsack sweep: move issues from the buyer-best end to
the supplier-best end in order of "supplier gain per unit of buyer loss". The Nash bargaining
solution (max of (U_b - d_b)(U_s - d_s)) is then found analytically on each frontier segment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Direction, IssueSpec, Terms
from .utility import EPS, UtilityModel


@dataclass
class FrontierPoint:
    u_buyer: float
    u_supplier: float
    terms: Terms
    positions: dict[str, float] = field(default_factory=dict)


@dataclass
class IssueInterval:
    key: str
    feasible: bool
    buyer_best: float
    supplier_best: float
    buyer_loss: float = 0.0  # weighted buyer utility lost moving buyer_best -> supplier_best
    supplier_gain: float = 0.0  # weighted supplier utility gained on the same move


def _in_lower_space(spec: IssueSpec, x: float) -> float:
    """Map a value into a space where the buyer prefers LOWER values."""
    return x if spec.buyer_prefers is Direction.LOWER else -x


def snap_between(spec: IssueSpec, x: float, lo: float, hi: float) -> float:
    """Nearest grid value to ``x`` that lies in [lo, hi]; falls back to x when the interval is off-grid."""
    lo, hi = min(lo, hi), max(lo, hi)
    step = spec.step
    candidates = [math.floor(x / step) * step, math.ceil(x / step) * step, round(x / step) * step]
    inside = [round(c, spec.decimals) for c in candidates if lo - EPS <= c <= hi + EPS]
    if inside:
        return float(min(inside, key=lambda c: abs(c - x)))
    return float(round(min(max(x, lo), hi), max(spec.decimals, 4)))


class BargainingSpace:
    def __init__(self, issues: list[IssueSpec], buyer: UtilityModel, supplier: UtilityModel,
                 buyer_reservation: float | None = None, supplier_reservation: float | None = None):
        self.issues = issues
        self.specs = {s.key: s for s in issues}
        self.buyer = buyer
        self.supplier = supplier
        self.d_buyer = buyer.reservation if buyer_reservation is None else buyer_reservation
        self.d_supplier = supplier.reservation if supplier_reservation is None else supplier_reservation
        self.intervals = {s.key: self._interval(s) for s in issues}
        self.infeasible_issues = [k for k, iv in self.intervals.items() if not iv.feasible]
        self._vertices: list[FrontierPoint] | None = None

    # ------------------------------------------------------------------ per-issue geometry

    def _interval(self, spec: IssueSpec) -> IssueInterval:
        k = spec.key
        # Work in a space where the buyer prefers lower values: buyer range [b_ideal, b_limit],
        # supplier range [s_limit, s_ideal].
        b_ideal, b_limit = _in_lower_space(spec, self.buyer.ideal[k]), _in_lower_space(spec, self.buyer.limit[k])
        s_ideal, s_limit = _in_lower_space(spec, self.supplier.ideal[k]), _in_lower_space(spec, self.supplier.limit[k])
        feasible = s_limit <= b_limit + EPS
        e_b = max(s_limit, b_ideal)
        e_s = min(b_limit, s_ideal)
        if e_b > e_s:  # ideals overlap: a point that fully satisfies both exists
            e_b = e_s = (e_b + e_s) / 2
        to_x = (lambda y: y) if spec.buyer_prefers is Direction.LOWER else (lambda y: -y)
        x_b, x_s = to_x(e_b), to_x(e_s)
        iv = IssueInterval(k, feasible, x_b, x_s)
        if feasible:
            iv.buyer_loss = self.buyer.weights[k] * (self.buyer.value(k, x_b) - self.buyer.value(k, x_s))
            iv.supplier_gain = self.supplier.weights[k] * (self.supplier.value(k, x_s) - self.supplier.value(k, x_b))
        return iv

    @property
    def feasible(self) -> bool:
        return not self.infeasible_issues

    def terms_at(self, positions: dict[str, float]) -> Terms:
        out = {}
        for k, iv in self.intervals.items():
            t = positions.get(k, 0.0)
            out[k] = iv.buyer_best + t * (iv.supplier_best - iv.buyer_best)
        return out

    def evaluate(self, terms: Terms) -> tuple[float, float]:
        return self.buyer.utility(terms), self.supplier.utility(terms)

    # ------------------------------------------------------------------ frontier

    def vertices(self) -> list[FrontierPoint]:
        """Frontier vertices from buyer-best to supplier-best (empty when some issue has no ZOPA)."""
        if self._vertices is not None:
            return self._vertices
        if not self.feasible:
            self._vertices = []
            return self._vertices
        positions = {k: 0.0 for k in self.intervals}
        movable = []
        for k, iv in self.intervals.items():
            if iv.buyer_loss <= EPS and iv.supplier_gain > EPS:
                positions[k] = 1.0  # free gain for the supplier
            elif iv.buyer_loss > EPS and iv.supplier_gain > EPS:
                movable.append(k)
        movable.sort(key=lambda k: self.intervals[k].supplier_gain / self.intervals[k].buyer_loss, reverse=True)
        pts = [self._point(positions)]
        for k in movable:
            positions = {**positions, k: 1.0}
            pts.append(self._point(positions))
        self._vertices = pts
        return pts

    def _point(self, positions: dict[str, float]) -> FrontierPoint:
        terms = self.terms_at(positions)
        ub, us = self.evaluate(terms)
        return FrontierPoint(ub, us, terms, dict(positions))

    def frontier(self, samples_per_segment: int = 6) -> list[FrontierPoint]:
        verts = self.vertices()
        if len(verts) <= 1:
            return list(verts)
        out = [verts[0]]
        for a, b in zip(verts, verts[1:]):
            moving = [k for k in b.positions if b.positions[k] != a.positions[k]]
            for i in range(1, samples_per_segment + 1):
                s = i / samples_per_segment
                pos = dict(a.positions)
                for k in moving:
                    pos[k] = a.positions[k] + s * (b.positions[k] - a.positions[k])
                out.append(self._point(pos))
        return out

    def max_buyer_utility_at(self, u_supplier: float) -> float | None:
        """Highest buyer utility attainable while giving the supplier at least ``u_supplier``."""
        verts = self.vertices()
        if not verts:
            return None
        if u_supplier <= verts[0].u_supplier:
            return verts[0].u_buyer
        for a, b in zip(verts, verts[1:]):
            if a.u_supplier - EPS <= u_supplier <= b.u_supplier + EPS:
                span = b.u_supplier - a.u_supplier
                s = 0.0 if span <= EPS else (u_supplier - a.u_supplier) / span
                return a.u_buyer + s * (b.u_buyer - a.u_buyer)
        return None

    # ------------------------------------------------------------------ Nash bargaining solution

    def zopa_exists(self) -> bool:
        return self.nash_point(round_terms=False) is not None

    def nash_point(self, round_terms: bool = True) -> FrontierPoint | None:
        verts = self.vertices()
        if not verts:
            return None
        d_b, d_s = self.d_buyer, self.d_supplier
        best: tuple[float, FrontierPoint] | None = None
        segments = list(zip(verts, verts[1:])) or [(verts[0], verts[0])]
        for a, b in segments:
            db = a.u_buyer - b.u_buyer
            ds = b.u_supplier - a.u_supplier
            candidates = [0.0, 1.0]
            if db > EPS and ds > EPS:
                s_star = (ds * (a.u_buyer - d_b) - db * (a.u_supplier - d_s)) / (2 * db * ds)
                candidates.append(min(1.0, max(0.0, s_star)))
            for s in candidates:
                ub = a.u_buyer - s * db
                us = a.u_supplier + s * ds
                if ub < d_b - EPS or us < d_s - EPS:
                    continue
                product = (ub - d_b) * (us - d_s)
                if best is None or product > best[0] + 1e-12:
                    pos = {k: a.positions[k] + s * (b.positions[k] - a.positions[k]) for k in a.positions}
                    best = (product, FrontierPoint(ub, us, self.terms_at(pos), pos))
        if best is None:
            return None
        point = best[1]
        if round_terms:
            point = self._rounded(point)
        return point

    def _rounded(self, point: FrontierPoint) -> FrontierPoint | None:
        """Snap a continuous point onto the issue grids while staying mutually acceptable."""
        terms = {}
        for k, x in point.terms.items():
            iv = self.intervals[k]
            terms[k] = snap_between(self.specs[k], x, iv.buyer_best, iv.supplier_best)
        ub, us = self.evaluate(terms)
        if self._mutually_ok(terms, ub, us):
            return FrontierPoint(ub, us, terms, point.positions)
        # Rounding pushed someone below reservation: nudge issue by issue towards whoever lost out.
        for _ in range(3 * len(terms)):
            needy = "buyer" if ub - self.d_buyer < us - self.d_supplier else "supplier"
            best_fix = None
            for k in terms:
                iv = self.intervals[k]
                spec = self.specs[k]
                target_end = iv.buyer_best if needy == "buyer" else iv.supplier_best
                direction = 1 if target_end > terms[k] else -1
                if abs(target_end - terms[k]) < EPS:
                    continue
                cand = dict(terms)
                cand[k] = snap_between(spec, terms[k] + direction * spec.step, iv.buyer_best, iv.supplier_best)
                cub, cus = self.evaluate(cand)
                score = min(cub - self.d_buyer, cus - self.d_supplier)
                if best_fix is None or score > best_fix[0]:
                    best_fix = (score, cand, cub, cus)
            if best_fix is None:
                break
            _, terms, ub, us = best_fix
            if self._mutually_ok(terms, ub, us):
                return FrontierPoint(ub, us, terms, point.positions)
        return None

    def _mutually_ok(self, terms: Terms, ub: float, us: float) -> bool:
        return (not self.buyer.violations(terms) and not self.supplier.violations(terms)
                and ub >= self.d_buyer - 1e-9 and us >= self.d_supplier - 1e-9)

    # ------------------------------------------------------------------ deal diagnostics

    def efficiency(self, terms: Terms) -> dict[str, float | None]:
        ub, us = self.evaluate(terms)
        best_ub = self.max_buyer_utility_at(us)
        verts = self.vertices()
        max_joint = max((p.u_buyer + p.u_supplier for p in self.frontier(12)), default=None)
        return {
            "u_buyer": ub,
            "u_supplier": us,
            "pareto_gap": None if best_ub is None else max(0.0, best_ub - ub),
            "joint_utility": ub + us,
            "max_joint_utility": max_joint,
            "nash_product": (ub - self.d_buyer) * (us - self.d_supplier),
            "frontier_vertices": float(len(verts)),
        }

    def normalised_gap(self, a: Terms, b: Terms) -> float:
        """Mean per-issue distance between two offers, normalised by the joint negotiation range."""
        total = 0.0
        for k in self.specs:
            values = [self.buyer.ideal[k], self.buyer.limit[k], self.supplier.ideal[k], self.supplier.limit[k]]
            rng = max(values) - min(values) or 1.0
            total += abs(a[k] - b[k]) / rng
        return total / len(self.specs)


def buyer_dominates(a: Terms, b: Terms, issues: list[IssueSpec]) -> bool:
    """True if deal ``a`` is at least as good as ``b`` for the buyer on every issue and better on one."""
    strictly = False
    for spec in issues:
        x, y = a[spec.key], b[spec.key]
        better = x < y if spec.buyer_prefers is Direction.LOWER else x > y
        worse = x > y if spec.buyer_prefers is Direction.LOWER else x < y
        if worse and abs(x - y) > EPS:
            return False
        if better and abs(x - y) > EPS:
            strictly = True
    return strictly
