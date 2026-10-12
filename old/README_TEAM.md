# JAGUAR — Quick Start

Structural optimiser for the ASCE concrete canoe. This page is everything you
need to run it. If something here disagrees with what the app says on screen,
**believe the app** — it knows what it actually loaded.

---

## 1. What to download

Grab the whole **`JAGUAR`** folder, not just the `.exe`. The executable needs
the `_internal` folder beside it.

```
JAGUAR/
  JAGUAR.exe        <- run this
  _internal/        <- must stay next to it
  logs/             <- created on first run
```

### Where to put it

**`C:\JAGUAR` or your Desktop.** Two places it must NOT go:

* **Not inside OneDrive.** OneDrive can turn files "online-only" during a
  cleanup, and the app then fails in confusing ways that look like bugs.
* **Not in `C:\Program Files\`.** The app writes hull geometry into its own
  `_internal\` folder, and Program Files is read-only for normal accounts. Every
  sweep then fails with a message about geometry, not about permissions — so
  the real cause is invisible. This is the single easiest way to make the app
  look broken.

Double-click `JAGUAR.exe`. No install, no Python.

---

## 2. Refreshing the mix data

**Do this before every run that matters.** The optimiser is only as good as the
lab data behind it.

1. Export the two lab sheets from the OneDrive workbook as CSV — the
   compressive sheet and the flexural sheet, **one file each**.
   (A single file with both tables side by side is refused, on purpose.)
2. In the app: **Mix Lab → Import Lab CSV**, pick both files.
3. **Read the dialog.** It is not a receipt — it is the check.

### The one thing that must be in the sheet

Every row needs a **curing age**. The column may be called any of:

> `Curing time (days)` · `Curing days` · `Cure days` · `Age days`

A row without an age is **excluded from every strength fit**. It still appears
in the table, so nothing looks wrong — which is exactly why the dialog now
tells you. A break of unknown age might be a 7-day break, and 7-day breaks are
half-strength.

### Reading the import dialog

| What it says | What it means |
|---|---|
| `Into the strength fit: N admitted` | N rows actually reached the regression |
| `excluded: 4 (age not recorded)` | those rows are in the table but invisible to the fit |
| `COLUMN(S) NOT RECOGNISED` | a column that looks like data was **ignored**; check the spelling |
| **`NOT USABLE FOR FITTING`** (red) | **stop.** No row reached the fit; the app is running on seeded design targets, not your measurements |

Zeros are handled for you: a typed `0` density is treated as missing, not as a
0 pcf specimen, and a density derived from a weight is discarded if it comes
out impossible (outside 20–200 pcf).

---

## 3. Running a sweep

Set your crew weights and geometry in the **Structural** tab, then run.

From a terminal (source install only), be explicit about which sweep:

```
python main.py --multizone      3-zone I-beam, refinement gated
python main.py --ungated-ok     2-mix, NOT refinement verified
```

Plain `python main.py` refuses to run — the default sweep has no refinement
gate and shouldn't be the accidental choice.

---

## 4. What the warnings mean

After a run, a **red panel** may appear above the results. It only shows things
that change whether you can trust the number.

**"The winner sits on the EDGE of the search range."**
Your best design is pinned against a limit you set, so it is not a free
optimum — a wider range may find something better. Widen the range and re-run
before treating it as the answer.

**"The strength curve is fitted to very few lab points."**
Predicted strengths are an extrapolation. See §5.

**"canoe_suite.cfg was NOT loaded."**
Every parameter in that run was a built-in default, not what the config file
says. Fix it before using the result.

**"The refinement gate did not confirm this winner."**
The design was not re-checked at higher resolution; its utilisation may
understate the true value. Re-run with the multizone path.

Everything else goes to `logs/jaguar-<timestamp>.log` beside the executable.
The log link is at the bottom of the warning panel — one click.

---

## 5. Read this before quoting a weight

**As of this release the compressive fit has 2 lab points and the tensile fit
has none.**

With the tensile fit empty, the app falls back to the *seeded mix table* —
design targets somebody typed, not concrete anyone broke. Two points define a
line exactly, so there is no scatter to tell you how good it is.

**Treat every hull weight as a RELATIVE comparison, not an absolute.**
"Design A is 4 lb lighter than design B" is meaningful. "Our canoe will weigh
124.2 lb" is not, until more breaks are in the Mix Lab.

The lightest defensible web density on today's confirmed data is **66.9 pcf** —
the lowest density anyone has actually broken a cylinder at. Below that the app
is extrapolating past its own data.

This gets better on its own: every properly-aged row you import tightens the
fit, and the warning goes away when there are enough points.

---

## 6. When something looks wrong

1. Read the red panel and the import dialog.
2. Open the log (link at the bottom of the panel).
3. Check the mix data went in: **Mix Lab → Lab Results** should show your rows
   with a curing age and a density between 20 and 200 pcf.
4. If a number seems impossible, it may be — see `KNOWN_ISSUES.md`, which lists
   what is deliberately imperfect in this release and why.


---

## 7. Running a sweep without the GUI (`jaguar-cli.exe`)

The build now ships a **second executable** beside `JAGUAR.exe`. It runs the
same sweep with a console, so you can script it, log it, and compare two runs
against each other.

```
jaguar-cli.exe --multizone --stations 161 --thickness 0.75 ^
               --depth-step 1.5 --depth-max 15.57 --density-step 2.0 ^
               --json winner.json
```

`--json` writes the winner's numbers as text, so two runs can be compared
exactly rather than by eye. Everything else has a `--help`.

The plain `jaguar-cli.exe --multizone` runs the gated 3-zone sweep. Running it
with no flags deliberately refuses: the default 2-mix path has no refinement
gate, and it will tell you so rather than hand you an unverified number.

---

## 8. TIER 2 — verifying the process pool before the team uses it

**Read this before flipping `sweep_executor` to `process` for anyone else.**

The process pool is ~5.7x faster than threads (measured: 1209 s -> 211 s on an
identical 6-task grid). Frozen builds hold it back because a `ProcessPoolExecutor`
driven from the GUI's worker thread is the one combination that has never been
verified here, and its worst failure is a **deadlock**: the window stops
responding, nothing is printed, and the only way out is Task Manager.

A watchdog now converts that into a fallback — if no task finishes within
`SWEEP_STALL_TIMEOUT_S` (default 1800 s) the pool is torn down, the search
restarts on threads, and it says why. **Tier 2 is checking that the watchdog is
never needed, and that it works if it is.**

### What you need

* The frozen build, `JAGUAR.exe` (the GUI, not the CLI — the GUI is the path
  under test, because it creates the pool from a daemon thread).
* `canoe_suite.cfg` with `sweep_executor: process`.
* Roughly two hours, most of it waiting.

### The runs

Do all five. Note what happened for each — "fine" is a result.

| # | What to do | What must happen |
|---|---|---|
| 1 | Start a normal multizone sweep. Let it finish. | Completes. Winner matches a `jaguar-cli.exe --executor thread` run on the same settings. |
| 2 | Start a sweep. Press **Stop** within 10 seconds. | Stops within a few seconds. Window stays responsive. No orphan `JAGUAR.exe` in Task Manager. |
| 3 | Start a sweep. Press **Stop** about halfway. | Same as 2. The partial-progress message names how many configs were done. |
| 4 | Start a sweep. Press **Stop** in the last few seconds. | Same as 2. This is the tightest race — Stop arriving as the pool is already shutting down. |
| 5 | Run two full sweeps back to back **without closing the app**. | Both complete. The second is not slower than the first and does not fail. |

### What counts as a failure

* **The window stops responding and never recovers** — the deadlock. Record
  which run number, kill it, and set `sweep_executor: thread`. This is the
  result that keeps the pool locked.
* **Orphaned processes** in Task Manager after a Stop — the pool did not tear
  down. Same action.
* **The watchdog fires** ("EXECUTOR STALLED") — not a disaster, the run
  recovers on threads, but it means the deadlock is real and the pool should
  stay locked until it is understood.
* **Different winner** between run 1 and the thread run — stop immediately and
  report it. That would mean the executor changed a result, which it must never
  do.

### If all five pass

Set `sweep_executor: process` in the shipped `canoe_suite.cfg` and rebuild. The
team gets the 5.7x. Record the date and the machine in `KNOWN_ISSUES.md` — a
verification with no hardware written next to it is worth very little, since
the failure being tested for is timing-dependent.
