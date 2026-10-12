# Known Issues

Everything here is **deliberately shipped**. The release rule is:

> A defect that produces a **wrong number in a design** blocks release.
> A defect that degrades output, slows tests, or costs a little weight ships.

Each entry says what it is, why it does not block, and what closing it would take.
Nothing here has been forgotten — it has been priced.

---

## Structural / gate

**The screen gates at 0.9296, not 0.9500.**
The refinement gate was designed to "screen loose, reject on the refined value",
but the sweep still screens on `kupfer_search_limit()` — the acceptance limit
pulled back by the global `kupfer_drift_per_doubling` constant. So designs that
refinement *would* reject are excluded before the gate sees them, and the gate
has yet to reject anything (0 refined rejections across a 312-config grid).
*Safe direction:* it rejects some feasible designs and admits nothing
infeasible. *To close:* screen at 0.95 and let refinement do the rejecting —
changes which designs are considered, so not a pre-release change.

**Drift tracks DEPTH, not design.**
Measured across 12 survivors: every design at depth 13.070 drifted
+1.224–1.227 %, every design at 13.570 drifted +0.901–0.911 %. Within a depth
the spread is 0.003 pp. Per-design extrapolation is therefore buying almost
nothing over a per-depth constant. It is not *wrong* — it is more machinery
than the evidence needs. *To close:* re-derive as a per-depth table once more
depths have been measured.

**The 2D (2-mix) path has no refinement gate.**
The gate is guarded on `cfg['multizone']` and calls
`evaluate_multizone_weight`. The 2D path returns an explicitly empty candidate
list. Mitigated, not fixed: `python main.py` now refuses to run without
`--multizone` or `--ungated-ok`, the winner banner prints an
`UNGATED RESULT — NOT REFINEMENT-VERIFIED` block, and the GUI warning banner
flags it. *To close:* a sibling `_evaluate_cfg_2d_at`; the gate body,
extrapolation and audit reuse unchanged.

**The gate pool is capped at `GATE_TOP_K = 12`.**
When the cap binds and candidate *k+1* is within 0.72 % of candidate *k* (the
measured weight drift under refinement) the ordering is not trustworthy and the
gate says so. It does not automatically widen. *To close:* raise `GATE_TOP_K`
and re-run.

**`knee_frac_bending` is an inert knob.**
While `PADDLER_LOAD_ENVELOPE = True` the envelope always contains the
no-toe-relief model, and that model governs the sagging peak, so the bending
basis cannot move the reported D/C — measured identical to 8 decimals at
161/321/641/1281/2561 stations. A startup warning says so when the value
differs from the punching basis.

**`MIN_PADDLER_SPACING_FT = 3.5` is dead; the code enforces `3.4`.**
`main.py:1464` and `:1485` use a hardcoded 3.4 ft. The check happens, 0.1 ft
(1.2 in) looser than the constant that documents it. Numbers for any admitted
design are correct; the constraint is simply looser than advertised.
*To close:* read the constant, or delete it and document 3.4.

**Dead constants.** `MIN_MIX_DENSITY_GAP_PCF`, `MOMENT_THRESHOLD_FRAC` — zero
references repo-wide, and no shadow check enforces a weaker version of either.

**`tensile_viability_floor()` has no span check.**
It extrapolates the tensile zero-crossing without asking whether that density
lies inside the measured range. On today's data a span check would move the
floor from 59.30 to 66.90 pcf. *To close:* clamp to the lowest measured break —
worth doing once the real lab rows land, not before.

---

**The torsion and lateral checks are ADVISORY and both overstate.**
Neither gates the sweep. A run will routinely show lateral over its allowable —
on the smoke-test hull, `sigma_lat = 741 psi` against `allow = 245.6 psi`, 3x —
and **that needs no action**.

* *Torsion*: `J = t^2*A/3` is the OPEN-section St. Venant bound. It ignores the
  warping restraint from the closed stems and gunwale flanges, so tau is high by
  roughly an order of magnitude. This was already documented.
* *Lateral*: `_section_lateral_inertia` is a hand-calc that models the section
  as a straight V (half-breadth rising linearly from zero at the keel) and is
  documented valid to <5% only for `B_half < D/3`. The smoke-test hull has
  `B_half = 10.35 in` against `D/3 = 4.69 in` — **2.2x outside its own stated
  range**. Two effects compound, both the same way: a real canoe is far fuller
  low down than a V so true `I_lat` is larger, and the dropped arc-length factor
  `sqrt(1 + (dy/dz)^2) >= 1` understates `I_lat` again. Both push `sigma_lat`
  up.

The transverse stress that DOES gate is the **Kupfer biaxial check**, on the
properly transformed composite section — D/C 0.28 on the same run.

Both now report `[ADVISORY]` rather than a bare `[WARNING]`, which was
indistinguishable from a real failure. *To close:* compute `I_lat` from the
actual offset table instead of the V idealisation, so the advisory is worth
reading rather than worth ignoring.

---

## Exception handling — the narrowing pass, and the six sites exempt from it

A pass across this module narrowed 24 broad `except Exception` handlers to the
specific exception each one was actually written for. **That pass was right for
18 of them** and should not be undone: a handler that catches everything turns a
`NameError` into a silent skip, which is exactly how the refinement gate came to
report a winner it had never touched.

**Six were re-widened deliberately, on this rule:**

> **A broad catch is justified when an exception would discard work already
> completed, not when it would skip work not yet attempted.**

That is the distinction that decides it. Skipping work not yet attempted is
cheap and recoverable, so a narrow catch there costs nothing and buys a loud
failure. Discarding completed work is neither, and the completed work in this
suite is measured in hours.

| Site | What an escaping exception would discard |
|---|---|
| `main.py::_audit_run` | the finished sweep, over an audit line |
| `main.py` crew-CG model (`~:3482`) | the finished sweep, over a printed GM_T |
| `main.py` real-KG suite (`~:3522`) | the finished sweep, over the stability block |
| `main.py` mix-development spec (`~:3743`) | the sweep **and** the re-pricing runs stacked on top of it |
| `services/structural_service.py` provenance tag (`~:168`) | the finished sweep, over a version string |
| `reporting/latex_report.py::_solid_keel_audit` | the report being built, over one optional audit |
| `reporting/report_generator.py::_add_hull_section` | seven later report sections, over section two |

Each carries the warrant in a comment at the handler. **Do not re-narrow them**
without reading it — the narrowing pass was a good idea applied one step too
far, and these six are where it went too far. The two report sites are the
lowest stakes of the six (losing a report is not losing a sweep) but they are
the same shape: work in a path that has already succeeded, thrown away by an
exception in an optional decoration on it.

None of the six is silent. Each prints or logs the exception **type and
message** — a bare `{exc}` was adequate when only `ImportError` could arrive and
is not adequate now, because a `KeyError`'s message is just the key.

**A failed fit-override read is indistinguishable from no override.**
Two sites (`services/structural_service.py` provenance tagging, and
`main.py:2057` in the startup guard) set `fit_override = None` when the mix
database will not answer. But `fit_override_record()` returns `None` for the
legitimate case *"no override is in force"* — so the failure value and the
clean-provenance value are the same value, pointing the same way, which is the
collision `CLAUDE.md` names. A design built on an overridden strength fit could
therefore travel into a PDF with its provenance silently clean. It degrades a
**warning**, not a number, so it ships. *To close:* return a truthy record
(`{"source": "unreadable", ...}`) so every consumer's truthiness test fires.

## Packaging

**Frozen builds force `SWEEP_EXECUTOR` to `thread`, and that costs 5.5x.**

MEASURED, same grid, same 7,712 configurations, same 6 tasks on 6 workers,
161 stations, each leg run as the first sweep in a fresh process:

| executor | wall | configs | |
|---|---|---|---|
| thread | **1209.1 s** | 7,712 | 202 s per task |
| process | **211.2 s** | 7,712 | six tasks in the wall time threads take for one |

**5.73x**, one controlled run, both legs the same 6 tasks on 6 workers and the
same 7,712 configurations. Independently corroborated at 5.54x by two earlier
runs measured separately (1217.8 s / 219.9 s). Perfect scaling on 6 workers would be 6.0x, so threads are
recovering essentially NONE of the available parallelism — the GIL serialises
the sweep almost exactly. That is what the hot-path shape predicts: ~12 M tiny
numpy calls plus an SLSQP solve, all of it holding the GIL.

Two caveats on the number. It was measured in the INTERPRETER, not the frozen
binary, because the frozen build has no console entry point to drive a sweep
headlessly (see below) — acceptable because PyInstaller changes how the
interpreter is packaged, not how it schedules threads. And it used
`SEATING_N_STARTS = 2` to keep the thread leg under an hour; that shrinks each
task without changing the workload SHAPE, which is all the ratio depends on.

**A process-mode failure seen during this work was NOT a product bug.**
One benchmark reported `RunState verification FAILED ... Differing: []` on a
161-station thread-then-process run, and it briefly looked like process mode
was unsafe. Cause: `config/run_state.py` was EDITED WHILE THAT BENCHMARK WAS
RUNNING. The parent had imported `RUN_LEVEL_CONFIG_ATTRS` at launch; the
process pool's children spawned after the edit and imported the newer list, so
the child's capture carried a key the parent's state did not. The fingerprints
differed, and the old diff iterated only the parent's keys -- hence a failure
that named nothing.

The exact configuration passes on a clean tree (it is the 5.73x run above).
Production never edits its own source mid-sweep, so this is a DEVELOPMENT
hazard, not a shipped defect. `RunState.verify()` now names key-set
differences, one-sided keys and payload mismatches; pinned by
`tests/test_worker_config_reaches_workers.py::TestVerificationDiagnostics`.

**This does NOT mean flip the switch.** The 5.5x is real and available, but
taking it means running a process pool from a frozen GUI daemon thread, which
is the exact combination CLAUDE.md's standing guardrail forbids. A frozen
console probe confirms `ProcessPoolExecutor` works under PyInstaller (spawn,
`freeze_support` holds, children do not re-run the app); what is NOT verified
is the sweep's own pool, which pickles `RunState`, hundreds of tasks and numpy
arrays through that daemon thread. The measurement justifies doing that
verification work; it does not substitute for it.

*Original entry:*

`canoe_suite.cfg` ships `process` on the strength of a >2× measurement made in
the *unfrozen* interpreter. A frozen console probe confirms a
`ProcessPoolExecutor` **does** work under PyInstaller (spawn, `freeze_support`
holds, children do not re-run the app). What is *not* verified is the sweep's
own pool — it pickles `RunState`, hundreds of tasks and numpy arrays through a
GUI daemon thread, which is the combination CLAUDE.md's standing guardrail
forbids. Slower is acceptable; a first-run deadlock is not. The override
announces itself.

**The console entry point now EXISTS** — `packaging/jaguar.spec` builds a
second executable, `jaguar-cli.exe`, beside `JAGUAR.exe` with `console=True`.
Until it did, the frozen build could only be driven through the GUI, which is
why "verify the process pool under PyInstaller" stayed open: the verification
was not merely undone, it was **unrunnable**. `--allow-process-pool-frozen`
lifts the thread override explicitly (never a config default that could arrive
by copying someone's `canoe_suite.cfg`).

**A WATCHDOG now bounds the worst case.** If no sweep task completes within
`SWEEP_STALL_TIMEOUT_S` (default 1800 s) the pool is torn down and the search
restarts from the beginning on threads, saying why. That converts "the window
stops responding and the only way out is Task Manager" into "slower, and it
says why" — the failure mode the guardrail exists for is now survivable rather
than fatal.

Implementation note that matters: the pool phase is a CLOSURE
(`_run_pool_phase(kind)`), so a retry gets fresh locals. Resetting seven
accumulators by hand at a retry point is exactly how a fallback run ends up
reporting a winner assembled from two half-searches. Also, the stall check is
`wait(FIRST_COMPLETED)`, not `as_completed(timeout=)` — the latter bounds the
whole iteration and would fire on a healthy two-hour sweep; what is wanted is
the GAP between completions. Pinned by `tests/test_executor_watchdog.py`,
which hangs a worker on purpose, because a watchdog that has never fired is a
comment rather than a mechanism.

*To close (what is LEFT):* Tier 2 — the GUI, with Stop pressed, five runs. The
exact procedure is in `README_TEAM.md` section 8. Until that passes, process
mode stays behind the explicit flag.

**The frozen build analysed a hull nobody designed.**
`filepath_matrix = os.path.join(cwd, "off1.mlt")` — and a frozen run's cwd is
wherever the user launched the exe, so the bundled copy was never found and the
FALLBACK GENERATOR quietly produced a default hull. Measured on the same
settings:

| | grid | winner |
|---|---|---|
| source | x = [0.0, **20.0098**] ft | 269.6 lb |
| frozen | x = [0.0, **20.0**] ft | **5814.2 lb** |

The 5,814 lb figure was not a bad design; it was a different boat. Third member
of the same family as the config file and the mix database: an asset resolved
relative to something a frozen build does not have.

`main.hull_offsets_path()` now searches beside the executable first (the copy a
team can replace with their own offsets), then cwd, then the bundled copy. Every
run prints `Hull offsets: <path>`, and a run that finds none prints a block
naming every location it looked — "a default hull was invented" must not be
something you infer from a weight.

**A gate PROMOTION crashed a completed sweep at the report step.**
The gate pool holds LEAN records (13 fields, no geometry) deliberately — a full
config is 74 KB, mostly hull geometry identical across designs at one depth. But
when the gate promotes one (the screen winner did not survive refinement and a
heavier candidate did), that record became `global_best_config` and the report
path indexed `global_best_config['x']`:

```
The screen winner did NOT survive refinement; the gate promoted a
heavier candidate that did (5814.228 -> 5818.052 lb).
...
KeyError: 'x'
```

Search, gate and verification all completed; the entire run was then lost on the
one path that exists BECAUSE the gate did its job. No test covered promotion
followed by reporting.

`main.rehydrate_gate_winner()` rebuilds the geometry from the design the lean
record describes, deterministically and by the same route the worker uses. The
caller RAISES if the rebuild fails rather than reporting a promoted winner
without the geometry it was gated on.

*Worth knowing if you touch it:* the rebuild was first written from memory and
had SIX wrong attribute names — the real fields are `cum_area_in2`,
`cum_Az_in3`, `cum_Iz_in4`, `cum_Iy_in4`, `yhalf_in`, `z_grid_in`, and
`sheer_in` is not a field at all (it is `c_bot + c_top`). Two were fetched with
`getattr(..., None)` defaults, so the promoted winner would have carried None
lateral terms and been silently unable to form its Kupfer state — quiet, not
loud. Pinned by `tests/test_frozen_asset_discovery.py`.

**A CHANGE VERIFIED FROM SOURCE SHIPPED BROKEN IN THE GUI. TWICE.**
Read this before handing anyone a build.

    TypeError: StructuralService.run_sweep() got an unexpected keyword
               argument 'price_mix_development'

Every GUI sweep died instantly. The pricing checkbox was wired ui -> service,
and the service was taught to forward the flag to the sweep, but `run_sweep()`
was never given the parameter. It survived 65 test files, the full guard suite,
a bit-for-bit source-vs-frozen comparison and five exercised GUI subsystems.
The CLI was fine the whole time. (The earlier instance: a warning banner whose
only reference to `logger` lived inside its own exception handler.)

**THE GAP: nothing crossed `ui -> services`.** The suite tests the sweep, the
service internals and the physics; the CLI exercises `run_grand_optimization`
directly. The one call the whole team makes was the one nobody made.

**RULE: anything touching the GUI needs the GUI exercised before the build is
handed over — the suite is not a substitute.**

Two tests now cover it, and each was verified by REINTRODUCING the bug:

* `tests/test_call_signatures.py` — statically compares what `ui/` passes
  against what the service accepts, and what the service forwards against what
  the sweep accepts. Also flags a parameter the callee accepts and never uses:
  the pricing flag was one line from exactly that (read off `opt_config` while
  the GUI passed a kwarg), and whichever half had landed alone, the checkbox
  would have looked wired and done nothing.
* `tests/test_gui_smoke.py` — builds the real `StructuralTab` headless and
  binds the live widget values against the real signature. Static analysis
  cannot catch a renamed widget; construction can.

*Two traps found while writing that second one, both worth knowing:*
building Qt inside the pytest process KILLED it — "collected 4 items", no
results, **exit 0**. A test that takes the process down while reporting success
is worse than none, so Qt runs in a subprocess and a crash is now a failure
with the child's output attached. And importing QtWebEngine under the offscreen
platform hard-crashes with `0xC0000409` and no output at all, so the 3-D view is
stubbed. **That test therefore does NOT cover the 3-D hull tab.**

*The same root cause, six times in one session:* code calling working code with
arguments nobody checked against the actual signature —
`CanoePerformance.__init__`, its method names, `straight_time`, `render_latex`
twice, `StructuralTab.__init__`, and six wrong `ShellProperties` field names.
`inspect.signature` sees what a plausible-looking diff does not.

**FOUR MORE modules resolved data files against their own folder.**
After `off1.mlt` and `out.mlt` were fixed, an audit of the whole project found
the same pattern in four more places — the defect had been reimplemented once
per file, and each copy was wrong the same way:

| site | resolved | what it did in a frozen build |
|---|---|---|
| `app.py` | the first-run wizard's cfg | wrote into `_internal` — settings a user cannot find and a reinstall deletes |
| `ui/main_window.py` | `off1.mlt`, `out.mlt` | ignored a user's own hull beside the exe |
| `ui/manual_hull_tab.py` | `in.mlt` | pointed at a file that did not ship at all |
| `reporting/latex_report.py` | `out.mlt`, `off1.mlt` | the REPORT could document a different hull than the sweep analysed |

The proximate cause throughout was a variable called `cwd` holding
`os.path.dirname(os.path.abspath(__file__))` — the MODULE directory, which
under PyInstaller is `_internal`. **The name invited the mistake and the
mistake was made six times.** It is now `module_dir`, and its only remaining
use is the unfrozen default for `reports_dir`.

`asset_paths.py` is now the ONE resolver: beside the executable, then the
working directory, then the bundled copy LAST — a bundled default outranking a
user's own file would be the same defect in new clothes. `main.hull_offsets_path`
is a thin alias and a test asserts it delegates rather than keeping a private
copy.

**A startup self-check now prints every data file and where it came from**, on
both entry points, unconditionally — because the failure mode here is output
that looks RIGHT, so there is no condition to trigger on. A missing file prints
NOT FOUND, every location searched, and what that file supplies.
`tests/test_asset_paths.py` greps the four modules above for any line naming a
data file alongside `__file__`, so a seventh occurrence fails the suite.

**`torch` is EXCLUDED from the bundle. It blocked six builds.**
The frozen bundle was 4.3 GB, of which **3.5 GB was torch** — a library nothing
in this project imports. PyInstaller was OOM-killed inside `hook-torch.py`
(which walks and collects the whole library) on six separate build attempts,
including one that died mid-COLLECT leaving both `.exe` files written and
`_internal` a gigabyte short, so the dist *looked* finished and the CLI died
mid-import. With `"torch", "torchvision"` in `excludes`, the bundle is
**786 MB** and both executables built first time.

**WHERE IT CAME FROM — and the obvious guess is wrong.** It is **scipy**, not
cadquery, and not `collect_submodules`. (An earlier note in this file blamed
`collect_submodules("cadquery")`; that was incorrect and is corrected here.
Widening or narrowing that collect has no effect on torch whatever.) Measured
across the bundled tree:

| package | files importing torch |
|---|---|
| cadquery, OCP, pymoo, vtkmodules, pandas, matplotlib, numpy, fpdf, yaml, PyQt6 | **0** |
| **scipy** | **8** |

They are all in `scipy/_lib/array_api_compat/torch/` — one of scipy's Array API
backend shims, alongside `numpy/`, `cupy/` and `dask/`. Those modules do
`from torch import *` at module level, so static analysis follows them into the
bundle.

**Both reachable references are provably dead in this application:**

* `common/_helpers.py:40` is inside `if TYPE_CHECKING:` — it never executes.
* `common/_helpers.py:594` sits under `elif is_torch_array(x):` — scipy imports
  torch only when it is *handed an actual torch tensor*. This suite passes
  numpy arrays everywhere, and nothing in it constructs a tensor.

Verified independently: `torch` is absent from `sys.modules` after importing
every runtime dependency, and no project source file imports it.

*Before removing this exclusion*, check whether anything now passes torch
tensors to scipy. "scipy's array API broke" is not by itself a reason — the
exclusion only removes a backend this code cannot reach.

**VERIFIED, two ways.**

*Bit-for-bit, source vs trimmed frozen*, same grid, same settings:

| field | source (torch installed) | frozen (torch absent) |
|---|---|---|
| weight_lbs | 269.6296966384532 | 269.6296966384532 |
| kupfer_dc | 0.24619189914537834 | 0.24619189914537834 |
| depth_in | 15.57035257349158 | 15.57035257349158 |
| pour_fracs | 0.2569 / 0.7707 | 0.2569 / 0.7707 |
| zone_densities | 69.5 / 65.5 / 69.5 | 69.5 / 65.5 / 69.5 |

Config fingerprint `24e560f9` on both sides. This is the STRONG form: the
source run had torch installed and importable the whole time and never reached
for it; the frozen run does not contain it; same answer to every digit.

*And the paths a sweep never touches* — Race Simulator, Hull Configurator, the
Michell drag path, report generation, and STEP/STL export through cadquery,
OCP and `step_export_service` — all run from source with torch available:
`torch in sys.modules` is **False** afterwards. cadquery was the original
(wrong) suspect and is the heaviest thing in the bundle; it never touches it.

*Residual risk:* a lazy import on a path neither the suite, a sweep, nor the
five exercised GUI paths reach. Bounded to "does anything hand scipy a torch
tensor", and nothing here constructs one.

**`reporting/report_generator.py` is dead code and is NOT in the frozen build.**
The fpdf2 `SubmittalReport` path has no importer anywhere in `ui/`, `services/`,
`app.py` or `main.py` — only `report_generator.py`, a compatibility shim that
nothing imports either. `tests/test_report.py` exercises
`reporting.latex_report` exclusively, and PyInstaller's analysis found no import
path to it, so the shipped binary does not contain it at all. It still imports
and still runs under pytest, so the re-widened `_add_hull_section` handler is
correct — it just guards a module the product does not use.
*To close:* delete `reporting/report_generator.py` and the shim, or wire one of
them to something. Deleting is the honest option; pick it deliberately rather
than by neglect, and drop the pinned case from
`tests/test_broad_catch_warrants.py` at the same time.

**A partial grid no longer reports a winner.**
If some sweep tasks raise and others survive, the run used to print a warning
and carry on — and the banner scrolls past while the number is what gets
written down. A winner from a grid that was never fully searched is not a wrong
calculation; it is a correct calculation of the wrong thing, which reads
identically on the page. It now raises, naming `ACCEPT_PARTIAL_GRID=True` as
the deliberate way past. (An assertion with no way past it blocks real work;
one that is easy to trip past is decoration.)

**The headless CLI died on its own banner (cp1252).**
The first run of `jaguar-cli.exe` failed on line one:

```
print("🛶 ASCE CONCRETE CANOE: 5-DIMENSIONAL PARAMETRIC SWEEP")
UnicodeEncodeError: 'charmap' codec can't encode character '🛶'
```

A frozen console build on Windows gets a **cp1252** stdout, and `main.py`
prints emoji, box-drawing and block characters throughout -- the executor
banner, the New Best lines, the phase markers, the winner header. Both
executors, every run, before any work began.

Notable because the hazard had already been met once and under-fixed:
`PhaseClock.report()` was deliberately written in ASCII, with a docstring
saying box-drawing characters raise on cp1252. That fixed one function. The
STREAM was the right place all along, and the evidence was already in hand.

`main.harden_stdio()` now reconfigures stdout/stderr to UTF-8 **with
`errors="replace"`** before the first print. The `errors` half is the half
that matters: UTF-8 alone still raises on a glyph the console cannot render,
and a sweep must never be lost because a decorative character did not fit the
code page -- a `?` in a banner costs nothing, a dead run costs hours. Pinned by
`tests/test_console_encoding.py`, which includes a positive control that the
cp1252 fixture really does reject an emoji (otherwise the file would pass
while testing nothing) and an AST check that hardening happens BEFORE the
sweep starts, since hardening after the first print fixes nothing.

**Do not build into a OneDrive-synced folder.**
`PyInstaller` fails with `PermissionError: [WinError 5]` deleting the previous
`dist/`. Build to `%TEMP%` and copy the result out.

*Build the two executables SEPARATELY.* `packaging/jaguar.spec` (GUI) and
`packaging/jaguar_cli.spec` (headless) were briefly one spec with two Analysis
passes, which is tidier and got the build **killed by the OOM reaper during
COLLECT** on a 32 GB machine. That failure is dangerous rather than merely
annoying: both `.exe` files had already been written, so the dist *looked*
finished while `_internal` was a gigabyte short, and the CLI died mid-import.
Sequential builds halve peak memory and fail honestly. Build the GUI, build the
CLI, then copy `jaguar-cli.exe` into the GUI bundle -- `main.py`'s imports are
a subset of `app.py`'s, so the GUI's `_internal` is already a superset. Verify
by RUNNING the copied exe, not by seeing it present; that is exactly the check
the OOM failure would have defeated.

*And watch the path separators.* In Git Bash, `--distpath C:\jaguar_build\dist`
arrives at PyInstaller as `C:jaguar_builddist` — both backslashes are eaten,
and `C:name` is DRIVE-RELATIVE, so the build silently lands in the current
directory, which is this repo, inside OneDrive. 4.3 GB of it. The build
succeeds and reports success; only the final "results are available in" line
says where it actually went. **Use forward slashes:**
`--distpath C:/jaguar_build/dist --workpath C:/jaguar_build/work`.

---

---

## Performance

**THE SPEED PROBLEM WAS NEVER THE SEARCH.** Read this before optimising
anything in the sweep.

With the process executor and the trapezoid hoist, a real 161-station sweep
searches in about **3 minutes**. A measured run:

| phase | time | share |
|---|---|---|
| search | 185.6 s | 6.3% |
| refinement gate | 6.0 s | 0.2% |
| winner verification | 1.9 s | 0.1% |
| **mix-development pricing** | **2735.2 s** | **93.3%** |
| TOTAL | 2931.5 s | 49 min |

Verification costs **8 seconds combined** — 0.3% — and is the only thing
standing between a user and an unverified winner. It is not optional and must
not be made optional: a switch that disables a correctness check for no
measurable gain is a switch someone eventually flips.

Pricing is 93% because it runs the whole search FIVE more times (the mixes on
record, then each target alone, then cumulatively) over a grid that is
legitimately LARGER than the search's. It is now **OFF by default** — a
checkbox in the Structural tab and `--price-mix-development` on the CLI — and
when off it prints what it would have done, so an absent payoff table can never
read as "nothing to develop".

**TWO OPTIMISATIONS WERE TRIED AND BOTH FAILED. Do not re-derive them.**

*1. "Pass `density_step_pcf` through to the pricing sweeps."* Looks obviously
right: the search honours the step, pricing appears to ignore it, and pricing's
grid is ~4x larger (194 combos from 22 real densities, against 54 from an
11-point 2 pcf grid). It is **wrong**. Pricing runs with `existing_only`, and
`get_structural_sweep_grid`'s docstring says so explicitly: *"step is
meaningless in this mode and is ignored"*. The pool is the team's REAL mixes.
Snapping them to a 2 pcf grid would assert a mix measured at 61.2 pcf is a mix
at 62.0 — one they do not own. The grid is bigger because real mixes pack
tighter than a stepped grid, which is not a defect. The ≥2 pcf pour-floor
filter is already applied on both paths.

*2. "Seed the companion with a prune bound."* Also looks obviously right, and
is genuinely admissible: every pricing pool is a superset of the mixes on
record, so the buildable-fallback weight is achievable in all of them and can
license an early stop. MEASURED: **0.0%** on a feasible pool (740 configs
before and after) and **2.6%** on an infeasible one (1940 -> 1890). It does
nothing because the combo walk is ALREADY ordered ascending by `min(combo)` and
the lightest design uses the lightest mix — so the incumbent is set on the
first feasible combo and is immediately as tight as any external bound. The
only case it helps is a pool with no feasible design at all, where the
incumbent prune never fires; 2.6% does not pay for a branch in the hot loop.
Reverted.

What survived from that attempt, because it stands on its own: the payoff table
now distinguishes **three** outcomes — a better design, "no improvement on
X lb" (buildable, not lighter), and "no feasible design" (the physics refused)
— where two of them used to print the same number with no way to tell why.

**A labelling trap in the payoff table, fixed.** The final cumulative row
prices the real mixes on record PLUS the targets. The row beneath it was
labelled "all targets developed" but comes from the PARENT search over the
continuous stepped grid, which contains interpolated densities nobody has
batched. Two pools, two numbers, and a reader would reasonably take them for
the same thing. It now reads "full density grid (interpolated, not just real
mixes)".

**The 8-start seating solve is ~99% of the search, and it scales with TASKS,
not configs.**
Measured on one 50-config / 2-task grid, identical except for the seating:

| | search | pricing | total |
|---|---|---|---|
| as shipped (SLSQP per task) | **589.4 s** | 796.0 s | **1390.8 s** |
| seating pinned (no SLSQP) | **4.9 s** | 277.2 s | **286.4 s** |

The solve runs once per TASK — one `(thickness, depth, gunwale_w, gunwale_h)` —
and costs roughly the same regardless of how many configs that task contains.
So its share of a run is set entirely by **configs-per-task**. A profiled task
holding 7,030 configs showed it as 22% of the task; a 25-config task shows it as
99% of the search. **Do not quote a percentage without saying how many configs
per task it was measured on** — that is exactly how this was misread once.

Cost per solve: 8 starts x SLSQP(14 variables, `eps=1e-3` forward differences,
`maxiter=200`), each objective evaluation enveloping 5 crew cases. Profiled at
203 s per task at 41 stations.

**The solve CANNOT be hoisted to once per run: its answer depends on depth.**
Measured per task inside one run (one worker, each solve tagged to the geometry
that produced it), thickness 0.5 in:

| depth (in) | objective | max seat movement vs 12.570 |
|---|---|---|
| 12.570 | 161.49 | — |
| 14.070 | 167.98 | **1.84 ft (22 in)** |
| 15.570 | 177.63 | 1.45 ft (17 in) |

Seats move nearly two feet across the depth range — orders of magnitude beyond
any placement tolerance. Since the solve already runs once per
`(thickness, depth, gunwale_w, gunwale_h)`, a grid that varies only depth is
**already minimal** and no count reduction is available.

*A METHOD WARNING, recorded because it nearly produced the opposite
conclusion.* The first version of this measurement compared the best-of-N
seating across three separate RUNS, each spanning a depth RANGE, and reported
all three byte-identical — apparently proving depth-independence. It proved
nothing: each run took the argmin over every task it contained, so it kept
selecting the same depth's solution. Any measurement of a per-task quantity
must be attributed to the task that produced it, not to the run.

Seating moves with THICKNESS too, measured in one run at one depth (12.570)
so the comparison is apples-to-apples: 0.375 -> 0.500 in moves seats
**1.54 ft (18 in)**. So the solve cannot be shared across thicknesses either.
It already runs once per `(thickness, depth, gunwale_w, gunwale_h)` and that is
minimal on every axis measured.

**Reproducibility is sound.** The same grid run twice gives seats identical to
`0.000000 ft`. `seating_start_points` re-seeds `default_rng` on every call, so
task order does not perturb it.

*What is left:* reduce the cost of each solve rather than the number of them.
The SLSQP objective bottoms out in `calculate_moment_envelope` ->
`_solve_beam_case` -> `np.trapz`, so the hot-path fixes below speed up the
seating solve AND the mix loop. (`kupfer_combined_gate` is called only from
`evaluate_multizone_weight`, never from the seating objective — verified from
the profile's caller table — so Kupfer work helps the mix loop only.)

**A failing sweep evaluates the whole grid.**
`main.py:1638` prunes with `if best_mz is not None and
min_density_weight_bound(...) >= best_mz[0]: break` — the prune needs an
INCUMBENT. A sweep where nothing is feasible never sets one, so nothing is ever
pruned; this is a reading of the code, and the 0% is not in dispute.

Measured prune fractions, as the sweep reports them itself:

| run | evaluated | pruned |
|---|---|---|
| 50-config feasible grid | 50 | 10 (**17%**) |
| its nested pricing sweep | 2,680 | 1,200 (**31%**) |
| any sweep with no feasible design | whole grid | **0%** |

*How much the freeboard prune below would actually save has NOT been measured.*
It depends on how much of the grid is ruled out by weight alone, which varies
with the density range.

*To close:* `min_density_weight_bound(combo, V)` is an admissible LOWER bound on
hull weight, and `_hull_wt_max` (`main.py:1412`) is a hard upper limit already
enforced inside every evaluation. When the lower bound exceeds the ceiling every
stack from that combo is already certain to be rejected, and because the walk is
ordered ascending by `min(combo)` and the bound is monotone in it, so is every
later combo — so it can `break` with **no incumbent**. Prunes only what would
have been rejected on the same criterion. Fires when the failure mode is
weight/freeboard, not when it is Kupfer stress.

**The hot path is many tiny numpy calls, not big array work.**
From one profiled task (941 s, 7,030 configs, 41 stations):

| | calls | cum | share |
|---|---|---|---|
| `_solve_beam_case` | 1,761,560 | 549.5 s | 58.4% |
| `np.trapz` | **12,330,920** | 194.2 s | 20.6% |
| `_deposit_patch` | 5,719,728 | 171.5 s | 18.2% |
| Kupfer gate + utilisation + principal stresses | 5,830 / 1.87M / 1.87M | — | 28.4% self |
| `cumulative_trapezoid` | 3,523,120 | 103.8 s | 11.0% |

`np.trapz` recomputes `np.diff(x)` on every one of its 12.3 M calls (15.9 M
`diff` calls, 79.4 s) against a station grid that is **constant for the whole
task**. `kupfer_combined_gate` makes ~320 per-station Python calls per
invocation. Both are hoistable/vectorisable with the same arithmetic; the trapz
change reassociates the sum, so it is not bit-identical (~1e-16 relative).

This shape also means the GIL is held for most of the run, so **thread workers
may be buying little or nothing** — and frozen builds are forced onto threads
(see Packaging). NOT YET MEASURED: an attempt collapsed to a single-task grid
and measured nothing. Measure before assuming either way.

**Every run now reports its own phase timing.** `main.PhaseClock` prints a
`WHERE THE TIME WENT` table on both the success path and the no-feasible-design
path, with the task count beside the config count. It exists because a two-hour
run could not previously say where its two hours went, and because the
configs-per-task ratio above cannot be reconstructed after the fact.

## Test suite

**Full-suite runtime varies enormously and unpredictably — unexplained.**
Whoever runs the release gate needs to know it may take twenty minutes or three
hours. Four measured runs, same machine, same tree:

| Run | Files | Time |
|---|---|---|
| forward, `--ignore=test_multizone` | 57 | **0:20:02** |
| reversed file order | 57 | **2:09:39** |
| first half (incl. `test_multizone`) | 29 | **0:11:30** |
| second half | 29 | **2:26:33** |
| full single pass, all files | 58 | **3:08:45** |
| full single pass, all files (green) | 58 | **9:05:15** |
| `test_multizone.py` ALONE | 1 | **2:12:34** |

**A 9-hour worst case makes this a process problem, not a curiosity.** Nobody
can gate a release on a run whose duration is unpredictable across a 47x range
on identical hardware and an identical tree.

The last row is the one that kills every easy explanation: `test_multizone.py`
run ALONE took longer (2:12:34) than the entire 29-file half that contained it
(0:11:30). So it is not collection order, not the session-scoped `small_sweep`
fixture (which builds once either way), and not which other tests run alongside.
Something inside that file's own execution varies by an order of magnitude
between runs — most likely a solver iteration count that depends on state we
have not identified.

*Not yet measured:* no run so far was captured with `--durations`, so which
files dominate a SLOW run is still unknown. Capture it on the next full run:

```
python -m pytest tests/ -q --durations=25 > durations.log 2>&1
```

Then compare a fast run against a slow one. Until that exists, **budget nine
hours** for a release gate.

**`test_paddler_jacobian` takes 8 m 23 s; `test_report` and `test_multizone`
each exceed 7 min alone.** They complete — nothing hangs. `test_report` alone
has now been measured at both ~7 min and **14 m 25 s** on the same tree, so the
unexplained variance above is not confined to `test_multizone`. `test_multizone` has
been excluded from most runs for time, not correctness.

**10 tests SKIP: hull drag is NOT covered by the automated suite.**
`TestGodzillaDragComparison` cross-checks the Python Michell engine against
Godzilla over 10 representative hulls (criterion: mean Rw error <= 5% per hull).
It is skipped whenever the Godzilla runtime is absent, and **`mlt933w.exe` has
no headless batch mode**, so it cannot be driven automatically — the skip is
structural, not a missing dependency somebody can install.

Consequence, stated plainly so nobody assumes otherwise: **a green suite says
nothing about hull drag.** The wave-resistance path is exercised only by unit
tests of its own internals, never against the reference implementation. If a
drag number looks wrong, the suite will not have caught it. Validate drag by
hand against Godzilla before trusting a resistance figure.

**Four sentinel sites are ACCEPTED, not fixed.**
`main.py::_seat_pin_cost`, `main.py::_top_flange_stability_trade`,
`validation_suite.py::parse_godzilla_drag_output`,
`validation_suite.py::run_godzilla_engine` — each returns a falsy value from a
broad handler. All degrade *output* (a report section, a validation run), never
a design number. Reviewed and priced; see `tests/test_sentinel_collisions.py`.

---

## Fixed after the first release candidate

**The gate rejected every candidate on a converging design.**
`extrapolate_kupfer_tail` fits a geometric tail from `r = d2/d1`. When the D/C
FELL from n to 2n and ticked up at 4n, `d1 <= 0` made the ratio meaningless and
the code reported "increments are not decaying" — the branch written for a
sequence that *climbs past the limit*. On a real multizone winner sitting at
**D/C 0.28 against a 0.95 limit**, all ten survivors were refused and every run
printed "do not build to it".

That cleared the release cut line despite producing no wrong number, on the
CLAUDE.md rule that a disabled warning is worse than a wrong number: a warning
that always fires is one the team learns to skip within a week, and then the
one time it is real nobody reads it.

Now a non-monotone sequence is BOUNDED rather than fitted —
`max(observed) + largest positive step` — which is conservative, is recorded as
a bound rather than a limit, and still refuses genuine divergence (`d1 > 0` and
`r >= 1`). A dip-then-leap bounds high enough that the gate rejects it on the
value instead: the extrapolator bounds, the gate decides. Both directions are
pinned by test so the fix cannot become a rubber stamp.

**A design's weight depended on which OTHER thicknesses were in the grid.**
`main.py` resolved the stem overhang once per sweep at
`min(thickness_options_in)` and applied it to every task. The threshold is
`1.5*t`, so the overhang is a function of thickness (1.045 in at t=0.375,
1.394 in at t=0.500) — a thick design was evaluated with a thin design's tip
geometry whenever both were in the grid. Measured, same `(t=0.500,
depth=12.570)` design:

| grid | before | after |
|---|---|---|
| `[0.5]` | 161.4949 | 161.4949 |
| `[0.375, 0.5, 0.625]` | **160.4491** | **161.4949** |
| seat deviation | 2.08 ft | **0.000000 ft** |

Neither number was *wrong for its own assumption*; what was wrong is that the
assumption came from the grid rather than the design, so no weight could be
compared across sweeps.

The sweep-wide rule was deliberate — `geometry.stem_overhang_ft` argues for it,
since the shortest overhang is the least tip mass and tip mass relieves the
governing midship sagging. That conservatism existed *only* because one value
had to serve every thickness. Resolved per task, each design gets its own true
overhang and the compensation is unnecessary.

`config.STEM_OVERHANG_BY_T` is published once, before any worker starts, and
read-only thereafter — a per-task WRITE to `config.STEM_OVERHANG_IN` would have
raced across the thread pool. It is in `RUN_LEVEL_CONFIG_ATTRS`, so it reaches
spawned workers and joins the fingerprint; without that a worker would find no
map, fall back to the scalar, and silently reinstate the sweep-wide value while
every test passed. A map missing the requested thickness RAISES rather than
borrowing a neighbour's.

**This changed results, deliberately.** Thicker designs now get more tip relief
and come out marginally lighter: t=0.625 moved 190.2750 -> 189.2698 on the
seating objective. t=0.375 is unchanged, being the grid minimum that was always
correct. Weights will not match runs made before this fix. Pinned by
`tests/test_stem_overhang_per_task.py` (9 tests), which asserts the premise
(the overhang really does vary with t) so it cannot silently pass if that stops
being true.

**The refinement gate crashed on a documented setting.** `LONGITUDINAL_STATIONS`
is `Optional[int]` on `OptimizationConfig`, where `None` legitimately means "use
the offset file's native station count". The gate's banner read it with
`int(getattr(config, "LONGITUDINAL_STATIONS", 161))` — a default that answers
"is it missing?" while the caller needed "is it usable?", so a present-and-None
value reached `int()` and raised `TypeError`. The gate's own guard then
correctly converted that to `RuntimeError` rather than skipping, which is how it
surfaced: four `test_multizone` failures in a single full pass, invisible to
halved runs. Replaced with `main.config_int()`, which handles absent and None
alike and names the offending attribute if the value is unusable.

**The Richardson weight extrapolates in whichever direction the last step
points.** `w_converged = w_2n + (w_2n - w_n)` doubles the final increment, so a
weight that FELL with refinement would extrapolate further down and understate
the converged value. Every measurement so far has risen (+2.016 lb on the
smoke-test design, +0.49 lb on the pinned one), so it is not biting — but it is
the same "assumes a direction" family as the non-monotone D/C bug fixed below,
and it has no guard. *To close:* bound it the way the D/C extrapolator now
does, rather than assuming monotone growth.

## Data

**The f'c fit has 2 age-confirmed points; the tensile lab fit has 0.**
The tensile tier falls back to the seeded `mixes` table, which is design intent
rather than measurement. Treat hull weights as **relative comparisons** until
lab data lands. The report states the point count; the GUI banner repeats it.

**A zero in a strength column is stored as 0 and then filtered.**
`_fit_tier` only takes points `> 0`, so a typed zero never reaches a
regression — but it is visible in the Lab Results table as `0.0`, which reads
like a measurement. Cosmetic.
