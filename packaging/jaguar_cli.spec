# packaging/jaguar_cli.spec
# The HEADLESS console build: jaguar-cli.exe.
#
# Separate from jaguar.spec on purpose.  Both exes in one spec means two
# Analysis passes in one process, and on a 32 GB machine with a ~4 GB bundle
# that got the build killed by the OOM reaper DURING COLLECT -- which is the
# worst possible failure, because both .exe files were already written and the
# dist looked finished while _internal was a gigabyte short.  Sequential
# builds halve peak memory and fail honestly if they fail at all.
#
# BUILD ORDER (see packaging/README.md):
#     pyinstaller --distpath <D> --workpath <W> packaging/jaguar.spec
#     pyinstaller --distpath <D> --workpath <W> packaging/jaguar_cli.spec
#     copy <D>/jaguar-cli/jaguar-cli.exe  ->  <D>/JAGUAR/
#
# The copy works because main.py's imports are a SUBSET of app.py's (app.py
# imports main), so the GUI bundle's _internal already contains everything the
# CLI needs.  Verify it after copying -- `jaguar-cli.exe --help` is enough to
# prove the bootstrap resolves.

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("cadquery")
    + [
        "matplotlib.backends.backend_qtagg",
        "matplotlib.backends.backend_pdf",
        "fpdf",
        "yaml",
        "run_log",
        "ui.warning_banner",
    ]
)

datas = [
    ("../ui/static", "ui/static"),
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
    ("../in.mlt", "."),
]

binaries = []
if sys.platform == "win32":
    binaries.append(("../mlt933w.exe", "."))
    binaries.append(("../alleg42.dll", "."))
    binaries.append(("../libstdc++.dll", "."))

a = Analysis(
    ["../main.py"],
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
    name="jaguar-cli",
    # REQUIRED.  A windowed exe on Windows has no stdout to attach to, so every
    # print, the phase table and the warning banner would go nowhere -- and the
    # whole point of this executable is being able to READ what a sweep says.
    console=True,
    icon="../ui/static/jaguar.ico" if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="jaguar-cli",
)
