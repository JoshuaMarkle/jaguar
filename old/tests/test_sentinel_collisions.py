# tests/test_sentinel_collisions.py
"""
A SKIP CONDITION MUST NOT BE INDISTINGUISHABLE FROM A LEGITIMATE COMPUTED VALUE.

The instance that cost the most: GM_T. `_gmt_in` was initialised 0.0 and every
gate read `if _gmt_in > 1e-9`, so "no Min-GMT configured" and "stability could
not be computed" were the same state -- and the gate SKIPPED rather than
failed.  Beneath it, compute_hydrostatics guarded its fallbacks with
`if volume > 1e-12`, and `NaN > 1e-12` is False, so NaN geometry returned
GM_T = -draft/2: a plausible marginal-stability figure, not an error.

WHY THIS IS A TEST AND NOT A SCRIPT.  A one-off scan found `gate_pool` --
a collision introduced two hours after the same collision had been fixed in
its loud form, inside the gate whose whole purpose is refusing to report an
unverified winner.  The pattern is invisible while writing.  Mechanical
enforcement is the only thing that has worked on this codebase.

FOUR FAMILIES.  A/C/D are enforced.  B is REPORT-ONLY and that is a decision,
not an omission -- see test_family_b_is_deliberately_not_enforced.

TWO LISTS, kept apart on purpose:
  EXEMPT     reviewed, permanent, each with the reason it is safe
  KNOWN_DEBT sites agreed to be fixed later.  The test RATCHETS: it fails if a
             new site appears, and it fails if a listed site disappears
             without being removed from the list, so the list cannot rot into
             a permanent allow-list nobody reviews.
"""
from __future__ import annotations

import ast
import hashlib
import io
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "tests", ".venv", "venvs", "build", "dist",
             "node_modules"}
FALSY_LITERALS = (0, 0.0, "", None, False)

# ── reviewed exemptions ────────────────────────────────────────────────────
# KEY   (file, enclosing function, identifier) -- MOVES with the code, so
#       adding lines above a site does not break its exemption.
# SIGS  short hashes of the matched source line(s).  An EDITED site fails
#       until someone re-reviews it: an exemption is a review of specific
#       code, and edited code has not been reviewed.
#
# Line numbers were the first key and are neither: adding code above an entry
# slid the exemption onto a DIFFERENT, unreviewed site and PASSED.  Per-file
# counts were the second and fail the same way -- fix one site, add another,
# the count still matches and nobody looks.  Both are the defect this file
# exists to catch, committed by this file.
EXEMPT = {
    ("run_log.py", "start_capture", "return None"): (
        "SAME DIRECTION: None means 'capture did not start', and that is "
        "exactly what a failure means too -- no successful capture returns "
        "None. current_log_path() then returns None and the warning banner "
        "omits the log link, which is the correct rendering.",
        {"6b056bb6ca"}),
    ("mix_database.py", "_account_for_fit_eligibility", "return None"): (
        "FIXED, and kept listed so the fix cannot silently regress: the "
        "early return now sets fit_usable=False and appends a COULD NOT "
        "VERIFY warning BEFORE returning, so a failed check no longer "
        "inherits a True default that claimed the import was fine.",
        {"63143b6f80"}),
    ("hull_matcher.py", "evaluate_physical_hull", "total_S"): (
        "UNREACHABLE by construction: S_straight and S_turn are each "
        "np.clip(..., 0.01, 1.0), so total_S >= 0.02 and the `< 1e-6` branch "
        "can never be entered. Accumulation cannot reach the floor, so the "
        "floor and a real value are never confusable.",
        {"8276b63c50"}),
    ("main.py", "run_grand_optimization", "_gmt_in"): (
        "FIXED: a separate `_gmt_error` flag distinguishes 'no Min-GMT "
        "configured' from 'stability could not be computed', and the latter "
        "FAILS the gate instead of skipping it. 0.0 remains only as the "
        "not-yet-computed initial value.",
        {"86245de44d", "de7491eacc", "fbb8baca06"}),
    # Moved from run_grand_optimization into the _run_pool_phase closure
    # when the pool phase was made re-runnable for the stall watchdog.
    # SAME CODE, new owner -- the signature below is unchanged, which is
    # how that was confirmed rather than assumed.
    ("main.py", "_run_pool_phase", "gate_pool"): (
        "FIXED: `gate_pool_fed` distinguishes 'no survivors' from 'the pool "
        "was never fed'; the latter raises as a wiring fault, so the len() "
        "gate is only reached with the two states already separated.",
        {"12c0fc41df"}),
    ("main.py", "_evaluate_geometry_worker", "_cutoff"): (
        "an empty cutoff list legitimately means 'no cutoff applies'; no "
        "failure path empties it.",
        {"408a6085af", "f5cd5dd2dd"}),
    ("reporting/latex_report.py", "_solid_keel_audit", "nsolid"): (
        "a genuine count; zero solid mixes really is zero, and no failure "
        "path produces the value.",
        {"519f7e8c5b"}),
    ("visualization.py", "plot_structural_envelope", "min_y"): (
        "a PLOT BOUND, cosmetic TODAY. CONDITIONAL exemption: if a plot bound "
        "is ever read back as data rather than used to set an axis, it stops "
        "being cosmetic and this entry is wrong.",
        {"345e87a35c"}),
    ("ui/structural_tab.py", "_run_structural_sweep_core", "pos_variance"): (
        "0.0 is the genuine configured default AND no failure produces it: a "
        "bad parse raises into the enclosing handler, which prints a "
        "traceback and ABANDONS the operation rather than continuing with the "
        "initial value.",
        {"cb933f9421"}),
    ("mix_database.py", "recommend_mix_targets", "groups"): (
        "empty legitimately means 'nothing grouped'; no failure path empties "
        "it.",
        {"bcc5740e7c"}),
    ("services/update_check.py", "_parse_semver", "result"): (
        "empty legitimately means 'not a semver string'; the caller treats "
        "absence as 'no update', which is the safe direction.",
        {"4b71bf01ca"}),
    ("ui/mix_tab.py", "_delete_selected", "ids"): (
        "empty legitimately means 'nothing selected'; deleting nothing is "
        "the correct response.",
        {"3225ffbae3"}),
    ("geometry.py", "parse_godzilla_offsets", "return (None, None, None)"): (
        "SAME DIRECTION: there is no successful parse that returns None, so "
        "the failure value and a legitimate value never collide -- None means "
        "'could not parse' and nothing else. The handler also logs at ERROR "
        "with the full parameter set.",
        {"77efe6ae28"}),
    ("services/hull_service.py", "launch_godzilla", "return False"): (
        "SAME DIRECTION: False is returned both when Godzilla exits non-zero "
        "and when it raises, and both mean 'it did not run'. The caller "
        "cannot and need not act differently on the two.",
        {"f7781144ea"}),
}

# ── ACCEPTED: decided, risk knowingly taken, NOT debt ──────────────────────
# Debt means UNDECIDED.  These four were reviewed and accepted because they
# degrade OUTPUT (a report section, a validation run) rather than a design
# number.  Keeping them in KNOWN_DEBT would peg the debt ratchet at 4 forever,
# and a ratchet that can never reach zero stops carrying information: a
# non-zero debt count must always mean "something nobody has looked at".
ACCEPTED = {
    ("main.py", "_seat_pin_cost", "return None"): (
        "ACCEPTED: a failed seat-pinning cost reads as 'no pinning cost'. It "
        "understates a REPORT figure, not a design number, and the sweep does "
        "not gate on it.",
        {"6b056bb6ca"}),
    ("main.py", "_top_flange_stability_trade", "return None"): (
        "ACCEPTED: a failed trade study omits a report SECTION. Nothing "
        "downstream consumes it, and the absence is visible in the report "
        "itself.",
        {"6b056bb6ca"}),
    ("validation_suite.py", "parse_godzilla_drag_output", "return None"): (
        "ACCEPTED: validation tooling, off the design path. A parse failure "
        "shows up as a validation run with no data, which a human is reading "
        "anyway.",
        {"6b056bb6ca"}),
    ("validation_suite.py", "run_godzilla_engine", "return None"): (
        "ACCEPTED: validation tooling, off the design path. An engine crash "
        "shows up as a run that produced nothing, in front of a human.",
        {"6b056bb6ca"}),
}

# ── agreed debt: real collisions, fix scheduled, ratcheted ─────────────────
KNOWN_DEBT = {
}

# Family C in ui/ is exempt wholesale: returning None from a failed repaint is
# correct, and a wrong pixel is not a wrong number.
EXEMPT_C_PREFIXES = ("ui/",)


def _py_files(root=None):
    root = root or REPO
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(dirpath, f)
                yield os.path.relpath(p, root).replace("\\", "/")


def _const(n):
    return isinstance(n, ast.Constant)


def _falsy_empty(n):
    if isinstance(n, (ast.List, ast.Dict, ast.Set, ast.Tuple)):
        return not getattr(n, "elts", None) and not getattr(n, "keys", None)
    return _const(n) and n.value in FALSY_LITERALS


def _fn_index(tree):
    """line -> innermost enclosing function name (or '<module>')."""
    spans = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((n.lineno, n.end_lineno or n.lineno, n.name))
    spans.sort(key=lambda t: (t[1] - t[0]))        # innermost first
    def lookup(line):
        for lo, hi, nm in spans:
            if lo <= line <= hi:
                return nm
        return "<module>"
    return lookup


def _sig(lines, lineno):
    """Short hash of the matched source line, whitespace-normalised.

    IDENTITY vs CONTENT, kept separate on purpose.  The key
    (file, function, identifier) MOVES with the code, so adding lines above a
    site does not break its exemption.  This signature is stored alongside it
    and fails when the site is EDITED -- an exemption is a review of specific
    code, and edited code has not been reviewed.

    Line numbers were the original key.  They are neither: adding code above
    an entry slid the exemption onto a DIFFERENT, unreviewed site, which is
    the same defect this file exists to catch.  Per-file COUNTS fail the same
    way -- fix one site, add another, the count matches and nobody looks.
    """
    try:
        raw = lines[lineno - 1]
    except IndexError:
        raw = ""
    norm = " ".join(raw.split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def _walk_scope(scope):
    """
    Walk `scope` WITHOUT descending into nested function bodies.

    ast.walk() on a Module descends into every nested def, so a module-level
    pass saw `S = 0.0` in wetted_surface() and matched it against `if S < 1e-6`
    in viscous_form_factor(), where S is a PARAMETER -- two different names in
    two different scopes reported as one collision.  A scanner that invents
    findings is worse than one that misses them: it spends the reviewer's
    attention and teaches them to distrust it.
    """
    NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    stack = list(getattr(scope, "body", []))
    while stack:
        n = stack.pop()
        yield n
        # Do not descend INTO a nested scope.  The first version of this only
        # declined to PUSH nested defs, then walked the children of one it had
        # already popped -- so the leak survived the fix and the false positive
        # was still there.  The test below plants exactly that case.
        if isinstance(n, NESTED):
            continue
        stack.extend(ast.iter_child_nodes(n))


def scan(root=None):
    """Findings as (rel, family, func, ident, sig)."""
    root = root or REPO
    A, B, C, D = [], [], [], []
    for rel in _py_files(root):
        try:
            src = io.open(os.path.join(root, rel), encoding="utf-8-sig").read()
            tree = ast.parse(src)
        except Exception:
            continue
        lines = src.split("\n")
        fn_at = _fn_index(tree)
        for scope in [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.Module))]:
            init_num, init_falsy = {}, {}
            for n in _walk_scope(scope):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                        and isinstance(n.targets[0], ast.Name):
                    nm = n.targets[0].id
                    if _const(n.value) and isinstance(n.value.value, (int, float)) \
                            and not isinstance(n.value.value, bool):
                        init_num.setdefault(nm, n.value.value)
                    if _falsy_empty(n.value):
                        init_falsy.setdefault(nm, n.lineno)
            for n in _walk_scope(scope):
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) \
                        and n.left.id in init_num and len(n.ops) == 1 \
                        and _const(n.comparators[0]) \
                        and isinstance(n.comparators[0].value, (int, float)):
                    iv, cv = init_num[n.left.id], n.comparators[0].value
                    if abs(cv - iv) <= max(1e-6, abs(iv) * 1e-6):
                        A.append((rel, fn_at(n.lineno), n.left.id,
                                  _sig(lines, n.lineno)))
                if isinstance(n, ast.If) and isinstance(n.test, ast.Name) \
                        and n.test.id in init_falsy:
                    B.append((rel, fn_at(n.lineno), n.test.id,
                              _sig(lines, n.lineno)))
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Call) \
                        and isinstance(n.left.func, ast.Name) \
                        and n.left.func.id == "len" and n.left.args \
                        and isinstance(n.left.args[0], ast.Name) \
                        and n.left.args[0].id in init_falsy:
                    D.append((rel, fn_at(n.lineno), n.left.args[0].id,
                              _sig(lines, n.lineno)))
        for n in ast.walk(tree):
            if not isinstance(n, ast.ExceptHandler):
                continue
            t = n.type
            if not ((t is None) or (isinstance(t, ast.Name)
                                    and t.id in ("Exception", "BaseException"))):
                continue
            for r in [x for x in ast.walk(n) if isinstance(x, ast.Return)]:
                v = r.value
                if v is None or _falsy_empty(v) or (
                        isinstance(v, ast.Tuple) and v.elts
                        and all(_falsy_empty(e) for e in v.elts)):
                    C.append((rel, fn_at(r.lineno),
                              'return ' + (ast.unparse(v) if v is not None else 'None'),
                              _sig(lines, r.lineno)))
    return sorted(set(A)), sorted(set(B)), sorted(set(C)), sorted(set(D))


# ── the positive control ───────────────────────────────────────────────────

PLANTED = (
    "def f(a):\n"
    "    margin = 0.0\n"
    "    items = []\n"
    "    if margin > 1e-9:\n"
    "        return 1\n"
    "    if len(items) > 0:\n"
    "        return 2\n"
    "    try:\n"
    "        return int(a)\n"
    "    except Exception:\n"
    "        return None\n"
)

CLEAN = (
    "def f(x):\n"
    "    total = 0.0\n"
    "    for v in x:\n"
    "        total += v\n"
    "    return total\n"
)

MOVABLE = (
    "def keep(a):\n"
    "    margin = 0.0\n"
    "    if margin > 1e-9:\n"
    "        return 1\n"
)


def test_the_scan_can_still_fail(tmp_path):
    """
    POSITIVE CONTROL.  The repo's slice scanner spent an entire refactor unable
    to fail, and nobody noticed because a passing scan and a broken scan look
    identical.  Plant one collision of each enforced family and assert the scan
    reports it.
    """
    (tmp_path / "planted.py").write_text(PLANTED, encoding="utf-8")
    A, _B, C, D = scan(str(tmp_path))
    assert any(f == "planted.py" and i == "margin" for f, _fn, i, _s in A), \
        "family A scan is not firing"
    assert any(f == "planted.py" and i == "items" for f, _fn, i, _s in D), \
        "family D scan is not firing"
    assert any(f == "planted.py" for f, _fn, _i, _s in C), \
        "family C scan is not firing"


def test_the_scan_is_quiet_on_clean_code(tmp_path):
    """The other half of the control: it must not fire on everything."""
    (tmp_path / "clean.py").write_text(CLEAN, encoding="utf-8")
    A, _B, C, D = scan(str(tmp_path))
    assert not A and not C and not D, f"false positives: {A} {C} {D}"


def test_the_key_survives_a_move_but_not_an_edit(tmp_path):
    """
    THE POINT OF THE KEY, pinned.

    Line numbers were the first key: adding code above an entry slid its
    exemption onto a DIFFERENT site and PASSED.  Per-file counts were the
    second: fix one site, add another, the count still matches and nobody
    looks.  Both granted an exemption to code nobody had reviewed, which is
    the defect this whole file exists to catch.

    (file, function, identifier) MOVES with the code; the signature changes
    when the line is EDITED.  So a relocated site keeps its review and an
    altered one loses it.
    """
    (tmp_path / "m.py").write_text(MOVABLE, encoding="utf-8")
    A0, _b, _c, _d = scan(str(tmp_path))
    assert len(A0) == 1
    key0, sig0 = A0[0][:3], A0[0][3]

    # MOVED: ten lines added above, same code
    (tmp_path / "m.py").write_text("# pad\n" * 10 + MOVABLE, encoding="utf-8")
    A1, _b, _c, _d = scan(str(tmp_path))
    assert A1[0][:3] == key0, "the key did not survive a move"
    assert A1[0][3] == sig0, "the signature changed on a pure move"

    # EDITED: same site, different threshold -> must lose its exemption
    (tmp_path / "m.py").write_text(MOVABLE.replace("1e-9", "1e-12"),
                                   encoding="utf-8")
    A2, _b, _c, _d = scan(str(tmp_path))
    assert A2[0][:3] == key0, "the key should still identify the site"
    assert A2[0][3] != sig0, (
        "an EDITED site kept its signature; it would silently keep an "
        "exemption granted to different code")


# ── enforcement ────────────────────────────────────────────────────────────

def _review(finding, family):
    """'exempt' | 'debt' | 'edited' | 'new' for one finding."""
    rel, fn, ident, sig = finding
    if family == "C" and rel.startswith(EXEMPT_C_PREFIXES):
        return "exempt"
    for table, status in ((EXEMPT, "exempt"), (ACCEPTED, "accepted"),
                          (KNOWN_DEBT, "debt")):
        got = table.get((rel, fn, ident))
        if got is not None:
            _reason, sigs = got
            return status if sig in sigs else "edited"
    return "new"


def test_no_new_or_edited_sentinel_collisions():
    """
    RATCHET.  Every A/C/D finding must be reviewed -- permanently EXEMPT with
    a stated reason, or on KNOWN_DEBT with a fix scheduled -- and its source
    line must be unchanged since that review.
    """
    A, _B, C, D = scan()
    bad = []
    for fam, findings in (("A", A), ("C", C), ("D", D)):
        for f in findings:
            st = _review(f, fam)
            if st in ("new", "edited"):
                bad.append((st, fam) + f)
    assert not bad, (
        "unreviewed or edited sentinel collisions:\n  "
        + "\n  ".join("%-6s %s  %s :: %s :: %s  sig=%s" % b for b in bad)
        + "\n\nA skip condition indistinguishable from a legitimate computed "
          "value. Fix it, or add it to EXEMPT with the reason it is safe, or "
          "to KNOWN_DEBT with the fix scheduled. If it is already listed, the "
          "line was EDITED: re-review it and update the signature.")


def test_the_debt_list_cannot_rot():
    """
    The other direction: a listed entry that no longer appears has been fixed
    and must be REMOVED, or the list silently becomes a permanent allow-list
    nobody re-reads.
    """
    A, _B, C, D = scan()
    live = {f[:3] for f in (A + C + D)}
    stale = sorted(k for k in list(KNOWN_DEBT) + list(EXEMPT) + list(ACCEPTED)
                   if k not in live)
    assert not stale, (
        "these entries no longer exist (fixed?) -- remove them:\n  "
        + "\n  ".join("%s :: %s :: %s" % k for k in stale))


def test_every_entry_states_a_reason_and_a_signature():
    for key, (reason, sigs) in (list(EXEMPT.items()) + list(ACCEPTED.items())
                                + list(KNOWN_DEBT.items())):
        assert isinstance(reason, str) and len(reason) > 40, \
            f"{key} needs a real reason, not a placeholder"
        assert sigs and all(isinstance(x, str) and len(x) == 10 for x in sigs), \
            f"{key} needs at least one 10-char source signature"


def test_family_b_is_deliberately_not_enforced():
    """
    B (bare truthiness on an empty init) is REPORT-ONLY, and that is a decision
    on the record rather than an omission.

    On inspection these are accumulators where empty legitimately means empty
    -- `errors`, `added`, `promoted`, `in_list`.  Enforcing it would mean a
    long list of exemptions nobody would read, which is the failure mode this
    file exists to prevent.  If a B-family site is ever found to hide a
    failure, promote that ONE site to KNOWN_DEBT rather than enforcing the
    family.
    """
    _A, B, _C, _D = scan()
    assert len(B) > 15, (
        "the B family has shrunk a lot; re-check whether it is now small "
        "enough to enforce")


def test_the_scan_does_not_leak_names_across_functions(tmp_path):
    """
    REGRESSION.  ast.walk() on a Module descends into every nested def, so the
    first version matched `S = 0.0` in one function against `if S < 1e-6` in
    ANOTHER, where S was a parameter.  Two of the ten sites on the debt list
    were this artifact -- and both were in the structural path, i.e. exactly
    the ones a reviewer would have spent attention on first.

    A scanner that invents findings is worse than one that misses them: it
    spends the reviewer's credibility and teaches them to skim.

    The first fix declined to PUSH nested defs but still walked the children
    of one it had already popped, so the leak survived it.  This plants that
    exact shape.
    """
    leak = (
        "def producer():\n"
        "    S = 0.0\n"
        "    return S\n"
        "\n"
        "def consumer(S):\n"
        "    if S > 0.0:\n"
        "        return 1\n"
        "    return 0\n"
    )
    (tmp_path / "leak.py").write_text(leak, encoding="utf-8")
    A, _B, _C, _D = scan(str(tmp_path))
    assert not A, (
        f"names leaked across function scopes: {A}. `S` is assigned in "
        "producer() and compared in consumer(), where it is a parameter -- "
        "two different names in two different scopes.")
