# packaging/jaguar.spec
# PyInstaller build recipe for JAGUAR — James Andrea Grappe's Utility for
# Aquatic Resistance (fix plan Phase 4).
#
# Build FROM THE PROJECT ROOT:
#     pip install pyinstaller
#     pyinstaller packaging/jaguar.spec
#
# Output: dist/JAGUAR/  (one-directory bundle; zip it for distribution).
# The macOS build must run ON macOS (PyInstaller does not cross-compile) —
# see .github/workflows/build-installers.yml.
#
# Known caveats (read packaging/README.md before the first build):
#   * cadquery/OCP makes the bundle large (~400–700 MB).
#   * mlt933w.exe is Windows-only and is skipped on macOS builds; the
#     Godzilla step is unavailable there.
#   * QtWebEngine resources are collected automatically by PyInstaller's
#     PyQt6 hooks, but verify the 3-D hull tab renders in the built app.
#
# THE HEADLESS CLI IS A SEPARATE SPEC: packaging/jaguar_cli.spec.
# Both executables were originally built from ONE spec with two Analysis
# passes, which is the tidier arrangement -- and PyInstaller was killed by the
# OOM reaper partway through COLLECT, leaving a dist that looked complete
# (both .exe present) but whose _internal was a gigabyte short, so the CLI
# died mid-import.  Two sequential builds halve peak memory.  See
# packaging/README.md for the build order and the copy step.

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# NOTE: scipy and pymoo are found by PyInstaller's normal static analysis
# (scipy also ships official hooks).  Do NOT collect_submodules() them:
# scipy ballooned the bundle, and pymoo 0.6.2 SEGFAULTS PyInstaller's
# isolated import helper on pymoo.gradient.toolbox.core (exit 0xC0000005).
hiddenimports = (
    collect_submodules("cadquery")     # OCP bindings load dynamically
    + [
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "matplotlib.backends.backend_qtagg",
        "matplotlib.backends.backend_pdf",
        "fpdf",
        "yaml",
        # The warning machinery. ui.warning_banner is imported INSIDE a method
        # (_show_run_critical_warnings), so if the analysis ever missed it the
        # frozen build would lose the run-critical banner silently -- the one
        # failure this whole release is guarding against. Named explicitly.
        "run_log",
        "ui.warning_banner",
    ]
)

datas = [
    ("../ui/static", "ui/static"),       # offline Three.js assets (3-D tab)
    ("../docs/help", "docs/help"),
    # THE CONFIG FILE.  Without this every frozen run fell back to built-in
    # defaults -- factor of safety, design method, density bounds, race
    # categories, station count -- and said so only via the
    # `[!! CONFIG DEFAULTED]` banner, which is the sole reason it was ever
    # noticed.  Shipped as the LAST-RESORT copy: `config_search_paths()` looks
    # beside the executable first, so a team member's own edited cfg beats this
    # one.  A bundled default that outranked a user's edits would be the same
    # defect wearing a different hat.
    ("../canoe_suite.cfg", "."),
    # THE MIX DATABASE.  `mix_database._configured_db_path()` falls back to a
    # copy beside its own module when cfg gives no usable path -- which in a
    # frozen build is inside `_internal`, where nothing was shipped.  sqlite
    # then CREATED one, so every frozen run opened a database holding the three
    # seeded design-intent mixes and ZERO of the 20 real lab results.  The
    # numbers looked plausible and were computed from design intent, not
    # measurement.  Shipping the real file makes the fallback a fallback rather
    # than a fresh empty database.
    ("../concrete_mixes.db", "."),
    # THE HULL ITSELF.  Without these a frozen run generated a DEFAULT hull
    # (a generic 20.0 ft boat instead of the team's 20.0098 ft one) and parsed
    # its length and draft from a 35-byte invented stub -- which sets the
    # waterline, the depth floor and the entire search grid.  Shipped as the
    # last-resort copy; `hull_offsets_path()` prefers one beside the exe so a
    # team can analyse their own hull.
    ("../off1.mlt", "."),
    ("../out.mlt", "."),
    ("../in.mlt", "."),       # in-app help markdown
]

binaries = []
if sys.platform == "win32":
    # Godzilla solver (Windows-only) + its runtime DLLs.  mlt933w.exe is a
    # MinGW/Allegro build: without alleg42.dll and libstdc++.dll BESIDE the
    # exe, Windows pops "alleg42.dll was not found" the moment it launches.
    binaries.append(("../mlt933w.exe", "."))
    binaries.append(("../alleg42.dll", "."))
    binaries.append(("../libstdc++.dll", "."))

a = Analysis(
    ["../app.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "IPython", "notebook",
        # TORCH IS 3.5 GB OF A 4.3 GB BUNDLE AND NOTHING IMPORTS IT.
        # It BLOCKED SIX BUILDS -- PyInstaller was OOM-killed inside
        # `hook-torch.py`, which walks and collects the whole library.
        #
        # WHERE IT COMES FROM, because the obvious guess is wrong:
        # NOT cadquery (zero torch imports) and not collect_submodules.
        # It is SCIPY -- `scipy/_lib/array_api_compat/torch/` is one of
        # scipy's Array API backend shims, alongside numpy, cupy and
        # dask. Those modules do `from torch import *` at module level,
        # so static analysis follows them, but they are only ever
        # IMPORTED when scipy dispatches on an actual torch tensor
        # (`elif is_torch_array(x)`). This suite passes numpy arrays,
        # so that branch cannot be taken. The remaining reference is
        # under `if TYPE_CHECKING:`, which never executes at runtime.
        #
        # Verified: nothing in this project imports torch, and it is
        # absent from sys.modules after importing every runtime
        # dependency (cadquery, pymoo, vtkmodules, pandas, matplotlib,
        # scipy, fpdf, yaml).
        #
        # Do NOT remove this to "fix" a scipy array-API problem without
        # first checking whether anything actually passes torch tensors.
        "torch", "torchvision",
    ],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JAGUAR",
    console=False,           # windowed app; set True to debug startup issues
    # Embedded in the .exe itself (Explorer / desktop shortcut icon).  The
    # same file ships inside ui/static (datas above) so app.py can also set
    # the runtime window/taskbar icon via QIcon + sys._MEIPASS.
    icon="../ui/static/jaguar.ico" if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="JAGUAR",
)

# macOS app bundle wrapper (ignored on other platforms).
# NOTE: macOS bundle icons require .icns, not .ico — convert
# ui/static/jaguar.ico (e.g. with `iconutil` or an online converter) and
# point icon= at it when a Mac release needs the branded icon.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="JAGUAR.app",
        icon=None,
        bundle_identifier="edu.virginia.concretecanoe.jaguar",
    )
