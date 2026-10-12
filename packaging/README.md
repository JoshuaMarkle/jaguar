# Packaging JAGUAR into a downloadable app

*Fix plan Phase 4 — "Repackage into downloadable exe for Windows and Mac".*

## Quick start (Windows, on this machine)

Use the clean build venv (created 2026-07-12; recreate with
`python -m venv C:\venvs\clean_build_env` + `pip install -r
requirements.txt pyinstaller` if it's gone) and build to a path **outside
OneDrive**:

```powershell
& C:\venvs\clean_build_env\Scripts\python.exe -m PyInstaller packaging\jaguar.spec `
    --noconfirm --distpath C:\venvs\jaguar_dist --workpath C:\venvs\jaguar_build
```

Output lands in `C:\venvs\jaguar_dist\JAGUAR\` — a self-contained folder
with `JAGUAR.exe` inside. Zip the folder and share it; no Python install
needed on the target machine.

Why the flags: building INSIDE the OneDrive-synced project folder fails
intermittently — OneDrive holds file locks while uploading the previous
761 MB output and `--noconfirm`'s dist cleanup dies with
`PermissionError: Access is denied`. (A still-running `JAGUAR.exe` or an
orphaned `mlt933w.exe` locks it the same way — close them first.)
Building in the global Python instead of the venv produced a 4.5 GB
bundle: PyInstaller swept in every globally installed package, including
PyTorch.

## macOS

PyInstaller **cannot cross-compile** — a Mac build must run on a Mac.
Two options:

1. **GitHub Actions (recommended):** push a `v*` tag or run the
   *Build installers* workflow manually; download the `JAGUAR-macos`
   artifact from the run page. `.github/workflows/build-installers.yml`
   builds Windows and macOS in one run.
2. A teammate with a Mac runs the same two commands as above.

Note: the Godzilla solver (`mlt933w.exe`) is a Windows binary and is not
bundled on macOS — step ② is unavailable there; everything else works.

## What the spec bundles

| Item | Why |
|---|---|
| `ui/static/` (three.min.js, OrbitControls.js, jaguar.ico) | offline 3-D hull viewer + runtime window/taskbar icon |
| `docs/help/` | in-app help pages |
| `mlt933w.exe` + `alleg42.dll` + `libstdc++.dll` (Windows only) | Godzilla hydrodynamics solver and its MinGW/Allegro runtime — without the DLLs beside the exe, Windows pops "alleg42.dll was not found" when Godzilla launches |
| scipy / cadquery / pymoo submodules | dynamically imported at runtime |

The JAGUAR icon is applied twice: embedded into `JAGUAR.exe` itself by the
spec's `icon=` flag (Explorer / shortcut icon), and set at runtime by
`app.py` via `QIcon` with a `sys._MEIPASS`-aware path (window + taskbar).
macOS bundle icons need `.icns` — see the note in the spec.

`canoe_suite.cfg` and `concrete_mixes.db` are **not** bundled — they are
user data, created next to the executable by the first-run wizard.

## First-build checklist (expect one iteration)

- [ ] App starts (if not: rebuild with `console=True` in the spec to see
      the traceback).
- [ ] JAGUAR icon shows on JAGUAR.exe in Explorer AND on the window/
      taskbar while running.
- [ ] 3-D hull tab renders (QtWebEngine resources + `ui/static` lookup —
      the code already resolves `sys._MEIPASS`).
- [ ] Godzilla runs (mlt933w.exe found — `services/hull_service.py`
      resolves it relative to the working directory; run the exe FROM the
      dist folder or fix the lookup to `sys._MEIPASS` if it misses).
- [ ] Help pages open (`docs/help` lookup may need the same `_MEIPASS`
      treatment as `ui/static` — check `ui/help_panel.py`).
- [ ] PDF report generation works (fpdf2 fonts).

## Size expectations

cadquery/OCP dominates: expect a 400–700 MB folder. If that is too heavy
for distribution, build a second "lite" spec without cadquery — everything
except the STEP wireframe exports works without it (the code already
guards `import cadquery` with a friendly error).


---

# THE BUILD PROCEDURE (2026-09-15)

Read this before building. Every rule below cost something to learn.

## 1. Build OUTSIDE OneDrive

`PyInstaller` fails with `PermissionError: [WinError 5]` deleting a previous
`dist/` inside a synced folder, and a 4 GB bundle in OneDrive will sync.

**Use forward slashes.** In Git Bash, `--distpath C:\jaguar_build\dist`
arrives as `C:jaguar_builddist` — both backslashes are eaten, and `C:name` is
DRIVE-RELATIVE, so the build silently lands in the current directory, which is
the repo. It reports success either way; only the final "results are available
in" line says where it actually went.

## 2. Build the two executables SEPARATELY, in this order

They were briefly one spec with two `Analysis` passes. That is tidier, and it
got the build **killed by the OOM reaper during COLLECT** on a 32 GB machine —
the worst possible failure, because both `.exe` files were already written and
the dist *looked* finished while `_internal` was a gigabyte short. The CLI then
died mid-import. Sequential builds halve peak memory and fail honestly.

```bash
rm -rf /c/jaguar_build          # stale work dirs cause confusing failures

python -m PyInstaller --noconfirm     --distpath C:/jaguar_build/dist --workpath C:/jaguar_build/work     packaging/jaguar.spec

python -m PyInstaller --noconfirm     --distpath C:/jaguar_build/dist --workpath C:/jaguar_build/work2     packaging/jaguar_cli.spec

cp /c/jaguar_build/dist/jaguar-cli/jaguar-cli.exe    /c/jaguar_build/dist/JAGUAR/
```

The copy works because `main.py`'s imports are a subset of `app.py`'s
(`app.py` imports `main`), so the GUI bundle's `_internal` is already a
superset.

## 3. VERIFY — do not skip this

```bash
cd /c/jaguar_build/dist/JAGUAR
./jaguar-cli.exe --help
```

**Verify by RUNNING the copied exe, not by seeing it present.** That is exactly
the check the OOM failure defeats: the file exists and the bundle is
incomplete. `--help` proves the bootstrap resolves and `_internal` is whole.

Then confirm the startup asset audit names real files:

```bash
./jaguar-cli.exe --multizone --stations 41 --thickness 0.75                  --depth-step 4.0 --depth-max 15.0 --density-step 20.0
```

The first block printed must be:

```
  DATA FILES IN USE
    canoe_suite.cfg    ...
    concrete_mixes.db  ...
    off1.mlt           ...
    out.mlt            ...
```

Any `NOT FOUND`, or `[!! CONFIG DEFAULTED]`, means the build is analysing
something other than the team's inputs. Five shipped bugs were exactly that,
and all five produced plausible numbers. **Do not hand over a build whose audit
is not clean.**

## 4. If it touches the GUI, LAUNCH THE GUI

Twice a change was verified from source, passed the whole suite, and shipped
broken in the GUI while the CLI stayed fine:

* `TypeError: run_sweep() got an unexpected keyword argument` — every sweep
* a warning banner whose only `logger` reference was inside its own handler

`tests/test_call_signatures.py` and `tests/test_gui_smoke.py` now cover that
boundary, but the suite is not a substitute for starting the app once and
pressing Run.

## 5. `torch` is excluded, and must stay excluded

3.5 GB of what was a 4.3 GB bundle, imported by nothing, and it **blocked six
builds** by OOM-killing PyInstaller inside `hook-torch.py`. It arrives via
**scipy** (`scipy/_lib/array_api_compat/torch/`), not cadquery. Verified
bit-for-bit source-vs-trimmed and across five non-sweep subsystems. See
KNOWN_ISSUES.md before removing the exclusion.

Trimmed bundle: **786 MB**.

## 6. What ships

Both specs carry `canoe_suite.cfg`, `concrete_mixes.db`, `off1.mlt`,
`out.mlt`, `in.mlt`, `ui/static`, `docs/help`, and (Windows) `mlt933w.exe`
with `alleg42.dll` and `libstdc++.dll`. `asset_paths.py` resolves each at
runtime: **beside the executable first**, then the working directory, then the
bundled copy LAST — so a team member's own `canoe_suite.cfg` or `off1.mlt`
placed next to the exe wins over the shipped one.

Note `.gitignore` has a blanket `*.spec` (PyInstaller drops one in the root
when run against a script) with `!packaging/*.spec` excepted. Both build
recipes went untracked for months because of that rule; nobody but the original
machine could build.
