# Porting map: old/ -> new

`old/` is ~35k lines of Python. v1 keeps the structural core and drops the rest. Read old code for
physics intent only; never copy its architecture (layers, global config, Qt, sentinels).

| Old | New | Status |
|---|---|---|
| `geometry.py::parse_godzilla_offsets` | `hull.rs::Hull::from_michlet` + `Grid::build` | done |
| `geometry.py::calculate_shell_properties` | `section.rs` | T2 (prototype version, simpler) |
| `structural.py::calculate_4_person_waterline`, `evaluate_canoe_weight` | `equilibrium.rs` | T3 (prototype version) |
| `structural.py::loaded_freeboard_check` | `check.rs` freeboard | T4 |
| `structural.py::kupfer_acceptance_limit`, `kupfer_search_limit` | `[gates]` in canoe.toml | done (constants) |
| `main.py` sweep, `services/structural_service.py` | `optimize.rs` | T5 |
| `canoe_suite.cfg` (YAML, 300+ lines) | `canoe.toml` (~35 lines) | done |
| `ui/hull3d_view.py`, `visualization.py` | `web/index.html` via `jaguar serve` | T7 |
| `structural.py::kupfer_*`, `apply_realistic_paddler_loads`, `distinct_arrangements` | - | v2 candidate |
| `hull_generator.py` | - | v2 candidate |
| `mix_database.py`, `data/lab_csv.py` | `[material]` slope/intercept | v2 candidate (fit from CSV) |

## Cut on purpose
Race simulator, Michell wave resistance, race paths/Dubins, Godzilla/Fortran driver and its CRLF
I/O, PyQt GUI and wizard, mix database, cost, PDF/LaTeX reporting, compliance panel, multizone pours,
pour-line layout, torsion/lateral/hoop checks, punching shear, GM_T stability, PyInstaller packaging,
process-pool fingerprinting, sweep audit logs.

## Legacy facts worth keeping (from old/CLAUDE.md)
- Knee load = 3 in patch, 100% knees. Point loads make D/C diverge under refinement.
- D/C drifts up ~1-2% per station doubling: search at 0.93, verify at 0.95 on 2n-1 stations.
- Sentinels: a failure value must never be readable as a legitimate value by a gate.
