# reporting/mix_targets.py
"""
Theoretical vs buildable mixes: what the optimum ASKS FOR, and what the Mix Lab
can actually pour today.

The sweep designs against a continuous density spectrum on purpose — the
regression predicts strengths for densities nobody has batched yet, and the
winning densities are the SPECIFICATION handed to the mix team.  That only
works if two things are never ambiguous:

  * which of the winning densities are real mixes and which are interpolated,
    per zone, and how far each interpolation reaches from the nearest data;
  * what the same design is worth today, restricted to mixes on record.

Both live here, plus the development spec: target density, the strength that
target has to hit, whether it already exists, and the weight each new mix buys.

No Qt, no I/O, no sweep execution — pure functions over a density list, the Mix
Lab's density records and a mix-property callable, so the report, the console
summary and the tests all read the same numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence
from errors import domain_only

# A winning density within this many pcf of a real mix counts as that mix: the
# team batches to a target, not to four decimal places, and the sweep's density
# grid is finer than anyone can weigh out.
REAL_MIX_TOL_PCF = 0.25

# Interpolation reach beyond which a predicted strength stops being a read of
# the data and starts being an extrapolation.  The regression is a straight
# line through a handful of points; inside a short gap between two real mixes
# it interpolates, far outside the cluster it just continues the line.
REACH_TRUSTED_PCF = 2.0
REACH_STRETCHED_PCF = 5.0


@dataclass
class DensityVerdict:
    """One winning density, classified against the Mix Lab's real mixes."""
    density_pcf: float
    zone: Optional[str] = None          # e.g. "bottom flange"
    is_real: bool = False
    matched_mix_pcf: Optional[float] = None     # the real mix it lands on
    below_pcf: Optional[float] = None           # nearest real mix below
    above_pcf: Optional[float] = None           # nearest real mix above
    fc_psi: Optional[float] = None              # required strengths at target
    ft_psi: Optional[float] = None

    @property
    def reach_pcf(self) -> Optional[float]:
        """Distance to the nearest real mix — how far the prediction reaches.

        None for a density that IS a real mix.  For an interpolated density
        bracketed on both sides this is the smaller of the two gaps; outside
        the data range it is the distance to the single nearest point, which
        is the honest number because there is nothing on the far side.
        """
        if self.is_real:
            return None
        gaps = [abs(self.density_pcf - m)
                for m in (self.below_pcf, self.above_pcf) if m is not None]
        return min(gaps) if gaps else None

    @property
    def bracketed(self) -> bool:
        """True when real mixes exist on BOTH sides — a true interpolation."""
        return self.below_pcf is not None and self.above_pcf is not None

    @property
    def confidence(self) -> str:
        if self.is_real:
            return "on record"
        r = self.reach_pcf
        if r is None:
            return "no data"
        if not self.bracketed:
            return "EXTRAPOLATED"
        if r <= REACH_TRUSTED_PCF:
            return "interpolated (short reach)"
        if r <= REACH_STRETCHED_PCF:
            return "interpolated (stretched)"
        return "interpolated (far — treat as a guess)"


@dataclass
class MixTargetSpec:
    """The whole theoretical-vs-buildable picture for one sweep result."""
    verdicts: List[DensityVerdict] = field(default_factory=list)
    theoretical_weight_lb: Optional[float] = None
    buildable_weight_lb: Optional[float] = None
    buildable_mixes_pcf: Optional[List[float]] = None
    existing_pcf: List[float] = field(default_factory=list)
    regression_n: Optional[int] = None
    regression_source: Optional[str] = None
    payoff: List[dict] = field(default_factory=list)   # incremental development

    @property
    def all_real(self) -> bool:
        return bool(self.verdicts) and all(v.is_real for v in self.verdicts)

    @property
    def needs_development(self) -> List[DensityVerdict]:
        """Distinct target densities the team does not have a mix for."""
        seen, out = set(), []
        for v in self.verdicts:
            if v.is_real:
                continue
            k = round(v.density_pcf, 1)
            if k not in seen:
                seen.add(k)
                out.append(v)
        return out

    @property
    def buildable_penalty_lb(self) -> Optional[float]:
        if self.theoretical_weight_lb is None or self.buildable_weight_lb is None:
            return None
        return self.buildable_weight_lb - self.theoretical_weight_lb


def classify_densities(densities: Sequence[float],
                       existing_pcf: Sequence[float],
                       get_props: Optional[Callable] = None,
                       zone_names: Optional[Sequence[str]] = None,
                       tol_pcf: float = REAL_MIX_TOL_PCF) -> List[DensityVerdict]:
    """
    Classify each winning density as a real Mix Lab mix or an interpolation,
    recording the nearest real mixes on either side and the strengths the
    target must hit.

    ``get_props(density)`` supplies {'strength_c', 'strength_t'} — the sweep's
    own mix-property callable, so the spec quotes exactly the numbers the
    design was checked against.
    """
    real = sorted(float(d) for d in existing_pcf)
    out: List[DensityVerdict] = []
    for i, d in enumerate(densities):
        d = float(d)
        v = DensityVerdict(density_pcf=d)
        if zone_names is not None and i < len(zone_names):
            v.zone = zone_names[i]
        near = min(real, key=lambda m: abs(m - d)) if real else None
        if near is not None and abs(near - d) <= tol_pcf:
            v.is_real = True
            v.matched_mix_pcf = near
        else:
            below = [m for m in real if m < d]
            above = [m for m in real if m > d]
            v.below_pcf = max(below) if below else None
            v.above_pcf = min(above) if above else None
        if get_props is not None:
            try:
                p = get_props(d)
                v.fc_psi = float(p.get("strength_c")) if p.get("strength_c") is not None else None
                v.ft_psi = float(p.get("strength_t")) if p.get("strength_t") is not None else None
            except Exception as _exc:
                domain_only(_exc)   # a bug is not a skippable condition
                pass
        out.append(v)
    return out


def zone_names_for(n_zones: int) -> List[str]:
    """Human names for an I-beam stack, bottom→top."""
    if n_zones <= 1:
        return ["uniform hull"]
    if n_zones == 2:
        return ["bottom", "top"]
    names = ["bottom flange"] + ["web"] * (n_zones - 2) + ["top flange"]
    if n_zones > 3:
        k = 1
        for i in range(1, n_zones - 1):
            names[i] = f"web {k}"
            k += 1
    return names


def format_spec(spec: MixTargetSpec, width: int = 74) -> str:
    """The hand-to-the-mix-team spec, as printable text."""
    bar = "─" * width
    L = ["", "  MIX DEVELOPMENT TARGETS — what the optimum is asking for", f"  {bar}"]

    if spec.theoretical_weight_lb is not None:
        L.append(f"    Theoretical optimum      {spec.theoretical_weight_lb:7.2f} lb"
                 "   ← the design target (assumes mixes below)")
    if spec.buildable_weight_lb is not None:
        L.append(f"    Achievable with mixes    {spec.buildable_weight_lb:7.2f} lb"
                 "   ← fallback: mixes currently on record")
        if spec.buildable_mixes_pcf:
            L.append("                             "
                     + "using [" + ", ".join(f"{m:g}" for m in spec.buildable_mixes_pcf)
                     + "] pcf")
        pen = spec.buildable_penalty_lb
        if pen is not None:
            L.append(f"    Cost of developing nothing  {pen:+6.2f} lb")

    L += ["", "    TARGET MIXES (bottom→top)", f"    {'-' * (width - 4)}"]
    for v in spec.verdicts:
        head = f"      {v.density_pcf:6.1f} pcf"
        if v.zone:
            head += f"  {v.zone:<14}"
        L.append(head + f"  {v.confidence}")
        strengths = []
        if v.fc_psi is not None:
            strengths.append(f"f'c ≥ {v.fc_psi:.0f} psi")
        if v.ft_psi is not None:
            strengths.append(f"f_t ≥ {v.ft_psi:.0f} psi")
        if strengths:
            L.append("               required: " + ",  ".join(strengths)
                     + "   (allowables, factor of safety already applied)")
        if v.is_real:
            L.append(f"               already on record at {v.matched_mix_pcf:g} pcf"
                     " — no development needed")
        else:
            lo = f"{v.below_pcf:g}" if v.below_pcf is not None else "—"
            hi = f"{v.above_pcf:g}" if v.above_pcf is not None else "—"
            reach = v.reach_pcf
            L.append(f"               nearest real mixes: {lo} pcf below, "
                     f"{hi} pcf above"
                     + (f"; reaches {reach:.1f} pcf from data" if reach is not None else ""))
            if not v.bracketed:
                L.append("               NOT bracketed by real data — the "
                         "regression is extrapolating here.")

    todo = spec.needs_development
    L += ["", f"    NEEDS DEVELOPMENT: {len(todo)} mix"
          + ("" if len(todo) == 1 else "es")
          + (" — " + ", ".join(f"{v.density_pcf:g} pcf" for v in todo)
             if todo else " — none, the optimum is buildable today")]

    if spec.payoff:
        L += ["", "    PROJECTED PAYOFF — weight as each target is added",
              f"    {'-' * (width - 4)}"]
        _base = spec.buildable_weight_lb
        for row in spec.payoff:
            note = row.get("note", "")
            w = row.get("weight_lb")
            delta = ""
            if w is not None and _base is not None:
                delta = ("      —" if abs(w - _base) < 0.005
                         else f"  {w - _base:+6.2f}")
            L.append(f"      {row['label']:<32}"
                     + (f"{w:7.2f} lb" if w is not None else "     n/a")
                     + delta
                     + (f"   {note}" if note else ""))
        # The single most actionable line in the whole spec: if no mix pays off
        # on its own, developing them one at a time reads as wasted lab work
        # right up until the last one lands.
        solo = [r for r in spec.payoff if r.get("note") == "developed alone"]
        if solo and _base is not None:
            gains = [r for r in solo
                     if r.get("weight_lb") is not None
                     and r["weight_lb"] < _base - 0.005]
            if not gains:
                L += ["",
                      "      NOTE: no target pays off ALONE — the saving needs "
                      "them together.",
                      "      An I-beam pairs a heavy flange with a light web, and "
                      "the pour floor",
                      "      needs >= 2 pcf between batches, so a new mix with "
                      "nothing to pair",
                      "      with changes nothing.  Budget the whole set, or the "
                      "first mix will",
                      "      look like wasted lab work."]
            elif len(gains) < len(solo):
                best = min(gains, key=lambda r: r["weight_lb"])
                L += ["", f"      DEVELOP FIRST: {best['label']} — the only "
                      "target that pays off on its own."]

    if spec.regression_n is not None:
        L += ["", "    CAVEAT — the strengths above are PREDICTIONS", f"    {bar[:width-4]}"]
        L.append(f"      The density→strength regression is fitted to "
                 f"n = {spec.regression_n} point"
                 + ("" if spec.regression_n == 1 else "s")
                 + (f" ({spec.regression_source})" if spec.regression_source else "") + ".")
        L.append("      With a sample this small a target's predicted f'c and f_t are")
        L.append("      an EXTRAPOLATION, not a measurement.  Hitting the density is")
        L.append("      necessary but NOT sufficient: batch the mix, test it, and add")
        L.append("      the result to the Mix Lab.  If the real strength lands below")
        L.append("      the value above, this design is not valid at that density.")
    L.append("")
    return "\n".join(L)
