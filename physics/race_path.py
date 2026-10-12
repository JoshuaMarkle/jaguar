# physics/race_path.py
"""
Dubins racing-line solver for ASCE concrete canoe courses.

Pure-math implementation of the six Dubins words (LSL, RSR, LSR, RSL,
RLR, LRL) over a strict ``min_turn_radius`` constraint.  Operates on a
list of buoy coordinates from ``race_course.RaceCourse`` and returns the
shortest kinodynamically feasible path between consecutive buoys.

Outputs the three quantities the Tier-1 GA needs:

    total_path_length     = total_arc_length + total_tangent_length    [m]
    total_arc_length      = sum of the curved (L/R) Dubins segments    [m]
    total_tangent_length  = sum of the straight (S) Dubins segments    [m]

No Qt, no Matplotlib (the optional ``plot_racing_line`` helper imports
matplotlib lazily so the module remains import-light).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np


# ── Constants ────────────────────────────────────────────────────────────────

_TWO_PI = 2.0 * math.pi
_EPS = 1e-9

# Six Dubins words, encoded as the segment-type triple they apply.
# 'L' = left arc, 'R' = right arc, 'S' = straight tangent.
_WORDS = ("LSL", "RSR", "LSR", "RSL", "RLR", "LRL")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mod2pi(x: float) -> float:
    """Wrap an angle into [0, 2π)."""
    v = x % _TWO_PI
    return v + _TWO_PI if v < 0.0 else v


# ── Per-word length formulas (Shkel & Lumelsky 2001) ────────────────────────
#
# Each function returns ``(t, p, q)`` where t and q are arc lengths in
# *radians* and p is either the straight tangent length divided by ρ
# (CSC words) or another arc length in radians (CCC words).  The total
# normalised length is ``t + p + q``.  A return of None means the word
# is not feasible for that (α, β, d) triple.

def _lsl(alpha: float, beta: float, d: float):
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    tmp0 = d + sa - sb
    p_sq = 2.0 + d * d - 2.0 * math.cos(alpha - beta) + 2.0 * d * (sa - sb)
    if p_sq < 0.0:
        return None
    tmp1 = math.atan2(cb - ca, tmp0)
    t = _mod2pi(-alpha + tmp1)
    p = math.sqrt(p_sq)
    q = _mod2pi(beta - tmp1)
    return t, p, q


def _rsr(alpha: float, beta: float, d: float):
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    tmp0 = d - sa + sb
    p_sq = 2.0 + d * d - 2.0 * math.cos(alpha - beta) + 2.0 * d * (sb - sa)
    if p_sq < 0.0:
        return None
    tmp1 = math.atan2(ca - cb, tmp0)
    t = _mod2pi(alpha - tmp1)
    p = math.sqrt(p_sq)
    q = _mod2pi(-beta + tmp1)
    return t, p, q


def _lsr(alpha: float, beta: float, d: float):
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    p_sq = -2.0 + d * d + 2.0 * math.cos(alpha - beta) + 2.0 * d * (sa + sb)
    if p_sq < 0.0:
        return None
    p = math.sqrt(p_sq)
    tmp = math.atan2(-ca - cb, d + sa + sb) - math.atan2(-2.0, p)
    t = _mod2pi(-alpha + tmp)
    q = _mod2pi(-_mod2pi(beta) + tmp)
    return t, p, q


def _rsl(alpha: float, beta: float, d: float):
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    p_sq = d * d - 2.0 + 2.0 * math.cos(alpha - beta) - 2.0 * d * (sa + sb)
    if p_sq < 0.0:
        return None
    p = math.sqrt(p_sq)
    tmp = math.atan2(ca + cb, d - sa - sb) - math.atan2(2.0, p)
    t = _mod2pi(alpha - tmp)
    q = _mod2pi(beta - tmp)
    return t, p, q


def _rlr(alpha: float, beta: float, d: float):
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    tmp_rlr = (6.0 - d * d + 2.0 * math.cos(alpha - beta)
               + 2.0 * d * (sa - sb)) / 8.0
    if abs(tmp_rlr) > 1.0:
        return None
    p = _mod2pi(_TWO_PI - math.acos(tmp_rlr))
    t = _mod2pi(alpha - math.atan2(ca - cb, d - sa + sb) + p / 2.0)
    q = _mod2pi(alpha - beta - t + p)
    return t, p, q


def _lrl(alpha: float, beta: float, d: float):
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    tmp_lrl = (6.0 - d * d + 2.0 * math.cos(alpha - beta)
               + 2.0 * d * (-sa + sb)) / 8.0
    if abs(tmp_lrl) > 1.0:
        return None
    p = _mod2pi(_TWO_PI - math.acos(tmp_lrl))
    t = _mod2pi(-alpha - math.atan2(ca - cb, d + sa - sb) + p / 2.0)
    q = _mod2pi(_mod2pi(beta) - alpha - t + p)
    return t, p, q


_WORD_FUNCS = {
    "LSL": _lsl,
    "RSR": _rsr,
    "LSR": _lsr,
    "RSL": _rsl,
    "RLR": _rlr,
    "LRL": _lrl,
}


# ── Public Dubins primitive ──────────────────────────────────────────────────

@dataclass
class DubinsPath:
    """A single shortest-Dubins solution between two configurations.

    Attributes
    ----------
    word        : one of "LSL", "RSR", "LSR", "RSL", "RLR", "LRL"
    radius      : turning radius ρ used for the solution [m]
    seg_lengths : (t, p, q) — arc lengths in radians for L/R segments;
                  for the middle 'S' segment of CSC words, p is the
                  straight length divided by ρ.  All three are non-negative.
    start       : (x, y, theta) — entry configuration [m, m, rad]
    end         : (x, y, theta) — exit configuration [m, m, rad]
    """
    word: str
    radius: float
    seg_lengths: tuple
    start: tuple
    end: tuple

    @property
    def length(self) -> float:
        """Total path length [m]."""
        t, p, q = self.seg_lengths
        return (t + p + q) * self.radius

    @property
    def arc_length(self) -> float:
        """Total length of curved (L/R) segments [m]."""
        t, p, q = self.seg_lengths
        types = self.word
        total = 0.0
        for seg_type, seg_len in zip(types, (t, p, q)):
            if seg_type in ("L", "R"):
                total += seg_len * self.radius
        return total

    @property
    def tangent_length(self) -> float:
        """Total length of straight (S) segments [m]."""
        t, p, q = self.seg_lengths
        types = self.word
        total = 0.0
        for seg_type, seg_len in zip(types, (t, p, q)):
            if seg_type == "S":
                total += seg_len * self.radius
        return total

    def sample_xy(self, step: float = 0.5) -> np.ndarray:
        """Return an (N×2) array of (x, y) samples along the path.

        ``step`` is the approximate spacing between samples in metres.
        """
        return _sample_path_xy(self, step)


def shortest_dubins(start: tuple, end: tuple, radius: float) -> DubinsPath:
    """Return the shortest Dubins path between two SE(2) configurations.

    Parameters
    ----------
    start  : (x, y, theta)  start configuration   [m, m, rad]
    end    : (x, y, theta)  goal  configuration   [m, m, rad]
    radius : float           strict minimum turning radius ρ  [m] — must be > 0

    Returns
    -------
    DubinsPath corresponding to whichever of the six Dubins words is shortest.
    """
    if radius <= 0.0:
        raise ValueError(f"min_turn_radius must be > 0, got {radius!r}")

    x0, y0, th0 = float(start[0]), float(start[1]), float(start[2])
    x1, y1, th1 = float(end[0]),   float(end[1]),   float(end[2])

    dx, dy = x1 - x0, y1 - y0
    D = math.hypot(dx, dy)
    d = D / radius                     # normalised distance

    # If start and goal coincide and headings match, return a zero-length path.
    if d < _EPS and abs(_mod2pi(th1 - th0)) < _EPS:
        return DubinsPath("LSL", radius, (0.0, 0.0, 0.0),
                          (x0, y0, th0), (x1, y1, th1))

    # If start and goal coincide but headings differ, only RLR/LRL can apply
    # — the CSC formulas degenerate to spurious p² that does not reflect a
    # real loop.  We still try every word and pick the shortest feasible one.
    theta = math.atan2(dy, dx) if d > _EPS else 0.0
    alpha = _mod2pi(th0 - theta)
    beta  = _mod2pi(th1 - theta)

    best = None  # (length_norm, word, (t, p, q))
    for word in _WORDS:
        result = _WORD_FUNCS[word](alpha, beta, d)
        if result is None:
            continue
        t, p, q = result
        L = t + p + q
        if best is None or L < best[0]:
            best = (L, word, (t, p, q))

    if best is None:
        # Fall back: this should never happen with finite radius because at
        # least one of LSL/RSR is always defined for non-degenerate inputs.
        # For the truly degenerate same-point/different-heading case, build
        # a single full-circle arc that lands on the right heading.
        delta = _mod2pi(th1 - th0)
        return DubinsPath("LRL" if delta <= math.pi else "RLR", radius,
                          (delta, 0.0, 0.0),
                          (x0, y0, th0), (x1, y1, th1))

    _, word, segs = best
    return DubinsPath(word, radius, segs,
                      (x0, y0, th0), (x1, y1, th1))


# ── Path sampling ────────────────────────────────────────────────────────────

def _seg_step(x: float, y: float, theta: float,
              seg_type: str, seg_len: float, radius: float) -> tuple:
    """Advance one Dubins segment by its full length.

    For 'L'/'R', ``seg_len`` is in radians.  For 'S', ``seg_len`` is in
    units of radius (so the physical length is ``seg_len * radius``).
    """
    if seg_type == "L":
        cx = x - radius * math.sin(theta)
        cy = y + radius * math.cos(theta)
        new_theta = theta + seg_len
        return (cx + radius * math.sin(new_theta),
                cy - radius * math.cos(new_theta),
                new_theta)
    if seg_type == "R":
        cx = x + radius * math.sin(theta)
        cy = y - radius * math.cos(theta)
        new_theta = theta - seg_len
        return (cx - radius * math.sin(new_theta),
                cy + radius * math.cos(new_theta),
                new_theta)
    # Straight segment — physical length = seg_len * radius.
    s_phys = seg_len * radius
    return (x + s_phys * math.cos(theta),
            y + s_phys * math.sin(theta),
            theta)


def _sample_segment_xy(x: float, y: float, theta: float,
                       seg_type: str, seg_len: float,
                       radius: float, step: float) -> list:
    """Return list of (x, y) samples along a single segment (excluding start)."""
    pts = []
    if seg_len <= _EPS:
        return pts
    if seg_type in ("L", "R"):
        phys_len = seg_len * radius
        n = max(2, int(math.ceil(phys_len / max(step, 1e-3))))
        for k in range(1, n + 1):
            sub = seg_len * (k / n)
            xn, yn, _ = _seg_step(x, y, theta, seg_type, sub, radius)
            pts.append((xn, yn))
    else:
        phys_len = seg_len * radius
        n = max(2, int(math.ceil(phys_len / max(step, 1e-3))))
        for k in range(1, n + 1):
            sub = seg_len * (k / n)
            xn, yn, _ = _seg_step(x, y, theta, seg_type, sub, radius)
            pts.append((xn, yn))
    return pts


def _sample_path_xy(path: DubinsPath, step: float) -> np.ndarray:
    pts = [(path.start[0], path.start[1])]
    x, y, th = path.start
    for seg_type, seg_len in zip(path.word, path.seg_lengths):
        if seg_len <= _EPS:
            continue
        pts.extend(_sample_segment_xy(x, y, th, seg_type, seg_len,
                                      path.radius, step))
        x, y, th = _seg_step(x, y, th, seg_type, seg_len, path.radius)
    return np.asarray(pts, dtype=float)


# ── Course-level analyser ────────────────────────────────────────────────────

# Default minimum turning radius for a 4-person concrete canoe (m).  Most
# competition canoes can be pivoted in 2 m of water with strong stern
# strokes; tighter than 1.5 m is unrealistic without losing speed.
DEFAULT_MIN_TURN_RADIUS_M = 2.0


@dataclass
class _Anchor:
    """Internal: a buoy anchor after collapsing colocated waypoints."""
    x: float
    y: float
    turn_radius: float       # buoy turn radius (geometric apex offset, m)
    turn_angle_deg: float    # signed heading change forced at this anchor
    label: str
    y_min: float | None = None   # optional y-range lower bound [m]
    y_max: float | None = None   # optional y-range upper bound [m]

    @property
    def is_bounded(self) -> bool:
        return self.y_min is not None and self.y_max is not None


def _collapse_waypoints(wps: list) -> list:
    """Merge consecutive waypoints at the same (x, y) into a single anchor.

    Buoy attributes are accumulated: turn_angle_deg sums (so two 90° buoys
    at the same point produce a 180° forced turn) and turn_radius takes the
    max (so a tight buoy dominates).  Bounded y-ranges (``y_min``/``y_max``)
    are inherited from the first waypoint that defines them.
    """
    anchors: list[_Anchor] = []
    for wp in wps:
        wp_ymin = getattr(wp, "y_min", None)
        wp_ymax = getattr(wp, "y_max", None)
        if anchors:
            last = anchors[-1]
            if abs(wp.x - last.x) < 1e-6 and abs(wp.y - last.y) < 1e-6:
                last.turn_angle_deg += float(wp.turn_angle)
                last.turn_radius = max(last.turn_radius, float(wp.turn_radius))
                if wp.label and not last.label:
                    last.label = wp.label
                if last.y_min is None and wp_ymin is not None:
                    last.y_min = float(wp_ymin)
                if last.y_max is None and wp_ymax is not None:
                    last.y_max = float(wp_ymax)
                continue
        anchors.append(_Anchor(
            x=float(wp.x), y=float(wp.y),
            turn_radius=float(wp.turn_radius),
            turn_angle_deg=float(wp.turn_angle),
            label=str(wp.label),
            y_min=None if wp_ymin is None else float(wp_ymin),
            y_max=None if wp_ymax is None else float(wp_ymax),
        ))
    return anchors


def _signed_turn_at(prev: _Anchor, here: _Anchor, nxt: _Anchor) -> float:
    """Return +1 for a left turn (CCW) at ``here``, −1 for a right turn."""
    cross = ((here.x - prev.x) * (nxt.y - prev.y)
             - (here.y - prev.y) * (nxt.x - prev.x))
    return 1.0 if cross >= 0.0 else -1.0


# ── Geometric tangent-and-arc primitives (multi-waypoint Dubins) ───────────
# These helpers replace the chord-heading + forced-buoy-arc graft used by an
# earlier version of analyse_course.  Each interior buoy becomes a turning
# circle, consecutive circles are joined by their common tangent (external
# for LSL/RSR, internal for LSR/RSL), and start/finish points are treated
# as zero-radius circles.  Because every transition is a tangent line
# meeting the circle perpendicular to its radius, G1 continuity is
# automatic at every arc/straight boundary.

@dataclass
class _BuoyCircle:
    """Internal: one turning circle placed at an interior buoy."""
    centre: np.ndarray   # (2,) (x, y) [m]
    radius: float        # [m]
    sigma: int           # +1 = canoe goes CCW (left turn), -1 = CW (right)
    label: str


def _cross2d(a: np.ndarray, b: np.ndarray) -> float:
    """2-D scalar cross product a × b."""
    return float(a[0] * b[1] - a[1] * b[0])


def _external_tangent(O1: np.ndarray, r1: float,
                      O2: np.ndarray, r2: float,
                      sigma: int) -> tuple:
    """Common external tangent for an LSL (sigma=+1) or RSR (sigma=-1)
    transition.  Returns ``(T_out_on_C1, T_in_on_C2)``.  Supports unequal
    radii.  T1→T2 is the canoe's motion direction along the straight.
    """
    diff = O2 - O1
    D = float(np.linalg.norm(diff))
    if D < _EPS:
        raise ValueError("external tangent undefined: circle centres coincide")
    alpha = math.atan2(diff[1], diff[0])
    arg = max(-1.0, min(1.0, (r2 - r1) / D))
    psi = alpha + sigma * math.acos(arg)
    n_hat = np.array([math.cos(psi), math.sin(psi)])
    return O1 - r1 * n_hat, O2 - r2 * n_hat


def _internal_tangent(O1: np.ndarray, r1: float, sigma1: int,
                      O2: np.ndarray, r2: float, sigma2: int,
                      label1: str = "?", label2: str = "?") -> tuple:
    """Common internal (crossing) tangent for an LSR (sigma1=+1, sigma2=-1)
    or RSL (sigma1=-1, sigma2=+1) transition.  Returns ``(T_out, T_in)``.

    Raises ``ValueError`` when ``D < r1 + r2`` — the canoe cannot perform
    a left↔right transition at the requested radii.
    """
    if sigma1 == sigma2:
        raise ValueError(
            "internal tangent requires opposite rounding directions"
        )
    diff = O2 - O1
    D = float(np.linalg.norm(diff))
    sum_r = r1 + r2
    if D + _EPS < sum_r:
        word = "left→right (LSR)" if sigma1 == 1 else "right→left (RSL)"
        raise ValueError(
            f"internal tangent infeasible between buoys {label1!r} and "
            f"{label2!r}: centre distance {D:.3f} m < r1+r2 = {sum_r:.3f} m "
            f"— cannot perform a {word} transition at the requested radii"
        )
    alpha = math.atan2(diff[1], diff[0])
    arg = max(-1.0, min(1.0, sum_r / D))
    beta = math.acos(arg)
    psi = alpha + sigma1 * (math.pi - beta)
    n_hat = np.array([math.cos(psi), math.sin(psi)])
    return O1 - r1 * n_hat, O2 + r2 * n_hat


def _point_to_circle_tangent(P: np.ndarray, O: np.ndarray, r: float,
                              sigma: int, exit_side: bool,
                              label: str = "?") -> np.ndarray:
    """Tangent point on circle (O, r) reached from external point P.

    sigma     : +1 if canoe traces the circle CCW, -1 if CW.
    exit_side : False → P is the predecessor (motion P → T → arc);
                True  → P is the successor   (motion arc → T → P).
    """
    diff_OP = P - O
    D = float(np.linalg.norm(diff_OP))
    if D + _EPS < r:
        raise ValueError(
            f"point ({P[0]:.2f}, {P[1]:.2f}) is inside buoy circle {label!r} "
            f"(distance {D:.3f} m < radius {r:.3f} m)"
        )
    if D < r + _EPS:
        return P.astype(float).copy()
    alpha_OP = math.atan2(diff_OP[1], diff_OP[0])
    delta_ang = math.acos(min(1.0, r / D))
    for sign in (+1.0, -1.0):
        ang = alpha_OP + sign * delta_ang
        T = O + r * np.array([math.cos(ang), math.sin(ang)])
        d = (P - T) if exit_side else (T - P)
        d_norm = float(np.linalg.norm(d))
        if d_norm < _EPS:
            continue
        d_hat = d / d_norm
        cross = _cross2d(d_hat, O - T)
        if (sigma == 1 and cross > 0.0) or (sigma == -1 and cross < 0.0):
            return T
    raise ValueError(
        f"could not resolve tangent from ({P[0]:.2f}, {P[1]:.2f}) to buoy "
        f"circle {label!r} for rounding direction sigma={sigma}"
    )


def _arc_length_signed(O: np.ndarray, r: float,
                        T_in: np.ndarray, T_out: np.ndarray,
                        sigma: int) -> float:
    """Arc length on circle (O, r) from T_in to T_out in direction sigma."""
    phi_in  = math.atan2(T_in[1]  - O[1], T_in[0]  - O[0])
    phi_out = math.atan2(T_out[1] - O[1], T_out[0] - O[0])
    if sigma == 1:                 # CCW
        d_phi = (phi_out - phi_in) % _TWO_PI
    else:                          # CW
        d_phi = (phi_in - phi_out) % _TWO_PI
    return r * d_phi


def _sample_arc_xy(O: np.ndarray, r: float,
                    T_in: np.ndarray, T_out: np.ndarray,
                    sigma: int, step: float) -> list:
    """(x, y) samples along an arc, excluding T_in, including T_out."""
    arc_len = _arc_length_signed(O, r, T_in, T_out, sigma)
    if arc_len <= _EPS:
        return [(float(T_out[0]), float(T_out[1]))]
    n = max(2, int(math.ceil(arc_len / max(step, 1e-3))))
    phi_in = math.atan2(T_in[1] - O[1], T_in[0] - O[0])
    sweep = arc_len / r
    direction = 1.0 if sigma == 1 else -1.0
    pts = []
    for k in range(1, n + 1):
        phi = phi_in + direction * sweep * (k / n)
        pts.append((float(O[0] + r * math.cos(phi)),
                    float(O[1] + r * math.sin(phi))))
    return pts


def _sample_tangent_xy(T_in: np.ndarray, T_out: np.ndarray,
                        step: float) -> list:
    """(x, y) samples along the straight tangent, excluding T_in."""
    seg = T_out - T_in
    L = float(np.linalg.norm(seg))
    if L <= _EPS:
        return []
    n = max(2, int(math.ceil(L / max(step, 1e-3))))
    pts = []
    for k in range(1, n + 1):
        t = k / n
        pts.append((float(T_in[0] + seg[0] * t),
                    float(T_in[1] + seg[1] * t)))
    return pts


def analyse_course(course, min_turn_radius: float | None = None,
                   sample_step_m: float = 0.5) -> dict:
    """Compute the geometric Dubins racing line for a ``RaceCourse``.

    Each interior buoy is represented by a turning circle of radius
    ``max(min_turn_radius, buoy.turn_radius)`` centred on the buoy
    itself, with rounding direction (CCW or CW) inferred from the chord
    cross-product at that buoy.  The path is the unique sequence of
    common tangent lines and circular arcs joining the start point, the
    buoy circles in order, and the finish point — which yields G1
    continuity at every transition.

    Parameters
    ----------
    course           : ``RaceCourse`` instance from ``race_course.py``.
    min_turn_radius  : strict minimum turning radius ρ [m] enforced at
                       every interior buoy.  A buoy may request a
                       *larger* ``turn_radius``; the larger value is
                       used at that buoy only.
    sample_step_m    : approximate spacing between samples in
                       ``path_xy`` [m].  Default 0.5 m matches the
                       legacy resolution; tests pass a tighter step to
                       verify G1 continuity at construction-level
                       precision.

    Returns
    -------
    dict with at minimum:

        path_xy                : (N×2) array of (x, y) samples [m]
        total_path_length_m    : total driven distance [m]
        total_arc_length_m     : sum of curved-segment distance [m]
        total_tangent_length_m : sum of straight-segment distance [m]
        pct_straight, pct_turn : percentages of the total
        Cs, Ct                 : Tier-1 GA capability targets in [0, 1]
        dubins_words           : list[str] of words used per tangent
                                 transition (LSL/RSR/LSR/RSL)
        min_turn_radius_m      : the ρ that was actually applied

    Backwards-compat keys (still populated, alias the new ones):
        straight_m  ≡ total_tangent_length_m
        turn_m      ≡ total_arc_length_m
        total_m     ≡ total_path_length_m

    Raises
    ------
    ValueError
        If ``min_turn_radius <= 0``, if two consecutive buoys are too
        close for the prescribed left↔right transition, or if the start
        / finish point lies inside a buoy turning circle.
    """
    if min_turn_radius is None:
        min_turn_radius = DEFAULT_MIN_TURN_RADIUS_M
    if min_turn_radius <= 0.0:
        raise ValueError(
            f"min_turn_radius must be > 0, got {min_turn_radius!r}"
        )

    if course is None or len(course.waypoints) < 2:
        return _empty_result(min_turn_radius)

    anchors = _collapse_waypoints(list(course.waypoints))
    if len(anchors) < 2:
        return _empty_result(min_turn_radius)

    n = len(anchors)
    start_xy  = np.array([anchors[0].x,  anchors[0].y],  dtype=float)
    finish_xy = np.array([anchors[-1].x, anchors[-1].y], dtype=float)

    # 1) Place a turning circle at every interior buoy.  Pass-through
    #    waypoints (turn_radius == 0) are skipped — they were always
    #    advisory and the chord-heading hack used to honour them broke
    #    G1 continuity at every bend.
    circles: list[_BuoyCircle] = []
    for i in range(1, n - 1):
        a = anchors[i]
        if a.turn_radius <= 0.0:
            continue
        prev_a, nxt_a = anchors[i - 1], anchors[i + 1]
        sigma = 1 if _signed_turn_at(prev_a, a, nxt_a) >= 0.0 else -1
        radius = max(float(min_turn_radius), float(a.turn_radius))
        circles.append(_BuoyCircle(
            centre=np.array([a.x, a.y], dtype=float),
            radius=radius,
            sigma=sigma,
            label=a.label or f"buoy_{i}",
        ))

    # 2a) If START / FINISH define a y-range, slide them along that
    #     range to minimise the first/last straight-tangent length.  For
    #     a tangent from external point P=(x_p, y) to circle (O, r) the
    #     length is √((x_p-x_o)² + (y-y_o)² − r²), which is minimised at
    #     y = y_o.  Clamp to the bounds.
    start_anchor, finish_anchor = anchors[0], anchors[-1]
    if circles:
        if start_anchor.is_bounded:
            y_target = float(circles[0].centre[1])
            start_xy[1] = max(start_anchor.y_min,
                              min(start_anchor.y_max, y_target))
        if finish_anchor.is_bounded:
            y_target = float(circles[-1].centre[1])
            finish_xy[1] = max(finish_anchor.y_min,
                               min(finish_anchor.y_max, y_target))

    # 2b) No interior buoys → a single straight line.
    if not circles:
        L = float(np.linalg.norm(finish_xy - start_xy))
        path_pts = [(float(start_xy[0]), float(start_xy[1]))]
        path_pts.extend(_sample_tangent_xy(start_xy, finish_xy, step=sample_step_m))
        return _build_result(np.asarray(path_pts, dtype=float),
                             0.0, L, [], float(min_turn_radius))

    # 3) Walk the circles, alternating arc and tangent line.
    total_arc, total_tangent = 0.0, 0.0
    words: list[str] = []
    path_pts: list[tuple] = [(float(start_xy[0]), float(start_xy[1]))]

    first = circles[0]
    T_in = _point_to_circle_tangent(
        start_xy, first.centre, first.radius, first.sigma,
        exit_side=False, label=first.label,
    )
    total_tangent += float(np.linalg.norm(T_in - start_xy))
    path_pts.extend(_sample_tangent_xy(start_xy, T_in, step=sample_step_m))

    for k, circle in enumerate(circles):
        is_last = (k == len(circles) - 1)
        if is_last:
            T_out = _point_to_circle_tangent(
                finish_xy, circle.centre, circle.radius, circle.sigma,
                exit_side=True, label=circle.label,
            )
            T_in_next = None
        else:
            nxt = circles[k + 1]
            if circle.sigma == nxt.sigma:
                T_out, T_in_next = _external_tangent(
                    circle.centre, circle.radius,
                    nxt.centre, nxt.radius, circle.sigma,
                )
                words.append("LSL" if circle.sigma == 1 else "RSR")
            else:
                T_out, T_in_next = _internal_tangent(
                    circle.centre, circle.radius, circle.sigma,
                    nxt.centre, nxt.radius, nxt.sigma,
                    label1=circle.label, label2=nxt.label,
                )
                words.append("LSR" if circle.sigma == 1 else "RSL")

        total_arc += _arc_length_signed(circle.centre, circle.radius,
                                        T_in, T_out, circle.sigma)
        path_pts.extend(_sample_arc_xy(
            circle.centre, circle.radius, T_in, T_out, circle.sigma,
            step=sample_step_m,
        ))

        T_target = finish_xy if is_last else T_in_next
        total_tangent += float(np.linalg.norm(T_target - T_out))
        path_pts.extend(_sample_tangent_xy(T_out, T_target, step=sample_step_m))

        T_in = T_in_next

    return _build_result(np.asarray(path_pts, dtype=float),
                         total_arc, total_tangent, words,
                         float(min_turn_radius))


def _build_result(path_xy: np.ndarray, total_arc: float,
                  total_tangent: float, words: list,
                  min_turn_radius: float) -> dict:
    total = total_arc + total_tangent
    if total < _EPS:
        pct_s, pct_t = 100.0, 0.0
    else:
        pct_s = total_tangent / total * 100.0
        pct_t = total_arc / total * 100.0
    return {
        "path_xy":                path_xy,
        "total_path_length_m":    total,
        "total_arc_length_m":     total_arc,
        "total_tangent_length_m": total_tangent,
        "pct_straight":           pct_s,
        "pct_turn":               pct_t,
        "Cs":                     pct_s / 100.0,
        "Ct":                     pct_t / 100.0,
        "dubins_words":           list(words),
        "min_turn_radius_m":      float(min_turn_radius),
        # Backwards-compatible aliases for existing UI / report consumers.
        "straight_m":             total_tangent,
        "turn_m":                 total_arc,
        "total_m":                total,
    }


def analyse_all_courses(courses: Iterable, min_turn_radius: float | None = None
                        ) -> dict[str, dict]:
    """Return per-course Dubins analysis dicts keyed by course name.

    ``min_turn_radius`` is in [m]; each result dict carries the same fields
    as ``analyse_course`` (lengths in [m], capability targets dimensionless).
    """
    return {c.name: analyse_course(c, min_turn_radius=min_turn_radius)
            for c in courses}


def race_capability_targets(course, min_turn_radius: float | None = None
                            ) -> tuple[float, float]:
    """Tier-1 GA hook: return (Cs, Ct) capability targets for one course.

    ``min_turn_radius`` in [m].  ``Cs`` is the fraction of the total Dubins
    driven distance spent on tangents (dimensionless, in [0, 1]); ``Ct`` is
    1 − Cs (fraction spent on arcs).  These are what
    ``race_simulator.run_strategy_evolution`` seeds the Pareto front with.
    """
    a = analyse_course(course, min_turn_radius=min_turn_radius)
    return float(a["Cs"]), float(a["Ct"])


# ── Optional plotting (lazy-imports matplotlib) ──────────────────────────────

def plot_racing_line(course, analysis: dict | None = None,
                     min_turn_radius: float | None = None):
    """Render a top-down map of the course and the Dubins driven path.

    Coordinates are in [m]; ``min_turn_radius`` in [m].  Returns a
    ``matplotlib.figure.Figure``.  Imports matplotlib lazily so the rest
    of the module stays import-light.
    """
    import matplotlib.pyplot as plt

    if analysis is None:
        analysis = analyse_course(course, min_turn_radius=min_turn_radius)

    wp_x = [w.x for w in course.waypoints]
    wp_y = [w.y for w in course.waypoints]
    path_xy = analysis["path_xy"]

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_edgecolor("#555")

    ax.plot(wp_x, wp_y, "o", color="#555", linewidth=0,
            markersize=5, alpha=0.6, label="Waypoints")

    ax.scatter([wp_x[0]], [wp_y[0]], color="#2ecc71", zorder=5, s=80,
               label="Start")
    ax.scatter([wp_x[-1]], [wp_y[-1]], color="#e74c3c", zorder=5, s=80,
               label="Finish")
    for wp in course.waypoints:
        if wp.turn_radius > 0:
            ax.scatter([wp.x], [wp.y], color="#f39c12", zorder=4, s=40,
                       marker="^")

    if len(path_xy) > 1:
        ax.plot(path_xy[:, 0], path_xy[:, 1],
                color="#00d2ff", linewidth=2.5, label="Dubins racing line")

    ax.set_title(
        f"{course.name}  —  {analysis['total_path_length_m']:.0f} m total  |  "
        f"{analysis['pct_straight']:.1f}% straight  /  "
        f"{analysis['pct_turn']:.1f}% turning\n"
        f"Cs = {analysis['Cs']:.3f}   Ct = {analysis['Ct']:.3f}   "
        f"ρ = {analysis['min_turn_radius_m']:.2f} m",
        color="white", fontsize=10,
    )
    ax.set_xlabel("x (m)", color="white")
    ax.set_ylabel("y (m)", color="white")
    ax.set_aspect("equal")
    ax.legend(fontsize=8, facecolor="#333", labelcolor="white",
              framealpha=0.8)
    fig.tight_layout(pad=1.0)
    return fig


# ── Internal: empty result ───────────────────────────────────────────────────

def _empty_result(min_turn_radius: float) -> dict:
    return {
        "path_xy":                np.zeros((0, 2)),
        "total_path_length_m":    0.0,
        "total_arc_length_m":     0.0,
        "total_tangent_length_m": 0.0,
        "pct_straight":           100.0,
        "pct_turn":               0.0,
        "Cs":                     1.0,
        "Ct":                     0.0,
        "dubins_words":           [],
        "min_turn_radius_m":      float(min_turn_radius),
        "straight_m":             0.0,
        "turn_m":                 0.0,
        "total_m":                0.0,
    }
