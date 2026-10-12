<p align="center">
  <img src="docs/jaguar.png" alt="JAGUAR — a jaguar sitting in a concrete canoe" width="180">
</p>

<h1 align="center">JAGUAR</h1>

<p align="center">
  <strong>J</strong>ames <strong>A</strong>ndrea <strong>G</strong>rappe's <strong>U</strong>tility for <strong>A</strong>quatic <strong>R</strong>esistance<br>
  <em>The structural design suite for the University of Virginia's ASCE Concrete Canoe team.</em>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-the-structural-sweep-actually-does">How it works</a> ·
  <a href="#the-command-line">CLI</a> ·
  <a href="#configuration">Config</a> ·
  <a href="#repository-layout">Layout</a> ·
  <a href="README_TEAM.md">Team guide</a> ·
  <a href="KNOWN_ISSUES.md">Known issues</a>
</p>

---

## The competition

The **ASCE Concrete Canoe Competition** asks civil engineering students to do
something that sounds like a joke and is actually a materials-and-structures
problem: design, mix, cast, and then *race* a canoe made of concrete.

A team is judged on four roughly equal parts — a **technical design paper**, an
**oral presentation**, the **finished canoe** itself, and the **races**. To score
at all, the canoe has to survive being paddled; to score well, it has to be light
enough to be fast. Those two demands pull in opposite directions, and that
tension is the entire engineering problem:

| The rules force… | Which means… |
|---|---|
| The hull is **concrete** — strong in compression, weak in tension | The governing limit state is *cracking*, not crushing |
| The canoe must float and keep **freeboard under crew load** | Lower density buys buoyancy and costs strength |
| Paddlers **kneel directly on the hull** | Concentrated patch loads on a thin unreinforced shell |
| The races are sprints and slaloms | Weight is time; every pound is scoreboard |

So the design question is never "will it work?" It is: **what is the lightest
wall thickness, hull depth, and set of concrete mixes that still passes every
stress check, at every paddler arrangement, for every race?**

That is a search over thousands of combinations, each needing a full beam
analysis of the hull. Done by hand it means checking three or four candidates
and hoping. JAGUAR checks all of them.

---

## What JAGUAR is

A desktop application (PyQt6) and a headless CLI that together do the structural
engineering for a concrete canoe:

* **Searches** the design space — wall thickness × hull depth × mix densities ×
  pour-line placement × gunwale geometry — for the **lightest hull that passes**.
* **Checks** every candidate against the **Kupfer–Gerstle biaxial stress
  envelope** under all five ASCE race categories, with the crew placed by an
  SLSQP solve rather than assumed.
* **Verifies** its own answer by re-running the winner at double the
  longitudinal resolution, and refuses to certify a design the refinement gate
  does not confirm.
* **Learns from your lab** — density-to-strength regressions are fitted to the
  cylinders and beams your team actually broke, imported from the lab
  spreadsheet, not taken from a textbook table.
* **Reports** — moment/shear envelopes, pour-zone diagrams, a PDF design report,
  and STEP/STL export of the finished hull for CAD and for the mold.
* **Says when it doesn't know.** A two-point strength fit, a winner pinned to the
  edge of your search range, a config file that failed to load, an unverified
  refinement gate — each raises a red panel with the consequence spelled out,
  because a plausible wrong number is worse than a visible gap.

It also carries the rest of the design workflow: a **Race Simulator** (Michell
wave resistance, genetic-algorithm course optimisation) and a **Hull
Configurator** driving the Fortran *Godzilla* (`mlt933w.exe`) potential-flow
solver.

---

## Quick start

### For the team — you just want to run it

Download the whole **`JAGUAR`** folder from the release (not just the `.exe` — it
needs `_internal/` beside it), put it somewhere like `C:\JAGUAR`, and
double-click `JAGUAR.exe`. No install, no Python.

> **Two places it must not go:** inside OneDrive (files can be made online-only
> and the app then fails in confusing ways) and inside `C:\Program Files\` (the
> app writes hull geometry next to itself, and the permission error surfaces as
> a geometry error, so the real cause is invisible).

Then read **[`README_TEAM.md`](README_TEAM.md)** — it covers importing lab data,
what each warning means, and *why you should not quote an absolute hull weight
yet*.

### For developers — you want to change it

```bash
git clone https://github.com/JamesGrappe/JAGUAR.git
cd JAGUAR
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -r requirements.txt

python app.py          # the GUI
python main.py --help  # the headless sweep
pytest tests/          # 69 test modules
```

Python **3.11+**. The pinned set is in `requirements.lock`.

On launch the app prints a **startup self-check** naming every data file it
resolved and where that file came from:

```
DATA FILES IN USE
  canoe_suite.cfg    C:\JAGUAR\_internal\canoe_suite.cfg
  concrete_mixes.db  C:\JAGUAR\_internal\concrete_mixes.db
  off1.mlt           C:\JAGUAR\_internal\off1.mlt
  out.mlt            C:\JAGUAR\_internal\out.mlt
```

That block exists because five shipped bugs had one cause: an asset resolved
against the wrong directory, so the app silently fell back to a default — a
generic 20 ft hull, an empty mix database, built-in settings — and returned a
number that looked entirely reasonable. **If a line says `NOT FOUND`, stop and
fix it before trusting anything downstream.**

---

## The four tabs

| Tab | What it does |
|---|---|
| **Race Simulator** | Michell-integral wave resistance plus a genetic algorithm over hull parameters and paddling strategy, scored across the race slate. Outputs course times and a racing line. |
| **Hull Configurator** | Drives the Godzilla Fortran solver to generate hull offsets (`off1.mlt` / `out.mlt`), with a live 3-D preview. |
| **Structural Sweep** | The core: the parametric search, the refinement gate, the moment/shear envelopes, the pour-zone layout, the PDF report. |
| **Mix Lab** | Your lab results. Import the compressive and flexural CSVs, see which rows reached the strength fit and which were excluded, and publish the regression the sweep will use. |

---

## What the structural sweep actually does

### 1. It builds a grid of buildable designs

Not a continuous optimisation — a search over things you can actually cast. Wall
thicknesses are restricted to the **1/32 in ∪ 0.5 mm** grid, divided by the
number of trowel layers, so every candidate is a wall the team can build
repeatably. Depth, mix densities and pour-line positions vary on their own axes.
A three-zone multizone run searches a strict **I-beam** section: a denser,
stronger mix in the flanges and a light mix in the web.

### 2. It loads each design the way a race loads it

* **All five 2026 race categories** — 2M, 2F, 3M, 3F and the 4-person coed — run
  as separate load cases, with per-paddler sex, stature and weight feeding an
  anthropometric model of the crew centre of gravity.
* **Seating arrangement moves the bending envelope by ~40% on hogging**, so the
  sweep does not trust the order written in the config. It takes a per-station
  maximum over **40 distinct arrangements** and solves paddler placement with
  SLSQP from **8 deterministic seeded starts**.
* Paddlers kneel, so the load is a **3-inch contact patch**, never a point. A
  point load is a singularity — its shear jumps and its moment kinks, and the
  governing utilisation then *does not converge* under refinement (measured:
  non-monotone, and rejecting the design at 321+ stations). With the patch,
  increments decay cleanly.

### 3. It gates on the right failure criterion

Concrete under combined stress fails on the **Kupfer–Gerstle biaxial envelope**,
not on a uniaxial allowable. Loads and capacities are factored per **LRFD**
(ASCE 7-22 load factors; φ = 0.60, the plain-concrete tension value from ACI
318-19, not the 0.90 that belongs to tension-controlled reinforced sections).
ASD is available as a regression baseline. The failure criterion is identical
under both.

### 4. It refuses to certify a number it hasn't re-checked

This is the part most tools skip. The governing utilisation **drifts upward as
the longitudinal grid is refined** — measured on five real designs on the live
hull:

| Refinement step | Mean drift | Worst design |
|---|---|---|
| 41 → 81 stations | 4.88% | 11.39% |
| 81 → 161 | 1.52% | 4.13% |
| 161 → 321 | 1.06% | 2.19% |
| 321 → 641 | 1.00% | 2.88% |

The increments decay but never plateau out to 641 stations. So a design accepted
at D/C = 1.0 on the 161-station search grid can *fail* one refinement step
(measured: 0.9957 at 161 → 1.0040 at 321). JAGUAR therefore accepts at **0.95**,
searches at **0.95 / 1.0219 = 0.930** using the *worst* observed drift rather
than the mean, re-runs the winner at double resolution, and reports a
**Richardson-extrapolated** converged weight. A winner the gate does not confirm
is labelled as such rather than quietly shipped.

That 0.95 is a **numerical margin, not a safety factor** — the factor of safety
covers the physics and is applied separately.

### 5. It says what it's doing while it does it

A long sweep announces its phases before the wait, not after:

```
This run will do: search; converged weight; refinement gate;
winner verification; mix-development pricing -- SKIPPED
(--price-mix-development to enable).
```

and prints a phase timing table at the end. Mix-development pricing is **off by
default** because it was measured at **93% of a run's wall time** (2735 s of
2931 s) for information most runs don't need. Winner verification stays **on
always** — it costs 8 seconds, and a switch that turns off a correctness check
for no measurable gain is a switch someone will eventually flip.

---

## The command line

The GUI and the CLI run the *same* sweep. The frozen build ships both
`JAGUAR.exe` (windowed) and `jaguar-cli.exe` (console).

```bash
python main.py --multizone --stations 161 --thickness 0.75 \
               --depth-step 1.5 --depth-max 15.57 --density-step 2.0 \
               --json winner.json
```

| Flag | Effect |
|---|---|
| `--multizone` / `--zones N` | Gated 3-zone I-beam sweep (the real one) |
| `--ungated-ok` | The 2-mix path, explicitly **not** refinement verified |
| `--stations N` | Longitudinal resolution (41 … 2561) |
| `--thickness` `--depth-step` `--depth-max` `--density-step` `--freeboard` | Bound the search grid |
| `--seating-starts N` | SLSQP restarts for paddler placement |
| `--executor thread\|process` | Concurrency model (see below) |
| `--price-mix-development` | Enable the slow mix-development pricing phase |
| `--json PATH` | Write the winner's numbers, so two runs can be diffed exactly rather than compared by eye |

Plain `python main.py` **refuses to run**: the default 2-mix path has no
refinement gate and should never be the accidental choice.

### Thread pool vs process pool

Processes escape the GIL and are **measured 6.52× faster** (1309 s against
8533 s on an identical 36-task, 161-station grid) with a **bit-identical
winner** — same weight to every digit, same config fingerprint. Each spawned
worker re-imports config fresh, so a run's settings reach it only through a
fingerprinted `RunState` carried in the task, which the worker verifies before
doing any physics: a mismatch fails the task loudly instead of returning a
plausible weight for the wrong design.

A stall watchdog (`SWEEP_STALL_TIMEOUT_S`, default 1800 s) tears the pool down
and restarts the search on threads if no task finishes in time, and says why
rather than hanging silently. The GUI-path verification protocol is in
[`README_TEAM.md` §8](README_TEAM.md).

---

## Configuration

**Everything lives in [`canoe_suite.cfg`](canoe_suite.cfg) (YAML).** Never edit a
Python file to change a parameter — the config is read at import through
`config/settings.py`, and the matching UI field is seeded from it.

Every key in that file is live, heavily commented, and carries its own
provenance. A sample of what it controls:

```yaml
structural:
  factor_of_safety: 1.5
  kupfer_acceptance_max: 0.95        # the numerical margin, not a safety factor
  kupfer_drift_per_doubling: 0.0219  # MEASURED, worst case at 161 -> 321
  design_method: LRFD                # or ASD
  thickness_options_in: [0.375, 0.5, 0.625, 0.75]
  freeboard_min_in: 6.0
crew:
  race_categories:
    "4_coed": {sexes: [M, M, F, F], heights_in: [70, 70, 65, 65],
               weights_lb: [175, 165, 145, 135], seat_pins: [null, null, null, null]}
mix:
  min_density_pcf: 60.0              # your manufacturing floor; it bounds the sweep
  design_age_days: 28                # younger breaks are logged but held out of the fit
```

The comments are not decoration. Where a number was measured, the measurement is
written beside it; where a number is an assumption, it says so, along with what
it would take to replace it. Read them before changing anything.

---

## Repository layout

Strict layering — do not bypass it.

```
app.py                 GUI entry point
main.py                headless sweep + CLI
asset_paths.py         the ONE data-file resolver (beside the exe -> cwd -> bundled)
structural.py          beam theory, stress envelopes, pour zones, SLSQP placement
geometry.py            Godzilla offset parsing, shell section properties
canoe_physics.py       resistance and race performance
mix_database.py        SQLite lab results and the density->strength fits

ui/          PyQt6. Thin wrappers over services. No business logic.
services/    Orchestration, subprocesses, worker threads. No Qt imports.
domain/      Dataclasses and unit conversions. No I/O, no Qt.
physics/     Pure math: Michell integral, biomechanics, race path.
reporting/   ASCE compliance checks and fpdf2 report generation.
data/        SQLite, CSV import, session persistence.
config/      canoe_suite.cfg loading, constants, worker RunState fingerprinting.
tests/       69 test modules.
packaging/   PyInstaller specs and the build procedure.
docs/        Roadmap and the in-app help topics.
```

Three things to know before you edit:

* **Godzilla I/O is brittle.** `mlt933w.exe` is Fortran. When touching
  `services/hull_service.py::write_seed_hull` you must preserve exact character
  columns, exact line counts and CRLF endings. Never use variable-length float
  formatting.
* **`config.RACE_4_PERSON` and its siblings are derived on read** via a PEP 562
  module `__getattr__`. Never `monkeypatch.setattr` them — patch
  `RACE_CATEGORIES`, the single source of truth. A stored copy re-creates a
  crew-weight split-brain this suite already removed once.
* **A sentinel is safe only when the failure value and the legitimate value
  point the same way.** `GM_T = 0.0` did not: every gate read `if gmt > 1e-9`, so
  the failure value *skipped* the stability check instead of failing it. When a
  value cannot be computed, callers must fail the gate, never skip it.

---

## Tests

```bash
pytest tests/              # everything
pytest -s -k census        # the guard-exemption and sentinel-collision census
```

The suite is unusually opinionated about its own failure modes, because most of
its tests exist in answer to a specific shipped bug:

* `test_asset_paths.py` — the five silent asset bugs, and the search order that
  guarantees a user's own file beats the bundled default.
* `test_call_signatures.py` — the `ui → service → sweep` contract, compared
  against the real signatures. A pricing checkbox once shipped wired to a
  parameter `run_sweep()` did not accept: the entire suite passed and every GUI
  sweep died instantly.
* `test_gui_smoke.py` — builds the real Qt tab offscreen in a subprocess, because
  building it in-process killed pytest while it reported exit 0. A silent death
  must never read as a pass.
* `test_sentinel_collisions.py` / `test_guard_exemption_census.py` — the sentinel
  rule above, plus every guard exemption listed in one place with a ceiling.

Most of these carry a **positive control** — a test that the test can still fail.
A scanner that cannot fail is this repository's recurring defect.

---

## Building the distributable

Full procedure in **[`packaging/README.md`](packaging/README.md)**. The short
version:

```bash
# Build OUTSIDE OneDrive, and use forward slashes -- Git Bash eats the backslashes
pyinstaller packaging/jaguar.spec     --distpath C:/jaguar_build/dist --workpath C:/jaguar_build/build
pyinstaller packaging/jaguar_cli.spec --distpath C:/jaguar_build/dist --workpath C:/jaguar_build/build
```

Two separate specs, built sequentially: it halves peak memory, and a combined
build was OOM-killed six times — once leaving both `.exe` files written with
`_internal` a gigabyte short. It **failed dishonestly**, which is why the
procedure now ends in verification rather than in a file listing.

`torch` is excluded and must stay excluded (it arrived through a SciPy Array API
shim and cost 3.5 GB of bundle). The exclusion is verified by exercising every
non-sweep GUI path with torch installed and importable, then asserting nothing
reached for it.

CI (`.github/workflows/ci.yml`) runs ruff, mypy and pytest on Python 3.11 and
3.12. `build-installers.yml` produces Windows and macOS bundles on a `v*` tag.

---

## Documentation map

| File | What's in it |
|---|---|
| **[`README_TEAM.md`](README_TEAM.md)** | Non-developer quick start: download, lab import, reading the warnings, the process-pool verification protocol |
| **[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)** | Everything deliberately shipped imperfect, each entry priced |
| **[`packaging/README.md`](packaging/README.md)** | The build procedure, verification steps, size expectations |
| **[`docs/DESIGN_ROADMAP.md`](docs/DESIGN_ROADMAP.md)** | Where the architecture is going |
| **[`CLAUDE.md`](CLAUDE.md)** | Engineering guardrails and provenance for the contested numbers |
| **`docs/help/`** | The in-app help topics |

---

## Before you quote a number

Read [`README_TEAM.md` §5](README_TEAM.md). The short version: the strength fits
are only as good as the lab data behind them, and until enough properly-aged
breaks are imported, **hull weights are relative comparisons, not absolute
predictions**. "Design A is 4 lb lighter than design B" is meaningful. "Our canoe
will weigh 124.2 lb" is not.

The application will tell you which situation you are in. Believe it over this
page — it knows what it actually loaded.

---

<p align="center">
  <sub>Built for the University of Virginia ASCE Concrete Canoe team · 2026–2027 season</sub>
</p>
