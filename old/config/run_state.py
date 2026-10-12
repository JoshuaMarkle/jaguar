# config/run_state.py
"""
The per-run configuration, as a PICKLABLE object.

WHY THIS EXISTS.  Sweep settings are written onto the module-level ``config``
singleton at run time (``StructuralService._apply_run_config``).  That works
in-process and fails silently across a process boundary: Windows spawns
workers, each child re-imports ``config`` fresh, and a worker then sees the
canoe_suite.cfg DEFAULTS rather than the run's values.

Two failure shapes, and the second is the dangerous one:

  * attributes only ever assigned at run time (LONGITUDINAL_STATIONS) do not
    exist in the child at all -> AttributeError, loud, harmless;
  * attributes that DO have an import-time default (MAX_DENSITY_PCF,
    FACTOR_OF_SAFETY, RACE_CATEGORIES, DESIGN_METHOD, the gate limits) resolve
    to the file's value -> the worker evaluates a DIFFERENT DESIGN and returns
    a perfectly plausible number.

That second shape is the commit-A crew-alias split-brain, reproduced across
processes.  It is why every RunState carries a FINGERPRINT and the worker
asserts against it before doing any work.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict

#: THE canonical list of run-level config attributes.
#:
#: It lives here, in production, and tests/conftest.py imports it -- it was
#: originally written there for the same isolation problem (stopping one test's
#: sweep settings leaking into the next), and a second copy would be the
#: two-homes-for-one-fact pattern this project keeps removing.
RUN_LEVEL_CONFIG_ATTRS = (
    "FACTOR_OF_SAFETY", "DYNAMIC_LOAD_FACTOR", "KUPFER_ACCEPTANCE_MAX",
    "DESIGN_METHOD", "GAMMA_DEAD", "GAMMA_LIVE", "LOAD_FACTORS_BY_CASE",
    "LRFD_COUNTERACTING_DEAD", "GAMMA_DEAD_COUNTERACTING",
    "PHI_RESISTANCE", "OMEGA_SAFETY",
    "MAX_DENSITY_PCF", "MIN_DENSITY_PCF",
    "MAX_MIXES", "N_MIXES_MAX", "EXISTING_MIXES_ONLY",
    # Crew composition: the CATEGORIES are the single source of truth.
    # RACE_4/2/3_PERSON are DERIVED on read and must never appear here --
    # capturing them would store a snapshot that then shadows the derivation.
    "RACE_CATEGORIES",
    # BOTH names, not just the alias.  run_grand_optimization publishes
    # STEM_OVERHANG_IN and then STRUCT_ROCKER_IN as a legacy alias of it, and
    # _struct_rocker_ft() reads whichever it finds first.  Only the alias was
    # listed, so the PRIMARY leaked: after any test that ran a sweep it stayed
    # set on the config module for the rest of the session, and later tests
    # could call an evaluator directly and pass on borrowed state.  Four tests
    # across two files were doing exactly that.
    "STEM_OVERHANG_IN",
    # The PER-THICKNESS map, which is what production actually reads now.
    # Omitting it would leak exactly the way STEM_OVERHANG_IN did -- worse,
    # a spawned worker would find no map, fall back to the scalar, and
    # silently reinstate the sweep-wide overhang this map exists to remove.
    # It must also be in the fingerprint: two sweeps whose grids differ in
    # thickness are NOT the same configuration, and the map is where that
    # difference now lives.
    "STEM_OVERHANG_BY_T",
    # THE SEATING SOLVE'S OWN KNOBS.  `seating_start_points` reads both
    # through `getattr(config, ...)` AT CALL TIME, and that call happens
    # INSIDE THE WORKER.  A spawned process re-imports config fresh and
    # sees only what RunState carries, so omitting these meant a
    # configured value took effect under threads and was silently ignored
    # under processes -- the executor changing a result, which
    # `resolve_sweep_executor` documents as the one thing it must never
    # do.  Caught by a thread-vs-process benchmark whose two legs
    # evaluated 7,712 and 7,562 configurations on an identical grid.
    #
    # The seed especially: it exists so a sweep is reproducible, and a
    # reproducibility control that does not reach the workers is the
    # same defect class as the default-argument bug already fixed in
    # `seating_start_points` -- a tunable that reports success without
    # tuning anything.
    "SEATING_N_STARTS", "SEATING_START_SEED",
    # Read by `gate_top_m_for_task`, which also runs worker-side: it sets
    # how many candidates each task offers the refinement gate, so a value
    # that reached threads but not processes would change the GATE POOL
    # and therefore what gets refined.  Found by the same scan.
    "GATE_TOP_M",
    "FIXED_PADDLER_POSITIONS", "PADDLER_POS_VARIANCE_FT",
    "ADVANCED_PADDLER_LOADING", "PADDLER_LOAD_ENVELOPE",
    "MANEUVER_SPEED_MPS", "MANEUVER_TURN_RADIUS_M", "MANEUVER_COUPLING_K",
    "LONGITUDINAL_STATIONS", "N_CONSTRUCTION_LAYERS",
    "BOW_STERN_ROCKER_IN", "STRUCT_ROCKER_IN", "MIN_GMT_IN",
)

#: Attributes that are CALLABLES.  ``config.get_mix_properties`` is a module
#: function -- picklable by reference in production, but NOT when a test
#: monkeypatches it with a lambda or a closure.  It is therefore never shipped:
#: the child re-imports ``config`` and rebinds the module's own function, which
#: is the same object production would have used.  A test that patches it is
#: in-process anyway, where the singleton already carries the patch.
_CALLABLE_ATTRS = ("get_mix_properties",)


class _Absent(enum.Enum):
    """Sentinel for "this attribute does not exist on config".

    IT HAS TO BE AN ENUM.  This marker is captured in the parent and read in a
    SPAWNED WORKER, so it crosses a pickle boundary, and ``apply()`` tests it
    with ``is``.  A plain string sentinel does not survive that: pickle
    reconstructs an equal-but-distinct object, ``v is _MISSING`` is False in
    the child, and the absent attribute gets SET to the sentinel text instead
    of being deleted.  That is not a loud failure — the worker then carried
    ``config.LONGITUDINAL_STATIONS = '\\x00<absent>\\x00'`` into
    parse_godzilla_offsets, whose ``int(n_stations)`` raised into a blanket
    except, and every configuration in the sweep was skipped as "offsets could
    not be parsed".  Enum members pickle BY REFERENCE, so identity holds.
    """
    TOKEN = "absent"


_MISSING = _Absent.TOKEN


def _stable(value) -> str:
    """Order-independent, float-exact repr for fingerprinting."""
    import numpy as _np
    if isinstance(value, dict):
        return "{" + ",".join(f"{k!r}:{_stable(value[k])}"
                              for k in sorted(value, key=repr)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable(v) for v in value) + "]"
    if isinstance(value, _np.ndarray):
        return _stable(value.tolist())
    if isinstance(value, float):
        return float(value).hex()          # exact, no formatting loss
    return repr(value)


@dataclass
class RunState:
    """One sweep's config, capturable in the parent and appliable in a worker."""

    values: Dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    # ── capture ──────────────────────────────────────────────────────────
    @classmethod
    def capture(cls) -> "RunState":
        import config
        vals = {}
        for a in RUN_LEVEL_CONFIG_ATTRS:
            if a in _CALLABLE_ATTRS:
                continue                    # rebound in the child, never shipped
            vals[a] = getattr(config, a, _MISSING)
        st = cls(values=vals)
        st.fingerprint = st.compute_fingerprint()
        return st

    @classmethod
    def capture_for_run(cls) -> "RunState":
        """``capture()`` plus stamping THIS process as already on that state."""
        st = cls.capture()
        _mark_active(st.fingerprint)
        return st

    def _payload(self) -> str:
        """The exact string the fingerprint hashes. ONE definition, so a
        diagnostic can never describe something the hash did not see."""
        return ";".join(f"{k}={_stable(self.values[k])}"
                        for k in sorted(self.values))

    def compute_fingerprint(self) -> str:
        return hashlib.sha256(self._payload().encode("utf-8")).hexdigest()[:32]

    # ── apply ────────────────────────────────────────────────────────────
    def apply(self) -> None:
        """
        Push this state onto the worker's ``config`` singleton, then VERIFY.

        Verification is not optional.  A worker that silently ran on file
        defaults would return a plausible weight for a design nobody asked for,
        and nothing downstream could tell.
        """
        import config
        for a, v in self.values.items():
            if v is _MISSING:
                if hasattr(config, a):
                    try:
                        delattr(config, a)
                    except AttributeError:
                        pass
                continue
            if a == "RACE_CATEGORIES":
                # Route through the setter so the DERIVED per-size aliases
                # follow.  A bare setattr would leave them stale.
                config.set_race_categories(v)
            else:
                setattr(config, a, v)
        config._mix_db = None               # re-init so the new FoS takes effect
        self.verify()

    def verify(self) -> None:
        """
        Confirm this process's config really is the state the parent intended.

        THE MESSAGE MATTERS AS MUCH AS THE CHECK.  This used to diff only the
        keys present in ``self.values``, so a KEY-SET difference -- the state
        carrying a name the fresh capture does not, or vice versa -- reported
        as ``Differing: []``: a failure that says something is wrong and then
        names nothing.  That is the same defect this suite keeps finding in
        other forms, and it cost a debugging session here.

        It now compares the fingerprint PAYLOADS, which is what the hash is
        actually computed from, so anything that can move the hash can be
        named: differing values, keys on one side only, and the payloads
        themselves when neither explains it.
        """
        got = RunState.capture()
        if got.fingerprint == self.fingerprint:
            return

        mine, theirs = set(self.values), set(got.values)
        only_intended = sorted(mine - theirs)
        only_actual = sorted(theirs - mine)
        differing = [k for k in sorted(mine & theirs)
                     if _stable(self.values[k]) != _stable(got.values[k])]

        lines = ["RunState verification FAILED in a worker: the applied config "
                 "does not match what the parent intended."]
        if differing:
            lines.append("  Differing values:")
            for k in differing:
                lines.append(f"    {k}:")
                lines.append(f"      intended {_stable(self.values[k])[:160]}")
                lines.append(f"      actual   {_stable(got.values[k])[:160]}")
        if only_intended:
            lines.append(f"  Present in the parent's state but NOT in the "
                         f"worker's capture: {only_intended}")
        if only_actual:
            lines.append(f"  Present in the worker's capture but NOT in the "
                         f"parent's state: {only_actual}")
        if not (differing or only_intended or only_actual):
            # The hash moved with every key and value equal, which means
            # _stable() is not deterministic across processes for something in
            # here -- a set or another unordered container reaching its repr()
            # fallback, where Python's per-process string hash randomisation
            # makes the order differ.  Say so, and show the payloads.
            lines.append("  NO key or value differs, yet the fingerprint moved."
                         " _stable() is non-deterministic across processes for"
                         " one of these values -- look for a set or other"
                         " unordered container falling through to repr().")
            lines.append(f"    intended payload: {self._payload()[:400]}")
            lines.append(f"    actual   payload: {got._payload()[:400]}")
        lines.append("  This is the split-brain failure -- the worker would "
                     "have evaluated a different design and returned a "
                     "plausible number. Refusing to continue.")
        raise RuntimeError(chr(10).join(lines))


# ── Idempotence across executor kinds ─────────────────────────────────────────
# The SAME task tuple is handed to a thread pool or a process pool.  In a
# thread the config singleton is ALREADY the parent's, so re-applying is at
# best redundant and at worst a race (``config._mix_db = None`` while a sibling
# thread is mid-lookup).  In a spawned process it is mandatory.  One flag
# distinguishes them without any is-this-a-child guesswork: the parent stamps
# it at capture time, a fresh child starts blank.
_ACTIVE_FINGERPRINT = None


def _mark_active(fp):
    global _ACTIVE_FINGERPRINT
    _ACTIVE_FINGERPRINT = fp


def ensure_applied(state):
    """
    Make this process's config match ``state``, then verify.

    Cheap and safe to call at the top of every task in either executor:
    applies only when the process is not already on this exact state, but
    VERIFIES every time -- so a config mutated mid-sweep is caught too.
    """
    if state is None:
        return
    if _ACTIVE_FINGERPRINT != state.fingerprint:
        state.apply()                    # applies, verifies, and stamps
        _mark_active(state.fingerprint)
    else:
        state.verify()
