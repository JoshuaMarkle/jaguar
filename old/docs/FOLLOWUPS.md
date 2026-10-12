# Follow-ups

Known work deferred deliberately, with enough context to pick it up cold.
Newest first. Close an item by deleting it and noting the commit.

---

## 6. PROCESS POOL — spec for the refactor. Start here, do not re-derive.

**Filed** 2026-08-11 · **Status: specified, not started.**

### Why

Threads give **1.93x on 22 logical / 16 physical cores** (~9% utilisation;
matches the observed 18% CPU). Processes plausibly give 8-10x. That is a
6-minute multizone run becoming under a minute -- the affordability threshold
that makes the difference between running multizone and defaulting to
single-mix, which costs **15-25 lb**.

### What is already measured (do not re-measure)

| question | answer |
|---|---|
| Does the sweep leak? | No. **114 MB peak**, flat over 1h43m / 11,367 samples |
| Does memory veto processes? | No. 114 MB x 16 workers = ~1.8 GB of 33.8 GB |
| Does pickling veto it? | No. **0.2 KB/task, 0.003 ms** round-trip |
| Do more threads help? | No. 8 -> 22 workers is 42.0 s -> 40.7 s (**3%**) |
| Can processes spawn from a DAEMON thread? | **YES** on Python 3.12.6 -- the comment at `main.py` ~1595 saying otherwise is stale and has been gating the design |

The 27.5 GB the user observed in Task Manager is **not the sweep** (114 MB).
It is the PyQt6 GUI process or something outside the app; unmeasured, separate
investigation.

### The two real blockers

**1. Run config does not cross the process boundary.** Windows uses `spawn`;
the child re-imports `config` fresh. Measured failure:

```
AttributeError: module 'config' has no attribute 'LONGITUDINAL_STATIONS'
```

That one crashes loudly because it is only ever assigned at runtime. **The
dangerous ones are the settings that DO have import-time defaults** --
`MAX_DENSITY_PCF`, `FACTOR_OF_SAFETY`, `RACE_CATEGORIES`, `DESIGN_METHOD`,
gate limits. A worker would silently use the FILE defaults, evaluate a
different design, and return a plausible number. That is the commit-A
split-brain across a process boundary.

**2. `stop_event` is a `threading.Event`** -- not picklable. Needs a
`multiprocessing.Event` or manager proxy, or the Stop button stops nothing.

### The plan

1. **`RunState`, a picklable dataclass**, capturing exactly the attributes
   `tests/conftest.py::_RUN_LEVEL_CONFIG_ATTRS` enumerates. That list is the
   canonical set -- it was written for this same isolation problem and is kept
   correct by an existing assertion. Do not invent a second list.
   Note `get_mix_properties` is in it and is a FUNCTION: picklable by reference
   in production, but not if a test monkeypatches it with a lambda. Handle
   explicitly.
2. `RunState.capture()` in the parent; pass as ONE element of the task args
   tuple; `RunState.apply()` at the top of `_evaluate_geometry_worker`.
3. **Worker-side verification**: the parent computes a fingerprint of the state
   it intended; the worker asserts what it applied matches. Silent divergence is
   the whole risk -- the guard must be mechanical, like the cross-basis and
   `as_optimizer_penalty` guards.
4. `stop_event` -> `multiprocessing.Event`.
5. `multiprocessing.freeze_support()` at the `app.py` entry point. **PyInstaller
   REQUIRES this on Windows or the frozen app spawns copies of itself
   indefinitely.**
6. Executor selectable (thread vs process) so a regression can be bisected.

### Acceptance

- **Winner bit-identical**, thread vs process, on a grid where the current code
  finds one (167.51 lb, D/C 0.928 at 41 stations, depth 1.5/6.0, density step
  2.0 is a usable reference).
- Speedup reported against the 8-worker thread baseline (42.0 s on 8 tasks).
- The suite green, including `test_seating_starts` and
  `test_load_case_expansion`.

### The one thing still untested

All spawn testing here used the **unfrozen** interpreter. Under PyInstaller,
`spawn` re-executes the bundled exe and the stale comment may become true again.
Test against a real frozen build before shipping, or keep the thread path
selectable as a fallback.

---

## 5. THE B&B BOUNDS — measured. The tighter bound is UNSAFE. Do not retry it as written.

**Filed** 2026-08-09 · **Status: closed.** Motivated by a real cost: running
single-mix because multizone is slow gives up 15-25 lb, so multizone speed is a
WEIGHT problem, not a convenience one.

### The two paths prune differently, and both are correct

| path | prunes against | measured |
|---|---|---|
| multi-zone (`main.py` ~887) | `best_mz[0]`, the incumbent best | **21.8%** of 3,157 configs |
| single-zone (`main.py` ~1051) | `-_cutoff[0]`, the **25th**-lightest | **0.6%** of 308 |
| single-mix | same, but grid is 52 | **0%** |

Single-zone's weak pruning is NOT a regression.  Its flare gate walks the
`_GATE_ATTEMPTS = 25` lightest feasible combos lightest-first because the
lightest can fail the flare check, so the 25-set must survive.  Pruning to the
incumbent would discard candidates the gate needs.

**Single-mix prunes 0% because there is nothing to prune** -- 52 configs, 15.5 s
total.  A "2% vs the historical 34-96%" comparison is apples to oranges: those
figures came from the multi-zone path on grids ~60x larger.  Improving
single-mix pruning would save nothing.

**21.8% is normal for this bound**, not a deficiency.  An independent 66-combo
walk measured 15.2% with the same bound.  The 34-96% history most likely came
from wider density grids where more of the space is trivially hopeless.

### Multizone is not slower per config -- it has more configs

Same grid, same fixed seating, 41 stations:

| mode | grid | evaluated | pruned | wall | **per config** | winner |
|---|---|---|---|---|---|---|
| single-mix | 52 | 52 | 0% | 15.5 s | 0.30 s | 171.87 lb |
| single-zone (2 mix) | 308 | 306 | 0.6% | 93.6 s | 0.31 s | 171.87 lb |
| multi-zone 3-zone | 3,157 | 2,468 | 21.8% | 353.2 s | **0.14 s** | **167.51 lb**, D/C 0.928 |

Multi-zone is **cheaper per configuration** and slower only because the I-beam
stack enumeration produces ~10x the grid.  Its B&B is the only thing keeping
that affordable.

### The tighter bound: admissible, and still WRONG

The bound in use is `min(combo) * volume_factor` -- every zone at the lightest
density.  But `ibeam_zone_stack` is deterministic and puts `max(combo)` in both
flanges, so the bound prices a hull the stack never builds.  Measured looseness:
**bound/true-min = 0.933 average**, giving away 7-14 lb per combo.

A tighter bound is easy to state: `min over the pour-fraction grid of
sum stack_k * V_k`.  It IS admissible -- 154 configurations checked against the
real evaluator, tightest margin `-2.8e-14 lb` (one ulp on 181 lb, i.e. exactly
tight; float non-associativity, not a violation).  With a gunwale rail the
margin is 23-26 lb because the rail's top-zone mass is omitted.

**On the real grid it moved the winner: 167.5681 -> 169.1880 lb.  Rejected.**

Why the admissibility check passed and the bound still failed: admissibility
(`weight >= bound` for the same combo) is necessary but NOT sufficient.  B&B
also needs the bound **monotone along the walk order**, because the walk is
sorted ascending by `min(combo)` and `break`s at the first combo whose bound
reaches the incumbent.  `min(combo) * V` is monotone in `min(combo)` by
construction -- the docstring at `main.py` ~552 says exactly this.  The tighter
bound depends on the whole stack, so a LATER combo can have a LOWER bound (a
narrower spread puts less mass in the heavy flanges).  The walk terminated early
and `(63.5, 71.5)` -- the true optimum -- was never examined.

**If anyone retries this:** the tighter bound cannot be a walk terminator.  It
would have to prune per-combo with `continue` instead of `break`, visiting all
combos and testing each -- a different algorithm, trading an O(1) early exit for
O(n) bound evaluations (6 volume integrations each).  It needs its own
measurement and its own bit-identity check.  Do not simply swap the bound in.

### Method note

The lesson is the same shape as entry 3: verifying the value of a thing is not
verifying the property the algorithm relies on.  The monotonicity requirement
was written in the docstring and read as motivation rather than as the
correctness condition.

---

## 4. SWEEP PERFORMANCE — measured dead ends. Do not re-open without new evidence.

**Filed** 2026-08-09 · **Status: closed.** Every candidate below was measured,
not reasoned about. Four of five evaporated on contact.

### What was actually slow: the tests, and that is fixed

`test_bending_signs` + `test_freeboard` + `test_report` ran **13 h 51 m** for 17
tests, because each paid for a full production sweep to obtain a result dict and
then asserted on cheap, resolution-independent properties. One shared
session-scoped fixture (`tests/conftest.py::small_sweep`, 41 stations, fixed
seating) took it to **4 m 33 s** — verified independently on a real machine.
17 sweeps became 1. No assertion was weakened.

That was the win that mattered. Everything below is the sweep itself, which
turned out not to need fixing.

### The dead ends

| candidate | projected | **measured** | verdict |
|---|---|---|---|
| Per-run overhead | "317 s, 63%" | **0.4%** | **does not exist** |
| Pour-line hoist | 3.8x | 3x, and invalid | **changes the physics** |
| Objective case-set reduction | 5x | **1.3x** | not worth the risk |
| Multi-start reduction | 8x | — | **stays: correctness** |
| Threads -> processes | "GIL serializes" | **1.93x already** | premise was wrong |

**Overhead was a task-count error.** The "317 s" came from comparing a
single-task measurement (189 s) against a 12-task sweep (506 s) and calling the
difference overhead. It was the other eleven tasks. Measured properly, every
phase outside the config loop — pre-sweep hydrostatics, 4-person waterline,
stem-overhang derivation, 2x refinement verification, stability suite,
torsion/lateral, report and graphics — totals **~0.76 s of a 171.5 s run**. The
2x refinement verification, a prime suspect, is **0.08 s**.

**The pour-line hoist changes the physics.** The loop in
`evaluate_canoe_weight` recomputes `w_self` from the pour hand-off height each
pass, and it moves materially: 0.73-1.03 lb/ft between passes 1 and 2. Caching
the envelope across passes would compute a moment envelope for a self-weight
distribution the design does not have. (It is also 3 passes, not 3.8 — the
earlier figure divided by an SLSQP `nfev` that undercounts objective calls.)
The multi-zone evaluator does not iterate at all: one envelope call.

**The case-set reduction is 1.3x and carries a real risk.** Using the two
heaviest races in the placement objective gives BIT-IDENTICAL SEATING on three
designs — the strong result. Using the governing case alone does not: on two of
three designs the solver converged 2.15 ft away and the (still five-case) gate
then reported a materially worse design, Kupfer 1.268 -> 1.586 and 0.987 ->
1.233. The near-margin design is exactly where that matters. The 5x projection
assumed cost scales linearly with case count; it does not, because per-call
overhead dominates. **Untested design class:** every design tried was governed
by `4_coed`. A design where 2M governs at baseline was never tested.

**Multi-start stays.** The seating landscape is genuinely multimodal — a
31-start study found six optima spanning 5.5 lb (see `main.py`, near
SEATING_N_STARTS). Cutting starts is how you land on a local optimum.

**Threads are already working.** 12 tasks summing 330.33 s completed in 171.5 s
wall: **~1.93x real concurrency** through `ThreadPoolExecutor`. Whatever the hot
path releases the GIL on (NumPy internals, SLSQP's Fortran core) is enough. A
process pool might still beat it, but "threads buy nothing because of the GIL"
is measurably false and should not be the basis for switching.

### The only remaining candidate

**An analytic objective gradient (~15x).** The placement objective is
finite-differenced: `minimize(...)` is called with no `jac=`, `eps=1e-3`, 14
variables -> 15 evaluations per iteration. The eps work supplied analytic
Jacobians for the CONSTRAINTS only.

Before attempting it: **measure the objective's smoothness.** During the eps
investigation the objective was a step function (loads snapped to stations,
gradient exactly zero) and was fixed with continuous load deposition; that fix
has not been re-verified with five load cases and 14 variables, nor checked for
quantisation by the discrete mix grid. If the objective is not smooth, an
analytic gradient is the wrong fix. An attempt to measure this produced a
penalty plateau (a malformed probe returning 1e6 everywhere) — the probe must be
validated to return a FEASIBLE design before its gradient means anything.

### Method note

`cProfile` over a full sweep never completed. What worked: time the smallest
units, count calls in a real run, and multiply. When the model disagreed with
the wall clock, the disagreement was the finding — it exposed the task-count
error rather than any hidden cost.

---

## 3. PROCESS NOTE — one defect, seven times: changing the shape of a shared structure

**Filed** 2026-08-08 · Closed by `aa83793` + follow-up fixes · **Read this before
changing the shape of anything shared.**

Expanding the seating vector from 6 elements (4-person + 2-person) to 14 (five
ASCE races) produced **seven instances of a single defect**: code that indexed
the seating vector by hand.

### (a) The seven

| # | site | what it silently did | found by |
|---|---|---|---|
| 1 | `structural.py` `evaluate_canoe_weight`, `vars[6:]` | read **seat coordinates as mix densities** (5.0, 13.0 pcf); rejected every single-zone design | `test_load_cases` |
| 2 | `main.py` post-solve spacing check, `v[2]-v[1]` | compared 2F's bow seat to 2M's stern seat -> always negative -> **every optimizer-mode geometry dropped** before a mix was costed | `test_load_cases` |
| 3 | `main.py` winner's final re-evaluation, missing `crew_cases` | fell back to legacy slicing; **failed a feasible winner** with the 1e6 penalty | `test_sweep_controls` |
| 4 | `main.py` `_build_multizone_result`, `coords[0:4]`/`coords[4:6]` | rebuilt the winner against a **phantom load state** (2M+2F as "the four"); source of a reported D/C of 1.06 being accepted, a 1e6 weight, zero freeboard, and a 16:1 M_sag/M_hog ratio | user's 9-hour run |
| 5 | `main.py` seat markers, `best_res.x[0:4]`/`[4:6]` | plotted seats no crew uses | **pattern scanner** |
| 6 | `main.py` `paddler_positions` result key, same slices | reported seats no crew uses | **pattern scanner** |
| 7 | `tests/test_paddler_jacobian.py` start filter, `v[2]-v[1]` | rejected every SLSQP start: "no seating start converged legally" | itself, after #2 was fixed |

### (b) Why it recurred

- **Each instance was in a different function**, so fixing one taught nothing
  about the others. There was no single place to look.
- **Every one failed silently but totally.** None raised. They produced wrong
  mix densities, an empty feasible set, or a plausible-looking phantom design —
  all of which read as "the design is infeasible" or "the number is what it is".
- **The first guard was scoped to a FUNCTION NAME** (`evaluate_canoe_weight`).
  Instance #4 was in `evaluate_multizone_weight`, so the guard passed while the
  bug shipped.

### (c) What finally caught it

`tests/test_load_case_expansion.py::test_no_hand_written_seating_slices_anywhere`
— a regex scan over every source and test file for `[0:4]`, `[4:6]`, `[6:9]`,
`[:6]`, `vars[6:`, `v[2]-v[1]`, `range(9)`, `size=9`.

**It found instances 5 and 6 within minutes of being written**, after the defect
class had twice been declared closed.

A **pattern** scan beat a **call-site** scan because the defect was a shape
assumption, not a call convention. Call-site scans enumerate what you already
know to check; pattern scans find the same mistake in code you did not think to
look at. A legitimate slice opts out with a deliberately verbose
`LEGACY-SEATING-SLICE-OK` marker.

### (d) Why the fixtures could not see it

Every fixture built during this work used **fixed seating and the multizone
path**. Instances 1 and 2 lived in the **single-zone path and optimizer mode** —
exactly the complement.

> A fixture built to avoid the slow paths systematically avoids exactly the code
> the slow tests cover. Speed and coverage were anti-correlated here.

### (e) The stand-in lesson

Eight sweep-driven tests were reported "verified by stand-in" — their assertions
copied verbatim onto a 41-station fixture. Every stand-in passed. **Not one
could have caught any of the seven defects**, because they re-used the same
fast code path the fixtures used.

> A stand-in verifies the assertion. It does not verify the code path. Only the
> real test does both.

The three sweep-driven files (`test_bending_signs`, `test_freeboard`,
`test_report`, 17 tests) were finally run on a real machine — 13h51m, all pass.
That run is what closed this work.

### (f) The rule for next time

**When the SHAPE of a shared data structure changes:**

1. Before anything else, grep for every hand-written index into it.
2. Add a **pattern-level guard** in the same commit as the shape change — not
   after the first bug, and not scoped to a function name.
3. Derive the layout from one function (here `config.crew_seat_layout()`) and
   treat any literal index as a defect until proven otherwise.
4. Run the slow tests. The fast ones are fast partly because they skip the
   paths where this class of bug lives.

### Related: two bugs made UNREPRESENTABLE rather than corrected

Both were fixed once, recurred, and were then made structurally impossible.

**Crew aliases** (`config/__init__.py`). `RACE_4/2/3_PERSON` were stored values
that `StructuralService` assigned per run without touching `RACE_CATEGORIES` —
so a sweep ran the punching check on the UI's crew and the bending envelope on
the file's. They now have **no module-level binding at all**: a PEP 562
`__getattr__` derives them from `RACE_CATEGORIES` on every read.
`set_race_categories()` is the only supported write. A stored alias can drift;
a derived one cannot. (`tests/conftest.py` re-created the split-brain within
minutes by snapshotting them with `getattr` and restoring with `setattr` — there
is now an assertion forbidding derived names in that list.)

**The rejection sentinel** (`structural.py`). An infeasible evaluation returned
`1e6`, a float indistinguishable from a very heavy hull. Consumers did
arithmetic on it and produced: a `+4.71 in` stability "gain" (a 1e6 lb hull makes
the crew negligible), a `-999842.82 lb` weight delta, and a loaded-freeboard
check that correctly concluded a million-pound canoe swamps — reporting
waterline == mold depth and **zero freeboard for a design whose real freeboard
was fine**. Each was patched where it surfaced; the sentinel kept finding new
consumers. It is now `REJECTED`, a singleton that is **not a number** — every
arithmetic and ordering operation raises `TypeError`.

> **THE ONE LEGITIMATE CONVERSION.** An optimizer objective must return an
> orderable number: SciPy's SLSQP does `if fx < self._lowest_f` internally, so
> returning `REJECTED` raises inside the solver and kills every worker (this
> happened — 0 configs costed, sweep returned `None`). Objectives convert at the
> SciPy boundary via `structural.as_optimizer_penalty()`, and **only there**.
> Do not "fix" a solver crash by reverting the marker; convert at the boundary.
> A test pins both halves apart, and a scanner asserts `as_optimizer_penalty`
> has at most one non-definition call site.

---

## 2. Load-case expansion 3 -> 5, and seating-arrangement enumeration

**Filed** 2026-08-03 · **Opened by** the crew-CG model (commit A)

This is planned work ("commit B"), recorded here with the measurements taken
before it so the approach decision is not re-litigated from scratch.

### 2a. Five race categories, not three

ASCE 2026 runs FIVE races: 2M, 2F, 3M, 3F, 4_coed.  `race_*_person_lbs` is
keyed by crew SIZE, so 2M and 2F share one weight list (likewise 3M/3F) and the
moment envelope runs three cases instead of five.  Sexes and heights already
distinguish the categories (`crew.race_categories`, added in commit A); the
weights cannot yet.

Scope:

| | now | after |
|---|---|---|
| Envelope cases | 3 | 5 |
| Seating solve variables | 9 (`[0:4]`,`[4:6]`,`[6:9]`) | 14 (4+2+2+3+3) |
| `best_config` seat sets | `coords_4/_2/_3` | 5 |
| `fixed_paddler_positions` keys | `"4"`,`"2"`,`"3"` | 5 |
| 9-var layout touchpoints | 46 across `main.py` + `structural.py` | all |

The envelope is a max over cases, so adding two can only raise it: **expect a
heavier winner.**

Two traps, both the same "one-shot estimate feeding a hard constraint" pattern
this suite has been clearing out:

- `main.py` `_crew4_lb = sum(config.RACE_4_PERSON)` assumes the 4-person crew is
  the heaviest and feeds the freeboard ceiling.  With five crews it must become
  a COMPUTED MAX.  (2M+2F coed at ~640 lb probably does beat 3M at ~570, but it
  must be computed, not assumed.)
- `_verify_winner_refinement` and the stability suite both consume the stored
  seat sets.  If they are not updated to five they will silently verify a
  SUBSET -- passing while checking less than they claim.

### 2b. Seating arrangement — measured, decision made

The sweep pairs `RACE_4_PERSON[i]` with seat `i` (bow->stern, enforced by the
SLSQP spacing constraints) and never permutes.  Exactly one arrangement is
analysed, but paddlers may sit in any order.

**Measured** on one real design at 161 stations, seats fixed, over all 12
distinct arrangements of `[160, 150, 170, 160]`
(scratch script `arrangements.py`):

| quantity | spread |
|---|---|
| Kupfer D/C | **0.0000 (0.00%)** |
| M_sag | 0.0 (274.6 lb·ft, invariant) |
| M_hog | **84.4 lb·ft (40.1%)**, 210.4 -> 294.8 |

Sagging is invariant because the trim-band constraint pins the weighted centroid
and sag follows total crew weight near midships; hogging is driven by end-to-end
ASYMMETRY, which is exactly what permutation changes.

Kupfer does not move because at these weights the **2-person** case governs
(`case_index: 1`, station 46) -- the 40% hogging swing stays under its envelope.
That is a threshold, not a general result:

| crew weight spread | Kupfer spread |
|---|---|
| 0 lb | 0.0000 |
| 20 lb | 0.0000 |
| 60 lb | 0.0062 |
| **100 lb** | **0.1044** (0.7600 -> 0.8644) |

At 100 lb spread, arrangement moves Kupfer by 0.104 -- against a 0.9296 search
limit that is the difference between passing and failing.  A coed crew of 2
males and 2 females is precisely where a large spread is likely.

**Decision: per-station max over arrangements, NOT enumerate-and-pick.**

1. Different arrangements govern sag and hog (`[160,150,170,160]` vs
   `[160,150,160,170]`), so no single ordering is conservative for both -- an
   enumeration would have to keep the envelope over all of them anyway, which
   IS the per-station max.
2. The envelope is already a max over load cases; arrangements are more
   instances of the same object.
3. Cost lands on the envelope call, not the sweep.  With five categories only
   4_coed has multiple arrangements (6 for 2M+2F); single-sex crews with uniform
   weights have 1 each.  Realistic cost ~6x on one of five cases.

Honest caveat: on today's crew weights this changes **nothing** (0.00%).  It is
insurance against a spread we plausibly will have, not a live defect.

---

## 1. Put the full-precision-sweep tests on a shared small fixture

**Filed** 2026-08-03 · **Opened by** the GM_T stability pass (`d062686`),
**widened** by the crew-CG model (commit A)

### Problem

Several tests call `run_grand_optimization` at production precision purely to
obtain a result dict, then assert on cheap, resolution-INDEPENDENT properties of
`build_report_model` / `render_latex`. Each sweep costs ~15-35 minutes.

| file | sweeps | what is actually asserted |
|---|---|---|
| `test_report.py` | 4 (one per test) | LaTeX headline weight, figure path format |
| `test_freeboard.py` | 1 (`test_report_uses_loaded_not_geometric_freeboard`) | buoyancy balance, loaded vs geometric freeboard |
| `test_bending_signs.py` | 1 (cached across 5 tests) | fibre-sense D/C, gate vs report agreement |

`test_bending_signs.py` is already the right shape — a module-level `_CACHE`
runs the sweep once for all five tests. `test_report.py` is the worst case: four
near-identical sweeps differing only in `reports_dir`, ~60 minutes to test string
formatting.

The cost is not merely slow. These **could not be run to completion** in the
development environment, killed repeatedly by the background-task runtime limit:

- `test_report.py::test_figure_paths_are_relative_not_absolute` (4 kills)
- `test_report.py::test_figure_paths_survive_a_moved_output_directory` (4 kills)
- `test_freeboard.py::test_report_uses_loaded_not_geometric_freeboard` (3 kills)
- all 5 of `test_bending_signs.py` (1 kill; they share one cached sweep)

Each was verified instead by driving its assertions — copied verbatim, including
`_real_env` and `_graphics_paths` — off a 41-station fixture sweep, all passing.
**Those are stand-ins, not the tests.** They must be run on a real machine, or
rebuilt so they can run anywhere.

Note the good news in the table: splitting `test_freeboard.py` showed 7 of its 8
tests run in **1.75 s total**. Only the sweep-driven one is expensive. Slicing
the slow test out of a file is worth doing before assuming a whole file is slow.

### Why it is safe to fix this way

The assertions are about `render_latex`'s path handling: that
`\includegraphics` targets are relative, drive-free, forward-slash, and
identical across two output directories. None of that depends on sweep
resolution, mix stack, or station count. A cheap winner exercises the same
code path as an expensive one.

### Proposed shape

Add a module-scoped fixture that runs ONE small sweep and yields the result
dict to all four tests:

```python
@pytest.fixture(scope="module")
def sweep_result(tmp_path_factory, ...):
    # 41 stations, single thickness, coarse density step — seconds, not minutes
```

Then have each test build its own `build_report_model` / `render_latex` from
that shared dict. Keep every existing assertion byte-for-byte; the point is to
change what the fixture costs, not what is checked.

Two cautions:

- `test_report_headline_weight_equals_sweep_weight` asserts the report headline
  equals `res["weight_lbs"]` exactly. That holds for any winner, so it is safe
  to share — but it is the test guarding the original stale-weight bug, so do
  not weaken it while refactoring.
- The two figure-path tests currently use `tmp_path`; the headline test uses
  `repo_safe_cwd`. A shared fixture must not let one test's output directory
  leak into another's path assertions. Render per-test, share only the dict.

### Done when

`pytest tests/test_report.py tests/test_freeboard.py -q` completes in seconds
and every test runs in CI without special handling.

### Reference

Stand-ins written during `d062686` and commit A, in the session scratchpad:
`figpaths.py` (same regex, same `_real_env`, same four assertions),
`fb_standin.py` (the five freeboard assertions verbatim) and `bs_standin.py`
(all eleven bending-sign assertions verbatim). All pass on a 41-station sweep
and are the model for what the shared fixture should produce.

---

## 7. Process-pool refactor: what the spec did not cover

Entry 6 specified the RunState, the fingerprint assertion, the stop event,
`freeze_support`, executor selection and worker count. Building it surfaced one
defect the spec did not name, plus two things worth writing down.

### 7a. The sweep tally does not survive a process boundary (FIXED)

`main._SWEEP_TALLY` is a module global. The comment above it said, correctly for
threads, that keeping it out of the worker args tuple costs nothing because the
workers share the object. Under processes that reasoning inverts: each spawned
child re-imports `main` and gets its own `_SWEEP_TALLY = None`, so every
`record()` / `skip()` a worker makes lands in a copy the parent never sees.

Nothing would have raised. The sweep would have finished, produced the right
winner, and printed a gate histogram containing only the parent's own pruning —
an **empty or near-empty gate table**, which is precisely the diagnostic built
in the "what is actually binding" work. A silent wrong number in the one place
built to explain wrong numbers.

Fix: `SweepTally` is now picklable (`__getstate__`/`__setstate__` drop the
`threading.Lock`) and has `snapshot()`. Under the process executor the sweep
starts a `BaseManager`, hosts ONE tally there, hands workers a proxy, replays
the parent's pre-sweep notes onto it, and repatriates a detached snapshot when
the pool closes. Per-`record()` IPC is one round trip per costed configuration —
the same frequency as a beam solve, so it rides in that shadow.

### 7b. The stop event is rate-limited, deliberately

The abort check sits inside the mix-combo loop (every 16 combos). Through a
manager proxy that is thousands of cross-process round trips per task, so
`_RateLimitedEvent` caches the answer for 250 ms. Stop stays responsive far
below the granularity at which a task can actually abandon work.

The caller's `threading.Event` is untouched: a daemon thread mirrors it onto the
proxy one-way. No service signature changed.

### 7c. Caveats that keep threads the default

* **Spawn was validated on the unfrozen interpreter only.** A PyInstaller build
  re-executes the same exe per child; `freeze_support()` is now called first
  thing at both entry points, but this is untested against an actual frozen
  build. Until it is, `SWEEP_EXECUTOR` stays `"thread"` by default.
* **`SWEEP_EXECUTOR` / `SWEEP_MAX_WORKERS` are deliberately NOT in
  `RUN_LEVEL_CONFIG_ATTRS`.** They select how the sweep runs, never what it
  computes. If either could change a result, that is a bug in the RunState, and
  the fingerprint assertion is what turns such a bug into a crash.

---

## 8. Flattening the seating multi-starts (BUILT, REVERTED, UNMEASURED)

The outer parallel level offers one task per (t, d, gunwale) combination -- four
on a typical grid, against 16 physical cores. The 8 seating multi-starts inside
each task are independent SLSQP solves, so submitting (task x start) pairs turns
4 coarse units into ~32 fine ones. This was built and reverted. **It was never
measured on a workload that runs the multi-start at all** -- see the trap below,
which is the reason this entry exists.

### 8a. THE MEASUREMENT TRAP (read this first)

**Every benchmark in this effort set `FIXED_PADDLER_POSITIONS`.** The harness was
copied from `tests/conftest.py::small_sweep`, which fixes seating *deliberately*
to skip the placement solve -- that is the whole reason the fixture is fast. With
seating fixed there are no starts to flatten, so the pre-pass correctly did
nothing, and two runs came back looking like ordinary process runs.

Consequences, all of which must be re-derived rather than reused:

* The 22% core utilisation / 530.9 s baseline were measured with seating FIXED.
  Outer starvation is real and structural (4 tasks, 16 cores), but the link from
  "22% idle" to "flatten the multi-starts" was never measured on a workload
  containing them.
* "The placement solve is ~95% of a task" came from EARLIER profiling of a
  different run and was carried forward as though it applied. It does not.
* Bit-identity held (a6c0d54f, 63 leaves, 167.511799 lb) on every run -- but of a
  code path that never executed. It proves nothing about the flattening.

**Any future measurement of this feature MUST unset `FIXED_PADDLER_POSITIONS`,
and must state in its result that it did so.** A seating-optimising run is much
slower per task, which is exactly why the fixture avoided it.

### 8b. The design (transfers directly)

Submit (task x start) pairs to the OUTER process pool, so each start is
process-parallel. Do NOT nest a thread pool inside each task: that was built,
measured and rejected -- this work is GIL-bound (processes beat threads >2x at
equal worker count), so inner threads add context switching, not throughput.
The nested version measured 627.8 s against a 483-805 s noise band, i.e. nothing.

**The ordered reduction is the part that matters.** The sequential path takes a
strict `<` over starts in `seating_start_points()` order, so the EARLIEST start
wins a tie. Units must therefore be collected BY INDEX and reduced in start
order, never as they complete, or concurrency moves the design. Each unit should
apply the same legality filter the sequential loop applies (finite `fun`, then
within-crew gaps >= 3.4) so the parent's reduction stays a pure ordered minimum.
A task whose starts are all illegal takes `preset=None` and re-runs the
sequential path, which owns the "nothing feasible" fallback -- keep that fallback
in one place.

### 8c. The guard bug (cost a false-negative measurement)

    _flatten = ... and len(tasks) < n_workers        # WRONG

`n_workers = min(cap, len(tasks))` is ALREADY clamped to the task count, so
`len(tasks) < n_workers` is false **exactly when the outer level is starved** --
the case the pre-pass exists for. The pre-pass sizing inherited the same clamp
and would have run 32 units on 4 workers. Compare against the worker CAP.

The tell was a missing `seating pre-pass` progress line. Keep that print: it is
what stops an inactive pre-pass masquerading as an active one.

### 8d. Still unknown

* Whether flattening helps at all on a seating-optimising workload.
* The real per-unit geometry-rebuild cost. Each unit rebuilds its geometry to
  gain parallelism on the solve; the ~5% figure is not applicable (see 8a).
* Whether the 32% of wall spent below peak concurrency (straggler drain, also
  measured with seating fixed) improves with finer units.

### 8e. The trap is now MECHANICAL (tools/bench_guard.py)

Entry 8a said any measurement must unset `FIXED_PADDLER_POSITIONS` and state
that it did. That was then violated a third time, while citing this entry, so
the instruction is now a function call:

    from tools.bench_guard import declare_seating, SEATING_OPTIMISED
    declare_seating(SEATING_OPTIMISED)      # asserts AND prints the evidence

The claim and the check are the same statement, so a harness cannot describe a
seating mode it is not in. Same shape as the hand-written-seating-slice scanner
and the BOM guard: the note did not hold three times, the scanner holds.

A second signal was in the logs the whole time and went unread -- the RunState
FINGERPRINT. `FIXED_PADDLER_POSITIONS` is run-level, so fixed-seating runs
print `run 25ca9e1a` and optimised ones print `run e7d1d862`. Reading it would
have caught the trap two runs earlier than the harness comments did.

### 8f. First seating-OPTIMISED process run (measured)

Process executor, seating genuinely optimised, guard-enforced, 41 stations:

| | fixed seating | optimised seating |
|---|---|---|
| wall | 158 s | **3062.9 s** |
| winner | 167.511799 lb | **158.985398 lb** |
| mixes | [71.5, 63.5, 71.5] | [67.5, 61.5, 67.5] |

* **No crash, no deadlock, zero worker errors** across six pool invocations --
  the first process run to reach the placement solve at all.
* **The "~95% of a task is the placement solve" figure is CONFIRMED on this
  workload**, not carried over: 158 s -> 3062.9 s on the same grid.
* Winner self-consistent (reported weight == winning configuration's weight).
* **Optimising seating is worth 8.53 lb** against the fixture's fixed
  positions. A design result, and larger than most of the performance work.

This raises the value of 8b considerably. Fifty-one minutes with 12 of 16 cores
idle, on a workload that is 95% eight INDEPENDENT solves, is the largest
remaining lever -- and 8a's trap no longer stands in the way of measuring it.

---

## 9. Geometry Search Bounds panel: two changes, specified (BUILT)

Deferred to a fresh session deliberately. Item 9b changes the SEARCHED GRID, so
it needs verification budget, and a partially-applied change to `depth_options`
would alter results silently.

Already landed: the sweep-side empty-range guard (`cd3bae5`), the
`depth_max_in` re-parameterisation with `depth_span_in` kept as a deprecated
alias (`01d5651`), and now the UI half -- see 9e for what the panel work
turned up that the spec did not anticipate.

### 9a. Delete the Wall Thickness STEP field

Thickness snaps to the buildable grid (1/32 in and 0.5 mm increments,
layer-divisible). **VERIFIED: no code path reads a thickness step.**
`main.py` does `thickness_options = thickness_options_in if thickness_options_in
else _default_t`, where the default comes from a buildable-layer grid taking
`(min, max, n_layers)` -- no step parameter anywhere. The UI's `0.0000` is an
inert control, which is why it neither divided by zero nor did anything.

Delete the field, keep min/max, and put in its place:
"snaps to buildable trowel increments (1/32 in & 0.5 mm)".

### 9b. Freeboard depth: span -> absolute maximum

Today `main.py`:

    depth_options = np.arange(MIN_MOLD_DEPTH, MIN_MOLD_DEPTH + _depth_span, _depth_step)

so the second field is a SPAN ABOVE THE FLOOR while the UI labels it a maximum.
"min 6 / max 4.5" therefore looks inverted but is not -- it is floor 6 in, span
4.5 in. **Fix the label/semantics BEFORE writing any min<max validation, or the
validation will reject valid input.**

Target: `arange(floor, depth_max, step)`, min -> max directly.

* Keep `depth_span_in` accepted as a DEPRECATED ALIAS. Several tests and saved
  presets pass it, some positionally.
* **The equivalence check IS the acceptance test, not an afterthought:** the new
  `(min=12.57, max=17.07)` must produce an array identical to the old
  `(floor=12.57, span=4.5)`. Assert on the array, then do the bit-identity run.
* **The hard floor is not optional.** `MIN_MOLD_DEPTH = true_draft_in +
  freeboard_min_in` (12.57 in on the current hull) is computed and enforced
  regardless of input, so a user max BELOW it yields an empty range even though
  `min < max` looks valid. Surface the effective floor in the UI and error with
  the actual numbers: "maximum depth 10.0 in is below the freeboard-required
  minimum of 12.57 in". The sweep-side guard (9's prerequisite, landed) already
  reports floor/span/step for the headless path.

### 9c. Deferred with them

The min/max validation dialog (one modal listing EVERY offending variable, not
one per field) must be built against 9b's corrected semantics. Keep the
sweep-side guard regardless: the UI protects one path, the guard protects
headless callers, and zero tasks must never read as "no feasible design".

### 9d. "Verified inert" was verified in one file (second instance)

Entry 8's rule -- grep the whole tree, not one file -- now has a second case.

The Wall Thickness STEP field was reported here as VERIFIED inert, "no code
path reads a thickness step". That was concluded from `main.py`, where
`thickness_options` arrives already built:

    thickness_options = thickness_options_in if thickness_options_in else _default_t

Nothing downstream consumes a step, which is true and irrelevant: the grid is
CONSTRUCTED UPSTREAM, in `ui/structural_tab.py` (~l.1674), where a positive
step deliberately overrides the buildable grid and prints "buildable grid
overridden". So `0.0000` was never a broken control -- it is the documented
sentinel for "use the buildable grid", and deleting the field on the strength
of that finding would have silently removed a working advanced feature.

Same shape as `_within_crew_gaps`: absence in the file being read was taken for
absence in the system. The consumer was one layer up, in the direction the
data comes FROM rather than the direction it goes TO.

**When checking whether a parameter is dead, trace where its value is
CONSTRUCTED as well as where it is consumed.** A UI-built value has its logic
in the UI; `main.py` only ever sees the result.

Consequence for 9a: the field is NOT deleted. It becomes an "Override
buildable thickness grid" checkbox revealing the step field -- zero-as-sentinel
is the exact pattern the REJECTED work exists to remove, and progressive
disclosure matches the gunwale controls in the same panel.

### 9e. What the UI half turned up (MEASURED)

Three things the spec did not anticipate. All three are the same species as 9d
-- a value whose meaning lives one layer away from where it is read.

**(i) Converting the default is not a relabel. It is arithmetic, and it
widened the search by one depth.**

The old field held a SPAN of 4.5 in; the new one holds an absolute depth, so
its default has to be `floor + 4.5` written into a spin box's precision. On
the current hull the floor is 10.576026 in, and `round(floor + 4.5, 2)` is
15.08 -- four thousandths ABOVE the true end. `np.arange`'s interval is
half-open, so those four thousandths admit one extra depth at every step fine
enough to reach them: 37 depths at 0.125 in where the old default searched 36.

The field TRUNCATES instead (`math.floor(x * 1000) / 1000`, 3 decimals).
Truncation can only lose a depth within 0.001 in of the old end, and the old
end was already excluded by the half-open interval. Verified over every step
the field can hold -- all 1,876 values from 0.125 to 2.000 in, at five
different floors -- old and new arrays identical, and no depth dropped in
either direction. The preset migration uses the same truncation for the same
reason.

`test_rounding_the_default_up_would_widen_the_search` pins the bug itself: if
it ever stops failing, `round()` became safe and the comment is stale.

**SUPERSEDED for the default (entry 10).** Bounding FREEBOARD instead of mold
depth removes the arithmetic rather than taming it: both ends are measured
from the same floor, so the span between them is exactly `max - min` inches
and nothing is rounded anywhere. The truncation survives only in the span-era
preset hop, which does have to write a converted number into a field.

**(ii) A user max no longer slides with the 4-person floor. This is a real
behavioural change, it is the correct one, and it is now REPORTED.**

`MIN_MOLD_DEPTH` starts at `true_draft_in + freeboard_min_in` and
`calculate_4_person_waterline` can RAISE it. Under the old span semantics the
whole window slid up with it: the search was always 4.5 in tall. Under an
absolute maximum the floor rises INTO a fixed ceiling, so the range narrows.

That is what "maximum" has to mean -- a ceiling the user set is not a ceiling
if the program moves it -- but it is not a relabel, and a hull where the 4P
check binds will search fewer depths than it used to.

> **CORRECTION (entry 10).** This paragraph continued: "It does not bind on
> the current hull (draft 4.576 in + 6 in freeboard clears it), which is why
> the default grid comes out identical." **Both halves are false.** It binds,
> and always has -- the 4-person waterline is 6.57 in, so the floor rises
> 10.58 -> 12.57 in, a 1.99 in raise. The default grid therefore did NOT come
> out identical: `d96d1c7` shipped searching **6 depths where the design
> intends 9** at the 0.5 in step, and 21 where it intends 36 at 0.125 in.
>
> The error came from reading 9b's own parenthetical -- "`MIN_MOLD_DEPTH =
> true_draft_in + freeboard_min_in` (12.57 in on the current hull)" -- as
> though 12.57 were draft + freeboard. It is the RAISED floor; draft +
> freeboard is 10.58. The formula and the number in that sentence describe
> different quantities, and the mistake was inherited rather than checked: no
> run output was consulted until a user pasted one. **A number quoted beside a
> formula is not evidence that the formula produced it.**
>
> Entry 10 removes the cause. The narrowing described here is real, but it
> applies only to the fixed mold-depth ceiling, which is no longer what the UI
> sends.

Correct is not the same as visible, though, and this one is invisible by
construction: all the sweep printed was the NEW floor, so a user on a binding
hull would watch their depth count drop with nothing stated. Fewer depths than
you asked for reads as a bug, not as a rule. `describe_depth_narrowing()` now
states it with the numbers -- the entered range and its depth count, the floor
the 4-person waterline raised it to, the resulting narrowed range and count,
and what to do (raise the maximum). It goes to the console at the point the
grid is built and into the search-space banner's `mold depth` entry, so it is
in the run record and not only in the scrollback. `None` when nothing
narrowed -- which, per the correction above, was NOT the current hull. It is
also None for a freeboard band (entry 10), which is anchored to the floor and
so slides rather than narrowing.

The empty-range error was misattributing the same floor: it said "(draft X +
freeboard Y)" unconditionally, so a 4P-raised floor sent the user to lower a
freeboard requirement that was already satisfied. It now names the 4-person
check when the 4-person check is what bound.

This fires on the current hull TODAY. The paragraph originally closed "this
is for whoever changes hulls next season; nothing in it fires today", which
was the same mistake as the correction above.

**(iii) The UI must not invent its own fallback draft.**

`_hull_draft_in` calls the SAME `parse_godzilla_dimensions` main.py calls, and
lets that function's own missing-file default (14.0 in) stand. A private
fallback of our own would drift from the sweep's exactly when out.mlt is
missing -- which is precisely when a user reads the floor label to find out
what is going on. The label and the guard must agree or neither is worth
printing.

### 9f. Presets: two controls changed MEANING, so old files convert

Both old keys are translated on load, never dropped (`_migrate_preset`):

* `depth_span` -> `depth_max` as `floor + span`, computed against the
  PRESET'S OWN freeboard rather than whatever is on screen. A 4.5 written
  straight into an absolute-maximum box asks for a 4.5 in mold, which is below
  every floor and searches nothing -- a preset that "loads successfully" and
  then finds no feasible design.
* `thickness_step: 0` was the buildable-grid sentinel, so it becomes
  `thickness_override: False` and the 0 is DISCARDED (the field's minimum is
  now 1/32 in; writing the sentinel back would clamp it to a real step and
  turn a buildable-grid preset into an override). A positive saved step
  becomes `thickness_override: True` with the step intact.

The step field's minimum being 1/32 in rather than 0 is the point of 9a, not a
detail of it: a spin box that still accepts 0 lets the sentinel back in.

### 9g. The banner needed a flag, not an inference

`main.py` receives `thickness_options` already built and printed
"N buildable options" unconditionally -- the banner asserting the opposite of
the truth whenever the grid was overridden. It is tempting to have main.py
detect this by testing the values against the 1/32 in / 0.5 mm lattice, and
that would even work. It is 9d's mistake wearing a different hat: the grid is
CONSTRUCTED in the UI, so the UI is the only layer that knows, and a detector
would silently start lying the day someone's custom step happened to land on
the lattice. `thickness_grid_overridden` travels explicitly through
`OptimizationConfig` -> `StructuralService` -> `run_grand_optimization`, and
through the `_companion` re-entry with it (which also needed `depth_max_in`
forwarded -- without it the mix-development companion would have priced its
designs against the DEFAULT 4.5 in span instead of the user's ceiling).

## 10. Freeboard as the bound, a live density floor, and the panel redesign (BUILT)

Three changes in one session, plus the bug the first of them shipped and the
correction it forced on entry 9e.

### 10a. THE DATUM BUG (read this before touching the depth row again)

The row now bounds FREEBOARD at both ends -- min and max of one quantity,
which is what the `Variable | Min | Max | Step` header always claimed. Mold
depth is derived. The first version of that conversion was:

    _depth_max = true_draft_in + freeboard_max_in          # WRONG

and it is wrong for a reason no amount of staring at the row would reveal.

`true_draft_in` is the **2-person design draft** -- what Godzilla shaped the
hull for, 4.58 in here. `MIN_MOLD_DEPTH` is `freeboard_min_in` above the
**governing** draft, and `calculate_4_person_waterline` moves that to the
heaviest crew's waterline, 6.57 in. So the minimum was measured from one
waterline and the maximum from another, with 1.99 in of unstated difference
between them.

The user typed "min 6.0 / max 6.5" and was told:

    maximum freeboard 6.50 in is at or below the minimum the sweep will
    accept (7.99 in of freeboard ...)

7.99 in is 12.57 - 4.58: the 4P-raised floor expressed in 2-person freeboard.
A number that is not a freeboard in any sense the user could act on.

**The loud face was the lucky one.** A maximum BELOW the floor raises; a
maximum ABOVE it just runs. The shipped default (min 6.0, max 10.5) resolved
to 15.08 in against a 12.57 in floor, so it ran and quietly searched **6
depths where the design intends 9**. Nothing in the output said so. That is
what this class of bug normally looks like, and it is why the correction in
9e-ii matters more than the crash did.

The fix anchors the ceiling to the same thing the floor is anchored to:

    _depth_max = MIN_MOLD_DEPTH + (freeboard_max_in - freeboard_min_in)

The band is `max - min` inches wide wherever the floor lands. A raised floor
SLIDES it. Both ends move together, so the width and the step count are
invariant -- verified across every plausible 4P outcome.

**The lesson is not "check your drafts".** It is that two names for the same
quantity are not the same measurement. `freeboard_min_in` and
`freeboard_max_in` are both freeboard, both in inches, both obviously
comparable -- and they were measured from different points. Same species as
9d and `_within_crew_gaps`: the thing that differed lived one layer away from
where the comparison was written.

**When a pair of bounds is introduced, verify the DATUM, not just the unit.**

### 10b. Freeboard bounds, and what they retired

`freeboard_max_in` is the form the UI sends. `depth_max_in` (absolute mold
depth) and `depth_span_in` (span above the floor) remain accepted; exactly one
of the three may be passed, and `run_grand_optimization` raises naming all
three otherwise.

What this bought, beyond the row finally holding one quantity:

* **The equivalence is exact by construction.** `_depth_span` comes out as
  `freeboard_max_in - freeboard_min_in` with no float conversion at all, so
  the default reproduces the historical `arange(floor, floor + 4.5, step)`
  bit-for-bit at every step -- including when the 4P check binds, which
  `depth_max_in` did not. The 9e-i truncation is no longer load-bearing here.
* **The conversion has one home.** The UI passes freeboard and never touches
  the draft; main.py converts against the draft that run parsed. Two
  conversions that can disagree became one that cannot.
* **The error answers in the units asked in.** A freeboard ceiling gets a
  freeboard error naming the two freeboards and nothing else. Naming a draft
  there is what made the first message unreadable.

The step floor went 0.125 -> 0.01 in. This axis MULTIPLIES the configuration
count, so 0.5 -> 0.05 in is a sweep ten times longer rather than ten more
evaluations; the range note under the grid prints the step count so the cost
is legible before the run. Note that a 0.5 in band at the 0.5 in step searches
exactly ONE depth -- the finer step is what makes a narrow band mean anything.

### 10c. The density floor stopped being documentation

`config.MIN_DENSITY_PCF` existed in `canoe_suite.cfg`, in
`RUN_LEVEL_CONFIG_ATTRS`, and in `config/__init__.py`, carrying this comment:

    MIN_DENSITY_PCF is the team's stated floor.  It is NOT the sweep's floor
    ... Nothing gates on it today.

A setting the user could change that changed nothing -- the same shape as the
thickness step in 9a, and worth noticing as a shape. A "Min Mix (pcf)" field
now bounds the grid through `get_structural_sweep_grid(min_density=...)`.

Two rules it inherits:

* It can only RAISE the Mix Lab regression's viability floor. Below that the
  regression predicts no strength, so those densities are configs built,
  evaluated and rejected -- sweep time spent to reach a foregone conclusion.
* It FILTERS the anchored grid and never re-phases it. This is the
  `_ANCHOR_PCF` note in `mix_database.py`, where moving a floor once turned
  57.5/59.5/61.5 into 59.3/61.3/63.3 and a winning stack stopped EXISTING.
  Tested: the bounded density set must be a SUBSET of the unbounded one.

Because either bound may now bind, the range note says WHICH one did --
otherwise a user who lowers Min Mix and watches the range refuse to move has
no way to learn why. An inverted range raises instead of returning None, for
the reason entry 9c gives: zero configurations must never reach the user as
"no feasible design".

### 10d. The panel redesign (imported from claude.ai/design)

Dark/amber theme, a Seed -> Godzilla -> Structural -> Export stepper, a pinned
run block, and independent section cards. Two notes worth keeping:

**`_CardStack` deliberately wears QToolBox's `addItem`/`setCurrentIndex`.**
The five category blocks are long and full of controls whose meaning has
already moved; carrying a presentation change and a control change in one diff
is how the next one moves unnoticed. Keeping the interface meant the redesign
could not touch them. The gain is not visual either: a QToolBox shows ONE page
at a time, so checking a thickness bound against the depth bound that
constrains it meant clicking back and forth holding a number in your head.

**Palette tokens are named by ROLE (`C.ACCENT`), not by colour.** The accent
moved cyan -> amber and every hard-coded `#00d2ff` had to be found by hand.

**Three parts of the mockup were NOT implemented**, because each would have
regressed a decision made deliberately:

1. an "Advanced Precision Override" card with a `Depth Span` field -- the span
   semantics 9b removed, in a panel the code records as deliberately deleted;
2. "Wall Thickness Sweep" with Min/Max only -- 9a's ORIGINAL proposal, which
   9d overturned;
3. a "Multi-Zone I-Beam Pour" toggle and zone count -- multizone is automatic
   above one mix, with a fixed 3-zone stack and no user-facing count.

The design's `github.md` records a sync against `main` at 2026-08-16T15:29Z
while this work sits on `docs/report-timeless-prose`, so it was drawn against
a tree predating it. **A mockup is a proposal about appearance; where it
encodes semantics, the code's history outranks it.**
