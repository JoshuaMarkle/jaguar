# ASCE Concrete Canoe Engineering Suite

## System Directives
* **Active Plan:** Read `docs/DESIGN_ROADMAP.md` before executing architecture changes.
* **Current Priority:** Complete-canoe CAD export (Phase B), design-state handoffs between pipeline steps (Phase A), and displacement convergence loop (Phase C) — see roadmap.
* **Dependencies:** `PyQt6`, `matplotlib`, `scipy`, `numpy`, `fpdf2`, `pymoo`, `PyYAML`.
* **Execution:** `python app.py` (GUI) | `python main.py` (Headless Sweep) | `pytest tests/` (Tests).

## Strict Engineering Guardrails
* **Godzilla (Fortran) I/O:** `mlt933w.exe` is highly volatile. When modifying `services/hull_service.py::write_seed_hull`, you MUST preserve strict character columns, exact line counts, and `\r\n` (CRLF) line endings. Never use variable-length float formatting.
* **Concurrency:** Use `ThreadPoolExecutor`. NEVER use `ProcessPoolExecutor` (causes Windows deadlocks in GUI daemon threads).
* **Biomechanics:** Paddler load distribution is **100% knees, 0% toes** (`knee_frac_bending: 1.00`), with a 17-inch toe offset retained for when a toe load is configured. Read the split from `canoe_suite.cfg`; never hardcode it.
  * **The knee contact is a 3-inch PATCH, not a point** (`knee_dia_in`), in BOTH load models and at every resolution. A point load is a singularity: its shear jumps and its moment kinks, so the governing D/C does not converge under refinement — measured non-monotone and REJECTING the design at 321+ stations. With the patch, increments decay (+1.0e-02 → +1.1e-03 → +5.4e-04 → +2.1e-04 over 161→2561) to D/C ≈ 0.9255. Do not restore a point deposit in either branch: `PADDLER_LOAD_ENVELOPE` designs to the worst of both models, so a bare point load in EITHER one governs the envelope and silently puts the shipped D/C back on the singularity.
  * **Provenance — do not re-derive this.** `0.80/0.20` was **unsourced**: carried from the initial commit with no measurement behind it, and retired 2026-08-28. `0.90/0.10` is **team-measured but n = 1 paddler**, and drifts with posture — the same paddler heels-sitting vs upright-kneeling spans ~68% to ~100% on the knees. `1.00/0.00` was adopted because it needs no measurement, cannot drift, and already matched `knee_frac_punching`. **It costs nothing.** Measured on the patched load model at 161/321/641/1281/2561 stations, 100/0 and 90/10 give D/C identical to every digit: `PADDLER_LOAD_ENVELOPE` always includes the no-toe-relief model and that model always governs, so `knee_frac_bending` cannot move the shipped D/C while the envelope is on. (An earlier ~1% estimate was measured with point loads and does not carry over.) Treat `knee_frac_bending` as INERT until someone turns the envelope off.
  * At 100/0 the "advanced" biomechanical model reduces **exactly** to the simple point-load model, so `ADVANCED_PADDLER_LOADING` is a no-op at the default and the two-model envelope covers one model. This is intended and pinned by a test; it stops being true if `knee_frac_bending` drops below 1.0.
  * Bending may never credit **more** toe relief than punching (`knee_frac_bending <= knee_frac_punching`). If a future measurement justifies relief, lower bending only.
* **Config:** Read parameters from `canoe_suite.cfg` (YAML) via `config/settings.py`. DO NOT mutate Python files to change configurations.
* **This repo lives inside OneDrive.** Files can be made **online-only** by a OneDrive or Storage Sense cleanup, especially when the disk fills. A dehydrated file surfaces as a bizarre, non-reproducible failure — a missing `off1.mlt`, a truncated script, an import that worked an hour ago — not as an obvious "file not found". If tests fail in a way that makes no sense, check the file is actually on disk before debugging the code. Keep the project folder pinned to **"Always keep on this device."**

## Sentinels and Absence
**A sentinel is safe only when the failure value and the legitimate value point the SAME WAY.**

That one test decides cases no scanner can pattern-match, and it is the rule to apply when writing new code — the scanners only catch shapes they already know.

* `version_stamp.py` **passes**: it never raises, and answers `"unknown"`. A failure and a real answer are never confusable, and nothing downstream acts on `"unknown"` as though it were a version.
* `mix_database._admit_unrecorded_age` **passes**: on an unreadable config it returns `False`, meaning *refuse to admit unverified lab rows*. The failure and the conservative legitimate answer agree.
* `GM_T = 0.0` **failed**: zero is not an absence, it is the specific claim *"neutrally stable"*. Worse, every gate read `if gmt > 1e-9`, so the failure value **skipped** the stability check instead of failing it.
* `compute_hydrostatics` **failed harder**: its fallbacks were guarded by `if volume > 1e-12`, and `NaN > 1e-12` is `False`, so NaN geometry took the empty-hull branch and returned `GM_T = -draft/2` — a plausible marginal-stability figure. A *fabricated* value is worse than a swallowed exception, because it cannot look wrong.
* `_settings_source` **failed worst**: it returned `{"loaded": True}` from an exception — a manufactured confidence claim that *suppressed* the "CONFIG DEFAULTED" banner. A disabled warning beats a wrong number for damage.

Practical consequences:
* Never let an initial value double as a skip condition. If `x = 0.0` and a gate reads `if x > 1e-9`, then "not configured", "not computed yet" and "computation failed" are one state.
* Prefer raising over a numeric sentinel for anything a gate reads. If a caller must survive, give it a distinct flag (`_gmt_error`, `gate_pool_fed`), not a magic value.
* When a value cannot be computed, callers must **fail** the associated gate, never skip it.
* `tests/test_sentinel_collisions.py` enforces the shapes it knows, with a reviewed allow-list; `tests/test_guard_exemption_census.py` lists every guard exemption in one place with a ceiling. Run `pytest -s -k census` to read them.

## Derived Attributes and monkeypatch
`config.RACE_4_PERSON` / `RACE_2_PERSON` / `RACE_3_PERSON` are **derived on read** from `RACE_CATEGORIES` via a PEP 562 module `__getattr__`. They exist as a hook precisely so they cannot be stored — a stored copy is the crew-weight "split-brain" this suite already removed once.

**Never `monkeypatch.setattr` a derived alias.** The hook makes it look assignable and monkeypatch defeats it *on teardown*, not on set: it reads the old value **through** `__getattr__` (getting a derived list), then restores it with `setattr`, writing that list into `config.__dict__` where it shadows the derivation for the rest of the session. The test that patched it passes; a later test fails, and only if the files happen to run in the wrong order. This was invisible in forward collection order and failed reversed.

* Patch **`RACE_CATEGORIES`** — the single source of truth, already in `RUN_LEVEL_CONFIG_ATTRS`, and the aliases follow automatically. `config.set_race_categories()` is the supported API.
* `tests/conftest.py` asserts on teardown that no derived alias is stored, naming the test that did it, whichever order the files ran in.
* The same trap applies to any attribute served by a module `__getattr__`, not just these three.

## Architecture & Layering
Strict separation of concerns. Do not bypass layers.
* `ui/` (PyQt6): Thin wrappers. Calls services. NO business logic.
* `services/`: Orchestration, subprocesses, thread workers. NO Qt imports.
* `domain/`: Dataclasses (`HullHydrostatics`, etc.) and unit conversions. NO I/O. NO Qt.
* `physics/`: Pure math and solvers (e.g., Michell integral, race path finding).
* `structural/`: Geometry parsing, moment/shear envelopes, load placement (SLSQP).
* `reporting/`: ASCE compliance checking, fpdf2 generation.
* `data/`: SQLite (`mix_database.py`) and JSON state persistence.