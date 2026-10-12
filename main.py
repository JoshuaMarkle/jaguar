# main.py
"""
ASCE Concrete Canoe: 5-Dimensional Parametric Structural Sweep.

Coordinates geometry parsing, structural weight evaluation, and
visualization generation.  Uses ThreadPoolExecutor (NOT
ProcessPoolExecutor) to avoid Windows multiprocessing crashes when
called from a GUI daemon thread.
"""
import heapq
import itertools
import logging
import os
import sys
import time
import traceback
import numpy as np
from np_compat import trapezoid
import collections
import concurrent.futures
import threading
import multiprocessing
import multiprocessing.managers
from scipy.optimize import minimize
from errors import domain_only

logger = logging.getLogger(__name__)

from geometry import (parse_godzilla_dimensions, parse_godzilla_offsets,
                      calculate_shell_properties, stem_overhang_ft)
from structural import evaluate_canoe_weight, calculate_4_person_waterline
from visualization import plot_structural_envelope, plot_pour_zones, plot_shear_envelope
import config
from config import run_state as _run_state_mod
from version_stamp import code_version as _code_version


def resolve_sweep_executor(cfg=None):
    """
    The executor this sweep will use: "thread" or "process".

    THE single resolution point.  The banner prints this and the pool is built
    from this, so the two cannot drift -- a banner that disagreed with the pool
    would be worse than no banner, since it would be actively misleading about
    which path produced a result.

    Deliberately NOT part of OptimizationConfig: this is a MACHINE choice, not
    a design parameter.  Folding it into a design preset would let it be saved
    and shared, so a preset would then run differently on someone else's
    hardware -- and it is excluded from RUN_LEVEL_CONFIG_ATTRS precisely
    because it must never be able to change a result.
    """
    import config as _c
    src = cfg if cfg is not None else _c
    kind = str(getattr(src, "SWEEP_EXECUTOR", "thread") or "thread").lower().strip()
    if kind not in ("thread", "process"):
        raise ValueError("config.SWEEP_EXECUTOR must be 'thread' or 'process', "
                         f"got {kind!r}")

    # FROZEN BUILDS RUN THREADS.  canoe_suite.cfg ships sweep_executor:process
    # on the strength of a >2x speed measurement -- but that was measured in
    # the unfrozen interpreter, and CLAUDE.md's standing guardrail is that this
    # suite must not use a process pool (Windows deadlocks in GUI daemon
    # threads).  Under PyInstaller every spawned child re-executes the .exe,
    # which is a materially different situation from a `python main.py` run and
    # has never been verified here.
    #
    # A slower sweep is an acceptable cost; a deadlock a teammate hits on first
    # run is not.  Override rather than trust an unverified fast path -- and
    # say so, because a silent executor swap would make two machines disagree
    # about why a run was slow.  See KNOWN_ISSUES.md.
    import sys as _sys
    if kind == "process" and getattr(_sys, "frozen", False):
        # VERIFIED 2026-09-13 (Tier 1), and unlocked on that evidence.
        #
        # From the frozen binary, 36 tasks at 161 stations, the same grid on
        # each executor:  process 1309 s, thread 8533 s -- 6.52x -- and the
        # winners were BIT-IDENTICAL on every field (weight, D/C, depth,
        # thickness, zone densities, pour fractions).  So a ProcessPoolExecutor
        # does survive PyInstaller: spawn, the .exe re-executing itself for
        # every child, RunState pickling, and both manager proxies, with 11
        # concurrent children.
        #
        # WHAT IS STILL NOT VERIFIED is Tier 2: the GUI, with Stop pressed
        # mid-sweep, which is where a deadlock would live (the stop bridge, the
        # manager proxy and pool shutdown all interact there).  The procedure is
        # in README_TEAM.md section 8 and has not been run.
        #
        # This is unlocked anyway because the WATCHDOG changes what an unproven
        # path costs.  Before it, the downside was a window that stops
        # responding with no error and no way out but Task Manager.  Now a pool
        # that stops producing results for SWEEP_STALL_TIMEOUT_S is torn down
        # and the search restarts on threads, saying why.  Worst case is a
        # sweep that takes as long as it used to and explains itself -- which
        # is a fair trade for 6.5x, where a silent hang was not.
        if bool(getattr(src, "FORCE_THREAD_POOL_FROZEN", False)):
            print("  [frozen build] SWEEP_EXECUTOR forced back to 'thread' "
                  "(FORCE_THREAD_POOL_FROZEN). Slower, not wrong.")
            kind = "thread"
    return kind


def _settings_source():
    """
    How the live settings were obtained.  Never raises -- but never claims
    success either.

    THE BUG THIS FIXES.  The fallback used to be

        return {"loaded": True, "reason": "unknown", "path": None}

    which manufactured a CONFIDENCE CLAIM out of an exception.  It is worse
    than a fabricated number: the caller prints "[!! CONFIG DEFAULTED]" when
    `loaded` is falsy, so a True here SUPPRESSED the warning and a run whose
    provenance was unknown reported as one whose config had loaded cleanly.
    It also inverted config.settings' own default, which is loaded=False.

    version_stamp.py is the model to copy: never raise, but return a value
    that CANNOT be mistaken for success -- it answers "unknown" and a caller
    can see that it does not know.
    """
    try:
        from config.settings import CONFIG_SOURCE
        return dict(CONFIG_SOURCE)
    except ImportError as _e:
        return {"loaded": False, "path": None,
                "reason": "settings provenance unavailable (%s: %s) -- "
                          "treat every value as a BUILT-IN DEFAULT"
                          % (type(_e).__name__, _e)}


def _new_run_id():
    """
    A fresh id per sweep INVOCATION.

    Deliberately not derived from configuration: two sweeps with identical
    settings are still two different runs, and the whole point is telling them
    apart in a log.
    """
    import uuid
    return f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _struct_rocker_ft(t_in=None):
    """
    Stem-overhang (tip cantilever) length [ft] FOR A GIVEN WALL THICKNESS.

    The threshold that defines the overhang is ``1.5 * t`` (see
    geometry.stem_overhang_ft), so the overhang is a function of thickness:
    1.045 in at t=0.375, 1.394 in at t=0.500.  This used to be resolved ONCE
    per sweep at ``min(thickness_options_in)`` and applied to every task, which
    made a design's weight depend on what ELSE was in the grid -- the same
    (t=0.500, depth=12.570) design scored 161.4949 in a `[0.5]` grid and
    160.4491 in a `[0.375, 0.5, 0.625]` grid, ~1 lb apart, with seats 2.08 ft
    apart.  A hull weight has to be a property of the hull.

    The old sweep-wide rule was deliberate, not an oversight: the smallest t
    gives the shortest overhang, the least tip mass, and (because tip mass
    RELIEVES the midship sagging that governs) the conservative end.  But that
    conservatism only existed because one value had to serve every thickness.
    Resolved per task, each design gets its own true overhang and the
    compensation is unnecessary.

    Resolution order, and it RAISES rather than defaulting to 0.0 -- a silent
    zero models a hull with no overhang and says nothing about it, which is how
    two evaluations of one design once came back 0.9388 and 0.9400:

      * ``t_in`` given and a per-thickness map is published -> that thickness's
        value.  A map that is published but MISSING this thickness raises: it
        means a task is running on a thickness the sweep never registered,
        which is a wiring fault, not a skippable condition.
      * no map published -> the scalar ``STEM_OVERHANG_IN`` / legacy
        ``STRUCT_ROCKER_IN``.  This is the explicit-user-input path (an
        assumption the user has stated, deliberately thickness-independent) and
        the path direct callers such as unit tests use.
      * neither -> raise.
    """
    _by_t = getattr(config, "STEM_OVERHANG_BY_T", None)
    if t_in is not None and _by_t:
        key = round(float(t_in), 6)
        if key in _by_t:
            return float(_by_t[key]) / 12.0
        raise RuntimeError(
            f"stem overhang has no entry for t={key} in. The sweep published "
            f"a per-thickness map covering {sorted(_by_t)}, so this evaluator "
            f"is running on a thickness the sweep never registered -- a wiring "
            f"fault. It is NOT safe to fall back to another thickness's "
            f"overhang: that is the grid-coupling this map exists to remove.")
    for _name in ("STEM_OVERHANG_IN", "STRUCT_ROCKER_IN"):
        if hasattr(config, _name):
            return float(getattr(config, _name)) / 12.0
    raise RuntimeError(
        "stem overhang is not published on config: neither STEM_OVERHANG_BY_T, "
        "STEM_OVERHANG_IN nor STRUCT_ROCKER_IN is set. run_grand_optimization "
        "resolves it before evaluating anything, so this means an evaluator is "
        "being called directly. Set config.STEM_OVERHANG_IN explicitly (0.0 "
        "for a hull with no tip cantilever) rather than inheriting a silent "
        "zero.")


def publish_stem_overhang_map(thicknesses, x_st, y_uw, explicit_in=None):
    """
    Resolve the overhang for EVERY thickness in the grid, once, up front.

    Cheap -- `stem_overhang_ft` is an interpolation over a 1-D array -- so this
    costs nothing next to a sweep and is done before any worker starts.  That
    ordering matters: the map is written once and only ever read afterwards, so
    it is safe to share across a thread pool, which a per-task write to
    `config.STEM_OVERHANG_IN` would NOT have been.

    An explicit user overhang is an assumption the user has stated; it applies
    to every thickness unchanged.
    """
    out = {}
    for t in thicknesses:
        key = round(float(t), 6)
        if explicit_in is not None:
            out[key] = float(explicit_in)
        else:
            out[key] = float(stem_overhang_ft(x_st, y_uw, float(t))) * 12.0
    return out


# ═══════════════════════════════════════════════════════════════════
#  FALLBACK FILE GENERATORS
# ═══════════════════════════════════════════════════════════════════

def _generate_fallback_out(filepath, L_ft=20.0, T_in=4.26):
    """Write a minimal out.mlt so dimension parsing has something to read."""
    with open(filepath, "w") as f:
        L_m = L_ft / 3.28084
        T_m = T_in / 39.3701
        f.write(f"LOA (m), {L_m:.4f}\n")
        f.write(f"Tmax (m), {T_m:.4f}\n")


def _generate_fallback_off1(filepath, L_ft=20.0, T_in=4.26):
    """Write a minimal offset matrix so the geometry pipeline can run."""
    n_st, n_wl = 41, 21
    from hull_generator import generate_hull
    L_m = L_ft / 3.28084
    T_m = T_in / 39.3701
    B_m = 0.61
    factors = [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05]
    offsets = generate_hull(factors, L_m, B_m, T_m, n_st, n_wl)
    # Convert to inches for off1.mlt (Godzilla imperial format)
    offsets_in = offsets * 39.3701
    with open(filepath, "w") as f:
        for row in offsets_in:
            vals = " ".join(f"{v:10.5f}" for v in row)
            f.write(vals.rstrip() + "\n")


# ═══════════════════════════════════════════════════════════════════
#  WORKER FUNCTION (must be top-level for pickling on Windows)
# ═══════════════════════════════════════════════════════════════════

import structural
from structural import (freeboard_flare_gate as _freeboard_flare_gate,
                        lower_hull_section_properties,
                        design_summary as _design_summary,
                        evaluate_multizone_weight,
                        max_hull_weight_for_freeboard as _max_hull_weight_for_freeboard)


#: Report artefacts a sweep writes.  Anything matching these is a PREVIOUS
#: run's output and must be preserved, not overwritten.
_REPORT_GLOBS = ("ASCE_*.pdf", "ASCE_*.tex", "*.png",
                 "mix_development_targets.txt")


def _archive_existing_reports(reports_dir):
    """Move a previous run's artefacts into a timestamped archive subdirectory.

    A sweep used to overwrite whatever was in the reports directory, so the
    only copy of a baseline's outputs was destroyed by the next run that
    happened to point at the same place -- which is exactly how the 161-station
    run's artefacts were lost, by a reproduction run of a DIFFERENT design.
    Disk is cheap and a lost baseline is not, so nothing is deleted: the
    previous set is moved aside under its own timestamp and the new run writes
    into a clean directory.
    """
    import glob
    import shutil
    existing = []
    for pat in _REPORT_GLOBS:
        existing.extend(glob.glob(os.path.join(reports_dir, pat)))
    if not existing:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S",
                          time.localtime(max(os.path.getmtime(f)
                                             for f in existing)))
    dest = os.path.join(reports_dir, f"archive-{stamp}")
    # A second run inside the same second must not collide with the first.
    _n = 1
    while os.path.exists(dest):
        _n += 1
        dest = os.path.join(reports_dir, f"archive-{stamp}-{_n}")
    os.makedirs(dest, exist_ok=True)
    for f in existing:
        try:
            shutil.move(f, os.path.join(dest, os.path.basename(f)))
        except OSError as exc:                 # never fail a sweep over this
            print(f"  ⚠  could not archive {os.path.basename(f)}: {exc}")
    print(f"  archived {len(existing)} previous report artefact(s) -> "
          f"{os.path.basename(dest)}/")
    return dest


def _richardson_weight(cfg, filepath_matrix, L_ft, draft_in):
    """Converged hull weight for the winner, from the search grid and 2x it.

    The shell-area integral converges FIRST order (increment ratios measured at
    0.507, 0.507, 0.505, 0.503 out to 1281 stations), because A(x) is kinked
    wherever a half-breadth crosses the offset table's inclusion threshold.  So
    the quoted weight at any workable resolution is systematically LIGHT --
    4.3% at 41 stations, 1.1% at 161 -- and simply refining is a poor lever:
    W(2n) still carries -1.48 lb at n=161 while the extrapolation carries
    -0.03 lb, roughly 40x better for one extra geometry evaluation.

        W_inf = W(2n) + (W(2n) - W(n))          [first order]

    Returns ``{"n", "w_n", "w_2n", "w_converged"}`` or None if it cannot run.
    Both raw values are kept so the correction is visible, not buried.
    """
    try:
        n = int(getattr(config, "LONGITUDINAL_STATIONS", 0) or 0)
        if n < 2 or not cfg.get('multizone'):
            return None
        t_in = float(cfg['thickness']); depth = float(cfg['depth'])
        stack = [float(m) for m in cfg['mixes']]
        fracs = np.asarray(cfg['pour_fracs'], dtype=float)

        def _weight_at(n_st):
            xr, zr, ym = parse_godzilla_offsets(
                filepath_matrix, L_ft=L_ft, draft_in=draft_in,
                mold_depth_in=depth, n_stations=n_st)
            if xr is None:
                return None
            p = calculate_shell_properties(xr, zr, ym, t_in)
            v = p.A_in2 > 0
            x = xr[v]
            sheer = p.c_bot[v] + p.c_top[v]
            bounds = ([np.zeros(len(x))] + [f * sheer for f in fracs]
                      + [np.asarray(sheer, dtype=float)])
            w = np.zeros(len(x))
            prev = structural._cum_at(p.cum_area_in2[v], p.z_grid_in, bounds[0])
            for i in range(len(stack)):
                a_t = structural._cum_at(p.cum_area_in2[v], p.z_grid_in,
                                         bounds[i + 1])
                w += (a_t - prev) * stack[i] / 144.0
                prev = a_t
            return float(trapezoid(w, x))

        w_n, w_2n = _weight_at(n), _weight_at(2 * n - 1)
        if w_n is None or w_2n is None:
            return None
        return {"n": n, "w_n": w_n, "w_2n": w_2n,
                "w_converged": w_2n + (w_2n - w_n)}
    except Exception as exc:                  # never fail a completed sweep
        # "skipped" and "failed" were the same return here too: a failure
        # left the report simply omitting the converged weight, with nothing
        # saying the number had been attempted and lost.
        domain_only(exc)   # a bug is not a skippable condition
        logger.error("Richardson weight FAILED (not skipped): %s", exc,
                     exc_info=True)
        return {"failed": True, "reason": "%s: %s" % (type(exc).__name__, exc)}


#: The audit summary's field set, in one place.  Anything a run can differ on
#: belongs here; anything omitted is a dimension two runs can vary on while
#: logging identically.
AUDIT_SUMMARY_FIELDS = (
    "best_weight_lbs", "t_in", "depth_in", "zone_densities", "pour_fracs",
    "kupfer_dc", "n_stations", "fit_override", "feasible", "outcome",
)


def build_audit_summary(result, outcome=None):
    """
    THE audit summary for a sweep result -- one definition, both entry points.

    It existed twice, in main._audit_run and in the GUI's own audit block, and
    the two disagreed in ways that only showed up one at a time:

      * the GUI wrote ``len(result.get("x") or ())``.  `arr or ()` evaluates
        the ARRAY's truth value, which numpy refuses for more than one element,
        so the whole audit block raised and NO record was written for the run.
        main.py's copy of the same field already used the safe form.
      * the GUI wrote ``result.get("feasible")``, and the result dict has no
        such key -- so that logged None on every run, successful ones included.
        main.py's copy computed it.

    Two implementations of one record that can disagree is the defect; the
    individual bugs were symptoms.  The INPUTS legitimately differ between the
    entry points (the GUI reads its widgets, the CLI reads config), but the
    summary is a pure function of the result and is now shared.
    """
    _res = result if isinstance(result, dict) else {}
    x = _res.get("x")
    return {
        # `weight_lbs` is authoritative; `best_weight` is its alias in the
        # result dict, so accept either rather than depending on which.
        "best_weight_lbs": _res.get("weight_lbs", _res.get("best_weight")),
        "t_in":            _res.get("t_in"),
        "depth_in":        _res.get("depth_in"),
        "zone_densities":  _res.get("zone_densities"),
        "pour_fracs":      _res.get("pour_fracs"),
        "kupfer_dc":       _res.get("kupfer_dc"),
        # `is not None`, never truthiness: x is a numpy array.
        "n_stations":      (len(x) if x is not None else None),
        "fit_override":    bool(_res.get("fit_override")),
        # A result dict AT ALL means a winner was found.
        "feasible":        bool(_res),
        # Why a run produced nothing -- the whole point of logging it.
        "outcome":         outcome or ("winner found" if _res
                                       else "no feasible design"),
    }


def _audit_run(result, *, freeboard_min_in, precision_level,
               density_step_pcf, num_zones, multizone, existing_only,
               outcome=None):
    """
    Append this sweep to sweep_audit.log.

    The inputs recorded here are the ones the HASH is taken over, so anything
    omitted is a dimension two runs can differ on while logging identically.
    That is not hypothetical: every historical entry carried hash eda79cfa41a0
    because the six fields then recorded never varied, and neither the station
    count nor the density grid was among them.  Read the values off ``config``
    rather than off the caller's arguments -- config is what the sweep actually
    ran on after every default and override resolved.
    """
    try:
        from data.session import log_run
        _res = result if isinstance(result, dict) else {}
        inputs = {
            "run_type":      "structural_sweep",
            "length_ft":     float(_res.get("L_ft") or 0.0),
            "draft_in":      float(_res.get("draft_in") or 0.0),
            "fos":           float(getattr(config, "FACTOR_OF_SAFETY", 0.0)),
            "freeboard_in":  float(freeboard_min_in or 0.0),
            "mix_max_pcf":   float(getattr(config, "MAX_DENSITY_PCF", 0.0)),
            "mix_min_pcf":   float(getattr(config, "MIN_DENSITY_PCF", 0.0) or 0.0),
            "longitudinal_stations": getattr(config, "LONGITUDINAL_STATIONS",
                                             None),
            "density_step_pcf": density_step_pcf,
            "n_mixes":       int(getattr(config, "MAX_MIXES", 0)),
            "num_zones":     num_zones,
            "multizone":     bool(multizone),
            "existing_mixes_only": bool(existing_only),
            "precision_level": int(precision_level),
            "executor":      getattr(config, "SWEEP_EXECUTOR", "thread"),
            "code_version":  _code_version(),
            "entry_point":   "cli",
        }
        summary = build_audit_summary(_res, outcome=outcome)
        _h = log_run("structural_sweep", inputs, summary)
        print(f"  audit: logged run {_h} "
              f"({inputs['longitudinal_stations']} stations, "
              f"{inputs['mix_min_pcf']:g}–{inputs['mix_max_pcf']:g} pcf, "
              f"{inputs['executor']} executor)")
    except Exception as _exc:
        # DELIBERATELY BROAD, and this is the reasoned exception to the
        # import-guard narrowing applied across this module.
        #
        # The sweep that reached this line has already run -- possibly for
        # hours -- and its result is in hand.  Losing an AUDIT LINE must never
        # cost the run: a ValueError while assembling the summary would
        # otherwise discard the whole thing.  The GUI's copy of this block has
        # always caught broadly for the same reason, and it is what surfaced
        # the numpy-truthiness bug in that copy rather than crashing a sweep.
        #
        # It is still not silent.  A silent pass here is how an audit trail
        # goes missing without anyone noticing it has.
        print(f"  ⚠  audit log NOT written: {type(_exc).__name__}: {_exc}")


# Every key the GATE reads off a configuration.  The lean record carries these
# and nothing else -- in particular NOT x_raw/z_raw/y_matrix, which are 90% of
# a config's 74.2 KB and are identical for every design at the same depth.
#
# THE RISK THIS LIST CREATES.  Dropping the geometry means the gate scores a
# design RECONSTRUCTED from these parameters, not the object the worker held.
# If reconstruction diverged anywhere -- a cached parse, a rounded depth,
# anything resolution-dependent -- the gate would refine a different canoe than
# the one that screened, and nothing would say so.
# tests/test_gate_record_equivalence.py pins that it does not, and pins that
# this list stays complete: it AST-scans _evaluate_cfg_at for every key read
# off cfg and fails if one is missing here.
GATE_RECORD_FIELDS = (
    "mixes", "thickness", "depth", "pour_fracs", "seats", "multizone",
    "pos_var_ft", "enable_mesh", "gw_width_mid", "gw_height_mid",
    "gw_fillet_ratio", "weight_lbs", "kupfer_dc",
)


def config_int(name, default):
    """
    Read an INT config value that may be ABSENT **or** present-and-None.

    `int(getattr(config, name, default))` covers only the absent case: getattr
    returns the stored None when the attribute exists and holds None, and
    int(None) raises TypeError.  That is the same shape as every sentinel
    collision in this codebase -- a default that answers one question ("is it
    missing?") while the caller needed another ("is it usable?").

    It bit for real: LONGITUDINAL_STATIONS is declared Optional[int] on
    OptimizationConfig, where None legitimately means "use the offset file's
    native station count", and the refinement gate's banner did
    int(getattr(config, "LONGITUDINAL_STATIONS", 161)) -- so a documented,
    supported setting crashed the gate, which then raised RuntimeError because
    a TypeError inside the gate is (correctly) treated as a bug rather than a
    skippable condition.

    Raises a message that NAMES the attribute rather than letting a bare
    "int() argument must be ... not 'NoneType'" travel up with no context.
    """
    import config as _c
    raw = getattr(_c, name, None)
    if raw is None:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "config.%s = %r cannot be read as an int (%s). Set it to a whole "
            "number, or leave it unset/None to take the default %r."
            % (name, raw, exc, default)) from None


def gate_top_m_for_task(n_candidates):
    """
    How many candidates ONE sweep task offers the gate.

    WHY MORE THAN ONE.  Each task used to return only its own best, and that
    best was chosen on the SCREEN D/C -- the number the gate exists because we
    cannot trust.  So a lighter design that screened slightly worse never
    reached refinement, and the candidate set was filtered by exactly the
    unreliable value the gate was built to stop relying on.  That is not a
    sampling gap, it is the circularity one layer up.

    WHY IT SCALES.  A uniform m over-samples a task that evaluated 8
    configurations and under-samples one that evaluated 400 -- the same
    task-boundary artefact in a milder form.

    WHY IT IS CHEAP.  These are lean records (~0.52 KB), not configs (74.2 KB,
    ~90% of it hull geometry identical for every design at the same depth and
    re-parsed by the gate anyway).  At m=10 across 504 tasks the whole pool is
    ~2.6 MB, against ~36.5 MB for today's ONE full config per task.  The
    contract change makes the sweep cheaper, not dearer.
    """
    import math
    base = int(getattr(config, "GATE_TOP_M", 10) or 10)
    scaled = max(4, int(math.ceil(0.02 * max(int(n_candidates), 0))))
    return max(1, min(25, max(base, scaled)))


def _lean_gate_record(cfg, weight=None):
    """
    A gate candidate, stripped to what the gate actually reads.

    0.52 KB against 74.2 KB for the full config -- 142x smaller -- which is
    what makes returning the top-m per task cheaper than today's single
    full config per task, not dearer.
    """
    rec = {}
    for k in GATE_RECORD_FIELDS:
        if k in cfg:
            rec[k] = cfg[k]
    if weight is not None:
        rec["weight_lbs"] = float(weight)
    return rec


def _evaluate_cfg_at(cfg, filepath_matrix, L_ft, draft_in, n_stations,
                     freeboard_min_in, flare_max_deg, enable_mesh):
    """
    Re-evaluate ONE configuration at ``n_stations``.  Returns
    (kupfer_max, weight) or None.  Shared by the top-k gate and the winner
    refinement so the two can never disagree about how a design is scored.
    """
    t_in = float(cfg['thickness']); depth = float(cfg['depth'])
    stack = [float(m) for m in cfg['mixes']]
    fracs = tuple(cfg['pour_fracs'])
    _seats = cfg.get('seats')
    _missing = ([] if not _seats
                else [nm for nm in config.RACE_CATEGORY_ORDER
                      if _seats.get(nm) is None])
    if not _seats or _missing:
        return None
    x_raw, z_raw, y_mat = parse_godzilla_offsets(
        filepath_matrix, L_ft=L_ft, draft_in=draft_in,
        mold_depth_in=depth, n_stations=int(n_stations))
    if x_raw is None:
        return None
    p = calculate_shell_properties(x_raw, z_raw, y_mat, t_in)
    v = p.A_in2 > 0
    x = x_raw[v]
    dx = float(np.max(x)) / (len(x_raw) - 1)
    det = evaluate_multizone_weight(
        None, None, stack, fracs, x, dx, p.width_ft[v], t_in,
        p.cum_area_in2[v], p.cum_Az_in3[v], p.cum_Iz_in4[v], p.z_grid_in,
        p.c_bot[v] + p.c_top[v], p.c_bot[v], config.get_mix_properties,
        crew_cases=config.crew_load_cases(_seats),
        position_variance_ft=float(cfg.get('pos_var_ft', 0.0)),
        enable_mesh=enable_mesh, rocker_ft=_struct_rocker_ft(t_in),
        freeboard_min_in=freeboard_min_in, flare_max_deg=flare_max_deg,
        waterline_in=draft_in,
        gw_width_mid=cfg.get('gw_width_mid', 0.0),
        gw_height_mid=cfg.get('gw_height_mid', 0.0),
        gw_fillet_ratio=cfg.get('gw_fillet_ratio', 1.0),
        cum_Iy=p.cum_Iy_in4[v], yhalf_grid=p.yhalf_in[v],
        return_details=True)
    u = det.get("kupfer_max")
    if u is None:
        return None
    w = det.get("weight")
    return float(u), (float(w) if isinstance(w, (int, float)) else None)


def _gate_refine_top_k(pool, filepath_matrix, L_ft, draft_in,
                       freeboard_min_in, flare_max_deg, enable_mesh,
                       k=None, n_screen=None):
    """
    THE GATE.  Screen loose, refine the k lightest, accept on each design's OWN
    extrapolated value -- never on a global drift constant.

      1. refine each survivor to 2n and 4n, giving it its own increments
      2. extrapolate ITS tail (structural.extrapolate_kupfer_tail)
      3. reject when the extrapolated value exceeds ACCEPTANCE
      4. promote to 8n only when the extrapolation is what decides it, i.e.
         the value lands within KUPFER_PROMOTE_BAND of the limit -- so 4x is
         paid on the two or three that are genuinely close, not on all k

    WHY PER-DESIGN AND NOT A PULL-BACK CONSTANT.  kupfer_drift_per_doubling is
    a tail drift measured on ONE design and applied to every other, and drift
    demonstrably does not transfer on this hull -- 3.16% against 1.43% on the
    same geometry.  Each design pays for its own increments here anyway, so
    the acceptance reads them instead of a constant nobody can re-derive.

    Screening at n is safe because a coarse grid reads D/C LOW (0.913 against
    0.925 converged on the pinned design): it over-admits, and refinement
    removes the false admits.  It would be unsafe in the other direction.

    Returns (winner_cfg, audit_records, notes).
    """
    accept = structural.kupfer_acceptance_limit()
    # Both null-safe: a present-and-None config value used to reach int()
    # through the `or`, which only shields a FALSY argument, not a None result
    # from getattr.
    k = int(k) if k else config_int("GATE_TOP_K", 12)
    n_screen = int(n_screen) if n_screen else config_int(
        "LONGITUDINAL_STATIONS", 161)
    band = float(getattr(structural, "KUPFER_PROMOTE_BAND", 0.002))
    notes = []

    # WHAT THE POOL ACTUALLY HOLDS.  Each sweep task returns ONE best_config,
    # so this is the best design PER TASK, not every surviving design.  With
    # 504 tasks that is a usable candidate set; on a 2-task grid it is two
    # entries, which is why a small demo cannot exercise the k cap.  The real
    # consequence: if the two lightest designs overall fall in the SAME task,
    # only one of them ever reaches the gate.  Widening it means changing the
    # worker contract to return its top-m, a bigger change than the gate.
    ranked = sorted(pool,
                    key=lambda c: float(c.get('weight_lbs') or float('inf')))
    n_pool = len(ranked)
    short = ranked[:k]

    # The k CAP is a budget, not a claim that candidate k+1 cannot win.
    # Weight itself moves under refinement (+0.72% measured on the pinned
    # design), so two candidates closer than that can swap order.  If the cap
    # cut the list where the gap is smaller than that drift, say so OUT LOUD --
    # the same reason the search-boundary warning exists, and the same reason
    # a silent truncation is the wrong behaviour.
    if n_pool > k:
        w_k = float(ranked[k - 1].get('weight_lbs') or 0.0)
        w_next = float(ranked[k].get('weight_lbs') or 0.0)
        gap = (w_next - w_k) / max(w_k, 1e-9)
        notes.append("k cap BOUND: %d survivors, refining the %d lightest."
                     % (n_pool, k))
        if gap < 0.0072:
            notes.append(
                "  [!] candidate %d is only %.3f%% heavier than candidate %d, "
                "INSIDE the 0.72%% weight drift that refinement itself "
                "introduces -- the cap may have excluded a design that would "
                "out-rank a refined survivor.  Raise GATE_TOP_K and re-run to "
                "confirm this winner." % (k + 1, 100 * gap, k))
    else:
        notes.append("k cap did not bind: %d survivor(s), cap %d."
                     % (n_pool, k))

    records = []
    for idx, cfg in enumerate(short):
        u_by_n = {}
        try:
            for mult in (1, 2, 4):
                n = n_screen if mult == 1 else n_screen * mult - (mult - 1)
                got = _evaluate_cfg_at(cfg, filepath_matrix, L_ft, draft_in, n,
                                       freeboard_min_in, flare_max_deg,
                                       enable_mesh)
                if got is None:
                    raise RuntimeError("no evaluation at n=%d" % n)
                u_by_n[n] = got[0]
            ext = structural.extrapolate_kupfer_tail(u_by_n)
            promoted = False
            if (ext.get("ok") and ext.get("u_inf") is not None
                    and abs(ext["u_inf"] - accept) <= band):
                # the extrapolation itself is deciding -- buy a real datum
                n8 = n_screen * 8 - 7
                got = _evaluate_cfg_at(cfg, filepath_matrix, L_ft, draft_in,
                                       n8, freeboard_min_in, flare_max_deg,
                                       enable_mesh)
                if got is not None:
                    u_by_n[n8] = got[0]
                    ext = structural.extrapolate_kupfer_tail(u_by_n)
                    promoted = True
            ok = (bool(ext.get("ok")) and ext.get("u_inf") is not None
                  and ext["u_inf"] <= accept)
            records.append({
                "rank": idx + 1, "weight_screen": cfg.get('weight_lbs'),
                "mixes": [float(m) for m in cfg.get('mixes') or []],
                "depth_in": cfg.get('depth'), "t_in": cfg.get('thickness'),
                "pour_fracs": list(cfg.get('pour_fracs') or []),
                "n": sorted(u_by_n), "u": [u_by_n[n] for n in sorted(u_by_n)],
                "d1": ext.get("d1"), "d2": ext.get("d2"),
                "ratio_measured": ext.get("ratio_measured"),
                "ratio_used": ext.get("ratio_used"),
                "u_extrapolated": ext.get("u_inf"), "accept": accept,
                "promoted_to_8n": promoted, "accepted": ok,
                "reason": ext.get("reason"), "_cfg": cfg})
        except Exception as _e:
            domain_only(_e)   # a bug is not a skippable condition
            logger.warning("gate: candidate %d could not be refined: %s",
                           idx + 1, _e, exc_info=True)
            records.append({"rank": idx + 1, "accepted": False,
                            "reason": "refinement failed: %s" % _e,
                            "weight_screen": cfg.get('weight_lbs'),
                            "_cfg": cfg})

    winners = [r for r in records if r.get("accepted")]
    winner = winners[0]["_cfg"] if winners else None
    if not winners:
        notes.append(
            "  [X] NO candidate survived refinement.  The screen is admitting "
            "designs the refined check rejects; widen the pool (raise "
            "GATE_TOP_K) or loosen the screen.")
    return winner, records, notes


def _verify_winner_refinement(cfg, filepath_matrix, L_ft, draft_in,
                              freeboard_min_in, flare_max_deg, enable_mesh,
                              factor=2):
    """
    Re-evaluate the WINNING design at ``factor`` x the search resolution and
    report whether it still clears the ACCEPTANCE limit there.

    The sweep gates on kupfer_search_limit(), which is the acceptance limit
    pulled back by the measured per-doubling drift.  That makes this check pass
    by construction -- which is exactly why it is worth running: if it ever
    fails, the drift assumption is wrong for this design and the sweep must say
    so loudly rather than return a winner that refinement rejects.

    Cost: ONE design, ONE evaluation.  Seconds, against an hour-long search.

    Returns None when the check cannot be run (single-zone path, missing
    geometry); otherwise a dict with both utilisations and ``ok``.
    """
    # None means NOT APPLICABLE and nothing else.  It used to mean that AND
    # "the check blew up", because a broad handler at the bottom returned None
    # too -- so a refinement failure read as "no refinement needed" in the one
    # function whose job is confirming the winner survives refinement.  That is
    # the second time this specific machinery has failed silently (the first
    # was a NameError printed as a one-line skip), and the gate is the piece
    # everything downstream trusts the verdict of.
    if not cfg.get('multizone'):
        return None
    try:
        n_search = int(getattr(config, "LONGITUDINAL_STATIONS", 0) or 0)
        if n_search < 2:
            return None
        n_verify = int(n_search * int(factor))
        t_in = float(cfg['thickness']); depth = float(cfg['depth'])
        stack = [float(m) for m in cfg['mixes']]
        fracs = tuple(cfg['pour_fracs'])
        x_raw, z_raw, y_mat = parse_godzilla_offsets(
            filepath_matrix, L_ft=L_ft, draft_in=draft_in,
            mold_depth_in=depth, n_stations=n_verify)
        if x_raw is None:
            return None
        p = calculate_shell_properties(x_raw, z_raw, y_mat, t_in)
        v = p.A_in2 > 0
        x = x_raw[v]
        dx = float(np.max(x)) / (len(x_raw) - 1)
        sheer = p.c_bot[v] + p.c_top[v]
        # The seating must be the WINNER's, not a default: verifying a
        # different load state than the sweep selected would make this check
        # meaningless.  ALL FIVE races must be present -- verifying a subset
        # would pass while checking an easier boat than the one gated on.
        _seats = cfg.get('seats')
        _missing = ([] if not _seats
                    else [n for n in config.RACE_CATEGORY_ORDER
                          if _seats.get(n) is None])
        if not _seats or _missing:
            logger.warning(
                "refinement verification skipped: the winning configuration "
                "carries seating for %s, missing %s",
                sorted(_seats) if _seats else "no race",
                _missing or list(config.RACE_CATEGORY_ORDER))
            return None
        det = evaluate_multizone_weight(
            None, None, stack, fracs,
            x, dx, p.width_ft[v], t_in,
            p.cum_area_in2[v], p.cum_Az_in3[v], p.cum_Iz_in4[v], p.z_grid_in,
            sheer, p.c_bot[v], config.get_mix_properties,
            crew_cases=config.crew_load_cases(_seats),
            position_variance_ft=float(cfg.get('pos_var_ft', 0.0)),
            enable_mesh=enable_mesh, rocker_ft=_struct_rocker_ft(t_in),
            freeboard_min_in=freeboard_min_in, flare_max_deg=flare_max_deg,
            waterline_in=draft_in,
            gw_width_mid=cfg.get('gw_width_mid', 0.0),
            gw_height_mid=cfg.get('gw_height_mid', 0.0),
            gw_fillet_ratio=cfg.get('gw_fillet_ratio', 1.0),
            cum_Iy=p.cum_Iy_in4[v], yhalf_grid=p.yhalf_in[v],
            return_details=True)
        u_verify = det.get("kupfer_max")
        if u_verify is None:
            return None
        accept = structural.kupfer_acceptance_limit()
        return {"ok": float(u_verify) <= accept,
                "u_search": float(cfg.get('kupfer_dc') or 0.0),
                "u_verify": float(u_verify),
                "accept": float(accept),
                "search_limit": float(structural.kupfer_search_limit()),
                "n_search": n_search, "n_verify": n_verify,
                "mixes": stack, "t_in": t_in, "depth_in": depth,
                "weight": float(cfg.get('weight_lbs') or det.get("weight") or 0.0)}
    except (NameError, AttributeError, TypeError) as _e:
        # A programming error in the verification is not a skippable condition.
        raise RuntimeError(
            "winner refinement verification is BROKEN (%s: %s). A winner "
            "reported without it has not been verified at %dx resolution."
            % (type(_e).__name__, _e, factor))
    except Exception as _e:
        # Environmental failure (unreadable offsets, missing geometry).  Report
        # it as a FAILED verification, never as an absent one: the caller must
        # be able to tell "did not need checking" from "could not be checked".
        domain_only(_e)   # a bug is not a skippable condition
        logger.error("refinement verification FAILED: %s", _e, exc_info=True)
        return {"ok": False, "failed": True, "reason": "%s: %s"
                % (type(_e).__name__, _e),
                "u_search": float(cfg.get('kupfer_dc') or 0.0),
                "u_verify": None, "accept": structural.kupfer_acceptance_limit(),
                "n_search": None, "n_verify": None,
                "mixes": [float(m) for m in (cfg.get('mixes') or [])],
                "t_in": cfg.get('thickness'), "depth_in": cfg.get('depth'),
                "weight": float(cfg.get('weight_lbs') or 0.0)}


def _top_flange_stability_trade(cfg, sr, kb_in, bmt_in, crew_lbs, draft_in,
                                freeboard_min_in, flare_max_deg,
                                enable_mesh, hull_weight_max_lb=None,
                                crew_kg_in=None):
    """
    Quantify the structural/stability tension in the I-beam, on the winner.

    The section wants DENSE concrete in the TOP flange -- that is where bending
    capacity comes from -- and that is exactly the placement that raises KG and
    costs stability.  The two objectives pull on the same variable, so the trade
    has to be measured rather than argued.  Re-evaluates the winner's geometry
    with the top zone dropped to the LIGHTEST density already in its own stack
    (so the variant needs no new mix development), and returns what that buys in
    GM_T and what it costs in weight and Kupfer utilisation.

    Uses the winner's cached section properties, so it is one evaluator call
    (~40 ms), not a sweep.  Returns None when inapplicable.
    """
    from structural import (evaluate_multizone_weight, hull_mass_and_cg,
                            gunwale_rail_section, transverse_gmt_design_in)
    try:
        if not cfg.get('multizone'):
            return None
        stack = [float(m) for m in (getattr(sr, "zone_densities", None)
                                    or cfg['mixes'])]
        if len(stack) < 2:
            return None
        light = min(stack)
        if abs(stack[-1] - light) < 1e-9:
            # Not a failure: there is no denser top flange to trade away.
            return {"n_a": "top zone is already the lightest mix in the stack",
                    "base_stack": stack}
        alt = list(stack)
        alt[-1] = light
        det = evaluate_multizone_weight(
            cfg['coords_4'], cfg['coords_2'], alt, cfg['pour_fracs'],
            cfg['x'], cfg['dx'], cfg['width_ft'], cfg['thickness'],
            cfg['cum_area'], cfg['cum_Az'], cfg['cum_Iz'], cfg['z_grid'],
            cfg['sheer_in'], cfg['na_ref'], config.get_mix_properties,
            coords_3=cfg.get('coords_3'),
            position_variance_ft=float(cfg.get('pos_var_ft', 0.0)),
            enable_mesh=enable_mesh, rocker_ft=_struct_rocker_ft(cfg['thickness']),
            freeboard_min_in=freeboard_min_in, flare_max_deg=flare_max_deg,
            waterline_in=draft_in,
            gw_width_mid=cfg.get('gw_width_mid', 0.0),
            gw_height_mid=cfg.get('gw_height_mid', 0.0),
            gw_fillet_ratio=cfg.get('gw_fillet_ratio', 1.0),
            cum_Iy=cfg.get('cum_Iy'), yhalf_grid=cfg.get('yhalf'),
            hull_weight_max_lb=hull_weight_max_lb,
            return_details=True)
        if not isinstance(det, dict) or det.get("kupfer_max") is None:
            return None
        sheer = cfg['sheer_in']
        zb = [np.zeros(len(cfg['x']))]
        for f in np.asarray(cfg['pour_fracs'], dtype=float):
            zb.append(f * sheer)
        zb.append(np.asarray(sheer, dtype=float))
        rail = gunwale_rail_section(
            cfg['x'], cfg['thickness'], cfg.get('gw_width_mid'),
            cfg.get('gw_height_mid'), cfg.get('gw_fillet_ratio'), sheer)
        # Weigh the variant from GEOMETRY, never from det["weight"]: a rejected
        # configuration comes back with a large sentinel weight, and a
        # million-pound "hull" makes the 640 lb crew negligible, which fabricates
        # a large and entirely artificial stability gain.  The variant here is
        # expected to be rejected (that is the finding), so this matters.
        w_alt, kg_alt = hull_mass_and_cg(
            cfg['cum_area'], cfg['cum_Az'], cfg['z_grid'], zb, alt, cfg['x'],
            rail=rail, rail_density=float(alt[-1]))
        # Same integral on the WINNER's own stack.  It must reproduce the weight
        # the sweep accepted; if it does not, this whole comparison is measuring
        # two different hulls and the delta is meaningless.
        w_base, _ = hull_mass_and_cg(
            cfg['cum_area'], cfg['cum_Az'], cfg['z_grid'], zb, stack, cfg['x'],
            rail=rail, rail_density=float(stack[-1]))
        return {
            "base_weight_lbs": float(w_base),
            "base_stack": stack, "alt_stack": alt,
            "kg_hull_in": float(kg_alt),
            "weight_lbs": w_alt,
            "kupfer_dc": float(det["kupfer_max"]),
            "gmt_bare_hull_in": transverse_gmt_design_in(
                kb_in, bmt_in, kg_alt, w_alt),
            # The SAME crew CG the base design was measured with.  Letting this
            # fall back to the flat config default while the base uses the
            # biomechanical model would compare two hulls on two different
            # bases and report the basis change as a flange effect.
            "gmt_loaded_in": transverse_gmt_design_in(
                kb_in, bmt_in, kg_alt, w_alt, crew_lbs=float(crew_lbs),
                crew_kg_in=crew_kg_in),
            "feasible": bool(det.get("kupfer_max")
                             <= structural.kupfer_acceptance_limit()),
        }
    except Exception as _e:
        domain_only(_e)   # a bug is not a skippable condition
        logger.warning("top-flange stability trade skipped: %s", _e)
        return None


def _seat_pin_cost(cfg, sr, seats, draft_in, freeboard_min_in, flare_max_deg,
                   enable_mesh):
    """
    What the seat pins COST, measured on the winner.

    Pinning shrinks the arrangement set, so the pinned envelope is a max over
    fewer load states and can only be lower.  That is a real reduction in
    conservatism, and it can also land on a structurally WORSE-than-average
    ordering -- a steering preference that quietly eats margin.  This re-runs
    the winner with the pins CLEARED and reports both numbers.

    CROSS-BASIS: the two sides differ ONLY in the pins.  Same seats, same crew
    weights, same geometry, same resolution, same crew CG -- ``crew_load_cases``
    builds both from one call site so they cannot drift.  Returns None when
    nothing is pinned (there is no cost to price).
    """
    from structural import evaluate_multizone_weight
    try:
        if not cfg.get('multizone') or not config.any_seats_pinned():
            return None
        det = evaluate_multizone_weight(
            None, None, [float(m) for m in (getattr(sr, "zone_densities", None)
                                            or cfg['mixes'])],
            cfg['pour_fracs'],
            cfg['x'], cfg['dx'], cfg['width_ft'], cfg['thickness'],
            cfg['cum_area'], cfg['cum_Az'], cfg['cum_Iz'], cfg['z_grid'],
            cfg['sheer_in'], cfg['na_ref'], config.get_mix_properties,
            crew_cases=config.crew_load_cases(seats, with_pins=False),
            position_variance_ft=float(cfg.get('pos_var_ft', 0.0)),
            enable_mesh=enable_mesh, rocker_ft=_struct_rocker_ft(cfg['thickness']),
            freeboard_min_in=freeboard_min_in, flare_max_deg=flare_max_deg,
            waterline_in=draft_in,
            gw_width_mid=cfg.get('gw_width_mid', 0.0),
            gw_height_mid=cfg.get('gw_height_mid', 0.0),
            gw_fillet_ratio=cfg.get('gw_fillet_ratio', 1.0),
            cum_Iy=cfg.get('cum_Iy'), yhalf_grid=cfg.get('yhalf'),
            return_details=True)
        if not isinstance(det, dict) or det.get("kupfer_max") is None:
            return None
        sd = det.get("struct_data") or {}
        return {
            "unpinned_kupfer_dc": float(det["kupfer_max"]),
            "unpinned_m_sag": float(np.max(np.abs(det["M_sag"]))),
            "unpinned_m_hog": float(np.max(np.abs(det["M_hog"]))),
            "unpinned_arrangements": dict(sd.get("arrangements") or {}),
            "pinned_arrangements": {
                k: v["n_evaluated"] for k, v in
                (cfg.get("arrangement_disclosure") or {}).items()},
        }
    except Exception as _e:
        domain_only(_e)   # a bug is not a skippable condition
        logger.warning("seat-pin cost comparison skipped: %s", _e)
        return None


def _build_multizone_result(cfg, coords):
    """
    Reconstruct the winning multi-zone (I-beam) design as a StructuralResult
    (so every downstream plot/report path works unchanged) plus extra
    zone-geometry attributes for the N-band pour plot.
    """
    from domain.models import StructuralResult
    from structural import evaluate_multizone_weight
    # The winner's OWN five-race seating.  This used to slice coords[0:4] /
    # coords[4:6] from what is now a 14-element vector, so the winner was
    # REBUILT against a load state that does not exist: 2M's and 2F's seats
    # became "the four", 3M's first two became "the two", and the legacy
    # 3-case path ran on RACE_4/2/3_PERSON.  Every number downstream -- the
    # reported D/C, the moment envelope, the weight -- described that phantom.
    det = evaluate_multizone_weight(
        None, None, cfg['mixes'], cfg['pour_fracs'],
        cfg['x'], cfg['dx'], cfg['width_ft'], cfg['thickness'],
        cfg['cum_area'], cfg['cum_Az'], cfg['cum_Iz'], cfg['z_grid'],
        cfg['sheer_in'], cfg['na_ref'], config.get_mix_properties,
        rocker_ft=_struct_rocker_ft(cfg['thickness']),
        crew_cases=config.crew_load_cases(cfg['seats']),
        position_variance_ft=cfg.get('pos_var_ft', 0.0),
        enable_mesh=cfg.get('enable_mesh', False),
        gw_width_mid=cfg.get('gw_width_mid', 0.0),
        gw_height_mid=cfg.get('gw_height_mid', 0.0),
        gw_fillet_ratio=cfg.get('gw_fillet_ratio', 1.0),
        return_details=True)

    # Feasibility is DERIVED from the evaluation, never asserted.  This used to
    # read `is_feasible=True` unconditionally, so a rejected rebuild was stamped
    # feasible and its REJECTED weight travelled into the report.
    # RE-GATE the rebuilt winner with THE SAME gate the sweep used --
    # structural.kupfer_gate, not a second implementation.  A separately
    # written check here would recreate the exact failure this fixes: a
    # reported D/C of 1.06 sailing past because nothing re-tested it.
    _rg_km = det.get("kupfer_max")
    _rg_bad, _rg_util, _rg_lim = structural.kupfer_gate(
        None if _rg_km is None else {"util_max": _rg_km},
        det.get("worst_util"))
    if (_rg_bad or not det.get("feasible", False)
            or structural.is_rejected(det["weight"])):
        raise RuntimeError(
            f"the winning multi-zone design re-evaluated as INFEASIBLE "
            f"(D/C {_rg_util:.4f} vs limit {_rg_lim:.4f}). The sweep gated it "
            f"as feasible, so the two evaluations disagree -- that is a "
            f"gate/report divergence, not a design outcome.")

    sd = det["struct_data"]
    mom, shear = sd["moment"], sd["shear"]
    util = det["worst_util"]        # governing per-station D/C (both fibers)
    sr = StructuralResult(
        weight_lbs=det["weight"], is_feasible=True,
        thickness_in=cfg['thickness'], depth_in=cfg['depth'],
        mix_densities=list(det["zone_dens"]),
        x_ft=cfg['x'],
        M_envelope=mom["envelope"], M_race_4=mom["race_4"],
        M_race_2=mom["race_2"], V_max_pos=shear["max_pos"],
        V_max_neg=shear["max_neg"], V_race_4=shear["race_4"],
        V_race_2=shear["race_2"],
        na_line=det["y_bar"],       # COMPOSITE neutral axis
        pour_layout=np.zeros((len(cfg['x']), 2), dtype=int),
        M_race_3=mom.get("race_3"), V_race_3=shear.get("race_3"),
        M_sag_env=mom["sag"], M_hog_env=mom["hog"],
        gov_sag=mom["gov_sag"], gov_hog=mom["gov_hog"],
        case_names=list(sd.get("cases", [])),
        # The evaluator's OWN per-fibre curves.  These used to be `util` twice
        # plus two zero arrays, which made "Top fiber tension (HOGGING)" a
        # relabelled copy of the bottom fibre, left its governing case
        # unreportable, and meant the compression advisory could never fire.
        util_bot_t=det["util_bot_t"], util_top_t=det["util_top_t"],
        util_bot_c=det["util_bot_c"], util_top_c=det["util_top_c"],
        pour_line_in=(det["zone_pour_lines"][0]
                      if det["zone_pour_lines"] else None),
        sheer_line_in=det["sheer_in"],
    )
    # Extra multi-zone geometry for the N-band pour plot / report
    sr.zone_pour_lines = det["zone_pour_lines"]     # list of (n_st,) heights
    sr.zone_densities = list(det["zone_dens"])      # bottom→top
    sr.pour_fracs = cfg['pour_fracs']
    # Design methodology actually applied, carried WITH the winner: the load
    # combinations per case, phi/Omega, and the unfactored dead/live moment
    # curves.  The report states the counteracting-dead finding from these
    # numbers rather than re-deriving (or worse, asserting) them.
    sr.design = sd.get("design")
    return sr


# SLSQP options for the 9-variable seating solve.  Exported so tests exercise
# the SAME settings production uses -- they previously hardcoded their own copy
# and kept passing after production changed.  See the call site for why eps is
# small: the objective is continuous in seat position now that paddler loads
# are deposited linearly rather than snapped to the nearest station.
SEATING_SLSQP_OPTS = {'disp': False, 'eps': 1e-3, 'maxiter': 200}

# The seating landscape is genuinely MULTIMODAL, not merely noisy: a 31-start
# study on the winning (t, d) found six distinct local optima spanning 5.5 lb
# (144.97 / 145.59 / 146.71 / 147.47 / 149.75 / 150.51), with 20/31 starts
# reaching the best.  A single SLSQP solve therefore cannot be trusted to be
# global no matter how well conditioned it is -- so run best-of-N from
# deterministic, feasible starts.  At a ~65% per-start hit rate, N=8 misses the
# best basin with probability ~(0.35)^8 ~ 2e-4.
SEATING_N_STARTS = 8
SEATING_START_SEED = 20260719          # fixed: the sweep must be reproducible


def seating_start_points(L, n_starts=None, seed=None):
    """
    Deterministic feasible starting points for the seating solve.

    The vector covers ALL FIVE ASCE races in ``config.RACE_CATEGORY_ORDER``
    (14 variables: 2M, 2F, 3M, 3F, 4_coed).  Element 0 is a spread-out
    hand-built guess; the rest are drawn from a seeded RNG and satisfy the same
    constraints SLSQP enforces -- adjacent spacing >= 3.5 ft, box bounds, and
    the weighted-centroid trim band -- so every start begins inside the feasible
    set.  Each crew is seeded with its OWN weights, so the trim target reflects
    the crew that will actually sit there.
    """
    # Resolved AT CALL TIME, not bound as a default argument.
    #
    # These were `n_starts=SEATING_N_STARTS, seed=SEATING_START_SEED`, which
    # Python evaluates once at import: assigning main.SEATING_N_STARTS or
    # config.SEATING_N_STARTS afterwards had NO EFFECT.  Every experiment that
    # "varied" the multi-start count silently measured the same 8 solves --
    # including a test fixture that believed it had reduced them.  A tunable
    # that cannot be tuned is worse than a constant, because it reports success.
    if n_starts is None:
        n_starts = int(getattr(config, "SEATING_N_STARTS", SEATING_N_STARTS))
    if seed is None:
        seed = int(getattr(config, "SEATING_START_SEED", SEATING_START_SEED))
    rng = np.random.default_rng(seed)
    layout = config.crew_seat_layout()

    def _crew(k, weights, rand=True):
        w = np.asarray(weights, dtype=float); W = float(w.sum())
        for _ in range(200):
            if rand:
                gaps = (3.5 + rng.exponential(1.2, size=k - 1) if k > 1
                        else np.zeros(0))
                target = L / 2.0 + rng.uniform(-0.9, 0.9)   # inside trim band
            else:
                gaps = np.full(max(k - 1, 0), max(3.5, (L - 6.0) / max(k, 1)))
                target = L / 2.0
            off = np.concatenate([[0.0], np.cumsum(gaps)])
            p = target - float(np.dot(w, off) / W) + off
            if p[0] >= 2.0 and p[-1] <= L - 3.5:
                return p
        return None

    def _assemble(rand):
        parts = []
        for name, _sl in layout:
            _s, _h, wts = config.race_category_crew(name)
            p = _crew(len(wts), wts, rand=rand)
            if p is None:
                return None
            parts.append(p)
        return np.concatenate(parts)

    first = _assemble(rand=False)
    starts = [] if first is None else [first]
    while len(starts) < max(1, int(n_starts)):
        s = _assemble(rand=True)
        if s is None:
            break                             # geometry too tight; use what we have
        starts.append(s)
    if not starts:                            # never hand SLSQP an empty list
        starts = [np.linspace(2.0, max(L - 3.5, 2.5),
                              config.crew_seat_vector_len())]
    return starts


def paddler_placement_constraints(L):
    """
    SLSQP inequality constraints (g(v) ≥ 0) for the paddler placement problem,
    each carrying an ANALYTIC Jacobian.

    Every constraint is affine in the position vector v, so its Jacobian is a
    constant length-9 row vector — exact by construction.  This removes the
    finite-difference sensitivity that made the spacing gradients meaningless
    when SLSQP's FD step approached the 3.5 ft spacing margin (the eps=0.5 bug:
    a probe step the size of the constraint margin gave a garbage gradient, so
    SLSQP stalled just short of feasibility and the whole thickness was
    discarded).  The box bounds (2.0, L−3.5) are handled directly by scipy and
    need no Jacobian.

    v layout comes from ``config.crew_seat_layout()`` -- all five ASCE races in
    RACE_CATEGORY_ORDER (2M, 2F, 3M, 3F, 4_coed; 14 variables).  Constraints are
    emitted spacing-then-trim per crew, in that same order.

    Each crew's trim constraint uses ITS OWN weights.  Sharing one weight list
    across same-size crews would centre 2F's seats on 2M's centroid.
    """
    layout = config.crew_seat_layout()
    n = config.crew_seat_vector_len()
    half = float(L) / 2.0

    def _spacing(j):
        # g = v[j+1] − v[j] − 3.5 ≥ 0   (≥ 3.5 ft between adjacent seats)
        g = np.zeros(n); g[j] = -1.0; g[j + 1] = 1.0
        return {'type': 'ineq',
                'fun': (lambda v, j=j: float(v[j + 1] - v[j] - 3.5)),
                'jac': (lambda v, g=g: g)}

    def _trim(sl, weights):
        # avg = Σ wₖ v[k] / W over the crew slice; two half-constraints hold the
        # weighted centroid within L/2 ± 1 ft.  d(avg)/dv = wₖ/W on the slice.
        w = np.asarray(weights, dtype=float); W = float(w.sum())
        gvec = np.zeros(n); gvec[sl] = w / W

        def _avg(v, sl=sl, w=w, W=W):
            return float(np.dot(np.asarray(v)[sl], w) / W)

        upper = {'type': 'ineq',                     # (half+1) − avg ≥ 0
                 'fun': (lambda v, _avg=_avg: (half + 1.0) - _avg(v)),
                 'jac': (lambda v, gvec=gvec: -gvec)}
        lower = {'type': 'ineq',                     # avg − (half−1) ≥ 0
                 'fun': (lambda v, _avg=_avg: _avg(v) - (half - 1.0)),
                 'jac': (lambda v, gvec=gvec: gvec)}
        return upper, lower

    cons = []
    for name, sl in layout:
        _s, _h, wts = config.race_category_crew(name)
        # adjacent gaps WITHIN this crew only -- never across the boundary
        # between two crews, whose seats are independent.
        for j in range(sl.start, sl.stop - 1):
            cons.append(_spacing(j))
        cons += list(_trim(sl, wts))
    return cons


def min_density_bound_order(valid_mixes):
    """
    Indices of ``valid_mixes`` sorted ascending by min(combo) — the walk order
    for the lightest-first branch-and-bound shared by the single-zone and
    multi-zone mix searches.  Because the admissible bound
    (min_density_weight_bound) is monotone in min(combo), the first combo whose
    bound reaches the incumbent terminates the walk: every later combo has a
    larger min, hence a larger bound.
    """
    return sorted(range(len(valid_mixes)), key=lambda i: min(valid_mixes[i]))


def min_density_weight_bound(combo, volume_factor):
    """
    Admissible lower bound on the hull weight of ANY density assignment drawn
    from ``combo``, given the task's density-independent shell volume factor
    ``volume_factor`` = ∫ A_shell(x)/144 dx  [lb per pcf].

    Derivation (holds for BOTH mix paths — one bound, no drift):
        The self-weight is a sum of (area slice)·(density) terms,
            weight = Σₖ densityₖ · Vₖ ,   Vₖ = ∫ (area of slice k)/144 dx ≥ 0,
        and the slices PARTITION the section, so Σₖ Vₖ = volume_factor exactly,
        independent of how the section is split:
          • single-zone: the bottom/top fibre split, ∫(A_bot+A_top)/144 dx;
          • multi-zone:  the N pour zones, whose volumes telescope to
            ∫(A_below(sheer) − A_below(0))/144 dx regardless of the pour-line
            fractions.
        Every densityₖ ≥ min(combo) (the multi-zone I-beam stack is built only
        from the combo's densities, so its lightest zone is min(combo)), hence
            weight = Σₖ densityₖ·Vₖ ≥ min(combo)·Σₖ Vₖ = min(combo)·volume_factor.

    This is exact, not heuristic: a combo whose bound already reaches the
    incumbent cannot beat it, so skipping it never changes the winner
    (verified bit-identical vs full enumeration on the 55-task grid, both
    paths).
    """
    return float(min(combo)) * float(volume_factor)


# Per-gate rejection tally for the sweep currently running (structural.SweepTally),
# or None outside a sweep.  Set by run_grand_optimization and read by every
# worker thread -- the tally locks internally, so the threads share one object
# and the failure report describes the whole grid rather than one task's slice.
# Kept as a module global for the THREAD executor, where the workers genuinely
# share this object.  Under the PROCESS executor a global cannot be shared, so
# the sweep hosts the tally in a manager process and passes a proxy in the args
# tuple; the worker prefers the passed one and falls back to this global.
_SWEEP_TALLY = None


class _TallyManager(multiprocessing.managers.BaseManager):
    """Hosts the ONE SweepTally that process-pool workers count into."""


# Registered at import so a spawned child -- which re-imports this module to
# reach the worker function -- can rebuild the proxy it is handed.
_TallyManager.register(
    "SweepTally", structural.SweepTally,
    exposed=("skip", "record", "record_stage2", "note", "snapshot"))


def _physical_core_count():
    """
    Physical cores, falling back to logical/2 then 4.

    Matters because the sweep is dense float math: the two hyperthreads of a
    core share one FPU, so the sibling adds far less than a whole core.
    """
    try:
        out = os.popen("wmic cpu get NumberOfCores").read()
        n = sum(int(x) for x in out.split() if x.isdigit())
        if n:
            return n
    except Exception as _exc:
        domain_only(_exc)   # a bug is not a skippable condition
        pass
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except ImportError:
        pass
    return max(1, (os.cpu_count() or 4) // 2)


class _RateLimitedEvent:
    """
    A manager-backed Event whose ``is_set()`` is polled at most every 250 ms.

    The stop check sits inside the mix-combo loop (every 16 combos); through a
    manager proxy that would be thousands of cross-process round trips per
    task.  Caching bounds the cost while keeping Stop responsive to a quarter
    second -- well below the granularity at which a task can abandon work.
    """

    __slots__ = ("_ev", "_interval", "_last_t", "_last_v")

    def __init__(self, ev, interval=0.25):
        self._ev = ev
        self._interval = interval
        self._last_t = 0.0
        self._last_v = False

    def is_set(self, force=False):
        now = time.monotonic()
        if force or self._last_v or (now - self._last_t) >= self._interval:
            self._last_v = bool(self._ev.is_set())
            self._last_t = now
        return self._last_v

    def set(self):
        self._ev.set()
        self._last_v = True

    def __reduce__(self):           # rebuilt in the child around the SAME proxy
        return (_RateLimitedEvent, (self._ev, self._interval))


class _PlacementResult:
    """
    Stand-in for a scipy OptimizeResult when the seating solve was skipped.

    Module level, NOT nested in the worker.  A class defined inside a function
    cannot be pickled, so under the process executor every task reaching that
    line died with "Can't get local object" -- and because the results loop
    logs worker exceptions and continues, the sweep then reported "no feasible
    design" rather than failing.  Keep this at module scope.
    """

    __slots__ = ("fun", "x")

    def __init__(self, fun, x):
        self.fun = fun
        self.x = x


def _pad_worker_args(args, n):
    """
    Right-pad the worker args tuple to ``n`` with None.

    The tuple has grown twice (RunState, then the tally proxy).  Direct callers
    -- several tests build it by hand -- pass the shorter form, and padding
    keeps them meaningful rather than failing on an arity they do not care
    about.  A SHORT tuple is fine; a LONG one is a real mismatch and raises.
    """
    args = tuple(args)
    if len(args) > n:
        raise ValueError(f"worker args tuple has {len(args)} items, expected <= {n}")
    return args + (None,) * (n - len(args))


def _evaluate_geometry_worker(args):
    """
    Independent worker function for evaluating a batch of valid mix geometries.
    Placed at the top-level scope to ensure it can be pickled by multiprocessing on Windows natively!

    Returns ``(best_weight, best_config, n_evaluated, gate_candidates)``.  The
    fourth element is this task's TOP-M feasible designs as lean records --
    see gate_top_m_for_task for why the gate needs more than the task's single
    best.
    """
    tally = _SWEEP_TALLY
    (t_in, d_in, valid_mixes, true_length_ft, true_draft_in, filepath_matrix,
     gw_width_mid, gw_height_mid, enable_mesh, gw_fillet_ratio, freeboard_min_in,
     flare_max_deg, stop_event, multizone, num_zones, run_state,
     run_tally) = _pad_worker_args(args, 17)
    if run_tally is not None:
        tally = run_tally

    # The run's config, made real in THIS process before any physics runs.
    #
    # Under threads this only verifies (the singleton is already the parent's).
    # Under a spawned process it is what stops the worker evaluating the
    # canoe_suite.cfg defaults instead of the run: FACTOR_OF_SAFETY,
    # MAX_DENSITY_PCF, DESIGN_METHOD, RACE_CATEGORIES and the gate limits all
    # have import-time values, so a missing apply() would not raise -- it would
    # return a plausible weight for a design nobody asked for.  Hence the
    # fingerprint assertion inside, which fails the task loudly instead.
    _run_state_mod.ensure_applied(run_state)

    # Bail immediately if the user already pressed Stop (threads share the
    # single app-wide event object, so this reads live).
    if stop_event is not None and stop_event.is_set():
        return None

    # Longitudinal resolution (numerical-accuracy control, threaded via config so
    # the worker args tuple stays stable). None = the offset file's native rows;
    # the production default oversamples to converge the V(x)/M(x)/weight
    # integrations (native under-reads both weight and bending demand).
    _n_stations = getattr(config, "LONGITUDINAL_STATIONS", None)
    x_raw, z_raw, y_matrix = parse_godzilla_offsets(
        filepath_matrix, L_ft=true_length_ft, draft_in=true_draft_in,
        mold_depth_in=d_in, n_stations=_n_stations
    )

    if x_raw is None:
        if tally is not None:
            tally.skip("offsets could not be parsed at this mold depth")
        return None

    # c_bot IS the Neutral Axis height at each station!
    props = calculate_shell_properties(x_raw, z_raw, y_matrix, t_in)
    A_in2, A_top_in2, A_bot_in2, I_in4, Q_in3, c_top, c_bot, width_ft = props

    valid = A_in2 > 0
    x, A_ft2 = x_raw[valid], A_in2[valid] / 144.0
    A_top_ft2, A_bot_ft2 = A_top_in2[valid] / 144.0, A_bot_in2[valid] / 144.0
    I_in4, Q_in3 = I_in4[valid], Q_in3[valid]
    c_top, c_bot, width_ft = c_top[valid], c_bot[valid], width_ft[valid]
    # Cumulative shell-area table for the depth-zoned pour split (C3)
    cum_area = props.cum_area_in2[valid]
    z_grid   = props.z_grid_in
    # Cumulative first/second moments for the multi-zone composite path
    cum_Az   = props.cum_Az_in3[valid]
    cum_Iz   = props.cum_Iz_in4[valid]
    # Lateral inertia + half-breadth profile for the transverse (hoop) gate
    cum_Iy   = props.cum_Iy_in4[valid]
    yhalf    = props.yhalf_in[valid]
    sheer_in = c_bot + c_top          # per-station section top [in above keel]
    L = np.max(x)
    dx = L / (len(x_raw) - 1)

    # Real lower-hull section for the freeboard/gunwale gate.  Depends only on
    # the offsets, the waterline and t — NOT on the mix — so it is hoisted out
    # of the mix loop.  (It is also independent of mold depth: the lower hull
    # sits entirely below the waterline.  The gate's freeboard height, which
    # DOES depend on d_in, is applied per-config at the call site below.)
    _lower_hull = lower_hull_section_properties(
        cum_area, cum_Az, cum_Iz, z_grid, true_draft_in)
    _freeboard_in = max(float(d_in) - float(true_draft_in), 1e-6)

    # ASCE LOADED-FREEBOARD, as a per-configuration gate.  The heaviest hull
    # that still clears freeboard_min_in with the 4-person crew aboard depends
    # only on the geometry and this task's mold depth, so it is one scalar here
    # and one comparison per configuration downstream.  The pre-sweep depth
    # floor uses an ESTIMATED hull mass and cannot bind the design that
    # actually wins; this can.  See structural.max_hull_weight_for_freeboard.
    # COMPUTED heaviest crew, never assumed to be the four.  With five races a
    # 3M crew of large paddlers can rival a coed four, and guessing here would
    # silently admit hulls that swamp under the real worst case.
    _crew_max_lb = config.heaviest_crew_lbs()
    _hull_wt_max = _max_hull_weight_for_freeboard(
        d_in, freeboard_min_in, _crew_max_lb, x_raw, z_raw, y_matrix)

    best_weight = 999999.0
    best_config = None

    # --- OPTIMIZATION: Phase 1 ---
    _n_m = getattr(config, 'MAX_MIXES', 2)
    if _n_m == 1:
        standard_mix = [80.0]
    elif _n_m == 3:
        standard_mix = [80.0, 70.0, 60.0]
    else:
        standard_mix = [80.0, 60.0]

    # Seating mode: user-specified positions are taken verbatim (no SLSQP,
    # no drift variance — the user is describing physical reality); the
    # optimizer mode places paddlers, then the mix-selection evaluations
    # widen the envelopes by PADDLER_POS_VARIANCE_FT to cover human drift.
    fixed_pos = getattr(config, 'FIXED_PADDLER_POSITIONS', None)
    pos_var   = 0.0 if fixed_pos else float(
        getattr(config, 'PADDLER_POS_VARIANCE_FT', 0.0))

    # The flat seating vector spans all five ASCE races in RACE_CATEGORY_ORDER.
    _layout = config.crew_seat_layout()
    _nseat = config.crew_seat_vector_len()

    def _seats_dict(vec):
        """Flat vector -> {category: coords}, the form every consumer wants."""
        return {name: np.asarray(vec[sl], dtype=float) for name, sl in _layout}

    if fixed_pos:
        # User-specified seating.  Accepts the five category keys; the legacy
        # "4"/"2"/"3" keys still work and seed every crew of that size, so an
        # existing saved config keeps running rather than silently losing races.
        _parts = []
        for name, sl in _layout:
            n = sl.stop - sl.start
            got = fixed_pos.get(name) or fixed_pos.get(str(n))
            if got is None or len(got) != n:
                got = list(np.linspace(0.2 * L, 0.8 * L, n))
            _parts.append([float(q) for q in got])
        best_paddlers = np.array([q for part in _parts for q in part])
    else:
        starts0 = seating_start_points(L, n_starts=1)[0]
        initial_paddlers = list(starts0)
        bnds = tuple((2.0, L - 3.5) for _ in range(_nseat))
        # Analytic constraint Jacobians (paddler_placement_constraints): the
        # spacing/trim constraints are affine, so their gradients are exact
        # constants passed via each constraint's 'jac'.  SLSQP no longer
        # finite-differences the constraints, which is what the eps=0.5 bug
        # corrupted (a probe step the size of the 3.5 ft spacing margin gave a
        # garbage gradient → stall → discarded thickness).
        cons = paddler_placement_constraints(L)

        def objective_init(paddler_vars):
            # SCIPY BOUNDARY -- the one place a rejection legitimately becomes a
            # number.  SLSQP compares candidate objective values internally
            # (`if fx < self._lowest_f`), so handing it the non-numeric REJECTED
            # marker raises inside the solver and kills every worker.  An
            # infeasible seating must instead rank LAST, which is what the large
            # finite penalty does.  Nothing that reports, stores or displays a
            # weight may do this -- see structural._RejectedWeight.
            full_vars = np.concatenate((paddler_vars, standard_mix))
            # Nominal positions during placement (variance applied later at
            # mix selection) keeps SLSQP cost at one beam solve per case.
            # Arrangements are OFF here: this is the seating placement proxy,
            # and enveloping 40 orderings per probe would multiply the solve
            # cost for a objective that only needs to rank seat positions. The
            # binding evaluation below runs them.
            return evaluate_canoe_weight(full_vars, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in,
                                         gw_width_mid=gw_width_mid, gw_height_mid=gw_height_mid,
                                         enable_mesh=enable_mesh, gw_fillet_ratio=gw_fillet_ratio,
                                         crew_cases=config.crew_load_cases(
                                             _seats_dict(paddler_vars)),
                                         # Arrangements OFF here: this is the
                                         # seating PLACEMENT proxy and runs once
                                         # per SLSQP probe.  Enveloping 40
                                         # orderings per probe multiplies the
                                         # solve cost for an objective that only
                                         # has to rank seat positions; the
                                         # binding evaluation below runs them.
                                         enumerate_arrangements=False,
                                         position_variance_ft=0.0,
                                         cum_area_in2=cum_area, z_grid_in=z_grid,
                                         # NOTE: no Kupfer arrays here on purpose — this is the SLSQP
                                         # SEATING objective (one call per probe); it uses the fast
                                         # Rankine weight proxy.  The binding Kupfer feasibility gate
                                         # is applied at MIX SELECTION and the winner re-eval below.
                                         rocker_ft=_struct_rocker_ft(t_in))

        _objective_init_raw = objective_init

        def objective_init(paddler_vars):            # noqa: F811 - wraps above
            return structural.as_optimizer_penalty(
                _objective_init_raw(paddler_vars))

        # eps is SLSQP's finite-difference step [ft].  The CONSTRAINTS carry
        # analytic Jacobians (paddler_placement_constraints), so eps touches
        # only the OBJECTIVE, which wraps evaluate_canoe_weight.
        #
        # HISTORY — why eps used to be 0.1, and why it no longer is.  Paddler
        # loads were formerly SNAPPED to the nearest station, which made the
        # objective a STEP function of seat position: byte-identical while a
        # paddler moved within +-dx/2 (0.25 ft), jumping at station edges.  A
        # finite difference smaller than that returned EXACTLY zero gradient,
        # so SLSQP saw a flat landscape, declared a stationary point
        # immediately and returned (near) its initial guess.  The old
        # "eps=0.01 -> 36/55 non-converged" reading was that pathology, not
        # underflow, and "eps=0.1 -> 0/55 non-converged" was false success:
        # a zero gradient looks exactly like convergence.  eps=0.1 only ever
        # "worked" because it was large enough to straddle a step.
        #
        # apply_realistic_paddler_loads now deposits every load linearly
        # between the two bracketing stations, so the objective is continuous
        # and piecewise-linear in seat position.  Measured forward-difference
        # gradient at the pipeline x0 along v[1]:
        #   h=1e-8 -> -0.096244   h=1e-4 -> -0.096252   h=1e-2 -> -0.096538
        #   h=1e-3 -> -0.096278   h=0.1  -> -0.101766 (6% high, over-large step)
        # Stable over six orders of magnitude, so a small step is now both
        # valid and more accurate.  1e-3 ft (0.012 in) is far below any
        # physical seat-placement tolerance and well above float64 noise.
        # BEST-OF-N over deterministic feasible starts: the landscape is
        # multimodal (see SEATING_N_STARTS), so a single solve is not global.
        # Starts are seeded, so the sweep stays reproducible run to run.
        res_init = None
        for _x0 in seating_start_points(L):
            _r = minimize(fun=objective_init, x0=_x0, method='SLSQP',
                          bounds=bnds, constraints=cons,
                          options=dict(SEATING_SLSQP_OPTS))
            if not np.isfinite(_r.fun):
                continue
            _v = _r.x
            # keep only starts that ended inside the spacing rule -- checked
            # WITHIN each crew, never across the boundary between two crews.
            _gaps = [_v[j + 1] - _v[j]
                     for _n, _s in _layout for j in range(_s.start, _s.stop - 1)]
            if _gaps and min(_gaps) < 3.4:
                continue
            if res_init is None or _r.fun < res_init.fun:
                res_init = _r
        if res_init is None:            # nothing feasible — fall back to the guess
            res_init = minimize(fun=objective_init, x0=initial_paddlers,
                                method='SLSQP', bounds=bnds, constraints=cons,
                                options=dict(SEATING_SLSQP_OPTS))

        best_paddlers = res_init.x

    # Paddler spacing sanity (optimizer mode only — user-specified seating is
    # physical reality and is never rejected). Mix-independent, so checked once.
    # Gaps are checked WITHIN each crew, never across the boundary between two
    # crews: seats belonging to different races are independent, and comparing
    # 2F's bow seat against 2M's stern seat yields a negative "gap" that drops
    # every geometry.  The hardcoded 4+2 indices did exactly that once the
    # vector grew to five races.
    v = best_paddlers
    _crew_gaps = [v[j + 1] - v[j]
                  for _n, _s in _layout for j in range(_s.start, _s.stop - 1)]
    if not fixed_pos and _crew_gaps and min(_crew_gaps) < 3.4:
        if tally is not None:
            tally.skip("paddler spacing unsatisfiable — whole geometry dropped "
                       "before any mix was costed", len(valid_mixes))
        # FOUR values on every path: the consumer unpacks four, so a
        # three-element early return raises ValueError mid-sweep. Caught
        # by the arity test, not by a run.
        return best_weight, None, 0, []   # rejected before any combo was costed

    # ══════════════════════════════════════════════════════════════════════
    #  MULTI-ZONE (I-BEAM) PATH — pour lines swept as independent variables
    # ══════════════════════════════════════════════════════════════════════
    if multizone and num_zones >= 2:
        from structural import (evaluate_multizone_weight, multizone_pour_grids,
                                ibeam_zone_stack)
        pour_grids = multizone_pour_grids(d_in, num_zones)
        # All five ASCE races, each with its OWN seats and OWN weights.
        seats = _seats_dict(best_paddlers)
        crew_cases = config.crew_load_cases(seats)
        na_ref = c_bot            # homogeneous NA height (τ reference)

        # STRICT I-BEAM HEURISTIC (replaces brute-force product stacking):
        # zone count is decoupled from mix count, and for each density pair we
        # test ONLY the structurally-sensible I-beam stack — heaviest mix at
        # the top+bottom extremes, lightest at the core.  No inverted or
        # uniform stacks are evaluated, so the full-resolution density grid
        # stays tiny (one stack per pair, not num_mixes^num_zones).
        # Freeboard gate is folded into evaluate_multizone_weight (via
        # freeboard_min_in/flare_max_deg), so a light-top stack is rejected as
        # infeasible right here.
        #
        # LIGHTEST-FIRST BRANCH & BOUND (same admissible bound + helpers as the
        # single-zone path).  The multi-zone weight is Σ densityₖ·Vₖ + the
        # gunwale rail, with the zone volumes telescoping to V_total
        # independent of the pour-line fractions.  The rail is poured from the
        # TOP zone (density ≥ min(combo)), so its weight ≥ min(combo)·V_gw where
        # V_gw = ∫ A_rail/144 dx (fixed per task).  Hence
        #     weight ≥ min(combo)·(V_total + V_gw)
        # remains a valid lower bound over ALL of a combo's fracs.  Walking
        # combos ascending in min(combo), the first whose bound reaches the
        # incumbent ends the walk (verified bit-identical vs full enumeration).
        from structural import _cum_at, gunwale_rail_section
        _A_hi = _cum_at(cum_area, z_grid, sheer_in)
        _A_lo = _cum_at(cum_area, z_grid, np.zeros(len(x)))
        _V_total = float(trapezoid((_A_hi - _A_lo) / 144.0, x))
        _rail_b = gunwale_rail_section(x, t_in, gw_width_mid, gw_height_mid,
                                       gw_fillet_ratio, sheer_in)
        _V_gw = (0.0 if _rail_b is None else
                 float(trapezoid((_rail_b.A_gw + _rail_b.A_fil) / 144.0, x)))
        _V_bound = _V_total + _V_gw
        _mz_order = min_density_bound_order(valid_mixes)
        # Hard cap on distinct mix densities across the canoe (team can batch
        # only this many).  Enforced BOTH in enumeration (skip an over-cap
        # stack before costing) and re-checked inside evaluate_multizone_weight
        # (n_mixes_max=), so an over-cap design can never win.
        _n_mixes_max = int(getattr(config, "N_MIXES_MAX", 2))

        best_mz = None            # (weight, zone_stack, fracs)
        # Every feasible candidate this task sees, for the gate's pool.
        _gate_cands = []
        n_evals = 0               # combos×fracs actually costed (rest pruned)
        for _k, _mi in enumerate(_mz_order):
            if (_k & 0xF) == 0 and stop_event is not None and stop_event.is_set():
                return None
            current_mixes = valid_mixes[_mi]
            # Bound: ascending min(combo), so once a combo's bound reaches the
            # incumbent no later combo (larger min) can beat it either.
            #
            # AN EXTERNAL BOUND WAS TRIED HERE AND REMOVED.  The idea was to
            # hand a pricing companion the buildable-fallback weight -- provably
            # achievable in every pricing pool -- so it could stop early instead
            # of walking from nothing.  Admissible, and worth 0.0% on a feasible
            # pool and 2.6% on an infeasible one.  It does nothing because the
            # walk is ALREADY ordered ascending by min(combo) and the lightest
            # design uses the lightest mix, so the incumbent is set on the first
            # feasible combo and is immediately as tight as any external bound.
            # Do not re-add it; see KNOWN_ISSUES, "Performance".
            if (best_mz is not None
                    and min_density_weight_bound(current_mixes, _V_bound) >= best_mz[0]):
                break
            if len({round(float(d), 3) for d in current_mixes}) < 2:
                if tally is not None:
                    tally.skip("combo has <2 distinct densities — no I-beam to "
                               "build", len(pour_grids))
                continue          # need ≥2 distinct mixes for a real I-beam
            stack = ibeam_zone_stack(current_mixes, num_zones)
            if len({round(float(d), 3) for d in stack}) > _n_mixes_max:
                if tally is not None:
                    tally.skip(f"stack pours more than N_mixes_max="
                               f"{_n_mixes_max} distinct densities",
                               len(pour_grids))
                continue          # N_mixes_max cap — skip over-cap stacks
            for fracs in pour_grids:
                w = evaluate_multizone_weight(
                    None, None, stack, fracs,
                    x, dx, width_ft, t_in,
                    cum_area, cum_Az, cum_Iz, z_grid, sheer_in, na_ref,
                    config.get_mix_properties,
                    crew_cases=crew_cases, position_variance_ft=pos_var,
                    enable_mesh=enable_mesh, rocker_ft=_struct_rocker_ft(t_in),
                    freeboard_min_in=freeboard_min_in, flare_max_deg=flare_max_deg,
                    waterline_in=true_draft_in,
                    gw_width_mid=gw_width_mid, gw_height_mid=gw_height_mid,
                    gw_fillet_ratio=gw_fillet_ratio, n_mixes_max=_n_mixes_max,
                    cum_Iy=cum_Iy, yhalf_grid=yhalf,
                    tally=tally, depth_in=d_in,
                    hull_weight_max_lb=_hull_wt_max)
                n_evals += 1
                if not structural.is_rejected(w):
                    # EVERY feasible candidate is offered to the gate, not
                    # just this task's best -- see gate_top_m_for_task.
                    _gate_cands.append((float(w), list(stack), tuple(fracs),
                                        float(d_in)))
                    if best_mz is None or w < best_mz[0]:
                        best_mz = (float(w), stack, fracs)
        if best_mz is not None:
            best_weight, cand_stack, cand_fracs = best_mz
            det = evaluate_multizone_weight(
                None, None, cand_stack, cand_fracs,
                x, dx, width_ft, t_in,
                cum_area, cum_Az, cum_Iz, z_grid, sheer_in, na_ref,
                config.get_mix_properties, crew_cases=crew_cases,
                position_variance_ft=pos_var, enable_mesh=enable_mesh,
                rocker_ft=_struct_rocker_ft(t_in),
                freeboard_min_in=freeboard_min_in, flare_max_deg=flare_max_deg,
                waterline_in=true_draft_in,
                gw_width_mid=gw_width_mid, gw_height_mid=gw_height_mid,
                gw_fillet_ratio=gw_fillet_ratio, n_mixes_max=_n_mixes_max,
                cum_Iy=cum_Iy, yhalf_grid=yhalf,
                hull_weight_max_lb=_hull_wt_max,
                return_details=True)
            best_config = {
                'thickness': t_in, 'depth': d_in,
                'mixes': list(cand_stack),          # bottom→top zone stack
                'result': None, 'multizone': True,
                'pour_fracs': tuple(cand_fracs),
                'x': x, 'dx': dx, 'width_ft': width_ft,
                'cum_area': cum_area, 'cum_Az': cum_Az, 'cum_Iz': cum_Iz,
                # cum_Iy/yhalf are the LATERAL section terms.  Without them a
                # re-evaluation of this winner cannot form the Kupfer biaxial
                # state and silently returns no utilisation, so any post-hoc
                # variant study (e.g. the top-flange stability trade) would come
                # back empty rather than wrong -- store them with the rest.
                'cum_Iy': cum_Iy, 'yhalf': yhalf,
                'z_grid': z_grid, 'sheer_in': sheer_in, 'na_ref': na_ref,
                'gw_width_mid': gw_width_mid, 'gw_height_mid': gw_height_mid,
                'gw_fillet_ratio': gw_fillet_ratio,
                'enable_mesh': enable_mesh,
                # ALL FIVE seating sets are stored: the refinement verification
                # re-evaluates this design on a finer grid and must reproduce
                # its exact load state.  Storing a SUBSET would let the verifier
                # pass while checking fewer races than the sweep gated on --
                # silently verifying a different, easier boat.
                'seats': {k: np.asarray(v, dtype=float)
                          for k, v in seats.items()},
                # What the envelope actually covered, per race: evaluated vs
                # physically possible orderings and which seats were pinned.
                # Travels with the winner so the report cannot omit it.
                'arrangement_disclosure':
                    (det.get("struct_data") or {}).get("arrangement_disclosure"),
                # Legacy aliases, derived from the same seats mapping so they
                # can never disagree with it.
                'coords_4': np.asarray(seats["4_coed"], dtype=float),
                'coords_2': np.asarray(seats["2M"], dtype=float),
                'coords_3': np.asarray(seats["3M"], dtype=float),
                'pos_var_ft': pos_var, 'seating_fixed': bool(fixed_pos),
                'peak_util': float(np.max(det["worst_util"])),
                # Transverse (hoop) results from the SAME evaluator call the
                # gate used — single source of truth, no gate/report drift.
                'hoop_util': det.get("hoop_util"),
                'hoop_gov_zone': det.get("hoop_gov_zone"),
                'hoop_dc_homog': det.get("hoop_dc_homog"),
                # Kupfer biaxial gate + comparisons (old independent longitudinal
                # = worst_util, old transverse = hoop_util, Rankine, Kupfer).
                'long_dc': float(np.max(det["worst_util"])),
                'hoop_dc': (None if det.get("hoop_util") is None
                            else float(np.max(det["hoop_util"]))),
                'rankine_dc': det.get("rankine_max"),
                'kupfer_dc': det.get("kupfer_max"),
                'kupfer_gov': det.get("kupfer_gov"),
                'x_raw': x_raw, 'z_raw': z_raw, 'y_matrix': y_matrix,
            }
            # Real gunwale state, same contract as the 2-mix path, so the UI's
            # all-stations flare report works on multi-zone runs too (the
            # default for 2+ mixes).  Carry the FULL per-station section and
            # re-run the all-stations gate for the winner to record where it
            # actually governs.
            _mz_fb_in = max(float(np.max(sheer_in)) - float(true_draft_in), 1e-6)
            _mz_allow = float(config.get_mix_properties(
                float(cand_stack[-1])).get('strength_t', 50.0))
            if enable_mesh:
                _mz_allow = max(_mz_allow, 800.0)
            _, _, _mz_stress, _mz_gov = _freeboard_flare_gate(
                det["struct_data"]["moment"]["race_2"], t_in, _mz_fb_in,
                flare_max_deg, _mz_allow, _lower_hull)
            best_config['lower_hull'] = _lower_hull
            best_config['freeboard_in'] = _mz_fb_in
            best_config['flare_stress_psi'] = _mz_stress
            best_config['flare_gov_station'] = _mz_gov
            if det.get("flare_req_deg") is not None:
                best_config['flare_req_deg'] = det["flare_req_deg"]
            best_config['result'] = _PlacementResult(best_weight, best_paddlers)
        # Trim to the task's top-m by WEIGHT (never by screen D/C: that is
        # the value the gate distrusts) and hand back lean records.
        _m = gate_top_m_for_task(len(_gate_cands))
        _gate_cands.sort(key=lambda c: c[0])
        _lean = []
        for _w, _stack, _fracs, _d in _gate_cands[:_m]:
            _lean.append({
                "mixes": _stack, "pour_fracs": _fracs, "depth": _d,
                "thickness": float(t_in), "multizone": True,
                "weight_lbs": _w, "pos_var_ft": float(pos_var),
                "enable_mesh": bool(enable_mesh),
                "gw_width_mid": float(gw_width_mid),
                "gw_height_mid": float(gw_height_mid),
                "gw_fillet_ratio": float(gw_fillet_ratio),
                "seats": {k: [float(x) for x in v]
                          for k, v in (seats or {}).items()},
            })
        return best_weight, best_config, n_evals, _lean

    _GATE_ATTEMPTS = 25          # bound the extra return_arrays evaluations
    _TIE_TOL_LB = 0.25           # weights this close count as a tie (C4.3)

    # ── Mix-grid branch & bound ───────────────────────────────────────────────
    # Only the _GATE_ATTEMPTS lightest feasible combos are ever inspected by the
    # flare gate below, so a combo that provably cannot reach that set need not
    # be evaluated at all.
    #
    # Admissible bound: every station carries at least the LIGHTEST mix in the
    # combo, whichever area basis the evaluator ends up using (cumulative split
    # or the legacy NA split — gunwale area only ever adds), hence
    #       weight  >=  min(combo) * K,     K = ∫ min(A_shell, A_bot+A_top) dx
    # K is fixed for the task, so the bound rises monotonically with
    # min(combo).  Walking the grid in ascending min(combo) therefore lets the
    # FIRST combo whose bound exceeds the current 25th-lightest feasible weight
    # terminate the entire walk — everything after it is bounded no lower.
    #
    # This is exact, not a heuristic: the surviving _GATE_ATTEMPTS set is
    # identical to full enumeration (verified against the 903-combo grid), so
    # the winner is unchanged.  It is the INVERSE of pruning by strength:
    # the sweep minimises weight, and ~95% of combos pass the stress check, so
    # walking strongest-first prunes almost nothing (first failure lands at
    # combo 858/903) while walking lightest-first prunes ~60%.  Bound + order
    # come from the shared helpers (also used by the multi-zone path).
    _K_lb = float(trapezoid(np.minimum(A_ft2, A_bot_ft2 + A_top_ft2), x))
    _order = min_density_bound_order(valid_mixes)
    _cutoff = []       # max-heap (negated weights) of the lightest feasible
    _n_evaluated = 0   # combos actually costed (the rest are bound-pruned)

    feasible = []      # (weight, mixes) for every combo passing shell checks
    for _k, _mi in enumerate(_order):
        # Responsive Stop: this loop is the bulk of a task's runtime (one
        # evaluation per mix combo), so check every 16 combos.
        if (_k & 0xF) == 0 and stop_event is not None and stop_event.is_set():
            return None
        current_mixes = valid_mixes[_mi]

        # Bound check — _order is ascending in min(combo), so once this trips
        # no later combo can enter the gate set either.
        if (len(_cutoff) >= _GATE_ATTEMPTS
                and min_density_weight_bound(current_mixes, _K_lb) > -_cutoff[0]):
            break

        full_vars = np.concatenate((best_paddlers, current_mixes))
        # Mix selection evaluates with the drift variance so the winning
        # design stays feasible when paddlers wander off-station.
        weight = evaluate_canoe_weight(full_vars, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in,
                                       gw_width_mid=gw_width_mid, gw_height_mid=gw_height_mid,
                                       enable_mesh=enable_mesh, gw_fillet_ratio=gw_fillet_ratio,
                                       crew_cases=config.crew_load_cases(
                                           _seats_dict(best_paddlers)),
                                       position_variance_ft=pos_var,
                                       cum_area_in2=cum_area, z_grid_in=z_grid,
                                       cum_Az_in3=cum_Az, cum_Iy_in4=cum_Iy, yhalf_in=yhalf,
                                       rocker_ft=_struct_rocker_ft(t_in),
                                       # Only THIS call tallies: the SLSQP seating
                                       # objective and the flare-gate re-evaluation
                                       # below run over the same combos, and
                                       # counting them would triple the histogram.
                                       tally=tally, depth_in=d_in)
        _n_evaluated += 1

        # Tensile stress filter — evaluate_canoe_weight returns 1e6 on failure
        if structural.is_rejected(weight):
            continue
        # ASCE loaded-freeboard, against THIS design's own weight (see the
        # multi-zone path and structural.max_hull_weight_for_freeboard).  A
        # heavier hull floats deeper and delivers less freeboard, so the pre-
        # sweep depth floor -- built from an ESTIMATED hull mass -- cannot bind
        # the winner.  This can.
        if np.isfinite(_hull_wt_max) and float(weight) > _hull_wt_max:
            if tally is not None:
                tally.skip("loaded freeboard: hull heavier than the 4-person "
                           "freeboard rule allows at this mold depth")
            continue
        feasible.append((float(weight), current_mixes))

        # Track the _GATE_ATTEMPTS lightest weights seen: -_cutoff[0] is the
        # heaviest of them, i.e. the bar a later combo must beat.
        if len(_cutoff) < _GATE_ATTEMPTS:
            heapq.heappush(_cutoff, -float(weight))
        elif -float(weight) > _cutoff[0]:
            heapq.heapreplace(_cutoff, -float(weight))

    feasible.sort(key=lambda t: t[0])

    # ── Flare structural gate ──────────────────────────────────────────────────
    # Walk the candidates lightest-first and crown the first whose freeboard
    # wall survives at SOME flare angle in [0°, flare_max_deg].  Gating only
    # the single lightest combo and discarding the whole task on failure made
    # mesh-enabled sweeps report "0 configurations survived": mesh admits very
    # light mixes into the shell check, whose bare strength then fails here.
    winner = None                # {weight, mixes, flare_req, peak_util}
    for cand_weight, cand_mixes in feasible[:_GATE_ATTEMPTS]:
        # Once candidates are decisively heavier than the current winner,
        # stop — the walk is sorted lightest-first.
        if winner is not None and cand_weight > winner["weight"] + _TIE_TOL_LB:
            break
        _fc_full = np.concatenate((best_paddlers, cand_mixes))
        _sr = evaluate_canoe_weight(
            _fc_full, x, dx, A_ft2, I_in4, Q_in3,
            c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in,
            gw_width_mid=gw_width_mid,
            gw_height_mid=gw_height_mid,
            enable_mesh=enable_mesh,
            gw_fillet_ratio=gw_fillet_ratio,
            return_arrays=True,
            crew_cases=config.crew_load_cases(_seats_dict(best_paddlers)),
            position_variance_ft=pos_var,
            cum_area_in2=cum_area, z_grid_in=z_grid,
            cum_Az_in3=cum_Az, cum_Iy_in4=cum_Iy, yhalf_in=yhalf,
            rocker_ft=_struct_rocker_ft(t_in),
        )
        flare_req_deg = None
        flare_stress_psi = None
        gate_ok = True
        if hasattr(_sr, 'M_race_2') and _sr.M_race_2 is not None:
            # Freeboard-wall strength = the STRONGEST mix in the combo (the
            # thin structural cantilever is poured from the heavy mix; the
            # light mix is reserved for low-stress volume).  Using
            # min(cand_mixes) once rejected every multi-mix combo.
            _fb_den = float(np.max(cand_mixes))
            _allow_psi = config.get_mix_properties(_fb_den).get('strength_t', 50.0)
            if enable_mesh:
                _allow_psi = max(_allow_psi, 800.0)
            # ALL-STATIONS gate: per-station moment vs per-station real
            # section, so a small off-midship section is caught even when the
            # moment peaks elsewhere.  gov_st is the true governing station.
            gate_ok, flare_req_deg, flare_stress_psi, gov_st = \
                _freeboard_flare_gate(
                    _sr.M_race_2, t_in, _freeboard_in, flare_max_deg,
                    _allow_psi, _lower_hull)
        if tally is not None:
            # Stage 2: this combo cleared the stress gates at mix selection and
            # has now been through the freeboard walk, so its outcome is final.
            _fb_dc = (None if flare_stress_psi is None
                      else float(flare_stress_psi) / max(_allow_psi, 1e-9))
            _pk = (None if getattr(_sr, "util_bot_t", None) is None else
                   float(max(np.max(_sr.util_bot_t), np.max(_sr.util_top_t))))
            tally.record_stage2(
                None if gate_ok else "freeboard_flare",
                {"freeboard wall": _fb_dc},
                {"longitudinal (Rankine)": _pk},
                t_in=t_in, depth_in=d_in, mixes=cand_mixes,
                weight=float(cand_weight), partial=not gate_ok,
                gov=("the freeboard wall at its best flare angle in "
                     f"0–{float(flare_max_deg):.0f}°" if not gate_ok else None))
        if not gate_ok:
            continue

        # C4.3 tie-break: among gate-passing candidates of (near-)equal
        # weight, prefer the one with the LOWEST peak tension D/C — same
        # weight, more margin everywhere.
        peak_util = 1.0
        if getattr(_sr, "util_bot_t", None) is not None:
            peak_util = float(max(np.max(_sr.util_bot_t),
                                  np.max(_sr.util_top_t)))
        if winner is None or peak_util < winner["peak_util"]:
            winner = {"weight": cand_weight, "mixes": cand_mixes,
                      "flare_req": flare_req_deg, "peak_util": peak_util,
                      "flare_stress_psi": flare_stress_psi,
                      "gov_station": gov_st if gate_ok else 0}

    if winner is not None:
        best_weight = winner["weight"]
        best_config = {
            'thickness': t_in, 'depth': d_in, 'mixes': winner["mixes"],
            'result': None,   # replaced by the DummyResult below
            'x': x, 'dx': dx, 'A_ft2': A_ft2, 'I_in4': I_in4, 'Q_in3': Q_in3,
            'c_top': c_top, 'c_bot': c_bot, 'width_ft': width_ft,
            'A_top_ft2': A_top_ft2, 'A_bot_ft2': A_bot_ft2,
            'gw_width_mid': gw_width_mid, 'gw_height_mid': gw_height_mid,
            'enable_mesh': enable_mesh,
            'seats': {k: np.asarray(v, dtype=float)
                      for k, v in _seats_dict(best_paddlers).items()},
            'coords_3': np.asarray(_seats_dict(best_paddlers)["3M"],
                                   dtype=float),
            'pos_var_ft': pos_var,
            'seating_fixed': bool(fixed_pos),
            'cum_area': cum_area, 'z_grid': z_grid,
            'cum_Az': cum_Az, 'cum_Iy': cum_Iy, 'yhalf': yhalf,
            'peak_util': winner["peak_util"],
            # Real gunwale/flare state, so the UI reports the sweep's own
            # numbers instead of recomputing them from its own copy.  The FULL
            # per-station section is carried so the UI re-runs the all-stations
            # gate (mark_structural persists only scalars, so arrays here are
            # safe; export serialises non-primitives to str, also safe).
            'flare_stress_psi': winner["flare_stress_psi"],
            'flare_gov_station': int(winner["gov_station"]),
            'freeboard_in': _freeboard_in,
            'lower_hull': _lower_hull,
            # Raw geometry for 3-D hull preview
            'x_raw': x_raw, 'z_raw': z_raw, 'y_matrix': y_matrix,
        }
        if winner["flare_req"] is not None:
            best_config['flare_req_deg'] = winner["flare_req"]
        dr = _PlacementResult(best_weight, best_paddlers)
        best_config['result'] = dr

    # Report combos actually costed, not the grid size — the bound prunes the
    # rest, so len(valid_mixes) would overstate the work by ~2x.
    # The 2-mix path has no gate evaluator yet (the gate calls
    # evaluate_multizone_weight). Return an EMPTY candidate list so the
    # arity matches and the gate's pool is honestly empty rather than
    # silently absent -- gate_pool_fed then reports it.
    return best_weight, best_config, _n_evaluated, []

def describe_depth_narrowing(floor_entered, floor_raised_to, depth_max,
                            depth_step, n_searched):
    """
    State, with numbers, that a raised floor SHRANK an absolute-maximum search.

    Under the old span semantics the whole window rose with the floor and the
    search stayed the same height, so a raised floor was worth a line and
    nothing more.  A maximum does not move: the floor rises INTO it and the
    range shrinks.  That is correct -- a ceiling the program moves is not a
    ceiling -- but it is invisible if all we print is the new floor, and on a
    hull where the 4-person check binds the user watches the depth count drop
    with no stated cause.  "Fewer depths than I asked for" reads as a bug.

    Returns None when nothing narrowed, so callers can append it or not.
    """
    if floor_raised_to is None or depth_max is None:
        return None
    if float(floor_raised_to) <= float(floor_entered):
        return None
    n_entered = len(np.arange(float(floor_entered), float(depth_max),
                              float(depth_step)))
    return (f"NARROWED by the 4-person floor: you asked for "
            f"{float(floor_entered):.2f}–{float(depth_max):.2f} in "
            f"({n_entered} depths); the 4-person waterline raises the floor to "
            f"{float(floor_raised_to):.2f} in, and your maximum does not move "
            f"with it, so the search is {float(floor_raised_to):.2f}–"
            f"{float(depth_max):.2f} in ({int(n_searched)} depths). Raise the "
            f"maximum to search as wide as before")


def hull_offsets_path(name="off1.mlt"):
    """
    Candidate locations for a data file, most-preferred first.

    Thin alias for `asset_paths.asset_search_paths`, kept because callers and
    tests already use this name.  The logic lives in ONE module now: it was
    reimplemented per-file five times, and five times it was wrong in a frozen
    build.  See asset_paths for the full account.
    """
    from asset_paths import asset_search_paths
    return asset_search_paths(name)


def rehydrate_gate_winner(lean, filepath_matrix, L_ft, draft_in, n_stations):
    """
    Rebuild a FULL config from a lean gate record.

    The gate pool holds lean records on purpose -- 13 fields instead of a 74 KB
    config dominated by hull geometry identical for every design at one depth.
    But when the gate PROMOTES one (the screen winner did not survive
    refinement and a heavier candidate did), that record became
    `global_best_config`, and the report path immediately did
    `global_best_config['x']`:

        KeyError: 'x'

    An entire sweep -- search, gate, verification -- lost at the report step, on
    the one path that exists BECAUSE the gate did its job.  Found by running the
    frozen CLI end to end; no test covered promotion followed by reporting.

    The lean record carries thickness, depth, mixes, pour_fracs and seats --
    enough to rebuild the geometry deterministically, by the same route the
    worker uses.  Nothing is guessed.
    """
    t_in = float(lean["thickness"])
    depth = float(lean["depth"])
    x_raw, z_raw, y_mat = parse_godzilla_offsets(
        filepath_matrix, L_ft=L_ft, draft_in=draft_in,
        mold_depth_in=depth, n_stations=n_stations)
    if x_raw is None:
        return None
    props = calculate_shell_properties(x_raw, z_raw, y_mat, t_in)
    (A_in2, A_top_in2, A_bot_in2, I_in4, Q_in3,
     c_top, c_bot, width_ft) = props
    valid = A_in2 > 0
    # Derived EXACTLY as _evaluate_geometry_worker derives them.  The field
    # names on ShellProperties are `cum_area_in2` / `cum_Az_in3` /
    # `cum_Iz_in4` / `cum_Iy_in4` / `yhalf_in` / `z_grid_in`, and `sheer_in` is
    # not a field at all but `c_bot + c_top`.  Writing this from memory
    # produced six wrong names and a silent None for the lateral terms, which
    # would have left the promoted winner unable to form its Kupfer state --
    # quiet, not loud.
    x = x_raw[valid]
    c_top_v, c_bot_v = c_top[valid], c_bot[valid]
    full = dict(lean)
    full.update({
        "x": x,
        "dx": float(np.max(x)) / max(len(x_raw) - 1, 1),
        "width_ft": width_ft[valid],
        "cum_area": props.cum_area_in2[valid],
        "cum_Az": props.cum_Az_in3[valid],
        "cum_Iz": props.cum_Iz_in4[valid],
        "cum_Iy": props.cum_Iy_in4[valid],
        "yhalf": props.yhalf_in[valid],
        "z_grid": props.z_grid_in,
        "sheer_in": c_bot_v + c_top_v,
        "na_ref": c_bot_v,
        "A_ft2": A_in2[valid] / 144.0,
        "A_top_ft2": A_top_in2[valid] / 144.0,
        "A_bot_ft2": A_bot_in2[valid] / 144.0,
        "I_in4": I_in4[valid], "Q_in3": Q_in3[valid],
        "c_top": c_top_v, "c_bot": c_bot_v,
        "result": None,
        # Provenance: this winner came from a gate PROMOTION, not the screen.
        "rehydrated_from_gate": True,
    })
    if full.get("seats"):
        full["coords_4"] = np.asarray(full["seats"]["4_coed"], dtype=float)
        full["coords_2"] = np.asarray(full["seats"]["2M"], dtype=float)
        full["coords_3"] = np.asarray(full["seats"]["3M"], dtype=float)
    return full


def harden_stdio(streams=None):
    """
    Make stdout/stderr survive a Windows console, before anything prints.

    A frozen console build gets a cp1252 stdout, and this module's very first
    banner line prints an emoji -- so `jaguar-cli.exe` died on line one with
    UnicodeEncodeError, every run, before any work began.  Emoji, box-drawing
    and block characters appear throughout (the executor banner, the New Best
    lines, the phase markers), so fixing them one at a time is whack-a-mole.
    The STREAM is the right place.

    ``errors="replace"`` is the part that matters.  UTF-8 alone still raises if
    the console cannot render a glyph, and a sweep must never be lost because a
    decorative character would not fit the code page: a '?' in a banner costs
    nothing, a dead run costs hours.

    Returns the streams it successfully hardened, so a caller can assert.
    """
    done = []
    for st in (streams if streams is not None else (sys.stdout, sys.stderr)):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
            done.append(st)
        except Exception:                    # noqa: BLE001
            # DELIBERATELY BROAD: a stream that cannot be reconfigured -- a
            # pipe, a pytest capture, an already-wrapped tee -- is not a reason
            # to refuse to run.  Worst case the original encoding stands, which
            # is exactly where we were before.
            pass
    return done


class _ExecutorStalled(RuntimeError):
    """
    No sweep task completed within the stall window.

    Distinct from a task RAISING: a raise is a result, this is the absence
    of one.  It exists so a hung pool becomes a catchable event instead of
    a window that stops repainting.
    """


class PhaseClock:
    """
    Wall time per sweep PHASE, printed at the end of every run.

    This exists because a two-hour run could not say where its two hours
    went.  The full test suite has the same problem (KNOWN_ISSUES records a
    47x spread across identical trees with no explanation), and the cause in
    both cases is the same: nothing measured itself, so every discussion of
    cost was an estimate, and this project has had several confident estimates
    turn out wrong by more than 4x.

    Cheap on purpose -- one monotonic() per phase boundary, no per-call
    instrumentation -- so it is always on rather than a flag somebody has to
    remember to set when a run is already slow.
    """

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self._marks = []          # [(name, duration_s)], closed phases
        self._open = None         # (name, t_started) or None

    def mark(self, name):
        """Close the running phase and open `name`."""
        if not self.enabled:
            return
        now = time.monotonic()
        if self._open is not None:
            self._marks.append((self._open[0], now - self._open[1]))
        self._open = (name, now)

    def close(self):
        if not self.enabled:
            return
        if self._open is not None:
            self._marks.append((self._open[0],
                                time.monotonic() - self._open[1]))
            self._open = None

    def report(self, extra=None):
        """
        The table.  Returns the rows so a caller can carry them onward.

        ASCII ONLY, deliberately.  The box-drawing and block characters this
        file uses elsewhere raise UnicodeEncodeError on a cp1252 console, and
        this prints at the very END of a run that may have taken hours -- the
        exact place where an exception costs the most and buys the least.
        The call site guards it as well; this just removes the reason to.
        """
        if not self.enabled:
            return []
        self.close()
        total = sum(d for _n, d in self._marks) or 1e-9
        rule = "  " + "-" * 68
        print("")
        print(rule)
        print("  WHERE THE TIME WENT")
        for name, d in self._marks:
            bar = "#" * max(1, int(round(40.0 * d / total)))
            print(f"    {name:<34s} {d:8.1f} s  {100.0 * d / total:5.1f}%  "
                  f"{bar}")
        print(f"    {'TOTAL':<34s} {total:8.1f} s")
        if extra:
            for line in extra:
                print(f"    {line}")
        print(rule)
        return list(self._marks)


#: The post-search phases, as (fraction, name).  The SEARCH is only the first
#: of them, and it is the only one the progress bar used to know about: the bar
#: hit 1.0 and said "Done" while Richardson, the refinement gate, the winner
#: verification and the mix-development pricing still had an hour to run.  A
#: user watching that has every reason to force-quit a run that is working.
#:
#: The fractions are deliberately coarse.  The companion phases cannot be
#: counted in advance -- the gate's k depends on how many designs survived, and
#: the pricing depends on how many target mixes do not exist yet -- so this
#: reports WHICH PHASE is running rather than pretending to a precision it does
#: not have.  Announcing the phase is the honest version; a bar that
#: interpolates through work it cannot measure is the dishonest one.
SWEEP_PHASES = (
    (0.80, "search"),
    (0.84, "converged weight (Richardson)"),
    (0.90, "refinement gate"),
    (0.94, "winner verification"),
    (0.97, "mix-development pricing"),
    (1.00, "done"),
)


def announce_phase(progress_callback, frac, headline, detail=None,
                   clock=None, phase=None):
    """
    Say what is about to happen, on BOTH channels.

    The console line matters as much as the bar: a frozen build's window shows
    the log, and "the bar is at 100% and nothing is printing" is precisely the
    state that looks like a hang.  Every companion phase now names itself
    before it starts, with its cost where that is knowable.
    """
    if clock is not None:
        clock.mark(phase or headline)
    print(f"\n  ▶ {headline}")
    if detail:
        print(f"      {detail}")
    if progress_callback:
        try:
            progress_callback(float(frac), headline)
        except Exception as _exc:               # noqa: BLE001
            # DELIBERATELY BROAD.  The caller's progress widget is not worth a
            # sweep: this runs after the search, so an exception here would
            # discard work already completed.  Same rule as the other six.
            print(f"      (progress callback failed: "
                  f"{type(_exc).__name__}: {_exc})")


def run_grand_optimization(progress_callback=None, freeboard_min_in=6.0,
                           thickness_options_in=None, precision_level=3,
                           enable_gunwales=False, enable_mesh=False,
                           gw_min_width_in=0.5, gw_max_width_in=2.5,
                           gw_min_height_in=0.5, gw_max_height_in=3.0,
                           gw_steps=5, gw_fillet_ratio=1.0,
                           flare_max_deg=15.0, stop_event=None,
                           multizone=False, num_zones=None,
                           reports_dir=None,
                           depth_step_in=None, depth_span_in=None,
                           depth_max_in=None, freeboard_max_in=None,
                           density_step_pcf=None,
                           thickness_grid_overridden=False,
                           audit_log=True,
                           price_mix_development=False,
                           _companion=None):
    """
    ``_companion`` is an internal re-entry hook, not a user parameter.  The mix
    development spec needs the same sweep re-run against a RESTRICTED density
    set ("what could we build today?", "what if we develop these two mixes?"),
    which is cheap -- those grids hold a handful of densities -- but must not
    regenerate reports or recurse into spec-building.  Set it to the list of
    densities to search (or True for "existing mixes only") and the run returns
    a bare {weight, mixes} dict.
    """
    # Phase timing, on for every real run.  Companion re-entries share the
    # parent's console but not its clock: a nested sweep is ONE PHASE of the
    # parent (pricing), and letting it print its own table would report the
    # same seconds twice.
    _clock = PhaseClock(enabled=(_companion is None))
    _clock.mark("setup (mix viability, geometry)")
    if _companion is None:
        # WHAT YOU ARE WAITING FOR, said BEFORE the wait.  The phase table at
        # the end explains a run that already cost you an hour; this explains it
        # while you can still decide not to.  Pricing is named with its cost
        # because it was 93% of a run and nothing announced it.
        _phases = ["search", "converged weight", "refinement gate",
                   "winner verification"]
        _phases.append("mix-development pricing (SLOW: several more searches)"
                       if price_mix_development
                       else "mix-development pricing -- SKIPPED "
                            "(--price-mix-development to enable)")
        print("  This run will do: " + "; ".join(_phases) + ".")
    print("="*65)
    print("\U0001f6f6 ASCE CONCRETE CANOE: 5-DIMENSIONAL PARAMETRIC SWEEP")
    print("="*65)
    print("\nInitializing Engineering Suite...")

    # STRENGTH-FIT SANITY GUARD, stated up front.  A blocked tier changes what
    # this sweep gates on, and an overridden one changes what the result means;
    # neither may be discoverable only by reading the mix database afterwards.
    try:
        _guard_db = config.get_mix_db()
        _guard_lines = _guard_db.fit_guard_banner()
        # WHICH DATABASE, AND HOW MUCH IS IN IT.
        #
        # Every frozen run before this printed nothing here and silently opened
        # a database sqlite had just CREATED inside `_internal`: three seeded
        # design-intent mixes, zero of the 20 real lab results.  The weights
        # that came out were plausible, reproducible, and computed from design
        # intent rather than measurement -- the worst combination, because
        # nothing about them looked wrong.
        #
        # A path and a row count cost one line and make that impossible to miss
        # again.  Printed for EVERY run, not just when something is wrong: the
        # failure mode here is a number that looks right, so there is no
        # condition to trigger on.
        try:
            _lab_n = _guard_db.lab_result_count()
            print(f"  Mix database: {_guard_db.db_path}")
            print(f"    {_lab_n} lab result(s) on record.")
            if _lab_n == 0:
                print("    !! NO LAB RESULTS. Every strength below comes from "
                      "the seeded design-intent mixes,")
                print("    !! not from anything anyone has broken. Treat the "
                      "weights as relative, not absolute.")
        except Exception as _dbx:               # noqa: BLE001
            # DELIBERATELY BROAD: this is provenance reporting on a database
            # that has already opened.  Failing to COUNT rows must not stop a
            # sweep that is otherwise ready to run.
            print(f"  Mix database: provenance unavailable "
                  f"({type(_dbx).__name__}: {_dbx})")
        _fit_override_record = _guard_db.fit_override_record()
    except Exception as _exc:
        domain_only(_exc)   # a bug is not a skippable condition
        _guard_lines = []
        _fit_override_record = None
    if _guard_lines:
        print("  " + "─" * 68)
        for _ln in _guard_lines:
            print(f"  {_ln}")
        print("  " + "─" * 68)

    # NOT the working directory, whatever four bugs' worth of code
    # assumed when this was called `cwd`.  It is the module's own
    # folder -- `_internal` in a frozen build.  Assets are resolved
    # by asset_paths now; this remains only as the unfrozen default
    # for reports_dir.
    module_dir = os.path.dirname(os.path.abspath(__file__))
    # SEARCHED, not assumed.  See hull_offsets_path: a frozen run's cwd is
    # wherever the user double-clicked, and taking the first existing candidate
    # is what stops the fallback generator inventing a hull in silence.
    _hull_candidates = hull_offsets_path("off1.mlt")
    filepath_matrix = next((c for c in _hull_candidates if os.path.exists(c)),
                           _hull_candidates[-1])
    if not os.path.exists(filepath_matrix):
        print("  " + "!" * 68)
        print("  NO HULL OFFSET FILE FOUND. Looked in:")
        for _c in _hull_candidates:
            print(f"    {_c}")
        print("  A DEFAULT hull will be generated. Every number below then")
        print("  describes a boat nobody designed. Put off1.mlt beside the")
        print("  executable to analyse your own hull.")
        print("  " + "!" * 68)
    else:
        print(f"  Hull offsets: {filepath_matrix}")
    # SEARCHED, exactly like off1.mlt.  out.mlt supplies true_length_ft and
    # true_draft_in, which set the 4-person waterline, the depth FLOOR and
    # therefore the whole search grid -- so a missing one does not degrade a
    # result, it silently searches a DIFFERENT BOAT's depths.  Measured: a
    # frozen run wrote a 35-byte fallback into `_internal` and came back with
    # a 6.26 in waterline against the real 6.57 in, a floor 0.31 in low, and
    # a winner 2.2 lb light on a grid that was never the same grid.
    _rep_candidates = hull_offsets_path("out.mlt")
    filepath_report = next((c for c in _rep_candidates if os.path.exists(c)),
                           _rep_candidates[-1])

    # ── Report output directory ───────────────────────────────────────────────
    # The ASCE report PDFs are written to an EXPLICIT, absolute directory.
    # Historically they were written to bare relative filenames, so the current
    # working directory silently decided where they landed — under the GUI they
    # dirtied the repo root, and any test that ran a sweep overwrote the tracked
    # copies.  reports_dir defaults to the project root (the same anchor as the
    # input files) and is created if missing; callers pass a scratch/output dir
    # to keep the tree clean.  These PDFs are generated artifacts (git-ignored).
    # NOT `cwd` when frozen.  `cwd` here is the MODULE's directory, which in
    # a frozen build is `_internal` -- so the default wrote the team's PDFs
    # inside the application bundle, where nobody would look for them and a
    # reinstall would delete them.
    if reports_dir:
        reports_dir = os.path.abspath(reports_dir)
    elif getattr(sys, "frozen", False):
        reports_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        reports_dir = module_dir
    os.makedirs(reports_dir, exist_ok=True)
    _archive_existing_reports(reports_dir)

    def _report_path(name: str) -> str:
        return os.path.join(reports_dir, name)

    # ── Guard: ensure out.mlt exists so dimensions can be parsed ──────────────
    if not os.path.exists(filepath_report):
        print("  " + "!" * 68)
        print("  NO out.mlt FOUND. Looked in:")
        for _c in _rep_candidates:
            print(f"    {_c}")
        print("  Writing a FALLBACK. Hull length and draft below are")
        print("  INVENTED, and they set the waterline, the depth floor and")
        print("  the whole search grid -- every result then describes a")
        print("  different boat.")
        print("  " + "!" * 68)
        _generate_fallback_out(filepath_report)
    else:
        print(f"  Hull dimensions: {filepath_report}")

    true_length_ft, true_draft_in = parse_godzilla_dimensions(filepath_report)

    # ── Guard: ensure off1.mlt exists so geometry workers can run ─────────────
    if not os.path.exists(filepath_matrix):
        print(f"  \u26a0  {os.path.basename(filepath_matrix)} not found \u2013 generating default hull.")
        _generate_fallback_off1(filepath_matrix, true_length_ft, true_draft_in)
    
    # ---------------------------------------------------------
    # 1. DEFINE THE DISCRETE SEARCH GRIDS (THE LOOPS)
    # ---------------------------------------------------------
    MIN_MOLD_DEPTH = true_draft_in + freeboard_min_in
    # Remember the floor AS ENTERED, before the 4-person check below can raise
    # it.  Under an absolute maximum a raised floor NARROWS the search (the
    # ceiling does not move with it), and a user watching their range shrink
    # needs the two numbers that did it -- see the banner note further down.
    _floor_entered = MIN_MOLD_DEPTH
    _floor_raised_to = None

    # ── Stem-rocker structural mass ────────────────────────────────────────────
    # The bow/stern rocker extends the hull past the waterline; that concrete is
    # cantilevered dead weight the 1-D beam must carry.  Resolve the single rocker
    # input (None = auto) to a numeric per-end length [in] and publish it on the
    # config singleton so the beam self-weight (calculate_moment_envelope) sees
    # it.  Auto matches the CAD export's derivation (suggest_stem_rocker_m: the
    # degenerate tip span clipped to 3–12 % of L).  Refined once the offsets are
    # parsed just below; this is the safe default (manual value, or 0 on failure).
    _rk_raw = getattr(config, "STEM_OVERHANG_INPUT_IN",
                      getattr(config, "BOW_STERN_ROCKER_IN", None))
    config.STEM_OVERHANG_IN = float(_rk_raw) if _rk_raw is not None else 0.0
    config.STRUCT_ROCKER_IN = config.STEM_OVERHANG_IN     # legacy alias

    # ── 4-Person Buoyancy Constraint ───────────────────────────────────────────
    # The hull must accommodate 4 paddlers + canoe mass while still clearing
    # freeboard_min_in above the resulting waterline.  Parse the underwater
    # offsets to integrate displaced volume at the 2-person design draft, then
    # solve analytically for how high the water rises with 4 paddlers aboard.
    _gmt_in = 0.0                    # transverse metacentric height [in] (task 7)
    _kb_in = _bmt_in = 0.0           # its separate terms, for the real-KG figures
    # Distinct from "not computed yet": set when the stability calculation was
    # ATTEMPTED and FAILED.  0.0 cannot carry that, because every gate below
    # reads `if _gmt_in > 1e-9`, so a zero SKIPS the Min-GMT gate rather than
    # failing it -- an uncomputable stability figure would have quietly
    # disabled the check instead of stopping the run.
    _gmt_error = None
    # Pre-bound: the parse below can raise, and every later reader of these
    # (the overhang map included) must see "absent", not NameError.
    _x_st = _z_uw = _y_uw = None
    try:
        _x_st, _z_uw, _y_uw = parse_godzilla_offsets(
            filepath_matrix, L_ft=true_length_ft,
            draft_in=true_draft_in, mold_depth_in=true_draft_in,
        )
        if _x_st is not None:
            _x_in = _x_st * 12.0  # feet → inches
            # Transverse (roll) stability at the design draft — GM_T [in], same
            # definition as the Hull Configurator so its Min-GMT gates the sweep.
            # KB and BM_T are kept SEPARATE (not just the collapsed GM_T) so the
            # winner's real-KG stability can be built without re-deriving the
            # waterline KG this form-only value assumes.
            from structural import transverse_hydro_terms_in
            try:
                _kb_in, _bmt_in, _gmt_in = transverse_hydro_terms_in(
                    _y_uw, true_length_ft * 12.0, true_draft_in)
            except (ValueError, ArithmeticError) as _gmt_exc:
                # transverse_hydro_terms_in used to return (0,0,0) here on any
                # exception.  It now RAISES, so this handler must exist and
                # must FAIL the design rather than let it through: a hull whose
                # stability cannot be computed has not passed a stability
                # check.  Reported, not swallowed.
                _gmt_error = str(_gmt_exc)
                _kb_in = _bmt_in = _gmt_in = 0.0
                logger.error("transverse stability could not be computed: %s",
                             _gmt_exc)
            # Double-integrate half-breadths to get displaced volume at 2-person draft
            _sec_areas_in2 = np.array([
                2.0 * trapezoid(_y_uw[i, :], _z_uw)
                for i in range(_y_uw.shape[0])
            ])
            _vol_2p_in3 = trapezoid(_sec_areas_in2, _x_in)
            _vol_2p_m3  = _vol_2p_in3 / 61_023.74

            # Resolve an AUTO stem overhang from the hull's own geometry: the
            # run at each end where the max half-breadth is under 1.5*t, so no
            # shell section can exist and the material there is a slender
            # cantilever tip.  Interpolated, hence independent of the offset
            # grid -- see geometry.stem_overhang_ft.
            #
            # The previous rule scanned for the first station with non-zero
            # underwater AREA and clipped the result to 3-12 % of L.  Both parts
            # were wrong for a structural input: the scan measured the offset
            # FILE'S ROW SPACING (12.0 in at 21 stations, 6.0 at 41, 1.5 at
            # 161), and the 3 % floor -- chosen in the CAD helper so the rocker
            # would be VISIBLE on wide-tipped hulls, not for any load reason --
            # then overrode it entirely.  On this hull that produced 7.20 in/end
            # = 0.03*LOA, roughly seven times the real overhang, and the extra
            # un-buoyed tip mass RELIEVED the midship sagging that governs.
            #
            # Resolved at the SMALLEST thickness in the sweep: the threshold
            # scales with t, and the shortest overhang is the least tip mass and
            # so the conservative end of the range.
            if _rk_raw is None:
                _t_min = (min(float(t) for t in thickness_options_in)
                          if thickness_options_in else 0.375)
                _auto_ft = stem_overhang_ft(_x_st, _y_uw, _t_min)
                config.STEM_OVERHANG_IN = _auto_ft * 12.0
                config.STEM_OVERHANG_DERIVED = True
                _src = f"derived from the hull (half-breadth < 1.5t, t={_t_min:.4f} in)"
            else:
                config.STEM_OVERHANG_DERIVED = False
                _src = "user input (an ASSUMPTION, not a measurement)"
            config.STRUCT_ROCKER_IN = config.STEM_OVERHANG_IN   # legacy alias
            # WHAT THE MODEL WILL ACTUALLY DO WITH IT.  The overhang is applied
            # as whole appended stations, and at n <= 1 the linear taper leaves
            # them massless — so on a coarse grid a real, correctly measured
            # overhang has NO structural effect.  Saying "raises hogging"
            # regardless described a cantilever that was not there, and sent a
            # real investigation looking for the hogging it implied.
            _n_st_eff = (getattr(config, "LONGITUDINAL_STATIONS", None)
                         or len(_x_st))
            _dx_ft = true_length_ft / max(int(_n_st_eff) - 1, 1)
            _n_oh, _oh_acts = structural.overhang_stations(
                config.STEM_OVERHANG_IN / 12.0, _dx_ft)
            # An INERT-KNOB warning, at startup, where it is read before the
            # afternoon is spent rather than after.  While
            # PADDLER_LOAD_ENVELOPE is on, the envelope always contains the
            # no-relief model, and that model governs the sagging peak (which
            # sets D/C on typical designs) because spreading load only lowers
            # a peak.  So knee_frac_bending provably cannot move the reported
            # D/C.  Measured identical to 8 decimals at 161/321/641/1281/2561.
            _kfb = float(getattr(config, "KNEE_FRAC_BENDING", 1.0))
            _kfp = float(getattr(config, "KNEE_FRAC_PUNCHING", 1.0))
            if getattr(config, "PADDLER_LOAD_ENVELOPE", False) and                     abs(_kfb - _kfp) > 1e-9:
                print(f"  ⚠  knee_frac_bending = {_kfb:.2f} CANNOT AFFECT ANY "
                      f"REPORTED RESULT while PADDLER_LOAD_ENVELOPE is on.")
                print(f"     The envelope always includes the no-relief model "
                      f"({_kfp:.2f}), and spreading load to the toes only "
                      f"lowers the sagging peak, so the no-relief model "
                      f"governs D/C regardless of this value.")
                print(f"     It still changes HOGGING and SHEAR at most "
                      f"stations; set PADDLER_LOAD_ENVELOPE = False to make "
                      f"it govern the headline number.")

            # NOT "the" overhang any more: it is resolved PER THICKNESS
            # (published below, once the real grid is known) because the
            # 1.5*t threshold makes it a function of t.  This value is the
            # thinnest-wall reference, and it is kept because the appended-
            # station diagnostic printed underneath is computed from it.
            # Announcing it as the run's overhang would restate exactly the
            # sweep-wide claim that made a hull's weight depend on its grid.
            print(f"  Stem overhang at the THINNEST wall: "
                  f"{config.STEM_OVERHANG_IN:.2f} in/end — {_src}."
                  f"  Resolved per thickness for the run itself.")
            if _oh_acts:
                print(f"    Applied as {_n_oh} appended station(s)/end at "
                      f"dx = {_dx_ft*12:.2f} in: un-buoyed cantilever mass "
                      f"beyond each end of the supported span, which raises "
                      f"hogging at the tips and RELIEVES midship sagging.")
                # The MASS is now fixed by geometry (w0*rocker/2 per end) and
                # is identical at every station count; only how coarsely it is
                # spread changes.  Said once, here, rather than from inside the
                # evaluator -- that fired 143,479 times in a 35-config sweep.
                if _n_oh <= 2:
                    print(f"    Its mass is the geometric value at every "
                          f"resolution, but with only {_n_oh} station(s)/end "
                          f"its DISTRIBUTION is coarse here; a finer grid "
                          f"spreads the same mass more smoothly.")
            else:
                print(f"    NO TIP CANTILEVER: the resolved overhang is zero, "
                      f"so no mass is appended beyond either end and no "
                      f"hogging should be expected from it.")

            # Actual crew masses from the UI weight inputs (config.RACE_*), NOT a
            # placeholder paddler mass — so the freeboard floor tracks the real
            # 2-person design load and 4-person heaviest case.
            _LB_TO_KG = 0.453592
            # The 2-person crew is the DESIGN draft Godzilla shaped the hull for,
            # so it stays a 2-person figure (the heavier of 2M/2F).  The mold
            # depth, by contrast, must clear the heaviest crew of ANY race --
            # computed, not assumed to be the four.
            _crew2_lb = float(sum(config.RACE_2_PERSON))
            _crew4_lb = config.heaviest_crew_lbs()
            _crew_max_cat = config.heaviest_crew_category()

            # Godzilla shapes the hull for the 2-person design draft, so the hull
            # floats at true_draft_in with canoe + the 2-person crew → back-out
            # the hull (canoe) mass from that displacement.
            _canoe_mass_kg = max(_vol_2p_m3 * 1000.0 - _crew2_lb * _LB_TO_KG, 5.0)

            # Average full waterplane beam from waterplane area
            _B_full_in  = 2.0 * _y_uw[:, -1]
            _WP_area_m2 = trapezoid(_B_full_in, _x_in) / 1550.003
            _B_wl_avg_m = _WP_area_m2 / max(true_length_ft * 0.3048, 1.0)

            # Draft that supports (hull + the ACTUAL 4-person crew), then the
            # legal minimum mold depth = that draft + the freeboard requirement.
            _wl_4p_in, _depth_4p_in = calculate_4_person_waterline(
                canoe_mass_kg=_canoe_mass_kg,
                base_draft_in=true_draft_in,
                vol_2p_m3=_vol_2p_m3,
                L_ft=true_length_ft,
                B_wl_avg_m=_B_wl_avg_m,
                flare_angle_deg=0.0,
                min_freeboard_m=freeboard_min_in * 0.0254,
                crew_mass_4p_kg=_crew4_lb * _LB_TO_KG,
            )
            if _depth_4p_in > MIN_MOLD_DEPTH:
                # The user's depth floor is illegal for the 4-person load —
                # raise it so no illegal-freeboard hull can be chosen.  NOT
                # silently: it is recorded here and restated in the search-space
                # banner, because with an absolute maximum this shrinks the
                # search rather than sliding it.
                _floor_raised_to = _depth_4p_in
                print(f"  4-Person Waterline calculated at {_wl_4p_in:.2f} in "
                      f"(crew {_crew4_lb:.0f} lb + est. hull). Enforcing absolute "
                      f"minimum mold depth of {_depth_4p_in:.2f} in to satisfy the "
                      f"{freeboard_min_in:.1f}\" freeboard rule "
                      f"(raised from {MIN_MOLD_DEPTH:.2f} in).")
                MIN_MOLD_DEPTH = _depth_4p_in
            else:
                print(f"  4-Person Waterline calculated at {_wl_4p_in:.2f} in "
                      f"(crew {_crew4_lb:.0f} lb + est. hull); the {MIN_MOLD_DEPTH:.2f} in "
                      f"depth floor already clears the {freeboard_min_in:.1f}\" "
                      f"freeboard rule (4P needs ≥ {_depth_4p_in:.2f} in).")
    except Exception as _e4p:
        domain_only(_e4p)   # a bug is not a skippable condition
        print(f"  ⚠  4-person waterline estimate failed ({_e4p}); "
              f"using 2-person draft + freeboard ({MIN_MOLD_DEPTH:.2f} in).")
    # ── End 4-Person Constraint ────────────────────────────────────────────────

    # ── Transverse-stability constraint (task 7) ────────────────────────────────
    # GM_T is a property of the HULL SHAPE at the design draft, so it gates the
    # whole hull rather than a single structural candidate: no thickness/depth/
    # density choice can rescue a hull that is too tender.  Enforced only when the
    # Hull Configurator's Min GMT is set (config.MIN_GMT_IN > 0); default 0 keeps
    # every existing run and test unchanged.
    _min_gmt_in = float(getattr(config, "MIN_GMT_IN", 0.0))
    if _gmt_error is not None:
        # FAIL, never skip.  The old (0,0,0) sentinel landed here as
        # `_gmt_in > 1e-9` being False, which silently bypassed the Min-GMT
        # gate -- the design sailed through with no stability check at all.
        print("  \u274c Transverse stability COULD NOT BE COMPUTED: %s"
              % _gmt_error)
        print("     This design has NOT passed a stability check and is "
              "treated as FAILING it. Sweep aborted rather than reporting a "
              "hull whose GM_T is unknown.")
        return None
    if _min_gmt_in > 1e-9 and _gmt_in > 1e-9:
        if _gmt_in < _min_gmt_in:
            print(f"  ❌ Transverse stability: GM_T = {_gmt_in:.2f} in < required "
                  f"{_min_gmt_in:.2f} in. Hull is too tender — widen the beam (Hull "
                  f"Configurator) or lower Min GMT. Sweep aborted.")
            return None
        print(f"  Transverse stability: GM_T = {_gmt_in:.2f} in "
              f"≥ Min GMT {_min_gmt_in:.2f} in — OK.")
    elif _gmt_in > 1e-9:
        print(f"  Transverse stability: GM_T = {_gmt_in:.2f} in (no Min-GMT gate set).")

    # Grid 1 & 2: Geometry — scaled by precision_level.
    # When thickness_options_in is supplied by the UI (via OptimizationConfig),
    # it is the buildable 1/32 in + 0.5 mm grid and overrides these defaults.
    # The wall is troweled in MAX_MIXES equal layers, so the buildable grid
    # only offers totals that divide into constructible layers.
    from structural import buildable_thickness_grid
    _n_layers = int(getattr(config, "MAX_MIXES", 3))
    if precision_level == 1:   # Coarse
        _default_t = buildable_thickness_grid(0.375, 0.375 + 0.30, _n_layers)[::4] or [0.375]
        _depth_step = 2.0
    elif precision_level == 2:  # Normal
        _default_t = buildable_thickness_grid(0.375, 0.375 + 0.60, _n_layers)[::2]
        _depth_step = 1.0
    else:                       # Fine (3): full buildable grid
        _default_t = buildable_thickness_grid(0.375, 0.375 + 1.50, _n_layers)
        _depth_step = 0.5
    thickness_options = thickness_options_in if thickness_options_in else _default_t

    # ── PER-THICKNESS STEM OVERHANG ──────────────────────────────────────
    # Published HERE, not at the earlier derivation: this is the first point
    # where the real grid is known.  The earlier block runs before
    # `thickness_options` exists and falls back to 0.375 when the caller
    # passed none, so a map built there would have missed every default
    # thickness.  Written once, before any worker starts, then read-only --
    # which is what makes it safe to share across the pool.
    if _x_st is not None and _y_uw is not None:
        config.STEM_OVERHANG_BY_T = publish_stem_overhang_map(
            thickness_options, _x_st, _y_uw, explicit_in=_rk_raw)
        _oh_vals = config.STEM_OVERHANG_BY_T
        if len(set(round(v, 4) for v in _oh_vals.values())) > 1:
            print("  Stem overhang, per thickness (it scales with t, so it "
                  "is resolved per design —")
            print("    a sweep-wide value made a hull's weight depend on "
                  "what else was in the grid):")
            for _t in sorted(_oh_vals):
                print(f"      t = {_t:.4f} in  ->  {_oh_vals[_t]:.3f} in/end")

    # Explicit depth increment / span (advanced panel) override the preset;
    # None falls back to the precision-level default.  depth_span is measured
    # ABOVE MIN_MOLD_DEPTH (the 4-person freeboard floor).
    _depth_step = float(depth_step_in) if depth_step_in else _depth_step
    # Depth bounds: an ABSOLUTE MAXIMUM, with the old SPAN kept as a
    # deprecated alias.
    #
    # The UI labelled this pair min/max while passing floor+span, so "min 6 /
    # max 4.5" looked inverted but meant floor 6 in, span 4.5 in -- a valid
    # search that any naive max<min validation would have rejected.  The label
    # was the defect; max now means max.
    #
    # depth_span_in stays accepted because saved presets and several tests
    # pass it, some positionally.  Exactly one may be given.
    _given = {"freeboard_max_in": freeboard_max_in,
              "depth_max_in": depth_max_in,
              "depth_span_in": depth_span_in}
    _given = {k: v for k, v in _given.items() if v is not None}
    if len(_given) > 1:
        raise ValueError(
            "pass exactly ONE depth bound, got "
            + ", ".join(f"{k}={v}" for k, v in _given.items()) + ". "
            "freeboard_max_in is the current form (the most hull the design "
            "may carry ABOVE the water, the same quantity freeboard_min_in "
            "bounds from below); depth_max_in is an absolute mold depth; "
            "depth_span_in is the deprecated span above the floor.")
    # Both fixed-ceiling forms resolve to a maximum mold depth and then share
    # ONE floor check and ONE span computation.  Giving each its own copy is
    # how the freeboard form first shipped without setting _depth_span at all
    # -- the checks belong to the ceiling, not to the spelling of it.
    _depth_max = None
    if freeboard_max_in is not None:
        # Freeboard is what the user reasons about -- how much canoe is out of
        # the water -- and it is what freeboard_min_in already meant, so the
        # pair is one quantity bounded twice.  Mold depth is the DERIVED
        # number, and it is derived HERE rather than in the UI so there is one
        # conversion rather than two that can disagree.
        #
        # Measure the ceiling from THE SAME DATUM AS THE FLOOR, which means
        # from MIN_MOLD_DEPTH and not from true_draft_in.
        #
        # true_draft_in is the 2-PERSON design draft.  MIN_MOLD_DEPTH is
        # freeboard_min_in above the GOVERNING draft, which the 4-person check
        # above may have moved to the heaviest crew's waterline -- 6.57 in
        # rather than 4.58 in on the current hull.  Converting the maximum
        # through true_draft_in while the minimum came from the 4-person
        # waterline puts the two ends of one row on two different datums, and
        # the user sees "min 6.0 / max 6.5" rejected because the sweep read
        # the maximum as 7.99 in of freeboard measured somewhere else.
        #
        # Anchoring to MIN_MOLD_DEPTH makes the searched band exactly
        # (max - min) inches wide wherever the floor ends up, which is what
        # "between 6.0 and 6.5 inches of freeboard" means.  A raised floor
        # SLIDES the band instead of narrowing it -- correct here precisely
        # because both ends move together, unlike a fixed mold-depth ceiling.
        _depth_max = MIN_MOLD_DEPTH + (float(freeboard_max_in)
                                       - float(freeboard_min_in))
    elif depth_max_in is not None:
        _depth_max = float(depth_max_in)

    if _depth_max is not None:
        # MIN_MOLD_DEPTH is computed from the freeboard rule and enforced
        # regardless of input, so a maximum below it is not a tight search --
        # it is an empty one, and it must say so with both numbers.
        if _depth_max <= MIN_MOLD_DEPTH:
            # Name the source of the floor HONESTLY.  "draft + freeboard" is
            # only where it came from when the 4-person check left it alone;
            # blaming the freeboard rule for a 4P-raised floor sends the user
            # to lower a number that is not the binding one.
            _why_floor = (
                f"(draft {true_draft_in:.2f} in + "
                f"freeboard {freeboard_min_in:.2f} in)"
                if _floor_raised_to is None else
                f"(raised to the 4-person waterline requirement from "
                f"{_floor_entered:.2f} in = draft {true_draft_in:.2f} in + "
                f"freeboard {freeboard_min_in:.2f} in)")
            # Answer in the units the caller ASKED in.  Telling someone who
            # typed a freeboard ceiling to "raise the maximum depth above
            # 12.57 in" makes them convert by hand, against a draft they were
            # never shown.
            #
            # For the freeboard form this is now reachable ONLY when the two
            # freeboards are themselves inverted -- the band is anchored to
            # the floor, so where the floor sits cannot make it empty.  The
            # message says so with the two numbers the user typed and nothing
            # else; naming a draft here is what made the last one unreadable.
            if freeboard_max_in is not None:
                raise ValueError(
                    f"maximum freeboard {float(freeboard_max_in):.2f} in is at "
                    f"or below the minimum freeboard "
                    f"{float(freeboard_min_in):.2f} in, so no freeboard "
                    f"satisfies both and nothing would be searched. Raise the "
                    f"maximum above {float(freeboard_min_in):.2f} in, or lower "
                    f"the minimum.")
            raise ValueError(
                f"maximum depth {_depth_max:.2f} in is below the "
                f"freeboard-required minimum mold depth of "
                f"{MIN_MOLD_DEPTH:.2f} in {_why_floor}. No depth can satisfy "
                f"both, so nothing would be searched. Raise the maximum above "
                f"{MIN_MOLD_DEPTH:.2f} in, or lower the freeboard requirement.")
        _depth_span = _depth_max - MIN_MOLD_DEPTH
    else:
        _depth_span = float(depth_span_in) if depth_span_in else 4.5
    depth_options = np.arange(MIN_MOLD_DEPTH, MIN_MOLD_DEPTH + _depth_span, _depth_step)

    # A raised floor NARROWS an absolute-maximum search.  Say so with the
    # numbers.
    #
    # Under the old span semantics the whole window rose with the floor and the
    # search stayed the same height, so the raise was worth a line and nothing
    # more.  A maximum does not move: the floor rises INTO it and the range
    # shrinks -- correct, because a ceiling the program moves is not a ceiling,
    # but invisible if all we print is the new floor.  On a hull where the 4P
    # check binds the user would watch the depth count drop with no stated
    # cause, and "fewer depths than I asked for" reads as a bug.
    #
    # Only the absolute-max path can narrow; a span re-measures from wherever
    # the floor ends up, so there is nothing to report there.
    # Only a FIXED mold-depth ceiling narrows.  A freeboard band is anchored
    # to the floor, so a raised floor slides it and its width is untouched --
    # reporting a narrowing there would be false.
    _depth_narrowed = describe_depth_narrowing(
        _floor_entered, _floor_raised_to,
        _depth_max if depth_max_in is not None else None,
        _depth_step, len(depth_options))
    if _depth_narrowed:
        print(f"  ⚠  {_depth_narrowed}.")

    # An EMPTY depth range is not an infeasible design.  np.arange returns []
    # for a non-positive span, and the first thing to touch it is a min() that
    # fails with "min() iterable argument is empty" -- an error that names
    # neither the range nor the cause.  Left further downstream it is worse:
    # zero tasks means no worker raises, so the all-tasks-failed guard cannot
    # see it and the sweep reports "no feasible design" for a search that never
    # happened.  Fail HERE, where the range is built and can be described.
    if len(depth_options) == 0:
        raise ValueError(
            "the depth search range is EMPTY -- zero mold depths to try, so "
            "nothing was searched. This is NOT an infeasible design. "
            f"floor={MIN_MOLD_DEPTH:.4f} in (the freeboard-required minimum "
            f"mold depth on this hull), span={_depth_span:.4f} in, "
            f"step={_depth_step:.4f} in. "
            "A non-positive span, or a maximum depth below that floor, "
            "produces an empty range. Widen the bounds rather than looking "
            "for a physics problem.")
    
    # ---------------------------------------------------------
    # Grid 3, 4, 5: Continuous Predictive Mix Chemistry
    # The sweep explores a continuous density spectrum.  The regression
    # engine in mix_database.py predicts strengths for each theoretical
    # density.  max_density is capped by the UI; the lower bound is
    # derived from where the regression predicts negligible strength.
    # ---------------------------------------------------------
    # Full-resolution density grid.  The multi-zone path stays tractable NOT
    # by coarsening densities but by testing only the strict I-beam stack
    # (heavy extremes / light core) for each density pair — one evaluation
    # per pair, no combinatorial itertools.product over stack permutations.
    # EXISTING_MIXES_ONLY is a manufacturing constraint like N_MIXES_MAX: it
    # restricts the search to densities the Mix Lab actually holds, so the
    # winner is a mix the team can batch rather than an interpolated one.
    _existing_only = bool(getattr(config, "EXISTING_MIXES_ONLY", False))

    # An INVERTED density range is not an infeasible design -- it is an empty
    # search, and it must say so with both numbers.  Left alone it reaches the
    # soft "mix grid is empty" return below, which hands back None, and None
    # reads downstream as "no feasible design" for a search that never
    # happened.  Same rule as the depth range: fail where the range is built.
    _dens_floor = float(getattr(config, "MIN_DENSITY_PCF", 0.0) or 0.0)
    _dens_ceil = float(config.MAX_DENSITY_PCF)
    if _dens_floor > _dens_ceil:
        raise ValueError(
            f"the mix density range is EMPTY -- minimum {_dens_floor:.1f} pcf "
            f"is above maximum {_dens_ceil:.1f} pcf, so no mix can satisfy "
            f"both and nothing would be searched. This is NOT an infeasible "
            f"design. Lower the minimum or raise the maximum.")

    if _companion is not None:
        # Companion run for the mix-development spec: search an explicit
        # density list (a hypothetical Mix Lab) instead of the normal grid.
        from mix_database import MixDatabase
        _pool = ([float(d) for d in _companion]
                 if not isinstance(_companion, bool)
                 else config.get_mix_db().get_existing_mix_densities())
        _pool = [d for d in sorted(set(_pool))
                 if _dens_floor - 1e-9 <= d <= _dens_ceil + 1e-9]
        valid_mixes = MixDatabase._combos_from_densities(_pool, config.MAX_MIXES)
        _existing_only = True
    else:
        valid_mixes = config.get_mix_db().get_structural_sweep_grid(
            fos=config.FACTOR_OF_SAFETY,
            max_density=config.MAX_DENSITY_PCF,
            n_mixes=config.MAX_MIXES,
            step=density_step_pcf,      # advanced override; None → engine default
            existing_only=_existing_only,
            # The team's stated floor is a real bound now, not documentation.
            min_density=_dens_floor or None,
        )
    if not valid_mixes:
        print("  (companion run skipped: no mix combinations in that pool)"
              if _companion is not None else
              "  ⚠  the mix grid is empty — check the density range.")
        # A sweep that found nothing is the entry most worth finding later:
        # it records the search that was attempted and why it was empty.
        if audit_log and _companion is None:
            _audit_run(None, freeboard_min_in=freeboard_min_in,
                       precision_level=precision_level,
                       density_step_pcf=density_step_pcf,
                       num_zones=num_zones, multizone=multizone,
                       existing_only=_existing_only,
                       outcome="no mix combinations in the density range")
        return None

    # Number of horizontal pour ZONES — decoupled from the number of mixes.
    # Default to the mix count for backward compatibility.
    _num_zones = int(num_zones) if num_zones else int(config.MAX_MIXES)

    # Build gunwale sweep grids.  When disabled, a single zero-valued point is
    # used so the product loop still runs without branching.  BOTH the
    # single-zone (evaluate_canoe_weight) and multi-zone (evaluate_multizone_
    # weight) evaluators now model the rails through the shared
    # gunwale_rail_section, so the grid is swept on either path when enabled.
    if enable_gunwales:
        gw_width_options  = np.linspace(gw_min_width_in,  gw_max_width_in,  gw_steps)
        gw_height_options = np.linspace(gw_min_height_in, gw_max_height_in, gw_steps)
    else:
        gw_width_options  = [0.0]
        gw_height_options = [0.0]

    # Un-pruned configuration total — the denominator for the bound-pruned %.
    # It MUST equal the brute-force evaluation count, i.e. Σ over every task of
    # (mix combos × pour-line sets).  The multi-zone pour-line count grows with
    # depth (multizone_pour_grids(d_in) admits more interior lines in a taller
    # section), so a single _pour_sets sampled at MIN_MOLD_DEPTH (the shallowest
    # depth, fewest lines) badly undercounts.  Sum the ACTUAL per-depth counts.
    _n_geom = (len(thickness_options) * len(gw_width_options)
               * len(gw_height_options))          # tasks share the depth axis
    if multizone and _num_zones >= 2:
        from structural import multizone_pour_grids
        _pour_sets_by_depth = [len(multizone_pour_grids(float(d), _num_zones))
                               for d in depth_options]
        total_configs = _n_geom * len(valid_mixes) * int(sum(_pour_sets_by_depth))
        _pour_note = (f" ({_num_zones} zones from {config.MAX_MIXES} mixes, "
                      f"strict I-beam, {min(_pour_sets_by_depth)}–"
                      f"{max(_pour_sets_by_depth)} pour-line sets across depths)")
    else:
        total_configs = _n_geom * len(depth_options) * len(valid_mixes)
        _pour_note = ""
    print(f"\nCommencing {'Multi-Zone I-Beam' if multizone else '2D'} Pour "
          f"Sweep across ~{total_configs} unique configurations{_pour_note}...")

    # ── Per-gate diagnostics ───────────────────────────────────────────────────
    # Describe the space BEFORE sweeping it, so a sweep that finds nothing can
    # print the ranges it actually searched — a range too narrow to contain any
    # feasible design looks identical to a physics failure until you can see it.
    global _SWEEP_TALLY
    from structural import SweepTally
    _dens_all = sorted({float(d) for combo in valid_mixes for d in combo})
    _dens_step = (density_step_pcf if density_step_pcf
                  else (2.0 if config.MAX_MIXES == 3 else 0.5))
    _t_opts = [float(t) for t in thickness_options]
    _search_space = {
        "mix density": (
            f"{_dens_all[0]:.1f} – {_dens_all[-1]:.1f} pcf"
            + ("" if _existing_only else f", step {_dens_step}")
            + f" ({len(_dens_all)} densities → {len(valid_mixes):,} combos of "
            f"{config.MAX_MIXES} mix{'es' if config.MAX_MIXES > 1 else ''})"),
        "  ceiling from": f"the sweep's Max Mix input = {config.MAX_DENSITY_PCF:.1f} pcf",
        "  floor from": (f"the sweep's Min Mix input = {_dens_floor:.1f} pcf"
                         if _dens_floor else
                         "the Mix Lab regression alone (no team floor set)"),
        "  floor from": (
            "EXISTING MIXES ONLY — restricted to the "
            f"{len(_dens_all)} densities in the Mix Lab "
            f"({', '.join(f'{d:g}' for d in _dens_all)} pcf); the continuous "
            "spectrum is NOT searched" if _existing_only else
            "the Mix Lab regression (lowest density predicted to reach 10 psi f'c)"),
        # The grid is CONSTRUCTED by the caller (the UI), so only the caller
        # can say whether these are buildable thicknesses -- calling them
        # buildable unconditionally is how an overridden grid stays invisible.
        "wall thickness": (f"{min(_t_opts):.4f} – {max(_t_opts):.4f} in "
                           + (f"({len(_t_opts)} custom steps — BUILDABLE GRID "
                              f"OVERRIDDEN, the winner may not be trowelable)"
                              if thickness_grid_overridden else
                              f"({len(_t_opts)} buildable options)")),
        "mold depth": (f"{float(min(depth_options)):.2f} – "
                       f"{float(max(depth_options)):.2f} in, step {_depth_step} "
                       f"({len(depth_options)} depths)"
                       + (f" — {_depth_narrowed}" if _depth_narrowed else "")),
        "gunwales": ("disabled" if not enable_gunwales else
                     f"width {gw_min_width_in:.2f}–{gw_max_width_in:.2f} in, "
                     f"height {gw_min_height_in:.2f}–{gw_max_height_in:.2f} in, "
                     f"{gw_steps} steps"),
        "flare angle": f"0 – {flare_max_deg:.0f}°  (searched at 1° resolution)",
        "pour zones": (f"{_num_zones} (multi-zone I-beam), "
                       f"max {int(getattr(config, 'N_MIXES_MAX', 2))} distinct "
                       f"densities on the canoe" if multizone and _num_zones >= 2
                       else "1 (uniform / depth-zoned single-mix path)"),
        "factor of safety": (f"{config.FACTOR_OF_SAFETY} "
                             f"(dynamic load factor {config.DYNAMIC_LOAD_FACTOR})"),
        "freeboard rule": f"≥ {freeboard_min_in:.2f} in",
        "stations": f"{getattr(config, 'LONGITUDINAL_STATIONS', None)} longitudinal",
    }
    _SWEEP_TALLY = SweepTally(_search_space)
    if _gmt_in > 1e-9:
        _SWEEP_TALLY.note(
            f"pre-sweep hull gate — GM_T = {_gmt_in:.2f} in"
            + (f" ≥ Min GMT {_min_gmt_in:.2f} in (passed)" if _min_gmt_in > 1e-9
               else " (no Min-GMT gate set)"))

    global_best_weight = float('inf')
    global_best_config = None
    # Every surviving configuration, for the top-k gate.  The sweep otherwise
    # keeps only the single lightest, which cannot support a gate that has to
    # refine several candidates and may reject the lightest.  Bounded so a
    # long sweep cannot grow this without limit: only the lightest
    # _GATE_POOL_CAP are worth keeping, since the gate walks lightest-first.
    _GATE_POOL_CAP = max(config_int("GATE_TOP_K", 12) * 6, 64)
    gate_pool = []
    # DISTINGUISH "no survivors" FROM "the pool was never filled".  An empty
    # list means both, and `if gate_pool:` cannot tell them apart -- the exact
    # collision that let GM_T = 0.0 mean both "no gate configured" and
    # "could not be computed", and that skipped the stability check instead of
    # failing it.  Reintroducing it HERE would be worse: this is the gate whose
    # whole purpose is refusing to report an unverified winner, and an empty
    # pool read as "nothing survived" is indistinguishable from the sweep
    # never having offered it anything.
    gate_pool_fed = False

    # ---------------------------------------------------------
    # 2. EXECUTE THE MULTIPROCESSING SWEEP
    # ---------------------------------------------------------
    configs_tested = 0

    # Bundle arguments to pass to the worker pool
    tasks = []
    # ── Executor: threads or processes ───────────────────────────────────────
    #
    # The comment that stood here said ProcessPoolExecutor "cannot reliably
    # spawn child processes from a non-main thread on Windows".  That was
    # MEASURED FALSE on this interpreter (3.12.6): spawning from a daemon
    # thread works.  What the note was really describing is the
    # freeze_support() requirement, which app.py now satisfies at entry.
    #
    # Threads stay the DEFAULT, for two reasons that have not been retired:
    # the spawn testing was done on the UNFROZEN interpreter only (a
    # PyInstaller build is untested), and every spawned child re-imports
    # `config` fresh -- so the run's settings reach it only through the
    # RunState in the args tuple, whose fingerprint assertion is the safety
    # net.  Choosing processes is therefore explicit, never implicit.
    _kind = resolve_sweep_executor()


    # ── What has to cross the boundary ───────────────────────────────────────
    # Under threads these are the very objects the parent already uses, so the
    # thread path is unchanged in every respect.  Under processes each becomes
    # a manager-hosted proxy.
    _RUN_ID = _new_run_id()
    _run_state = _run_state_mod.RunState.capture_for_run()
    _worker_stop = stop_event
    _worker_tally = _SWEEP_TALLY
    _mgr = _tally_mgr = None
    _bridge_done = threading.Event()
    # Tasks that RAISED.  Tracked because the results loop deliberately logs and
    # continues -- without this, a sweep in which every task died would report
    # "no feasible configurations", sending the user off to widen a grid that
    # was never actually searched.
    _worker_errors = []
    if _kind == "process":
        if stop_event is not None:
            _mgr = multiprocessing.Manager()
            _worker_stop = _RateLimitedEvent(_mgr.Event())

            # The caller (services/worker.py) owns a threading.Event that a
            # child cannot see and that cannot be pickled into one.  Rather
            # than change every service signature, mirror it one-way onto the
            # proxy -- only the parent ever sets stop; workers only read.
            #
            # _bridge_done ends the thread when the sweep finishes normally.
            # Without it the mirror would keep polling the manager for the life
            # of the GUI process and would start raising the moment the manager
            # is shut down below.
            def _mirror_stop():
                while not stop_event.wait(0.1):
                    if _bridge_done.is_set():
                        return
                _worker_stop.set()

            threading.Thread(target=_mirror_stop, daemon=True,
                             name="sweep-stop-bridge").start()
        if _SWEEP_TALLY is not None:
            _tally_mgr = _TallyManager()
            _tally_mgr.start()
            _worker_tally = _tally_mgr.SweepTally(_search_space)
            # The parent has already attached pre-sweep context (the mix-viability
            # and pre-gate results printed above the histogram).  The hosted
            # tally is a FRESH object, so replay them -- otherwise repatriating
            # it below would silently drop them from the failure report.
            for _n in _SWEEP_TALLY.notes:
                _worker_tally.note(_n)

    for t_in, d_in, gw_w_mid, gw_h_mid in itertools.product(
            thickness_options, depth_options, gw_width_options, gw_height_options):
        tasks.append((t_in, d_in, valid_mixes, true_length_ft, true_draft_in,
                      filepath_matrix, gw_w_mid, gw_h_mid,
                      enable_mesh, gw_fillet_ratio, freeboard_min_in, flare_max_deg,
                      _worker_stop, multizone, _num_zones, _run_state,
                      _worker_tally))

    # -- An EMPTY grid is not an infeasible design ---------------------------
    # np.arange(floor, floor + span, step) returns [] whenever the span is
    # non-positive, so an inverted range -- or a maximum below the hard
    # freeboard-required floor -- yields ZERO tasks.  Zero tasks means no
    # worker ever raises, so the all-tasks-failed guard cannot see it, and the
    # sweep reports "no feasible design": a search that never happened,
    # presented as a design verdict.  That reading costs a day chasing physics
    # that is fine.
    #
    # The UI validates its own fields, but headless callers of
    # run_grand_optimization reach here directly, so the guard belongs at the
    # GRID, not only at the widget.
    if not tasks:
        _ranges = {
            "thickness_in": [float(t) for t in thickness_options],
            "depth_in": [round(float(d), 4) for d in depth_options],
            "gw_width_in": [float(g) for g in gw_width_options],
            "gw_height_in": [float(g) for g in gw_height_options],
        }
        _empty = [k for k, v in _ranges.items() if len(v) == 0]
        raise ValueError(
            "the sweep grid is EMPTY -- zero configurations to evaluate, so "
            "nothing was searched. This is NOT an infeasible design. "
            f"Empty range(s): {_empty}. Ranges: {_ranges}. "
            "A max below its min, or a maximum depth below the "
            f"freeboard-required minimum mold depth ({MIN_MOLD_DEPTH:.2f} in "
            "on this hull), produces an empty range. Widen the bounds rather "
            "than looking for a physics problem.")

    # Physical cores for processes (dense float math: the two hyperthreads of a
    # core share one FPU).  Threads keep the historical cap of 8.
    _phys = _physical_core_count()
    _cap = getattr(config, "SWEEP_MAX_WORKERS", None)
    _cap = int(_cap) if _cap else (_phys if _kind == "process" else 8)
    n_workers = max(1, min(_cap, len(tasks)))

    completed_tasks = 0
    total_tasks = len(tasks)
    _clock.mark("search")
    _t0 = time.monotonic()

    def _fmt_eta(sec: float) -> str:
        sec = int(max(sec, 0))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

    stopped = False
    def _PoolFor(kind):
        return (concurrent.futures.ProcessPoolExecutor if kind == "process"
                else concurrent.futures.ThreadPoolExecutor)

    # How long "nothing is moving" has to last before the pool is declared
    # stalled.  Generous by design: a single task at 161 stations with the full
    # 8-start seating solve runs ~13 min on this hardware, and a false positive
    # would throw away a healthy sweep to restart it slower.  This bounds the
    # WORST case (an unkillable window) rather than trying to be tight.
    _stall_s = float(getattr(config, "SWEEP_STALL_TIMEOUT_S", 1800.0) or 1800.0)
    # Three DIFFERENT identifiers, previously conflated into one:
    #
    #   run  -- unique per INVOCATION.  What "which run produced this output?"
    #           actually needs.  The fingerprint was mislabelled `run` and could
    #           not do this: it is a hash of the run-level CONFIG, so two
    #           sweeps over different grids printed the same value (grid extent
    #           is not run-level config).  Identical by design, useless as an id.
    #   cfg  -- the RunState fingerprint.  Identifies the CONFIGURATION, which
    #           is what the worker-side assertion compares.  Equal values across
    #           runs is correct and meaningful.
    #   code -- the commit actually executing.
    #
    # Plus a loud marker when the config file did NOT load: a silent fallback
    # to built-in defaults makes every cfg edit a no-op while the file on disk
    # still reads what was typed.
    _src = _settings_source()
    _cfg_warn = "" if _src.get("loaded") else "  [!! CONFIG DEFAULTED]"
    print(f"  ⚙ {_kind} executor · {n_workers} workers "
          f"({_phys} physical / {os.cpu_count()} logical) · "
          f"{total_tasks} tasks · run {_RUN_ID} "
          f"· cfg {_run_state.fingerprint[:8]} "
          f"· code {_code_version()}{_cfg_warn}")
    if not _src.get("loaded"):
        print(f"     !! {_src.get('reason')}")
        print(f"     !! every value below is a BUILT-IN DEFAULT, not what "
              f"{_src.get('path')} says.")
    # ── THE POOL PHASE, as a closure ─────────────────────────────────────
    # Nested on purpose.  If the executor stalls, this whole phase re-runs
    # on threads, and every accumulator has to start clean.  Resetting them
    # by hand at a retry point is exactly how a fallback run ends up
    # reporting a winner assembled from two half-searches; fresh locals make
    # that impossible rather than merely unlikely.
    def _run_pool_phase(kind):
        completed_tasks = 0
        configs_tested = 0
        global_best_weight = float('inf')
        global_best_config = None
        gate_pool = []
        gate_pool_fed = False
        _worker_errors = []
        stopped = False
        _t0 = time.monotonic()

        with _PoolFor(kind)(max_workers=n_workers) as executor:
            futures = [executor.submit(_evaluate_geometry_worker, t) for t in tasks]

            _pending = set(futures)
            while _pending:
                # STALL, not TOTAL, timeout.  as_completed(timeout=) bounds the
                # whole iteration, so on a two-hour sweep it would fire on a
                # perfectly healthy run.  wait(FIRST_COMPLETED) bounds the GAP
                # between completions, which is what 'nothing is moving' means.
                _done_set, _pending = concurrent.futures.wait(
                    _pending, timeout=_stall_s,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                if not _done_set:
                    for _f in _pending:
                        _f.cancel()
                    raise _ExecutorStalled(
                        f"no sweep task completed in {_stall_s:.0f} s under "
                        f"the {kind!r} executor "
                        f"({completed_tasks}/{total_tasks} done)")
                for future in _done_set:
                    completed_tasks += 1

                    # ── Stop button: honour a user abort request promptly ────────────
                    # The remaining futures are cancelled (those not yet started never
                    # run); in-flight ones finish but their results are ignored.
                    if stop_event is not None and stop_event.is_set():
                        if not stopped:
                            print("\n  ■ Stop requested — cancelling remaining "
                                  f"configurations ({completed_tasks}/{total_tasks} done).")
                            stopped = True
                        for f in futures:
                            f.cancel()
                        continue

                    # Process the result FIRST so configs_tested reflects the real work
                    # (pruned combos never counted) before progress reports it.
                    try:
                        result = future.result()
                    except concurrent.futures.CancelledError:
                        result = None
                    except Exception as e:
                        domain_only(e)   # a bug is not a skippable condition
                        logger.error("[Sweep worker error] %s\n%s", e, traceback.format_exc())
                        _worker_errors.append(f"{type(e).__name__}: {e}")
                        result = None

                    if result is not None:
                        weight, best_config, n_combos, _lean_cands = result
                        configs_tested += n_combos     # combos ACTUALLY costed (post-prune)

                        # Structural + constraint filters: only configurations that survived
                        # tensile stress and paddler spacing checks inside the worker reach here.
                        # Guard against penalty weights (1e6) or empty results slipping through.
                        if best_config is not None and not structural.is_rejected(weight):
                            # Record EVERY survivor, not just improvements: the gate
                            # may reject the lightest, and the runner-up must still be
                            # available to it.
                            # THE UNION, not the per-task winners.  gate_pool used
                            # to get one dict(best_config) per task -- the task's best
                            # by SCREEN D/C, the number the gate exists because we
                            # cannot trust.  It now takes every task's top-m lean
                            # records, so the gate ranks the k lightest DESIGNS rather
                            # than the k lightest task-bests.
                            if _lean_cands:
                                gate_pool.extend(_lean_cands)
                                gate_pool_fed = True
                            if len(gate_pool) > _GATE_POOL_CAP * 2:
                                gate_pool.sort(key=lambda c: c.get('weight_lbs') or 1e18)
                                del gate_pool[_GATE_POOL_CAP:]
                            if len(gate_pool) > _GATE_POOL_CAP * 2:
                                gate_pool.sort(key=lambda c: c['weight_lbs'])
                                del gate_pool[_GATE_POOL_CAP:]
                            if weight < global_best_weight:
                                global_best_weight = weight
                                global_best_config = best_config
                                mixes_str = ", ".join(f"{m:.1f}" for m in best_config['mixes'])
                                print(f"  \U0001f4a1 New Best: {weight:.1f} lbs  |  "
                                      f"t={best_config['thickness']}in, d={best_config['depth']:.1f}in, "
                                      f"mixes=[{mixes_str}]")

                    if progress_callback:
                        # The bar fills by TASK completion (frac); the config counter
                        # reports configs ACTUALLY evaluated so far vs the un-pruned
                        # total, so numerator, denominator and the final bound-pruned %
                        # are all on the same footing (it ends below total by exactly
                        # the pruned count).  ETA from average time per task so far.
                        # The search owns the first SWEEP_PHASES[0][0] of the bar,
                        # not all of it.  Filling to 1.0 here is what made the bar
                        # say "Done" with an hour of verification and pricing still
                        # to run -- and scaling it is also what keeps the sequence
                        # MONOTONIC, since the next callback is the search-complete
                        # announcement at exactly that fraction.  A bar that jumps
                        # backwards is its own kind of lie.
                        _done     = completed_tasks / total_tasks
                        frac      = SWEEP_PHASES[0][0] * _done
                        elapsed   = time.monotonic() - _t0
                        # ETA is for the SEARCH, and is computed from the search's
                        # own fraction -- never from the scaled bar position, which
                        # would overstate the remaining time by 1/0.8.
                        eta       = elapsed / _done - elapsed if _done > 1e-9 else 0.0
                        progress_callback(
                            frac,
                            f"Configuration {configs_tested:,}/{total_configs:,}  "
                            f"•  ETA {_fmt_eta(eta)}")

        return dict(completed_tasks=completed_tasks,
                    configs_tested=configs_tested,
                    global_best_weight=global_best_weight,
                    global_best_config=global_best_config,
                    gate_pool=gate_pool, gate_pool_fed=gate_pool_fed,
                    worker_errors=_worker_errors, stopped=stopped)

    # WATCHDOG.  A pool that hangs is the failure mode this project fears
    # most: no exception, no progress, the window simply stops responding
    # and the only way out is Task Manager.  A try/except cannot catch that
    # because nothing is raised -- but a stall timeout can, and it turns
    # 'frozen forever' into 'slower, and it says why'.
    try:
        _bundle = _run_pool_phase(_kind)
    except _ExecutorStalled as _stall:
        if _kind == 'thread':
            raise                      # nothing safer to fall back to
        print('')
        print('  ' + '!' * 68)
        print(f'  EXECUTOR STALLED: {_stall}')
        print('  Falling back to the THREAD executor and starting the')
        print('  search again from the beginning. Slower, not wrong --')
        print('  nothing from the stalled attempt is reused.')
        print('  PLEASE REPORT THIS: it is the deadlock the frozen build')
        print('  held the process pool back for.')
        print('  ' + '!' * 68)
        _kind = 'thread'
        # RE-STATE IT.  The executor banner printed 'process' minutes
        # ago and is now wrong; a run's own record must not say one
        # thing while the pool did another.  test_source_guards checks
        # exactly that property.
        print(f"  ⚙ {_kind} executor · {n_workers} workers · restarted after a stall")
        _bundle = _run_pool_phase('thread')

    completed_tasks    = _bundle['completed_tasks']
    configs_tested     = _bundle['configs_tested']
    global_best_weight = _bundle['global_best_weight']
    global_best_config = _bundle['global_best_config']
    gate_pool          = _bundle['gate_pool']
    gate_pool_fed      = _bundle['gate_pool_fed']
    _worker_errors     = _bundle['worker_errors']
    stopped            = _bundle['stopped']

    # ── Bring the manager-hosted tally home ──────────────────────────────────
    # Everything downstream (the gate table, the failure report, n_feasible)
    # reads a REAL SweepTally, so replace the module global with a detached
    # snapshot of what the workers actually counted into.  Without this the
    # process executor would report only the parent's own pruning and print an
    # empty gate histogram -- the exact silent-wrong-number failure this
    # refactor exists to prevent.
    _bridge_done.set()

    # ── Did the workers actually run? ────────────────────────────────────────
    # A task that raises is logged and skipped, which is right for one bad
    # geometry and badly wrong when it is systemic (an unpicklable object under
    # the process executor put EVERY task here once).  Left unsaid, the sweep
    # then reports an empty search space and the user goes hunting for a
    # physics problem that does not exist.
    if _worker_errors:
        _kinds = collections.Counter(_worker_errors)
        _survived = total_tasks - len(_worker_errors)
        print("")
        print(f"  !! {len(_worker_errors)} of {total_tasks} tasks RAISED "
              f"({_kind} executor) -- what follows came from the {_survived} "
              f"that survived, NOT from a full search of the grid:")
        for _msg, _n in _kinds.most_common(5):
            print(f"       {_n}x  {_msg[:110]}")
        if _survived == 0:
            raise RuntimeError(
                f"every one of the {total_tasks} sweep tasks failed under the "
                f"{_kind!r} executor: {_kinds.most_common(1)[0][0]}. Reporting "
                f"this as 'no feasible design' would be a lie about the grid.")
        # PARTIAL GRID.  Some tasks died, some survived -- and the winner below
        # would be presented as THE optimum over a grid that was never fully
        # searched.  It is not a wrong calculation; it is a correct calculation
        # of the wrong thing, which reads exactly the same on the page.
        #
        # Printing a warning was not enough: the banner scrolls past and the
        # number is what gets written down.  Refuse, and name the way out.
        if not bool(getattr(config, "ACCEPT_PARTIAL_GRID", False)):
            raise RuntimeError(
                f"{len(_worker_errors)} of {total_tasks} sweep tasks failed "
                f"under the {_kind!r} executor, so the grid was NOT fully "
                f"searched. Refusing to report a winner that would look like "
                f"the optimum over the whole grid."
                + chr(10) +
                f"  Most common failure: {_kinds.most_common(1)[0][0]}"
                + chr(10) +
                "  Fix the failure and re-run, or set "
                "ACCEPT_PARTIAL_GRID=True to take the partial result "
                "deliberately.")
    if _tally_mgr is not None:
        _SWEEP_TALLY = _worker_tally.snapshot()
        _tally_mgr.shutdown()
    if _mgr is not None:
        _mgr.shutdown()

    if stopped:
        print(f"\n--- Sweep STOPPED by user: {configs_tested} configurations "
              "evaluated before abort ---")
        if progress_callback:
            progress_callback(1.0, "Stopped")
        _SWEEP_TALLY = None
        if audit_log and _companion is None:
            _audit_run(None, freeboard_min_in=freeboard_min_in,
                       precision_level=precision_level,
                       density_step_pcf=density_step_pcf,
                       num_zones=num_zones, multizone=multizone,
                       existing_only=_existing_only,
                       outcome=f"aborted by user after {configs_tested} "
                               f"configurations")
        return None

    _pruned = max(total_configs - configs_tested, 0)
    print(f"\n--- Sweep Complete: {configs_tested:,} configurations evaluated"
          + (f", {_pruned:,} bound-pruned "
             f"({100.0 * _pruned / total_configs:.0f}% of the grid skipped, "
             f"same optimum)" if _pruned else "")
          + " ---")
    # NOT 1.0, and NOT "Done".  The search is one phase of five; the four
    # after it used to run with the bar pinned at 100% saying "Done", which
    # is the single most force-quit-provoking thing this program did.
    announce_phase(progress_callback, SWEEP_PHASES[0][0],
                   f"Search complete — {configs_tested:,} configurations",
                   clock=_clock, phase="post-search bookkeeping",
                   detail="verification and pricing still to run; the run is NOT"
                   " finished yet")

    # ---------------------------------------------------------
    # 3. OUTPUT & VISUALIZATION
    # ---------------------------------------------------------
    # Hard gate: abort before printing success or generating any reports if
    # no configuration survived both the tensile-stress and freeboard-flare checks.
    if global_best_config is None or global_best_weight == float('inf'):
        # The old message here was a guess ("the flare angle exceeded limits, or
        # the concrete was too weak") that fit every failure equally badly.  The
        # tally has carried the real answer all along: which gate rejected how
        # many configurations, what the closest one achieved on each gate, and
        # the ranges that were searched to get there.
        print(_SWEEP_TALLY.failure_report(total_configs=total_configs))
        _SWEEP_TALLY = None
        # WHERE THE TIME WENT.  Guarded broadly and deliberately: this
        # prints at the very end of a run that may have taken hours, so an
        # exception here would discard work already completed -- the same
        # rule as the other broad catches in this file.  A timing table is
        # never worth a sweep.
        try:
            _clock.report(extra=[f"{configs_tested:,} configurations evaluated, "
                              f"NO feasible design"])
        except Exception as _clk_exc:            # noqa: BLE001
            print(f"  (phase timing unavailable: "
                  f"{type(_clk_exc).__name__}: {_clk_exc})")
        return None
    _tally, _SWEEP_TALLY = _SWEEP_TALLY, None
    # Same numbers on a SUCCESSFUL sweep, on request (config.SWEEP_DIAGNOSTICS)
    # — useful for seeing how much of the space a change just cost you.
    if getattr(config, "SWEEP_DIAGNOSTICS", False):
        print(_tally.failure_report(total_configs=total_configs)
              .replace("❌ SWEEP FAILED: 0 of",
                       f"SWEEP DIAGNOSTICS: {_tally.n_feasible:,} feasible of"))

    best_res = global_best_config['result']
    best_mixes = global_best_config['mixes']
    full_winning_vars = np.concatenate((best_res.x, best_mixes))

    print("\n" + "="*65)
    print("\U0001f3c6 GLOBAL OPTIMIZATION COMPLETE - 2D ZONES APPLIED")
    print("="*65)

    from structural import describe_layer_breakdown
    _win_t = float(global_best_config['thickness'])
    # Echo the CONSTRUCTION layer schedule (troweled passes), not the mix-count
    # divisor used to build the thickness grid -- these are separate concepts.
    _n_constr = int(getattr(config, "N_CONSTRUCTION_LAYERS", 3))
    # THE 2D PATH HAS NO REFINEMENT GATE.  multizone=False is the DEFAULT, so
    # a plain `python main.py` runs it, and the gate is guarded on
    # cfg['multizone'] -- meaning the obvious command produces a winner that
    # nothing verified at 2n/4n.  It must not read like a gated result.
    if not (global_best_config or {}).get('multizone'):
        print()
        print("=" * 72)
        print("  UNGATED RESULT -- NOT REFINEMENT-VERIFIED")
        print("=" * 72)
        print("  This is the 2-mix (non-multizone) path. The refinement gate")
        print("  only runs on multizone designs, so this winner has NOT been")
        print("  re-evaluated at 2x/4x resolution and its utilisation may")
        print("  understate the converged value. On the pinned design the")
        print("  screen-to-converged drift was +1.2% of D/C.")
        print("  Run with multizone=True for a gated result.")
        print("=" * 72)
        print()
    print(f"PHYSICAL HULL TARGETS:")
    print(f" -> Trowel Thickness: {_win_t:.4f} inches "
          f"({describe_layer_breakdown(_win_t, _n_constr)})")
    print(f" -> Mold Depth:       {global_best_config['depth']:.2f} inches")
    print(f" -> Optimal Weight:   {best_res.fun:.1f} lbs")

    if global_best_config.get('multizone'):
        _n_distinct = len(set(round(float(d), 1) for d in best_mixes))
        print(f"\n--- {len(best_mixes)}-Zone I-Beam Pour "
              f"({_n_distinct} distinct mix{'es' if _n_distinct != 1 else ''}) ---")
        for _i, _pcf in enumerate(best_mixes):     # bottom→top
            _pos = ("Bottom" if _i == 0 else
                    "Top" if _i == len(best_mixes) - 1 else f"Zone {_i + 1}")
            print(f"Zone {_i + 1} ({_pos}): {float(_pcf):.1f} pcf")
    else:
        _mix_labels = ["Heavy/Structural", "Medium", "Lightweight"]
        print(f"\n--- {len(best_mixes)}-Mix Pour Strategy (Stress-Ranked) ---")
        for _i, _pcf in enumerate(best_mixes):
            _lbl = _mix_labels[_i] if _i < len(_mix_labels) else f"Mix {_i + 1}"
            print(f"Mix {_i + 1} ({_lbl}): {_pcf:.1f} pcf")

    if _companion is not None:
        # Companion runs exist only to price a restricted mix pool.  Stop here:
        # no graphics, no PDFs, no spec recursion, nothing written to disk.
        return {"weight_lbs": float(global_best_weight),
                "mixes_pcf": [float(m) for m in
                              (global_best_config.get("mixes") or [])]}

    # ── Refinement verification gate ───────────────────────────────────────
    # The search ran at LONGITUDINAL_STATIONS, where the gating utilisation is
    # NOT converged (see structural.kupfer_acceptance_limit).  Re-evaluate the
    # WINNER -- one design, one evaluation, seconds against an hour-long sweep --
    # at double that resolution, and require it to clear the ACCEPTANCE limit
    # there, not merely the tightened search limit it was selected under.
    # kupfer_search_limit makes this pass by construction; this PROVES it.
    # Converged hull weight for the winner.  The search-grid figure is
    # systematically light (the shell-area integral converges first order), so
    # the project quotes the extrapolation and keeps the raw value beside it.
    announce_phase(progress_callback, SWEEP_PHASES[1][0],
                   "Converged weight (Richardson extrapolation)",
                   clock=_clock, phase="converged weight (Richardson)",
                   detail="re-evaluates the WINNER at 2x and 4x the search"
                   " resolution — about 7 evaluations")
    _rw = _richardson_weight(global_best_config, filepath_matrix,
                             true_length_ft, true_draft_in)
    if _rw is not None and _rw.get("failed"):
        # A failed Richardson step used to return None, identical to "not
        # applicable" -- the report simply omitted the converged weight and
        # nothing said the number had been attempted and lost.
        print("\n  !! CONVERGED WEIGHT COULD NOT BE COMPUTED: %s"
              % _rw.get("reason"))
        print("     The search-grid weight below has NOT been Richardson-"
              "extrapolated; it is the coarse-grid value.")
        _rw = None
    if _rw is not None:
        print(f"\n  Hull weight   search grid  n={_rw['n']:<5} "
              f"{_rw['w_n']:8.3f} lb")
        print(f"                refined      n={2*_rw['n']-1:<5} "
              f"{_rw['w_2n']:8.3f} lb   ({_rw['w_2n']-_rw['w_n']:+.3f})")
        print(f"                CONVERGED (Richardson)  "
              f"{_rw['w_converged']:8.3f} lb   "
              f"({_rw['w_converged']-_rw['w_n']:+.3f} vs the search grid)")

    # ---- THE GATE ---------------------------------------------------------
    # Screen loose at the search grid, then refine the k lightest and accept on
    # each design's OWN extrapolated value.  Runs before the winner is treated
    # as final: if the screen winner does not survive refinement, the gate
    # hands back the lightest candidate that does.
    _gate_records, _gate_notes = [], []
    if global_best_config is not None and global_best_config.get('multizone'):
        try:
            if not gate_pool_fed:
                raise RuntimeError(
                    "the gate pool was never fed: the sweep produced a winner "
                    "(%s lb) but no configuration was ever recorded as a gate "
                    "candidate. That is a wiring fault, not an empty result -- "
                    "an empty pool would otherwise read as 'nothing survived "
                    "refinement' and hide it."
                    % (global_best_config.get('weight_lbs') or '?'))
            _k_gate = min(config_int("GATE_TOP_K", 12), len(gate_pool))
            announce_phase(
                progress_callback, SWEEP_PHASES[2][0],
                f"Refinement gate — re-solving the {_k_gate} lightest"
                f" designs",
                clock=_clock, phase="refinement gate",
                detail=f"each at 2x AND 4x the search resolution: about"
                f" {2 * _k_gate} evaluations, each costlier than a search"
                f" evaluation")
            _gw, _gate_records, _gate_notes = _gate_refine_top_k(
                gate_pool, filepath_matrix, true_length_ft, true_draft_in,
                freeboard_min_in, flare_max_deg, enable_mesh)
            print("\n" + "=" * 72)
            print("  REFINEMENT GATE  (screen n=%d -> 2n -> 4n, per-design "
                  "extrapolation)" % config_int("LONGITUDINAL_STATIONS", 161))
            print("=" * 72)
            for _n in _gate_notes:
                print("  " + _n)
            for _r in _gate_records:
                if _r.get("u_extrapolated") is None:
                    print("   #%-2d  %8s lb   REJECTED  (%s)"
                          % (_r["rank"],
                             ("%.3f" % _r["weight_screen"]) if _r.get("weight_screen") else "?",
                             _r.get("reason") or "no extrapolation"))
                    continue
                # A non-monotone design has NO ratio -- it was bounded, not
                # fitted.  Printing float('nan') there read like a crash to
                # anyone who does not know the internals, in the one table the
                # team is meant to read and trust.  Say "bounded" instead.
                if _r.get("ratio_used") is None:
                    _how = "bounded: max(seen) + largest step"
                else:
                    _how = ("r_meas %.3f, r_used %.2f"
                            % (_r["ratio_measured"], _r["ratio_used"]))
                print("   #%-2d  %8.3f lb  D/C %s  ->  extrap %.6f  (%s)%s  %s"
                      % (_r["rank"], _r["weight_screen"],
                         "/".join("%.5f" % u for u in _r["u"]),
                         _r["u_extrapolated"], _how,
                         "  [8n]" if _r.get("promoted_to_8n") else "",
                         "ACCEPT" if _r["accepted"] else "reject"))
            # Compare by WEIGHT, not identity: gate_pool holds dict COPIES, so
            # `_gw is not global_best_config` was ALWAYS true and this fired on
            # every accepted winner -- announcing a promotion that never
            # happened.  Caught by reading the demo output, not by a test.
            _gw_w = None if _gw is None else float(_gw.get('weight_lbs') or 0.0)
            if _gw is not None and _gw_w > global_best_weight + 1e-9:
                print("\n  The screen winner did NOT survive refinement; the "
                      "gate promoted a heavier candidate that did "
                      "(%.3f -> %.3f lb)." % (global_best_weight, _gw_w))
                # The promoted record is LEAN -- 13 fields, no geometry --
                # and everything downstream indexes it like a full config.
                # Rebuild before it becomes the winner, and FAIL LOUDLY if the
                # rebuild cannot happen: reporting a promoted design without
                # its geometry is what crashed a completed sweep at the report
                # step with `KeyError: 'x'`.
                _rehydrated = rehydrate_gate_winner(
                    _gw, filepath_matrix, true_length_ft, true_draft_in,
                    config_int("LONGITUDINAL_STATIONS", 161))
                if _rehydrated is None:
                    raise RuntimeError(
                        "the refinement gate promoted a candidate but its "
                        "geometry could not be rebuilt from the lean record "
                        "(offsets unreadable at depth "
                        f"{_gw.get('depth')}). The sweep is complete and its "
                        "winner is known; refusing to report it without the "
                        "geometry it was gated on.")
                global_best_config = _rehydrated
                global_best_weight = _gw_w
            elif _gw is not None:
                print("\n  The screen winner survived refinement.")
            elif _gw is None:
                print("\n  The gate accepted NOTHING. Reporting the screen "
                      "winner UNVERIFIED -- do not build to it.")
        except (NameError, AttributeError, TypeError) as _e:
            # A programming error in the gate is NOT a skippable condition.
            # Swallowing one let the sweep report a winner the gate had never
            # touched: `name 'L_ft' is not defined` printed as a one-line
            # "gate skipped" and everything downstream looked normal.
            raise RuntimeError(
                "refinement gate is broken (%s: %s). This is a bug in the "
                "gate, not a condition to skip: a winner reported without it "
                "has NOT been verified at 2n/4n." % (type(_e).__name__, _e))
        except Exception as _e:
            # Genuinely skippable: missing geometry, an unreadable offset file.
            domain_only(_e)   # a bug is not a skippable condition
            logger.warning("refinement gate skipped: %s", _e, exc_info=True)
            print("  ⚠  refinement gate SKIPPED (%s) -- the winner below "
                  "is NOT refinement-verified." % _e)

    announce_phase(progress_callback, SWEEP_PHASES[3][0],
                   "Winner verification at double resolution",
                   clock=_clock, phase="winner verification",
                   detail="proves the winner clears the ACCEPTANCE limit, not"
                   " just the tightened search limit")
    _vr = _verify_winner_refinement(global_best_config, filepath_matrix,
                                    true_length_ft, true_draft_in,
                                    freeboard_min_in, flare_max_deg, enable_mesh)
    if _vr is not None and _vr.get("failed"):
        print("\n  !! REFINEMENT VERIFICATION COULD NOT BE RUN: %s"
              % _vr.get("reason"))
        print("     The winner below has NOT been verified at higher "
              "resolution. This is not the same as 'no verification needed'.")
    if _vr is not None and not _vr["ok"] and not _vr.get("failed"):
        _acc, _us, _uv = _vr["accept"], _vr["u_search"], _vr["u_verify"]
        print("\n" + "=" * 74)
        print("❌ WINNER REJECTED BY THE REFINEMENT CHECK")
        print("=" * 74)
        print(f"    design             {_vr['mixes']} pcf, t={_vr['t_in']:.4f} in, "
              f"depth {_vr['depth_in']:.2f} in, {_vr['weight']:.2f} lb")
        print(f"    search grid        {_vr['n_search']:,} stations  ->  "
              f"Kupfer D/C {_us:.4f}   (search limit {_vr['search_limit']:.4f})")
        print(f"    verification grid  {_vr['n_verify']:,} stations  ->  "
              f"Kupfer D/C {_uv:.4f}")
        print(f"    ACCEPTANCE LIMIT   {_acc:.4f}  --  EXCEEDED by "
              f"{_uv - _acc:+.4f} ({100.0 * (_uv / _acc - 1.0):+.2f}%)")
        print(f"    drift this design  {100.0 * (_uv / max(_us, 1e-12) - 1.0):+.2f}% "
              f"over one doubling, vs the "
              f"{100.0 * structural.kupfer_drift_per_doubling():.2f}% assumed")
        print("\n    The winner passed the search but does not survive refinement,")
        print("    so it is not a design.  Its utilisation grew faster than the")
        print("    drift the search limit was derived from.")
        print("\n    FIX: raise structural.kupfer_drift_per_doubling in")
        print("    canoe_suite.cfg (currently "
              f"{structural.kupfer_drift_per_doubling():.4f}) so the search gates")
        print("    harder, or lower kupfer_acceptance_max, then re-run.")
        print("=" * 74)
        return None
    if _vr is not None:
        print(f"  Refinement check: Kupfer {_vr['u_search']:.4f} at "
              f"{_vr['n_search']:,} stations -> {_vr['u_verify']:.4f} at "
              f"{_vr['n_verify']:,}; acceptance {_vr['accept']:.2f} — PASS.")

    print("\n--- Generating ASCE report graphics ---")

    x, dx = global_best_config['x'], global_best_config['dx']

    if global_best_config.get('multizone'):
        # Multi-zone (I-beam): rebuild via the composite evaluator.
        sr = _build_multizone_result(global_best_config, best_res.x)
    else:
        A_ft2, I_in4 = global_best_config['A_ft2'], global_best_config['I_in4']
        c_top, c_bot, width_ft = (global_best_config['c_top'], global_best_config['c_bot'],
                                   global_best_config['width_ft'])
        A_top_ft2, A_bot_ft2 = global_best_config['A_top_ft2'], global_best_config['A_bot_ft2']

        # Re-evaluate the winner with the SAME structural options it won with —
        # omitting gunwale/mesh kwargs here evaluates a weaker structure and can
        # return the 1e6 infeasibility penalty for a genuinely feasible winner.
        sr = evaluate_canoe_weight(
            full_winning_vars, x, dx, A_ft2, I_in4, global_best_config['Q_in3'],
            c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2,
            global_best_config['thickness'], return_arrays=True,
            gw_width_mid=global_best_config.get('gw_width_mid', 0.0),
            gw_height_mid=global_best_config.get('gw_height_mid', 0.0),
            enable_mesh=global_best_config.get('enable_mesh', False),
            gw_fillet_ratio=gw_fillet_ratio,
            # The winner's OWN five-race seating.  Without this the call falls
            # back to the legacy 4+2 layout and slices a 14-seat vector as
            # vars[0:4]/vars[4:6]/vars[6:] -- reading seat coordinates as mix
            # densities and failing a genuinely feasible winner.
            crew_cases=config.crew_load_cases(global_best_config['seats']),
            position_variance_ft=global_best_config.get('pos_var_ft', 0.0),
            cum_area_in2=global_best_config.get('cum_area'),
            z_grid_in=global_best_config.get('z_grid'),
            cum_Az_in3=global_best_config.get('cum_Az'),
            cum_Iy_in4=global_best_config.get('cum_Iy'),
            yhalf_in=global_best_config.get('yhalf'),
            rocker_ft=_struct_rocker_ft(global_best_config['thickness']),
        )
        if not hasattr(sr, "M_envelope"):
            # evaluate_canoe_weight returned its scalar penalty — the winner
            # failed re-evaluation, which means the two calls diverged somehow.
            print("\n❌ SWEEP FAILED: the winning configuration failed its final "
                  f"re-evaluation (penalty={sr!r}).")
            print("   The report evaluation ran with different structural options "
                  "than the sweep — please report this as a bug.")
            return None
        sr.depth_in = global_best_config['depth']

    # ── Governing-case report (signed hog/sag envelopes, fix plan C2) ────────
    governing = {}
    if sr.util_bot_t is not None:
        def _case_at(gov_arr, i):
            ci = int(gov_arr[i]) if gov_arr is not None else -1
            return (sr.case_names[ci].upper()
                    if sr.case_names and 0 <= ci < len(sr.case_names) else "—")

        i_bt = int(np.argmax(sr.util_bot_t))
        i_tt = int(np.argmax(sr.util_top_t))
        governing = {
            "bottom_tension": {"case": _case_at(sr.gov_sag, i_bt),
                               "x_ft": float(sr.x_ft[i_bt]),
                               "dc": float(sr.util_bot_t[i_bt])},
            "top_tension":    {"case": _case_at(sr.gov_hog, i_tt),
                               "x_ft": float(sr.x_ft[i_tt]),
                               "dc": float(sr.util_top_t[i_tt])},
        }
        print("\n--- Governing Load Cases (signed hog/sag envelopes) ---")
        print(f" Bottom fiber tension (SAGGING): {governing['bottom_tension']['case']}"
              f" @ x={governing['bottom_tension']['x_ft']:.1f} ft"
              f"   D/C = {governing['bottom_tension']['dc']:.2f}")
        print(f" Top fiber tension   (HOGGING): {governing['top_tension']['case']}"
              f" @ x={governing['top_tension']['x_ft']:.1f} ft"
              f"   D/C = {governing['top_tension']['dc']:.2f}")
        _worst = max(governing.values(), key=lambda g: g["dc"])
        _sense = ("SAGGING (bottom fiber)"
                  if _worst is governing["bottom_tension"]
                  else "HOGGING (gunwale/top fiber)")
        print(f" → Overall: {_sense} governs — {_worst['case']}"
              f" @ x={_worst['x_ft']:.1f} ft, D/C = {_worst['dc']:.2f}")
        print(f" Peak M_sag = {float(np.max(sr.M_sag_env)):.0f} ft·lb   "
              f"Peak M_hog = {float(np.max(sr.M_hog_env)):.0f} ft·lb")
        _uc = max(float(np.max(sr.util_bot_c)), float(np.max(sr.util_top_c)))
        if _uc > 1.0:
            print(f" [ADVISORY] Compression D/C = {_uc:.2f} > 1.0 — the "
                  "light mix's f'c may be exceeded (not gated; verify mix data).")
        # GOVERNING GATE FIRST.  The legacy per-station tension array below
        # is NOT the gate and has not been since the Kupfer biaxial criterion
        # landed.  Printing it as the headline made a design at 93.7% of its
        # Kupfer limit look like it had half its capacity spare (0.57 legacy
        # vs 0.87 Kupfer), which sent a real investigation hunting for a
        # constraint that does not exist.  Same class as the torsion
        # advisory: report it, label it, never let it read as the number in
        # force.
        _kd = global_best_config.get("kupfer_dc")
        if _kd is not None:
            _lim = structural.kupfer_search_limit()
            _acc = structural.kupfer_acceptance_limit()
            print("")
            print(f" GOVERNING GATE: Kupfer-Gerstle biaxial   D/C = {float(_kd):.4f}")
            print(f"   vs search limit {_lim:.4f} -> "
                  f"{100.0*float(_kd)/_lim:.1f}% utilised   "
                  f"(acceptance {_acc:.4f} -> {100.0*float(_kd)/_acc:.1f}%)")
            _g = global_best_config.get("kupfer_gov") or {}
            if _g:
                print(f"   governing at station {_g.get('station_index')}, "
                      f"fibre {_g.get('fibre_index')} "
                      f"(z = {float(_g.get('fibre_z_in', 0.0)):.2f} in), "
                      f"case index {_g.get('case_index')}, {_g.get('quadrant')}")

        # Legacy per-station longitudinal check -- NOT the gate.
        _ut = np.maximum(sr.util_bot_t, sr.util_top_t)
        print(f" [legacy - subsumed by Kupfer, not the gate] per-station "
              f"tension D/C: peak {float(np.max(_ut)):.2f}   "
              f"mean {float(np.mean(_ut)):.2f}   "
              f"stations at >= 0.85: {int(np.count_nonzero(_ut >= 0.85))}"
              f"/{len(_ut)}")

    # Index order (env, 4P, 2P, 3P) is load-bearing: downstream code reads
    # M_envelope[2] for the 2-person governing moment.
    M_tuple = (sr.M_envelope, sr.M_race_4, sr.M_race_2, sr.M_race_3)
    V_dict  = {"max_pos": sr.V_max_pos, "max_neg": sr.V_max_neg,
               "race_4": sr.V_race_4,   "race_2": sr.V_race_2,
               "race_3": sr.V_race_3}

    # Seat marks for the plots, taken from the winner's STORED five-race
    # seating.  Slicing best_res.x by hand here read 2M+2F as "the four" and
    # 3M's first two as "the two", so the moment plot marked seats no crew uses.
    _win_seats = global_best_config['seats']
    _seats = {name: [float(q) for q in _win_seats[name]]
              for name in config.RACE_CATEGORY_ORDER}
    plot_structural_envelope(sr.x_ft, M_tuple, _report_path("ASCE_Moment_Envelope.pdf"),
                             sag_hog=(sr.M_sag_env, sr.M_hog_env),
                             seats=_seats)
    plot_shear_envelope(sr.x_ft, V_dict, _report_path("ASCE_Shear_Envelope.pdf"))
    plot_pour_zones(sr.x_ft, sr.pour_layout, _report_path("ASCE_Pour_Zones.pdf"),
                    na_line=sr.na_line, pour_line=sr.pour_line_in,
                    sheer_line=sr.sheer_line_in, mixes_pcf=list(best_mixes),
                    zone_pour_lines=getattr(sr, "zone_pour_lines", None),
                    zone_densities=getattr(sr, "zone_densities", None))
    if sr.util_bot_t is not None:
        from visualization import plot_utilization
        plot_utilization(sr.x_ft,
                         {"bot_t": sr.util_bot_t, "top_t": sr.util_top_t,
                          "bot_c": sr.util_bot_c, "top_c": sr.util_top_c},
                         _report_path("ASCE_Utilization.pdf"))

    # ── 3-D hull preview ────────────────────────────────────────────────────
    from visualization import plot_hull_3d
    from matplotlib.backends.backend_pdf import PdfPages
    _x_raw   = global_best_config.get('x_raw')
    _z_raw   = global_best_config.get('z_raw')
    _y_mat   = global_best_config.get('y_matrix')
    if _x_raw is not None and _y_mat is not None:
        _L_m = float(np.max(_x_raw)) * 0.3048          # ft → m
        _T_m = float(np.max(_z_raw)) * 0.0254          # in → m
        _off = _y_mat * 0.0254                          # in → m
        _hull_title = (
            f"Optimised Hull  —  t={t_in}\"  d={d_in:.2f}\"  "
            f"{global_best_config['best_res_weight']:.1f} lb"
            if 'best_res_weight' in global_best_config
            else f"Optimised Hull  —  t={t_in}\"  d={d_in:.2f}\""
        )
        fig_hull = plot_hull_3d(_off, _L_m, _T_m, title=_hull_title)
        _hull_pdf = _report_path("ASCE_Optimized_Hull.pdf")
        with PdfPages(_hull_pdf) as _pdf:
            _pdf.savefig(fig_hull)
        fig_hull.clf()
        print("3-D hull preview saved.")
    else:
        print("  [Hull preview] raw geometry not available — skipping PDF.")

    print("Graphics saved successfully.")

    # ── Torsion + lateral checks ────────────────────────────────────────
    from structural import run_torsion_lateral_checks
    t_in  = global_best_config["thickness"]
    d_in  = global_best_config["depth"]

    if global_best_config.get('multizone'):
        # Reconstruct section inputs from the composite winner
        from structural import _cum_at
        _sheer = sr.sheer_line_in
        A_in2  = _cum_at(global_best_config['cum_area'],
                         global_best_config['z_grid'], _sheer)   # full area/stn
        _width_ft = global_best_config['width_ft']
        _c_bot = sr.na_line
        _c_top = np.maximum(_sheer - sr.na_line, 1e-6)
        # ISSUE 1 FIX: the torsion/lateral checks concern the upper hull /
        # gunwale, so they use the TOP-zone mix (sr.zone_densities[-1]) — the
        # material actually there — not min(best_mixes) (lightest in the batch).
        _sec_den = float(sr.zone_densities[-1])
    else:
        A_in2  = A_ft2 * 144.0  # ft^2 -> in^2
        _width_ft, _c_bot, _c_top = width_ft, c_bot, c_top
        _sec_den = float(min(best_mixes))

    lm_props      = config.get_mix_properties(_sec_den)
    allow_shear   = lm_props.get("strength_t", 50.0) * 0.55
    allow_bend    = lm_props.get("strength_t", 50.0)
    paddler_w     = list(config.RACE_4_PERSON)
    # sr.weight_lbs is REJECTED when the winner rebuild came back infeasible;
    # adding crew to it used to yield a million-pound canoe that then "swamped".
    if structural.is_rejected(getattr(sr, "weight_lbs", None)):
        raise RuntimeError(
            "the winning design re-evaluated as INFEASIBLE -- it has no weight "
            "and must not reach the report; this is a gate/report divergence")
    total_w_lb    = sr.weight_lbs + sum(paddler_w)

    try:
        _mvert = np.maximum(np.abs(np.asarray(sr.M_sag_env)),
                            np.abs(np.asarray(sr.M_hog_env)))
        _res_obj = global_best_config.get('result')
        _padpos = (np.asarray(_res_obj.x)[:4]
                   if _res_obj is not None and getattr(_res_obj, 'x', None) is not None
                   else None)
        tl_result = run_torsion_lateral_checks(
            sr.x_ft, A_in2, _width_ft, _c_bot, _c_top, t_in, d_in,
            allow_shear, allow_bend,
            paddler_weights=paddler_w,
            total_weight_lbs=total_w_lb,
            m_vert_env_lbft=_mvert,
            paddler_positions_ft=_padpos,
            avg_speed_mps=getattr(config, "MANEUVER_SPEED_MPS", 2.5),
            min_turn_radius_m=getattr(config, "MANEUVER_TURN_RADIUS_M", 4.0),
        )
    except Exception as _tl_exc:
        domain_only(_tl_exc)   # a bug is not a skippable condition
        tl_result = None
        print(f"[Torsion/Lateral] skipped: {_tl_exc}")

    # ── Transverse stability on the REAL pour ───────────────────────────────
    # The pre-sweep gate uses compute_hydrostatics' form-only GM_T, which assumes
    # KG = waterline and therefore cannot see where the concrete sits.  This suite
    # is the winner's ACTUAL stability: the zoned-pour shell CG, bare and loaded.
    # Reported only — MIN_GMT_IN stays the gate, and stays 0 until the numbers are
    # trusted (last season's hull was accepted with B below G in every racing case).
    _stability = None
    try:
        from structural import (hull_vertical_cg_in, stability_conventions,
                                gunwale_rail_section)
        _gb = global_best_config
        _zdens = list(getattr(sr, "zone_densities", None) or best_mixes)
        if _gb.get('multizone') and _bmt_in > 1e-9:
            _sh = sr.sheer_line_in
            _zb = [np.zeros(len(_gb['x']))]
            for _f in np.asarray(_gb['pour_fracs'], dtype=float):
                _zb.append(_f * _sh)
            _zb.append(np.asarray(_sh, dtype=float))
            # The rail is real mass at the sheer — the highest concrete in the
            # section — so it must be in KG, not just in the weight.
            _rail = gunwale_rail_section(
                _gb['x'], t_in, _gb.get('gw_width_mid'), _gb.get('gw_height_mid'),
                _gb.get('gw_fillet_ratio'), _sh)
            _kg_hull = hull_vertical_cg_in(
                _gb['cum_area'], _gb['cum_Az'], _gb['z_grid'], _zb,
                _zdens, _gb['x'], rail=_rail, rail_density=float(_zdens[-1]))
            # Crew CG from segmented anthropometry rather than a flat guess.
            # The crew outweighs the hull ~4:1, so this is the largest single
            # term in loaded stability -- and the datum matters: the model
            # returns height above the hull FLOOR, while GM_T is measured from
            # the KEEL, so the shell thickness and any knee pad are added.
            _crew_kg = None
            _crew_band = None
            _crew_basis = None
            try:
                from physics.biomechanics import crew_kg_in, crew_kg_band_in
                # The HEAVIEST crew, computed -- the same crew the freeboard
                # ceiling uses.  Taking 4_coed here while the ceiling used a
                # computed max would report stability for a different crew than
                # the one the hull was gated against.
                _stab_cat = config.heaviest_crew_category()
                _sx, _ht, _wt = config.race_category_crew(_stab_cat)
                _datum = float(t_in) + float(getattr(config, "KNEE_PAD_IN", 0.0))
                _hip = getattr(config, "KNEEL_HIP_HEIGHT_IN", None)
                _post = getattr(config, "KNEEL_POSTURE", "B_upright_kneel")
                _th = float(getattr(config, "TORSO_ANGLE_DEG", 70.0))
                _crew_kg = crew_kg_in(_ht, _sx, _wt, _th, posture=_post,
                                      hip_height_in=_hip, datum_offset_in=_datum)
                _crew_band = crew_kg_band_in(_ht, _sx, _wt, datum_offset_in=_datum)
                _crew_basis = (
                    f"biomechanical model, 4_coed, posture {_post}, "
                    f"torso {_th:.0f} deg, "
                    + (f"MEASURED hip {float(_hip):.1f} in" if _hip is not None
                       else "hip from posture preset (NOT measured)"))
            except Exception as _bio_exc:
                # DELIBERATELY BROAD.  The rule: a broad catch is justified when
                # an exception would DISCARD WORK ALREADY COMPLETED, not when it
                # would skip work not yet attempted.
                #
                # The sweep is over by the time this line runs and the winner is
                # in hand.  Losing the biomechanical crew CG costs one printed
                # figure and falls back to the flat assumption, which is what
                # this suite shipped for years; letting the exception out of
                # here costs the entire run.  Not silent, and the type is named
                # because a bare message from a KeyError says nothing.
                print(f"[Stability] crew-CG model unavailable, falling back to "
                      f"the flat assumption: "
                      f"{type(_bio_exc).__name__}: {_bio_exc}")
            _stability = stability_conventions(
                _kb_in, _bmt_in, _kg_hull, float(sr.weight_lbs),
                config.heaviest_crew_lbs(), gmt_form_in=float(_gmt_in),
                crew_kg_in=_crew_kg, crew_kg_band=_crew_band,
                crew_kg_basis=_crew_basis)
            print(f"\n  Transverse stability (GM_T, in) — KB {_kb_in:.2f}, "
                  f"BM_T {_bmt_in:.2f}, KG_hull {_kg_hull:.2f}")
            print(f"    form-only (KG = waterline) : {_stability['gmt_form_in']:6.2f}"
                  "   <- hull shape only; comparable to a published hull-form figure")
            print(f"    bare hull  (real KG)       : "
                  f"{_stability['gmt_bare_hull_in']:6.2f}")
            print(f"    loaded     (real KG + crew): "
                  f"{_stability['gmt_loaded_in']:6.2f}"
                  f"   (crew CG {_stability['kg_crew_in']:.2f} in)")
            print(f"    crew CG basis: {_stability['crew_kg_basis']}")
            _leg = _stability['gmt_loaded_legacy_in']
            if abs(_stability['gmt_loaded_in'] - _leg) > 1e-9:
                print(f"    vs the old flat {_stability['crew_kg_legacy_in']:.0f} in "
                      f"assumption: GM_T {_leg:+.2f} -> "
                      f"{_stability['gmt_loaded_in']:+.2f} in "
                      f"({_stability['gmt_loaded_in'] - _leg:+.2f})")
            _band = _stability.get('crew_kg_posture_band')
            if _band:
                print("    posture x torso-angle band (posture DOMINATES):")
                for _p, _th, _kg, _gm in _band:
                    print(f"      {_p:<16} theta={_th:4.0f} deg  "
                          f"crew KG {_kg:6.2f} in  ->  GM_T {_gm:+6.2f} in")
                _gms = [g for *_r, g in _band]
                if min(_gms) < 0.0:
                    print("      NOTE: loaded GM_T is NEGATIVE over part of this "
                          "band — the hull is not self-righting with the crew")
                    print("      aboard; it is held upright by the paddlers "
                          "actively balancing it.")
            else:
                print("    loaded vs assumed crew CG: " + ",  ".join(
                    f"{c:.0f} in -> {g:+.2f}"
                    for c, g in _stability['crew_kg_sensitivity']))
    except Exception as _st_exc:
        # DELIBERATELY BROAD, same rule: this reports on a winner that already
        # exists.  Every number in the block above is decoration on a completed
        # sweep -- GM_T does not gate anything -- so an exception here must cost
        # the stability printout and nothing else.  `_stability = None` is a
        # distinct flag, not a numeric sentinel: the only downstream reader is
        # `if _stability is not None`, so a failure SKIPS the trade study rather
        # than feeding it a fabricated GM_T.
        _stability = None
        print(f"[Stability] real-KG suite skipped: "
              f"{type(_st_exc).__name__}: {_st_exc}")

    # The I-beam's dense TOP flange buys bending capacity and costs stability.
    # Measure that trade on the winner rather than asserting it.
    _stab_trade = None
    if _stability is not None:
        _stab_trade = _top_flange_stability_trade(
            global_best_config, sr, _kb_in, _bmt_in,
            config.heaviest_crew_lbs(), true_draft_in,
            freeboard_min_in, flare_max_deg, enable_mesh,
            # No freeboard ceiling needed: the variant only ever LIGHTENS the top
            # zone, and the winner already cleared the ceiling at this depth, so a
            # strictly lighter hull cannot violate it.
            hull_weight_max_lb=None,
            crew_kg_in=_stability["kg_crew_in"])
        if _stab_trade is not None and _stab_trade.get("n_a"):
            print(f"    top-flange trade: n/a — {_stab_trade['n_a']}")
        elif _stab_trade is None:
            print("    top-flange trade: could not be evaluated (see log)")
        else:
            _d_gm = _stab_trade['gmt_loaded_in'] - _stability['gmt_loaded_in']
            # Base weight from the SAME geometric integral as the variant.
            # Reading sr.weight_lbs here is what printed -999842.82 lb when the
            # winner rebuild came back REJECTED.
            _d_w = (_stab_trade['weight_lbs']
                    - _stab_trade.get('base_weight_lbs', sr.weight_lbs))
            _d_u = _stab_trade['kupfer_dc'] - float(getattr(sr, 'kupfer_dc', 0.0)
                                                    or global_best_config.get('kupfer_dc') or 0.0)
            print(f"    top-flange trade: lighten top zone "
                  f"{_stab_trade['base_stack'][-1]:.1f} -> "
                  f"{_stab_trade['alt_stack'][-1]:.1f} pcf  =>  "
                  f"GM_T {_d_gm:+.2f} in, weight {_d_w:+.2f} lb, "
                  f"Kupfer D/C {_d_u:+.3f}"
                  f"  [{'feasible' if _stab_trade['feasible'] else 'INFEASIBLE'}]")

    # ── Seat pins: what they cover, and what they cost ─────────────────────
    # Pinning shrinks the arrangement set, so a pinned design is valid only for
    # the pinned subset.  Price that against the fully-enumerated run -- same
    # seats, same weights, same geometry, pins the only difference.
    _pin_cost = None
    _pin_disclosure = global_best_config.get('arrangement_disclosure')
    if config.any_seats_pinned():
        _pin_cost = _seat_pin_cost(
            global_best_config, sr,
            global_best_config.get('seats') or {}, true_draft_in,
            freeboard_min_in, flare_max_deg, enable_mesh)
        print("\n  Seat pins (conservatism REDUCED — see the report):")
        for _cat, _d in (_pin_disclosure or {}).items():
            if _d.get("fully_enumerated"):
                continue
            print(f"    {_cat:<8} {_d['n_evaluated']} of {_d['n_possible']} "
                  f"orderings evaluated; seats pinned "
                  f"{ {k: v for k, v in _d['pinned_seats'].items()} }")
        if _pin_cost:
            _dk = _pin_cost["unpinned_kupfer_dc"] - float(
                global_best_config.get('kupfer_dc') or 0.0)
            print(f"    COST OF THE PIN: Kupfer D/C "
                  f"{global_best_config.get('kupfer_dc'):.4f} pinned vs "
                  f"{_pin_cost['unpinned_kupfer_dc']:.4f} fully enumerated "
                  f"({_dk:+.4f})")
            if _dk > 1e-9:
                print("    -> the pinned set EXCLUDES worse arrangements; the "
                      "design is valid ONLY for the pinned seating.")

    # ── Search-space provenance + winner boundary flags ─────────────────────
    # A result is only meaningful WITH the space it was found in.  Record the
    # ranges/steps actually swept, and flag when the winner sits on a bound —
    # the "the search space, not the design, may be the constraint" signal
    # (e.g. a thickness fenced to [0.5] hiding the true 0.375 optimum, or the
    # gunwale grid picking its own minimum = "no rail").
    _thk_vals = [float(v) for v in thickness_options]
    _dep_vals = [float(v) for v in depth_options]
    _all_dens = sorted({float(mm) for combo in valid_mixes for mm in combo})
    _dens_lo, _dens_hi = (min(_all_dens), max(_all_dens)) if _all_dens else (0.0, 0.0)
    _n_st_used = getattr(config, "LONGITUDINAL_STATIONS", None)
    try:
        _nat_x, _, _ = parse_godzilla_offsets(
            filepath_matrix, L_ft=true_length_ft, draft_in=true_draft_in,
            mold_depth_in=true_draft_in)
        _native_st = int(len(_nat_x))
    except Exception as _exc:
        domain_only(_exc)   # a bug is not a skippable condition
        _native_st = None
    _dens_step = (float(density_step_pcf) if density_step_pcf
                  else (2.0 if int(getattr(config, "MAX_MIXES", 2)) == 3 else 0.5))
    search_space = {
        "thickness_in": {"min": min(_thk_vals), "max": max(_thk_vals),
                         "n": len(_thk_vals), "physical_floor_in": 0.375},
        "depth_in": {"min": float(MIN_MOLD_DEPTH),
                     "max": float(MIN_MOLD_DEPTH + _depth_span),
                     "step": float(_depth_step), "n": len(_dep_vals),
                     "floor_reason": "4-person freeboard"},
        "density_pcf": {"min": _dens_lo, "max": _dens_hi, "step": _dens_step,
                        "n_combos": len(valid_mixes),
                        "cap_reason": "team Max Density"},
        "longitudinal_stations": {"used": int(_n_st_used) if _n_st_used else _native_st,
                                   "native": _native_st,
                                   "interpolated": bool(_n_st_used and _native_st
                                                        and _n_st_used > _native_st)},
        "gunwale": ({"width_in": [float(gw_min_width_in), float(gw_max_width_in)],
                     "height_in": [float(gw_min_height_in), float(gw_max_height_in)],
                     "steps": int(gw_steps)} if enable_gunwales else None),
    }
    _win_dens = list(getattr(sr, "zone_densities", None) or best_mixes)
    boundary_flags = []

    def _flag(var, val, edge, physical, why=""):
        boundary_flags.append({"variable": var, "value": float(val), "edge": edge,
                               "physical": bool(physical), "why": why})
    if abs(float(t_in) - min(_thk_vals)) < 1e-4:
        _flag("thickness", t_in, "min", abs(float(t_in) - 0.375) < 1e-4,
              "min buildable trowel")
    elif abs(float(t_in) - max(_thk_vals)) < 1e-4 and len(_thk_vals) > 1:
        _flag("thickness", t_in, "max", False, "")
    if abs(float(d_in) - float(MIN_MOLD_DEPTH)) < 1e-3:
        _flag("depth", d_in, "min", True, "4-person freeboard floor")
    elif abs(float(d_in) - max(_dep_vals)) < 1e-3 and len(_dep_vals) > 1:
        _flag("depth", d_in, "max", False, "")
    if _win_dens and _all_dens:
        if abs(max(_win_dens) - _dens_hi) < 1e-3:
            _flag("density", max(_win_dens), "max",
                  abs(_dens_hi - float(config.MAX_DENSITY_PCF)) < 1e-3, "team Max Density cap")
        if abs(min(_win_dens) - _dens_lo) < 1e-3:
            # The density floor is the regression's viability limit (tensile
            # strength → 0 below it), a physical floor, not a fenceable range.
            _flag("density", min(_win_dens), "min", True,
                  "regression viability floor (no tensile strength below)")
    if boundary_flags:
        print("\n  ⚠  WINNER AT SEARCH BOUNDARY:")
        for b in boundary_flags:
            kind = (f"physical limit ({b['why']})" if b["physical"]
                    else "SEARCH-RANGE EDGE — widen the range and re-run to confirm")
            print(f"      {b['variable']} = {b['value']:.3f} at {b['edge']}  [{kind}]")

    # ── Mix development spec ───────────────────────────────────────────────
    # The theoretical optimum's densities ARE the specification for the mix
    # team, so say plainly which of them exist and which have to be developed,
    # and price both the target and today's fallback.  Companion runs are cheap
    # (a handful of densities), so this stays on by default; it is skipped only
    # when the sweep was already restricted to real mixes, where the question
    # does not arise.
    mix_spec = None
    try:
        from reporting.mix_targets import (classify_densities, zone_names_for,
                                           MixTargetSpec, format_spec)
        _db = config.get_mix_db()
        _existing = _db.get_existing_mix_densities()
        mix_spec = MixTargetSpec(
            verdicts=classify_densities(
                _win_dens, _existing, config.get_mix_properties,
                zone_names_for(len(_win_dens))),
            theoretical_weight_lb=float(global_best_weight),
            existing_pcf=list(_existing),
            regression_n=getattr(_db, "_regression_n", None),
            regression_source=getattr(_db, "_regression_source", None),
        )
        if not _existing_only and not price_mix_development:
            # SKIPPED, and it has to SAY so.  An absent section reads as
            # "there was nothing to develop", which is the opposite of the
            # truth: there are targets, nobody priced them.  The whole point of
            # this block is that a missing section must never be mistaken for
            # an empty answer.
            _skipped = [v.density_pcf for v in mix_spec.needs_development]
            print("")
            print("  " + "-" * 68)
            print("  MIX-DEVELOPMENT PRICING: SKIPPED")
            if _skipped:
                print(f"    {len(_skipped)} target mix(es) "
                      f"({', '.join(f'{d:g} pcf' for d in _skipped)}) are not "
                      f"on record.")
            print("    This phase re-runs the whole search several more times "
                  "-- once for the")
            print("    mixes you already have, then once per target -- to "
                  "price what developing")
            print("    each one would actually save. It was measured at 93% of "
                  "a run's time.")
            print("    NOT priced here means NOT KNOWN, not 'worth nothing'.")
            print("    Turn it on:  jaguar-cli.exe --price-mix-development")
            print("                 or the Structural tab checkbox.")
            print("  " + "-" * 68)
        elif not _existing_only:
            _todo = [v.density_pcf for v in mix_spec.needs_development]

            # The lightest weight any earlier pricing pool reached.  Used for
            # REPORTING ONLY -- it is what makes "no improvement" sayable.
            #
            # It was briefly also fed to the workers as a prune bound, on the
            # reasoning that every pricing pool is a superset of the mixes on
            # record so this weight is achievable in all of them.  True, and
            # worth 0.0%: the combo walk is already ordered ascending by
            # min(combo), so the incumbent prune is tight from the first
            # feasible combo. Removed; see KNOWN_ISSUES, "Performance".
            _price_bound = {"lb": None}

            def _price(pool, label, note=""):
                r = run_grand_optimization(
                    freeboard_min_in=freeboard_min_in,
                    thickness_options_in=thickness_options_in,
                    precision_level=precision_level,
                    enable_gunwales=enable_gunwales, enable_mesh=enable_mesh,
                    gw_min_width_in=gw_min_width_in, gw_max_width_in=gw_max_width_in,
                    gw_min_height_in=gw_min_height_in, gw_max_height_in=gw_max_height_in,
                    gw_steps=gw_steps, gw_fillet_ratio=gw_fillet_ratio,
                    flare_max_deg=flare_max_deg, multizone=multizone,
                    num_zones=num_zones, depth_step_in=depth_step_in,
                    # Both depth forms travel: exactly one is ever set, and
                    # dropping the max would silently price the companion
                    # against the DEFAULT 4.5 in span instead of the user's.
                    depth_max_in=depth_max_in, depth_span_in=depth_span_in,
                    freeboard_max_in=freeboard_max_in,
                    density_step_pcf=density_step_pcf,
                    thickness_grid_overridden=thickness_grid_overridden,
                    _companion=pool)
                # THREE outcomes, and they must not be conflated.  A row
                # reading "274.12" next to a row reading "274.12" tells a
                # reader nothing about WHY they match; one of them saying "no
                # improvement" tells them the target was priced and found not
                # to pay.
                _w = None if r is None else r.get("weight_lbs")
                _ref = _price_bound["lb"]
                if _w is None:
                    # The physics refused: no design in this pool stands up.
                    _note = "no feasible design"
                elif _ref is not None and float(_w) >= _ref - 1e-9:
                    # Buildable, and no lighter than what we already had.
                    _note = f"no improvement on {_ref:.2f} lb"
                else:
                    _note = note                      # a real, better answer
                mix_spec.payoff.append({
                    "label": label, "weight_lb": _w, "note": _note,
                })
                if _w is not None and (_price_bound["lb"] is None
                                       or _w < _price_bound["lb"]):
                    _price_bound["lb"] = float(_w)
                return r

            # The expensive companion phase: each _price() call is a FULL
            # SWEEP over a restricted density set.  Say how many are
            # coming before the first one starts.
            _n_price = 1 + len(_todo) + (len(_todo) if len(_todo) > 1 else 0)
            announce_phase(
                progress_callback, SWEEP_PHASES[4][0],
                f"Mix-development pricing — {_n_price} further sweeps",
                clock=_clock, phase="mix-development pricing",
                detail="each one re-runs the whole search over a restricted"
                " density set; this phase can take LONGER than the"
                " search above")
            print("\n--- Pricing the buildable fallback and the development payoff ---")
            _today = _price(True, "mixes on record today",
                            "= the achievable fallback")
            if _today is not None:
                mix_spec.buildable_weight_lb = _today["weight_lbs"]
                mix_spec.buildable_mixes_pcf = _today["mixes_pcf"]
            # Price each target ALONE before pricing them together.  The mixes
            # in an I-beam are not independently useful -- a flange mix is worth
            # nothing until a web mix exists far enough below it to pair with
            # (the pour floor needs >= 2 pcf between batches) -- so "develop
            # this one first" is only answerable by asking one at a time.
            if len(_todo) > 1:
                for _t in _todo:
                    _price(sorted(set(list(_existing) + [_t])),
                           f"with only {_t:g} pcf added", "developed alone")
            _added = list(_existing)
            for _t in _todo:
                _added = sorted(set(_added + [_t]))
                _price(list(_added), f"+ develop {_t:g} pcf", "cumulative")
            if len(_todo) > 1:
                mix_spec.payoff.append({
                    # NOT the same pool as the final cumulative row above it.
                    # That row prices the real mixes on record PLUS the targets
                    # -- densities that exist or are being asked for.  This one
                    # is the parent search's own winner, over the continuous
                    # stepped density grid, which contains interpolated
                    # densities nobody has batched.  Two pools, two numbers, and
                    # a reader who saw "all targets developed" next to
                    # "+ develop 73.5 pcf" would reasonably assume they were
                    # the same thing.
                    "label": "full density grid (interpolated, not just "
                             "real mixes)",
                    "weight_lb": float(global_best_weight),
                    "note": "= the theoretical optimum, unconstrained by which "
                            "densities exist",
                })
        print(format_spec(mix_spec))
        if reports_dir:
            try:
                _spec_path = os.path.join(reports_dir, "mix_development_targets.txt")
                with open(_spec_path, "w", encoding="utf-8") as _fh:
                    _fh.write(format_spec(mix_spec))
                print(f"  Mix development spec written to {_spec_path}")
            except OSError as _e:
                logger.warning("could not write the mix spec: %s", _e)
    except Exception as _e:
        # DELIBERATELY BROAD, and the clearest case of the rule in this file.
        # The block above does not merely format a spec: it RE-PRICES the design
        # against several candidate mix sets, each a sweep of its own, on top of
        # a winner that already cost hours.  All of that is completed work.
        #
        # Its failure mode is OSError, and both triggers have been hit on this
        # project: a report directory that cannot be written, and a OneDrive
        # dehydrated file surfacing as a read error mid-pricing.  The inner
        # handler covers the spec write itself; this one covers everything else
        # the block touches.  Losing the spec must never lose the sweep.
        logger.warning("mix development spec skipped: %s: %s",
                       type(_e).__name__, _e, exc_info=True)
        print(f"  ⚠  mix development spec skipped: "
              f"{type(_e).__name__}: {_e}  (the winner above is unaffected)")

    _is_mz = bool(global_best_config.get('multizone'))
    _result = {
        "mix_spec": mix_spec,
        # Methodology block (LRFD/ASD, per-case load factors, phi/Omega, and the
        # unfactored dead/live curves).  Comes from the WINNER's own evaluation,
        # so the report can never describe a different run than it documents.
        "design": (getattr(sr, "design", None)
                   or _design_summary(getattr(sr, "case_names", None))),
        "search_space": search_space,
        "boundary_flags": boundary_flags,
        "x": sr.x_ft, "M_envelope": M_tuple,
        "V_envelope": V_dict,
        # Signed envelopes + governing-case trace (fix plan C2)
        "M_sag": sr.M_sag_env, "M_hog": sr.M_hog_env,
        "gov_sag": sr.gov_sag, "gov_hog": sr.gov_hog,
        "case_names": sr.case_names,
        "governing": governing,
        "utilization": {"bot_t": sr.util_bot_t, "top_t": sr.util_top_t,
                        "bot_c": sr.util_bot_c, "top_c": sr.util_top_c},
        # Transverse (hoop) per-station D/C from the transformed gate — the
        # authoritative transverse check for multi-zone (None on single-zone).
        "hoop_util": global_best_config.get("hoop_util"),
        "hoop_gov_zone": global_best_config.get("hoop_gov_zone"),
        "hoop_dc_homog": global_best_config.get("hoop_dc_homog"),
        # Kupfer biaxial gate (the binding tensile check) with the side-by-side
        # comparison values the report tabulates: old independent longitudinal
        # D/C, old independent transverse (hoop) D/C, Rankine D/C on the combined
        # state, and the governing fibre/station/case/quadrant.
        "long_dc": global_best_config.get("long_dc"),
        "hoop_dc": global_best_config.get("hoop_dc"),
        "rankine_dc": global_best_config.get("rankine_dc"),
        "kupfer_dc": global_best_config.get("kupfer_dc"),
        "kupfer_gov": global_best_config.get("kupfer_gov"),
        "layout": sr.pour_layout, "na_line": sr.na_line,
        "pour_line_in": sr.pour_line_in, "sheer_line_in": sr.sheer_line_in,
        # Multi-zone (I-beam) geometry — None on the 2-zone path
        "multizone": _is_mz,
        "zone_pour_lines": getattr(sr, "zone_pour_lines", None),
        "zone_densities": getattr(sr, "zone_densities", None),
        "pour_fracs": getattr(sr, "pour_fracs", None),
        "I_in4":      None if _is_mz else I_in4,
        "c_top":      None if _is_mz else c_top,
        "c_bot":      None if _is_mz else c_bot,
        "Q_in3":      None if _is_mz else global_best_config.get("Q_in3"),
        "t_in":       t_in,
        "best_mixes": list(best_mixes),
        "A_bot_ft2":  None if _is_mz else A_bot_ft2,
        "A_top_ft2":  None if _is_mz else A_top_ft2,
        "dx":         dx,
        "torsion_lateral": tl_result,
        # Full winning upper-section configuration — consumed by the
        # complete-canoe CAD export and the design-state persistence.
        "depth_in":        float(d_in),
        "gw_width_in":     float(global_best_config.get("gw_width_mid", 0.0)),
        "gw_height_in":    float(global_best_config.get("gw_height_mid", 0.0)),
        "gw_fillet_ratio": float(gw_fillet_ratio),
        "enable_mesh":     bool(global_best_config.get("enable_mesh", False)),
        "flare_req_deg":   float(global_best_config.get("flare_req_deg", 0.0)),
        # Gunwale/flare state computed by the sweep against the REAL lower-hull
        # section.  The UI reports these rather than recomputing — one source
        # of truth (see structural.freeboard_flare_gate).
        "flare_stress_psi": global_best_config.get("flare_stress_psi"),
        "flare_gov_station": global_best_config.get("flare_gov_station"),
        "freeboard_in":     global_best_config.get("freeboard_in"),
        "lower_hull":       global_best_config.get("lower_hull"),
        # Paddler seating actually used by the winning evaluation
        "paddler_positions": {
            **{name: [float(q) for q in global_best_config['seats'][name]]
               for name in config.RACE_CATEGORY_ORDER},
        },
        "position_variance_ft": float(global_best_config.get("pos_var_ft", 0.0)),
        "seating_mode": ("fixed" if global_best_config.get("seating_fixed")
                         else "optimized"),
        "weight_lbs":      float(sr.weight_lbs),
        "best_weight":     float(sr.weight_lbs),
        # Converged weight and the two raw values behind it.  The search-grid
        # weight stays authoritative for `weight_lbs` so nothing downstream
        # silently changes meaning; the report quotes `weight_converged` and
        # shows the correction alongside rather than burying it.
        "gate_records": [{k: v for k, v in r.items() if k != "_cfg"}
                         for r in _gate_records],
        "gate_notes": list(_gate_notes),
        "weight_converged": (None if _rw is None else _rw["w_converged"]),
        "weight_grid":      (None if _rw is None else
                             {"n": _rw["n"], "w_n": _rw["w_n"],
                              "n2": 2 * _rw["n"] - 1, "w_2n": _rw["w_2n"]}),
        "L_ft":            float(true_length_ft),
        "draft_in":        float(true_draft_in),
        # Strength-fit override provenance.  The service path stamps this too;
        # it has to be here as well or a headless run produces a result dict
        # that the report and design state read as "clean fit" when it is not.
        "fit_override":    _fit_override_record,
        # Transverse stability (task 7): hull GM_T at the design draft and the
        # Min-GMT it was checked against (0 = no gate), both inches.
        "gmt_in":          float(_gmt_in),
        "min_gmt_in":      float(_min_gmt_in),
        # The three GM_T conventions on the real pour + crew-CG sensitivity +
        # the top-flange stability/strength trade.  None when the winner is
        # single-zone or the hydrostatics were degenerate.
        "stability":       _stability,
        "stability_trade": _stab_trade,
        # Seat pinning: what the envelope covered per race, and what excluding
        # the other orderings cost.  Drives the BINDING compliance note.
        "arrangement_disclosure": _pin_disclosure,
        "seat_pin_cost":          _pin_cost,
        "seat_pins": {k: list(v["seat_pins"])
                      for k, v in config.RACE_CATEGORIES.items()},
        # Raw hull geometry for the in-GUI 3-D preview tab
        "hull_offsets_m": (_y_mat * 0.0254  if _y_mat  is not None else None),
        "hull_L_m":       (_L_m             if _x_raw  is not None else None),
        "hull_T_m":       (_T_m             if _x_raw  is not None else None),
    }

    # ── AUDIT ─────────────────────────────────────────────────────────────
    # Every non-GUI caller logs from HERE.  The audit file previously held GUI
    # runs only, because this path never called log_run at all -- so a sweep
    # scripted against this function (the way verification runs are) left no
    # trace, and the one number the log did carry could not be attributed to a
    # resolution or a density grid.  The GUI passes audit_log=False and writes
    # its own richer record from the widgets, so this never double-logs.
    if audit_log and _companion is None:
        _audit_run(_result, freeboard_min_in=freeboard_min_in,
                   precision_level=precision_level,
                   density_step_pcf=density_step_pcf,
                   num_zones=num_zones, multizone=multizone,
                   existing_only=_existing_only)
    # NOW the run is finished.  Nothing runs after this point, so this is
    # the only honest place for 1.0.  Companion re-entries (_companion is
    # not None) never get a progress_callback, so they cannot drive the
    # parent bar to Done from inside a nested sweep.
    announce_phase(progress_callback, SWEEP_PHASES[5][0],
                   "Run complete",
                   clock=_clock, phase="reports and wrap-up",
                   detail="search, verification and pricing all finished")
    # WHERE THE TIME WENT.  Guarded broadly and deliberately: this
    # prints at the very end of a run that may have taken hours, so an
    # exception here would discard work already completed -- the same
    # rule as the other broad catches in this file.  A timing table is
    # never worth a sweep.
    try:
        _clock.report(extra=[f"{configs_tested:,} configurations evaluated across "
                          f"{total_tasks} tasks"])
    except Exception as _clk_exc:            # noqa: BLE001
        print(f"  (phase timing unavailable: "
              f"{type(_clk_exc).__name__}: {_clk_exc})")
    return _result

if __name__ == "__main__":
    # Required before ANY process pool is created, and it must be the first
    # thing the entry point does.  A frozen (PyInstaller) build re-executes
    # this same executable for every spawned child; without this call the child
    # would re-run the whole application instead of the worker, forking GUIs
    # until the machine dies.  Harmless in the unfrozen interpreter.
    import multiprocessing
    multiprocessing.freeze_support()

    harden_stdio()

    # STARTUP SELF-CHECK.  Every data file this run will read, and where it
    # actually came from.
    #
    # Five shipped bugs were assets silently resolving to the wrong place or to
    # nothing: a defaulted config, an empty mix database, a generated default
    # hull, a 35-byte invented out.mlt, and reports written inside the bundle.
    # All five produced plausible numbers. Four survived months. Every one was
    # found by running the artifact, not the source.
    #
    # Printed unconditionally, before any work: the failure mode is output that
    # looks right, so there is no condition to trigger on.
    from asset_paths import print_asset_audit
    print_asset_audit()

    # `python main.py` with no arguments runs the 2D sweep, which has NO
    # refinement gate.  Refuse rather than hand a teammate an unverified
    # number from the obvious command: either ask for the gated path, or say
    # explicitly that an ungated one is what you want.
    import argparse
    _ap = argparse.ArgumentParser(
        description="ASCE canoe structural sweep.")
    _ap.add_argument("--multizone", action="store_true",
                     help="run the 3-zone I-beam sweep (REFINEMENT GATED)")
    _ap.add_argument("--zones", type=int, default=3,
                     help="zone count for --multizone (default 3)")
    _ap.add_argument("--ungated-ok", action="store_true",
                     help="run the 2-mix sweep anyway, accepting that its "
                          "winner is NOT refinement-verified")
    # ── Grid and machine controls, so the FROZEN binary can be driven
    # headlessly.  Until these existed the frozen build had no way to run a
    # sweep without the GUI, which is why "verify the process pool under
    # PyInstaller" stayed an open item -- the verification was unrunnable.
    _ap.add_argument("--stations", type=int, default=None,
                     help="longitudinal stations (default: config)")
    _ap.add_argument("--thickness", type=float, nargs="+", default=None,
                     help="wall thickness grid [in]")
    _ap.add_argument("--depth-step", type=float, default=None)
    _ap.add_argument("--depth-max", type=float, default=None)
    _ap.add_argument("--density-step", type=float, default=None)
    _ap.add_argument("--freeboard", type=float, default=6.0)
    _ap.add_argument("--executor", choices=("thread", "process"), default=None,
                     help="override SWEEP_EXECUTOR for this run")
    _ap.add_argument("--force-thread-pool-frozen", action="store_true",
                     help="force a FROZEN build back onto the thread pool. "
                          "The process pool is the default since Tier 1 "
                          "(6.52x, bit-identical winners); use this if a run "
                          "stalls and you want the slow path deliberately.")
    # Accepted and ignored: the flag that unlocked the pool before it was the
    # default. Kept so a saved command line or a teammate's note still runs.
    _ap.add_argument("--allow-process-pool-frozen", action="store_true",
                     help=argparse.SUPPRESS)
    _ap.add_argument("--seating-starts", type=int, default=None,
                     help="SEATING_N_STARTS override (a smaller value makes "
                          "the DESIGN meaningless; for timing work only)")
    _ap.add_argument("--price-mix-development", action="store_true",
                     help="price what developing each missing target mix "
                          "would save. SLOW: re-runs the whole search once "
                          "per target plus once for the mixes on record "
                          "(measured at 93%% of a run). Off by default.")
    _ap.add_argument("--json", default=None,
                     help="write the winner to this path as JSON, for "
                          "machine comparison between runs")
    _args = _ap.parse_args()

    if _args.stations is not None:
        config.LONGITUDINAL_STATIONS = _args.stations
    if _args.seating_starts is not None:
        config.SEATING_N_STARTS = _args.seating_starts
    if _args.executor is not None:
        config.SWEEP_EXECUTOR = _args.executor
    if _args.force_thread_pool_frozen:
        config.FORCE_THREAD_POOL_FROZEN = True

    _grid = dict(price_mix_development=_args.price_mix_development,
                 freeboard_min_in=_args.freeboard,
                 thickness_options_in=_args.thickness,
                 depth_step_in=_args.depth_step,
                 depth_max_in=_args.depth_max,
                 density_step_pcf=_args.density_step)

    def _emit(_res):
        """Winner -> JSON, for comparing two runs bit-for-bit."""
        if not _args.json:
            return
        import json as _json
        _x = None if not isinstance(_res, dict) else _res.get("x")
        _rec = {} if not isinstance(_res, dict) else {
            # repr() on the floats, not round(): this file exists to prove two
            # runs agree EXACTLY, and a formatted number cannot do that.
            "weight_lbs": repr(_res.get("weight_lbs")),
            "t_in": repr(_res.get("t_in")),
            "depth_in": repr(_res.get("depth_in")),
            "kupfer_dc": repr(_res.get("kupfer_dc")),
            "zone_densities": [repr(v) for v in
                               (_res.get("zone_densities") or [])],
            "pour_fracs": [repr(v) for v in (_res.get("pour_fracs") or [])],
            "n_stations": (len(_x) if _x is not None else None),
        }
        _rec["executor"] = resolve_sweep_executor()
        _rec["feasible"] = bool(_res)
        with open(_args.json, "w", encoding="utf-8") as _fh:
            _json.dump(_rec, _fh, indent=2, sort_keys=True)
        print(f"  winner written to {_args.json}")

    if _args.multizone:
        _emit(run_grand_optimization(multizone=True, num_zones=_args.zones,
                                     **_grid))
    elif _args.ungated_ok:
        _emit(run_grand_optimization(**_grid))
    else:
        print("=" * 72)
        print("  REFUSING TO RUN AN UNGATED SWEEP WITHOUT ACKNOWLEDGEMENT")
        print("=" * 72)
        print("  The default (2-mix) sweep has NO refinement gate: its winner")
        print("  is never re-evaluated at 2x/4x resolution, so the reported")
        print("  utilisation can understate the converged value.")
        print()
        print("  Choose one:")
        print("    python main.py --multizone        gated 3-zone sweep")
        print("    python main.py --ungated-ok       2-mix sweep, unverified")
        print("=" * 72)
        raise SystemExit(2)
