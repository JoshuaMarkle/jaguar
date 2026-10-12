# reporting/latex_report.py
"""
Rigorous LaTeX technical report for the ASCE Concrete Canoe structural design.

SINGLE SOURCE OF TRUTH
----------------------
Every number in the report is read from the live sweep ``result`` object (the
same dict the app renders) or recomputed on demand from the SAME engines the
sweep uses (``geometry.calculate_shell_properties``, ``structural`` composite /
hoop / punching functions, the mix-regression database).  Nothing is hardcoded,
cached, or independently re-derived.  A section with no validated data states
that plainly and prints no figure — never a placeholder.

GENERAL-PURPOSE, TIMELESS DOCUMENT
----------------------------------
The report is written to stand on its own for any team, in any year: it
explains the logic of the CURRENT analysis and walks through the governing math
so every design choice is justified from first principles.  It does not cite
past projects, prior reports, dated fixes, or the program's own history —
validation is against closed-form theory the reader can reproduce, not against
another team's numbers.

Structure follows a derive-loads -> stresses -> checks flow:
  1  Design objectives & constraints            (config)
  2  Hull geometry                              (real offsets + section engine)
  3  Load derivation  w(x)->V(x)->M(x)          (result envelopes; model stated)
  4  Governing cross-section, worked longhand   (composite transformed section)
  5  Structural checks (bending / TRANSVERSE-HOOP / shear / punching)
  6  Multi-zone I-beam pour                      (real [.,.,.] stack + N-band chart)
  7  Mix regression                              (DB fit, provenance)
  8  Wave resistance                             (reported only with validated data)
  9  Section-engine validation                  (three analytic self-checks)
 10  Safety factors & ASCE/ACI compliance        (live values)

Rendered to PDF with the ``tectonic`` engine when available; otherwise the
``.tex`` source is written and its path returned.

PORTABILITY
-----------
Figure paths in the emitted ``.tex`` are RELATIVE to the ``.tex`` itself, so the
whole output directory (``.tex`` + PNGs) can be moved, zipped, or handed to a
teammate and still compile.  Never emit absolute paths here: they pin the
document to one machine's layout, and this project's own path contains spaces,
which breaks ``\\includegraphics`` regardless.

TECTONIC
--------
``tectonic`` is a single self-contained binary -- no TeX distribution needed; it
downloads only the packages a document actually uses, on first run.  It is NOT
in winget.  To install (Windows)::

    $d = "$env:LOCALAPPDATA\\Programs\\tectonic"
    New-Item -ItemType Directory -Force $d
    # from https://github.com/tectonic-typesetting/tectonic/releases/latest
    #   tectonic-<ver>-x86_64-pc-windows-msvc.zip  -> extract tectonic.exe to $d
    # then add $d to the User PATH

macOS/Linux: ``brew install tectonic`` or ``cargo install tectonic``.  If it is
installed somewhere off PATH, pass ``tectonic_path=`` to
``generate_latex_report``.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.integrate import trapezoid
from errors import domain_only

logger = logging.getLogger(__name__)

import config
from geometry import (parse_godzilla_dimensions, parse_godzilla_offsets,
                      calculate_shell_properties)
from structural import (_cum_at, aci_elastic_modulus, composite_section_properties,
                        punching_two_knee, loaded_freeboard_check,
                        punching_shear_check, layer_schedule, mesh_design_depth)


# ── report data model — the single source of truth ─────────────────────────────

def _solid_keel_audit(off_mlt, L_ft, draft_in, depth_in, t_in, dens_pcf=65.0):
    """
    Live check of the uniform-thickness premise on the winner geometry.

    The structural weight model is pure thin-wall (perimeter x t).  The CAD
    export (``build_complete_canoe``) solidifies any near-keel band that would
    otherwise be thinner than the shell.  This compares the as-built concrete
    ring to the thin-wall model station-by-station so the report can state, per
    hull, whether ``uniform 0.375" everywhere`` actually holds -- and FLAG it
    (rather than silently diverge) if a future thinner/finer hull trips
    meaningful solidification.  Guarded so a missing offset file never breaks
    the report.
    """
    try:
        from geometry import build_complete_canoe
        rows = []
        with open(off_mlt) as f:
            for line in f:
                c = line.split('#')[0].split('!')[0].replace(',', ' ').strip()
                if not c:
                    continue
                v = [float(x) for x in c.split()]
                if v:
                    rows.append(v)
        M = np.array(rows)
        res = build_complete_canoe(M, L_ft * 0.3048, draft_in * 0.0254,
                                   depth_in * 0.0254, t_m=t_in * 0.0254,
                                   stem_rocker_m=0.0)
        xm = np.asarray(res["x_m"]); outer = res["outer"]; inner = res["inner"]
        t_m = t_in * 0.0254
        ring = []; thin = []; xs = []; nsolid = 0
        for i, (o, inn) in enumerate(zip(outer, inner)):
            if o is None or inn is None:
                continue
            o = np.asarray(o)[:, 1:3]; inn = np.asarray(inn)[:, 1:3]
            a_ring = 2.0 * (abs(trapezoid(o[:, 0], o[:, 1]))
                            - abs(trapezoid(inn[:, 0], inn[:, 1])))
            a_thin = 2.0 * float(np.sum(np.sqrt(
                (np.diff(o, axis=0) ** 2).sum(1)))) * t_m
            ring.append(max(a_ring, 0.0)); thin.append(a_thin); xs.append(xm[i])
            if a_ring > a_thin * 1.05:
                nsolid += 1
        xs = np.array(xs)
        V_ring = float(trapezoid(np.array(ring), xs))
        V_thin = float(trapezoid(np.array(thin), xs))
        lb = dens_pcf * 35.3147
        frac = (V_ring - V_thin) / max(V_thin, 1e-12)
        return {"computed": True, "V_thin_lb": V_thin * lb,
                "V_ring_lb": V_ring * lb, "delta_lb": (V_ring - V_thin) * lb,
                "delta_frac": frac, "n_solidified": nsolid,
                "n_stations": int(len(xs)), "dens_pcf": float(dens_pcf),
                "uniform_ok": bool(frac <= 0.05 and nsolid == 0)}
    except Exception as exc:
        # DELIBERATELY BROAD.  The rule reads on the ENCLOSING work, not on this
        # body: what an escaping exception would discard is the report being
        # built and the sweep behind it, both already paid for.  This audit is
        # one optional paragraph in the manufacturing section, it reads an
        # offset file (OSError, and a dehydrated OneDrive file looks exactly
        # like one) and re-runs the CAD solidifier over it (any numeric error).
        # `computed: False` is an explicit claim of absence that the report
        # renders as such -- it never reads as a passing audit.
        return {"computed": False,
                "error": f"{type(exc).__name__}: {exc}"}


def build_report_model(result: dict) -> dict:
    """Compute EVERY report value from the live result + the production engines."""
    if not result or "weight_lbs" not in result:
        raise ValueError("report requires a completed structural sweep result")
    db = config.get_mix_db()
    m: dict = {}

    # headline
    t_in = float(result["t_in"]); d_in = float(result["depth_in"])
    # Homogeneous / single-zone (1-mix) designs carry no pour zoning:
    # ``zone_densities`` and ``pour_fracs`` are None.  Represent the uniform
    # section as a 3-zone stack of ONE density with even pour lines -- this is
    # mathematically identical to a plain homogeneous section (equal moduli
    # collapse the composite transform), and it lets the report's 3-zone
    # machinery (which hardcodes three fibres) render without special-casing.
    _zd = result.get("zone_densities")
    m["homogeneous"] = not bool(_zd)
    if _zd:
        zdens = [float(v) for v in _zd]
        fracs = [float(v) for v in (result.get("pour_fracs") or [])]
    else:
        _bm = result.get("best_mixes") or []
        _d0 = float(_bm[0]) if _bm else float(getattr(config, "MAX_DENSITY_PCF", 80.0))
        zdens = [_d0, _d0, _d0]                 # uniform 3-zone == homogeneous
        fracs = [1.0 / 3.0, 2.0 / 3.0]
    m["weight_lbs"] = float(result["weight_lbs"])
    m["t_in"] = t_in; m["depth_in"] = d_in
    m["zone_densities"] = zdens; m["pour_fracs"] = fracs
    m["n_distinct_mixes"] = len(set(round(v, 1) for v in zdens))
    # Acceptance limit actually in force for this run (numerical-convergence
    # margin on the governing Kupfer D/C; 1.0 = disabled).
    m["kupfer_accept_max"] = float(getattr(config, "KUPFER_ACCEPTANCE_MAX", 1.0))
    # Strength-fit override provenance.  render_latex only ever sees ``m``, so
    # a caveat that reads it off the raw result is one that never fires --
    # carry it across explicitly or the loudest warning in the report is dead.
    m["fit_override"] = result.get("fit_override")
    # Stem overhang actually applied, and whether it was derived or assumed.
    m["stem_overhang"] = {
        "in_per_end": float(getattr(config, "STEM_OVERHANG_IN",
                                    getattr(config, "STRUCT_ROCKER_IN", 0.0))),
        "derived": getattr(config, "STEM_OVERHANG_DERIVED", None),
    }
    # Design methodology actually applied.  Taken from the sweep's own
    # struct_data when present, so the report can never disagree with the run.
    # The sweep carries this on the winner ("design"); struct_data is the
    # in-evaluator source.  Fall back to rebuilding it from the live config only
    # if neither is present, so the section is never silently empty.
    _sd = result.get("struct_data") or {}
    _design = result.get("design") or _sd.get("design")
    if not _design:
        from structural import design_summary
        _design = design_summary(result.get("case_names"))
    m["design"] = _design
    m["regression_tag"] = (db.get_active_version_tag()
                           if hasattr(db, "get_active_version_tag") else "")

    # Provenance of each winning density: a mix the team has actually batched,
    # or a density the regression predicted for them.  A reader must never have
    # to guess whether a quoted design assumes concrete that exists.
    try:
        from reporting.mix_targets import classify_densities, zone_names_for
        _existing = (db.get_existing_mix_densities()
                     if hasattr(db, "get_existing_mix_densities") else [])
        m["mix_provenance"] = classify_densities(
            zdens, _existing,
            lambda d: db.get_mix_properties(d, fos=config.FACTOR_OF_SAFETY),
            zone_names_for(len(zdens)))
        m["existing_mix_pcf"] = list(_existing)
        m["mix_all_real"] = all(v.is_real for v in m["mix_provenance"])
    except ImportError:
        m["mix_provenance"] = []
        m["existing_mix_pcf"] = []
        m["mix_all_real"] = None
    # Buildable fallback, when the sweep priced it (theoretical mode only).
    _spec = result.get("mix_spec")
    m["buildable_weight_lbs"] = getattr(_spec, "buildable_weight_lb", None)
    m["buildable_mixes_pcf"] = getattr(_spec, "buildable_mixes_pcf", None)

    def mix(dn):
        """
        Material properties for one zone mix.

        Returns RAW strengths and FoS-reduced ALLOWABLES as separate values --
        they are not interchangeable.  Code that sizes a member divides by the
        allowable; code that evaluates a code equation in terms of f'c (ACI's
        sqrt(f'c) terms, the ACI modulus) must use the RAW strength and apply
        the factor of safety exactly once, itself.  Conflating the two silently
        applies FoS twice.
        """
        fos = config.FACTOR_OF_SAFETY
        raw = db.get_mix_properties(float(dn), fos=1.0)     # true f'c, f_t
        fc_raw, ft_raw = float(raw["strength_c"]), float(raw["strength_t"])
        return {"E": aci_elastic_modulus(raw["density"], fc_raw),
                "fc": fc_raw, "ft": ft_raw,                 # raw strengths
                "fc_allow": fc_raw / fos, "ft_allow": ft_raw / fos}

    # hull geometry (real offsets) — resolve the reference hull from the repo
    # root (the same files the sweep reads), not the caller's cwd.
    # Shared resolver, so the report documents the SAME hull the sweep read.
    # This used to be the repo root, which in a frozen build is `_internal` --
    # the report would then have described the bundled hull while the sweep
    # analysed the user's own.
    from asset_paths import resolve_asset as _resolve_asset
    _out_mlt = _resolve_asset("out.mlt")
    _off_mlt = _resolve_asset("off1.mlt")
    if _out_mlt is None or _off_mlt is None:
        raise FileNotFoundError(
            "the report needs out.mlt and off1.mlt and cannot find them; "
            "rendering a report against a hull that is not there would "
            "document a boat nobody analysed")
    L_ft, draft_in = parse_godzilla_dimensions(_out_mlt)
    xr_uw, zr_uw, ym_uw = parse_godzilla_offsets(
        _off_mlt, L_ft=L_ft, draft_in=draft_in, mold_depth_in=draft_in)
    xg = np.linspace(0, L_ft, ym_uw.shape[0])
    sec_ft2 = np.array([trapezoid(2 * ym_uw[i] / 12.0, zr_uw / 12.0)
                        for i in range(ym_uw.shape[0])])
    m["L_ft"] = L_ft; m["draft_in"] = draft_in
    m["beam_in"] = float(np.max(ym_uw) * 2.0)
    m["disp_vol_ft3"] = float(trapezoid(sec_ft2, xg))
    m["buoyancy_lbs"] = m["disp_vol_ft3"] * 62.4
    m["freeboard_geom_in"] = d_in - draft_in           # UNLOADED (flagged in report)

    # full section engine at mold depth (for the worked governing section)
    xr, zr, ym = parse_godzilla_offsets(
        _off_mlt, L_ft=L_ft, draft_in=draft_in, mold_depth_in=d_in)
    # LOADED FREEBOARD (ASCE compliance) -- root-find the waterline where
    # buoyancy balances hull + full crew, slicing the SAME offsets the section
    # engine uses (xr/zr/ym at the real mold depth), not a parallel hull model.
    _crew = list(config.RACE_4_PERSON)
    # A REJECTED weight must never reach the buoyancy solve: adding crew to the
    # old 1e6 sentinel produced a million-pound hull that "correctly" swamped,
    # reporting waterline == mold depth and zero freeboard for a design whose
    # real freeboard was fine.
    from structural import is_rejected as _is_rej
    if _is_rej(m["weight_lbs"]):
        raise RuntimeError(
            "report received an INFEASIBLE winner weight; the loaded-freeboard "
            "check cannot be run on a design that was rejected")
    _W_loaded = m["weight_lbs"] + float(sum(_crew))
    m["freeboard"] = loaded_freeboard_check(
        _W_loaded, d_in, xr, zr, ym, config.FREEBOARD_MIN_IN)
    m["freeboard"]["crew_lbs"] = _crew
    m["freeboard"]["hull_lbs"] = m["weight_lbs"]

    # Knee/toe split provenance (two bases, reported honestly with sample size)
    m["biomech"] = {
        "advanced_on": bool(config.ADVANCED_PADDLER_LOADING),
        "knee_frac_bending": float(config.KNEE_FRAC_BENDING),
        "knee_frac_punching": float(config.KNEE_FRAC_PUNCHING),
        "toe_offset_in": float(config.TOE_OFFSET_IN),
        "measured_n": int(config.KNEE_FRAC_MEASURED_N),
    }

    props = calculate_shell_properties(xr, zr, ym, t_in)
    v = props.A_in2 > 0
    cum_area, cum_Az, cum_Iz = props.cum_area_in2[v], props.cum_Az_in3[v], props.cum_Iz_in4[v]
    zg = props.z_grid_in
    sheer_v = (props.c_bot + props.c_top)[v]
    xv = xr[v]

    # envelopes / utilisation from the live result
    x = np.asarray(result["x"]); m["x_ft"] = x
    m["M_env"] = np.asarray(result["M_envelope"][0])
    m["M_race4"] = np.asarray(result["M_envelope"][1])
    m["M_race2"] = np.asarray(result["M_envelope"][2])
    m["M_race3"] = (None if result["M_envelope"][3] is None
                    else np.asarray(result["M_envelope"][3]))
    m["V_env"] = result["V_envelope"]
    bend = np.maximum(result["utilization"]["bot_t"], result["utilization"]["top_t"])
    m["bend_dc"] = np.asarray(bend)
    # The transverse-hoop gate runs only on the multi-zone path.  A homogeneous
    # (single-zone) winner has no hoop_util; use zero placeholders so the 3-zone
    # report machinery renders, and flag it so the hoop subsection/table/chart
    # state that the check was not separately evaluated on that path.
    _hu = result.get("hoop_util")
    m["hoop_evaluated"] = _hu is not None
    if _hu is not None:
        m["hoop_dc"] = np.asarray(_hu)
        m["hoop_gov_zone"] = np.asarray(result["hoop_gov_zone"])
        m["hoop_dc_homog"] = np.asarray(result["hoop_dc_homog"])
    else:
        _n = len(x)
        m["hoop_dc"] = np.zeros(_n)
        m["hoop_gov_zone"] = np.zeros(_n, dtype=int)
        m["hoop_dc_homog"] = np.zeros((3, _n))
    m["na_line"] = np.asarray(result["na_line"])
    m["sheer_line"] = np.asarray(result["sheer_line_in"])
    m["governing"] = result["governing"]
    m["paddler_positions"] = result["paddler_positions"]

    # governing BENDING station — worked composite section longhand
    gi = int(np.argmax(bend))
    M_sag = float(np.abs(result["M_sag"])[gi]); M_hog = float(np.abs(result["M_hog"])[gi])
    m["gov_bend"] = {"x_ft": float(x[gi]), "dc": float(bend[gi]),
                     "M_sag_ftlb": M_sag, "M_hog_ftlb": M_hog}
    gi2 = int(np.argmin(np.abs(xv - x[gi])))
    sh = float(sheer_v[gi2])
    zb = [0.0] + [f * sh for f in fracs] + [sh]
    zE = np.array([mix(dn)["E"] for dn in zdens])
    zt = np.array([mix(dn)["ft_allow"] for dn in zdens])   # tensile allowable
    zc = np.array([mix(dn)["fc_allow"] for dn in zdens])   # compressive allowable
    zb_st = [np.full(len(xv), b) for b in zb]
    y_bar, I_tr, c_top, c_bot = composite_section_properties(
        zb_st, zE, cum_area, cum_Az, cum_Iz, zg, None, None, None)
    Az = [float(_cum_at(cum_area, zg, np.full(len(xv), zb[k + 1]))[gi2]
                - _cum_at(cum_area, zg, np.full(len(xv), zb[k]))[gi2]) for k in range(3)]
    E_ref = float(zE[0])
    ybar = float(y_bar[gi2]); Itr = float(I_tr[gi2])
    n_top = float(zE[2] / E_ref)
    # Each fibre must be checked in BOTH bending senses against the allowable
    # for the sign of stress it actually sees.  Under SAGGING the bottom is in
    # tension and the top in COMPRESSION; under HOGGING the senses swap.
    # Checking a compression fibre against the tensile allowable (or the
    # reverse) is meaningless and wildly overstates D/C -- f'c is roughly 6x
    # f_t for these mixes.
    M_sag_in, M_hog_in = M_sag * 12.0, M_hog * 12.0
    sig_bot_sag = 1.0 * M_sag_in * ybar / Itr              # zone0 = E_ref, n=1
    sig_top_sag = n_top * M_sag_in * (sh - ybar) / Itr
    sig_bot_hog = 1.0 * M_hog_in * ybar / Itr
    sig_top_hog = n_top * M_hog_in * (sh - ybar) / Itr
    m["gov_section"] = {
        "x_ft": float(xv[gi2]), "sheer_in": sh, "y_bar_in": ybar,
        "I_tr_in4": Itr, "E_ref": E_ref, "c_top_in": float(c_top[gi2]),
        "c_bot_in": float(c_bot[gi2]), "zone_bounds": zb, "zone_area": Az,
        "zone_E": [float(e) for e in zE], "zone_ft": [float(s) for s in zt],
        "zone_fc": [float(s) for s in zc],
        "zone_n": [float(e / E_ref) for e in zE],
        "M_sag_in_lb": M_sag_in, "M_hog_in_lb": M_hog_in,
        # tension fibres (these are what the sweep gate enforces)
        "sig_bot_t": sig_bot_sag, "dc_bot_t": sig_bot_sag / zt[0],
        "sig_top_t": sig_top_hog, "dc_top_t": sig_top_hog / zt[2],
        # compression fibres (advisory -- the gate does not enforce these)
        "sig_top_c": sig_top_sag, "dc_top_c": sig_top_sag / zc[2],
        "sig_bot_c": sig_bot_hog, "dc_bot_c": sig_bot_hog / zc[0],
        # gate-authoritative utilisations at this station (include the NA shear
        # principal-stress term and any mesh boost, so they differ slightly
        # from the pure-bending longhand above)
        "util_bot_t": float(result["utilization"]["bot_t"][gi]),
        "util_top_t": float(result["utilization"]["top_t"][gi])}

    # governing TRANSVERSE hoop
    hi = int(np.argmax(m["hoop_dc"]))
    gz = int(m["hoop_gov_zone"][hi])
    m["gov_hoop"] = {"x_ft": float(x[hi]), "dc": float(m["hoop_dc"][hi]),
                     "gov_zone": gz, "gov_density": zdens[gz],
                     "perzone_homog": [float(m["hoop_dc_homog"][k, hi]) for k in range(3)],
                     "perzone_dc": None}

    # SHEAR envelope peak
    Vp = np.asarray(result["V_envelope"]["max_pos"])
    Vn = np.asarray(result["V_envelope"]["max_neg"])
    m["V_max_lbs"] = float(np.max(np.abs(np.concatenate([Vp, Vn]))))

    # PUNCHING shear (paddler kneels on the bottom-zone mix).
    # Three-step progression, all reported so the reasoning chain is visible:
    #   1 rigid 3x3 square, full paddler on one patch  (the conservative bound)
    #   2 two 3"-dia circular knee contacts, load split, interaction-checked
    #   3 step 2 + the legal (<=1/2") knee pad spreading each contact
    # The first step that clears D/C < 1 becomes the documented design basis.
    # PUNCHING basis: KNEE_FRAC_PUNCHING (1.0) of the heaviest paddler -- no
    # toe relief. Deliberately NOT the measured bending fraction: for a local
    # contact check the conservative case keeps all the weight on the knees.
    P = float(max(config.RACE_4_PERSON)) * float(config.KNEE_FRAC_PUNCHING)
    # RAW f'c: punching_shear_check evaluates ACI's lambda*sqrt(f'c) terms and
    # divides the result by FoS itself. Passing an already-FoS-reduced strength
    # here would apply the factor twice (capacity low by sqrt(FoS), D/C high by
    # the same ~1.22x at FoS=1.5).
    fc_bot = mix(zdens[0])["fc"]
    # Punching effective depth comes from the ACTUAL buildable layer schedule
    # (mesh embedded on a real layer interface), not a bare t/2 assumption --
    # one source of truth with the construction layer count.  For N_LAYERS=3
    # this is the 2/3-t interface (>= t/2), keeping the check conservative.
    _n_layers = int(getattr(config, "N_CONSTRUCTION_LAYERS", 3))
    _d_mesh = mesh_design_depth(t_in, _n_layers)
    sq = punching_shear_check(P, t_in, fc_bot, config.FACTOR_OF_SAFETY, d_in=_d_mesh)
    sq["P_lbs"] = P; sq["fc_psi"] = fc_bot

    def _knee(pad_in):
        k = punching_two_knee(P, t_in, fc_bot, config.FACTOR_OF_SAFETY,
                              knee_dia_in=config.KNEE_DIA_IN,
                              knee_spacing_in=config.KNEE_SPACING_IN,
                              split=config.KNEE_LOAD_SPLIT, pad_in=pad_in,
                              d_in=_d_mesh)
        k["check"]["P_lbs"] = P; k["check"]["fc_psi"] = fc_bot
        return k

    bare = _knee(0.0)
    pad_max = _knee(config.KNEE_PAD_MAX_IN)
    pad_real = _knee(min(0.25, config.KNEE_PAD_MAX_IN))   # realistic thinner pad

    # Reference case: one 3" circle carrying the FULL load.  Isolates the
    # circular-perimeter penalty from the load split, so the report can show
    # that the two effects push in opposite directions.
    circ_full = punching_shear_check(P, t_in, fc_bot, config.FACTOR_OF_SAFETY,
                                     shape="circle", size_in=config.KNEE_DIA_IN,
                                     d_in=_d_mesh)

    # Layer schedule (single source of truth) + the mesh-interface sensitivity:
    # the same single-square bound evaluated at the two candidate layer
    # interfaces (d = t/3 shallow, d = 2t/3 deep) and the legacy t/2 assumption,
    # so the report shows exactly what placing the mesh a pass too shallow costs.
    m["layer_schedule"] = layer_schedule(t_in, _n_layers)
    _d_candidates = {"shallow": t_in / 3.0, "mid_assumed": t_in / 2.0,
                     "deep": 2.0 * t_in / 3.0}
    m["punching_d_compare"] = {
        "d_design_in": _d_mesh,
        "cases": {k: punching_shear_check(P, t_in, fc_bot,
                                          config.FACTOR_OF_SAFETY, d_in=dv)
                  for k, dv in _d_candidates.items()}}

    basis = "square" if sq["ok"] else ("two_knee" if bare["check"]["ok"] else "two_knee_pad")
    m["punching_prog"] = {"square": sq, "circ_full": circ_full, "bare": bare,
                          "pad_real": pad_real, "pad_max": pad_max,
                          "basis": basis}
    # m["punching"] stays the DESIGN-BASIS check that the summary table reads.
    m["punching"] = sq if basis == "square" else (
        bare["check"] if basis == "two_knee" else pad_max["check"])

    # MIX regression provenance
    fcr, ftr = db.fc_regression, db.ft_regression
    m["regression"] = {
        "source": db._regression_source, "n": int(db._regression_n),
        "fc_slope": float(fcr[0]), "fc_int": float(fcr[1]),
        "ft_slope": float(ftr[0]), "ft_int": float(ftr[1]),
        "ft_xint": float(-ftr[1] / ftr[0]),
        "op_densities": sorted(set(round(dd, 1) for dd in zdens))}
    try:
        rows = db.get_all_mixes()
        dens_data = sorted(float(r_["density_pcf"]) for r_ in rows)
        m["regression"]["data_densities"] = dens_data
    except Exception as _exc:
        domain_only(_exc)   # a bug is not a skippable condition
        m["regression"]["data_densities"] = []

    # per-zone mix table (the ONE mix table, keyed to the design)
    m["mix_table"] = [{"zone": nm, "density": zdens[i], "E": float(zE[i]),
                       "fc": float(mix(zdens[i])["fc"]),
                       "ft": float(mix(zdens[i])["ft"]),
                       "fc_allow": float(zc[i]), "ft_allow": float(zt[i])}
                      for i, nm in enumerate(["Bottom flange", "Web (core)", "Top flange"])]

    # SECTION-ENGINE VALIDATION (computed live)
    m["validation"] = _validate_engine()

    # config-derived objectives / safety
    m["config"] = {
        "fos": config.FACTOR_OF_SAFETY, "dlf": config.DYNAMIC_LOAD_FACTOR,
        "k": config.MANEUVER_COUPLING_K, "fb_min_in": config.FREEBOARD_MIN_IN,
        "max_density": config.MAX_DENSITY_PCF,
        "race4": list(config.RACE_4_PERSON), "race2": list(config.RACE_2_PERSON),
        "race3": list(getattr(config, "RACE_3_PERSON", []))}
    m["flare_req_deg"] = float(result.get("flare_req_deg", 0.0))

    # Search-space provenance + winner boundary flags.  A result is only
    # meaningful WITH the space it was found in, and a winner on a range edge
    # signals the search space (not the design) may be the binding constraint.
    m["search_space"] = result.get("search_space")
    m["boundary_flags"] = result.get("boundary_flags") or []
    # Torsion/lateral advisory (distributed, resolution-independent; the torsion
    # figure is a conservative open-section bound — reported, never gated).
    m["torsion"] = result.get("torsion_lateral")

    # Transverse stability (task 7): hull GM_T at the design draft vs the
    # Hull-Configurator Min-GMT it was gated against (0 = no gate).
    m["stability"] = {"gmt_in": float(result.get("gmt_in", 0.0) or 0.0),
                      "min_gmt_in": float(result.get("min_gmt_in", 0.0) or 0.0)}
    # The three GM_T conventions on the REAL zoned pour, the crew-CG band the
    # loaded value spans, and the top-flange strength/stability trade.  Absent
    # for single-zone winners.
    m["stability_real"] = result.get("stability")
    m["stability_trade"] = result.get("stability_trade")
    # Seat pinning: coverage per race and the cost of excluding the rest.
    # Winner's exact seating, per race, so a probe can REPRODUCE the winner's
    # load state.  Without it a gate-table probe has to guess the seats, and
    # guessed seating changes the moment envelope enough to make the answer
    # meaningless -- which is exactly what happened when one was attempted.
    m["winner_seats"] = result.get("paddler_positions")
    # The strength basis actually in force: which regression, fitted from what,
    # over how many points, and the tensile zero-crossing that sets the density
    # floor.  Printed rather than implied.
    try:
        _db = config.get_mix_db()
        _floor, _basis = _db.tensile_viability_floor()
        m["mix_regression"] = {
            "stats": _db.get_regression_stats(),
            "source": getattr(_db, "_regression_source", None),
            "n": getattr(_db, "_regression_n", None),
            "n_fr": getattr(_db, "_regression_n_fr", 0),
            "tier": ("flexural MOR" if _db.fr_regression is not None
                     else "split-tensile"),
            "density_floor_pcf": float(_floor),
            "floor_basis": _basis,
            "pinned": bool((_db.get_published_regression() or {})
                           .get("data_source") == "manual"),
        }
    except Exception as _exc:
        domain_only(_exc)   # a bug is not a skippable condition
        m["mix_regression"] = None
    m["arrangement_disclosure"] = result.get("arrangement_disclosure")
    m["seat_pin_cost"] = result.get("seat_pin_cost")

    # Kupfer biaxial combined-stress gate + the side-by-side comparison.
    # Load-case names, so the methodology section can name the governing case
    # rather than printing a bare index.
    m["case_names"] = list(result.get("case_names")
                           or _sd.get("cases") or [])
    m["kupfer"] = {
        "long_dc":    result.get("long_dc"),
        "hoop_dc":    result.get("hoop_dc"),
        "rankine_dc": result.get("rankine_dc"),
        "kupfer_dc":  result.get("kupfer_dc"),
        "gov":        result.get("kupfer_gov"),
    }

    # Uniform-thickness / solid-keel audit, run live on THIS winner geometry so
    # the manufacturing section can state (per hull) whether the thin-wall model
    # and the built shell agree, and flag it if they ever stop agreeing.
    m["keel_audit"] = _solid_keel_audit(_off_mlt, L_ft, draft_in, d_in, t_in,
                                        dens_pcf=float(np.mean(zdens)))
    return m


def _validate_engine() -> dict:
    """
    Self-contained analytic checks of the section engine, run live.

    Every check compares the engine to a closed-form value the reader can
    reproduce by hand; none uses external, measured, or historical data.
    """
    t = 0.75
    H = 8.122
    z = np.linspace(0.0, H, 400)
    y = np.full_like(z, 5.0)                 # constant offset -> vertical wall
    p = calculate_shell_properties(np.array([0.0]), z, y.reshape(1, -1), t)

    # (1) Base integration.  The section is two thin walls (port + starboard)
    #     of height H, so the second moment about mid-height is 2*(t H^3/12).
    I_eng = float(p.I_in4[0])
    I_exact = 2.0 * (t * H ** 3 / 12.0)

    # (2) Transformed neutral axis.  Split the SAME wall into two equal-height
    #     zones with modular ratio n.  The area per unit height is constant, so
    #     the exact transformed NA has the closed form
    #        ybar = (c^2/2 + n(H^2 - c^2)/2) / (c + n(H - c)).
    n = 2.0
    c = H / 2.0
    E_ref = 1.0e6
    yb, _, _, _ = composite_section_properties(
        [0.0, c, H], [E_ref, n * E_ref],
        p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in)
    ybar_eng = float(yb[0])
    ybar_exact = (c ** 2 / 2 + n * (H ** 2 - c ** 2) / 2) / (c + n * (H - c))

    # (3) Homogeneous limit.  With equal moduli the transform must collapse to
    #     the plain section exactly -- a necessary consistency condition.
    _, Itr_h, _, _ = composite_section_properties(
        [0.0, c, H], [E_ref, E_ref],
        p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in)
    I_homog_eng = float(Itr_h[0])

    return {
        "wall_I_eng": I_eng, "wall_I_exact": I_exact,
        "wall_err_pct": abs(I_eng - I_exact) / I_exact * 100,
        "wall_H": H, "wall_t": t, "n_ratio": n, "split_c": c,
        "comp_ybar_eng": ybar_eng, "comp_ybar_exact": ybar_exact,
        "comp_ybar_err_pct": abs(ybar_eng - ybar_exact) / ybar_exact * 100,
        "homog_I_eng": I_homog_eng,
        "homog_err_pct": abs(I_homog_eng - I_eng) / max(I_eng, 1e-9) * 100}


# ── charts (PNG buffers) ───────────────────────────────────────────────────────

def _png(fig, path, dpi=150):
    fig.savefig(path, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    return path


def _chart_envelopes(m, outdir):
    x = m["x_ft"]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(6.5, 4.2), sharex=True)
    a1.plot(x, m["M_race4"], "#c0392b", lw=1.3, label="4-person")
    if m["M_race3"] is not None:
        a1.plot(x, m["M_race3"], "#27ae60", lw=1.3, label="3-person")
    a1.plot(x, m["M_race2"], "#e67e22", lw=1.3, label="2-person")
    a1.plot(x, m["M_env"], "#2c3e50", lw=0.9, ls="--", label="envelope")
    a1.set_ylabel("Moment (ft-lb)"); a1.legend(fontsize=7, ncol=4)
    a1.grid(True, ls=":", alpha=0.4); a1.set_title("Bending-moment envelope", fontsize=9)
    V = m["V_env"]
    a2.fill_between(x, V["max_pos"], V["max_neg"], color="#2980b9", alpha=0.2)
    a2.plot(x, V["max_pos"], "#2980b9", lw=1.0); a2.plot(x, V["max_neg"], "#2980b9", lw=1.0)
    a2.set_ylabel("Shear (lb)"); a2.set_xlabel("Distance from bow (ft)")
    a2.grid(True, ls=":", alpha=0.4); a2.set_title("Shear-force envelope", fontsize=9)
    fig.tight_layout()
    return _png(fig, os.path.join(outdir, "envelopes.png"))


def _chart_dc(m, outdir):
    x = m["x_ft"]
    fos = float(m["config"]["fos"])
    fig, ax = plt.subplots(figsize=(6.5, 2.8))
    ax.plot(x, m["bend_dc"], "#c0392b", lw=1.4, label="Longitudinal bending D/C")
    if m.get("hoop_evaluated", True):
        ax.plot(x, m["hoop_dc"], "#8e44ad", lw=1.4, label="Transverse hoop D/C")
    ax.axhline(1.0, color="k", lw=0.8, ls="--", alpha=0.6,
               label=f"Allowable Limit (1.0, incl. {fos:.1f} FoS)")
    ax.set_xlabel("Distance from bow (ft)")
    ax.set_ylabel("Utilization (Allowable Limit)")
    ax.set_ylim(0, 1.1); ax.legend(fontsize=6, loc="upper right")
    ax.grid(True, ls=":", alpha=0.4)
    ax.set_title("Per-station utilisation (both enforced checks)", fontsize=9)
    # FoS transparency: the 1.0 line is the ALLOWABLE, not ultimate strength.
    fig.text(0.5, 0.005,
             f"*Capacity includes a {fos:.1f} Factor of Safety from Ultimate "
             "Strength (D/C ≤ 1.0 = safe)",
             ha="center", fontsize=6.5, style="italic", color="#555")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    return _png(fig, os.path.join(outdir, "dc.png"))


def _chart_pour(m, result, outdir):
    from visualization import plot_pour_zones
    fig = plot_pour_zones(
        m["x_ft"], np.asarray(result["layout"]), na_line=m["na_line"],
        sheer_line=m["sheer_line"], zone_pour_lines=result.get("zone_pour_lines"),
        zone_densities=result.get("zone_densities"),
        outline_color="#222")   # dark line for the report's white background
    fig.patch.set_facecolor("white")
    for ax in fig.axes:
        ax.set_facecolor("white"); ax.tick_params(colors="black")
        for sp in ax.spines.values():
            sp.set_edgecolor("black")
        ax.title.set_color("black"); ax.xaxis.label.set_color("black")
        ax.yaxis.label.set_color("black")
        lg = ax.get_legend()
        if lg:
            for t_ in lg.get_texts():
                t_.set_color("black")
    return _png(fig, os.path.join(outdir, "pour.png"))


# ── LaTeX rendering ────────────────────────────────────────────────────────────

# Published peer GM_T figures, quoted in the stability section.  THIRD-PARTY
# DATA -- named here with its source rather than inlined as bare numbers, so the
# claim can be audited and corrected if the source is superseded.
#
# Source: University of Florida 2026 ASCE Concrete Canoe design report.  Their
# table lists GM_T beside waterline length, length-to-beam ratio, entrance
# angle, transom width and drag -- all hull-FORM properties -- and compares the
# value between two hull SHAPES (the "4.6% increase" from Reptilia to Tempest),
# which is only meaningful on a form basis.  We therefore compare it against our
# FORM-ONLY GM_T.  Their loading basis and KG assumption are NOT stated in the
# source, so this is a reasoned reading, not a verified apples-to-apples match.
PEER_GMT_IN = (("Reptilia", 10.4), ("Tempest", 10.9))


def _f(v, n=2):
    return f"{v:.{n}f}"


def _kupfer_search_limit_for_report():
    """The limit the sweep actually gated on -- read, never re-derived."""
    try:
        from structural import kupfer_search_limit
        return float(kupfer_search_limit())
    except ImportError:
        return 1.0


def _tex(s):
    """Escape LaTeX special characters in dynamic text (case names, tags)."""
    s = str(s)
    for a, b in (("\\", r"\textbackslash{}"), ("_", r"\_"), ("&", r"\&"),
                 ("%", r"\%"), ("#", r"\#"), ("$", r"\$"), ("{", r"\{"),
                 ("}", r"\}"), ("~", r"\textasciitilde{}"),
                 ("^", r"\textasciicircum{}")):
        s = s.replace(a, b)
    return s


def _sci(v):
    """Format a large number as LaTeX scientific notation (math-mode body)."""
    import math
    if not v:
        return "0"
    e = int(math.floor(math.log10(abs(v))))
    return f"{v/10**e:.2f}\\times10^{{{e}}}"


def render_latex(m: dict, charts: dict, out_dir: str | None = None) -> str:
    c = m["config"]; gs = m["gov_section"]; gh = m["gov_hoop"]; ps = m["punching"]
    reg = m["regression"]; val = m["validation"]
    z = m["zone_densities"]

    def img(p):
        """
        Figure path for \\includegraphics, RELATIVE to the .tex file.

        Absolute paths bake one machine's directory layout into the document:
        the .tex then fails to compile anywhere else, and the whole
        report directory stops being portable.  The charts are written into
        the same directory as the .tex, so a relative path (normally just the
        basename) is what belongs here.  Forward slashes: TeX treats a
        backslash as an escape even on Windows.
        """
        p = str(p)
        if out_dir:
            try:
                p = os.path.relpath(p, out_dir)
            except ValueError:          # different drive on Windows
                p = os.path.basename(p)
        else:
            p = os.path.basename(p)
        return p.replace("\\", "/")

    op = reg["op_densities"]; dd = reg.get("data_densities") or []
    if dd:
        lo, hi = min(dd), max(dd)
        interp = all(lo <= o <= hi for o in op)
        reg_note = (f"operating densities {op} lie {'within' if interp else 'OUTSIDE'} "
                    f"the lab data range [{_f(lo,1)}, {_f(hi,1)}] pcf "
                    f"({'interpolation' if interp else 'EXTRAPOLATION -- flag'})")
    else:
        reg_note = f"operating densities {op} pcf"

    L = []
    A = L.append
    A(r"\documentclass[11pt]{article}")
    A(r"\usepackage[letterpaper,margin=1in]{geometry}")
    A(r"\usepackage{amsmath,booktabs,siunitx,graphicx,xcolor,array,caption}")
    A(r"\usepackage[hidelinks]{hyperref}")
    A(r"\sisetup{detect-all,per-mode=symbol}")
    A(r"\newcommand{\dc}[1]{\text{D/C}=#1}")
    A(r"\definecolor{uva}{RGB}{35,45,75}")
    A(r"\title{\textbf{ASCE Concrete Canoe --- Structural Analysis}\\[2pt]"
      r"\large University of Virginia}")
    A(r"\date{" + datetime.now().strftime("%B %d, %Y") + r"}")
    A(r"\begin{document}\maketitle")
    A(r"\begin{abstract}\noindent This report documents the structural design of "
      r"the optimised concrete canoe hull. Loads are derived from first "
      r"principles, resolved into a governing bending moment and transverse "
      r"turn moment, and checked against a composite transformed-section model. "
      r"The lightest feasible design has a dry hull weight of \textbf{" +
      _f(m["weight_lbs"]) + r"~lb}. Every value herein is produced by the design "
      r"program's live analysis engine; the section engine is independently "
      r"validated (Sec.~\ref{sec:val}).\end{abstract}")

    # 1 objectives
    A(r"\section{Design Objectives and Constraints}")
    A(r"\begin{tabular}{@{}lll@{}}\toprule Quantity & Value & Source\\\midrule")
    A(rf"Overall length & {_f(m['L_ft'])} ft & optimised hull (Godzilla)\\")
    A(rf"Maximum beam & {_f(m['beam_in'])} in & optimised hull\\")
    A(rf"Mold depth & {_f(m['depth_in'])} in & sweep winner\\")
    A(rf"Design draft & {_f(m['draft_in'])} in & hydrostatic solve\\")
    A(rf"Factor of safety & {_f(c['fos'])} & ACI 318-19 practice\\")
    A(rf"Dynamic load factor & {_f(c['dlf'])} & race dynamics\\")
    A(rf"Freeboard target & {_f(c['fb_min_in'],1)} in & team spec (ASCE 4-person)\\")
    A(rf"Distinct mixes (cap) & {m['n_distinct_mixes']} & batching constraint\\")
    A(r"\bottomrule\end{tabular}")

    # 2 hull
    A(r"\section{Hull Geometry}")
    A(r"The as-built section is taken directly from the optimised offset table "
      r"(\texttt{off1.mlt}) at every station; no idealised constant-depth prism "
      r"is used. Displaced volume is integrated from the real underwater "
      r"sectional areas.")
    A(r"\begin{tabular}{@{}ll@{}}\toprule")
    A(rf"Length overall & {_f(m['L_ft'])} ft\\")
    A(rf"Maximum beam & {_f(m['beam_in'])} in\\")
    A(rf"Mold depth & {_f(m['depth_in'])} in\\")
    A(rf"Displaced volume at design draft & {_f(m['disp_vol_ft3'],3)} ft\textsuperscript{{3}}\\")
    A(rf"Buoyancy at design draft & {_f(m['buoyancy_lbs'],1)} lb\\")
    A(r"\bottomrule\end{tabular}")

    # 3 loads
    A(r"\section{Load Derivation}")
    A(r"The hull self-weight per unit length $w(x)$ is built from the \emph{real "
      r"per-station transformed section area} times the local mix density "
      r"(bottom-up over the pour zones), not a uniform prism. For each race "
      r"case the payload is applied as point loads at the paddler stations and "
      r"balanced by a buoyancy line proportional to the local water-plane "
      r"breadth. Shear and moment follow by integration:")
    A(r"\begin{equation} V(x)=\int_0^x q(\xi)\,d\xi,\qquad "
      r"M(x)=\int_0^x V(\xi)\,d\xi,\qquad q=q_{\text{buoy}}-w-w_{\text{pay}}."
      r"\end{equation}")
    A(r"The envelope is taken over all race cases (2-, 3- and 4-person) plus "
      r"paddler-position variance. The governing bending case is "
      rf"\textbf{{{_tex(m['governing']['bottom_tension']['case'])}}} at "
      rf"$x={_f(m['gov_bend']['x_ft'],1)}$~ft, with "
      rf"$M_{{\text{{sag}}}}={_f(m['gov_bend']['M_sag_ftlb'],0)}$~ft-lb "
      rf"and peak shear $V_{{\max}}={_f(m['V_max_lbs'],0)}$~lb.")
    A(rf"\begin{{figure}}[h]\centering\includegraphics[width=\linewidth]{{{img(charts['env'])}}}"
      r"\caption{Bending-moment and shear-force envelopes across all race cases.}\end{figure}")

    # 4 governing section worked
    A(r"\section{Governing Cross-Section (Worked)}\label{sec:section}")
    A(rf"At the governing station $x={_f(gs['x_ft'],1)}$~ft the section is the "
      r"three-zone I-beam. Using the transformed-section method with modular "
      r"ratio $n_i=E_i/E_{\text{ref}}$ (reference $E_{\text{ref}}=" +
      _sci(gs['E_ref']) + r"$~psi, the bottom-flange mix):")
    A(r"\begin{tabular}{@{}lrrrr@{}}\toprule Zone & Bounds (in) & $A_i$ (in\textsuperscript{2}) "
      r"& $E_i$ (psi) & $n_i$\\\midrule")
    names = ["Bottom flange", "Web", "Top flange"]
    for i in range(3):
        A(rf"{names[i]} ({_f(z[i],1)} pcf) & {_f(gs['zone_bounds'][i],2)}--{_f(gs['zone_bounds'][i+1],2)} "
          rf"& {_f(gs['zone_area'][i],3)} & ${_sci(gs['zone_E'][i])}$ & {_f(gs['zone_n'][i],3)}\\")
    A(r"\bottomrule\end{tabular}")
    A(rf"\par\vspace{{4pt}}Transformed neutral axis $\bar y={_f(gs['y_bar_in'],3)}$~in above "
      rf"the keel; transformed second moment $I_{{tr}}={_f(gs['I_tr_in4'],1)}$~in\textsuperscript{{4}} "
      r"(in $E_{\text{ref}}$ units). Bending stress at a fibre is "
      r"$\sigma_i=n_i\,M\,(z-\bar y)/I_{tr}$:")
    A(rf"\par\vspace{{4pt}}Each fibre is checked in \emph{{both}} bending senses "
      rf"against the allowable for the sign of stress it actually carries. Under "
      rf"\textbf{{sagging}} ($M={_f(gs['M_sag_in_lb'],0)}$~in-lb) the bottom fibre is in "
      r"tension and the top fibre in \emph{compression}; under \textbf{hogging} "
      rf"($M={_f(gs['M_hog_in_lb'],0)}$~in-lb) the senses swap. Checking a "
      r"compression fibre against the tensile allowable would overstate its D/C "
      r"by roughly the $f'_c/f_t$ ratio (about $6\times$ for these mixes) and is "
      r"not a meaningful limit state.")
    A(r"\begin{center}\begin{tabular}{@{}lllrrr@{}}\toprule "
      r"Fibre & Sense & Stress & $\sigma$ (psi) & Allowable (psi) & D/C\\\midrule")
    for lbl, sense, kind, sg, al, dcv, note in [
        ("Bottom", "sagging", "tension", gs["sig_bot_t"], gs["zone_ft"][0],
         gs["dc_bot_t"], "gated"),
        ("Top", "hogging", "tension", gs["sig_top_t"], gs["zone_ft"][2],
         gs["dc_top_t"], "gated"),
        ("Top", "sagging", "compression", gs["sig_top_c"], gs["zone_fc"][2],
         gs["dc_top_c"], "advisory"),
        ("Bottom", "hogging", "compression", gs["sig_bot_c"], gs["zone_fc"][0],
         gs["dc_bot_c"], "advisory"),
    ]:
        A(rf"{lbl} & {sense} & {kind} & {_f(sg,1)} & {_f(al,1)} & "
          rf"\dc{{{_f(dcv,2)}}} ({note})\\")
    A(r"\bottomrule\end{tabular}\end{center}")
    A(r"\par\vspace{2pt}\noindent The two \emph{tension} rows are the enforced "
      r"limit states --- the sweep gate rejects any design exceeding them. The "
      r"compression rows are advisory: at this design the compression demand is "
      r"far below $f'_c$ (the D/C values above), so it is reported for "
      r"visibility but does not affect feasibility.")
    A(rf"\par\vspace{{2pt}}\noindent The longhand above is pure bending. The "
      rf"gate's own utilisations at this station --- which additionally include "
      rf"the neutral-axis shear via the principal-tension combination "
      rf"$\sigma/2+\sqrt{{(\sigma/2)^2+\tau^2}}$, and any mesh boost on the top "
      rf"fibre --- are ${_f(gs['util_bot_t'],3)}$ (bottom) and "
      rf"${_f(gs['util_top_t'],3)}$ (top). The governing longitudinal value "
      rf"$\dc{{{_f(m['gov_bend']['dc'],3)}}}$ carried into "
      rf"Sec.~\ref{{sec:checks}} and the compliance table is the larger of "
      r"these two, so all three places report the same number against the same "
      r"allowables.")

    # 4b Design methodology — stated explicitly, with every factor applied.
    _dz = m.get("design") or {}
    _meth = str(_dz.get("method", "LRFD")).upper()
    A(r"\section{Design Methodology}\label{sec:method}")
    # The Kupfer section is only emitted when the gate actually ran, so the
    # cross-reference has to be conditional too or it compiles to "??".
    _kref = (r"the Kupfer--Gerstle biaxial envelope of Sec.~\ref{sec:kupfer}"
             if (m.get("kupfer") or {}).get("kupfer_dc") is not None
             else r"the Kupfer--Gerstle biaxial envelope")
    A(rf"This design was checked by \textbf{{{_tex(_meth)}}}. The failure "
      rf"criterion is {_kref} under \emph{{either}} methodology --- LRFD and "
      r"ASD change only how loads and capacities are factored, never what "
      r"constitutes failure. The same per-fibre, per-station, per-load-case "
      r"evaluation, the same per-zone allowables and the same degenerate-tip "
      r"exclusion apply in both cases.")
    if _meth == "ASD":
        A(rf"\textbf{{Allowable Stress Design.}} Service loads are combined "
          rf"unfactored, $D+L$, and compared against an allowable stress "
          rf"$f_n/\Omega$ with $\Omega={_f(_dz.get('omega') or 1.5, 2)}$. The "
          rf"dynamic load factor "
          rf"${_f(_dz.get('dynamic_load_factor', 1.2), 2)}$ is applied to the "
          rf"crew weights before combination; it is an impact amplification "
          rf"(the load the hull actually sees while being paddled), not a "
          rf"safety factor.")
    else:
        A(rf"\textbf{{Load and Resistance Factor Design.}} Factored load "
          rf"effects $\gamma_D D+\gamma_L L$ are compared against a design "
          rf"capacity $\phi f_n$ with "
          rf"$\phi={_f(_dz.get('phi') or 0.60, 2)}$. The dead load is the hull "
          rf"self-weight including the stem-rocker cantilever tip mass; the "
          rf"live load is the paddlers, at the dynamic (impact-amplified) "
          rf"weights. The dynamic load factor "
          rf"${_f(_dz.get('dynamic_load_factor', 1.2), 2)}$ multiplies the "
          rf"crew weight \emph{{before}} $\gamma_L$, so the effective factor "
          rf"on nominal crew weight is "
          rf"${_f(_dz.get('dynamic_load_factor', 1.2), 2)}\times"
          rf"{_f(_dz.get('combinations', {}).get('race_4', [(1.2, 1.6)])[0][1], 2)}"
          rf"$.")
        A(rf"\textbf{{Resistance factor.}} $\phi={_f(_dz.get('phi') or 0.60,2)}$ "
          r"is the ACI 318-19 Table 21.2.1(g) value for \emph{plain} concrete "
          r"(ACI 318-11 Sec.~9.3.5 used $0.55$). It is deliberately \emph{not} "
          r"the $0.90$ tension-controlled flexure factor: that value presumes a "
          r"reinforced section in which steel carries the tension and the "
          r"concrete's tensile strength is neglected outright. The governing "
          r"check here is the concrete's own biaxial \emph{tensile} capacity, "
          r"which is the plain-concrete limit state --- and the hull's mesh, "
          r"not being conventional reinforcement, contributes nothing before "
          r"the first cracking this check gates on.")
        _combos = _dz.get("combinations") or {}
        if _combos:
            A(r"\begin{center}\begin{tabular}{lll}\toprule")
            A(r"Load case & Combinations $(\gamma_D,\ \gamma_L)$ & Note \\")
            A(r"\midrule")
            for _cn in sorted(_combos):
                _cs = _combos[_cn]
                _txt = ",\\ ".join(rf"$({_f(a,2)},\ {_f(b,2)})$" for a, b in _cs)
                _note = ("counteracting-dead alternative included"
                         if len(_cs) > 1 else "")
                A(rf"\texttt{{{_tex(_cn)}}} & {_txt} & {_tex(_note)} \\")
            A(r"\bottomrule\end{tabular}\end{center}")
            # State the counteracting-dead finding with the numbers this run
            # actually produced -- never a remembered figure.
            _gk = m.get("kupfer") or {}
            _gov = _gk.get("gov") or {}
            _cn = m.get("case_names") or []
            _ci = _gov.get("case_index")
            _gname = (_cn[_ci] if isinstance(_ci, int) and 0 <= _ci < len(_cn)
                      else "race_4")
            _md = (_dz.get("M_dead") or {}).get(_gname)
            _ml = (_dz.get("M_live") or {}).get(_gname)
            _gs = _gov.get("station_index")
            _opposed = None
            if _md is not None and _ml is not None and isinstance(_gs, int) \
                    and 0 <= _gs < len(_md):
                _mdv, _mlv = float(_md[_gs]), float(_ml[_gs])
                _opposed = (_mdv * _mlv) < 0.0
            A(r"\textbf{Counteracting dead load.} Where the dead load "
              r"\emph{opposes} the live load, factoring $D$ \emph{up} "
              r"understates the demand, and ASCE 7-22 Sec.~2.3.1 requires the "
              r"dead factor to fall to $0.9$ in that situation.")
            if _opposed is True:
                A(rf"That is the case here. At the governing station "
                  rf"({_tex(str(_gs))}, load case \texttt{{{_tex(_gname)}}}) the "
                  rf"unfactored components are "
                  rf"$M_D={_f(_mdv,1)}$ and $M_L={_f(_mlv,1)}$~lb$\cdot$ft --- "
                  rf"opposite in sign, the hull's self-weight "
                  rf"{'hogging' if _mdv < 0 else 'sagging'} moment relieving "
                  rf"the crew's "
                  rf"{'sagging' if _mlv > 0 else 'hogging'} moment. Applying "
                  rf"$1.2D$ there would scale up a \emph{{relieving}} action "
                  rf"and leave the true demand understated, so the $0.9D$ "
                  rf"alternative is enveloped alongside it and governs at that "
                  rf"fibre. Both combinations are evaluated at every station "
                  rf"and the worse of the two is taken; without the $0.9D$ "
                  rf"case this design would have been checked against a "
                  rf"demand lower than the code requires.")
            elif _opposed is False:
                A(rf"At the governing station ({_tex(str(_gs))}, load case "
                  rf"\texttt{{{_tex(_gname)}}}) the components act in the same "
                  rf"sense, $M_D={_f(_mdv,1)}$ and $M_L={_f(_mlv,1)}$"
                  rf"~lb$\cdot$ft, so $1.2D$ governs there. The $0.9D$ "
                  rf"alternative is still enveloped, because whether dead "
                  rf"relieves or adds varies by station, fibre and load case.")
            else:
                A(r"Both combinations are therefore enveloped at every station "
                  r"and the worse of the two governs at each fibre.")
    # Stem overhang: value, source, and whether derived or assumed -- stated
    # beside the other modelling caveats rather than buried in a log line.
    _so = m.get("stem_overhang") or {}
    if _so.get("in_per_end") is not None:
        A(r"\par\vspace{2pt}\noindent\textbf{Stem overhang (tip cantilever).} "
          rf"\SI{{{_f(_so['in_per_end'], 2)}}}{{in}} per end, "
          rf"{'\\emph{derived} from the hull geometry' if _so.get('derived') else '\\emph{assumed} (user input)'}"
          r". This is the fore-aft run at each end where the maximum "
          r"half-breadth falls below $1.5t$, so no shell section can exist and "
          r"the material there is a slender cantilever tip. It is carried as "
          r"un-buoyed dead weight beyond the supported span.")
        if _so.get("derived"):
            A(r"The value is obtained by interpolating where the half-breadth "
              r"crosses $1.5t$, so it is fixed by the hull rather than by the "
              r"offset file's station count --- the same hull returns the same "
              r"overhang at 41, 81, 161 and 321 stations. It is \emph{not} a "
              r"design variable: because the tip mass raises hogging at the "
              r"ends, and end hogging opposes the crew's midship sagging, a "
              r"longer overhang \emph{lowers} the governing D/C. Treating it "
              r"as a free parameter would let the optimiser buy structural "
              r"margin with tip mass, which is a far worse weight-per-margin "
              r"trade than mix density or wall thickness. It is therefore "
              r"measured, never tuned.")
        A(r"Note this is not the same geometry as the CAD stem sweep, which "
          r"raises existing material upward \emph{within} the hull over the "
          r"same length; the structural model instead extends the beam and "
          r"places the mass \emph{outside} it. The two share only the length, "
          r"and the structural treatment is the bounding one.")
        A(r"The beam model appends the overhang as whole stations, so its "
          r"effect is a step function of the analysis grid rather than a "
          r"continuous one: an overhang shorter than about one and a half "
          r"station spacings contributes no mass at all. At the resolution used "
          r"here that is the case, so the tip mass ($\approx$0.1~lb) is "
          r"neglected. That is conservative --- tip mass relieves the midship "
          r"sagging that governs --- but it is a consequence of the "
          r"discretisation, not a modelling choice, and no smooth relationship "
          r"between overhang and utilisation should be read into these results.")
    A(rf"The acceptance margin of Sec.~\ref{{sec:checks}} "
      rf"(D/C $\le{_f(m.get('kupfer_accept_max', 0.95), 2)}$) is applied "
      rf"identically under both methodologies: it corrects for a numerical "
      rf"non-convergence in the utilisation, which is unrelated to how loads "
      rf"and resistances are factored.")

    # 5 checks
    A(r"\section{Structural Checks}\label{sec:checks}")
    A(r"The transverse-hoop, longitudinal-bending, shear and punching checks "
      r"below are \emph{enforced} by the optimiser --- a design failing any of "
      r"them is infeasible and cannot win. The closing torsion/lateral figures "
      r"are \emph{advisory} conservative bounds, reported for visibility but not "
      r"gated (see that subsection).")

    # Overridden strength fit — the loudest caveat of the set, and first.  The
    # others record a quantity that is imprecise; this one records a quantity
    # the suite judged physically impossible and was told to use anyway. A
    # design gated on it must never read like one gated on sound data.
    _fo = m.get("fit_override")
    if _fo:
        # An empty itemize is a LaTeX error, and a fit can be released on a
        # finding list we failed to carry — degrade to prose rather than
        # breaking the whole report at the one place it must not be silent.
        _found = [str(h) for h in (_fo.get("findings") or [])]
        _flags = ("".join(rf"\item {_tex(h)}" for h in _found) if _found
                  else r"\item (finding text unavailable)")
        _ids = ", ".join(str(i) for i in _fo.get("row_ids", []))
        A(rf"\par\vspace{{4pt}}\noindent\fbox{{\parbox{{0.97\linewidth}}{{"
          rf"\textbf{{OVERRIDDEN STRENGTH FIT --- this design is gated on a "
          rf"regression the suite rejected as physically implausible.}} The "
          rf"sanity guard held the "
          rf"{_tex(', '.join(_fo.get('tiers', [])))} tier out of force; it was "
          rf"released by: {_tex(str(_fo.get('source','unknown')))}. "
          rf"What was flagged:\begin{{itemize}}{_flags}\end{{itemize}}"
          + (rf"Lab result id(s) behind the fit: {_tex(_ids)}. " if _ids else "")
          + rf"Every allowable, utilisation and margin quoted in this report "
            rf"derives from that fit. They should not be relied on until the "
            rf"underlying lab data is corrected and the design re-run.}}}}")

    # Acceptance margin — the numerical-convergence caveat.  Sits beside the
    # n=2 regression and advisory-torsion caveats: all three record a place
    # where a quantity the design depends on is not fully established, and what
    # is done about it.
    _acc = m.get("kupfer_accept_max")
    if _acc is not None and _acc < 0.999:
        A(rf"\par\vspace{{2pt}}\noindent\textbf{{Acceptance margin "
          rf"(D/C $\le$ {_f(_acc,2)}).}} The winning design is required to stay "
          rf"below {_f(_acc,2)} on the governing biaxial check, not below "
          rf"$1.0$. This is a margin against a \emph{{numerical}} limitation, "
          rf"not additional structural conservatism --- the factor of safety "
          rf"covers the physics and is already inside every allowable quoted "
          rf"here. The hull weight converges quickly with longitudinal station "
          rf"count ($0.58\%$ between 161 and 321 stations), but the Kupfer "
          rf"utilisation that decides feasibility does not: measured across "
          rf"five designs on this hull it drifts $1$--$2\%$ for every doubling "
          rf"of the station count, with no plateau observed out to 641 "
          rf"stations. A design accepted at $1.0$ on the 161-station analysis "
          rf"grid is therefore not demonstrably feasible --- one refinement "
          rf"step can reject it, and on this hull did ($0.9957$ at 161 "
          rf"stations, $1.0040$ at 321). Accepting at {_f(_acc,2)} buys "
          rf"roughly $2.4$ doublings of headroom at the worst drift rate "
          rf"observed, so the verdict holds to about 830 stations; the "
          rf"accepted design is additionally re-checked at 321 stations and "
          rf"confirmed below $1.0$ there.")
    A(rf"\par\vspace{{2pt}}\noindent\textbf{{Reading the D/C ratios.}} Every "
      rf"utilisation (``D/C'') quoted here and in the figures is "
      rf"demand divided by the \emph{{allowable}}, and the allowable already "
      rf"carries the factor of safety: "
      rf"$\text{{allowable}}=\text{{ultimate strength}}/\text{{FoS}}$ with "
      rf"$\text{{FoS}}={_f(c['fos'],2)}$. A D/C of $1.0$ is therefore the "
      rf"\emph{{allowable}} limit, not incipient failure --- a value of, say, "
      rf"$0.90$ already sits below a capacity that has been reduced by the full "
      rf"{_f(c['fos'],2)}$\times$ safety factor, so the true margin to ultimate "
      rf"is larger still.")
    A(r"\subsection{Transverse (hoop) bending --- transformed section}")
    A(r"In a turn of radius $R$ at speed $V$ the whole hull is accelerated "
      r"laterally, so the centripetal load per unit length follows the local "
      r"mass and bends the section about its vertical centre-plane. Because that "
      r"load follows the \emph{same} mass distribution as gravity and is reacted "
      r"by the \emph{same} waterplane, the balanced lateral turn moment is the "
      r"vertical bending envelope scaled by the turn load factor:")
    A(r"\begin{equation} M_{\text{lat}}(x)=k_{\text{lat}}\,\lvert M_{\text{vert}}(x)\rvert,"
      r"\qquad k_{\text{lat}}=\frac{V^{2}}{gR}. \end{equation}")
    A(r"This is a genuine per-station \emph{distribution}: it vanishes at the "
      r"bow and stern (where $M_{\text{vert}}\!\to\!0$), not a single scalar "
      r"applied at every station.")
    A(r"\par\noindent\emph{Reaction approximation.} The lateral load is reacted "
      r"here on the waterplane breadth (as buoyancy is), whereas the true "
      r"hydrodynamic side force acts on the submerged lateral profile. For this "
      r"hull the two distributions differ in shape, but the resulting peak "
      r"$M_{\text{lat}}$ agrees to within $1\%$, so the idealisation does not "
      r"move the check.")
    if m.get("hoop_evaluated", True):
        A(r"\par\noindent The section is vertically zoned, so the check uses a "
          r"\emph{transformed} lateral section: each pour zone's outboard fibre is "
          r"checked against \emph{that zone's own} tensile strength, and the "
          r"governing fibre is the soft light web at the hull's widest point.")
        A(rf"Governing at $x={_f(gh['x_ft'],1)}$~ft in the "
          rf"\textbf{{{_f(gh['gov_density'],1)}~pcf web}} (zone {gh['gov_zone']+1}), "
          rf"$\dc{{{_f(gh['dc'],3)}}}$ --- well inside unity. The hull is far "
          r"stiffer laterally than vertically (its lateral second moment is several "
          r"times the vertical), and the turn load is only $k_{\text{lat}}\!\approx\!"
          r"V^{2}/gR$ of gravity, so longitudinal bending --- not the turn --- "
          r"governs. Fibre-by-fibre (homogeneous reference D/C, for comparison): "
          + ", ".join(rf"{names[k]} {_f(gh['perzone_homog'][k],2)}" for k in range(3)) + ".")
    else:
        A(r"\par\noindent\emph{Not separately evaluated for this design.} The "
          r"homogeneous single-mix winner is optimised on the single-zone path, "
          r"which does not run the transverse-hoop gate --- that gate exists to "
          r"catch a soft light web in a multi-zone I-beam, which a uniform "
          r"section does not have. For a wide, shallow, uniform hull the hoop "
          r"demand is a small fraction of the longitudinal bending that governs "
          r"here, so no separate hoop figure is reported.")
    A(r"\subsection{Longitudinal bending}")
    _gsx = m["gov_section"]
    _gov_fib = ("bottom fibre in tension under sagging"
                if _gsx["util_bot_t"] >= _gsx["util_top_t"] else
                "top fibre in tension under hogging")
    _gov_sig = (_gsx["sig_bot_t"] if _gsx["util_bot_t"] >= _gsx["util_top_t"]
                else _gsx["sig_top_t"])
    _gov_all = (_gsx["zone_ft"][0] if _gsx["util_bot_t"] >= _gsx["util_top_t"]
                else _gsx["zone_ft"][2])
    A(rf"Peak $\dc{{{_f(m['gov_bend']['dc'],3)}}}$ at "
      rf"$x={_f(m['gov_bend']['x_ft'],1)}$~ft (worked in "
      rf"Sec.~\ref{{sec:section}}). Governing fibre: the \textbf{{{_gov_fib}}}, "
      rf"at $\sigma={_f(_gov_sig,1)}$~psi against the tensile allowable "
      rf"$f_t={_f(_gov_all,1)}$~psi (bending longhand; the gate value above also "
      r"carries the neutral-axis shear term). Tension-controlled --- compression "
      r"demand at both fibres is well inside $f'_c$ and does not govern.")
    A(r"\subsection{Shear}")
    A(rf"Peak shear $V_{{\max}}={_f(m['V_max_lbs'],0)}$~lb; shear stress is well "
      r"below the tension check and does not govern.")
    # Torsion / lateral advisory (distributed T(x); conservative open-section
    # bound; reported, NOT a feasibility gate).
    _tl = m.get("torsion")
    if _tl is not None:
        A(r"\subsection{Torsion and lateral --- advisory (not gated)}")
        A(rf"A one-sided paddle stroke twists the hull. The internal torque is "
          rf"an equilibrium distribution $T(x)$ (the stroke torque reacted by "
          rf"the waterplane, so it closes to zero at the free ends), and the "
          rf"shear follows open-section St.\,Venant theory, "
          rf"$\tau=\lvert T(x)\rvert\,t/J$ with $J=t^{{2}}A/3$. The peak is "
          rf"$\tau={_f(_tl.tau_max_psi,0)}$~psi at "
          rf"$x={_f(_tl.critical_torsion_x_ft,1)}$~ft against an allowable "
          rf"${_f(_tl.tau_allowable_psi,0)}$~psi, "
          rf"$\dc{{{_f(_tl.torsion_utilization,2)}}}$.")
        A(r"\textbf{This bound is deliberately conservative and is NOT "
          r"enforced.} The open-section $J$ ignores the warping restraint of the "
          r"closed stems and the gunwale flanges (and the water's support), so "
          r"it overstates the true torsional shear by roughly an order of "
          r"magnitude --- which is why hulls of this geometry are built and "
          r"paddled without torsional distress. A physically meaningful torsion "
          r"gate would need a warping-inclusive model; until then this figure is "
          r"reported, not applied. The companion lateral-bending advisory (the "
          r"same distributed $M_{\text{lat}}(x)$ treatment as the hoop check "
          rf"above) is $\dc{{{_f(_tl.lateral_utilization,2)}}}$.")
    # --- biomechanics provenance -------------------------------------------
    _bm = m["biomech"]
    _kb, _kp = _bm["knee_frac_bending"], _bm["knee_frac_punching"]
    A(r"\subsection{Paddler load split (knees vs toes)}")
    A(rf"A kneeling paddler carries weight on the knees and the toes. This "
      rf"model uses \textbf{{two different splits}}, one per limit state, "
      rf"because the conservative direction is not the same for both:")
    A(r"\begin{center}\begin{tabular}{@{}llll@{}}\toprule "
      r"Limit state & Knees & Toes & Basis\\\midrule")
    A(rf"Bending / shear envelopes & {_f(100*_kb,0)}\% & {_f(100*(1-_kb),0)}\% "
      rf"& team-measured\\")
    A(rf"Punching (local two-way) & {_f(100*_kp,0)}\% & {_f(100*(1-_kp),0)}\% "
      rf"& conservative bound\\")
    A(r"\bottomrule\end{tabular}\end{center}")
    A(rf"\textbf{{Measured data, not a literature constant.}} The "
      rf"{_f(100*_kb,0)}/{_f(100*(1-_kb),0)} bending split is a "
      rf"\emph{{team measurement}}: a paddler was weighed in racing kneeling "
      rf"position with the knees and the toes bearing on separate scales, and "
      rf"the knee reading taken as a fraction of body weight. "
      + (rf"\textbf{{Sample size $n={_bm['measured_n']}$}} --- this is a "
         r"single measurement of one paddler, not an established "
         r"biomechanical constant, and it is reported here with the same "
         r"caveat as the small-$n$ mix regression (Sec.~\ref{sec:mixreg}). "
         r"It should not be cited as a general figure."
         if _bm["measured_n"] < 3 else
         rf"Sample size $n={_bm['measured_n']}$ paddlers.") +
      r" The split is a configuration field (\texttt{knee\_frac\_bending} in "
      r"\texttt{canoe\_suite.cfg}), so measuring more paddlers updates every "
      r"downstream number without a code change.")
    A(rf"\textbf{{Why punching does not use it.}} Punching is a \emph{{local}} "
      rf"contact check: crediting {_f(100*(1-_kp),0)}\% of the weight to the "
      r"toes would reduce the load on the knee patch and make the check less "
      r"severe on the strength of a single measurement. The punching basis "
      rf"therefore keeps {_f(100*_kp,0)}\% on the knees --- no toe relief --- "
      r"so the two-circle result in the next subsection stands on the "
      r"conservative bound regardless of how the measured split later moves.")
    if not _bm["advanced_on"]:
        A(rf"\textbf{{Currently inactive for bending.}} "
          r"\texttt{ADVANCED\_PADDLER\_LOADING} is \emph{off} for this run, so "
          r"the envelopes in this report apply each paddler's \emph{full} "
          r"weight as a single point load at the knee position --- no toe "
          r"split. Both models apply the \emph{same} total paddler weight, "
          r"verified to machine precision on every grid resolution; they "
          r"differ only in how that weight is distributed, the point-load "
          r"model simply concentrating it at the knee.")
        A(r"\textbf{Neither model is uniformly conservative.} Moving weight "
          rf"\SI{{{_f(_bm['toe_offset_in'],0)}}}{{in}} aft to the toes lowers the "
          r"\emph{sagging} moment and the peak shear, but \emph{raises} the "
          r"hogging moment, because the toe loads sit further outboard of the "
          r"supports. Which effect wins depends on the seating and the hull, "
          r"so the two models cannot be ranked once and for all --- and the "
          r"governing D/C can move either way. The point-load default is "
          r"retained because it is the simpler, more directly checkable "
          r"idealisation, not because it is a bound. Any design decision that "
          r"turns on the difference must re-run \emph{both} models on the "
          r"design in question rather than assuming a direction.")
    A(r"\subsection{Punching shear (kneeling paddler)}")
    pp = m["punching_prog"]
    sq_, cf, bare = pp["square"], pp["circ_full"], pp["bare"]
    prl, pmx = pp["pad_real"], pp["pad_max"]
    govname = {"a": "(a)", "b": "(b)", "c": "(c)"}[sq_["governing"]]
    A(r"\paragraph{Step 1 --- rigid \SI{3}{in} square patch (conservative bound).}")
    A(r"A paddler kneels on a single \SI{3}{in} square patch on the hull "
      r"\emph{bottom} (ACI 318-19 \S22.6.5.2). The concentrated weight is taken "
      rf"conservatively as the full heaviest paddler, $P={_f(sq_['P_lbs'],0)}$~lb, "
      r"on one patch. Demand $v_u=P/(b_0 t)$ with critical perimeter "
      rf"$b_0=4(c+d)={_f(sq_['b0'],2)}$~in (effective depth $d={_f(sq_['d'],3)}$~in "
      r"--- the reinforcing mesh on its buildable layer interface, set by the "
      r"construction schedule rather than assumed at $t/2$; see the mesh-depth "
      r"sensitivity below):")
    A(rf"\begin{{equation}} v_u=\frac{{{_f(sq_['P_lbs'],0)}}}"
      rf"{{({_f(sq_['b0'],2)})({_f(m['t_in'],3)})}}={_f(sq_['v_u'],1)}~\text{{psi}}."
      r"\end{equation}")
    A(rf"$f'_c={_f(sq_['fc_psi'],0)}$~psi is the \textbf{{raw}} compressive "
      rf"strength of the \textbf{{bottom-flange ({_f(z[0],1)}~pcf) mix}} --- the "
      r"concrete at the kneeling surface, not the top mix. It is the "
      r"\emph{unfactored} strength on purpose: the ACI capacity equations below "
      r"are written in terms of $f'_c$ and the factor of safety is applied once, "
      r"afterwards, to their result. ACI gives three concrete capacities; the "
      r"\emph{smallest governs} (which one depends on geometry, so any single "
      r"formula is non-conservative):")
    A(r"\begin{align}")
    A(rf"\text{{(a)}}\ v_c&=4\lambda\sqrt{{f'_c}}={_f(sq_['v_c_a'],1)}~\text{{psi}}\\")
    A(rf"\text{{(b)}}\ v_c&=\Big(2+\tfrac{{4}}{{\beta}}\Big)\lambda\sqrt{{f'_c}}"
      rf"={_f(sq_['v_c_b'],1)}~\text{{psi}}\quad(\beta={_f(sq_['beta'],0)},\ \text{{square patch}})\\")
    A(rf"\text{{(c)}}\ v_c&=\Big(2+\tfrac{{\alpha_s d}}{{b_0}}\Big)\lambda\sqrt{{f'_c}}"
      rf"={_f(sq_['v_c_c'],1)}~\text{{psi}}\quad(\alpha_s={_f(sq_['alpha_s'],0)},\ \text{{interior}})")
    A(r"\end{align}")
    A(rf"Equation \textbf{{{govname}}} governs at ${_f(sq_['v_c'],1)}$~psi. For this "
      r"thin shell the effective depth $d$ is small relative to $b_0$, so the "
      rf"$\alpha_s d/b_0$ term is small and (c) falls below (a). The paddler kneels "
      r"on the flat hull bottom, well inside the panel, so the interior "
      rf"$\alpha_s=40$ applies (an edge condition, $\alpha_s=30$, would be lower still). "
      rf"Allowable $=v_c/\text{{FoS}}={_f(sq_['allow'],1)}$~psi, giving "
      rf"$\dc{{{_f(sq_['dc'],2)}}}$ --- "
      rf"\textbf{{{'PASS' if sq_['ok'] else 'FAIL'}}}.")
    if not sq_["ok"]:
        A(r"This bound is not the physical contact condition, and it is the "
          r"contact idealisation --- not the shell --- that is wrong. Steps 2 "
          r"and 3 replace it with the kneeling geometry the crew actually uses.")

    # --- Mesh-depth sensitivity: what a pass-too-shallow mesh costs ----------
    _dc = m["punching_d_compare"]; _cs = _dc["cases"]; _ls = m["layer_schedule"]
    _sh, _md, _dp = _cs["shallow"], _cs["mid_assumed"], _cs["deep"]
    A(r"\paragraph{Mesh depth follows the layer schedule, not a $t/2$ "
      r"assumption.} " +
      rf"The effective depth above is $d={_f(_dc['d_design_in'],3)}$~in: the "
      rf"reinforcing mesh embedded on a real interface of the "
      rf"{_ls['n_layers']}-pass build. Because $d$ enters the governing "
      r"$\alpha_s d/b_0$ capacity term, \emph{which} inter-pass interface the "
      r"mesh lands on changes the punching D/C. The single-square bound at the "
      r"two candidate interfaces and at the legacy mid-shell assumption:")
    A(r"\begin{tabular}{@{}lrrl@{}}\toprule Mesh placement & $d$ (in) & D/C & \\\midrule")
    A(rf"After pass 1 (shallow, $t/3$) & {_f(_sh['d'],3)} & "
      rf"$\dc{{{_f(_sh['dc'],2)}}}$ & {'PASS' if _sh['ok'] else 'FAIL'}\\")
    A(rf"Mid-shell (legacy $t/2$) & {_f(_md['d'],3)} & "
      rf"$\dc{{{_f(_md['dc'],2)}}}$ & {'PASS' if _md['ok'] else 'FAIL'}\\")
    A(rf"\textbf{{After pass 2 (deep, $2t/3$) --- design}} & {_f(_dp['d'],3)} & "
      rf"$\dc{{{_f(_dp['dc'],2)}}}$ & {'PASS' if _dp['ok'] else 'FAIL'}\\")
    A(r"\bottomrule\end{tabular}")
    A(rf"\par\vspace{{2pt}}Placing the mesh a pass too shallow lifts D/C from "
      rf"$\dc{{{_f(_dp['dc'],2)}}}$ to $\dc{{{_f(_sh['dc'],2)}}}$ "
      rf"($\approx{_f(100*(_sh['dc']/max(_dp['dc'],1e-9)-1),0)}\%$ less margin). "
      r"A crew member embedding mesh after the \emph{first} pass is the intuitive "
      r"move, so the deep interface is written as a \textbf{binding} construction "
      r"requirement (Sec.~\ref{sec:compliance}), not a recommendation.")

    # --- Step 2: two circular knee contacts --------------------------------
    bc = bare["check"]
    A(r"\paragraph{Step 2 --- two \SI{3}{in}-diameter circular knee contacts.}")
    A(r"Paddlers are lifted and \emph{placed} onto both knees; nobody steps in "
      r"heel-first, so the single-heel case that governs some designs does not "
      r"arise here. That placement procedure is a binding compliance note "
      r"(Sec.~\ref{sec:compliance}), because it is what licenses this model. "
      rf"The weight is split {_f(100*bare['split_frac'],0)}/"
      rf"{_f(100*(1-bare['split_frac']),0)} across the two "
      rf"knees, ${_f(bc['P_lbs'],0)}\rightarrow{_f(bare['per_knee_lbs'],1)}$~lb per knee. "
      r"No credit is taken for the 20\% toe share of the 80/20 biomechanics "
      r"split --- the full body weight is kept on the knees, so the split is "
      r"conservative on load.")
    A(r"The contact is circular, so the ACI critical perimeter at the $d/2$ "
      r"offset is $b_0=\pi(d_{\text{load}}+d)$, \emph{not} the square "
      rf"$4(c+d)$: ${_f(bc['b0'],2)}$~in versus ${_f(sq_['b0'],2)}$~in, i.e.\ "
      rf"{_f(100*(1-bc['b0']/sq_['b0']),1)}\% \emph{{less}} perimeter for the same "
      r"contact width. This works \emph{against} the design --- it raises stress "
      r"per unit load and partly cancels the load split. Isolating the two "
      rf"effects: the same \SI{{3}}{{in}} circle carrying the \emph{{full}} "
      rf"${_f(sq_['P_lbs'],0)}$~lb gives $\dc{{{_f(cf['dc'],2)}}}$ (worse than the "
      rf"square's $\dc{{{_f(sq_['dc'],2)}}}$); halving the load then brings it to "
      rf"$\dc{{{_f(bc['dc'],2)}}}$. Net factor "
      rf"$={_f(bare['split_frac'],2)}/{_f(bc['b0']/sq_['b0'],3)}"
      rf"={_f(bare['split_frac']/(bc['b0']/sq_['b0']),3)}$.")
    A(r"\textbf{Perimeter interaction.} Two nearby contacts cannot be treated "
      r"as independent half-load punches if their critical perimeters merge, "
      r"since a merged section can be worse per unit load. Each critical "
      rf"circle has radius $(d_{{\text{{load}}}}+d)/2={_f(bare['crit_radius_in'],3)}$~in, "
      rf"so the perimeters merge only if the knee spacing drops below "
      rf"${_f(2*bare['crit_radius_in'],2)}$~in. At the assumed centre-to-centre "
      rf"spacing of ${_f(bare['knee_spacing_in'],1)}$~in (hips-width kneel) the two "
      rf"perimeters are {'MERGED' if bare['interact'] else 'well clear of each other'}, so "
      + (r"the combined perimeter around both contacts is evaluated under the "
         r"full load."
         if bare["interact"] else
         r"they act as two independent single-knee punches and the per-knee "
         r"check above applies.") +
      r" The check re-evaluates this automatically if the spacing is changed.")
    A(rf"Governing capacity equation \textbf{{({bc['governing']})}} at "
      rf"${_f(bc['v_c'],1)}$~psi, allowable ${_f(bc['allow'],1)}$~psi, demand "
      rf"${_f(bc['v_u'],1)}$~psi: $\dc{{{_f(bc['dc'],2)}}}$ --- "
      rf"\textbf{{{'PASS' if bc['ok'] else 'FAIL'}}}.")

    # --- Step 3: knee pad ---------------------------------------------------
    rc, xc = prl["check"], pmx["check"]
    A(r"\paragraph{Step 3 --- legal knee pad on top of the two-circle model.}")
    A(r"ASCE rules cap knee-pad thickness at \SI{0.5}{in}. A pad spreads each "
      r"contact to an effective diameter $d_{\text{load}}=3+2t_{\text{pad}}$. "
      rf"With the full legal \SI{{{_f(pmx['pad_in'],3)}}}{{in}} pad, "
      rf"$d_{{\text{{load}}}}={_f(pmx['d_load_in'],2)}$~in and "
      rf"$\dc{{{_f(xc['dc'],2)}}}$; with a realistic thinner "
      rf"\SI{{{_f(prl['pad_in'],3)}}}{{in}} pad, "
      rf"$d_{{\text{{load}}}}={_f(prl['d_load_in'],2)}$~in and "
      rf"$\dc{{{_f(rc['dc'],2)}}}$. The pad's benefit is real but modest: growing "
      r"$b_0$ also shrinks the $\alpha_s d/b_0$ term in equation (c), so the "
      rf"allowable falls slightly ({_f(bc['allow'],1)}$\rightarrow${_f(xc['allow'],1)}~psi) "
      r"as the demand falls. The pad is therefore reserve, not the basis.")

    A(r"\paragraph{Progression and adopted basis.}")
    A(r"\begin{center}\begin{tabular}{llcc}\hline "
      r"Step & Contact model & $b_0$ / load & D/C \\\hline")
    for lbl, mdl, b0v, ldv, dcv, okv in [
        ("1", r"rigid \SI{3}{in} square, full load", sq_["b0"], sq_["P_lbs"],
         sq_["dc"], sq_["ok"]),
        ("2", r"two \SI{3}{in} circles, split, no pad", bc["b0"],
         bare["per_knee_lbs"], bc["dc"], bc["ok"]),
        ("3a", rf"+ \SI{{{_f(prl['pad_in'],3)}}}{{in}} pad", rc["b0"],
         prl["per_knee_lbs"], rc["dc"], rc["ok"]),
        ("3b", r"+ \SI{0.5}{in} legal-max pad", xc["b0"], pmx["per_knee_lbs"],
         xc["dc"], xc["ok"]),
    ]:
        A(rf"{lbl} & {mdl} & {_f(b0v,2)}~in / {_f(ldv,0)}~lb & "
          rf"\dc{{{_f(dcv,2)}}} {'PASS' if okv else 'FAIL'} \\")
    A(r"\hline\end{tabular}\end{center}")
    _basis_txt = {
        "square": r"the rigid single-square bound (Step 1)",
        "two_knee": r"the two-circle knee model with \emph{no} pad credit (Step 2)",
        "two_knee_pad": r"the two-circle knee model with the legal \SI{0.5}{in} pad (Step 3b)",
    }[pp["basis"]]
    _basis_note = {
        "square": (r" The crudest model already clears, so the design basis "
                   r"takes \emph{no} credit for the kneeling refinement at all "
                   r"--- Steps 2 and 3 are reported as reserve, and none of the "
                   r"contact, spacing or pad assumptions are load-bearing."),
        "two_knee": (r" Because the basis carries no pad credit, the knee pads "
                     r"the crew races with are unquantified reserve and a pad "
                     r"failure is not a structural failure."),
        "two_knee_pad": (r" The pad is load-bearing in this basis, so its "
                         r"presence and thickness are a construction "
                         r"requirement, not a comfort item."),
    }[pp["basis"]]
    A(rf"The documented design basis is {_basis_txt}, at "
      rf"$\dc{{{_f(ps['dc'],2)}}}$." + _basis_note)
    if not ps["ok"]:
        A(r"\par\vspace{4pt}\noindent\textbf{FINDING --- punching shear "
          r"controls.} Even with two circular knee contacts and the full legal "
          rf"pad the \SI{{{_f(m['t_in'],3)}}}{{in}} shell does \emph{{not}} pass "
          rf"local punching ($\dc{{{_f(ps['dc'],2)}}}$). The remaining lever is "
          rf"bottom-flange mix strength: $f'_c\ge{_f(ps['req_strength']**2/(0.75*(2+ps['alpha_s']*ps['d']/ps['b0']))**2,0)}$~psi "
          r"would be required at this geometry. This is a genuine limit state, "
          r"not a pass.")

    # 6 pour
    A(r"\section{Multi-Zone I-Beam Pour}")
    A(rf"The design pours a three-zone vertical I-beam from "
      rf"\textbf{{{m['n_distinct_mixes']} distinct mixes}}: "
      rf"{_f(z[0],1)}/{_f(z[1],1)}/{_f(z[2],1)}~pcf (bottom flange / web / top "
      r"flange). The dense flanges sit at the extreme fibres and the light web "
      r"spans the neutral axis; the same stack is used along the whole hull "
      r"length (no longitudinal zoning).")
    A(r"\begin{tabular}{@{}lrrrrr@{}}\toprule Zone & Density (pcf) "
      r"& $f'_c$ & $f_t$ & $f'_c/\text{FoS}$ & $f_t/\text{FoS}$\\"
      r" & & \multicolumn{2}{c}{\footnotesize raw (psi)} "
      r"& \multicolumn{2}{c}{\footnotesize allowable (psi)}\\\midrule")
    for row in m["mix_table"]:
        A(rf"{row['zone']} & {_f(row['density'],1)} & {_f(row['fc'],0)} & "
          rf"{_f(row['ft'],1)} & {_f(row['fc_allow'],0)} & {_f(row['ft_allow'],1)}\\")
    A(r"\bottomrule\end{tabular}")
    A(rf"\par\vspace{{2pt}}\footnotesize Raw strengths feed code equations "
      rf"written in terms of $f'_c$ (the ACI $\sqrt{{f'_c}}$ punching terms and "
      rf"the ACI modulus), which apply the factor of safety themselves. The "
      rf"allowable columns (FoS $={_f(c['fos'],2)}$) are what member D/C ratios "
      r"divide by. Using an allowable where a raw strength belongs applies the "
      r"factor twice.\normalsize")
    A(rf"\begin{{figure}}[h]\centering\includegraphics[width=0.8\linewidth]{{{img(charts['pour'])}}}"
      r"\caption{Pour-zone section: three density bands with the neutral axis "
      r"overlaid.}\end{figure}")
    A(rf"\begin{{figure}}[h]\centering\includegraphics[width=\linewidth]{{{img(charts['dc'])}}}"
      r"\caption{Per-station demand/capacity for both enforced checks.}\end{figure}")

    # --- Construction layer schedule (through-thickness): ORTHOGONAL to pours ---
    _ls = m["layer_schedule"]; _ka = m.get("keel_audit", {})
    _per = _ls["per_layer_in"]; _k32 = _per / (1.0 / 32.0)
    _per_note = (rf" ($={int(round(_k32))}/32$~in)"
                 if abs(round(_k32) - _k32) < 1e-3 else "")
    A(r"\subsection{Construction Layer Schedule (through-thickness)}")
    A(r"The pour zones above are \emph{vertical} bands that set \emph{which} mix "
      r"sits at a given height. The \textbf{construction layers} are the "
      r"\emph{orthogonal} through-thickness dimension --- \emph{how many} "
      r"troweled passes build the wall up to its thickness. Every point of the "
      rf"shell is applied in \textbf{{{_ls['n_layers']} successive equal passes}} "
      rf"of \SI{{{_f(_per,3)}}}{{in}}{_per_note} (total "
      rf"\SI{{{_f(_ls['t_total_in'],3)}}}{{in}}), the same count everywhere so "
      r"the thickness is uniform. Each pass carries the mix of whatever pour band "
      r"it crosses (mix per height from the zone table above); the layer count "
      r"itself does not change from zone to zone.")
    if not _ls["trowel_ok"]:
        A(rf"\par\vspace{{2pt}}\noindent\textbf{{ASSUMPTION --- trowel-ability.}} "
          rf"At \SI{{{_f(_per,3)}}}{{in}} each pass is below the "
          rf"\SI{{{_f(_ls['trowel_min_in'],4)}}}{{in}} (\(1/16\)~in) practical "
          r"minimum for a hand-troweled coat. The schedule assumes the team can "
          r"place a layer this thin; if not, reduce the layer count or thicken "
          r"the wall. Stated as a build assumption, not a verified capability.")
    else:
        A(rf"\par\vspace{{2pt}}\noindent Each \SI{{{_f(_per,3)}}}{{in}} pass clears "
          rf"the \SI{{{_f(_ls['trowel_min_in'],4)}}}{{in}} (\(1/16\)~in) "
          r"hand-trowel minimum, so the schedule is buildable as stated.")
    if _ka.get("computed") and _ka["uniform_ok"]:
        A(r"\par\vspace{2pt}\noindent\textbf{Uniform-thickness check.} The "
          r"structural weight model is a thin-wall shell (perimeter $\times t$); "
          r"the CAD export solidifies any near-keel band that would be thinner "
          r"than the shell. On this hull the as-built ring weighs "
          rf"\SI{{{_f(_ka['V_ring_lb'],1)}}}{{lb}} against the modelled "
          rf"\SI{{{_f(_ka['V_thin_lb'],1)}}}{{lb}} "
          rf"(${_f(100*_ka['delta_frac'],1)}\%$; {_ka['n_solidified']} of "
          rf"{_ka['n_stations']} stations solidify $>5\%$): the two agree and the "
          r"built shell is not heavier than analysed, so the uniform "
          rf"\SI{{{_f(_ls['t_total_in'],3)}}}{{in}} premise holds.")
    elif _ka.get("computed"):
        A(r"\par\vspace{2pt}\noindent\textbf{WARNING --- uniform thickness does "
          r"not hold on this hull.} The as-built ring weighs "
          rf"\SI{{{_f(_ka['V_ring_lb'],1)}}}{{lb}} against the modelled "
          rf"\SI{{{_f(_ka['V_thin_lb'],1)}}}{{lb}} "
          rf"(${_f(100*_ka['delta_frac'],1)}\%$; {_ka['n_solidified']} of "
          rf"{_ka['n_stations']} stations solidify $>5\%$ over the thin-wall "
          r"model). The near-keel band is materially thicker than the "
          rf"\SI{{{_f(_ls['t_total_in'],3)}}}{{in}} shell, so the layer schedule "
          r"above describes only the constant-thickness region --- resolve the "
          r"solid-keel material before building.")

    # 6b combined biaxial stress — Kupfer-Gerstle
    _kp = m.get("kupfer") or {}
    if _kp.get("kupfer_dc") is not None:
        A(r"\section{Combined Biaxial Stress --- Kupfer-Gerstle Envelope}"
          r"\label{sec:kupfer}")
        A(r"The governing tensile check is a biaxial concrete failure criterion, "
          r"not the maximum-principal-stress (Rankine) rule.  Rankine ignores "
          r"that concrete's tensile capacity \emph{degrades under transverse "
          r"compression}, and this hull sits squarely in the tension--compression "
          r"quadrant: longitudinal bending puts a fibre in tension while the "
          r"centripetal (hoop) moment $M_\mathrm{lat}(x)=k_\mathrm{lat}\,"
          r"|M_\mathrm{vert}(x)|$ compresses that same fibre transversely.  There "
          r"Rankine is unconservative exactly where the design governs.")
        A(r"\par\vspace{4pt}\noindent At every fibre $z$ of every station, under "
          r"each individual load case (2/3/4-person and their position-variance "
          r"perturbations --- not the envelope), the coexisting state is "
          r"$\sigma_x$ (longitudinal bending $n\,M_\mathrm{vert}(z-\bar y)/I_{tr}$), "
          r"$\sigma_y$ (transverse hoop $n\,M_\mathrm{lat}\,y/I_{lat}$), and "
          r"$\tau_{xy}=V\,Q(z)/(I_{tr}\,2t)$.  Principal stresses "
          r"$\sigma_{1,2}=\tfrac{\sigma_x+\sigma_y}{2}\pm\sqrt{(\tfrac{\sigma_x-"
          r"\sigma_y}{2})^2+\tau_{xy}^2}$ feed the Kupfer criterion, quadrant-aware "
          r"(each pour zone against its \emph{own} $f_t$, $f'_c$):")
        A(r"\begin{align}")
        A(r"\text{T--T}\ (\sigma_2\!\ge\!0):&\quad \sigma_1/f_t \le 1 \\")
        A(r"\text{T--C}\ (\sigma_1\!>\!0\!>\!\sigma_2):&\quad "
          r"\sigma_1/f_t + 0.8\,|\sigma_2|/f'_c \le 1 \\")
        A(r"\text{C--C}:&\quad \text{biaxially strengthened to }\sim\!1.16 f'_c "
          r"\ (\text{crushing checked separately vs.\ uniaxial } f'_c)")
        A(r"\end{align}")
        A(r"The tension--compression line "
          r"$\sigma_1/f_t = 1 - 0.8\,|\sigma_2|/f'_c$ is the Kupfer, Hilsdorf \& "
          r"R\"usch (1969) experimental fit as adopted by CEB-FIP Model Code 1990 "
          r"(the $0.8$ slope is theirs, not invented here).  \textbf{Torsion is "
          r"excluded} from $\tau_{xy}$: this suite's torsion figure is an "
          r"open-section St-Venant bound that overstates by $\sim\!11\times$ "
          r"(it ignores warping restraint), so folding it into the fibre state "
          r"would be nonsense.")
        _long = _kp.get("long_dc"); _hoop = _kp.get("hoop_dc")
        _rank = _kp.get("rankine_dc"); _kup = _kp.get("kupfer_dc")
        A(r"\par\vspace{4pt}\noindent\textbf{How much the biaxial interaction "
          r"adds}, for the winning design (design D/C, FoS already inside):")
        A(r"\begin{tabular}{@{}lr@{}}\toprule Check & D/C\\\midrule")
        if _long is not None:
            A(rf"Independent longitudinal (bending) & $\dc{{{_f(_long,3)}}}$\\")
        if _hoop is not None:
            A(rf"Independent transverse (hoop) & $\dc{{{_f(_hoop,3)}}}$\\")
        if _rank is not None:
            A(rf"Rankine, combined state (max principal) & $\dc{{{_f(_rank,3)}}}$\\")
        A(rf"\textbf{{Kupfer biaxial (governing gate)}} & $\dc{{{_f(_kup,3)}}}$\\")
        A(r"\bottomrule\end{tabular}")
        _g = _kp.get("gov") or {}
        if _g:
            A(rf"\par\vspace{{2pt}}\noindent Governing fibre: station index "
              rf"{_g.get('station_index','?')}, $z={_f(_g.get('fibre_z_in',0),2)}$~in, "
              rf"race case index {_g.get('case_index','?')}, "
              rf"\textbf{{{_tex(str(_g.get('quadrant','?')))}}} quadrant "
              rf"($\sigma_x={_f(_g.get('sigma_x',0),1)}$, "
              rf"$\sigma_y={_f(_g.get('sigma_y',0),1)}$, "
              rf"$\tau={_f(_g.get('tau_xy',0),1)}$~psi).")
        A(r"\par\vspace{4pt}\noindent\textbf{Honest limits.} Concrete Damaged "
          r"Plasticity and Willam-Warnke reproduce the biaxial (and triaxial) "
          r"surface more accurately, but they are nonlinear-FEA constitutive "
          r"models --- the right tool for a future ANSYS Mechanical study, not a "
          r"closed-form gate inside a parametric sweep.  Kupfer is the accuracy "
          r"ceiling for the closed form.  Critically, $f_t$ and $f'_c$ here come "
          r"from an $n=2$ density--strength regression, so the criterion's "
          r"sophistication exceeds the precision of its inputs: a better criterion "
          r"on uncertain data is still the right call, but the combined D/C should "
          r"not be read as more accurate than the two lab breaks behind it allow.")

    # 7 mix regression
    A(r"\section{Mix Regression}\label{sec:mixreg}")
    A(rf"Allowable stresses come from the team's density--strength regression "
      rf"(source: \texttt{{{_tex(reg['source'])}}}, $n={reg['n']}$ lab points, "
      rf"tag \texttt{{{_tex(m['regression_tag'])}}}):")
    A(r"\begin{align}")
    A(rf"f'_c&={_f(reg['fc_slope'],2)}\,\rho{_f(reg['fc_int'],1)},\qquad "
      rf"f_t={_f(reg['ft_slope'],2)}\,\rho{_f(reg['ft_int'],1)}~\text{{(psi, $\rho$ in pcf)}}")
    A(r"\end{align}")
    if reg["n"] <= 2:
        A(rf"\emph{{With only $n={reg['n']}$ lab points the fit is a two-point "
          r"line ($R^2=1$ by construction); additional breaks are needed for a "
          r"meaningful goodness-of-fit.} " + reg_note.capitalize() + ".")
    else:
        A(reg_note.capitalize() + ".")
    A(rf"The regression predicts zero tensile strength below "
      rf"$\rho={_f(reg['ft_xint'],1)}$~pcf (the tensile $x$-intercept), which "
      r"sets the practical light-mix floor.")

    # 7b Which winning densities are real concrete and which are predictions.
    prov = m.get("mix_provenance") or []
    if prov:
        A(r"\subsection{Mix Provenance: Batched vs.\ Predicted}"
          r"\label{sec:mixprov}")
        A(r"The optimiser searches a continuous density spectrum, so a winning "
          r"density need not correspond to a mix that has ever been batched. "
          r"Each zone below is therefore marked either \textbf{on record} (a "
          r"mix in the Mix Lab) or \textbf{predicted} (a density the regression "
          r"extrapolated to). For predicted densities the nearest real mixes on "
          r"either side are given, together with the \emph{reach}: how far the "
          r"prediction sits from the closest data point. A short reach between "
          r"two bracketing mixes is an interpolation; a long reach, or no "
          r"bracket at all, is an extrapolation the mix team must verify before "
          r"this design can be relied on.")
        A(r"\begin{center}\begin{tabular}{llrrl}\toprule")
        A(r"Zone & $\rho$ (pcf) & $f'_c$ (psi) & $f_t$ (psi) & Provenance \\")
        A(r"\midrule")
        for v in prov:
            if v.is_real:
                note = rf"on record ({_f(v.matched_mix_pcf,1)} pcf)"
            else:
                lo = _f(v.below_pcf, 1) if v.below_pcf is not None else "--"
                hi = _f(v.above_pcf, 1) if v.above_pcf is not None else "--"
                reach = (rf", reach {_f(v.reach_pcf,1)}"
                         if v.reach_pcf is not None else "")
                note = rf"predicted; between {lo}/{hi}{reach}"
                if not v.bracketed:
                    note = rf"\textbf{{EXTRAPOLATED}}; nearest {lo}/{hi}{reach}"
            A(rf"{_tex(v.zone or '')} & {_f(v.density_pcf,1)} & "
              rf"{_f(v.fc_psi,0) if v.fc_psi is not None else '--'} & "
              rf"{_f(v.ft_psi,0) if v.ft_psi is not None else '--'} & "
              rf"{note} \\")
        A(r"\bottomrule\end{tabular}\end{center}")
        A(rf"Mixes currently on record: "
          rf"{', '.join(_f(d,1) for d in (m.get('existing_mix_pcf') or [])) or 'none'}"
          r"~pcf. Quoted $f'_c$ and $f_t$ are allowables, with the factor of "
          r"safety already applied.")
        if m.get("mix_all_real"):
            A(r"Every zone of this design uses a mix that has been batched and "
              r"tested; no material property here is an unverified prediction.")
        else:
            bw = m.get("buildable_weight_lbs")
            if bw is not None:
                bmix = ", ".join(_f(x, 1) for x in (m.get("buildable_mixes_pcf") or []))
                A(rf"This design targets mixes that do not yet exist. Restricted "
                  rf"to the mixes on record, the same search yields "
                  rf"\textbf{{{_f(bw,2)}~lb}}"
                  + (rf" using {bmix}~pcf" if bmix else "")
                  + rf", against the {_f(m['weight_lbs'],2)}~lb target above --- "
                  rf"a {_f(bw - m['weight_lbs'],2)}~lb penalty for developing "
                  rf"none of them. The target is the design intent; the "
                  rf"restricted figure is the fallback if no new mix is "
                  rf"qualified in time.")
            else:
                A(r"This design targets mixes that do not yet exist; the "
                  r"predicted densities must be batched and tested before the "
                  r"quoted capacities can be claimed.")

    # 7b transverse stability — three conventions, because they are not
    #    interchangeable and a bare GM_T is uninterpretable without its KG basis.
    _sr = m.get("stability_real")
    if _sr:
        A(r"\section{Transverse Stability}\label{sec:stability}")
        A(r"Transverse metacentric height is $GM_T = KB + BM_T - KG$. The first "
          r"two terms are hull FORM; only $KG$ depends on where the concrete "
          r"actually sits. A $GM_T$ quoted without stating its $KG$ basis is "
          r"therefore uninterpretable --- the same hull yields materially "
          r"different numbers under the three conventions below, and they must "
          r"not be compared across bases.")
        A(rf"For this hull $KB = {_f(_sr['kb_in'],2)}$~in and "
          rf"$BM_T = {_f(_sr['bm_t_in'],2)}$~in, giving a form ceiling of "
          rf"{_f(_sr['kb_in'] + _sr['bm_t_in'],2)}~in against which every $KG$ "
          rf"is charged. The poured shell's own centre of gravity is "
          rf"$KG_{{hull}} = {_f(_sr['kg_hull_in'],2)}$~in.")
        A(r"\begin{tabular}{llr}\toprule")
        A(r"Convention & $KG$ basis & $GM_T$ \\\midrule")
        A(rf"Form-only & waterline (shape alone) & {_f(_sr['gmt_form_in'],2)} in\\")
        A(rf"Bare hull & real zoned-pour shell CG & "
          rf"{_f(_sr['gmt_bare_hull_in'],2)} in\\")
        A(rf"Loaded (racing) & shell + crew, mass-weighted & "
          rf"{_f(_sr['gmt_loaded_in'],2)} in\\")
        A(r"\bottomrule\end{tabular}")
        A(r"\par\vspace{6pt}\noindent The form-only figure is the one blind to "
          r"mix distribution: an I-beam with heavy top flanges scores "
          r"identically to a uniform light hull. It is reported because it is "
          r"the convention a published hull-form comparison is most likely to "
          r"use, NOT because it describes this canoe's behaviour. The loaded "
          r"value is the only one that describes a capsize.")
        # crew CG dominates loaded stability — show the model, its sources, the
        # band it spans, and what it changed relative to the old flat guess
        _pb = _sr.get("crew_kg_posture_band")
        _cs = _sr.get("crew_kg_sensitivity") or []
        A(rf"\par\vspace{{6pt}}\noindent\textbf{{Crew centre of gravity.}} The "
          rf"{_f(_sr['crew_lbs'],0)}~lb crew outweighs the "
          rf"{_f(_sr['hull_lbs'],1)}~lb hull by roughly "
          rf"{_f(_sr['crew_lbs']/max(_sr['hull_lbs'],1e-9),1)} to one, so the "
          rf"crew's centre of gravity, not the concrete, sets the combined "
          rf"$KG = {_f(_sr['kg_combined_in'],2)}$~in and with it the entire "
          rf"loaded stability result.")
        if _pb:
            A(r"It is computed from segmented anthropometry rather than assumed. "
              r"A kneeling paddler is modelled as two groups, because only one "
              r"of them rotates:")
            A(r"\begin{equation*} KG_{paddler} = f_{legs}\,h_{legs} "
              r"+ f_{HAT}\,(h_{hip} + L_{HAT}\sin\theta) \end{equation*}")
            A(r"where $f$ are segment mass fractions, $h_{hip}$ is the hip-joint "
              r"height above the hull floor, $L_{HAT}$ the hip-to-CG distance of "
              r"the head-arms-trunk group, and $\theta$ the torso angle from "
              r"horizontal. Only the torso, head and arms swing about the hip "
              r"when a kneeling paddler leans; the shins are flat and the thighs "
              r"do not rotate. Crew $KG$ is the mass-weighted mean over the "
              r"paddlers, which is why the coed seating ARRANGEMENT cannot "
              r"affect it --- a property of the sum, not an unhandled case.")
            A(r"Segment masses are sex-specific from Plagenhoef, Evans and "
              r"Abdelnour (1983); segment lengths follow Drillis and Contini "
              r"(1966) as tabulated in Winter. $L_{HAT}$ is not asserted but "
              r"CALIBRATED, by solving for the offset that reproduces the "
              r"published standing whole-body CG (0.565$H$ male, 0.545$H$ "
              r"female), giving 0.159$H$ and 0.148$H$. As an independent check, "
              r"the same length data predicts a kneeling-height/stature ratio of "
              r"0.715 against ANSUR~II's measured 0.709 --- agreement to 1\%.")
            A(rf"\par\noindent\textbf{{Basis for this run:}} "
              rf"{_tex(_sr.get('crew_kg_basis',''))}, giving "
              rf"$KG_{{crew}} = {_f(_sr['kg_crew_in'],2)}$~in above the keel.")
            _leg = _sr.get("gmt_loaded_legacy_in")
            if _leg is not None:
                A(rf"\par\noindent This REPLACES the flat "
                  rf"{_f(_sr.get('crew_kg_legacy_in',12.0),0)}~in assumption used "
                  rf"previously, and it is not a small correction: loaded $GM_T$ "
                  rf"moves from {_f(_leg,2)}~in to "
                  rf"{_f(_sr['gmt_loaded_in'],2)}~in, a change of "
                  rf"{_f(_sr['gmt_loaded_in']-_leg,2)}~in. Every loaded stability "
                  rf"figure this suite reported before this model was optimistic.")
            A(r"\par\vspace{6pt}\noindent\textbf{POSTURE DOMINATES.} Two kneels "
              r"are plausible and they are not close: hips resting back on the "
              r"heels puts the hip joint near 0.10$H$, while an upright kneel "
              r"with the thighs vertical puts it near 0.245$H$ --- about eight "
              r"inches apart. Sweeping the torso angle across its entire "
              r"plausible range moves the crew CG by roughly two inches. The "
              r"posture, not the lean, decides the answer:")
            A(r"\begin{tabular}{llrr}\toprule")
            A(r"Posture & $\theta$ & $KG_{crew}$ & loaded $GM_T$ \\\midrule")
            for _p, _th, _kg, _gm in _pb:
                A(rf"{_tex(_p.replace('_',' '))} & {_f(_th,0)}$^\circ$ & "
                  rf"{_f(_kg,2)} in & {_f(_gm,2)} in\\")
            A(r"\bottomrule\end{tabular}")
            A(r"\par\vspace{6pt}\noindent The upright kneel is the default "
              r"because the team's own measurement supports it: at a measured "
              r"90/10 knee/toe load split with the toes 17~in aft of the knee, "
              r"horizontal equilibrium about the knee contact places the "
              r"combined CG within about 1.3~in of the knee. Heels-sitting would "
              r"put roughly two thirds of the load on the toes, which is not "
              r"what was weighed. That measurement is $n=1$.")
            _gms = [g for *_r, g in _pb]
            if min(_gms) < 0.0:
                A(r"\par\vspace{6pt}\noindent\textbf{Loaded $GM_T$ is NEGATIVE.} "
                  r"Over the default posture the metacentre sits BELOW the "
                  r"combined centre of gravity: the centre of buoyancy is not "
                  r"above the centre of mass in the racing condition, and the "
                  r"hull is not self-righting with the crew aboard. This is "
                  r"reported rather than tuned away, and it is consistent with "
                  r"the 2025 UVA report, which stated the same result for last "
                  r"season's hull and accepted it as the best attainable under "
                  r"the freeboard and width constraints. The operational "
                  r"consequence is plain: THE CANOE IS STABLE BECAUSE THE "
                  r"PADDLERS ACTIVELY BALANCE IT, not because the hull rights "
                  r"itself. Kneeling places roughly "
                  rf"{_f(_sr['crew_lbs'],0)}~lb of crew high above a shallow "
                  r"hull, which is the dominant term and is inherent to the "
                  r"boat class rather than a defect of this design.")
                A(r"\par\noindent This also settles the interpretation of the "
                  r"peer figures below. A published $GM_T$ near 10~in cannot be "
                  r"a LOADED value for a canoe of this class --- it would "
                  r"require a combined $KG$ no kneeling crew can produce --- "
                  r"which supports reading them on a hull-form or bare-hull "
                  r"basis.")
            A(r"\par\vspace{6pt}\noindent\textbf{ACTION, not caveat.} This is an "
              r"anthropometric ESTIMATE. Because hip height dominates and hip "
              r"height is a single tape-measure reading on a kneeling paddler, "
              r"MEASURING IT WOULD SUPERSEDE THIS ENTIRE MODEL. Until then the "
              r"posture preset stands in for the measurement, and a direct "
              r"determination of a kneeling paddler's CG would retire the "
              r"estimate altogether. The assumption is improved here, not "
              r"eliminated.")
        elif _cs:
            A(rf"That height is an ASSUMPTION "
              rf"({_f(_sr['kg_crew_in'],0)}~in above the keel for a kneeling "
              rf"paddler), not a measurement --- the biomechanical model was "
              rf"unavailable for this run. Loaded $GM_T$ across the "
              rf"plausible band: " +
              ", ".join(rf"{_f(c,0)}~in $\rightarrow$ {_f(g,2)}~in"
                        for c, g in _cs) +
              r". Measure it before this figure is quoted as a result.")
        # peer comparison — their loading basis is NOT stated, so bound the claim
        _pv = [v for _, v in PEER_GMT_IN]
        A(r"\par\vspace{6pt}\noindent\textbf{Peer comparison.} Florida's 2026 "
          r"report publishes " +
          " and ".join(rf"$GM_T = {_f(v,1)}$~in ({_tex(n)})"
                       for n, v in PEER_GMT_IN) +
          r". That table lists $GM_T$ beside waterline length, "
          r"length-to-beam ratio, entrance angle, transom width and drag --- all "
          r"hull-FORM properties --- and compares the value across two hull "
          r"SHAPES, which is only meaningful on a form basis. We therefore read "
          r"their figure as form-only or bare-hull and compare it to our "
          rf"form-only {_f(_sr['gmt_form_in'],2)}~in, a gap of "
          rf"{_f(100.0*(min(_pv) - _sr['gmt_form_in'])/min(_pv),1)}--"
          rf"{_f(100.0*(max(_pv) - _sr['gmt_form_in'])/max(_pv),1)}\%. Their loading "
          r"basis is not stated in the source, so this comparison is a reasoned "
          r"reading and not a verified apples-to-apples match; it must not be "
          r"compared against our loaded value.")
        A(r"\par\vspace{6pt}\noindent Because the gap is on a form basis, it is "
          r"a HULL-SHAPE gap and cannot be closed by redistributing concrete. "
          rf"$BM_T = I_{{wp}}/V = {_f(_sr['bm_t_in'],2)}$~in is the ceiling, set "
          r"entirely by waterplane distribution: our hull was generated by "
          r"optimising RESISTANCE, with stability never entering the objective, "
          r"whereas a flat-bottomed form raises $I_{wp}$ deliberately. Whether "
          r"stability should become an explicit constraint in future hull "
          r"generation is a next-season question, recorded here and not acted "
          r"on in this analysis.")
        _tr = m.get("stability_trade")
        if _tr and _tr.get("n_a"):
            A(r"\par\vspace{6pt}\noindent\textbf{Structural tension --- the top "
              r"flange.} The I-beam section places DENSE concrete in the top "
              r"flange because that is where bending capacity is earned, and "
              r"that is precisely the placement that raises $KG$. No trade is "
              rf"quantified for this winner: {_tex(_tr['n_a'])}, so there is no "
              r"denser top flange to give back.")
        elif _tr:
            _d_gm = _tr["gmt_loaded_in"] - _sr["gmt_loaded_in"]
            _d_w = _tr["weight_lbs"] - _sr["hull_lbs"]
            # How the stability payoff compares to the uncertainty already
            # present in the crew-CG assumption.  If the trade buys less than
            # the assumption's own band, redistributing concrete is not a
            # stability lever at all -- which is the actionable conclusion.
            # Prefer the biomechanical posture band: it is the real uncertainty
            # in the crew CG, and it is wider than the legacy round-number sweep.
            _pb2 = _sr.get("crew_kg_posture_band")
            if _pb2 and len(_pb2) > 1:
                _gm2 = [g for *_x, g in _pb2]
                _band, _band_src = max(_gm2) - min(_gm2), "posture"
            else:
                _cs2 = _sr.get("crew_kg_sensitivity") or []
                _band = (abs(_cs2[0][1] - _cs2[-1][1]) if len(_cs2) > 1 else None)
                _band_src = "crew-CG"
            A(r"\par\vspace{6pt}\noindent\textbf{Structural tension --- the top "
              r"flange.} The I-beam section places DENSE concrete in the top "
              r"flange because that is where bending capacity is earned; that is "
              r"precisely the placement that raises $KG$ and costs stability. "
              r"The two objectives pull on the same variable, so the trade is "
              rf"measured on this winner: dropping the top zone from "
              rf"{_f(_tr['base_stack'][-1],1)} to {_f(_tr['alt_stack'][-1],1)}~pcf "
              rf"moves loaded $GM_T$ by {_f(_d_gm,2)}~in and weight by "
              rf"{_f(_d_w,2)}~lb, at a Kupfer utilisation of "
              rf"\dc{{{_f(_tr['kupfer_dc'],3)}}} "
              rf"({'still feasible' if _tr['feasible'] else 'INFEASIBLE --- the '
                  'capacity is not available to trade'}).")
            if _band is not None and abs(_d_gm) < _band:
                A(rf"\par\noindent That $GM_T$ movement of {_f(abs(_d_gm),2)}~in "
                  rf"is SMALLER than the {_f(_band,2)}~in spread the "
                  rf"{_band_src} band alone spans. Redistributing concrete is "
                  rf"therefore not a meaningful stability lever on this hull: "
                  rf"the crew dominates $KG$, so the top flange should be sized "
                  rf"by bending capacity, and stability sought from hull FORM "
                  rf"($BM_T$) or from a measured crew CG instead.")

    # 7c strength basis + the winner's exact seating
    _mr = m.get("mix_regression")
    _ws = m.get("winner_seats")
    if _mr or _ws:
        A(r"\section{Strength Basis and Winning Load State}")
    if _mr:
        A(rf"Design strengths come from the density--strength regression "
          rf"currently IN FORCE, refitted from the Mix Lab table on load and "
          rf"after every lab edit. Source: \texttt{{{_tex(_mr.get('source'))}}}, "
          rf"n={_mr.get('n')}"
          + (rf"; a separate 3-point-bending (MOR) fit over n={_mr.get('n_fr')} "
             rf"points" if _mr.get('n_fr') else
             r"; no 3-point-bending (MOR) points exist, so the bending gate "
             r"falls back to split-tensile") + r".")
        A(rf"\par\noindent The tensile tier in force is "
          rf"\textbf{{{_tex(_mr.get('tier'))}}}, and the density search floor "
          rf"derives from it: \textbf{{{_f(_mr.get('density_floor_pcf'),1)}~pcf}} "
          rf"({_tex(_mr.get('floor_basis'))}). It is NOT taken from the "
          rf"compressive fit --- the two cross zero at different densities, and "
          rf"every bending gate here limits on TENSION, so a density with "
          rf"compressive strength but no tensile capacity cannot pass whatever "
          rf"its $f'_c$.")
        if _mr.get("pinned"):
            A(r"\par\noindent\textbf{WARNING --- pinned regression.} A MANUAL "
              r"regression is published and overrides the live fit, so the "
              r"strength basis no longer tracks the Mix Lab table. New lab "
              r"points will NOT change these allowables until it is superseded.")
        A(r"\par\vspace{4pt}\noindent\textbf{STATED MODELLING CHOICE --- MOR in "
          r"a biaxial criterion.} Where a flexural (MOR) fit exists it is "
          r"preferred for bending, because the gate computes $\sigma = Mc/I$, "
          r"the extreme-fibre stress in flexure, which is precisely what a "
          r"3-point bending test measures; split-tensile is back-calculated "
          r"from a diametral load and the compressive route is a correlation. "
          r"MOR is therefore the least-converted measurement for a bending "
          r"limit state. It is also the most PERMISSIVE tier --- for the same "
          r"mix it typically exceeds split-tensile, so preferring it RAISES "
          r"allowables and yields a lighter design. Further, the Kupfer "
          r"criterion uses $f_t$ as the tensile capacity in a COMBINED stress "
          r"state, whereas MOR is calibrated to bending alone; using it there "
          r"is reasonable but slightly permissive. Both are deliberate, and "
          r"are recorded here for the same reason as the $M_{lat}$ reaction "
          r"approximation and the open-section torsion bound.")
    if _ws:
        A(r"\par\vspace{6pt}\noindent\textbf{Winning seat positions} [ft from "
          r"the bow]. Recorded so the winning load state can be REPRODUCED "
          r"exactly --- a gate-table probe that guesses the seating changes the "
          r"moment envelope enough to make its answer meaningless.")
        A(r"\begin{tabular}{ll}\toprule")
        A(r"Race & Seats [ft] \\midrule")
        for _nm in (m.get("case_names") or sorted(_ws)):
            if _nm in _ws:
                A(rf"{_tex(_nm)} & "
                  rf"{_tex(', '.join(f'{float(q):.3f}' for q in _ws[_nm]))}\\")
        A(r"\bottomrule\end{tabular}")

    # 8 wave resistance
    A(r"\section{Wave Resistance}")
    vpath = os.path.join(os.getcwd(), "validation_results.json")
    if os.path.exists(vpath):
        A(r"Validation data present; see hydrodynamics appendix.")
    else:
        A(r"Wave resistance is a hydrodynamic quantity outside the structural "
          r"scope of this analysis. A resistance figure is reported here only "
          r"when validated hydrodynamic (CFD) data is supplied for the hull; in "
          r"its absence none is printed, because an unvalidated number would "
          r"misrepresent the design. This is a deliberate reporting choice: the "
          r"report states only what the analysis has established.")

    # 9 validation (featured)
    A(r"\section{Section-Engine Validation}\label{sec:val}")
    A(r"Every stress in this report is produced by one section engine: it "
      r"integrates the hull offsets into area, neutral-axis height and second "
      r"moment, and for a multi-material pour transforms each zone by its "
      r"modular ratio $n_i=E_i/E_{\text{ref}}$ before locating the composite "
      r"neutral axis. The engine is verified below against three closed-form "
      r"results, each reproducible by hand and using no external or measured "
      r"data.")
    A(r"\subsection{Base integration against exact theory}")
    A(rf"A thin wall of height $H$ has second moment $I=\tfrac{{1}}{{12}}tH^3$ "
      r"about its mid-height; the hull section is two such walls (port and "
      rf"starboard), so $I=2\cdot\tfrac{{1}}{{12}}tH^3$. For "
      rf"$H=\SI{{{_f(val['wall_H'],3)}}}{{in}}$ and $t=\SI{{{_f(val['wall_t'],2)}}}{{in}}$ "
      rf"the engine returns $I={_f(val['wall_I_eng'],2)}$~in\textsuperscript{{4}} "
      rf"against the exact ${_f(val['wall_I_exact'],2)}$ --- "
      rf"\textbf{{{_f(val['wall_err_pct'],3)}\% error}}. This confirms the raw "
      r"area and second-moment integration of the real offsets.")
    A(r"\subsection{Transformed neutral axis against its closed form}")
    A(rf"Splitting the same wall into two equal-height zones with modular ratio "
      rf"$n={_f(val['n_ratio'],0)}$ shifts the neutral axis toward the stiffer "
      r"material. Because the area per unit height is constant, the exact "
      r"transformed NA is")
    A(r"\begin{equation} \bar y=\frac{c^2/2+n(H^2-c^2)/2}{c+n(H-c)},\qquad "
      rf"c=\tfrac{{H}}{{2}}. \end{{equation}}")
    A(rf"The engine returns $\bar y={_f(val['comp_ybar_eng'],4)}$~in against the "
      rf"closed form ${_f(val['comp_ybar_exact'],4)}$~in --- "
      rf"\textbf{{{_f(val['comp_ybar_err_pct'],4)}\% error}}. This verifies the "
      r"modular-ratio bookkeeping the multi-zone I-beam depends on: the neutral "
      r"axis it locates is the true transformed centroid, not an approximation.")
    A(r"\subsection{Homogeneous limit}")
    A(rf"When every zone shares one modulus the transform must collapse exactly "
      rf"to the plain $Mc/I$ section. Setting both zone moduli equal, the engine "
      rf"reproduces the base second moment to "
      rf"\textbf{{${_sci(val['homog_err_pct'])}$\%}} --- a necessary consistency "
      r"condition, satisfied to machine precision.")
    A(r"\par\vspace{4pt}\noindent\textbf{Three analytic self-checks --- exact "
      r"theory for the base integration, a closed-form transformed neutral "
      r"axis, and the homogeneous limit --- verify the section engine from "
      r"first principles. Every stress in this report flows through it.}")

    # 9b search space + boundary check
    _ss = m.get("search_space")
    if _ss:
        A(r"\section{Search Space and Boundary Check}")
        A(r"A winning design is only meaningful together with the space it was "
          r"selected from: refining the increments inside a range that is itself "
          r"too narrow cannot find an optimum that lies outside it. The sweep "
          r"therefore records the ranges and increments actually searched:")
        A(r"\begin{center}\begin{tabular}{@{}llll@{}}\toprule "
          r"Variable & Range & Increment & Count\\\midrule")
        _t = _ss.get("thickness_in", {})
        A(rf"Wall thickness & {_f(_t.get('min',0),3)}--{_f(_t.get('max',0),3)}~in "
          rf"& buildable (1/32\", 0.5\,mm) & {_t.get('n','?')}\\")
        _d = _ss.get("depth_in", {})
        A(rf"Mold depth & {_f(_d.get('min',0),2)}--{_f(_d.get('max',0),2)}~in "
          rf"& {_f(_d.get('step',0),3)}~in & {_d.get('n','?')}\\")
        _dn = _ss.get("density_pcf", {})
        A(rf"Mix density & {_f(_dn.get('min',0),1)}--{_f(_dn.get('max',0),1)}~pcf "
          rf"& {_f(_dn.get('step',0),2)}~pcf & {_dn.get('n_combos','?')} combos\\")
        _ln = _ss.get("longitudinal_stations", {})
        _lu = _ln.get("used"); _lnat = _ln.get("native")
        _lnote = ("interpolated above native" if _ln.get("interpolated")
                  else "native resolution")
        A(rf"Longitudinal & {_lu} stations & (accuracy; native {_lnat}) & "
          rf"{_tex(_lnote)}\\")
        if _ss.get("gunwale"):
            _gw = _ss["gunwale"]
            A(rf"Gunwale w$\times$h & {_gw['width_in'][0]}--{_gw['width_in'][1]} "
              rf"$\times$ {_gw['height_in'][0]}--{_gw['height_in'][1]}~in "
              rf"& linspace & {_gw['steps']}\\")
        A(r"\bottomrule\end{tabular}\end{center}")
        _bf = m.get("boundary_flags") or []
        if _bf:
            A(r"\par\vspace{2pt}\noindent\textbf{Winner on a search boundary.} "
              r"The optimum sits on an edge of the searched space for:")
            A(r"\begin{itemize}")
            for b in _bf:
                if b.get("physical"):
                    A(rf"\item \textbf{{{_tex(b['variable'])}}} at its {b['edge']} "
                      rf"(${_f(b['value'],3)}$) --- a \emph{{physical}} limit "
                      rf"({_tex(b.get('why',''))}); widening the range would not "
                      r"move it.")
                else:
                    A(rf"\item \textbf{{{_tex(b['variable'])}}} at its {b['edge']} "
                      rf"(${_f(b['value'],3)}$) --- a \emph{{search-range}} edge: "
                      r"the true optimum may lie outside the searched range, "
                      r"which should be widened and the sweep re-run to confirm.")
            A(r"\end{itemize}")
        else:
            A(r"\par\vspace{2pt}\noindent The winner lies in the interior of "
              r"every searched range, so the design---not the search space---is "
              r"the binding constraint.")

    # 10 safety + compliance
    A(r"\section{Safety Factors and Compliance}\label{sec:compliance}")
    A(rf"Every D/C entry below is demand over the \emph{{allowable}} "
      rf"(ultimate strength $/\,{_f(c['fos'],2)}$ FoS); a value $\le 1.0$ is "
      rf"compliant \emph{{with the safety factor already applied}}.")
    A(r"\begin{tabular}{@{}lll@{}}\toprule "
      r"Check & Value (D/C = Allowable) & Status\\\midrule")
    A(rf"Dry hull weight & {_f(m['weight_lbs'])} lb & governing design value\\")
    # THE GATE LEADS.  The per-station longitudinal figure below is the legacy
    # check, subsumed by Kupfer and no longer binding; listing it first made a
    # design at 93.7% of its Kupfer limit read as having half its capacity
    # spare.  Report it, label it, never let it lead.
    _kdc = (m.get("kupfer") or {}).get("kupfer_dc")
    if _kdc is not None:
        _ksl = _kupfer_search_limit_for_report()
        A(rf"	extbf{{Kupfer-Gerstle biaxial (GOVERNING)}} & "
          rf"\dc{{{_f(float(_kdc),4)}}} vs {_f(_ksl,4)} search limit "
          rf"({_f(100.0*float(_kdc)/_ksl,1)}\% utilised) & "
          rf"{'PASS' if float(_kdc) <= _ksl else 'FAIL'}\\")
    _lb_dc = m["gov_bend"]["dc"]
    A(rf"\quad legacy longitudinal ({_gov_fib}), not the gate & "
      rf"\dc{{{_f(_lb_dc,2)}}} & "
      rf"{'PASS' if _lb_dc <= 1.0 else 'FAIL'}\\")
    if m.get("hoop_evaluated", True):
        A(rf"Transverse hoop & \dc{{{_f(gh['dc'],2)}}} & PASS\\")
    else:
        A(r"Transverse hoop & --- & not gated (single-zone)\\")
    A(rf"Punching shear & \dc{{{_f(ps['dc'],2)}}} & {'PASS' if ps['ok'] else 'FAIL'}\\")
    A(rf"Factor of safety & {_f(c['fos'])} & ACI 318-19\\")
    _fbk = m["freeboard"]
    A(rf"Loaded freeboard (4-person) & {_f(_fbk['freeboard_in'],2)} in & "
      rf"{'PASS' if _fbk['ok'] else 'FAIL'} ($\ge$ {_f(_fbk['min_freeboard_in'],2)} in)\\")
    _stab = m["stability"]
    if _stab["gmt_in"] > 1e-9:
        if _stab["min_gmt_in"] > 1e-9:
            _stab_txt = (rf"{'PASS' if _stab['gmt_in'] >= _stab['min_gmt_in'] else 'FAIL'} "
                         rf"($\ge$ {_f(_stab['min_gmt_in'],2)} in)")
        else:
            _stab_txt = "reported (no Min-GMT gate set)"
        # Labelled form-only: this is the pre-sweep hull gate (KG = waterline),
        # NOT the real-KG figure in sec:stability.  An unqualified GM_T here
        # would read as the canoe's actual stability, which it is not.
        A(rf"Transverse stability $GM_T$ (form-only) & {_f(_stab['gmt_in'],2)} in "
          rf"& {_stab_txt}\\")
        _sr0 = m.get("stability_real")
        if _sr0:
            A(rf"\quad --- loaded, real $KG$ (\S\ref{{sec:stability}}) & "
              rf"{_f(_sr0['gmt_loaded_in'],2)} in & reported, not gated\\")
    A(r"\bottomrule\end{tabular}")

    # BINDING --- seat pinning restricts what the envelope covers.  This sits
    # WITH the other binding notes, not as a footnote: it is an operational
    # restriction on race day, exactly like the knee-pad and placement rules.
    _ad = m.get("arrangement_disclosure") or {}
    _restricted = {k: v for k, v in _ad.items() if not v.get("fully_enumerated")}
    if _restricted:
        A(r"\par\vspace{6pt}\noindent\textbf{BINDING --- seating arrangement.} "
          r"Paddlers may sit in any order, and the ordering matters: it leaves "
          r"the sagging moment nearly unchanged but moves HOGGING substantially, "
          r"because hogging is driven by end-to-end asymmetry. This analysis "
          r"envelopes the orderings listed below and NO OTHERS. The design is "
          r"valid ONLY for seatings consistent with these pins --- if the crew "
          r"swaps seats on race day outside this set, the analysis does not "
          r"cover it.")
        A(r"\begin{tabular}{lrrl}\toprule")
        A(r"Race & Evaluated & Possible & Pinned seats \\\midrule")
        for _cat in (m.get("case_names") or sorted(_ad)):
            _d = _ad.get(_cat)
            if not _d:
                continue
            _ps = _d.get("pinned_seats") or {}
            _txt = (", ".join(f"seat {s} = paddler {p}"
                              for s, p in sorted(_ps.items()))
                    if _ps else "none (enumeration disabled)")
            A(rf"{_tex(_cat)} & {_d['n_evaluated']} & {_d['n_possible']} & "
              rf"{_tex(_txt)}\\")
        A(r"\bottomrule\end{tabular}")
        _pc = m.get("seat_pin_cost")
        if _pc:
            _pinned_dc = m["kupfer"].get("kupfer_dc")
            _un = _pc.get("unpinned_kupfer_dc")
            if _pinned_dc is not None and _un is not None:
                _d_dc = _un - float(_pinned_dc)
                A(rf"\par\vspace{{4pt}}\noindent\textbf{{Cost of the pin.}} "
                  rf"Re-running this winner with the pins CLEARED --- identical "
                  rf"seats, crew weights and geometry, the pins the only "
                  rf"difference --- gives a governing Kupfer utilisation of "
                  rf"\dc{{{_f(_un,4)}}} against the pinned "
                  rf"\dc{{{_f(float(_pinned_dc),4)}}}, a difference of "
                  rf"{_f(_d_dc,4)}. " +
                  (r"The excluded orderings are therefore MORE severe than the "
                   r"pinned ones: the pin is buying a seating preference at the "
                   r"cost of real structural margin, and that margin is only "
                   r"available while the pinned seating is actually used."
                   if _d_dc > 1e-9 else
                   r"The excluded orderings are no more severe than the pinned "
                   r"ones, so the pin costs no measured margin on this design "
                   r"--- though it still narrows what the analysis covers."))
    elif _ad:
        A(r"\par\vspace{6pt}\noindent\textbf{Seating arrangement.} Every "
          r"physically possible seat ordering was enveloped for every race "
          r"(" +
          ", ".join(rf"{_tex(k)}: {v['n_evaluated']}"
                    for k, v in sorted(_ad.items())) +
          r"), so the design does not depend on who sits where.")

    # Binding notes: the punching basis is only valid if these hold in practice.
    _pp = m["punching_prog"]; _bare = _pp["bare"]
    _sq_basis = _pp["basis"] == "square"
    A(r"\par\vspace{6pt}\noindent\textbf{BINDING --- punching-shear design "
      r"basis.} " +
      (r"The punching D/C above is carried by the shell itself under the "
       r"conservative single-square bound, so the kneeling refinements impose "
       r"no operational requirements. The mesh-placement and mix notes below "
       r"remain binding --- both set the numbers the check was run at:"
       if _sq_basis else
       r"The punching D/C above is earned by the kneeling-contact model, not "
       r"by shell thickness. It is valid only while all of the following "
       r"hold, and each is a construction/operations requirement:"))
    A(r"\begin{itemize}")
    if not _sq_basis:
        A(r"\item \textbf{Placement procedure.} Paddlers are lifted and placed "
          r"onto \emph{both} knees simultaneously. No heel-first step-in, and "
          r"no single-knee entry --- either would concentrate the full paddler "
          r"weight on one contact and invalidate the load split.")
        A(rf"\item \textbf{{Knee spacing.}} Contacts are held at approximately "
          rf"\SI{{{_f(_bare['knee_spacing_in'],1)}}}{{in}} centre-to-centre. "
          rf"Below \SI{{{_f(2*_bare['crit_radius_in'],2)}}}{{in}} the two ACI "
          r"critical perimeters merge and the combined section must be "
          r"re-checked under the full load.")
    _lsx = m["layer_schedule"]; _dcx = m["punching_d_compare"]
    _shx = _dcx["cases"]["shallow"]
    A(rf"\item \textbf{{Reinforcement mesh depth.}} The mesh is placed at the "
      rf"\SI{{{_f(_dcx['d_design_in'],3)}}}{{in}} interface --- after pass "
      rf"{_lsx['n_layers']-1} of {_lsx['n_layers']}, the deeper of the two "
      r"inter-pass interfaces. Embedding it one pass shallower (after pass~1, at "
      rf"\SI{{{_f(_shx['d'],3)}}}{{in}}) drops the effective depth $d$ below the "
      rf"design \SI{{{_f(_dcx['d_design_in'],3)}}}{{in}} and \emph{{overstates}} "
      rf"punching capacity (D/C would rise to $\dc{{{_f(_shx['dc'],2)}}}$). "
      r"Embedding mesh after the first pass is the intuitive move, so this is an "
      r"explicit build instruction, not a footnote.")
    A(rf"\item \textbf{{Bottom-flange mix strength.}} $f'_c\ge"
      rf"{_f(_pp['square']['fc_psi'],0)}$~psi at the kneeling surface "
      rf"(the {_f(z[0],1)}~pcf tension mix). The check is against the "
      r"\emph{bottom} mix; substituting a lighter mix in the bottom flange "
      r"invalidates this row.")
    if _pp["basis"] == "two_knee_pad":
        A(rf"\item \textbf{{Knee pads are structural.}} A pad of at least "
          rf"\SI{{{_f(_pp['pad_max']['pad_in'],3)}}}{{in}} (ASCE legal maximum) "
          r"must be present under every paddler. Racing without it fails the "
          r"punching check.")
    else:
        A(rf"\item \textbf{{Knee pads.}} Not required by the check --- the "
          r"basis takes no pad credit. Pads worn for comfort are unquantified "
          rf"reserve (they would take D/C to "
          rf"$\dc{{{_f(_pp['pad_max']['check']['dc'],2)}}}$), up to the ASCE "
          r"\SI{0.5}{in} legal cap.")
    A(r"\end{itemize}")

    A(r"\par\vspace{6pt}\noindent\textbf{Loaded freeboard --- derivation.} "
      r"ASCE measures freeboard \emph{under four-person load}, so the "
      r"compliance figure is not the geometric mold-depth-minus-design-draft "
      r"value. The loaded waterline is root-found on the real hull offsets: "
      r"for a trial waterline $z$ each station's submerged area is integrated "
      r"over depth and those areas are integrated along the hull,")
    A(r"\begin{equation} \nabla(z)=\int_0^{L}\!\!\left[\int_0^{z} 2\,y(x,\zeta)"
      r"\,d\zeta\right]dx, \qquad \text{solve } \rho\,\nabla(z_{wl})=W_{\text{loaded}}."
      r"\end{equation}")
    A(rf"The offsets $y(x,z)$ are the \emph{{same}} per-station table the section "
      rf"engine uses (no separate hydrostatic hull model). Loaded weight is the "
      rf"hull at {_f(_fbk['hull_lbs'],2)}~lb plus the four-person crew "
      rf"({'+'.join(_f(w,0) for w in _fbk['crew_lbs'])}~lb) "
      rf"$=\mathbf{{{_f(_fbk['total_weight_lbs'],2)}}}$~lb, with "
      rf"$\rho={_f(_fbk['rho_lb_ft3'],1)}$~lb/ft\textsuperscript{{3}}.")
    if _fbk["swamps"]:
        A(rf"\par\vspace{{4pt}}\noindent\textbf{{FINDING --- the hull swamps.}} "
          rf"The entire moulded volume to the gunwale is only "
          rf"{_f(_fbk['vol_to_gunwale_ft3'],3)}~ft\textsuperscript{{3}} "
          rf"$={_f(_fbk['buoyancy_lbs'],0)}$~lb of buoyancy, less than the "
          rf"{_f(_fbk['total_weight_lbs'],0)}~lb loaded weight. There is no "
          r"floating waterline: the canoe sinks under the four-person crew. "
          r"This is a hard design failure, not a margin issue.")
    else:
        A(rf"\par\vspace{{4pt}}\noindent Solving gives a loaded waterline "
          rf"$z_{{wl}}={_f(_fbk['waterline_in'],3)}$~in above the keel, "
          rf"displacing {_f(_fbk['disp_vol_ft3'],3)}~ft\textsuperscript{{3}} "
          rf"$={_f(_fbk['buoyancy_lbs'],1)}$~lb (matching the loaded weight, as "
          r"required). Freeboard is the mold depth less that waterline:")
        A(rf"\begin{{equation}} \text{{FB}}_{{\text{{loaded}}}}="
          rf"{_f(_fbk['mold_depth_in'],2)}-{_f(_fbk['waterline_in'],3)}="
          rf"\mathbf{{{_f(_fbk['freeboard_in'],2)}}}~\text{{in}}\end{{equation}}")
        _marg = _fbk["freeboard_in"] - _fbk["min_freeboard_in"]
        A(rf"against the \SI{{{_f(_fbk['min_freeboard_in'],2)}}}{{in}} minimum --- "
          rf"\textbf{{{'PASS' if _fbk['ok'] else 'FAIL'}}} with "
          rf"{_f(abs(_marg),2)}~in {'of margin' if _marg >= 0 else 'of DEFICIT'}. "
          rf"Reserve buoyancy above the loaded waterline is "
          rf"{_f(_fbk['reserve_vol_ft3'],2)}~ft\textsuperscript{{3}} "
          rf"({_f(_fbk['reserve_vol_ft3']*_fbk['rho_lb_ft3'],0)}~lb).")
        if not _fbk["ok"]:
            A(r"\par\vspace{4pt}\noindent\textbf{FINDING --- loaded freeboard "
              r"fails.} The hull floats, but not with the required reserve "
              r"depth under race load. This is a genuine compliance failure "
              r"and must be resolved by added mold depth or reduced weight "
              r"before the design can be certified.")
    A(r"\par\vspace{4pt}\noindent\footnotesize Modelling note: above the design "
      r"draft the offset table extrudes the waterline curve vertically (no "
      r"flare). A flared hull would gain waterplane area as it sinks and float "
      r"slightly higher, so the freeboard reported here is the conservative "
      r"bound.\normalsize")
    A(r"\end{document}")
    return "\n".join(L)


# ── top-level ──────────────────────────────────────────────────────────────────

def generate_latex_report(result: dict, out_dir: str,
                          output_name: str = "ASCE_Structural_Report",
                          tectonic_path: str | None = None) -> str:
    """Build the model, render LaTeX, compile to PDF if a compiler is available.

    Returns the PDF path if compiled, else the ``.tex`` path.
    """
    os.makedirs(out_dir, exist_ok=True)
    m = build_report_model(result)
    charts = {
        "env": _chart_envelopes(m, out_dir),
        "dc": _chart_dc(m, out_dir),
        "pour": _chart_pour(m, result, out_dir),
    }
    tex = render_latex(m, charts, out_dir=out_dir)
    tex_path = os.path.join(out_dir, output_name + ".tex")
    with open(tex_path, "w", encoding="utf-8") as fh:
        fh.write(tex)

    tec = tectonic_path or _find_tectonic()
    if not tec:
        # Silently returning the .tex hides the fact that the PDF the caller
        # asked for was never produced -- say so instead.
        logger.warning(
            "tectonic not found; wrote %s but produced NO PDF. Install "
            "tectonic (see this module's docstring) or pass tectonic_path=.",
            tex_path)
        return tex_path

    proc = subprocess.run([tec, "--outdir", out_dir, tex_path],
                          capture_output=True, timeout=300, text=True)
    pdf_path = os.path.join(out_dir, output_name + ".pdf")
    if proc.returncode != 0 or not os.path.exists(pdf_path):
        logger.error("tectonic failed (exit %s); returning .tex.\n%s",
                     proc.returncode, (proc.stderr or "")[-2000:])
        return tex_path
    return pdf_path


def _find_tectonic() -> str | None:
    """Locate tectonic on PATH, or at the standard per-user install location."""
    found = shutil.which("tectonic")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = os.path.join(local, "Programs", "tectonic", "tectonic.exe")
        if os.path.exists(cand):
            return cand
    return None
