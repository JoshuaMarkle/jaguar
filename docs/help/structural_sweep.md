# Godzilla + Structural Sweep

## What it does
Runs the Fortran **Godzilla** wave-resistance solver on your hull geometry, then sweeps over hull thickness and concrete mix densities to find the lightest feasible canoe that passes all stress and freeboard checks.

## Workflow
1. Set **Hull Geometry** — race weight, length range, and seed draft.
2. Upload Godzilla output files (`off1.mlt` + `out.mlt`) from a previous Godzilla run, **or** click **Run Godzilla + Structural Sweep** to generate them fresh.
3. Review the **Hull Preview**, **Moment/Shear Envelope**, and **Pour Zones** tabs.
4. Generate a PDF report from the **Report** button.

## Godzilla files
| File | Contents |
|------|---------|
| `off1.mlt` | Hull offset table (half-breadths at each station/waterline) |
| `out.mlt` | Wave resistance vs. speed output |

If `mlt933w.exe` is absent, the app automatically uses the Python Michell-integral solver.

## Structural sweep dimensions
The sweep varies three concrete mix densities (pcf) and two hull thicknesses (in) for a 5-D optimisation.

## Factor of Safety
FOS ≥ 1.5 is required. The design tensile stress must be below `f_t / FOS` at every station.

## Freeboard check
The hull is rejected if its freeboard with the full crew aboard is less than **Min Freeboard (in)** (default: 6 in per ASCE rules).
