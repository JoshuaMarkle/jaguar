# structural.py
import collections
import itertools
import logging
import threading
from collections import namedtuple

import numpy as np
from np_compat import trapezoid
from scipy.integrate import cumulative_trapezoid
from scipy.optimize import brentq
import config
from domain.models import StructuralResult

logger = logging.getLogger(__name__)

# Constructible wall-thickness increments (the trowel/mold can only hit real,
# repeatable dimensions).  1/32 in and 0.5 mm are the two practical build
# grids; the sweep tests the UNION so both imperial and metric target
# thicknesses are available.
_IN_PER_32ND = 1.0 / 32.0          # 0.03125 in
_IN_PER_HALF_MM = 0.5 / 25.4       # 0.0196850 in


def buildable_thickness_grid(t_min_in: float, t_max_in: float,
                             n_layers: int = 1,
                             dedupe_tol_in: float = 0.005) -> list:
    """
    Wall thicknesses the team can actually build, across [t_min_in, t_max_in].

    The wall is troweled in ``n_layers`` EQUAL layers (default 3 in the app),
    so the total must divide into layers that are themselves buildable.  A
    thickness is returned only if  total / n_layers  lands on the 1/32 in OR
    the 0.5 mm grid — i.e. the total is a whole number of  n_layers·(1/32 in)
    or  n_layers·(0.5 mm).  The two grids are unioned so both imperial and
    metric targets are available ("more options to check"), and points
    within ``dedupe_tol_in`` of each other are collapsed (an imperial and a
    metric mark that round to the same wall add sweep cost, not a distinct
    buildable option).

    n_layers = 1 reduces to the plain 1/32 in ∪ 0.5 mm grid.

    See ``describe_layer_breakdown`` for the human-readable layer schedule.
    """
    n = max(int(n_layers), 1)
    t_lo, t_hi = float(min(t_min_in, t_max_in)), float(max(t_min_in, t_max_in))
    marks = []
    for unit in (_IN_PER_32ND, _IN_PER_HALF_MM):
        step = n * unit                       # total advances one layer-unit
        k0 = int(np.ceil(t_lo / step - 1e-9))
        k1 = int(np.floor(t_hi / step + 1e-9))
        marks.extend(k * step for k in range(max(k0, 1), k1 + 1))
    if not marks:
        # Range too narrow to contain a buildable n_layers·unit total; fall
        # back to the smallest buildable layer stack at or above the minimum
        # (never thinner than the user asked for).
        base = n * min(_IN_PER_32ND, _IN_PER_HALF_MM)
        return [max(int(np.ceil(t_lo / base - 1e-9)), 1) * base]
    marks.sort()
    out = [marks[0]]
    for m in marks[1:]:
        if m - out[-1] >= dedupe_tol_in:
            out.append(m)
    return out


def describe_layer_breakdown(t_total_in: float, n_layers: int) -> str:
    """
    Human-readable build schedule for a total wall thickness: the per-layer
    thickness and which buildable grid it sits on (1/32 in or 0.5 mm).

    e.g.  describe_layer_breakdown(0.46875, 3) -> "3 layers × 0.156\" (5/32\")"
    """
    n = max(int(n_layers), 1)
    per = t_total_in / n
    # Prefer the imperial description when the layer is a clean 1/32 in count
    k32 = per / _IN_PER_32ND
    if abs(round(k32) - k32) < 1e-4:
        return f'{n} layers × {per:.3f}" ({int(round(k32))}/32")'
    per_mm = per * 25.4
    return f'{n} layers × {per:.3f}" ({per_mm:.1f} mm)'


_TROWEL_MIN_LAYER_IN = 1.0 / 16.0     # 0.0625 in — thinnest hand-troweled pass


def layer_interface_depths(t_total_in: float, n_layers: int) -> list:
    """
    Depths (from the FIRST-poured / mould face) of the interior interfaces
    between the ``n_layers`` equal troweled passes that build up the wall.

    n equal layers of thickness t/n leave n-1 interior interfaces at
    k·(t/n) for k = 1 … n-1.  Reinforcing mesh embedded between passes lands on
    one of these — not at an arbitrary t/2.  Returns [] for n_layers <= 1
    (a single pass has no interior interface).
    """
    n = max(int(n_layers), 1)
    t = float(t_total_in)
    return [k * t / n for k in range(1, n)]


def mesh_design_depth(t_total_in: float, n_layers: int) -> float:
    """
    Effective depth ``d`` to the reinforcing mesh for the punching check,
    derived from the ACTUAL buildable layer schedule instead of assuming t/2.

    The mesh is embedded on a real layer interface (see
    ``layer_interface_depths``).  We pick the shallowest interface that is still
    at or below the concrete-tension face by at least t/2 — i.e. the smallest
    interface depth ``>= t/2`` — so the design ``d`` is never LESS than the old
    t/2 assumption (a shallower mesh lowers punching capacity; choosing the
    deeper straddling interface keeps the check conservative and gives the crew
    an unambiguous, buildable placement).  Falls back to t/2 when no interior
    interface reaches t/2 (e.g. a single-pass wall).
    """
    t = float(t_total_in)
    half = t / 2.0
    deep = [d for d in layer_interface_depths(t, n_layers) if d >= half - 1e-9]
    return min(deep) if deep else half


def layer_schedule(t_total_in: float, n_layers: int) -> dict:
    """
    Single source of truth for the through-thickness construction schedule.

    Returns the per-layer thickness, its human-readable buildable description,
    the interior layer interfaces, the punching mesh design-depth that sits on
    one of them, and whether each pass is thick enough to hand-trowel
    (>= 1/16 in).  The report, the trowel-ability warning and the punching
    effective-depth all read these same numbers.
    """
    n = max(int(n_layers), 1)
    t = float(t_total_in)
    per = t / n
    return {
        "n_layers": n,
        "t_total_in": t,
        "per_layer_in": per,
        "description": describe_layer_breakdown(t, n),
        "interfaces_in": layer_interface_depths(t, n),
        "mesh_depth_in": mesh_design_depth(t, n),
        "trowel_min_in": _TROWEL_MIN_LAYER_IN,
        "trowel_ok": per >= _TROWEL_MIN_LAYER_IN - 1e-9,
    }


def compute_maneuver_load_factor(avg_speed, min_turn_radius, g=9.807):
    """
    Compute the Maneuverability Load Factor (MLF) for structural analysis.
    """
    # Centripetal acceleration [m/s^2]
    a_c = avg_speed ** 2 / max(min_turn_radius, 0.1)

    # Lateral load factor [dimensionless]
    n_lat = a_c / g

    # Maneuverability load factor with empirical coupling
    k_m = getattr(config, "MANEUVER_COUPLING_K", 0.5)
    mlf = 1.0 + k_m * n_lat

    return mlf


def _require_real_underwater_geometry(y_half_in, draft_in):
    """
    Raise unless ``y_half_in`` is geometry a stability figure can be computed
    from.  Returns the validated array.

    THIS EXISTS BECAUSE THE ENGINE DOES NOT RAISE.  compute_hydrostatics
    accepts an all-zero, all-NaN or negative half-breadth matrix and returns a
    NUMBER: KB = 0, BM_T = 0, KG = draft/2, so GM_T = -draft/2.  On this hull
    that is -2.2880 in -- a plausible-looking negative stability figure, of the
    right order, indistinguishable from a real marginal result.  The
    `except: return 0.0` below it was guarding a case that essentially never
    happens (a zero-SIZE array) while the case it names in its docstring --
    degenerate geometry -- sailed through as a measurement.

    Measured: of five degenerate inputs, exactly one (zero-size) raised.  The
    other four returned -2.2880.
    """
    arr = np.asarray(y_half_in, dtype=float)
    if arr.size == 0:
        raise ValueError("underwater half-breadth matrix is empty")
    if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
        raise ValueError(
            "underwater half-breadth matrix must be at least 2x2 "
            "(stations x waterlines); got shape %r" % (arr.shape,))
    if not np.all(np.isfinite(arr)):
        raise ValueError("underwater half-breadth matrix contains non-finite "
                         "values; a stability figure from it would be fiction")
    if np.any(arr < 0.0):
        raise ValueError("underwater half-breadth matrix contains NEGATIVE "
                         "half-breadths")
    if float(np.max(arr)) <= 0.0:
        raise ValueError("underwater half-breadth matrix is entirely zero; "
                         "there is no hull to compute stability for")
    if not np.isfinite(draft_in) or float(draft_in) <= 0.0:
        raise ValueError("draft must be positive and finite, got %r" % (draft_in,))
    return arr


def transverse_gmt_in(y_half_in, L_in, draft_in):
    """
    Transverse metacentric height GM_T [in] at the given waterline, from the
    underwater half-breadth matrix ``y_half_in`` (stations x waterlines, inches).

    Wraps the hull hydrostatics engine so the sweep's stability constraint uses
    the SAME definition the Hull Configurator sets its Min-GMT against:
        GM_T = KB + BM_T - KG,   BM_T = I_wp / V,   with KG taken at the
    waterline (compute_hydrostatics' convention).  The calculation is unit-
    consistent, so passing inches in returns inches out.

    RAISES on degenerate geometry; it does not return a number.  This used to
    `except Exception: return 0.0`, and zero is not neutral for a metacentric
    height -- it is the specific claim "neutrally stable", which is a real and
    alarming result.  A caught exception wearing that value is a stability
    statement nobody made.  Callers must handle the exception; there is no
    value that can stand in for "could not be computed".
    """
    from hull_generator import compute_hydrostatics
    arr = _require_real_underwater_geometry(y_half_in, draft_in)
    h = compute_hydrostatics(arr, float(L_in), float(draft_in))
    gmt = float(h["gmt"])
    if not np.isfinite(gmt):
        raise ValueError("hydrostatics returned a non-finite GM_T")
    return gmt


def transverse_hydro_terms_in(y_half_in, L_in, draft_in):
    """
    The SEPARATE terms of GM_T = KB + BM_T - KG, all inches: ``(kb, bm_t,
    gmt_form)``.

    ``transverse_gmt_in`` collapses these into the form-only number, which is
    unusable for a real-KG stability figure: recovering KB and BM_T from a
    single GM_T requires already knowing the KG that was assumed.  BM_T here is
    the hull-form stability CEILING -- no distribution of concrete can raise it,
    only the shape of the waterplane can (BM_T = I_wp / V).

    RAISES on degenerate geometry -- see transverse_gmt_in for why a numeric
    sentinel is the wrong answer here.  ``(0.0, 0.0, 0.0)`` was especially bad:
    KB = 0 and BM_T = 0 are not merely "unknown", they describe a hull with no
    buoyancy and no waterplane stiffness.
    """
    from hull_generator import compute_hydrostatics
    arr = _require_real_underwater_geometry(y_half_in, draft_in)
    h = compute_hydrostatics(arr, float(L_in), float(draft_in))
    out = (float(h["vcb"]), float(h["bmt"]), float(h["gmt"]))
    if not all(np.isfinite(v) for v in out):
        raise ValueError("hydrostatics returned non-finite KB/BM_T/GM_T")
    return out


def hull_mass_and_cg(cum_area, cum_Az, z_grid, z_bounds, zone_dens, x_ft,
                     rail=None, rail_density=None):
    """
    ``(weight_lbs, KG_in)`` of the SHELL for a zoned pour, from geometry alone.

    Both fall out of the same two integrals, so they are computed together.  The
    weight here is UNCONDITIONAL: it is what this stack of densities in this
    geometry weighs, with no accept/reject logic in the way.  That matters --
    ``evaluate_multizone_weight`` returns a large sentinel weight for a rejected
    configuration, and feeding that sentinel into a stability figure silently
    produces a hull so heavy the crew stops mattering, i.e. a plausible-looking
    GM_T that is pure artifact.  Variant studies must weigh the variant, not
    read the reject flag as a mass.
    """
    zd = [float(d) for d in zone_dens]
    m_tot = 0.0          # pcf * in^2 * ft  -> lb after /144
    mz_tot = 0.0
    prev_A = _cum_at(cum_area, z_grid, z_bounds[0])
    prev_Az = _cum_at(cum_Az, z_grid, z_bounds[0])
    for i in range(len(zd)):
        A_t = _cum_at(cum_area, z_grid, z_bounds[i + 1])
        Az_t = _cum_at(cum_Az, z_grid, z_bounds[i + 1])
        m_tot += zd[i] * float(trapezoid(A_t - prev_A, x_ft))
        mz_tot += zd[i] * float(trapezoid(Az_t - prev_Az, x_ft))
        prev_A, prev_Az = A_t, Az_t
    if rail is not None:
        rho = float(rail_density if rail_density is not None else zd[-1])
        A_r = rail.A_gw + rail.A_fil
        S_r = rail.A_gw * rail.gw_cent + rail.A_fil * rail.fil_cent
        m_tot += rho * float(trapezoid(A_r, x_ft))
        mz_tot += rho * float(trapezoid(S_r, x_ft))
    kg = float(mz_tot / m_tot) if abs(m_tot) > 1e-12 else 0.0
    return m_tot / 144.0, kg


def hull_vertical_cg_in(cum_area, cum_Az, z_grid, z_bounds, zone_dens, x_ft,
                        rail=None, rail_density=None):
    """
    KG of the SHELL itself [in above the keel], for a zoned pour.

    Exact for the zoned model: each zone's mass is its own density times its own
    area, and its first moment is that density times the zone's first moment
    about the keel, both already available as cumulative section quantities.

        KG = sum_i rho_i * INT (Az_i) dx  /  sum_i rho_i * INT (A_i) dx

    with A_i and Az_i the area and keel-datum first moment BETWEEN that zone's
    pour lines.  A gunwale rail, when present, is top-zone material sitting at
    its own centroid and is folded in the same way.

    WHY THIS EXISTS.  The stability figure the sweep gates on came from
    hull_generator.compute_hydrostatics, whose GM_T is ``bmt - (T - vcb)`` --
    i.e. it ASSUMES KG equals the waterline.  That is a hull-FORM estimate: it
    never sees where the concrete actually is, so an I-beam with heavy top
    flanges scores identically to a uniform light hull.  For a top-flanged
    section the real KG is HIGHER than the waterline, so the assumption is
    unconservative in exactly the case this suite optimises toward.
    """
    return hull_mass_and_cg(cum_area, cum_Az, z_grid, z_bounds, zone_dens,
                            x_ft, rail=rail, rail_density=rail_density)[1]


def transverse_gmt_design_in(kb_in, bm_t_in, kg_hull_in, hull_lbs,
                             crew_lbs=0.0, crew_kg_in=None):
    """
    GM_T [in] for a SPECIFIC design: ``KB + BM_T - KG``, with KG the real
    combined centre of gravity rather than the waterline.

        KG = (hull_lbs*KG_hull + crew_lbs*KG_crew) / (hull_lbs + crew_lbs)

    ``crew_kg_in`` is the kneeling crew's CG height above the keel.  It is an
    ASSUMPTION, not a measurement, and it dominates: the crew outweighs the hull
    roughly five to one, so their CG largely sets KG and therefore the stability
    margin.  It is exposed on config (CREW_VERTICAL_CG_IN) precisely so it is
    stated rather than buried -- pass crew_lbs=0 to get the bare-hull GM_T.
    """
    hull_lbs = float(hull_lbs)
    crew_lbs = float(crew_lbs)
    if crew_lbs > 0.0:
        kg_c = float(crew_kg_in if crew_kg_in is not None
                     else getattr(config, "CREW_VERTICAL_CG_IN", 12.0))
        tot = hull_lbs + crew_lbs
        kg = (hull_lbs * float(kg_hull_in) + crew_lbs * kg_c) / max(tot, 1e-12)
    else:
        kg = float(kg_hull_in)
    return float(kb_in) + float(bm_t_in) - kg


# Legacy crew-CG band [in above keel], kept only as the fallback when no crew
# anthropometry is supplied.  These were arbitrary round numbers bracketing a
# flat 12 in guess; the real band now comes from the biomechanical model and
# spans posture x torso angle, because POSTURE dominates (~8 in of hip height)
# and torso angle does not (~2 in).
CREW_KG_SENSITIVITY_IN = (10.0, 12.0, 14.0)


def stability_conventions(kb_in, bm_t_in, kg_hull_in, hull_lbs, crew_lbs,
                          gmt_form_in=None, crew_kg_in=None,
                          crew_kg_sweep=CREW_KG_SENSITIVITY_IN,
                          crew_kg_band=None, crew_kg_basis=None):
    """
    The THREE GM_T conventions for one design, side by side, plus the loaded
    value's sensitivity to the assumed crew CG.

    These are not interchangeable and a GM_T quoted without its KG basis is
    uninterpretable -- the same hull scores very differently under each:

      ``form``       KG = waterline.  A property of the SHAPE alone; blind to
                     where the concrete sits, so an I-beam with heavy top
                     flanges scores identically to a uniform light hull.  This
                     is the number comparable to a published hull-form figure.
      ``bare_hull``  KG = the real zoned-pour shell CG, no crew.  What the
                     empty canoe actually does.
      ``loaded``     KG = mass-weighted shell + crew.  The racing condition, and
                     the only one that describes a capsize.

    Only ``bare_hull`` and ``loaded`` respond to mix distribution; ``form`` is
    the hull-shape baseline they are measured against.  Returns a dict of plain
    floats -- no I/O, no formatting.

    ``crew_kg_in`` is the crew's CG height above the keel.  Pass the value from
    physics.biomechanics.crew_kg_in to use the anthropometric model; omitting it
    falls back to the flat config assumption.  ``crew_kg_band`` is an optional
    ``[(label, theta_deg, kg_in), ...]`` from crew_kg_band_in -- when given it
    REPLACES the arbitrary crew_kg_sweep, so the reported sensitivity spans real
    postures instead of round numbers.  ``crew_kg_basis`` labels where kg_c came
    from, so the report can never present a modelled value and an assumed one
    as though they carried the same weight.
    """
    kb, bm = float(kb_in), float(bm_t_in)
    kg_h = float(kg_hull_in)
    hull_lbs, crew_lbs = float(hull_lbs), float(crew_lbs)
    kg_c = float(crew_kg_in if crew_kg_in is not None
                 else getattr(config, "CREW_VERTICAL_CG_IN", 12.0))

    bare = transverse_gmt_design_in(kb, bm, kg_h, hull_lbs)
    loaded = transverse_gmt_design_in(kb, bm, kg_h, hull_lbs,
                                      crew_lbs=crew_lbs, crew_kg_in=kg_c)
    tot = hull_lbs + crew_lbs
    kg_combined = ((hull_lbs * kg_h + crew_lbs * kg_c) / tot
                   if tot > 1e-12 else kg_h)
    return {
        "kb_in": kb, "bm_t_in": bm,
        "kg_hull_in": kg_h, "kg_crew_in": kg_c, "kg_combined_in": kg_combined,
        "hull_lbs": hull_lbs, "crew_lbs": crew_lbs,
        # form-only: the caller's measured value when supplied, else the
        # definition itself (KG = waterline = KB + BM_T - GM_T  =>  T).
        "gmt_form_in": (float(gmt_form_in) if gmt_form_in is not None
                        else kb + bm - kg_h),
        "gmt_bare_hull_in": bare,
        "gmt_loaded_in": loaded,
        "crew_kg_basis": (str(crew_kg_basis) if crew_kg_basis
                          else "assumed flat value"),
        "crew_kg_sensitivity": [
            (float(c), transverse_gmt_design_in(kb, bm, kg_h, hull_lbs,
                                                crew_lbs=crew_lbs,
                                                crew_kg_in=float(c)))
            for c in crew_kg_sweep],
        # Posture x torso-angle band from the biomechanical model, when supplied.
        # Each entry is (posture, theta_deg, crew_KG_in, loaded_GM_T_in).
        "crew_kg_posture_band": ([
            (str(p), float(th), float(kg),
             transverse_gmt_design_in(kb, bm, kg_h, hull_lbs,
                                      crew_lbs=crew_lbs, crew_kg_in=float(kg)))
            for p, th, kg in crew_kg_band] if crew_kg_band else None),
        # Loaded GM_T under the OLD flat assumption, so the delta the model
        # introduces is always visible rather than silently replacing it.
        "gmt_loaded_legacy_in": transverse_gmt_design_in(
            kb, bm, kg_h, hull_lbs, crew_lbs=crew_lbs,
            crew_kg_in=float(getattr(config, "CREW_VERTICAL_CG_IN", 12.0))),
        "crew_kg_legacy_in": float(getattr(config, "CREW_VERTICAL_CG_IN", 12.0)),
    }


_PADDLER_MASS_KG = 95.25   # ASCE competition paddler mass [kg]
_IN3_PER_M3      = 61_023.74  # 1 m³ in cubic inches
_IN2_PER_M2      = 1_550.003  # 1 m² in square inches
_M_PER_IN        = 0.0254


def calculate_4_person_waterline(
    canoe_mass_kg: float,
    base_draft_in: float,
    vol_2p_m3: float,
    L_ft: float,
    B_wl_avg_m: float,
    flare_angle_deg: float = 0.0,
    rho_water: float = 1000.0,
    min_freeboard_m: float = 0.1524,
    crew_mass_4p_kg: float = None,
) -> tuple:
    """
    Calculate the 4-person waterline and minimum required mold depth.

    The hull sinks deeper with 4 paddlers; the freeboard walls above the
    2-person waterline supply the extra buoyancy.  Returns imperial inches.

    Parameters
    ----------
    canoe_mass_kg   : structural mass of the concrete hull [kg]
    base_draft_in   : 2-person design draft from Godzilla [in]
    vol_2p_m3       : hull displaced volume at base_draft [m³]
    L_ft            : waterline length [ft]
    B_wl_avg_m      : average full waterplane beam at base_draft [m]
    flare_angle_deg : outward flare of freeboard walls above waterline [deg]
    rho_water       : water density [kg/m³]
    min_freeboard_m : required freeboard above the 4-person waterline [m]
    crew_mass_4p_kg : total mass of the 4-person crew [kg]; None falls back to
                      4 × the nominal ASCE paddler mass.  Pass the SUM of the
                      user's actual 4-person weights so the freeboard floor
                      reflects the real heaviest load case, not a placeholder.

    Returns
    -------
    (waterline_4p_in, min_mold_depth_in) — both in inches
    """
    crew_4p = (4.0 * _PADDLER_MASS_KG if crew_mass_4p_kg is None
               else float(crew_mass_4p_kg))
    total_mass_4p = canoe_mass_kg + crew_4p
    req_vol_m3    = total_mass_4p / rho_water
    delta_vol_m3  = max(req_vol_m3 - vol_2p_m3, 0.0)

    L_m   = L_ft * 0.3048
    denom = L_m * B_wl_avg_m
    if denom < 1e-9:
        logger.warning("calculate_4_person_waterline: zero waterplane area; "
                       "falling back to draft + freeboard.")
        return base_draft_in, base_draft_in + min_freeboard_m / _M_PER_IN

    tan_theta = np.tan(np.radians(flare_angle_deg))

    if abs(tan_theta) < 1e-9:
        # Vertical walls: linear relationship between extra volume and rise
        delta_z_m = delta_vol_m3 / denom
    else:
        # Flared walls: delta_vol = L*(B_wl*dz + tan(θ)*dz²)
        # Rearranges to: L*tan(θ)*dz² + L*B_wl*dz - delta_vol = 0
        a = L_m * tan_theta
        b = L_m * B_wl_avg_m
        disc = b * b - 4.0 * a * (-delta_vol_m3)
        delta_z_m = (-b + np.sqrt(max(disc, 0.0))) / (2.0 * a) if disc >= 0 else delta_vol_m3 / denom

    waterline_4p_in   = base_draft_in + delta_z_m / _M_PER_IN
    min_mold_depth_in = waterline_4p_in + min_freeboard_m / _M_PER_IN
    return waterline_4p_in, min_mold_depth_in


# ── Loaded freeboard (ASCE compliance) ────────────────────────────────────────
#
# calculate_4_person_waterline() above is a PARAMETRIC estimate: it assumes
# prismatic walls of average beam above the 2-person waterline.  The functions
# below instead slice and integrate the REAL per-station offsets -- the same
# geometry the section engine reads -- and root-find the waterline where
# displaced volume balances the loaded weight.  That is the number ASCE
# freeboard is measured against, so it is what the compliance check uses.

WATER_LB_FT3 = 62.4          # fresh water


def displaced_volume_ft3(z_wl_in, x_ft, z_in, yhalf_in, n_slice=400):
    """
    Displaced volume [ft³] below waterline ``z_wl_in`` by slice-and-integrate.

    Per station the submerged sectional area is A(x) = ∫₀^{z_wl} 2·y(x,z) dz
    (full breadth = twice the half-breadth); the volume is then ∫ A(x) dx.
    Both integrals are trapezoidal on a uniform sub-grid, so a waterline that
    falls between tabulated offset rows is handled by interpolation rather
    than snapping to the nearest row.

    Parameters
    ----------
    z_wl_in  : waterline height above the keel [in]
    x_ft     : (n_st,) station positions [ft]
    z_in     : (n_z,) offset table heights above keel [in]
    yhalf_in : (n_st, n_z) half-breadths [in]
    """
    z_wl = float(max(z_wl_in, 0.0))
    if z_wl <= 0.0:
        return 0.0
    zs = np.linspace(0.0, z_wl, int(n_slice))
    A_in2 = np.array([trapezoid(2.0 * np.interp(zs, z_in, yhalf_in[i]), zs)
                      for i in range(yhalf_in.shape[0])])
    return float(trapezoid(A_in2 / 144.0, x_ft))       # in²→ft², × ft = ft³


def loaded_freeboard_check(total_weight_lbs, mold_depth_in, x_ft, z_in,
                           yhalf_in, min_freeboard_in,
                           rho_lb_ft3=WATER_LB_FT3):
    """
    ASCE loaded-freeboard compliance check.

    Root-finds the waterline where buoyancy equals ``total_weight_lbs`` (hull
    plus the full racing crew), then reports freeboard = mold depth - waterline
    against ``min_freeboard_in``.

    A hull whose entire moulded volume cannot float the load SWAMPS: that is
    reported explicitly (``swamps=True``, freeboard 0) rather than returned as
    a negative freeboard, because the root does not exist in that case.

    Returns a dict with the waterline, freeboard, the displaced volume and
    buoyancy at that waterline, the reserve volume to the gunwale, and ``ok``.
    """
    def _net(zz):                       # buoyancy − weight
        return displaced_volume_ft3(zz, x_ft, z_in, yhalf_in) * rho_lb_ft3 \
            - float(total_weight_lbs)

    vol_gunwale = displaced_volume_ft3(mold_depth_in, x_ft, z_in, yhalf_in)
    if _net(float(mold_depth_in)) < 0.0:
        return {"swamps": True, "waterline_in": float(mold_depth_in),
                "freeboard_in": 0.0, "ok": False,
                "total_weight_lbs": float(total_weight_lbs),
                "disp_vol_ft3": vol_gunwale,
                "buoyancy_lbs": vol_gunwale * rho_lb_ft3,
                "vol_to_gunwale_ft3": vol_gunwale, "reserve_vol_ft3": 0.0,
                "mold_depth_in": float(mold_depth_in),
                "min_freeboard_in": float(min_freeboard_in),
                "rho_lb_ft3": float(rho_lb_ft3)}

    z_wl = float(brentq(_net, 1e-4, float(mold_depth_in), xtol=1e-6))
    vol = displaced_volume_ft3(z_wl, x_ft, z_in, yhalf_in)
    fb = float(mold_depth_in) - z_wl
    return {"swamps": False, "waterline_in": z_wl, "freeboard_in": fb,
            "ok": fb >= float(min_freeboard_in),
            "total_weight_lbs": float(total_weight_lbs),
            "disp_vol_ft3": vol, "buoyancy_lbs": vol * rho_lb_ft3,
            "vol_to_gunwale_ft3": vol_gunwale,
            "reserve_vol_ft3": vol_gunwale - vol,
            "mold_depth_in": float(mold_depth_in),
            "min_freeboard_in": float(min_freeboard_in),
            "rho_lb_ft3": float(rho_lb_ft3)}


def max_hull_weight_for_freeboard(mold_depth_in, min_freeboard_in, crew_lbs,
                                  x_ft, z_in, yhalf_in,
                                  rho_lb_ft3=WATER_LB_FT3):
    """
    The heaviest HULL that still clears ``min_freeboard_in`` with the crew
    aboard [lb], or -inf if the requirement is unreachable at this mold depth.

    This is loaded_freeboard_check INVERTED, and it is exact rather than an
    approximation of it.  Freeboard is monotone in weight, so

        freeboard >= min                                (what ASCE requires)
        <=> waterline <= mold_depth - min
        <=> V(waterline) <= V(mold_depth - min)          (V monotone in draft)
        <=> total_weight <= rho * V(mold_depth - min)
        <=> hull_weight  <= rho * V(mold_depth - min) - crew

    The right-hand side depends only on the HULL GEOMETRY and the mold depth --
    not on the mix, the thickness or the pour lines -- so it is computed ONCE
    per (depth) task and every configuration is then gated by a single scalar
    comparison.  That is what makes a per-configuration freeboard gate free.

    WHY THIS EXISTS.  The minimum mold depth used to be set once, before the
    sweep, from an ESTIMATED hull mass, and never reconciled against the design
    that actually won.  The winner came out heavier than the estimate, floated
    deeper, and delivered 5.87 in of freeboard against a 6.00 in requirement --
    a rules-compliance failure invisible to every structural check.  A one-shot
    estimate feeding a hard constraint cannot self-correct; a per-configuration
    gate cannot drift.
    """
    z_limit = float(mold_depth_in) - float(min_freeboard_in)
    if z_limit <= 0.0:
        return float("-inf")
    v = displaced_volume_ft3(z_limit, x_ft, z_in, yhalf_in)
    return float(v) * float(rho_lb_ft3) - float(crew_lbs)


def apply_realistic_paddler_loads(x, dx, coords, dynamic_weights, L,
                                  advanced_loading=None):
    """
    Distributes paddler loads along the 1-D beam array.

    advanced_loading=False (default): each paddler's full weight applied as a
        single point load at their longitudinal position — simple and easy to
        verify against hand calculations.
    advanced_loading=True: biomechanical split — ``config.KNEE_FRAC_BENDING``
        of the weight on the knees and the remainder on the toes as a point
        load ``config.TOE_OFFSET_IN`` aft.  BOTH are deposited as bracketed
        point loads; the knee load is NOT spread over the 3-in patch.  This
        docstring claimed a patch for some time after the patch was removed
        below — see that comment for why it was removed and what it costs.

    The knee fraction is BENDING-basis.  It is no longer distinct from the
    punching basis (both 1.00 since the 100/0 adoption), but the ordering
    still matters: bending may never credit more toe relief than punching.
    Both live in ``config``; do not re-hardcode either here.

    NOTE ON CONSERVATISM: neither model bounds the other.  Splitting weight to
    the toes SPREADS the load, which lowers the SAGGING moment and peak shear
    but RAISES the hogging moment (toe loads sit further outboard).  Measured
    on the 109.76 lb winner with seating pinned and only the load model varied:
    M_sag 182.6 -> 167.6, |V| 89.4 -> 86.6, but M_hog 21.0 -> 22.4 and the
    governing bending D/C 0.702 -> 0.764.  So the point-load default is NOT a
    conservative bound -- do not assume a direction; re-measure per design.

    CONTINUITY (why loads are deposited linearly, not snapped to a station):
    every point load is split between the two stations that BRACKET it, in
    inverse proportion to distance.  Snapping each load to argmin|x - s|
    instead makes the whole load vector -- and therefore any objective built
    on it -- a STEP function of seat position: it is byte-identical while a
    paddler moves within +-dx/2 (0.25 ft here) and jumps at station
    boundaries.  That made the SLSQP seating objective exactly flat at its
    eps=0.1 probe step, so the finite-difference gradient was identically
    zero and the solver returned (near) its initial guess.  Linear deposition
    makes the objective piecewise-linear and continuous in seat position, so
    the gradient is real.  It is also simply more accurate: a paddler at
    7.3 ft is no longer modelled as sitting at 7.5 ft.
    """
    if advanced_loading is None:
        advanced_loading = config.ADVANCED_PADDLER_LOADING

    num_points = len(x)
    w_payload = np.zeros(num_points)

    # Trapezoidal node weights: the integral of w_payload uses dx/2 at the two
    # ends and dx in between.  Dividing by the node's OWN weight makes each
    # deposit conserve exactly, at the ends as well as the interior.
    _node_w = np.full(num_points, dx, dtype=float)
    if num_points > 1:
        _node_w[0] = _node_w[-1] = dx / 2.0

    def _deposit(pos, load):
        """Add `load` [lb] at continuous position `pos`, conserving exactly."""
        if load == 0.0:
            return
        p = float(np.clip(pos, x[0], x[-1]))
        j = int(np.clip(np.searchsorted(x, p) - 1, 0, num_points - 2))
        span = x[j + 1] - x[j]
        a = 0.0 if span <= 0 else (p - x[j]) / span      # 0 at x[j], 1 at x[j+1]
        w_payload[j]     += load * (1.0 - a) / _node_w[j]
        w_payload[j + 1] += load * a         / _node_w[j + 1]

    def _deposit_patch(center, load, width_ft):
        """Add `load` [lb] spread UNIFORMLY over `width_ft` centred on `center`.

        The patch is integrated against the grid's linear hat functions, one
        overlapped element at a time, depositing each segment's resultant at
        that segment's own centroid.  Two consequences, both exact and both
        the reason this is safe to ship unconditionally:

          * hats reproduce constants AND linears, so the RESULTANT and the
            FIRST MOMENT are exact on ANY grid, however coarse;
          * when the patch lies inside a single element the segment centroid
            IS the patch centre, so this reduces to _deposit(center, load)
            bit for bit -- the sub-grid case and the point case agree BY
            CONSTRUCTION rather than by a resolution test.

        The earlier removal of the patch cited mis-scaling on coarse grids.
        That is a property of a NON-consistent deposit (splitting by station
        count, or snapping to the nearest node), not of patch loading itself.
        """
        if load == 0.0:
            return
        if width_ft <= 0.0:
            _deposit(center, load)
            return
        a = max(float(center) - 0.5 * width_ft, x[0])
        b = min(float(center) + 0.5 * width_ft, x[-1])
        if b <= a:                      # patch fell entirely off the beam
            _deposit(center, load)
            return
        q = load / (b - a)              # lb/ft
        j0 = int(np.clip(np.searchsorted(x, a) - 1, 0, num_points - 2))
        j1 = int(np.clip(np.searchsorted(x, b) - 1, 0, num_points - 2))
        for j in range(j0, j1 + 1):
            xl, xr = x[j], x[j + 1]
            span = xr - xl
            if span <= 0:
                continue
            lo, hi = max(a, xl), min(b, xr)
            if hi <= lo:
                continue
            seg_load = q * (hi - lo)
            frac = (0.5 * (lo + hi) - xl) / span     # segment centroid in [0,1]
            w_payload[j]     += seg_load * (1.0 - frac) / _node_w[j]
            w_payload[j + 1] += seg_load * frac        / _node_w[j + 1]

    for i, knee_loc in enumerate(coords):
        weight = dynamic_weights[i]

        if not advanced_loading:
            # The knee contact is a 3-in PATCH in this model too.  It must be:
            # PADDLER_LOAD_ENVELOPE designs to the worst of both models, and
            # spreading a load LOWERS the peak, so a bare point load here
            # governs the SAGGING PEAK -- which is what sets D/C on the
            # current design -- and masks the patch in the other branch
            # entirely: measured at exactly zero D/C change with the envelope
            # on, -4.267e-03 with it off.  That would leave the SHIPPED D/C
            # sitting on the point-load singularity, which is the very thing
            # the patch exists to remove.
            #
            # It governs the PEAK, not every station: measured per-station at
            # 161, the split branch wins hogging at 142/161 stations and shear
            # at 32-37, for every split from 0.70 to 0.90.  The envelope is
            # doing real work; it is not the same model twice.  It collapses
            # to one model at knee_frac == 1.00 exactly, where the two
            # branches are identical by construction.
            #
            # The two models differ in the knee/toe SPLIT, which is the real
            # physical distinction between them.  They do not differ on
            # whether a knee is a point, because it is not one.
            _deposit_patch(knee_loc, weight, float(config.KNEE_DIA_IN) / 12.0)
        else:
            knee_frac = float(config.KNEE_FRAC_BENDING)
            w_knee = knee_frac * weight
            w_toe  = (1.0 - knee_frac) * weight
            toe_loc = knee_loc + (float(config.TOE_OFFSET_IN) / 12.0)

            # The knee contact is a REAL 3-in patch, not a point, and it is
            # deposited as one at every resolution.  A point load is a
            # singularity: its shear jumps and its moment kinks, so the
            # governing D/C never converges under refinement -- it just
            # resolves the singularity harder.  Spreading the load over its
            # true footprint gives a bounded, converging demand.
            #
            # This is UNCONDITIONAL on purpose.  Making it grid-aware would
            # leave the production grid on the point load, i.e. leave the
            # shipped D/C divergent and fix convergence only at resolutions
            # nobody races on.  It is safe unconditionally because the patch
            # deposit reduces EXACTLY to the point deposit when the patch is
            # narrower than one element -- see _deposit_patch.
            _deposit_patch(knee_loc, w_knee, float(config.KNEE_DIA_IN) / 12.0)
            if toe_loc <= L:
                _deposit(toe_loc, w_toe)

    return w_payload

# calculate_cross_section_NA() was removed here: a single-section neutral-axis
# helper with ZERO callers anywhere in the repo.  The neutral axis the sweep
# actually uses is computed elsewhere and is NOT dead -- the homogeneous NA
# (c_bot) comes from geometry.calculate_shell_properties, and the composite
# (transformed-section) NA y_bar comes from composite_section_properties; both
# feed the gated bending stress sigma = M*(z - y_bar)/I.  Only this unused
# standalone helper was removed.

# generate_2d_pour_map() was removed here.  It had NO production callers, and
# it ignored its own mid_mix_pcf argument (midbody was hardcoded to
# (heavy, heavy)).  It was a leftover of the LONGITUDINAL pour zoning that was
# deliberately killed -- the design pours a VERTICAL three-zone I-beam with the
# same stack along the whole hull, built by evaluate_multizone_weight.  Do not
# reintroduce a longitudinal zone map without building the pour to match.


# ── Torsion + Lateral Load Checks ──────────────────────────────────────────


def _section_torsion_constant(A_in2: float, thickness_in: float) -> float:
    """
    St. Venant torsion constant J for an open thin-walled cross-section.

    For a thin-walled open section with uniform thickness t the classical
    result is J = (t³/3) × perimeter.  Since A = t × perimeter for a
    thin shell, this simplifies to J = (t²/3) × A, which is exact for
    any constant-thickness open profile.

    Parameters
    ----------
    A_in2        : total shell cross-sectional area [in²]
    thickness_in : uniform wall thickness [in]

    Returns
    -------
    J_in4 : St. Venant torsion constant [in⁴]
    """
    return max((thickness_in ** 2 / 3.0) * A_in2, 1e-9)


def _section_lateral_inertia(
    width_ft: float,
    c_bot_in: float,
    c_top_in: float,
    thickness_in: float,
) -> tuple:
    """
    Second moment of area about the vertical (Z) axis and extreme-fibre
    distance for transverse (lateral) bending analysis.

    Derivation  (hand-calc approximation)
    --------------------------------------
    Assume the hull half-breadth varies linearly from 0 at the keel to
    B_half at the gunwale: y(z) = B_half × (z/D).  Then for both halves:

        I_lat = 2t × ∫₀ᴰ y(z)² × √(1 + (dy/dz)²) dz
              ≈ (2/3) × t × B_half² × D          (shallow-slope limit K ≈ 1)

    This is a first-order approximation.  For hulls with B_half < D/3 the
    error is below 5%, which is acceptable for hand-calc checks.

    Parameters
    ----------
    width_ft     : full beam at the gunwale [ft]  (= 2 × half-breadth)
    c_bot_in     : neutral-axis height above keel [in]
    c_top_in     : distance from NA to top fibre [in]
    thickness_in : wall thickness [in]

    Returns
    -------
    (I_lat_in4, c_lat_in)
        I_lat_in4 : second moment about vertical axis [in⁴]
        c_lat_in  : max half-breadth (extreme lateral fibre) [in]
    """
    B_half_in = width_ft * 6.0            # ft → in half-breadth
    D_in      = c_bot_in + c_top_in       # total section depth [in]
    I_lat     = (2.0 / 3.0) * thickness_in * B_half_in ** 2 * D_in
    return max(I_lat, 1e-9), B_half_in


def torsional_design_torque(
    paddler_weights_lb: list,
    beam_ft: float,
    dynamic_load_factor: float = 1.2,
    paddle_force_fraction: float = 0.25,
    paddle_offset_ft: float = 0.5,
) -> float:
    """
    Worst-case torsional design torque from an asymmetric paddle stroke.

    Physical model
    --------------
    The critical case is a single paddler on one side executing a full
    power stroke while the opposite paddler provides no torque counter.
    The lateral component of the stroke force is:

        F_lat = DLF × max_weight × paddle_force_fraction

    The moment arm runs from the hull centreline to the paddle blade:

        arm = beam_ft / 2 + paddle_offset_ft

    The 0.25 paddle_force_fraction approximates the peak lateral component
    of a J-stroke or sweep stroke for a 75 W paddler at race pace.

    Parameters
    ----------
    paddler_weights_lb   : paddler static weights [lb]
    beam_ft              : maximum hull beam [ft]
    dynamic_load_factor  : impact multiplier on static weight
    paddle_force_fraction: lateral stroke force / paddler weight
    paddle_offset_ft     : blade distance outboard of gunwale [ft]

    Returns
    -------
    T_lb_in : design torque [lb·in]
    """
    F_lat  = dynamic_load_factor * max(paddler_weights_lb) * paddle_force_fraction
    arm_ft = beam_ft / 2.0 + paddle_offset_ft
    return F_lat * arm_ft * 12.0    # lb·ft → lb·in


def lateral_design_moment(
    total_weight_lbs: float,
    avg_speed_mps: float,
    min_turn_radius_m: float,
    hull_depth_in: float,
    g: float = 9.807,
) -> tuple:
    """
    Lateral bending moment at the keel from centripetal loading in a turn.

    Physical model
    --------------
    The centripetal acceleration a_c = V²/r acts on the entire system
    (canoe + paddlers).  The resulting lateral force is:

        F_lat [lb] = W_total [lb] × a_c [m/s²] / g [m/s²]

    This force acts at the combined system CG, estimated at 40% of the
    total hull depth (keel to gunwale).  This is a conservative proxy for
    paddlers seated just above the waterline in a typical kneeling posture.

    The lateral bending moment at the keel:

        M_lat = F_lat × H_CG

    Parameters
    ----------
    total_weight_lbs   : canoe + all paddlers [lb]
    avg_speed_mps      : design turn speed [m/s]
    min_turn_radius_m  : design turn radius [m]
    hull_depth_in      : total mold depth from keel to gunwale [in]
    g                  : gravitational acceleration [m/s²]

    Returns
    -------
    (F_lat_lb, M_lat_lb_in) : lateral force [lb] and moment [lb·in]
    """
    a_c         = avg_speed_mps ** 2 / max(min_turn_radius_m, 0.1)
    F_lat_lb    = total_weight_lbs * a_c / g
    H_CG_in     = 0.40 * hull_depth_in
    M_lat_lb_in = F_lat_lb * H_CG_in
    return F_lat_lb, M_lat_lb_in


def _band_max_halfbreadth(yhalf_grid, z_grid, z1, z2):
    """Per-station max half-breadth [in] over the height band [z1, z2].

    z1/z2 are per-station arrays (or scalars).  Uses the interpolated
    half-breadth at both band edges plus every grid height that falls inside
    the band, so the widest fibre in the band is captured even when the hull is
    widest at the waterline (mid-section) rather than at the sheer.
    """
    zg = np.asarray(z_grid, dtype=float)
    n_st = yhalf_grid.shape[0]
    z1a = np.broadcast_to(np.asarray(z1, dtype=float), (n_st,))
    z2a = np.broadcast_to(np.asarray(z2, dtype=float), (n_st,))
    out = np.maximum(_cum_at(yhalf_grid, zg, z1a), _cum_at(yhalf_grid, zg, z2a))
    for k in range(len(zg)):
        inside = (zg[k] >= z1a) & (zg[k] <= z2a)
        out = np.where(inside, np.maximum(out, yhalf_grid[:, k]), out)
    return out


# Below this section area a station is a DEGENERATE TIP, not structure: the
# hull has tapered to a sliver and every transverse quantity there is a ratio of
# epsilons rather than geometry.  ONE constant, shared by every transverse check
# -- the hoop gate and the Kupfer biaxial gate have now each had to learn this
# separately, and the second time cost a sweep that reported 0 feasible designs.
#
# WHY THE TIPS ARE POISON.  Transverse stress is sigma_y ~ M_lat * y / I_lat,
# and at a tip the section becomes a thin VERTICAL sliver: the half-breadth
# b -> 0 so I_lat ~ b^2*A collapses, while the depth (and hence the longitudinal
# I_tr ~ h^2*A) does not.  sigma_y therefore diverges as 1/b^2 while sigma_x
# stays finite.  Two things normally hide it -- M(x) -> 0 at a free end, and
# yhalf -> 0 -- but neither is guaranteed: a stem rocker puts cantilever mass
# BEYOND the last waterplane station so M(tip) != 0, and the geometry carries a
# 1e-6 half-breadth floor which, divided by the 1e-9 inertia floor, manufactures
# a 1000x stress out of an EMPTY section.  Excluding the station is the fix;
# clamping the denominator harder is not (it just moves the epsilon around).
DEGENERATE_SECTION_A_IN2 = 0.5

# ...and an AREA test alone is not enough, because it is grid-dependent: refine
# the station grid and one will always land where the area has just cleared the
# threshold while the half-breadth is still nearly zero, so 1/b^2 bites again
# and the utilisation grows with resolution.  The divergence is driven by b, so
# the second test is on b, RELATIVE to the hull's own maximum half-breadth --
# scale-free and independent of how many stations the grid happens to place in
# the taper.  Below this fraction the "section" is a stem, not a hull girder,
# and transverse hull-girder bending simply does not describe it.
DEGENERATE_SECTION_BEAM_FRAC = 0.05


def structural_station_mask(cum_area, z_grid, sheer, yhalf_grid):
    """
    Which stations are REAL STRUCTURE for the transverse checks.

    Shared by the standalone hoop gate and the Kupfer biaxial gate so the two
    can never disagree about which stations exist -- they have each had to
    learn the tip-divergence lesson separately, and the second time cost a
    sweep that reported 0 feasible designs out of 10,350.

    A station qualifies when it has real section area AND a half-breadth that
    is a meaningful fraction of the hull's widest.  See the constants above for
    why both tests are needed.
    """
    A_full = _cum_at(np.asarray(cum_area, float), np.asarray(z_grid, float),
                     np.asarray(sheer, float))
    b_max = np.max(np.asarray(yhalf_grid, float), axis=1)
    b_ref = float(np.max(b_max)) if b_max.size else 0.0
    return (A_full > DEGENERATE_SECTION_A_IN2) & \
           (b_max >= DEGENERATE_SECTION_BEAM_FRAC * b_ref)


def transverse_hoop_utilization(
        cum_Iy, yhalf_grid, z_grid, z_bounds, zone_E, zone_str_t, t_in,
        A_full, M_vert_env_lbft,
        rail_lat_Itr=None,
        avg_speed_mps=2.5, min_turn_radius_m=4.0, g=9.807):
    """
    Per-station transverse (hoop) utilisation for a vertically-zoned section
    under the centripetal turn moment, on a TRANSFORMED (modular-ratio) lateral
    section about the vertical centerplane.

    Each pour zone's OUTBOARD lateral fibre carries
        σ_i = n_i · M_lat(x) · y_i / I_lat_tr        (n_i = E_i / E_ref)
    and is checked against THAT zone's own tensile allowable f_t,i.  The
    governing zone — often the soft light web, which sits at the hull's widest
    point yet has the lowest f_t — sets the utilisation.  This corrects the
    earlier gate, which divided the single extreme-fibre stress by the
    top-zone strength (the wrong allowable for the governing fibre).

    ``rail_lat_Itr`` (per-station) is the gunwale rail's TRANSFORMED lateral
    inertia (n_rail·2·A_rail·y_rail², already in E_ref units), folded into
    I_lat_tr the same way the shell zones are — so a rail lowers EVERY fibre's
    stress by raising the shared denominator, including the governing web
    fibre.  It is not added to the homogeneous reference (which is the naive
    shell-only baseline for comparison).

    TURN MOMENT IS A DISTRIBUTION M_lat(x), NOT A SCALAR.
    The lateral (centripetal) load per unit length is the local mass per unit
    length times the turn acceleration k_lat = a_c/g = V²/(g·R).  Because it
    follows the SAME mass distribution as gravity and is reacted by the SAME
    waterplane (see _solve_beam_case), the balanced lateral bending moment is
    the vertical bending envelope scaled EXACTLY by k_lat:

        M_lat(x) = k_lat · |M_vert_env(x)|      [lb·in here; ×12 from lb·ft]

    so the per-station vertical moment envelope ``M_vert_env_lbft`` (lb·ft) is
    passed in and scaled here.  This is what makes the gate resolution-
    independent: M_lat(x) → 0 at the free ends because M_vert_env → 0 there.
    The earlier code applied a single scalar F_lat·H_CG identically at EVERY
    station; divided by the near-zero lateral section modulus at the bow/stern
    taper it diverged as 1/b², so the whole gate depended on how many stations
    the grid happened to place near the degenerate tips.  The A_full > 0.5 in²
    mask below is only a secondary filter on degenerate sections; the physical
    M_lat(x) → 0 at the ends is what removes the divergence, not the mask.

    Returns dict: util, gov_zone, util_zone (n_zones×n_st), dc_homog, M_lat_peak.
    """
    n_zones = len(zone_str_t)
    n_st = cum_Iy.shape[0]
    # Lateral turn-moment DISTRIBUTION [lb·in]: k_lat · |vertical envelope|.
    k_lat = avg_speed_mps ** 2 / (g * max(min_turn_radius_m, 0.1))
    M_lat = k_lat * np.abs(np.asarray(M_vert_env_lbft, dtype=float)) * 12.0
    E = np.asarray(zone_E, dtype=float)
    n = E / E[0]
    dIy = [(_cum_at(cum_Iy, z_grid, z_bounds[k + 1])
            - _cum_at(cum_Iy, z_grid, z_bounds[k])) for k in range(n_zones)]
    I_tr  = sum(n[k] * dIy[k] for k in range(n_zones))
    if rail_lat_Itr is not None:
        I_tr = I_tr + np.asarray(rail_lat_Itr, dtype=float)
    I_tr  = np.maximum(I_tr, 1e-9)
    I_hom = np.maximum(sum(dIy), 1e-9)
    # Degenerate end stations are not hull girder -- shared criterion, so this
    # gate and the Kupfer gate always agree on which stations exist.  A_full is
    # passed in already computed; the mask helper recomputes nothing else.
    struct = np.asarray(A_full) > DEGENERATE_SECTION_A_IN2
    _bmax = np.max(np.asarray(yhalf_grid, float), axis=1)
    _bref = float(np.max(_bmax)) if _bmax.size else 0.0
    struct = struct & (_bmax >= DEGENERATE_SECTION_BEAM_FRAC * _bref)
    util = np.zeros(n_st)
    gov  = np.zeros(n_st, dtype=int)
    util_zone = np.zeros((n_zones, n_st))
    dc_homog  = np.zeros((n_zones, n_st))
    for k in range(n_zones):
        y_k = _band_max_halfbreadth(yhalf_grid, z_grid, z_bounds[k], z_bounds[k + 1])
        allow = max(float(zone_str_t[k]), 1e-9)
        util_zone[k] = np.where(struct, n[k] * M_lat * y_k / I_tr / allow, 0.0)
        dc_homog[k]  = np.where(struct, M_lat * y_k / I_hom / allow, 0.0)
        upd = util_zone[k] > util
        gov = np.where(upd, k, gov)
        util = np.maximum(util, util_zone[k])
    return {"util": util, "gov_zone": gov, "util_zone": util_zone,
            "dc_homog": dc_homog, "M_lat_peak": float(np.max(M_lat))}


# ── KUPFER-GERSTLE BIAXIAL CONCRETE FAILURE ENVELOPE ────────────────────────
#
# Replaces the Rankine (maximum-principal-stress) tensile criterion.  Rankine
# ignores that concrete's tensile capacity DEGRADES under transverse
# compression; this hull governs in the tension-compression quadrant
# (longitudinal bending tension with transverse hoop compression at the same
# fibre), where Rankine is unconservative.  Kupfer-Gerstle is the standard
# experimental biaxial envelope for plain concrete and the accuracy ceiling for
# a closed-form criterion usable inside a parametric sweep.  (Concrete Damaged
# Plasticity and Willam-Warnke are more accurate but are nonlinear-FEA
# constitutive models -- the wrong tool here; see the report.)
#
# Sign convention throughout: TENSION POSITIVE, sigma_1 >= sigma_2.
# ``ft`` and ``fc`` are the uniaxial tensile and compressive strengths, both
# passed as POSITIVE magnitudes.

_KUPFER_K_TC = 0.8   # tension-compression interaction slope (see below)


def kupfer_acceptance_limit():
    """
    The utilisation a design must stay UNDER to be accepted [-], default 0.95.

    WHY THIS IS NOT 1.0.  The sweep's weight converges quickly with station
    count -- 0.58% between 161 and 321 -- but the GATING Kupfer utilisation does
    not, and it is the utilisation that decides feasibility.  Measured on the
    live hull with the real stem rocker, across five designs:

        stations      41       81      161      321      641
        drift/doubling      4.88%    1.52%    1.06%    1.00%   (mean)
                           11.39%    4.13%    2.19%    2.88%   (worst design)

    The increments decay and then settle near ~1% per doubling; no plateau is
    observed out to 641 stations, which is as far as it is practical to go.  A
    design accepted AT 1.0 on the production 161-station grid is therefore not
    a design at all -- the 110.39 lb winner that motivated this sat at 0.9957 at
    161 and failed at 1.0040 at 321.

    Refining the search grid does not fix this, it relocates it: a winner at
    0.99 on 321 stations fails at 641 by the same argument, at several hours per
    run.  Accepting with headroom does fix it, because the headroom is spent on
    refinement the search never performs.

    HOW MUCH HEADROOM 0.95 BUYS.  Taking the WORST per-doubling drift observed
    at the production refinement step (2.19% over 161->321):

        log(1/0.95) / log(1.0219)  =  2.37 doublings

    i.e. a design accepted at 0.95 on 161 stations stays under 1.0 out to about
    830 stations.  At the mean drift (1.06%) it is 4.9 doublings, ~4,700
    stations.  Both are far beyond any grid this suite will run, so the verdict
    is refinement-stable in the only sense that can be checked.

    This is a margin against a NUMERICAL non-convergence, not against physics --
    the factor of safety already covers the physics, and is applied separately
    inside the allowables.  Like the n=2 regression caveat and the advisory
    torsion bound, it exists because a quantity the design depends on is not
    fully converged, and the report says so.

    Set to 1.0 to disable (accept anything the gate passes at the search
    resolution) -- the sweep then reports designs it cannot stand behind.
    """
    return float(getattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95))


def kupfer_drift_per_doubling():
    """
    Measured fractional growth of the governing Kupfer utilisation per doubling
    of the longitudinal station count.  File-backed; see canoe_suite.cfg for the
    study behind the number.
    """
    return max(float(getattr(config, "KUPFER_DRIFT_PER_DOUBLING", 0.0219)), 0.0)


KUPFER_TAIL_RATIO_FLOOR = 0.50
KUPFER_PROMOTE_BAND = 0.002


def extrapolate_kupfer_tail(u_by_n, ratio_floor=KUPFER_TAIL_RATIO_FLOOR):
    """
    Extrapolate ONE design's converged Kupfer D/C from ITS OWN increments.

    WHY PER-DESIGN.  The alternative is a global pull-back constant
    (kupfer_drift_per_doubling), which is a tail drift measured on one design
    and applied to every other.  Drift demonstrably does NOT transfer between
    designs on this hull -- 3.16% against 1.43% on the same geometry -- so a
    better-sourced constant is still a constant.  Each design pays for its own
    increments during refinement; this reads the answer out of those.

    WHY THE RATIO FLOOR.  With u at (n, 2n, 4n) the increments are
    d1 = u2-u1 and d2 = u3-u2, and a geometric tail sums to d2*r/(1-r) with
    r = d2/d1.  Measured on the pinned winner:

        161->321 +1.013e-02, ->641 +1.132e-03, ->1281 +5.427e-04, ->2561 +2.120e-04
        ratios    d2/d1 = 0.1117     d3/d2 = 0.4796     d4/d3 = 0.3907

    The FIRST ratio is anomalously small -- 161 is coarse, so the first
    refinement makes an outsized correction -- and the tail then settles near
    0.4-0.5.  Trusting r = 0.1117 extrapolates to 0.924880 against a true
    limit of ~0.925628: LOW by 7.5e-04, i.e. non-conservative by the very
    margin this machinery exists to control.  Flooring r at 0.50 gives
    0.925869, conservative by 2.4e-04.  The floor never invents drift where
    a design shows none (d2 <= 0 short-circuits); it only refuses to believe
    an implausibly fast first decay.

    THE FLOOR IS ITSELF CALIBRATED ON ONE DESIGN, so it inherits exactly the
    transfer risk that made a global drift constant unacceptable -- it is a
    weaker version of the same bet, bounded to the tail rather than the whole
    value.  It is defensible only while no design shows a genuine first ratio
    above it.  A design whose MEASURED r exceeds 0.50 is not a failure (the
    floor is a floor, and the larger measured ratio is used); it is EVIDENCE
    that 0.50 was fitted to one hull, and the floor should be re-derived from
    the designs then in hand.

    Returns a dict carrying every input and intermediate, so an acceptance
    can be audited later instead of resting on a number nobody can re-derive.
    """
    ns = sorted(int(n) for n in u_by_n)
    if len(ns) < 3:
        return {"ok": False, "reason": "need at least three resolutions",
                "n": ns, "u": [float(u_by_n[n]) for n in ns]}
    u1, u2, u3 = (float(u_by_n[ns[-3]]), float(u_by_n[ns[-2]]),
                  float(u_by_n[ns[-1]]))
    d1, d2 = u2 - u1, u3 - u2
    out = {"n": ns[-3:], "u": [u1, u2, u3], "d1": d1, "d2": d2,
           "ratio_floor": float(ratio_floor)}
    if d2 <= 0.0:
        # already flat or falling: the finest value is the estimate
        out.update({"ok": True, "ratio_measured": (d2 / d1 if d1 > 0 else 0.0),
                    "ratio_used": 0.0, "tail": 0.0, "u_inf": u3,
                    "note": "increments non-increasing; no tail added"})
        return out
    if d1 <= 0.0:
        # NON-MONOTONE: the value FELL from n to 2n and then rose at 4n.  There
        # is no meaningful ratio to fit -- d2/d1 is negative or infinite -- so
        # a geometric tail cannot be estimated, and the old code took that as
        # "not decaying" and REJECTED.
        #
        # That conflated two different sequences.  A design whose D/C climbs
        # past the limit under refinement is genuinely divergent and must be
        # refused.  A design that dips and then wobbles in the fourth decimal
        # is converging, just not monotonically -- and refusing it meant the
        # gate rejected EVERY candidate on a hull sitting at D/C 0.28 against a
        # 0.95 limit, printing "do not build to it" ten times a run.  A warning
        # that always fires is a warning nobody reads, which is worse than no
        # warning at all.
        #
        # So bound it instead of fitting it: the largest value seen, plus the
        # largest single positive step.  That is conservative (it assumes the
        # sequence takes one more step as big as its biggest so far and then
        # stops), it is honest about being a BOUND rather than a limit, and it
        # still refuses anything whose wobble is large enough to matter.
        bound_step = max(d1, d2, 0.0)
        u_bound = max(u1, u2, u3) + bound_step
        out.update({"ok": True, "ratio_measured": None, "ratio_used": None,
                    "non_monotone": True,
                    "tail": u_bound - u3, "u_inf": u_bound,
                    "note": "NON-MONOTONE (fell then rose): no geometric tail "
                            "is fittable, so this is a BOUND -- max(observed) "
                            "+ largest positive step (%.3e)" % bound_step})
        return out

    r_meas = d2 / d1
    r = max(r_meas, float(ratio_floor))
    if not (r < 1.0):
        # increments are NOT decaying -- this design has no converged value to
        # accept against.  Never silently pass it.  Reached only when d1 > 0,
        # i.e. the value was ALREADY climbing and kept climbing at least as
        # fast: genuine divergence, not a wobble.
        out.update({"ok": False, "ratio_measured": r_meas, "ratio_used": r,
                    "tail": None, "u_inf": None,
                    "reason": "increments are not decaying; refine further"})
        return out
    tail = d2 * r / (1.0 - r)
    out.update({"ok": True, "ratio_measured": r_meas, "ratio_used": r,
                "tail": tail, "u_inf": u3 + tail})
    return out


def kupfer_search_limit(doublings=1):
    """
    The limit the SEARCH gates on -- the acceptance limit pulled back by the
    measured drift, so a winner found at the search resolution still clears
    ACCEPTANCE after ``doublings`` refinements.

    WHY THIS EXISTS.  The acceptance limit is a threshold, not a target: the
    optimiser will sit just underneath it and bank the remaining weight.  At the
    search resolution that is a design which passes only because the grid is too
    coarse to see its real utilisation.  It happened three times on this hull --
    0.9487, 0.9466 and 0.9380 winners, each of which then exceeded 0.95 once the
    grid was refined -- and each time it was caught by hand afterwards.

    Gating the search at

        acceptance / (1 + drift)^doublings

    makes the winner clear acceptance at the verification resolution BY
    CONSTRUCTION rather than by luck.  With acceptance 0.95 and the worst
    measured drift 2.19 %/doubling this is 0.930, which would have rejected all
    three of those designs during the search instead of after it.

    The drift is the WORST observed rather than the mean (1.06 %) on purpose:
    the cost of over-tightening is a slightly heavier design, while the cost of
    under-tightening is an hour-long sweep that fails its own verification gate
    at the end.  Relax it toward the mean if lighter designs matter more than
    first-run success.
    """
    return kupfer_acceptance_limit() / (1.0 + kupfer_drift_per_doubling()) ** max(
        int(doublings), 0)

# Opt-in per-config sweep diagnostics.  When set to a list, every
# evaluate_multizone_weight call appends a record of which gate rejected it and
# the gate utilisations -- so a failing sweep can be broken down into a per-gate
# rejection histogram without changing any gate.  None (default) = zero overhead.
#
# This is the DEEP debug hook (one dict per configuration, O(n) memory).  For the
# always-available failure report the sweep uses SweepTally below, which keeps
# the same information in O(1) memory.
_SWEEP_DIAG = None


# ── SWEEP DIAGNOSTICS: per-gate rejection tally ─────────────────────────────
#
# WHY THIS EXISTS.  A sweep that finds nothing used to print one generic guess
# ("the flare angle exceeded limits, or the concrete was too weak"), which hides
# the only thing that matters: WHICH gate rejected everything, and by HOW MUCH.
# "best D/C 1.03" (one more thickness step and it flies) and "best D/C 6.45" (no
# mix in the range can carry the load) are opposite diagnoses behind the same
# message.  A SweepTally records both for the price of a few float compares per
# configuration, so the answer is in the failure itself rather than a debugging
# session.
#
# It is an OPT-IN collector: the evaluators take ``tally=None`` and touch it only
# when a sweep passes one in, so the production fast path is byte-for-byte the
# code that ran before.  Memory is O(1) -- counts plus a single snapshot -- so it
# can stay on for the whole sweep and still describe a run that costed hundreds
# of thousands of configurations.

# Gates that are NOT part of sweep feasibility, listed in the failure report so
# a reader never wonders whether one of them silently rejected the space.
_NON_SWEEP_GATES = {
    "punching":    "report-only — checked for the winning design, never gates the sweep",
    "compression": "report-only — advisory utilisation, never gates the sweep",
    "buckling":    "report-only — checked for the winning design, never gates the sweep",
}

# Rejection-reason -> the D/C key that reason is measured on, so the histogram
# can print how close that gate ever came to letting something through.
_GATE_DC_KEY = {
    "kupfer_tensile":  "Kupfer biaxial",
    "freeboard_flare": "freeboard wall",
    "longitudinal":    "longitudinal (Rankine)",
}


class SweepTally:
    """
    O(1)-memory record of why a sweep rejected what it rejected.

    Counts terminal outcomes per gate, tracks the best (lowest governing D/C)
    utilisation each gate ever achieved, and keeps ONE full snapshot: the
    closest-to-feasible configuration in the whole space.

    Thread-safe: the sweep runs its workers in a ThreadPoolExecutor, so every
    mutation takes a lock.  One uncontended lock per configuration is noise
    beside the beam solve that produced the numbers.
    """

    __slots__ = ("_lock", "search_space", "notes", "rejects", "skipped",
                 "n_costed", "n_feasible", "n_passed_stress",
                 "best_dc_by_gate", "best", "best_partial")

    def __init__(self, search_space=None):
        self._lock = threading.Lock()
        # Free-form {label: text} description of the ranges actually swept and
        # any pre-sweep gate results; printed verbatim above the histogram.
        self.search_space = dict(search_space or {})
        self.notes = []
        self.rejects = {}          # gate -> configurations it rejected
        self.skipped = {}          # reason -> combos skipped BEFORE costing
        self.n_costed = 0          # configurations actually evaluated
        self.n_feasible = 0        # survived every gate applied to them
        self.n_passed_stress = 0   # single-zone: cleared stage 1, awaiting the
                                   # bounded freeboard walk (stage 2)
        self.best_dc_by_gate = {}  # gate -> lowest D/C any configuration reached
        self.best = None           # closest-to-feasible config put through EVERY gate
        self.best_partial = None   # ... and the best of those an early gate cut short

    # -- crossing a process boundary -----------------------------------------
    # Under the PROCESS executor the tally cannot be a per-process global: each
    # child would count into its own copy and the failure report would describe
    # only the parent's pruning, silently losing every gate rejection the
    # workers made.  The sweep therefore hosts ONE tally in a manager process
    # and hands workers a proxy; these three methods are what makes that legal.
    def __getstate__(self):
        with self._lock:
            return {k: getattr(self, k) for k in self.__slots__ if k != "_lock"}

    def __setstate__(self, st):
        self._lock = threading.Lock()
        for k, v in st.items():
            setattr(self, k, v)

    def snapshot(self):
        """
        A detached, lock-free copy -- brings a manager-hosted tally home.

        DEEP copy, not a state handoff: `rejects`, `skipped`, `notes` and the
        two best-config dicts are mutable, and a shallow copy would keep
        counting later mutations into the "snapshot".  The pickle path happens
        to deep-copy for free, so this only shows up when snapshot() is called
        in-process -- which is exactly where it would be hardest to notice.
        """
        import copy
        new = SweepTally.__new__(SweepTally)
        new.__setstate__(copy.deepcopy(self.__getstate__()))
        return new

    # -- recording -----------------------------------------------------------
    def record(self, reject, dcs, info=None, *, t_in=None, depth_in=None,
               mixes=None, fracs=None, weight=None, gov=None,
               stage2_pending=False, partial=False):
        """
        Log one costed configuration.

        reject  : gate name that rejected it, or None if it survived.
        dcs     : {gate: demand/capacity} for the gates that DECIDE feasibility;
                  None values allowed (gate not applied).  The governing D/C is
                  the largest of these.
        info    : {label: D/C} for quantities reported alongside but which never
                  gate (the standalone longitudinal and hoop checks the Kupfer
                  envelope subsumes).  Shown, never allowed to "govern".
        partial : an early gate rejected it before the later gates ran, so its
                  D/Cs are incomplete.  Such a record cannot be compared like
                  for like against a fully-evaluated one, so it competes for a
                  separate "best" and is only reported when nothing in the whole
                  space made it through every gate.
        The remaining keyword arguments describe the configuration and are
        materialised into a snapshot ONLY when it is the new closest-to-feasible.
        """
        gov_dc = max((v for v in dcs.values() if v is not None), default=None)
        with self._lock:
            self.n_costed += 1
            if reject is None:
                if stage2_pending:
                    self.n_passed_stress += 1
                else:
                    self.n_feasible += 1
            else:
                self.rejects[reject] = self.rejects.get(reject, 0) + 1
            for g, v in dcs.items():
                if v is None:
                    continue
                cur = self.best_dc_by_gate.get(g)
                if cur is None or v < cur:
                    self.best_dc_by_gate[g] = float(v)
            _cur = self.best_partial if partial else self.best
            if gov_dc is not None and (_cur is None or gov_dc < _cur["gov_dc"]):
                _snap = {
                    "gov_dc": float(gov_dc),
                    "gov_gate": max((k for k, v in dcs.items() if v is not None),
                                    key=lambda k: dcs[k]),
                    "dcs": {k: (None if v is None else float(v))
                            for k, v in dcs.items()},
                    "info": {k: (None if v is None else float(v))
                             for k, v in (info or {}).items()},
                    "reject": reject,
                    "t_in": None if t_in is None else float(t_in),
                    "depth_in": None if depth_in is None else float(depth_in),
                    "mixes": None if mixes is None else [float(m) for m in mixes],
                    "fracs": None if fracs is None else [float(f) for f in fracs],
                    "weight": None if weight is None else float(weight),
                    "gov": gov,
                    "partial": bool(partial),
                }
                if partial:
                    self.best_partial = _snap
                else:
                    self.best = _snap

    def record_stage2(self, reject, dcs, info=None, **kw):
        """
        Resolve a configuration that cleared stage 1 (stress) and has now been
        through the single-zone path's bounded freeboard walk.  Moves it out of
        ``n_passed_stress`` into feasible or the named gate.
        """
        with self._lock:
            if self.n_passed_stress > 0:
                self.n_passed_stress -= 1
            self.n_costed -= 1          # re-recorded below, not a new config
        self.record(reject, dcs, info, **kw)

    def skip(self, reason, n=1):
        """Log combos rejected by enumeration, before any evaluation cost."""
        with self._lock:
            self.skipped[reason] = self.skipped.get(reason, 0) + int(n)

    def note(self, text):
        """Attach a line of context (pre-sweep gate results, warnings, ...)."""
        with self._lock:
            self.notes.append(str(text))

    # -- reporting -----------------------------------------------------------
    def failure_report(self, total_configs=None, width=74):
        """
        The whole story of a zero-feasible sweep as printable lines: the ranges
        searched, the per-gate rejection histogram, the gates that do NOT gate,
        and the closest-to-feasible configuration with its D/C on every gate.
        """
        bar = "─" * width
        n = self.n_costed
        L = [""]
        L.append(f"❌ SWEEP FAILED: 0 of "
                 f"{(total_configs or n):,} configurations were feasible.")

        if self.search_space:
            L += ["", "  SEARCH SPACE — what was actually swept", f"  {bar}"]
            pad = max((len(k) for k in self.search_space), default=0)
            for k, v in self.search_space.items():
                L.append(f"    {k.ljust(pad)} : {v}")
        for t in self.notes:
            L.append(f"    {t}")

        L += ["", f"  REJECTIONS BY GATE — {n:,} configurations costed", f"  {bar}"]
        if not n:
            L.append("    (nothing was costed — every combination was skipped "
                     "during enumeration)")
        for gate, cnt in sorted(self.rejects.items(), key=lambda kv: -kv[1]):
            best = self.best_dc_by_gate.get(_GATE_DC_KEY.get(gate, gate))
            L.append(f"    {gate.ljust(20)} {cnt:>9,}  {100.0*cnt/max(n,1):5.1f}%"
                     + (f"   closest this gate came: D/C {best:.3f}"
                        if best is not None else ""))
        if self.n_passed_stress:
            L.append(f"    {'(passed stress)'.ljust(20)} {self.n_passed_stress:>9,}"
                     f"  {100.0*self.n_passed_stress/max(n,1):5.1f}%"
                     "   cleared the stress gates but was never reached by the "
                     "bounded freeboard walk")
        L.append(f"    {'FEASIBLE'.ljust(20)} {self.n_feasible:>9,}"
                 f"  {100.0*self.n_feasible/max(n,1):5.1f}%")
        if self.skipped:
            L.append("")
            for r, c in sorted(self.skipped.items(), key=lambda kv: -kv[1]):
                L.append(f"    skipped before costing — {r}: {c:,}")
        _unc = (total_configs or 0) - n - sum(self.skipped.values())
        if _unc > 0:
            L.append(f"    never costed — bound-pruned by the lightest-first "
                     f"walk: {_unc:,}")

        L += ["", "  NOT SWEEP GATES — these cannot have caused this", f"  {bar}"]
        for g, why in _NON_SWEEP_GATES.items():
            L.append(f"    {g.ljust(20)} {why}")

        b = self.best or self.best_partial
        L += ["", ("  MOST-MARGIN CONFIGURATION — lowest governing D/C"
                   if self.n_feasible else
                   "  CLOSEST-TO-FEASIBLE CONFIGURATION — lowest governing D/C"),
              f"  {bar}"]
        if b is None:
            L.append("    (none — no configuration was costed far enough to "
                     "report a utilisation)")
        else:
            if b.get("partial"):
                L.append("    NOTE: an early gate rejected every configuration in "
                         "the space, so the")
                L.append("    later gates never ran — the D/Cs below are the ones "
                         "reached before it.")
                L.append("")
            cfg = []
            if b["t_in"] is not None:
                cfg.append(f"wall thickness {b['t_in']:.3f} in")
            if b["depth_in"] is not None:
                cfg.append(f"mold depth {b['depth_in']:.2f} in")
            if b["mixes"] is not None:
                cfg.append("mixes (bottom→top) ["
                           + ", ".join(f"{m:.1f}" for m in b["mixes"]) + "] pcf")
            if b["fracs"]:
                cfg.append("pour fractions ["
                           + ", ".join(f"{f:.3f}" for f in b["fracs"]) + "]")
            if b["weight"] is not None:
                cfg.append(f"shell weight {b['weight']:.1f} lb")
            for c in cfg:
                L.append(f"    {c}")
            L.append("")
            _info = b.get("info") or {}
            pad = max((len(k) for k in list(b["dcs"]) + list(_info)), default=0)
            L.append("      GATES (these decide feasibility)")
            for g, v in sorted(b["dcs"].items(),
                               key=lambda kv: (kv[1] is None, -(kv[1] or 0.0))):
                if v is None:
                    L.append(f"        {g.ljust(pad)}  D/C =     n/a"
                             "   (not applied to this configuration)")
                else:
                    L.append(f"        {g.ljust(pad)}  D/C = {v:7.3f}"
                             + ("   ← GOVERNS" if g == b["gov_gate"] else ""))
            if _info:
                L.append("      COMPONENTS (reported, never gate — the Kupfer "
                         "envelope subsumes them)")
                for g, v in sorted(_info.items(),
                                   key=lambda kv: (kv[1] is None, -(kv[1] or 0.0))):
                    L.append(f"        {g.ljust(pad)}  D/C = "
                             + ("    n/a" if v is None else f"{v:7.3f}"))
            if b["gov"]:
                L.append(f"      governs at {b['gov']}")
            if not self.n_feasible:
                L += ["", f"    Governing D/C = {b['gov_dc']:.3f} against the "
                      "1.0 limit — this is"]
                L += ["    " + ln for ln in self._verdict(b["gov_dc"])]
        L.append("")
        return "\n".join(L)

    @staticmethod
    def _verdict(gov_dc):
        """The one sentence that turns a number into a next action."""
        if gov_dc <= 1.15:
            return ["a MARGINAL miss.  One more step of thickness, depth or mix",
                    "density — or a slightly lower factor of safety — should "
                    "close it."]
        if gov_dc <= 2.0:
            return ["a REAL shortfall, not a rounding error.  The search space "
                    "has to",
                    "MOVE, not widen slightly: check the mix ceiling and the "
                    "thickness range."]
        return ["a GROSS capacity shortfall.  Nothing in this space is close, and "
                "no",
                "thickness or pour-line tweak closes a gap this size.  The mix "
                "range,",
                "the load cases or the factor of safety is the problem — not the "
                "geometry."]


def principal_stresses(sx, sy, txy):
    """
    In-plane principal stresses from a plane-stress state (tension positive).

        sigma_{1,2} = (sx+sy)/2 +/- sqrt( ((sx-sy)/2)^2 + txy^2 )

    Returns ``(sigma_1, sigma_2)`` with ``sigma_1 >= sigma_2`` (array-safe).
    """
    sx = np.asarray(sx, dtype=float); sy = np.asarray(sy, dtype=float)
    txy = np.asarray(txy, dtype=float)
    avg = 0.5 * (sx + sy)
    rad = np.sqrt((0.5 * (sx - sy)) ** 2 + txy ** 2)
    return avg + rad, avg - rad


def kupfer_utilization(s1, s2, ft, fc, k_tc=_KUPFER_K_TC):
    """
    Kupfer-Gerstle biaxial *tension-side* failure utilisation (=1 on the
    envelope; ``util * FoS <= 1`` is the design gate).  Array-safe.

    Quadrants (sigma_1 >= sigma_2, tension positive):

    * TENSION-TENSION (sigma_2 >= 0): the biaxial tensile strength is
      ~constant at f_t (Kupfer 1969, Fig. 8), so the maximum principal stress
      governs and Rankine is recovered exactly:  util = sigma_1 / f_t.

    * TENSION-COMPRESSION (sigma_1 > 0 > sigma_2) -- THE GOVERNING QUADRANT
      HERE.  The tensile strength falls ~linearly as transverse compression
      grows.  CEB-FIP Model Code 1990 (eq. 3.1.9), fitting Kupfer-Gerstle
      (1969), gives  sigma_1/f_t = 1 - 0.8 * |sigma_2|/f_c , i.e. the linear
      interaction

            util = sigma_1/f_t + 0.8 * |sigma_2|/f_c .

      The 0.8 slope is Kupfer's experimental value; at |sigma_2| ~ 0.6 f_c the
      tensile capacity is roughly halved -- exactly the effect Rankine misses.

    * COMPRESSION-COMPRESSION (sigma_1 <= 0): no tension, so the tension gate
      is inactive here (util = 0).  Biaxial compression actually STRENGTHENS
      concrete (~1.16 f_c near a 0.5 ratio, Kupfer's elliptical CC envelope);
      crushing is checked separately against the conservative UNIAXIAL f_c, so
      ignoring the CC gain here is conservative -- see ``kupfer_cc_strength``.

    Returns ``(util, quadrant)`` where quadrant is 0=CC, 1=TT, 2=TC (ints).
    """
    s1 = np.asarray(s1, dtype=float); s2 = np.asarray(s2, dtype=float)
    ft = np.maximum(np.asarray(ft, dtype=float), 1e-9)
    fc = np.maximum(np.asarray(fc, dtype=float), 1e-9)
    s1, s2, ft, fc = np.broadcast_arrays(s1, s2, ft, fc)
    util = np.zeros(s1.shape, dtype=float)
    quad = np.zeros(s1.shape, dtype=int)          # default CC (0), util 0
    tt = (s1 > 0.0) & (s2 >= 0.0)
    util = np.where(tt, s1 / ft, util)
    quad = np.where(tt, 1, quad)
    tc = (s1 > 0.0) & (s2 < 0.0)
    util = np.where(tc, s1 / ft + k_tc * np.abs(s2) / fc, util)
    quad = np.where(tc, 2, quad)
    return util, quad


def kupfer_cc_strength(s1, s2, fc):
    """
    Kupfer biaxial COMPRESSIVE strength (magnitude) for a compression-
    compression state, for REPORTING the strengthening the separate uniaxial
    crushing check conservatively ignores.

    With both principal stresses compressive and ``alpha = sigma_minor /
    sigma_major`` in [0, 1] (ratio of the smaller to the larger compression),
    Kupfer-Gerstle (1969) fit:

        f_2c / f_c = (1 + 3.65 alpha) / (1 + alpha)^2

    which peaks at ~1.16 f_c near alpha = 0.5.  Returns the biaxial compressive
    strength magnitude for the given state (= f_c for uniaxial/tension states).
    """
    s1 = np.asarray(s1, dtype=float); s2 = np.asarray(s2, dtype=float)
    fc = np.asarray(fc, dtype=float)
    major = np.minimum(s1, s2)                    # most compressive (<=0)
    minor = np.maximum(s1, s2)
    cc = (major < 0.0) & (minor < 0.0)
    alpha = np.where(cc, np.clip(minor / np.where(major < 0, major, -1.0), 0.0, 1.0), 0.0)
    factor = np.where(cc, (1.0 + 3.65 * alpha) / (1.0 + alpha) ** 2, 1.0)
    return fc * factor


def kupfer_combined_gate(*, z_grid, cum_area, cum_Az, cum_Iy, yhalf, sheer,
                         y_bar, I_tr, t_in, zone_bounds, zone_n, zone_ft,
                         zone_fc, instances, k_lat, Kt=None, rail_lat_Itr=None):
    """
    PER-FIBRE, PER-STATION, PER-LOAD-INSTANCE Kupfer biaxial utilisation.

    For EACH load instance (a single race case x load model x seating -- NOT the
    envelope), at EACH station, sweeping the section-depth grid z, the coexisting
    stress state at the outboard fibre is

        sigma_x(z) = n(z) * (M*12) * (z - y_bar) / I_tr          [long. bending]
        sigma_y(z) = - n(z) * (M_lat*12) * yhalf(z) / I_lat_tr   [transverse HOOP,
                     M_lat = k_lat*|M|, taken as COMPRESSION -- the governing
                     turn direction that puts the bending-tension fibre into the
                     tension-compression quadrant]

    LRFD NOTE -- k_lat MULTIPLIES THE FACTORED MOMENT, and must.  The
    centripetal turn load is INERTIAL: it is the same mass, times the same
    turn acceleration, reacted by the same waterplane, so M_lat is a fixed
    ratio k_lat = V^2/(gR) of the vertical moment produced by that mass.
    Factoring the mass therefore factors both moments identically.  Because
    ``instances`` already carry the combined (factored, under LRFD) M and V,
    M_lat = k_lat*|M| is automatically the correctly factored lateral moment
    and this function needs no methodology awareness.  Applying k_lat to the
    UNfactored moment would leave the transverse component at service level
    while the longitudinal one was factored -- an inconsistent load state at
    the same fibre, which is exactly what the biaxial criterion exists to
    evaluate correctly.
        tau_xy(z)  = |V| * Q_tr(z) / (I_tr * 2 t)                [transverse shear
                     via the transformed first moment Q_tr(z); TORSION EXCLUDED,
                     see note below]

    with all section quantities TRANSFORMED (modular ratio n_i = E_i/E_ref) and
    the allowables taken from the zone that actually owns fibre z, so composite
    pours are checked against each material's OWN f_t / f'c (never a single
    global allowable -- the bug that hit the hoop gate twice).

    The principal stresses feed ``kupfer_utilization``; the UTILISATION (not the
    component stresses) is enveloped over fibres, then stations, then instances.

    TORSION is deliberately excluded from tau_xy: the suite's torsion figure is
    an open-section St-Venant bound that overstates by ~11x (it ignores warping
    restraint), so folding it into the fibre stress state would be nonsense.

    Q_tr(z) is built on the grid as a cumulative sum of n(z)*(dAz - y_bar*dA), so
    it is exact at the fibres and needs no interpolation; it -> 0 at both extreme
    fibres (tau peaks at the NA where bending -> 0) and is max at the NA.

    Returns a dict of per-station enveloped utilisations (Kupfer, and a Rankine
    max-principal value on the SAME combined state for comparison) plus the
    governing fibre/station/instance/quadrant.
    """
    zg = np.asarray(z_grid, dtype=float)
    nz = zg.shape[0]
    nst = np.asarray(y_bar).shape[0]
    cum_area = np.asarray(cum_area, float); cum_Az = np.asarray(cum_Az, float)
    cum_Iy = np.asarray(cum_Iy, float);     yhalf = np.asarray(yhalf, float)
    y_bar = np.asarray(y_bar, float);       I_tr = np.maximum(np.asarray(I_tr, float), 1e-9)
    sheer = np.asarray(sheer, float)
    # broadcast a scalar or (nst,) Kt to (nst,) so Kt[:, None] is always valid
    Kt = np.ones(nst) if Kt is None else (np.asarray(Kt, float) * np.ones(nst))
    z2 = np.broadcast_to(zg[None, :], (nst, nz))

    # ── Per-(station, fibre) zone assignment: n(z), f_t(z), f'c(z), Kt(z) ──────
    n_zones = len(zone_ft)
    n2  = np.ones((nst, nz))
    ft2 = np.full((nst, nz), 1e9)
    fc2 = np.full((nst, nz), 1e9)
    kt2 = np.ones((nst, nz))
    for i in range(n_zones):
        zb = np.asarray(zone_bounds[i], float);   zt = np.asarray(zone_bounds[i + 1], float)
        top = (i == n_zones - 1)
        inb = (z2 >= zb[:, None] - 1e-9) & (z2 <= zt[:, None] + (1e-9 if top else -1e-12))
        n2  = np.where(inb, np.asarray(zone_n[i], float)[:, None], n2)
        ft2 = np.where(inb, np.asarray(zone_ft[i], float)[:, None], ft2)
        fc2 = np.where(inb, np.asarray(zone_fc[i], float)[:, None], fc2)
        if top:
            kt2 = np.where(inb, Kt[:, None], kt2)     # fillet Kt on the top zone

    # ── Transformed section increments on the grid ────────────────────────────
    dA  = np.diff(cum_area, axis=1, prepend=0.0)
    dAz = np.diff(cum_Az,   axis=1, prepend=0.0)
    dIy = np.diff(cum_Iy,   axis=1, prepend=0.0)
    # Transformed first moment about the NA of everything BELOW each fibre.
    Q_tr = np.cumsum(n2 * (dAz - y_bar[:, None] * dA), axis=1)
    # Transformed lateral inertia about the vertical centreplane (whole section).
    I_lat = np.sum(n2 * dIy, axis=1)
    if rail_lat_Itr is not None:
        I_lat = I_lat + np.asarray(rail_lat_Itr, float)
    I_lat = np.maximum(I_lat, 1e-9)[:, None]

    # Fibre validity.  The station must be REAL STRUCTURE, not a degenerate tip
    # -- same DEGENERATE_SECTION_A_IN2 test the hoop gate applies, on the same
    # A_full, so the two transverse checks can never again disagree about which
    # stations exist.  Without it this gate evaluates the two stations the hoop
    # gate skips, where a 1e-6 half-breadth floor over a 1e-9 inertia floor
    # fabricates ~1500 psi of transverse stress on a section of zero area.
    real_section = structural_station_mask(cum_area, zg, sheer, yhalf)[:, None]
    valid = (z2 <= sheer[:, None] + 1e-6) & (yhalf > 1e-9) & real_section
    b_shear = 2.0 * float(t_in)

    # Envelope accumulators
    util_env = np.zeros(nst)
    rank_env = np.zeros(nst)
    quad_env = np.zeros(nst, dtype=int)
    fib_env  = np.zeros(nst, dtype=int)
    inst_env = np.full(nst, -1, dtype=int)
    _packed = np.zeros((nst, 5))          # sx,sy,tau,s1,s2 at each station's worst

    for k, (ci, M, V) in enumerate(instances):
        M = np.asarray(M, float); V = np.asarray(V, float)
        M_lat = k_lat * np.abs(M)
        sx = kt2 * n2 * (M[:, None] * 12.0) * (z2 - y_bar[:, None]) / I_tr[:, None]
        sy_mag = (n2 * (M_lat[:, None] * 12.0) * yhalf) / I_lat       # >= 0
        tau = np.abs(V[:, None]) * Q_tr / (I_tr[:, None] * b_shear)
        # The turn is bidirectional, so at each fibre the hoop stress can be
        # COMPRESSION (with bending tension -> tension-compression, capacity
        # degraded) or TENSION (-> tension-tension, i.e. the pure transverse-
        # tension mode).  Evaluate both signs and keep the worse -- this is what
        # lets the single Kupfer gate subsume BOTH old independent tensile checks
        # (longitudinal at sy=0, transverse at the wide fibre) as well as the T-C
        # interaction they never captured.
        s1n, s2n = principal_stresses(sx, -sy_mag, tau)
        un, qn = kupfer_utilization(s1n, s2n, ft2, fc2)
        s1p, s2p = principal_stresses(sx, sy_mag, tau)
        up, qp = kupfer_utilization(s1p, s2p, ft2, fc2)
        neg = un >= up
        util = np.where(neg, un, up)
        quad = np.where(neg, qn, qp)
        s1 = np.where(neg, s1n, s1p); s2 = np.where(neg, s2n, s2p)
        sy = np.where(neg, -sy_mag, sy_mag)
        rank = np.where(s1 > 0.0, s1 / ft2, 0.0)      # Rankine on the same state
        util = np.where(valid, util, 0.0)
        rank = np.where(valid, rank, 0.0)
        # worst fibre for THIS instance at each station
        jmax = np.argmax(util, axis=1)
        rows = np.arange(nst)
        u_st = util[rows, jmax]
        upd = u_st > util_env
        util_env = np.where(upd, u_st, util_env)
        quad_env = np.where(upd, quad[rows, jmax], quad_env)
        fib_env  = np.where(upd, jmax, fib_env)
        inst_env = np.where(upd, k, inst_env)
        for c, arr in enumerate((sx, sy, tau, s1, s2)):
            _packed[:, c] = np.where(upd, arr[rows, jmax], _packed[:, c])
        # Rankine envelope on the SAME combined state (its own worst fibre)
        rank_env = np.maximum(rank_env, np.max(rank, axis=1))

    gi = int(np.argmax(util_env))
    gj = int(fib_env[gi])
    _qname = {0: "compression-compression", 1: "tension-tension",
              2: "tension-compression"}
    gov = {
        "station_index": gi, "fibre_index": gj,
        "fibre_z_in": float(zg[gj]), "instance": int(inst_env[gi]),
        "case_index": (int(instances[int(inst_env[gi])][0])
                       if inst_env[gi] >= 0 else -1),
        "quadrant": _qname.get(int(quad_env[gi]), "n/a"),
        "util": float(util_env[gi]),
        "sigma_x": float(_packed[gi, 0]), "sigma_y": float(_packed[gi, 1]),
        "tau_xy": float(_packed[gi, 2]),
        "sigma_1": float(_packed[gi, 3]), "sigma_2": float(_packed[gi, 4]),
    }
    return {
        "util": util_env, "util_max": float(np.max(util_env)),
        "rankine_util": rank_env, "rankine_max": float(np.max(rank_env)),
        "quad_map": quad_env, "gov": gov,
    }


def punching_shear_check(paddler_lbs, t_in, fc_psi, fos,
                         column_in=3.0, d_in=None, lambda_lw=0.75,
                         beta=1.0, alpha_s=40.0, shape="square", size_in=None,
                         b0_override=None):
    """
    Kneeling-paddler punching (two-way) shear on the hull bottom — ACI 318-19
    §22.6.5.2.  A paddler kneels on a single square patch; the load punches
    through the thin shell.

        demand   v_u = P / (b0 · t),   b0 = 4 (column + d)   (critical
                 perimeter taken d/2 out from the patch face)

    ACI gives THREE concrete capacities; the governing capacity is the
    SMALLEST (which one wins depends on geometry, so a single formula is
    non-conservative):

        (a) v_c = 4 λ √f'c
        (b) v_c = (2 + 4/β) λ √f'c        β = long/short of the loaded area
        (c) v_c = (2 + α_s d / b0) λ √f'c α_s = 40 interior / 30 edge / 20 corner

    For a thin shell, small d relative to b0 makes (c) govern — its capacity
    falls well below the (a) value.  Allowable = min(v_c) / FoS.

    ``d`` (effective depth to the reinforcing mesh) defaults to t/2 (mesh at
    mid-shell).  ``fc_psi`` is f'c of the mix the paddler kneels on — the hull
    BOTTOM, i.e. the bottom-flange (tension) mix, NOT the top mix.  Returns a
    dict with all three capacities, the governing one, demand, allowable, D/C
    and pass flag — the report and any gate read the same values.
    """
    d = float(t_in) / 2.0 if d_in is None else float(d_in)
    _sz = float(column_in) if size_in is None else float(size_in)
    if b0_override is not None:
        b0 = float(b0_override)                       # merged/combined perimeter
    elif shape == "circle":
        b0 = np.pi * (_sz + d)                        # d_load = circle diameter
    else:
        b0 = 4.0 * (_sz + d)                          # square patch side + d
    area = b0 * float(t_in)
    v_u = float(paddler_lbs) / max(area, 1e-9)
    sq = float(lambda_lw) * np.sqrt(max(float(fc_psi), 0.0))
    v_c_a = 4.0 * sq
    v_c_b = (2.0 + 4.0 / max(float(beta), 1e-9)) * sq
    v_c_c = (2.0 + float(alpha_s) * d / max(b0, 1e-9)) * sq
    caps = {"a": v_c_a, "b": v_c_b, "c": v_c_c}
    gov = min(caps, key=caps.get)
    v_c = caps[gov]
    allow = v_c / max(float(fos), 1e-9)
    return {"v_u": v_u, "v_c": v_c, "v_c_a": v_c_a, "v_c_b": v_c_b,
            "v_c_c": v_c_c, "governing": gov, "allow": allow,
            "dc": v_u / max(allow, 1e-9), "ok": v_u <= allow,
            "b0": b0, "d": d, "area": area, "beta": float(beta),
            "alpha_s": float(alpha_s), "shape": shape, "size_in": _sz,
            "req_strength": v_u * float(fos)}


def punching_two_knee(total_lbs, t_in, fc_psi, fos, *, knee_dia_in=3.0,
                      knee_spacing_in=8.0, split=0.5, pad_in=0.0,
                      lambda_lw=0.75, alpha_s=40.0, d_in=None):
    """
    Two-circle kneeling-paddler punching model.

    A lifted-and-placed paddler lands on TWO knees (no heel-first step-in, so a
    single-heel case does not govern).  The total weight is split across two
    circular contacts of diameter ``knee_dia_in`` (+ ``2*pad_in`` if a
    load-spreading knee pad is used).  Each circle's ACI critical perimeter is
    b0 = π(d_load + d) at the d/2 offset (a circle has a SMALLER perimeter than
    the same-width square, raising stress per unit load and partly offsetting
    the load split).

    Interaction: the two d/2 critical circles (radius (d_load+d)/2) MERGE when
    the knee spacing is below twice that radius; then they cannot be treated as
    independent half-load punches and the combined (obround) perimeter carrying
    the FULL load is evaluated instead (a merged perimeter can be worse per unit
    load).  This function checks that and reports which case applies.

    Returns a dict: ``interact`` flag, ``per_knee_lbs``, ``crit_radius``,
    ``d_load``, and the governing ``check`` (a punching_shear_check result).
    """
    # Effective depth to the mesh: t/2 by default, or the caller-supplied
    # layer-interface depth so the whole punching family shares one d source.
    d = float(t_in) / 2.0 if d_in is None else float(d_in)
    d_load = float(knee_dia_in) + 2.0 * float(pad_in)
    crit_r = (d_load + d) / 2.0
    interact = float(knee_spacing_in) < 2.0 * crit_r
    if not interact:
        per_knee = float(total_lbs) * float(split)
        chk = punching_shear_check(per_knee, t_in, fc_psi, fos,
                                   lambda_lw=lambda_lw, alpha_s=alpha_s,
                                   shape="circle", size_in=d_load, d_in=d)
    else:
        # Combined obround perimeter around both circles carrying the full load
        b0 = 2.0 * float(knee_spacing_in) + 2.0 * np.pi * crit_r
        per_knee = float(total_lbs)
        chk = punching_shear_check(total_lbs, t_in, fc_psi, fos,
                                   lambda_lw=lambda_lw, alpha_s=alpha_s,
                                   shape="circle", size_in=d_load,
                                   b0_override=b0, d_in=d)
    return {"interact": interact, "per_knee_lbs": per_knee,
            "split_frac": float(split),
            "crit_radius_in": crit_r, "d_load_in": d_load,
            "knee_spacing_in": float(knee_spacing_in), "pad_in": float(pad_in),
            "check": chk}


def run_torsion_lateral_checks(
    x_ft: np.ndarray,
    A_in2: np.ndarray,
    width_ft: np.ndarray,
    c_bot: np.ndarray,
    c_top: np.ndarray,
    thickness_in: float,
    hull_depth_in: float,
    allowable_shear_psi: float,
    allowable_bending_psi: float,
    *,
    paddler_weights: list,
    total_weight_lbs: float,
    m_vert_env_lbft=None,
    paddler_positions_ft=None,
    avg_speed_mps: float = 2.5,
    min_turn_radius_m: float = 4.0,
    g: float = 9.807,
    paddle_force_fraction: float = 0.25,
    paddle_offset_ft: float = 0.5,
):
    """
    Per-station torsion and lateral-load structural checks.

    Evaluates both checks at every hull station using the approximate
    hand-calc formulas documented in _section_torsion_constant() and
    _section_lateral_inertia(), then reports the critical (worst) station.

    Design loads (per-station DISTRIBUTIONS, not scalars)
    -----------------------------------------------------
    Torsion : one-sided paddle stroke → internal torque diagram T(x), reacted
              by the waterplane so it closes to zero at the free ends.
    Lateral : centripetal bending M_lat(x) = k_lat·|M_vert(x)|, k_lat = V²/gR.

    Both vanish at the bow/stern taper, so neither diverges with station count
    (the earlier scalars F_lat·H_CG and a single T applied at every station both
    went as 1/section at the degenerate tips and were resolution-dependent).

    ADVISORY ONLY.  The torsion number is a CONSERVATIVE open-section St. Venant
    bound: J = t²·A/3 ignores the warping restraint from the closed stems and
    the gunwale flanges, so τ is overstated by ~an order of magnitude.  It does
    NOT gate the sweep; it is logged for visibility only.

    Stress formulae
    ---------------
    Torsional shear  : τ(x) = |T(x)| · t / J_open   (St. Venant, open section)
    Lateral bending  : σ(x) = M_lat(x) · c_lat / I_lat

    Parameters
    ----------
    x_ft                 : station positions [ft]
    A_in2                : total shell area at each station [in²]
    width_ft             : full beam (gunwale) at each station [ft]
    c_bot                : NA height above keel at each station [in]
    c_top                : distance NA→top at each station [in]
    thickness_in         : uniform shell thickness [in]
    hull_depth_in        : total mold depth (keel to gunwale) [in]
    allowable_shear_psi  : allowable torsional shear stress [psi]
    allowable_bending_psi: allowable lateral bending stress [psi]
    paddler_weights      : paddler static weights [lb]
    total_weight_lbs     : canoe + all paddlers [lb]
    avg_speed_mps        : design slalom speed [m/s]
    min_turn_radius_m    : design slalom radius [m]
    g                    : gravitational acceleration [m/s²]
    paddle_force_fraction: lateral stroke force / paddler weight
    paddle_offset_ft     : blade distance outboard of gunwale [ft]

    Returns
    -------
    TorsionLateralResult
    """
    from domain.models import TorsionLateralResult

    n       = len(x_ft)
    beam_ft = float(np.max(width_ft))

    # ── Design loads — DISTRIBUTIONS, not scalars ─────────────────────────────
    dlf   = getattr(config, 'DYNAMIC_LOAD_FACTOR', 1.2)
    a_c   = avg_speed_mps ** 2 / max(min_turn_radius_m, 0.1)
    k_lat = a_c / g
    F_lat_lb = total_weight_lbs * k_lat                    # total centripetal [lb]

    # LATERAL bending: same correction as the transverse-hoop gate.  The
    # centripetal load follows the local mass and is reacted by the waterplane,
    # so M_lat(x) = k_lat·|M_vert(x)| — it vanishes at the free ends instead of
    # applying the full-hull F_lat·H_CG scalar at every station (which went as
    # 1/b² at the bow/stern taper).  Legacy scalar only if no envelope supplied.
    if m_vert_env_lbft is not None:
        M_lat_arr = k_lat * np.abs(np.asarray(m_vert_env_lbft, dtype=float)) * 12.0
    else:
        _, _m0 = lateral_design_moment(total_weight_lbs, avg_speed_mps,
                                       min_turn_radius_m, hull_depth_in, g)
        M_lat_arr = np.full(n, float(_m0))
    M_lat_lb_in = float(np.max(M_lat_arr))                 # peak, for reporting

    # TORSION: internal torque DIAGRAM T(x) from the worst one-sided paddle
    # stroke (applied at the heaviest paddler), reacted by a waterplane-
    # distributed roll reaction so T(x) closes to zero at the free ends.  |T| ≤
    # T0 and T → 0 as the section vanishes, so τ is resolution-independent.  See
    # the docstring: this is an ADVISORY conservative open-section bound only.
    T_lb_in = torsional_design_torque(
        paddler_weights, beam_ft, dynamic_load_factor=dlf,
        paddle_force_fraction=paddle_force_fraction, paddle_offset_ft=paddle_offset_ft)
    _w = np.maximum(np.asarray(width_ft, dtype=float), 0.0)
    _Wtot = float(trapezoid(_w, x_ft))
    if _Wtot > 1e-9:
        if paddler_positions_ft is not None and len(paddler_positions_ft):
            x_p = float(np.asarray(paddler_positions_ft, dtype=float)[
                int(np.argmax(paddler_weights))])
        else:
            x_p = float(x_ft[n // 2])                       # fallback: midship
        Wcum = cumulative_trapezoid(_w, x_ft, initial=0.0) / _Wtot     # 0 → 1
        T_arr = T_lb_in * ((x_ft >= x_p).astype(float) - Wcum)         # closes to 0
    else:
        T_arr = np.zeros(n)

    # ── Per-station section properties and stresses ───────────────────────────
    J_arr         = np.empty(n)
    tau_arr       = np.empty(n)
    I_lat_arr     = np.empty(n)
    c_lat_arr     = np.empty(n)
    sigma_lat_arr = np.empty(n)

    for i in range(n):
        J_arr[i]     = _section_torsion_constant(A_in2[i], thickness_in)
        tau_arr[i]   = abs(T_arr[i]) * thickness_in / J_arr[i]

        I_lat_arr[i], c_lat_arr[i] = _section_lateral_inertia(
            width_ft[i], c_bot[i], c_top[i], thickness_in
        )
        sigma_lat_arr[i] = M_lat_arr[i] * c_lat_arr[i] / I_lat_arr[i]

    # ── Critical stations ─────────────────────────────────────────────────────
    # Bow/stern tip stations carry EPS-floor shell area (1e-6 in²) which
    # drives J → 0 and I_lat → 1e-9, producing non-physical singularities.
    # Exclude them from the peak search; any station with real geometry has
    # A_in2 >> DEGENERATE_SECTION_A_IN2 for the thinnest practical shell.
    #
    # This check reaches the same conclusion as the hoop, Kupfer and
    # longitudinal checks, and used to spell it out separately.  It now shares
    # their constant so the five of them cannot drift apart; the breadth half of
    # structural_station_mask needs a yhalf grid this function is not given, so
    # only the area test applies here — which is the one that matters for J and
    # I_lat, both driven by the shell area collapsing.
    structural_mask = A_in2 > DEGENERATE_SECTION_A_IN2
    if not np.any(structural_mask):
        structural_mask = np.ones(n, dtype=bool)

    crit_t_idx = int(np.argmax(np.where(structural_mask, tau_arr,       0.0)))
    crit_l_idx = int(np.argmax(np.where(structural_mask, sigma_lat_arr, 0.0)))

    tau_max       = float(tau_arr[crit_t_idx])
    sigma_lat_max = float(sigma_lat_arr[crit_l_idx])
    torsion_ok    = tau_max <= allowable_shear_psi
    lateral_ok    = sigma_lat_max <= allowable_bending_psi

    # ADVISORY, and the label must say so.  Neither number gates the sweep,
    # and both overstate by construction -- but a bare "[WARNING]" is
    # indistinguishable from a real failure, so a reader cannot tell that an
    # exceedance here needs no action.  The transverse stress that DOES gate is
    # the Kupfer biaxial check, on the properly transformed composite section.
    log = logger.warning if not torsion_ok else logger.info
    log(
        "Torsion (secondary): tau_max=%.1f psi  allow=%.1f psi  [%s] "
        "-- open-section St. Venant bound, overstated ~10x; does not gate",
        tau_max, allowable_shear_psi,
        "OK" if torsion_ok else "ADVISORY: over, no action needed",
    )
    log = logger.warning if not lateral_ok else logger.info
    log(
        "Lateral (secondary): sigma_lat=%.1f psi  allow=%.1f psi  [%s] "
        "-- hand-calc I_lat valid only for B_half < D/3; a beamy hull is "
        "outside that and the value is overstated; does not gate",
        sigma_lat_max, allowable_bending_psi,
        "OK" if lateral_ok else "ADVISORY: over, no action needed",
    )

    return TorsionLateralResult(
        T_design_lb_in         = T_lb_in,
        M_lat_lb_in            = M_lat_lb_in,
        F_lat_lb               = F_lat_lb,
        J_min_in4              = float(J_arr[crit_t_idx]),
        tau_max_psi            = tau_max,
        tau_allowable_psi      = allowable_shear_psi,
        torsion_utilization    = tau_max / max(allowable_shear_psi, 1e-9),
        torsion_status         = "OK" if torsion_ok else "ADVISORY",
        critical_torsion_x_ft  = float(x_ft[crit_t_idx]),
        I_lat_min_in4          = float(I_lat_arr[crit_l_idx]),
        c_lat_max_in           = float(c_lat_arr[crit_l_idx]),
        sigma_lat_psi          = sigma_lat_max,
        sigma_lat_allowable_psi = allowable_bending_psi,
        lateral_utilization    = sigma_lat_max / max(allowable_bending_psi, 1e-9),
        lateral_status         = "OK" if lateral_ok else "ADVISORY",
        critical_lateral_x_ft  = float(x_ft[crit_l_idx]),
        x_ft                   = x_ft.copy(),
        J_in4                  = J_arr,
        tau_psi                = tau_arr,
        I_lat_in4              = I_lat_arr,
        sigma_lat_psi_arr      = sigma_lat_arr,
    )


# ── DESIGN METHODOLOGY: LRFD / ASD ──────────────────────────────────────────
#
# The FAILURE CRITERION is the same under both methods -- the Kupfer-Gerstle
# biaxial envelope, per fibre, per station, per load instance, with per-zone
# allowables.  LRFD and ASD change only WHERE the safety lives:
#
#   ASD   nominal loads  (D + L)                vs  allowable  f_n / Omega
#   LRFD  factored loads (gamma_D*D + gamma_L*L) vs  design cap  phi * f_n
#
# so both reduce to the SAME comparison, ``utilisation <= acceptance margin``,
# with the method deciding (a) how the dead and live envelopes are combined and
# (b) what divides the nominal strength.  Nothing in kupfer_utilization,
# kupfer_combined_gate or the acceptance margin changes; there is one
# implementation of the criterion and both methods feed it.
#
# The acceptance margin is orthogonal to all of this -- it compensates for the
# utilisation not converging with station count (see kupfer_acceptance_limit)
# and applies identically under either method.
DESIGN_METHOD_LRFD = "LRFD"
DESIGN_METHOD_ASD = "ASD"


def design_method():
    """Active design methodology, ``"LRFD"`` (default) or ``"ASD"``."""
    m = str(getattr(config, "DESIGN_METHOD", DESIGN_METHOD_LRFD)).upper().strip()
    return m if m in (DESIGN_METHOD_LRFD, DESIGN_METHOD_ASD) else DESIGN_METHOD_LRFD


def load_combinations(case_name=None):
    """
    The (gamma_dead, gamma_live) pairs to envelope over for one load case.

    ASD returns the single nominal combination (1, 1): service loads, with the
    safety carried entirely by Omega on the capacity side.

    LRFD returns the factored combinations.  TWO of them by default, because on
    this hull the dead load COUNTERACTS the live load at the governing station
    -- measured M_dead = -16.4 lb*ft (hogging) against M_live = +242.9 lb*ft
    (sagging).  Factoring a relieving permanent load UP by 1.2 understates the
    demand, so ASCE 7-22 Sec. 2.3.1 requires the dead factor to drop to 0.9
    wherever dead counteracts the variable load.  Enveloping (1.2, 1.6) and
    (0.9, 1.6) covers both senses without the caller having to know which
    stations relieve and which add -- that varies by station, fibre and case.

    Per-case overrides come from config.LOAD_FACTORS_BY_CASE, e.g.
    ``{"race_4": (1.3, 1.7)}``; anything unlisted uses the globals.
    """
    if design_method() == DESIGN_METHOD_ASD:
        return [(1.0, 1.0)]
    gd = float(getattr(config, "GAMMA_DEAD", 1.2))
    gl = float(getattr(config, "GAMMA_LIVE", 1.6))
    by_case = getattr(config, "LOAD_FACTORS_BY_CASE", None) or {}
    if case_name and case_name in by_case:
        gd, gl = (float(v) for v in by_case[case_name])
    combos = [(gd, gl)]
    if getattr(config, "LRFD_COUNTERACTING_DEAD", True):
        gd_relief = float(getattr(config, "GAMMA_DEAD_COUNTERACTING", 0.9))
        if abs(gd_relief - gd) > 1e-12:
            combos.append((gd_relief, gl))
    return combos


def design_summary(case_names=None, m_dead=None, m_live=None):
    """
    What the active methodology actually applied, for the report.

    ONE builder, used both by calculate_moment_envelope (which supplies the
    per-case dead/live curves) and by the sweep's result assembly (which does
    not, on paths that never split).  The report reads this rather than
    re-deriving anything from config, so it cannot describe a different
    configuration than the run it documents.
    """
    names = list(case_names or ["race_4", "race_2", "race_3"])
    asd = design_method() == DESIGN_METHOD_ASD
    return {
        "method": design_method(),
        "combinations": {n: load_combinations(n) for n in names},
        "dynamic_load_factor": float(getattr(config, "DYNAMIC_LOAD_FACTOR", 1.2)),
        "capacity_divisor": capacity_divisor(),
        "phi": (None if asd else float(getattr(config, "PHI_RESISTANCE", 0.60))),
        "omega": (float(getattr(config, "OMEGA_SAFETY", None)
                        or getattr(config, "FACTOR_OF_SAFETY", 1.5))
                  if asd else None),
        "M_dead": dict(m_dead or {}),
        "M_live": dict(m_live or {}),
    }


def capacity_divisor():
    """
    What divides the NOMINAL material strength to give the value the check is
    made against.

    ASD  -> Omega (the factor of safety): allowable = f_n / Omega.
    LRFD -> 1/phi:                        design capacity = phi * f_n.

    Returning a single divisor for both keeps one code path: every consumer
    asks for ``strength / capacity_divisor()`` and the methodology is invisible
    downstream.  With ASD and Omega = FACTOR_OF_SAFETY this reproduces the
    historical behaviour exactly, which is the refactor's regression test.
    """
    if design_method() == DESIGN_METHOD_ASD:
        return float(getattr(config, "OMEGA_SAFETY", None)
                     or getattr(config, "FACTOR_OF_SAFETY", 1.5))
    phi = float(getattr(config, "PHI_RESISTANCE", 0.60))
    return 1.0 / max(phi, 1e-9)


_BeamGeom = collections.namedtuple("_BeamGeom", "tw S0 S1 S2 det")


def _beam_geom(x, width_ft):
    """
    The per-geometry constants of a beam solve, computed ONCE.

    `_solve_beam_case` makes seven `trapezoid` calls, and FOUR of them depend
    only on `x` and `width_ft` -- both fixed for a whole sweep task.  They were
    being recomputed on every solve: 12,330,920 `trapezoid` calls in one
    profiled task, each re-running `np.diff(x)` on a grid that never changes
    (15.9 M `diff` calls, 79.4 s).  Two of the four were literally the same
    integral computed twice in the same function body.

    S0/S1/S2 are still computed WITH `trapezoid`, deliberately: same function,
    same inputs, just once instead of 1.76 M times, so they are bit-identical
    to what the old code produced.  Only `tw` below changes any arithmetic.

    `tw` is the trapezoid rule as a WEIGHT VECTOR, so an integral becomes one
    dot product: ``integral(y) = tw @ y``.  Exact in exact arithmetic; in
    floating point it reassociates the sum, moving results by ~1e-16 relative.
    Used only for the three integrals that actually vary with the load.
    """
    x = np.asarray(x, dtype=float)
    w = np.asarray(width_ft, dtype=float)
    tw = np.zeros_like(x)
    if x.size >= 2:
        d = np.diff(x)
        tw[0] = 0.5 * d[0]
        tw[-1] = 0.5 * d[-1]
        if x.size > 2:
            tw[1:-1] = 0.5 * (x[2:] - x[:-2])
    S0 = trapezoid(w, x)
    S1 = trapezoid(w * x, x)
    S2 = trapezoid(w * x * x, x)
    return _BeamGeom(tw, S0, S1, S2, S0 * S2 - S1 * S1)


def _solve_beam_case(x, dx, w_self, width_ft, coords, dynamic_weights, L,
                     advanced_loading=None, w_self_scale=1.0,
                     payload_scale=1.0, geom=None):
    """
    One balanced beam solve: returns (M, V) for a single loading case.

    ``advanced_loading`` selects the paddler load model (None = follow the
    global).  calculate_moment_envelope passes it explicitly so it can envelope
    both models -- see the load-model note there.

    ``w_self_scale`` / ``payload_scale`` scale the dead and live load
    independently BEFORE the buoyancy reaction is computed, which is what makes
    the dead/live split exact -- see _solve_beam_case_split.
    """
    w_payload = apply_realistic_paddler_loads(x, dx, coords, dynamic_weights, L,
                                              advanced_loading=advanced_loading)
    w_self = np.asarray(w_self, float) * float(w_self_scale)
    w_payload = w_payload * float(payload_scale)

    # Per-geometry constants, computed once by the caller when there is one.
    # Falling back keeps every direct caller (tests included) correct.
    g = geom if geom is not None else _beam_geom(x, width_ft)

    total_weight = float(g.tw @ (w_self + w_payload))
    # g.S0 IS trapezoid(width_ft, x) -- the same integral this line used to
    # compute for itself, and then S0 computed again a few lines below.
    q_buoyancy = (width_ft / g.S0) * total_weight

    q_net = q_buoyancy - w_self - w_payload

    # BALANCE.  A free-free beam closes (V[-1] = M[-1] = 0) only if the net
    # load is self-equilibrated in BOTH force and moment:
    #     int q dx = 0        and        int q*x dx = 0
    # so the correction needs two degrees of freedom.  The physical ones are
    # SINKAGE and TRIM: the hull settles and tilts until buoyancy balances the
    # load.  Both act through the WATERPLANE, so the correction is
    #     dq(x) = width(x) * (a + b*x)
    # -- shaped like the buoyancy it adjusts.  Weighting by width matters: the
    # bow and stern stations have width -> 0 (the offsets include degenerate
    # tips), and a correction of the bare form (a + b*x) would add or remove
    # buoyancy out at the tips where the hull has no waterplane to generate it,
    # which measurably distorts the envelopes.
    #
    # The previous code applied two SEPARATE uniform corrections: one to zero
    # int q, then another subtracted from V to zero int V.  The second shifted
    # the whole shear curve, destroying the closure the first had just achieved
    # -- V[-1] ended at -m_residual instead of 0, i.e. the model quietly
    # planted a point force at the origin.  Solving sinkage and trim together
    # closes both envelopes without any such shift.
    _w = np.asarray(width_ft, dtype=float)
    S0, S1, S2, det = g.S0, g.S1, g.S2, g.det   # hoisted: geometry, not load
    R0 = float(g.tw @ q_net)              # int q dx
    R1 = float(g.tw @ (q_net * x))        # int q*x dx
    if abs(det) > 1e-9 and S0 > 1e-9:
        a = (R0 * S2 - R1 * S1) / det
        b = (S0 * R1 - S1 * R0) / det
        q_net = q_net - _w * (a + b * x)
    elif S0 > 1e-9:                       # degenerate: force balance only
        q_net = q_net - _w * (R0 / S0)

    V = cumulative_trapezoid(q_net, x, initial=0)
    M = cumulative_trapezoid(V, x, initial=0)
    return M, V


def _solve_beam_case_split(x, dx, w_self, width_ft, coords, dynamic_weights, L,
                           advanced_loading=None, geom=None):
    """
    The same beam solve, resolved into independent DEAD and LIVE envelopes:
    returns (M_dead, V_dead, M_live, V_live).

        DEAD = hull self-weight, INCLUDING the stem-rocker cantilever tip mass
               (the rocker is part of w_self on the extended grid, so it lands
               in the dead component automatically).
        LIVE = paddlers, at the dynamic (impact-amplified) weights.

    HOW BUOYANCY IS APPORTIONED, AND WHY THIS RULE.
    Buoyancy is a REACTION proportional to total weight, so it belongs to
    neither component on its own, and the waterline itself shifts as the live
    load is applied.  Both facts would normally make the split ambiguous.  They
    do not here, because the whole solve is LINEAR in the applied load:

        buoyancy      q_b = (width / int width) * total_weight     linear in W
        net load      q   = q_b - w_self - w_payload               linear
        sinkage/trim  dq  = width*(a + b*x), with (a, b) solved from
                            int q dx and int q*x dx                linear in q
        V = int q dx,  M = int V dx                                linear

    Every step is a linear operator, so solving the dead-only and live-only
    cases and adding them is not an approximation -- it is the same
    computation.  Each component therefore carries exactly the buoyancy its own
    weight generates, which is also what "apportion buoyancy in proportion to
    weight" means: the two candidate rules coincide.  The waterline shift is
    captured, because sinkage and trim ARE the (a, b) correction and they split
    with everything else.

    VERIFIED on the real hull, both paddler load models, rocker on and off:
    max |M_dead + M_live - M_total| = 2.2e-12 lb*ft (1.0e-14 relative), and
    doubling the live load doubles M_live bit-exactly.  tests/test_lrfd_asd.py
    pins this -- if the hydrodynamic model ever becomes nonlinear in the load
    (e.g. the waterplane width starts responding to draft), that test fails and
    this docstring is wrong.

    The physics is untouched: this calls the SAME _solve_beam_case with the
    same buoyancy-vs-weight equilibrium and waterplane-shaped sinkage/trim.
    Nothing is modelled as simply supported.
    """
    g = geom if geom is not None else _beam_geom(x, width_ft)
    M_d, V_d = _solve_beam_case(x, dx, w_self, width_ft, coords,
                                dynamic_weights, L, advanced_loading,
                                payload_scale=0.0, geom=g)
    M_l, V_l = _solve_beam_case(x, dx, w_self, width_ft, coords,
                                dynamic_weights, L, advanced_loading,
                                w_self_scale=0.0, geom=g)
    return M_d, V_d, M_l, V_l


def _combined_cases(x, dx, w_self, width_ft, coords, dynamic_weights, L,
                    advanced_loading, combos, geom=None):
    """
    (M, V) for each (gamma_dead, gamma_live) combination, from ONE split solve.

    The nominal combination (1, 1) takes the un-split solve instead of adding
    the two components back together.  That is not an optimisation detail: the
    dead/live split is exact in exact arithmetic, but re-summing the pieces
    reassociates the floating-point operations, which perturbs the result by
    ~1e-14 relative.  Avoiding an unnecessary perturbation on the path most
    runs take is worth one branch, so the split is used only where the
    combination actually needs it.

    THIS USED TO CLAIM BYTE-IDENTITY with the pre-LRFD engine, and that claim
    is no longer true -- `_beam_geom` replaced the load-dependent integrals in
    `_solve_beam_case` with dot products against a precomputed trapezoid weight
    vector, which reassociates the summation by ~1e-16 relative.  The claim is
    retired rather than quietly left standing: a comment asserting an exactness
    the code no longer provides is worse than no comment, because the next
    person debugging a 1e-16 discrepancy would trust it.  The LRFD/ASD
    regression tests assert to 1e-11/1e-12, four orders of magnitude looser
    than the perturbation and far tighter than anything physical, so what they
    actually prove -- that the split is exact and the physics unchanged -- is
    untouched.
    """
    if len(combos) == 1 and combos[0] == (1.0, 1.0):
        return [_solve_beam_case(x, dx, w_self, width_ft, coords,
                                 dynamic_weights, L, advanced_loading,
                                 geom=geom)]
    Md, Vd, Ml, Vl = _solve_beam_case_split(
        x, dx, w_self, width_ft, coords, dynamic_weights, L, advanced_loading,
        geom=geom)
    return [(gd * Md + gl * Ml, gd * Vd + gl * Vl) for gd, gl in combos]


def _variance_coord_sets(coords, variance_ft, L):
    """
    Perturbed seating patterns for the position-variance envelope.

    Real paddlers drift off the optimized station.  Rather than the full
    2^n corner enumeration, four deterministic worst-case patterns govern
    the beam response: everyone shifted bow-ward, stern-ward, packed toward
    midship (max sagging), and spread toward the ends (max hogging).
    """
    base = np.asarray(coords, dtype=float)
    v = float(variance_ft)
    mid = L / 2.0
    toward = base + np.where(base < mid, v, -v)
    away   = base - np.where(base < mid, v, -v)
    return [np.clip(c, 0.0, L) for c in (base + v, base - v, toward, away)]


def _append_rocker_overhang(x, dx, w_self, width_ft, rocker_ft):
    """
    Append the bow/stern STEM OVERHANGS (tip cantilevers) to the 1-D beam arrays.

    The overhang is the run at each end too narrow to host a shell section.  It
    is real dead weight cantilevered beyond the buoyant hull, yet the offset-
    file stations stop at the last section with area, so the moment envelope
    never sees it.  Physically it is a wedge of shell tapering from the endmost
    section down to a point; it sits above the water, so it carries mass but
    generates NO buoyancy.

    THIS IS NOT THE SAME GEOMETRY AS THE CAD STEM SWEEP, and earlier comments
    claiming the two "match" were wrong.  Over the same length:

        CAD export  sweeps EXISTING material UPWARD, INSIDE the hull -- the keel
                    line rises to the sheer on a quarter-ellipse and the LOA is
                    unchanged (geometry._stem_rise / _stem_rocker_specs).
        this model  APPENDS new stations OUTSIDE the hull, lengthening the beam
                    by one overhang at each end and adding mass there.

    That is a deliberate modelling choice, not an equivalence.  Structurally
    what matters is that the tip material is unsupported, and putting it on a
    slightly longer lever arm bounds the real case rather than understating it.
    The two share only the LENGTH; do not read across any other property.

    A CONSEQUENCE WORTH KNOWING: because the tip mass is un-buoyed, it raises
    hogging at the ends, and end hogging OPPOSES the crew's midship sagging.  On
    this hull sagging governs, so a LONGER overhang LOWERS the governing D/C.
    The overhang must therefore be a truthful geometric measurement, never a
    tuned parameter -- see geometry.stem_overhang_ft.

    We model it as ``n = round(rocker_ft/dx)`` extra stations at each end (same
    uniform spacing, so the paddler-deposit node weights stay exact), with the
    self-weight line load tapering linearly from an END ANCHOR down to zero at
    the tip and the waterplane width set to zero (no buoyancy).  Adding un-buoyed
    tip mass forces the midship buoyancy to carry it, which drives the ends down
    and the middle up -- i.e. it INCREASES the hogging moment, exactly the
    cantilever effect the rocker has in reality.

    QUANTISATION -- the overhang acts as a STEP FUNCTION, not a continuous one.
    Rounding to whole stations means the applied length is a multiple of dx, and
    the linear taper ``w0*(1 - k/n)`` is IDENTICALLY ZERO at n = 1: a single
    appended station carries no mass at all, so the beam grid is extended for
    nothing.  Any overhang shorter than ~1.5*dx therefore has exactly the same
    structural effect as no overhang.

    On the live hull at 161 stations dx is about 1.50 in, and the true stem
    overhang is about 1.05 in -- so it rounds to n = 1 and contributes ZERO.
    That is defensible on the merits: the real tip mass there is roughly 0.1 lb,
    below what a 1.5 in grid can resolve, and neglecting it is conservative
    because tip mass RELIEVES the midship sagging that governs.  But it is a
    consequence of the discretisation rather than a modelling decision, and it
    means geometry.stem_overhang_ft returning a continuous value does not make
    the RESPONSE continuous.  Left as-is deliberately; documented so nobody
    later reads a smooth overhang-vs-D/C curve into this model.  A finer
    station grid moves the threshold, not the physics.

    ANCHOR: the offset file tapers the outermost section to (near-)zero area, so
    ``w_self`` at the very end station is ~0 and would make the overhang massless.
    The rocker instead sweeps the near-tip hull UP into the stem, so the overhang
    carries the shell material of that region.  We anchor the taper to the
    heaviest shell line load within one rocker-length of the tip -- a real,
    non-degenerate section scale that grows with the rocker length.

    Returns ``(x_ext, w_ext, width_ext, i0, i1)`` where ``[i0:i1]`` is the slice
    of the ORIGINAL stations inside the extended arrays.  A non-positive rocker
    (or one below half a station) returns the inputs unchanged.
    """
    x = np.asarray(x, float); w_self = np.asarray(w_self, float)
    width_ft = np.asarray(width_ft, float)
    n = int(round(float(rocker_ft) / dx)) if dx > 0 else 0
    if rocker_ft <= 1e-9 or len(x) < 2:
        return x, w_self, width_ft, 0, len(x)
    # A real overhang shorter than half a station used to round to zero and
    # vanish from the model entirely -- so the SAME hull carried tip mass at
    # 161 stations and none at 41.  Represent it on at least one station and
    # let the rescaling below carry the correct geometric mass; a coarse grid
    # then distributes it crudely rather than deleting it.
    n = max(n, 1)
    k = np.arange(1, n + 1, dtype=float)          # 1 .. n (1 = nearest the hull)
    m = min(n + 1, len(w_self))
    w0 = float(np.max(w_self[:m]))                 # bow anchor (near-tip section)
    wL = float(np.max(w_self[-m:]))                # stern anchor
    # Bow: stations OUTBOARD of x[0], ordered tip -> hull; taper 0 (tip) -> w0.
    x_bow = x[0] - dx * k[::-1]
    w_bow = w0 * (1.0 - k[::-1] / n)
    # Stern: stations outboard of x[-1], ordered hull -> tip; taper wL -> 0.
    x_stern = x[-1] + dx * k
    w_stern = wL * (1.0 - k / n)

    # ── TIP MASS IS GEOMETRY, NOT GRID ────────────────────────────────────
    # The raw taper carries w0*dx*(n-1)/2 per end: IDENTICALLY ZERO at n = 1
    # and only approaching the physical w0*rocker/2 as n grows.  So the same
    # hull gained tip mass between 321 and 641 stations purely because
    # round(rocker/dx) crossed a threshold -- a physical quantity changing
    # with a numerical parameter, and in the relieving direction, which
    # flattered every drift measurement whose last point was 641.
    #
    # The target is the triangle the model intends: zero at the tip rising to
    # the near-tip section line load over the rocker length.  The discrete
    # taper is rescaled to carry exactly that at ANY n.
    for _w, _anchor in ((w_bow, w0), (w_stern, wL)):
        target = 0.5 * float(_anchor) * float(rocker_ft)     # lb, per end
        got = float(np.sum(_w)) * dx                          # rectangle rule
        if target <= 0.0:
            continue
        if got > 1e-12:
            _w *= target / got
        else:
            # n == 1: the taper is identically zero and cannot be scaled.
            # Deposit the whole physical tip mass on the one appended station.
            _w[:] = target / dx
    # NO WARNING HERE.  This runs once per configuration per load case -- one
    # 35-config sweep emitted it 143,479 times, which is not a warning, it is
    # a way to bury one.  The coarse-distribution note belongs to the run, not
    # the evaluation, so run_grand_optimization states it once in the overhang
    # banner where a person will actually read it.
    zeros = np.zeros(n)
    x_ext = np.concatenate([x_bow, x, x_stern])
    w_ext = np.concatenate([w_bow, w_self, w_stern])
    width_ext = np.concatenate([zeros, width_ft, zeros])   # overhang: no buoyancy
    return x_ext, w_ext, width_ext, n, n + len(x)


def overhang_stations(rocker_ft, dx_ft):
    """How many stations the overhang model appends, and whether they carry mass.

    Returns ``(n, carries_mass)``.

    HISTORY, because the answer changed.  The taper used to be ``w0*(1 - k/n)``
    unscaled, which is identically zero at n = 1 and only reached the physical
    tip mass as n grew — so the same hull was massless at 321 stations and
    carried mass at 641, a physical quantity moving with a numerical parameter.
    ``_append_rocker_overhang`` now sets the tip mass from GEOMETRY
    (``w0*rocker/2`` per end) and rescales the discrete taper to carry exactly
    that at any n >= 1, and it no longer lets a real overhang round away to
    nothing.  So a non-zero rocker now always carries its mass, and what varies
    with the grid is only how coarsely that mass is distributed.
    """
    n = int(round(float(rocker_ft) / float(dx_ft))) if dx_ft > 0 else 0
    if float(rocker_ft) > 1e-9:
        n = max(n, 1)
    return n, n >= 1 and float(rocker_ft) > 1e-9


def kupfer_gate(kupfer, worst_util, limit=None):
    """
    THE strength gate.  One function, called by the sweep and by the winner
    re-gate, so a reported utilisation and a gated utilisation can never be two
    different numbers again -- which is the shape of every gate bug this project
    has had.

    Returns ``(infeasible, util_max, limit)``.  ``kupfer`` is the biaxial result
    dict (None on the legacy path, where ``worst_util`` is gated instead).
    """
    lim = float(kupfer_search_limit() if limit is None else limit)
    if kupfer is not None:
        u = float(kupfer["util_max"])
    else:
        u = float(np.max(worst_util)) if np.size(worst_util) else 0.0
    return bool(u > lim), u, lim


class _RejectedWeight:
    """
    The value an INFEASIBLE evaluation reports where a weight would go.

    Not a number.  Every arithmetic and ordering operation raises TypeError
    naming the mistake, so a rejected design cannot be silently folded into a
    sum, a delta or a comparison.

    WHY IT IS A TYPE AND NOT 1e6.  The old sentinel was a float, so a rejected
    evaluation was indistinguishable from a very heavy hull to anything that
    did arithmetic on it.  That produced, at different times: a top-flange
    "stability gain" of +4.71 in (a 1e6 lb hull makes the crew negligible), a
    reported hull weight delta of -999842.82 lb, and a loaded-freeboard check
    that "correctly" concluded a million-pound canoe swamps.  Each was patched
    where it surfaced; the sentinel kept finding new consumers.  Making it a
    non-number ends the class: the five call sites that used to read it now
    fail loudly at the point of misuse instead of returning a plausible lie.

    Truthiness is False and equality with itself works, so ``if not weight``
    and ``weight is REJECTED`` remain the natural guards.

    THE ONE LEGITIMATE CONVERSION -- optimizer objectives.
    ------------------------------------------------------
    An OPTIMIZER needs an orderable number: SciPy's SLSQP does ``if fx <
    self._lowest_f`` internally to descend, so a non-number raises inside the
    solver and kills the run.  An objective function is therefore allowed --
    required -- to turn a rejection into :data:`INFEASIBLE_PENALTY`, a large
    finite scalar, AT THE SCIPY BOUNDARY and nowhere else.  Do it explicitly at
    the call site (``w = INFEASIBLE_PENALTY if is_rejected(w) else float(w)``)
    so the conversion is visible rather than implied.

    Everything that REPORTS, STORES or DISPLAYS a weight keeps REJECTED.  The
    distinction is the whole design: a solver may rank an infeasible candidate
    last, but no report may ever print it as a mass.
    """
    __slots__ = ()

    def _fail(self, op):
        raise TypeError(
            f"cannot {op} a REJECTED design's weight: this evaluation was "
            f"infeasible and has no mass. Check `is REJECTED` (or the "
            f"evaluation's 'feasible' flag) before using the weight.")

    def __bool__(self):                 return False
    def __float__(self):                self._fail("convert to float")
    def __int__(self):                  self._fail("convert to int")
    def __add__(self, o):               self._fail("add to")
    __radd__ = __add__
    def __sub__(self, o):               self._fail("subtract from")
    __rsub__ = __sub__
    def __mul__(self, o):               self._fail("multiply")
    __rmul__ = __mul__
    def __truediv__(self, o):           self._fail("divide")
    __rtruediv__ = __truediv__
    def __lt__(self, o):                self._fail("order")
    __le__ = __gt__ = __ge__ = __lt__
    def __repr__(self):                 return "REJECTED"


#: Singleton rejection marker -- see :class:`_RejectedWeight`.
REJECTED = _RejectedWeight()

#: The finite stand-in an OPTIMIZER OBJECTIVE substitutes for REJECTED at the
#: SciPy boundary.  Large enough that no feasible hull can reach it, finite so
#: SLSQP can order candidates and descend.  This is the only sanctioned
#: conversion of a rejection into a number -- see :class:`_RejectedWeight`.
INFEASIBLE_PENALTY = 1e6


def as_optimizer_penalty(weight):
    """
    Evaluation result -> orderable scalar, for an optimizer objective ONLY.

    SLSQP compares candidate objective values internally, so it needs a number;
    handing it REJECTED raises inside SciPy and kills the worker.  Call this at
    the boundary where a value is passed to a solver, never on a value headed
    for a report, a result dict or a stored design.
    """
    return INFEASIBLE_PENALTY if is_rejected(weight) else float(weight)


def is_rejected(weight):
    """True when ``weight`` is the rejection marker (or a legacy 1e6 float)."""
    if weight is REJECTED:
        return True
    try:
        return bool(np.isnan(weight)) or float(weight) >= 1e5
    except (TypeError, ValueError):
        return True


def _crew_seat_count(crew_cases):
    """Total seats across a crew-case list -- where the mix variables begin."""
    return sum(len(row[1]) for row in crew_cases)


def distinct_arrangements(weights, pins=None):
    """
    The DISTINCT orderings of one crew's weights, bow to stern.

    Paddlers may sit in any order, but the sweep pairs weight ``i`` with seat
    ``i``, so only one ordering is ever analysed unless the envelope covers them
    all.  Duplicates are collapsed: four equal paddlers give one arrangement,
    not 24.

    Different orderings govern sagging and hogging, so no single ordering is
    conservative for both and the envelope has to cover them.  Measured on a
    real design at the shipped weights, enumerating raises the envelope by ~10%
    in sagging and ~67% in hogging.

    ``pins`` is one entry per PADDLER: the 1-based seat that paddler must
    occupy, or ``None`` for "any".  Pinning shrinks the returned set, which
    makes the resulting envelope LESS CONSERVATIVE -- it is only valid for
    arrangements consistent with the pins.  Callers must disclose that; see
    ``arrangement_disclosure``.
    """
    w = [float(q) for q in weights]
    n = len(w)
    if not pins or all(p is None for p in pins):
        return sorted(set(itertools.permutations(w)))
    if len(pins) != n:
        raise ValueError(f"{len(pins)} seat pins for {n} paddlers")
    fixed = {}
    for i, p in enumerate(pins):
        if p is None:
            continue
        s = int(p) - 1                       # 1-based seat -> index
        if not (0 <= s < n):
            raise ValueError(f"paddler {i + 1} pinned to seat {p}, outside 1..{n}")
        if s in fixed:
            raise ValueError(f"seat {p} pinned to more than one paddler")
        fixed[s] = w[i]
    free_w = [w[i] for i, p in enumerate(pins) if p is None]
    free_seats = [s for s in range(n) if s not in fixed]
    out = set()
    for perm in itertools.permutations(free_w):
        row = [None] * n
        for s, val in fixed.items():
            row[s] = val
        for s, val in zip(free_seats, perm):
            row[s] = val
        out.add(tuple(row))
    return sorted(out)


def arrangement_disclosure(weights, pins=None):
    """
    What a pinned enumeration actually covers, for the compliance note.

    Returns ``{n_evaluated, n_possible, pinned_seats, fully_enumerated}``.
    ``n_possible`` is the UNPINNED count -- the number of orderings the crew
    could physically produce on race day -- so the report can state plainly how
    much of that space the analysis covers.  A design enumerated over a subset
    is valid ONLY for that subset.
    """
    n_possible = len(distinct_arrangements(weights, None))
    n_eval = len(distinct_arrangements(weights, pins))
    pinned = ({} if not pins else
              {int(p): i + 1 for i, p in enumerate(pins) if p is not None})
    return {"n_evaluated": n_eval, "n_possible": n_possible,
            "pinned_seats": pinned,
            "fully_enumerated": n_eval == n_possible}


def calculate_moment_envelope(x, dx, w_self, width_ft, paddler_coords_4=None,
                              paddler_coords_2=None,
                              return_all=False, paddler_coords_3=None,
                              position_variance_ft=0.0, rocker_ft=0.0,
                              crew_cases=None, enumerate_arrangements=None):
    """
    Calculates absolute maximum moment and dual-sign shear envelopes
    across all race conditions with force balancing for numerical stability.

    paddler_coords_3     : optional 3-person race seating [ft]; the 3-person
                           case is included whenever this is provided.
    position_variance_ft : when > 0, the envelopes additionally cover
                           perturbed seatings (see _variance_coord_sets);
                           the per-case curves stay at the nominal positions.
    rocker_ft            : bow/stern stem-rocker overhang length [ft].  When
                           > 0 the beam is extended with un-buoyed cantilever
                           tip mass (see _append_rocker_overhang), raising the
                           hogging moment; the returned envelopes are still on
                           the original station grid.
    """
    num_points = len(x)
    # Signed envelopes.  Sign convention (see _solve_beam_case: M = ∬q_net
    # with net-UPWARD load positive):  M > 0 = SAGGING (bottom fiber in
    # tension), M < 0 = HOGGING (top fiber / gunwales in tension).  Both
    # envelopes are stored as positive magnitudes; gov_* holds the index of
    # the race case that produced the extreme at each station (-1 = none).
    M_sag = np.zeros(num_points)
    M_hog = np.zeros(num_points)
    gov_sag = np.full(num_points, -1, dtype=int)
    gov_hog = np.full(num_points, -1, dtype=int)
    V_env_pos = np.zeros(num_points)
    V_env_neg = np.zeros(num_points)
    L = np.max(x)                        # REAL hull length (paddlers live in [0,L])

    # Stem-rocker overhangs: extend the beam with un-buoyed cantilever tip mass
    # (see _append_rocker_overhang).  The solve runs on the EXTENDED grid; every
    # per-station output is sliced back to [i0:i1] so it aligns 1:1 with the
    # caller's section-property arrays and only real stations are stress-checked.
    xE, wE, widthE, i0, i1 = _append_rocker_overhang(x, dx, w_self, width_ft, rocker_ft)
    # The beam-solve constants for THIS envelope's grid, computed once and
    # handed to every solve below.  One envelope call drives ~68 solves and
    # each one used to rebuild these from scratch.
    _geomE = _beam_geom(xE, widthE)
    _sl = slice(i0, i1)

    if crew_cases is not None:
        # Accepts (name, coords, weights) or (name, coords, weights, pins).
        cases = []
        for _row in crew_cases:
            _n, _c, _ws = _row[0], _row[1], _row[2]
            _pn = _row[3] if len(_row) > 3 else None
            cases.append((str(_n), _c, [float(q) for q in _ws], _pn))
    else:
        # Legacy three-case form, kept so existing callers and tests are
        # unaffected.  Each case still carries its OWN weights -- never a
        # shared list -- so no caller can apply one crew's weights to another's
        # seats.
        cases = [
            ("race_4", paddler_coords_4, list(config.RACE_4_PERSON), None),
            ("race_2", paddler_coords_2, list(config.RACE_2_PERSON), None),
        ]
        if paddler_coords_3 is not None and getattr(config, "RACE_3_PERSON", None):
            cases.append(("race_3", paddler_coords_3,
                          list(config.RACE_3_PERSON), None))

    for _n, _c, _ws, _pn in cases:
        if len(_c) != len(_ws):
            raise ValueError(
                f"crew case {_n!r} has {len(_c)} seats but {len(_ws)} weights "
                f"-- a seat/weight mismatch would silently load the wrong crew")

    all_M, all_V = {}, {}
    case_names = [row[0] for row in cases]
    # Arrangement enumeration: on unless explicitly disabled.  Cost lands here,
    # on the envelope, not on the sweep's outer loops.
    _enum_arr = (bool(getattr(config, "ENUMERATE_ARRANGEMENTS", True))
                 if enumerate_arrangements is None
                 else bool(enumerate_arrangements))
    n_arr = {}
    # Per-case disclosure of WHAT WAS COVERED: how many orderings were
    # evaluated out of how many the crew could physically produce, and which
    # seats were pinned.  A pinned run is valid only for the pinned subset, so
    # this travels with the result rather than being reconstructed downstream.
    arr_disclosure = {}

    def _absorb(M, V, ci):
        nonlocal V_env_pos, V_env_neg
        M, V = M[_sl], V[_sl]                 # extended grid -> real stations
        pos, neg = np.maximum(M, 0.0), np.maximum(-M, 0.0)
        upd = pos > M_sag
        M_sag[upd], gov_sag[upd] = pos[upd], ci
        upd = neg > M_hog
        M_hog[upd], gov_hog[upd] = neg[upd], ci
        V_env_pos = np.maximum(V_env_pos, V)
        V_env_neg = np.minimum(V_env_neg, V)

    # Paddler load models to envelope over.  NEITHER model bounds the other:
    # splitting weight to the toes lowers the sagging moment and peak shear but
    # RAISES the hogging moment, and which effect governs depends on the hull
    # and the seating.  Designing to whichever single model happens to be
    # selected therefore leaves the other one unchecked.  With the envelope on,
    # every station is designed to the worst of both, so the result is a bound
    # regardless of how the measured knee/toe split is later revised.
    if getattr(config, "PADDLER_LOAD_ENVELOPE", False):
        load_models = (False, True)      # point load, measured knee/toe split
    else:
        load_models = (None,)            # legacy: whatever the global says

    # Per-INSTANCE signed (M, V) curves — one per (race case x load model x
    # seating), NOT the envelope.  The Kupfer combined-stress gate needs the
    # actual co-existing M and V of each load state, because peak bending and
    # peak shear occur in different cases, at different instants and fibres;
    # enveloping the components first and combining would be non-conservative
    # in the wrong direction.  Each entry: (case_index, M[nst], V[nst]).
    instances = []

    # Load combinations, per the active design methodology.  ASD gives the
    # single nominal (1, 1) pair, so every array below is bit-identical to the
    # pre-LRFD code and the whole refactor is a no-op on that path.  LRFD gives
    # the factored pairs, including the counteracting-dead alternative.
    _combos_all = {}
    # Unfactored dead and live moment curves, per case, kept for the report so
    # it can state WHERE the dead load relieves the live one rather than
    # asserting it.  LRFD only: under ASD there are no load factors, so the
    # counteracting-dead question does not arise, and skipping it there keeps
    # the ASD path on its un-split, bit-identical fast route.
    _split_M = {}
    _want_split = design_method() == DESIGN_METHOD_LRFD

    for ci, (name, coords, static_weights, seat_pins) in enumerate(cases):
        # DYNAMIC_LOAD_FACTOR is an IMPACT amplification -- it turns static crew
        # weight into the load the hull actually sees while being paddled.  That
        # is a load characterisation, not a safety factor, so it stays inside
        # the live load and gamma_live multiplies it (effective 1.2*1.6 = 1.92
        # on nominal crew weight under the default LRFD factors).  The report
        # states both numbers so the stack-up is never implicit.
        dynamic_weights = [w * config.DYNAMIC_LOAD_FACTOR for w in static_weights]
        combos = load_combinations(name)
        _combos_all[name] = combos

        # Seating ARRANGEMENTS.  The crew may sit in any order and the sweep
        # analyses only the order it was handed, so every distinct ordering is
        # enveloped -- a per-station max, not a pick-the-worst, because sag and
        # hog are governed by DIFFERENT orderings.  The nominal (as-configured)
        # ordering is evaluated FIRST so the per-case reporting curves below
        # describe the configured crew rather than an arbitrary permutation.
        if _enum_arr:
            _cand = distinct_arrangements(dynamic_weights, seat_pins)
            # The nominal ordering leads ONLY when the pins admit it; a pinned
            # run must not smuggle in an arrangement the pins exclude.
            _nom = tuple(dynamic_weights)
            _arrs = ([list(_nom)] if _nom in _cand else [])
            _arrs += [list(a) for a in _cand if a != _nom]
        else:
            _arrs = [dynamic_weights]
        n_arr[name] = len(_arrs)
        arr_disclosure[name] = arrangement_disclosure(
            static_weights, seat_pins if _enum_arr else
            list(range(1, len(static_weights) + 1)))

        per_model = []
        for ai, w_arr in enumerate(_arrs):
            for adv in load_models:
                for Mm, Vm in _combined_cases(xE, dx, wE, widthE, coords,
                                              w_arr, L, adv, combos,
                                              geom=_geomE):
                    if ai == 0:
                        per_model.append((Mm, Vm))
                    _absorb(Mm, Vm, ci)
                    instances.append((ci, Mm[_sl], Vm[_sl]))

        if _want_split:
            # Nominal seating, first load model: enough for the report to show
            # the sign relationship between the two components.
            _Md, _, _Ml, _ = _solve_beam_case_split(
                xE, dx, wE, widthE, coords, dynamic_weights, L,
                advanced_loading=load_models[0], geom=_geomE)
            _split_M[name] = (_Md[_sl], _Ml[_sl])

        # Per-case curves kept for reporting: take the signed value with the
        # larger magnitude at each station so the stored curve is itself the
        # envelope, not an arbitrary one of the two models.  Slice back to the
        # real stations so the stored curves match the section-property grid.
        M = per_model[0][0]
        V = per_model[0][1]
        for Mm, Vm in per_model[1:]:
            M = np.where(np.abs(Mm) > np.abs(M), Mm, M)
            V = np.where(np.abs(Vm) > np.abs(V), Vm, V)
        all_M[name], all_V[name] = M[_sl], V[_sl]

        if position_variance_ft > 1e-9:
            for coords_p in _variance_coord_sets(coords, position_variance_ft, L):
                for adv in load_models:
                    for Mp, Vp in _combined_cases(xE, dx, wE, widthE, coords_p,
                                                  dynamic_weights, L, adv,
                                                  combos, geom=_geomE):
                        _absorb(Mp, Vp, ci)  # perturbations belong to their case
                        instances.append((ci, Mp[_sl], Vp[_sl]))

    M_envelope = np.maximum(M_sag, M_hog)   # legacy absolute envelope

    # Per-case curves are keyed by CASE NAME, whatever the names are.  The
    # legacy race_4/race_2/race_3 keys are additionally filled -- from the
    # largest crew of each size when the five ASCE categories are in play -- so
    # older plotting/report call sites keep working without being handed a
    # curve from a crew they did not ask for.
    def _legacy(dsrc):
        out = {}
        for size, key in ((4, "race_4"), (2, "race_2"), (3, "race_3")):
            if key in dsrc:
                out[key] = dsrc[key]
                continue
            same = [(r[0], r[2]) for r in cases if len(r[2]) == size]
            if same:
                heaviest = max(same, key=lambda t: sum(t[1]))[0]
                out[key] = dsrc.get(heaviest)
            else:
                out[key] = None
        return out

    res = {
        "moment": {"envelope": M_envelope,
                   "sag": M_sag, "hog": M_hog,
                   "gov_sag": gov_sag, "gov_hog": gov_hog,
                   "by_case": dict(all_M),
                   **_legacy(all_M)},
        "shear":  {"max_pos": V_env_pos, "max_neg": V_env_neg,
                   "by_case": dict(all_V),
                   **_legacy(all_V)},
        "cases": case_names,
        # How many distinct seat orderings each case was enveloped over (1 =
        # enumeration off, or a crew whose paddlers all weigh the same).
        "arrangements": dict(n_arr),
        # Per case: evaluated vs physically possible orderings, and which seats
        # were pinned.  When fully_enumerated is False the design is valid ONLY
        # for arrangements consistent with those pins -- a binding limitation,
        # carried with the result so the report cannot omit it.
        "arrangement_disclosure": dict(arr_disclosure),
        "instances": instances,          # per (case x model x seating x combo) signed M,V
        # What was actually applied, for the report -- never re-derived there.
        # The dead/live curves are LRFD-only (see _want_split above).
        "design": design_summary(
            case_names,
            m_dead={k: v[0] for k, v in _split_M.items()},
            m_live={k: v[1] for k, v in _split_M.items()}),
    }

    if return_all:
        return res
    return M_envelope


def _cum_at(cum, z_grid_in, z_q):
    """
    Per-station value of a cumulative table (area / first moment / second
    moment) at height(s) z_q [in above keel], by row-wise linear
    interpolation.  z_q may be a scalar (broadcast to all stations) or a
    per-station array.
    """
    zg = np.asarray(z_grid_in, dtype=float)
    z_q = np.asarray(z_q, dtype=float)
    if z_q.ndim == 0:
        z_q = np.full(cum.shape[0], float(z_q))
    k = np.clip(np.searchsorted(zg, z_q), 1, len(zg) - 1)
    z0, z1 = zg[k - 1], zg[k]
    f = np.clip((z_q - z0) / np.maximum(z1 - z0, 1e-12), 0.0, 1.0)
    rows = np.arange(cum.shape[0])
    return cum[rows, k - 1] + f * (cum[rows, k] - cum[rows, k - 1])


def _area_below(cum_area_in2, z_grid_in, z_q):
    """Per-station shell area [in²] below height z_q (fix plan C3 weight
    split).  Thin wrapper kept for the existing 2-zone evaluator path."""
    return _cum_at(cum_area_in2, z_grid_in, z_q)


def aci_elastic_modulus(density_pcf, fc_psi):
    """
    ACI 318 elastic modulus for (lightweight) concrete [psi]:

        E = 33 · w^1.5 · √(f'c)          (w in pcf, f'c in psi)

    The concrete-canoe mixes span ~40–90 pcf, exactly the lightweight range
    this empirical relation was calibrated for.  Derived from the density
    and compressive strength already in the mix database, so no new material
    data is required for the composite (transformed-section) analysis.
    """
    w = np.asarray(density_pcf, dtype=float)
    fc = np.maximum(np.asarray(fc_psi, dtype=float), 1.0)
    return 33.0 * w ** 1.5 * np.sqrt(fc)


def composite_section_properties(z_bounds, moduli,
                                 cum_area, cum_Az, cum_Iz, z_grid,
                                 gw_EA=None, gw_EAz=None, gw_EIz=None):
    """
    Transformed-section properties of a shell divided into N horizontal pour
    zones, per station (multi-zone I-beam strategy).

    The section is a thin U-shaped shell whose area is distributed over
    height; a horizontal zone [z_a, z_b] owns the shell perimeter area
    between those heights.  Each zone is made of a different mix, so the
    section is a COMPOSITE: areas are weighted by the modular ratio
    n_i = E_i / E_ref (transformed-section method) before finding the
    neutral axis and moment of inertia.

    Parameters
    ----------
    z_bounds : list of N+1 zone-boundary heights [in above keel], ascending,
               bottom→top.  z_bounds[0] = keel (0), z_bounds[-1] = sheer.
               Each entry is a scalar (constant elevation across the canoe)
               or a per-station array.
    moduli   : (N,) elastic moduli E_i [psi], one per zone, bottom→top.
    cum_area, cum_Az, cum_Iz : (n_st × n_z) cumulative area / first moment /
               second moment of shell area about the keel baseline (z = 0),
               from geometry.calculate_shell_properties.
    z_grid   : (n_z,) heights the cumulative tables are sampled at.

    Returns
    -------
    (y_bar, I_trans, c_top, c_bot), each a per-station array.
    I_trans is in units of the reference modulus E_ref = moduli[0].  The
    bending stress in zone i is  σ_i = (E_i/E_ref)·M·(z − y_bar)/I_trans;
    with all moduli equal this reduces exactly to the homogeneous M·c/I.
    """
    n_st = cum_area.shape[0]
    moduli = np.asarray(moduli, dtype=float)
    E_ref = float(moduli[0])

    EA = np.zeros(n_st)          # Σ n_i A_i
    EAz = np.zeros(n_st)         # Σ n_i (∫z dA)_i   about baseline
    EIz = np.zeros(n_st)         # Σ n_i (∫z² dA + I_own)_i  about baseline

    prev_A  = _cum_at(cum_area, z_grid, z_bounds[0])
    prev_Az = _cum_at(cum_Az,   z_grid, z_bounds[0])
    prev_Iz = _cum_at(cum_Iz,   z_grid, z_bounds[0])
    for i in range(len(moduli)):
        A_t  = _cum_at(cum_area, z_grid, z_bounds[i + 1])
        Az_t = _cum_at(cum_Az,   z_grid, z_bounds[i + 1])
        Iz_t = _cum_at(cum_Iz,   z_grid, z_bounds[i + 1])
        n_i = moduli[i] / E_ref
        EA  += n_i * (A_t  - prev_A)
        EAz += n_i * (Az_t - prev_Az)
        EIz += n_i * (Iz_t - prev_Iz)
        prev_A, prev_Az, prev_Iz = A_t, Az_t, Iz_t

    # Optional gunwale rail — already transformed by its modular ratio and
    # expressed about the keel baseline (first / second moments), so it folds
    # straight into the running transformed sums.  z_bounds[-1] (the sheer)
    # stays the top fibre: the rail sits on the sheer, so c_top is unchanged.
    if gw_EA is not None:
        EA  = EA  + gw_EA
        EAz = EAz + gw_EAz
        EIz = EIz + gw_EIz

    EA = np.maximum(EA, 1e-9)
    y_bar = EAz / EA
    I_trans = np.maximum(EIz - EA * y_bar ** 2, 1e-9)   # parallel-axis shift

    z_top  = np.broadcast_to(np.asarray(z_bounds[-1], dtype=float), (n_st,))
    z_keel = np.broadcast_to(np.asarray(z_bounds[0],  dtype=float), (n_st,))
    c_top = np.maximum(z_top - y_bar, 1e-9)
    c_bot = np.maximum(y_bar - z_keel, 1e-9)
    return y_bar, I_trans, c_top, c_bot


#: Per-station gunwale rail section, heights ABOVE THE KEEL (so any datum can
#: consume it).  ``A_gw``/``I_gw_own`` are the two rail strips; ``A_fil``/
#: ``I_fil_own`` the two fillet spandrels; ``gw_cent``/``fil_cent`` their
#: centroid heights; ``Kt`` the fillet stress-concentration factor.
GunwaleRail = namedtuple("GunwaleRail",
                         "A_gw I_gw_own gw_cent A_fil fil_cent I_fil_own Kt")


def gunwale_rail_section(x, t_in, gw_width_mid, gw_height_mid, gw_fillet_ratio,
                         sheer_in):
    """
    Per-station gunwale rail section — the ONE geometry shared by the
    single-zone (``evaluate_canoe_weight``) and multi-zone
    (``evaluate_multizone_weight``) evaluators, so the two can never drift.

    The rail's top face sits on the sheer and extrudes downward; width and
    height taper from the midship values (gw_*_mid) to the wall thickness at
    the tips via a raised cosine, so the rail vanishes continuously into the
    shell at bow and stern.  Two strips (port + starboard) plus a fillet
    spandrel at each lower inner corner (radius t·gw_fillet_ratio).

    All returned heights are measured ABOVE THE KEEL.  Returns ``None`` when no
    gunwale is requested (width or height ≤ 0).
    """
    if not (gw_width_mid > 0.0 and gw_height_mid > 0.0):
        return None
    x = np.asarray(x, dtype=float)
    sheer = np.asarray(sheer_in, dtype=float)
    _L = np.max(x)
    taper = (1.0 - np.cos(2.0 * np.pi * x / _L)) / 2.0    # 1 midship, 0 at tips
    local_width  = t_in + (gw_width_mid  - t_in) * taper
    local_height = t_in + (gw_height_mid - t_in) * taper

    A_gw     = 2.0 * local_width * local_height           # two strips
    I_gw_own = A_gw * local_height ** 2 / 12.0
    gw_cent  = sheer - local_height / 2.0                 # rail centroid ↑ keel

    r = t_in * gw_fillet_ratio
    if r > 1e-6:
        _pi_4      = np.pi / 4.0
        A_fil_side = r ** 2 * (1.0 - _pi_4)               # one spandrel
        A_fil      = np.full_like(A_gw, 2.0 * A_fil_side)
        y_fil      = r / (6.0 * (1.0 - _pi_4))            # above junction corner
        fil_cent   = (sheer - local_height) + y_fil       # ↑ keel
        I_fil_corner = r ** 4 / 3.0 - np.pi * r ** 4 / 16.0
        I_fil_own    = 2.0 * (I_fil_corner - A_fil_side * y_fil ** 2)
    else:
        A_fil    = np.zeros_like(A_gw)
        fil_cent = np.zeros_like(A_gw)
        I_fil_own = 0.0

    Kt = (1.0 + 0.5 / max(gw_fillet_ratio, 1e-6) ** 0.5
          if gw_fillet_ratio > 0.0 else 1.0)
    return GunwaleRail(A_gw, I_gw_own, gw_cent, A_fil, fil_cent, I_fil_own, Kt)


def evaluate_canoe_weight(vars, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in, return_arrays=False,
                          gw_width_mid=0.0, gw_height_mid=0.0,
                          enable_mesh=False, gw_fillet_ratio=1.0,
                          coords_3=None, position_variance_ft=None,
                          cum_area_in2=None, z_grid_in=None, rocker_ft=0.0,
                          cum_Az_in3=None, cum_Iy_in4=None, yhalf_in=None,
                          tally=None, depth_in=None, crew_cases=None,
                          enumerate_arrangements=None):
    """
    Depth-zoned structural evaluator (fix plan C3).

    Every station independently receives, per fiber, the LIGHTEST mix whose
    allowable covers the local demand (bottom fiber ← sagging tension, top
    fiber ← hogging tension), replacing the legacy 8-cell longitudinal
    binning.  A pour transition height z*(x) is then solved from the linear
    bending-stress profile — the heavier mix extends from its extreme fiber
    only as far toward the neutral axis as demand requires — and the weight
    integral splits at z*(x) when the cumulative-area table is provided
    (``cum_area_in2`` / ``z_grid_in`` from calculate_shell_properties);
    without it the split stays at the neutral axis (legacy weights, new
    assignment).

    The transition uses the principal-stress model conservatively: with the
    NA-level shear τ applied through the depth,  p(z) ≤ a  ⇔
    σ(z) ≤ a − τ²/a,  so the hand-off height already accounts for diagonal
    tension in the lighter mix.
    """
    # ``crew_cases`` (all five ASCE races) supersedes the legacy 4/2/3 triple
    # when given; ``vars`` then carries only the mix variables at its tail, and
    # the seating comes from the cases.  The legacy slices stay for callers that
    # still pass a 6-seat vector.
    coords_4 = vars[0:4] if crew_cases is None else None  # LEGACY-SEATING-SLICE-OK
    coords_2 = vars[4:6] if crew_cases is None else None  # LEGACY-SEATING-SLICE-OK
    # coords_3 (kwarg): optional 3-person seating; position_variance_ft=None
    # reads the run-level config so sweep call sites stay unchanged.
    if position_variance_ft is None:
        position_variance_ft = getattr(config, "PADDLER_POS_VARIANCE_FT", 0.0)
    # Mix densities occupy the TAIL of ``vars``, after the seating variables.
    # The boundary is the seating vector's length, which is 6 in the legacy
    # 4+2 layout and 14 with the five ASCE races -- hardcoding 6 made the mix
    # densities read seat COORDINATES once the vector grew, which silently
    # priced a hull out of paddler positions instead of concrete.
    _n_seat = 6 if crew_cases is None else _crew_seat_count(crew_cases)
    densities = np.asarray(vars[_n_seat:])    # 1, 2, or 3 mix densities
    n_mixes   = len(densities)

    # 0-indexed mix list: mixes[0] = heaviest, mixes[-1] = lightest
    mixes = [config.get_mix_properties(float(densities[i])) for i in range(n_mixes)]

    num_points = len(x)

    # ── Per-fiber mix assignment machinery (fix plan C3) ─────────────────────
    order_light_first = np.argsort(densities)        # ascending density

    def _assign(p, allow_lut):
        """Lightest-density mix whose allowable covers demand p at each
        station.  Stations that no mix can carry get the highest-allowable
        mix so the survival check rejects them explicitly."""
        chosen = np.full(len(p), -1, dtype=int)
        for mi in order_light_first:
            ok = (chosen < 0) & (p <= allow_lut[mi])
            chosen[ok] = mi
        chosen[chosen < 0] = int(np.argmax(allow_lut))
        return chosen

    heaviest = int(np.argmax(densities))
    current_layout = np.full((num_points, 2), heaviest, dtype=int)
    z_line = None            # pour hand-off height [in]; None → NA split

    w_self = np.zeros(num_points)

    # ── Gunwale augmentation — longitudinal cosine taper, flat sheer line ────
    # The rail geometry (taper, two strips, fillet spandrels) is the SHARED
    # gunwale_rail_section helper (also used by the multi-zone evaluator).  It
    # returns heights above the keel; here we shift onto this section's neutral
    # axis (subtract c_bot) so the NA-shift bookkeeping below is unchanged.
    _rail = gunwale_rail_section(x, t_in, gw_width_mid, gw_height_mid,
                                 gw_fillet_ratio, c_bot + c_top)
    if _rail is not None:
        A_gw, I_gw_own = _rail.A_gw, _rail.I_gw_own
        A_fil, I_fil_own = _rail.A_fil, _rail.I_fil_own
        gw_cent = _rail.gw_cent - c_bot                   # keel-datum → NA-datum
        fil_cent = _rail.fil_cent - c_bot
        _Kt = _rail.Kt

        A_shell_in2 = A_ft2 * 144.0
        # NA shift: gunwale and fillet centroids are both above the shell NA
        A_total = A_shell_in2 + A_gw + A_fil
        y_bar   = (A_gw * gw_cent + A_fil * fil_cent) / A_total  # upward shift (array)
        d_gw    = gw_cent  - y_bar
        d_fil   = fil_cent - y_bar

        # Sheer line stays at c_top; new c_top = c_top − y_bar (NA moved up)
        _c_top     = c_top - y_bar
        _c_bot     = c_bot + y_bar
        _I         = (I_in4    + A_shell_in2 * y_bar ** 2
                      + I_gw_own + A_gw * d_gw ** 2
                      + I_fil_own + A_fil * d_fil ** 2)
        _A_ft2     = A_total / 144.0
        _A_top_ft2 = (A_top_ft2 * 144.0 + A_gw + A_fil) / 144.0
        _extra_top_in2 = A_gw + A_fil          # gunwale area, always top band
        _Q         = Q_in3 + A_gw * d_gw + A_fil * d_fil   # _Kt from the rail helper
    else:
        _c_top, _c_bot, _I = c_top, c_bot, I_in4
        _A_ft2, _A_top_ft2, _Q = A_ft2, A_top_ft2, Q_in3
        _extra_top_in2 = 0.0
        _Kt = 1.0
    # ── End gunwale augmentation ─────────────────────────────────────────────

    # Iterate layout + pour line to convergence
    dens_lut = np.array([mixes[i]['density'] for i in range(n_mixes)])
    use_cum = cum_area_in2 is not None and z_grid_in is not None
    A_shell_in2_total = A_ft2 * 144.0        # shell only (gunwale separate)

    for iteration in range(10):
        rho_bot = dens_lut[current_layout[:, 0]]
        rho_top = dens_lut[current_layout[:, 1]]
        if use_cum and z_line is not None:
            A_below_in2 = _area_below(cum_area_in2, z_grid_in, z_line)
            A_above_in2 = (A_shell_in2_total - A_below_in2) + _extra_top_in2
            w_self = (A_below_in2 * rho_bot + A_above_in2 * rho_top) / 144.0
        else:
            # Legacy neutral-axis split (also the first-iteration seed)
            w_self = A_bot_ft2 * rho_bot + _A_top_ft2 * rho_top

        struct_data = calculate_moment_envelope(
            x, dx, w_self, width_ft, coords_4, coords_2, return_all=True,
            paddler_coords_3=coords_3,
            position_variance_ft=position_variance_ft,
            rocker_ft=rocker_ft, crew_cases=crew_cases,
            enumerate_arrangements=enumerate_arrangements)
        # ── Fiber-correct signed stress states ──────────────────────────
        # Sign convention: M_sag (＋) puts the BOTTOM fiber in tension,
        # M_hog (－) puts the TOP fiber (gunwales) in tension.  The old code
        # applied the ABSOLUTE envelope to both fibers, which hid which race
        # case / bending sense governs and over-penalised the compression
        # fiber (diagonal tension was computed as if σ were tensile).
        M_sag = struct_data["moment"]["sag"]     # [ft·lb], ≥ 0
        M_hog = struct_data["moment"]["hog"]     # [ft·lb], ≥ 0 magnitude
        # τ from the full |V| envelope (fix plan C4.4).  The code
        # historically used only the POSITIVE shear envelope, which
        # under-counted τ by up to 2× on the stations where negative shear
        # governs (roughly half the hull).  The corrected demand is visible
        # in the utilization plots rather than failing silently.
        V_abs = np.maximum(struct_data["shear"]["max_pos"],
                           -struct_data["shear"]["max_neg"])
        tau_na = V_abs * _Q / (_I * (2.0 * t_in))

        # Signed extreme-fiber bending stresses [psi].  Kt (fillet stress
        # concentration) amplifies the top-fiber stress in both senses.
        sig_bot_sag = (M_sag * 12.0) * _c_bot / _I          # bottom tension
        sig_bot_hog = -(M_hog * 12.0) * _c_bot / _I         # bottom compression
        sig_top_hog = (M_hog * 12.0) * _c_top / _I * _Kt    # top tension
        sig_top_sag = -(M_sag * 12.0) * _c_top / _I * _Kt   # top compression

        # Max principal (tensile) stress with the NA shear:  σ/2 + √((σ/2)²+τ²)
        # (signed σ — a compressive fiber still sees diagonal tension from τ,
        # but far less than the old |σ| formulation charged it with).
        def _p_tension(sig):
            return sig / 2.0 + np.sqrt((sig / 2.0) ** 2 + tau_na ** 2)

        # Tension demand per fiber: worst of its two bending senses
        p_bot = np.maximum(_p_tension(sig_bot_sag), _p_tension(sig_bot_hog))
        p_top = np.maximum(_p_tension(sig_top_hog), _p_tension(sig_top_sag))
        # Compression demand per fiber (magnitude)
        c_dem_bot = -sig_bot_hog        # ≥ 0
        c_dem_top = -sig_top_sag        # ≥ 0

        # --- Per-station, per-fiber assignment (lightest adequate mix) ---
        # When composite mesh is active the top-fiber TENSION allowable is
        # raised to the composite value; compression stays plain concrete.
        # Flexural (bending) allowable: modulus of rupture f_r when available,
        # else the conservative direct-tensile f_t (see strength_flexural).
        str_lut  = np.array([mixes[i].get('strength_flexural', mixes[i]['strength_t'])
                             for i in range(n_mixes)])
        strc_lut = np.array([mixes[i].get('strength_c', 1e9) for i in range(n_mixes)])
        top_lut  = np.maximum(str_lut, 800.0) if enable_mesh else str_lut
        new_layout = np.column_stack([_assign(p_bot, str_lut),
                                      _assign(p_top, top_lut)])

        # --- Pour transition height z*(x) ---
        # Bending stress is linear in the distance from the NA, so the
        # heavier mix is only needed where σ(z) exceeds what the lighter
        # mix can carry:  p(z) ≤ a  ⇔  σ(z) ≤ a − τ²/a  (τ taken at the
        # NA through the depth — conservative).  MESH NOTE: the hand-off
        # uses the lighter mix's PLAIN tensile strength, not the mesh-
        # boosted value — mesh reinforcement lives at the top fiber, not
        # at the pour boundary.
        na_z    = _c_bot                     # NA height above keel [in]
        new_z_line = np.array(na_z, dtype=float, copy=True)  # same-mix: inert

        i_b, i_t = new_layout[:, 0], new_layout[:, 1]
        d_b, d_t = dens_lut[i_b], dens_lut[i_t]
        sig_bot_ext = (M_sag * 12.0) * _c_bot / _I           # bending only
        sig_top_ext = (M_hog * 12.0) * _c_top / _I * _Kt

        m = d_b > d_t          # heavier below (sagging-governed midbody)
        if np.any(m):
            a = str_lut[i_t[m]]
            sig_eff = np.maximum(a - tau_na[m] ** 2 / np.maximum(a, 1e-9), 0.0)
            frac = np.clip(sig_eff / np.maximum(sig_bot_ext[m], 1e-9), 0.0, 1.0)
            new_z_line[m] = na_z[m] * (1.0 - frac)

        m = d_t > d_b          # heavier above (hogging-governed gunwales)
        if np.any(m):
            a = str_lut[i_b[m]]
            sig_eff = np.maximum(a - tau_na[m] ** 2 / np.maximum(a, 1e-9), 0.0)
            frac = np.clip(sig_eff / np.maximum(sig_top_ext[m], 1e-9), 0.0, 1.0)
            new_z_line[m] = na_z[m] + _c_top[m] * frac

        # Survival check — tension per fiber: bottom ← sagging, top ← hogging.
        # Compression demand is NOT gated (it never was): it is exposed as
        # advisory utilization so over-compressed light mixes stay visible
        # without changing sweep feasibility.
        bot_allow_t = str_lut[new_layout[:, 0]]
        top_allow_t = top_lut[new_layout[:, 1]]
        if np.any((p_bot > bot_allow_t) | (p_top > top_allow_t)):
            if tally is not None:
                _dc = float(max(np.max(p_bot / np.maximum(bot_allow_t, 1e-9)),
                                np.max(p_top / np.maximum(top_allow_t, 1e-9))))
                tally.record("longitudinal", {"longitudinal (Rankine)": _dc},
                             t_in=t_in, depth_in=depth_in, mixes=vars[_n_seat:],
                             partial=True,
                             gov="a bending-tension fibre (Rankine pour-line "
                                 "iteration, before the Kupfer gate)")
            return REJECTED   # infeasible: no mass, not a heavy one

        if (np.array_equal(current_layout, new_layout)
                and z_line is not None
                and np.allclose(z_line, new_z_line, atol=0.01)):
            z_line = new_z_line
            break
        current_layout = np.copy(new_layout)
        z_line = new_z_line

    # ── UNIFIED KUPFER BIAXIAL TENSILE GATE (single-zone) ────────────────────
    # Same per-fibre biaxial criterion as the multi-zone path, on the converged
    # bottom/top mix layout (n = 1, homogeneous section).  This is the binding
    # feasibility gate; the in-loop Rankine survival check above is a subset of
    # it (Kupfer >= Rankine) kept only to drive the pour-line iteration.  Needs
    # the lateral section arrays; legacy callers without them keep the Rankine
    # gate.  Single-zone previously had NO transverse (hoop) check at all -- the
    # Kupfer gate adds it here.
    _kupfer_sz = None
    if (cum_area_in2 is not None and cum_Az_in3 is not None
            and cum_Iy_in4 is not None and yhalf_in is not None
            and z_grid_in is not None):
        _sheer = _c_bot + _c_top
        _ones = np.ones(len(np.atleast_1d(_I)))
        _zl = np.clip(np.asarray(z_line, float), 0.0, _sheer)
        _k_lat = (getattr(config, "MANEUVER_SPEED_MPS", 2.5) ** 2
                  / (9.807 * max(getattr(config, "MANEUVER_TURN_RADIUS_M", 4.0), 0.1)))
        _kupfer_sz = kupfer_combined_gate(
            z_grid=z_grid_in, cum_area=cum_area_in2, cum_Az=cum_Az_in3,
            cum_Iy=cum_Iy_in4, yhalf=yhalf_in, sheer=_sheer,
            y_bar=_c_bot, I_tr=_I, t_in=t_in,
            zone_bounds=[np.zeros_like(_zl), _zl, _sheer],
            zone_n=[_ones, _ones],
            zone_ft=[str_lut[current_layout[:, 0]], top_lut[current_layout[:, 1]]],
            zone_fc=[strc_lut[current_layout[:, 0]], strc_lut[current_layout[:, 1]]],
            instances=struct_data["instances"], k_lat=_k_lat,
            Kt=_Kt, rail_lat_Itr=None)
        if _kupfer_sz["util_max"] > kupfer_search_limit():
            if tally is not None:
                tally.record("kupfer_tensile",
                             {"Kupfer biaxial": float(_kupfer_sz["util_max"])},
                             {"longitudinal (Rankine)":
                                 float(_kupfer_sz["rankine_max"])},
                             t_in=t_in, depth_in=depth_in, mixes=vars[_n_seat:],
                             gov=_kupfer_sz.get("gov"))
            return REJECTED

    total_weight = trapezoid(w_self, x)
    if tally is not None:
        # Cleared the stress gates.  The single-zone path applies its freeboard
        # gate afterwards, in a bounded walk over the lightest candidates, so
        # this is not yet a terminal "feasible" — the caller resolves it.
        tally.record(None,
                     {"Kupfer biaxial": (None if _kupfer_sz is None
                                         else float(_kupfer_sz["util_max"]))},
                     {"longitudinal (Rankine)": (None if _kupfer_sz is None
                                                 else float(_kupfer_sz["rankine_max"]))},
                     t_in=t_in, depth_in=depth_in, mixes=vars[_n_seat:],
                     weight=float(total_weight), stage2_pending=True,
                     gov=(None if _kupfer_sz is None else _kupfer_sz.get("gov")))

    if return_arrays:
        mom   = struct_data["moment"]
        shear = struct_data["shear"]
        # Utilization (demand / allowable) per fiber under the FINAL layout.
        # FOS is already inside the allowables (strength / FOS), so 1.0 here
        # means "exactly at the design limit at the configured FOS".
        _tiny = 1e-9
        util_bot_t = p_bot / np.maximum(bot_allow_t, _tiny)
        util_top_t = p_top / np.maximum(top_allow_t, _tiny)
        util_bot_c = c_dem_bot / np.maximum(strc_lut[current_layout[:, 0]], _tiny)
        util_top_c = c_dem_top / np.maximum(strc_lut[current_layout[:, 1]], _tiny)
        # Pour hand-off height for reporting: NaN where one mix spans the
        # full depth (no transition to pour at that station)
        pour_line = np.array(z_line, dtype=float, copy=True)
        pour_line[current_layout[:, 0] == current_layout[:, 1]] = np.nan
        return StructuralResult(
            weight_lbs=total_weight,
            is_feasible=True,
            thickness_in=t_in,
            depth_in=0.0,           # caller fills in from sweep config
            mix_densities=list(densities),
            x_ft=x,
            M_envelope=mom["envelope"],
            M_race_4=mom["race_4"],
            M_race_2=mom["race_2"],
            V_max_pos=shear["max_pos"],
            V_max_neg=shear["max_neg"],
            V_race_4=shear["race_4"],
            V_race_2=shear["race_2"],
            na_line=_c_bot,
            pour_layout=current_layout,
            M_race_3=mom.get("race_3"),
            V_race_3=shear.get("race_3"),
            M_sag_env=mom["sag"],
            M_hog_env=mom["hog"],
            gov_sag=mom["gov_sag"],
            gov_hog=mom["gov_hog"],
            case_names=list(struct_data.get("cases", [])),
            util_bot_t=util_bot_t,
            util_top_t=util_top_t,
            util_bot_c=util_bot_c,
            util_top_c=util_top_c,
            pour_line_in=pour_line,
            sheer_line_in=_c_bot + _c_top,
        )

    return total_weight


# ═══════════════════════════════════════════════════════════════════════════
#  MULTI-ZONE (I-BEAM) POUR STRATEGY
#
#  Instead of solving a single pour line, the wall is divided into N stacked
#  horizontal zones by N-1 pour-line elevations that the SWEEP treats as
#  independent variables (as fractions of the local shell depth, so a
#  constant fraction tracks the sheer along the length).  Each zone is a
#  different mix; the section is analysed as a COMPOSITE via the transformed-
#  section method (modular ratio n = E_i/E_ref), which lets strong mixes at
#  the extremes and a light core form an I-beam.
# ═══════════════════════════════════════════════════════════════════════════

def multizone_pour_grids(depth_in: float, n_zones: int,
                         step_in: float = 2.0, min_sep_in: float = 2.0,
                         margin_frac: float = 0.15) -> list:
    """
    Enumerate constructible pour-line elevation sets for an N-zone wall.

    Returns a list of tuples, each holding the N-1 interior pour-line
    FRACTIONS of the shell depth (ascending), so a set applies to every
    station by scaling with the local sheer height.

    Explosion control (as requested):
      * pour lines snap to a ``step_in`` grid on the mold depth (default 2 in),
      * consecutive lines are ≥ ``min_sep_in`` apart (default 2 in),
      * the top and bottom zones keep at least ``margin_frac`` of the depth
        (a paper-thin extreme zone is neither buildable nor useful).

    n_zones = 1 returns [()] (no pour lines — a uniform wall).
    """
    if n_zones <= 1:
        return [()]
    d = float(depth_in)
    lo, hi = margin_frac * d, (1.0 - margin_frac) * d
    marks = np.arange(np.ceil(lo / step_in) * step_in, hi + 1e-9, step_in)
    if len(marks) < n_zones - 1:
        # Depth too shallow for the requested grid — space the lines evenly.
        even = np.linspace(lo, hi, n_zones + 1)[1:-1]
        return [tuple(round(e / d, 4) for e in even)]

    import itertools as _it
    out = []
    for combo in _it.combinations(marks, n_zones - 1):
        if all(b - a >= min_sep_in - 1e-9 for a, b in zip(combo, combo[1:])):
            out.append(tuple(round(c / d, 4) for c in combo))
    return out or [tuple(round(e / d, 4)
                         for e in np.linspace(lo, hi, n_zones + 1)[1:-1])]


#: Lower-hull (below-waterline) section properties, SI, about the WATERLINE
#: datum: ``A_m2`` shell material area, ``y_m`` centroid height relative to the
#: waterline (negative = below it), ``I_m4`` second moment about the section's
#: OWN centroid.  Scalars at one station, or per-station arrays.
LowerHullSection = namedtuple("LowerHullSection", "A_m2 y_m I_m4")

_IN2_TO_M2 = 0.0254 ** 2
_IN4_TO_M4 = 0.0254 ** 4
_PSI_TO_PA = 6894.76
_FTLB_TO_NM = 1.35582


def lower_hull_section_properties(cum_area_in2, cum_Az_in3, cum_Iz_in4,
                                  z_grid_in, waterline_in) -> LowerHullSection:
    """
    REAL per-station lower-hull section properties from the Godzilla offsets.

    Replaces the former hardcoded mock (A=0.050 m², y=-0.080 m, I=1.5e-5 m⁴),
    which modelled the underwater hull as a SOLID section: 0.050 m² is very
    nearly the ENCLOSED (displaced) sectional area, whereas bending only sees
    the thin shell's MATERIAL area — about 10× less.

    No new geometry maths: ``calculate_shell_properties`` already accumulates
    area / first moment / second moment about the keel baseline into
    cum_area_in2 / cum_Az_in3 / cum_Iz_in4, so the lower hull is just those
    tables read at the waterline and shifted onto the waterline datum.

    Scales with the shell thickness baked into the cumulative tables, so it
    must be recomputed per thickness (it is independent of mold depth — the
    lower hull sits entirely below the waterline).
    """
    A_in2 = _cum_at(cum_area_in2, z_grid_in, waterline_in)
    S_in3 = _cum_at(cum_Az_in3, z_grid_in, waterline_in)      # 1st moment / keel
    I_keel_in4 = _cum_at(cum_Iz_in4, z_grid_in, waterline_in)  # 2nd moment / keel

    A_safe = np.maximum(A_in2, 1e-9)
    z_bar_in = S_in3 / A_safe                                 # centroid ↑ keel
    # Parallel-axis back to the section's own centroid
    I_own_in4 = np.maximum(I_keel_in4 - A_in2 * z_bar_in ** 2, 1e-12)

    return LowerHullSection(
        A_m2=A_in2 * _IN2_TO_M2,
        y_m=(z_bar_in - float(waterline_in)) * 0.0254,        # <0 below waterline
        I_m4=I_own_in4 * _IN4_TO_M4,
    )


def gunwale_composite_section(lower_hull: LowerHullSection, t_in, freeboard_in,
                              flare_deg):
    """
    Composite section of (lower hull + BOTH flared freeboard walls), about the
    waterline datum.  Returns (I_total_m4, c_gunwale_m).

    The cross-section has TWO freeboard walls — port and starboard — matching
    ``calculate_shell_properties``, which builds a half-section and doubles it.
    The former model counted ONE, understating the freeboard's contribution.

    Wall geometry is the same segment model geometry.py uses: a wall flared
    ``flare_deg`` off vertical spans the same height h but runs h/cosθ long, so
    A = t·h/cosθ and I_own = t·h³/(12·cosθ) — identical to that module's
    (t·ds³/12)·sin²θ with ds = h/cosθ.

    At flare_deg = 0 this reproduces calculate_shell_properties' I_in4 / c_top
    exactly (regression-tested), because a zero-flare freeboard IS the straight
    extrusion parse_godzilla_offsets builds.
    """
    t_m = float(t_in) * 0.0254
    h_m = float(freeboard_in) * 0.0254
    ct = max(np.cos(np.radians(float(flare_deg))), 1e-9)

    A_up = 2.0 * t_m * h_m / ct                 # port + starboard
    y_up = h_m / 2.0
    I_up = 2.0 * (t_m / ct) * (h_m ** 3) / 12.0

    A_t = A_up + lower_hull.A_m2
    y_na = (A_up * y_up + lower_hull.A_m2 * lower_hull.y_m) / A_t
    I_t = (I_up + A_up * (y_up - y_na) ** 2
           + lower_hull.I_m4 + lower_hull.A_m2 * (lower_hull.y_m - y_na) ** 2)
    return I_t, h_m - y_na


def freeboard_flare_gate(M_race2_ftlb, t_in, freeboard_in,
                         flare_max_deg, allow_psi, lower_hull: LowerHullSection,
                         flare_min_deg=0.0):
    """
    ALL-STATIONS freeboard/gunwale check: can the freeboard wall survive the
    2-person bending moment at EVERY station, at SOME single flare angle in
    [flare_min_deg, flare_max_deg]?

    Because the shell section varies along the hull, peak gunwale stress is NOT
    necessarily at the peak-moment station — a smaller section further from
    midship carries a lower moment but can develop a higher stress.  So this
    evaluates  σ(x) = M(x)·c(x)/I(x)  at every station (vectorised over the
    per-station arrays) and gates on the worst station, not just the loudest
    moment.

    ``allow_psi`` MUST be the tensile capacity of the mix actually poured into
    the freeboard (the top zone), not the lightest mix in the batch.
    ``lower_hull`` MUST come from lower_hull_section_properties() for the config
    under evaluation and align element-wise with ``M_race2_ftlb``.

    The single source of truth for the gunwale/flare check — the UI reports
    the same numbers through this function rather than its own copy.

    Parameters
    ----------
    M_race2_ftlb : per-station 2-person moment [ft·lb] (array), or a scalar for
                   a single-station check.
    freeboard_in : nominal freeboard wall height [in] (mold depth − draft);
                   ≈ constant along the hull, so passed as a scalar.
    lower_hull   : per-station LowerHullSection (array fields), or scalar.

    Returns (ok, flare_req_deg, worst_stress_psi, gov_station):
      ok               all stations pass at flare_req_deg
      flare_req_deg    smallest passing angle (None if none passes)
      worst_stress_psi governing station's stress at the reported angle; on
                       failure, the lowest achievable worst-case across angles
      gov_station      index of the governing (max-stress) station

    The smallest passing angle is also the lightest (wall area 2·t·h/cosθ grows
    with θ); a larger angle relieves stress, so the failure report is taken at
    the least-stress angle.  The freeboard walls give the composite a non-zero
    inertia floor, so degenerate tip stations (M→0) never spuriously govern.
    """
    M_Nm = np.abs(np.asarray(M_race2_ftlb, dtype=float)) * _FTLB_TO_NM
    allow_Pa = float(allow_psi) * _PSI_TO_PA
    best = None                      # (worst_stress_Pa, gov_idx) seen so far
    for td in np.arange(float(flare_min_deg), float(flare_max_deg) + 1e-9, 1.0):
        I_t, c_gun = gunwale_composite_section(lower_hull, t_in, freeboard_in, td)
        stress_Pa = np.atleast_1d(M_Nm * c_gun / np.maximum(I_t, 1e-12))
        gov = int(np.argmax(stress_Pa))
        worst = float(stress_Pa[gov])
        if worst <= allow_Pa:
            return True, float(td), worst / _PSI_TO_PA, gov
        if best is None or worst < best[0]:
            best = (worst, gov)
    if best is None:                 # empty flare range (flare_min > flare_max)
        return False, None, None, 0
    return False, None, best[0] / _PSI_TO_PA, best[1]


def ibeam_zone_stack(available_densities, num_zones):
    """
    The single STRICT I-beam mix stack (bottom→top) for a set of available
    mixes and a zone count — heaviest mix at the two extreme zones, lightest
    at the core, graded in between.

    This replaces brute-force itertools.product over every stacking
    permutation: only the structurally-sensible I-beam is tested, so the
    multi-zone sweep stays tiny even at full density resolution.  Inverted
    ([Light, Heavy, Light]) and uniform ([Heavy, Heavy, Heavy]) stacks are
    never generated.

    Examples (2 mixes dH > dL):
        num_zones = 2 → (dH, dL)              # heavy bottom, light top
        num_zones = 3 → (dH, dL, dH)          # classic I-beam
        num_zones = 4 → (dH, dL, dL, dH)
    """
    ds = sorted({float(d) for d in available_densities}, reverse=True)  # heavy→light
    n = int(num_zones)
    if n <= 1:
        return (ds[0],)
    if n == 2:
        return (ds[0], ds[-1])
    stack = []
    for z in range(n):
        dist = min(z, n - 1 - z)          # 0 at the extreme zones, larger toward core
        stack.append(ds[min(dist, len(ds) - 1)])
    return tuple(stack)


def evaluate_multizone_weight(
        coords_4, coords_2, zone_dens, pour_fracs,
        x, dx, width_ft, t_in,
        cum_area, cum_Az, cum_Iz, z_grid, sheer_in, na_ref,
        get_props, coords_3=None, position_variance_ft=0.0,
        enable_mesh=False, return_details=False,
        freeboard_min_in=None, flare_max_deg=None, waterline_in=None,
        gw_width_mid=0.0, gw_height_mid=0.0, gw_fillet_ratio=1.0,
        n_mixes_max=None, cum_Iy=None, yhalf_grid=None, enable_hoop_gate=True,
        rocker_ft=0.0, tally=None, depth_in=None, hull_weight_max_lb=None,
        crew_cases=None):
    """
    Weight of a multi-zone (I-beam) canoe, or REJECTED if any zone's governing
    fiber overstresses OR (when freeboard_min_in/flare_max_deg are given) the
    freeboard wall — built from the TOP-zone mix — cannot survive at any flare
    angle.  Folding the freeboard check into feasibility here (instead of a
    bounded post-sort walk) means the lightest surviving design is found even
    when many light-top stacks would fail the freeboard.

    Parameters
    ----------
    coords_4/2/3     : paddler stations [ft]
    zone_dens        : EXPLICIT mix density per horizontal zone, bottom→top
                       (e.g. [Struct, Light, Struct] for a 3-zone I-beam
                       poured from 2 physical mixes).  The number of zones is
                       len(zone_dens) and is decoupled from the number of
                       distinct mixes — the caller enumerates the vertical
                       stacking permutations.
    pour_fracs       : (n_zones-1,) interior pour-line fractions of local
                       shell depth, ascending (from multizone_pour_grids)
    x, dx, width_ft  : station geometry
    t_in             : wall thickness
    cum_area/Az/Iz, z_grid : cumulative section moments (composite input)
    sheer_in         : per-station section top height [in above keel]
    na_ref           : per-station homogeneous NA height [in] (τ reference)
    get_props        : callable density -> {strength_t, strength_c, density}
    gw_width_mid / gw_height_mid / gw_fillet_ratio :
                       gunwale rail dimensions.  The rail is built by the shared
                       gunwale_rail_section, poured from the TOP-zone mix, added
                       to the self-weight and transformed into the composite
                       section (n = E_top/E_ref); its fillet Kt amplifies the
                       top-zone top-fiber stress — identical modeling to the
                       single-zone evaluate_canoe_weight.  0 = no gunwale.
    tally            : optional SweepTally.  When given, every call logs which
                       gate rejected it and its D/C on all of them, so a sweep
                       that finds nothing can say exactly what bound it and by
                       how much.  None (the default) = the untouched fast path.
    depth_in         : mold depth [in] — carried for the tally's report only.
    Returns
    -------
    weight [lb] (float), or a details dict when return_details=True.
    """
    zone_dens = [float(d) for d in zone_dens]
    n_zones = len(zone_dens)
    n_st = len(x)
    # N_mixes_max cap — hard feasibility gate on the number of DISTINCT mix
    # densities poured.  Re-checked here (not only filtered in enumeration) so
    # an over-cap stack can never win even if handed to the evaluator directly.
    # For future longitudinal zoning, the caller aggregates the distinct
    # densities across all segments before this check.
    if n_mixes_max is not None and \
            len({round(d, 3) for d in zone_dens}) > int(n_mixes_max):
        if tally is not None:
            tally.record("n_mixes_max", {}, t_in=t_in, depth_in=depth_in,
                         mixes=zone_dens, fracs=pour_fracs)
        return (REJECTED if not return_details
                else {"weight": REJECTED, "feasible": False})
    # Per-zone material: ACI elastic modulus (f'c = strength_c·FOS, but FOS
    # cancels in the modular ratio) and tensile allowable.
    zone_props = [get_props(d) for d in zone_dens]
    zone_E = np.array([float(aci_elastic_modulus(p["density"], p["strength_c"]))
                       for p in zone_props])
    # Bending tension is a FLEXURAL stress (σ=My/I), so it is checked against the
    # modulus of rupture f_r when the team has a MOR fit, falling back to the
    # (conservative) direct-tensile f_t otherwise.  ``strength_flexural`` encodes
    # that choice in the mix DB; ``.get`` keeps legacy/stub property dicts working.
    zone_str = np.array([float(p.get("strength_flexural", p["strength_t"]))
                         for p in zone_props])
    # Per-zone compressive allowable f'c (already FoS-reduced), for the Kupfer
    # tension-compression interaction -- each zone against its OWN f'c.
    zone_str_c = np.array([float(p.get("strength_c", 1e9)) for p in zone_props])

    # Interior pour heights per station (fraction × local sheer), then the
    # full ascending boundary list [0, z1, …, sheer].
    fr = np.asarray(pour_fracs, dtype=float)
    z_bounds = [np.zeros(n_st)]
    for f in fr:
        z_bounds.append(f * sheer_in)
    z_bounds.append(np.asarray(sheer_in, dtype=float))

    # ── Gunwale rail (shared geometry; poured from the TOP-zone mix, which
    #    sits at the sheer).  Same helper the single-zone evaluator uses. ─────
    rail = gunwale_rail_section(x, t_in, gw_width_mid, gw_height_mid,
                                gw_fillet_ratio, sheer_in)

    # ── Self-weight from the zone areas (drives the moment envelope) ─────────
    w_self = np.zeros(n_st)
    prev = _cum_at(cum_area, z_grid, z_bounds[0])
    for i in range(n_zones):
        A_t = _cum_at(cum_area, z_grid, z_bounds[i + 1])
        w_self += (A_t - prev) * float(zone_dens[i]) / 144.0
        prev = A_t
    if rail is not None:                          # rail mass = top-zone density
        w_self += (rail.A_gw + rail.A_fil) * float(zone_dens[-1]) / 144.0

    struct_data = calculate_moment_envelope(
        x, dx, w_self, width_ft, coords_4, coords_2, return_all=True,
        crew_cases=crew_cases,
        paddler_coords_3=coords_3, position_variance_ft=position_variance_ft,
        rocker_ft=rocker_ft)
    M_sag = struct_data["moment"]["sag"]        # bottom-tension driver
    M_hog = struct_data["moment"]["hog"]        # top-tension driver

    # ── Composite section properties for this zoning (+ gunwale) ────────────
    # The rail is the top-zone material, so transform it by n = E_top/E_ref and
    # fold its keel-baseline first/second moments into the composite.  With no
    # rail these terms are None and the section is the plain zoned shell.
    _gw_EA = _gw_EAz = _gw_EIz = None
    if rail is not None:
        _n_gw = float(zone_E[-1]) / float(zone_E[0])
        _S_rail  = rail.A_gw * rail.gw_cent + rail.A_fil * rail.fil_cent
        _Iz_rail = (rail.A_gw * rail.gw_cent ** 2 + rail.I_gw_own
                    + rail.A_fil * rail.fil_cent ** 2 + rail.I_fil_own)
        _gw_EA  = _n_gw * (rail.A_gw + rail.A_fil)
        _gw_EAz = _n_gw * _S_rail
        _gw_EIz = _n_gw * _Iz_rail
    y_bar, I_tr, c_top, c_bot = composite_section_properties(
        z_bounds, zone_E, cum_area, cum_Az, cum_Iz, z_grid,
        _gw_EA, _gw_EAz, _gw_EIz)

    # ── Per-zone tension check at each zone's worst fiber ───────────────────
    # Bending stress in zone i at height z:  σ = (E_i/E_ref)·M·(z−y_bar)/I_tr.
    # Sagging tensions material BELOW y_bar; hogging tensions material ABOVE.
    E_ref = zone_E[0]
    # Which stations are real hull girder.  yhalf_grid is optional on the legacy
    # path, and without it the breadth half of the test cannot be applied; the
    # area half still removes the empty tip sections, which is where I_tr hits
    # its clamp.  ONE definition, shared with both transverse checks.
    if yhalf_grid is not None:
        _real_station = structural_station_mask(cum_area, z_grid, sheer_in,
                                                yhalf_grid)
    else:
        _real_station = _cum_at(cum_area, z_grid, sheer_in) > \
            DEGENERATE_SECTION_A_IN2
    worst_util = np.zeros(n_st)
    # PER-FIBRE, PER-SENSE utilisation, kept separate from worst_util (which
    # collapses them).  The report's governing-case block and the utilisation
    # plot need the four distinct curves; handing them one array twice and two
    # zeros made a hogging case unreportable and the compression advisory
    # unfireable on this path.  Sign convention, both envelopes positive:
    #   SAGGING  tensions material BELOW the NA, compresses ABOVE
    #   HOGGING  tensions material ABOVE the NA, compresses BELOW
    util_bot_t = np.zeros(n_st)      # sagging  tension     at the zone's low fibre
    util_top_t = np.zeros(n_st)      # hogging  tension     at the zone's high fibre
    util_bot_c = np.zeros(n_st)      # hogging  compression at the zone's low fibre
    util_top_c = np.zeros(n_st)      # sagging  compression at the zone's high fibre
    zone_peak_tension = np.zeros(n_zones)
    for i in range(n_zones):
        zb, zt = z_bounds[i], z_bounds[i + 1]
        n_i = zone_E[i] / E_ref
        allow = zone_str[i]
        if enable_mesh and i == n_zones - 1:      # mesh boosts only the top zone
            allow = max(allow, 800.0)
        # Sagging tension: greatest at the zone's lowest fiber below y_bar
        d_sag = np.maximum(y_bar - zb, 0.0)       # ≥0 where zone dips below NA
        sig_sag = n_i * (M_sag * 12.0) * d_sag / I_tr
        # Hogging tension: greatest at the zone's highest fiber above y_bar.
        # The top zone carries the gunwale rail at its fillet junction, so its
        # top-fiber tension is amplified by the fillet Kt (as in single-zone).
        d_hog = np.maximum(zt - y_bar, 0.0)
        sig_hog = n_i * (M_hog * 12.0) * d_hog / I_tr
        # Compression at the opposite fibre of each sense, against the zone's
        # OWN f'c.  Same levers, other moment: sagging compresses the high
        # fibre, hogging the low one.
        sig_top_c = n_i * (M_sag * 12.0) * d_hog / I_tr
        sig_bot_c = n_i * (M_hog * 12.0) * d_sag / I_tr
        if rail is not None and i == n_zones - 1:
            # The fillet junction concentrates whatever stress is at that
            # fibre, so it scales the top-fibre compression as well.
            sig_hog = sig_hog * rail.Kt
            sig_top_c = sig_top_c * rail.Kt
        allow_c = zone_str_c[i]
        util_bot_t = np.maximum(util_bot_t, np.where(
            _real_station, sig_sag / max(allow, 1e-9), 0.0))
        util_top_t = np.maximum(util_top_t, np.where(
            _real_station, sig_hog / max(allow, 1e-9), 0.0))
        util_top_c = np.maximum(util_top_c, np.where(
            _real_station, sig_top_c / max(allow_c, 1e-9), 0.0))
        util_bot_c = np.maximum(util_bot_c, np.where(
            _real_station, sig_bot_c / max(allow_c, 1e-9), 0.0))
        sig = np.maximum(sig_sag, sig_hog)
        util = sig / max(allow, 1e-9)
        # Degenerate end stations are excluded here for the SAME reason as in
        # the two transverse checks: at a tip the composite I_tr collapses to
        # its 1e-9 clamp while the fibre distance does not, so sigma = M*d/I_tr
        # is a ratio of guard values, not a stress.  Measured on the live hull:
        # 60.52 at both tips against an interior peak of 0.79 -- a 76x artifact
        # that both corrupted the reported longitudinal D/C and, on the legacy
        # path where this check still GATES (no lateral geometry -> no Kupfer),
        # rejected the sweep's own winner with weight 1e6.
        util = np.where(_real_station, util, 0.0)
        worst_util = np.maximum(worst_util, util)
        zone_peak_tension[i] = float(np.max(sig[_real_station])
                                     if np.any(_real_station) else 0.0)
    # NOTE: this independent longitudinal-tension D/C is kept for REPORTING only.
    # The feasibility gate is now the unified Kupfer biaxial check below, which
    # subsumes it (Kupfer with sigma_y=0 == this longitudinal util) plus the
    # transverse hoop check plus their tension-compression interaction.

    # Freeboard-wall gate — built from the TOP-zone mix (zone_dens[-1]).  Part
    # of feasibility so a light-top stack is rejected here, not silently
    # dropped by a bounded post-sort walk.
    flare_req = None
    _fb_infeasible = False
    _fb_dc = None                     # freeboard-wall demand/capacity, for the tally
    if (freeboard_min_in is not None and flare_max_deg is not None
            and waterline_in is not None):
        _fb_allow = zone_str[-1]
        if enable_mesh:
            _fb_allow = max(_fb_allow, 800.0)
        # ALL-STATIONS gate: the real per-station section vs the per-station
        # moment, so a small off-midship section is caught even though the
        # moment peaks elsewhere.  Nominal freeboard = tallest sheer − waterline
        # (≈ constant along the hull).
        _lh_all = lower_hull_section_properties(
            cum_area, cum_Az, cum_Iz, z_grid, waterline_in)
        _fb_in = max(float(np.max(sheer_in)) - float(waterline_in), 1e-6)
        _ok, flare_req, _fb_stress, _ = freeboard_flare_gate(
            struct_data["moment"]["race_2"], t_in, _fb_in, flare_max_deg,
            _fb_allow, _lh_all)
        # On failure the gate reports the LOWEST worst-case stress achievable
        # across the flare range, so this D/C is the wall's true shortfall at
        # its best angle — the number that says "marginal" or "hopeless".
        if _fb_stress is not None:
            _fb_dc = float(_fb_stress) / max(float(_fb_allow), 1e-9)
        if not _ok:
            # The freeboard wall is checked BEFORE the (much more expensive)
            # hoop + Kupfer work, and stays that way with the tally on: a
            # freeboard rejection is recorded from its own exact D/C and the
            # call returns, so diagnostics never slow the sweep down.  The
            # record is flagged PARTIAL because the later gates were never
            # evaluated for it.
            if tally is not None:
                tally.record("freeboard_flare", {"freeboard wall": _fb_dc},
                             t_in=t_in, depth_in=depth_in, mixes=zone_dens,
                             fracs=pour_fracs, partial=True,
                             gov="the freeboard wall at its best flare angle "
                                 f"in 0–{float(flare_max_deg):.0f}°")
            if not return_details and _SWEEP_DIAG is None:
                return REJECTED
            _fb_infeasible = True        # defer so the diagnostics still build

    # ── Transverse (hoop) gate — centripetal turn moment vs the section's
    #   lateral capacity, enforced PER ZONE on a transformed (modular-ratio)
    #   lateral section.  Each zone's outboard fibre is checked against its OWN
    #   f_t, so the soft light web (widest fibre, lowest strength) is caught —
    #   the earlier gate wrongly used the top-zone strength for the global
    #   extreme fibre.  Skipped only when the lateral geometry is not supplied
    #   (legacy callers); production always provides cum_Iy / yhalf_grid.
    _hull_wt = trapezoid(w_self, x)
    _hoop = None
    if cum_Iy is not None and yhalf_grid is not None:
        _A_full = _cum_at(cum_area, z_grid, sheer_in)
        # Gunwale rail's transformed lateral inertia: it hugs the sheer at
        # y_rail ≈ half-breadth at the top, poured from the top-zone mix, so
        # n_rail = E_top/E_ref.  Folded into I_lat_tr like the shell zones.
        #
        # DECISION (commit 448c37b, 2026-07-18): under THIS corrected gate the
        # rail was given fair transverse credit (transformed, at the wide
        # fibre) and gunwales still lost to plain web densification by >=9 lb.
        # The rail's benefit saturates by ~1" (the full-height side walls
        # dominate I_lat_tr, so a small top area barely moves it) and never
        # buys even one 0.5-pcf step of web reduction, while a 1" rail already
        # weighs ~9 lb.  Verified winner: 111.48 lb [65,61,65] (web densified
        # 57.5->61).  Do not re-open "should gunwales carry the hoop load" as an
        # unknown — it was measured, not assumed.
        _rail_lat = None
        if rail is not None:
            _n_rail = float(zone_E[-1]) / float(zone_E[0])
            _y_rail = _cum_at(yhalf_grid, z_grid, sheer_in)
            _rail_lat = _n_rail * 2.0 * (rail.A_gw + rail.A_fil) * _y_rail ** 2
        # Turn moment as a per-station distribution: k_lat · |vertical bending
        # envelope|.  Passing the envelope (not a scalar) is what makes the gate
        # resolution-independent — the moment vanishes at the degenerate tips.
        _hoop = transverse_hoop_utilization(
            cum_Iy, yhalf_grid, z_grid, z_bounds, zone_E, zone_str, t_in,
            _A_full, struct_data["moment"]["envelope"],
            rail_lat_Itr=_rail_lat,
            avg_speed_mps=getattr(config, "MANEUVER_SPEED_MPS", 2.5),
            min_turn_radius_m=getattr(config, "MANEUVER_TURN_RADIUS_M", 4.0))
        # _hoop["util"] is kept for REPORTING only — the feasibility gate is the
        # unified Kupfer biaxial check below.

    # ── UNIFIED KUPFER BIAXIAL TENSILE GATE ─────────────────────────────────
    # Replaces the two independent tensile checks (longitudinal + transverse)
    # with one per-fibre, per-station, per-load-instance biaxial criterion.  The
    # old worst_util / _hoop above are retained for the report's side-by-side
    # comparison but no longer gate.  Requires the lateral geometry; legacy
    # callers without it fall back to the longitudinal check.
    _kupfer = None
    if cum_Iy is not None and yhalf_grid is not None:
        _E_ref = zone_E[0]
        _ones = np.ones(n_st)
        _znt = [(_ones * float(zone_E[i] / _E_ref)) for i in range(n_zones)]
        _zft = [(_ones * float(zone_str[i])) for i in range(n_zones)]
        if enable_mesh:
            _zft[-1] = np.maximum(_zft[-1], 800.0)
        _zfc = [(_ones * float(zone_str_c[i])) for i in range(n_zones)]
        _Kt = (np.asarray(rail.Kt, float) * _ones) if rail is not None else None
        # enable_hoop_gate=False (a comparison toggle) drops the transverse work
        # by zeroing k_lat, so the gate reduces to longitudinal + shear.
        _k_lat = (getattr(config, "MANEUVER_SPEED_MPS", 2.5) ** 2
                  / (9.807 * max(getattr(config, "MANEUVER_TURN_RADIUS_M", 4.0), 0.1))
                  if enable_hoop_gate else 0.0)
        _kupfer = kupfer_combined_gate(
            z_grid=z_grid, cum_area=cum_area, cum_Az=cum_Az, cum_Iy=cum_Iy,
            yhalf=yhalf_grid, sheer=sheer_in, y_bar=y_bar, I_tr=I_tr, t_in=t_in,
            zone_bounds=z_bounds, zone_n=_znt, zone_ft=_zft, zone_fc=_zfc,
            instances=struct_data["instances"], k_lat=_k_lat,
            Kt=_Kt, rail_lat_Itr=_rail_lat)
    # Feasibility from the unified gate (or the legacy longitudinal fallback),
    # plus the freeboard/flare wall gate above.  When return_details is set we
    # DON'T early-return: the full diagnostic dict (hoop_util, worst_util,
    # Kupfer/Rankine D/C, the reject reason, ...) is returned with feasible=False
    # so the report/UI/diagnostics can inspect WHY it failed.
    # Acceptance limit, not 1.0 -- see kupfer_acceptance_limit(): the gating
    # utilisation is not converged at the search resolution, so a design is
    # accepted only with enough headroom to survive refinement.
    # ONE gate function, shared with the winner re-gate in main.py.
    _kupfer_infeasible, _gate_util, _acc = kupfer_gate(_kupfer, worst_util)
    _legacy_infeasible = False        # folded into kupfer_gate
    # ASCE LOADED-FREEBOARD RULE, per configuration, against THIS design's own
    # weight -- see max_hull_weight_for_freeboard.  A heavier hull floats deeper
    # and delivers less freeboard, so the check has to see the real weight, not
    # a pre-sweep estimate of it.
    _fb_load_infeasible = bool(hull_weight_max_lb is not None
                               and _hull_wt > float(hull_weight_max_lb))
    _infeasible = (_fb_infeasible or _kupfer_infeasible or _legacy_infeasible
                   or _fb_load_infeasible)
    _reject = ("freeboard_flare" if _fb_infeasible else
               "loaded_freeboard" if _fb_load_infeasible else
               "kupfer_tensile" if _kupfer_infeasible else
               "longitudinal" if _legacy_infeasible else None)
    if _SWEEP_DIAG is not None:
        _SWEEP_DIAG.append({
            "reject": _reject, "feasible": not _infeasible,
            "kupfer_max":  (None if _kupfer is None else float(_kupfer["util_max"])),
            "rankine_max": (None if _kupfer is None else float(_kupfer["rankine_max"])),
            "worst_util_max": float(np.max(worst_util)),
            "hoop_max": (None if _hoop is None else float(np.max(_hoop["util"]))),
            "flare_req_deg": (None if flare_req is None else float(flare_req)),
            "t_in": float(t_in), "zone_dens": [float(d) for d in zone_dens],
            "fracs": [float(f) for f in np.asarray(fr)],
            "kupfer_gov": (None if _kupfer is None else _kupfer.get("gov")),
            "weight_if_feasible": float(_hull_wt),
        })
    if tally is not None and not _fb_infeasible:
        # Every gate this configuration was actually put through, with the
        # margin it had on each — a complete record (the freeboard partials
        # returned above are the only incomplete ones).
        tally.record(
            _reject,
            {"Kupfer biaxial":  (float(np.max(worst_util)) if _kupfer is None
                                 else float(_kupfer["util_max"])),
             "freeboard wall":  _fb_dc,
             "loaded freeboard": (None if hull_weight_max_lb is None
                                  or float(hull_weight_max_lb) <= 0
                                  else float(_hull_wt) / float(hull_weight_max_lb))},
            {"longitudinal (Rankine)": (None if _kupfer is None
                                        else float(_kupfer["rankine_max"])),
             "longitudinal (bending only)": float(np.max(worst_util)),
             "transverse hoop":  (None if _hoop is None
                                  else float(np.max(_hoop["util"])))},
            t_in=t_in, depth_in=depth_in, mixes=zone_dens, fracs=pour_fracs,
            weight=float(_hull_wt),
            gov=(None if _kupfer is None else _kupfer.get("gov")))
    if _infeasible and not return_details:
        return REJECTED

    # REJECTED, not a large float: an infeasible design has no mass, and a
    # float sentinel is indistinguishable from a very heavy hull to anything
    # downstream that does arithmetic on it.
    total_weight = REJECTED if _infeasible else float(_hull_wt)
    if not return_details:
        return total_weight
    # Per-station interior pour-line heights (constant fraction × local sheer)
    zone_pour_lines = [np.asarray(z_bounds[i + 1], dtype=float)
                       for i in range(n_zones - 1)]
    return {
        "weight": total_weight,
        "feasible": not _infeasible,
        "reject": _reject,          # which gate rejected: freeboard_flare | kupfer_tensile | longitudinal | None
        "worst_util_max": float(np.max(worst_util)),
        "zone_dens": zone_dens,             # bottom→top densities
        "pour_fracs": np.asarray(fr),
        "zone_pour_lines": zone_pour_lines,  # list of (n_st,) heights [in]
        "y_bar": y_bar, "I_tr": I_tr,
        "c_top": c_top, "c_bot": c_bot,
        "worst_util": worst_util,
        # The four per-fibre curves behind worst_util (see their definition
        # above).  Report-only, like the single-zone path's equivalents.
        "util_bot_t": util_bot_t, "util_top_t": util_top_t,
        "util_bot_c": util_bot_c, "util_top_c": util_top_c,
        # Transverse (hoop) results — transformed per-zone D/C, the governing
        # zone per station, and the homogeneous per-zone D/C for comparison.
        "hoop_util": None if _hoop is None else _hoop["util"],
        "hoop_gov_zone": None if _hoop is None else _hoop["gov_zone"],
        "hoop_dc_homog": None if _hoop is None else _hoop["dc_homog"],
        "hoop_util_zone": None if _hoop is None else _hoop["util_zone"],
        # Unified Kupfer biaxial gate (the feasibility check) + comparisons.
        "kupfer_util": None if _kupfer is None else _kupfer["util"],
        "kupfer_max": None if _kupfer is None else _kupfer["util_max"],
        "rankine_util": None if _kupfer is None else _kupfer["rankine_util"],
        "rankine_max": None if _kupfer is None else _kupfer["rankine_max"],
        "kupfer_gov": None if _kupfer is None else _kupfer["gov"],
        "M_sag": M_sag, "M_hog": M_hog,
        "sheer_in": np.asarray(sheer_in, dtype=float),
        "w_self": w_self,
        "struct_data": struct_data,          # full moment/shear cases
        "flare_req_deg": flare_req,
    }
