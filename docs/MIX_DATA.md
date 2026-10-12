# Mix data: where concrete strength comes from

JAGUAR does not guess concrete strength. It reads the team's lab breaks and fits a line.

## The file

`data/mix_tests.csv` (path set by `material.mix_data` in canoe.toml, relative to the toml).
The original lab sheet is kept untouched in `data/raw/Splitting_Tensile_Testing.csv`; the CSV is
a cleaned copy of it. Lines starting with `#` are comments. Columns (found by header name, order
does not matter, extra columns are ignored):

| column | meaning |
|---|---|
| `mix` | name of the mix / cylinder, free text |
| `cured_days` | age at the break (informational) |
| `density_pcf` | wet density of the cylinder, pcf |
| `split_tensile_psi` | measured split-cylinder tensile strength, psi ("7,191" style commas ok) |
| `fraction_of_final` | share of final strength reached at test age (see below) |
| `note` | anything else |

## fraction_of_final

A 7-day break is weaker than the concrete will be at race day. The team states 7-day breaks are at
**60%** of final strength, so those rows have `0.6` and JAGUAR uses `final = measured / 0.6`.
A row that already is a final (28-day or design) strength has `1.0`.

## How strength is derived

1. Fit, by least squares, final split tensile strength vs density: `f_sp = a * rho + b`.
2. Bending tension capacity (modulus of rupture) = `flex_over_split * f_sp`. Default 1.0, which is
   conservative (ACI relations give about 1.12).
3. Compressive strength f'c from ACI 318 `f_sp = 6.7 sqrt(f'c)`, so `f'c = (f_sp / 6.7)^2`.
   Used by compression, shear and punching checks.
4. Every capacity is divided by `factor_of_safety`.

Check the fit any time with `jaguar mix` (or `jaguar mix path/to.csv`): it prints every point
(measured, fraction, final, fitted) and the line. `jaguar check` warns when the fit is weak
(fewer than 5 points, or rms scatter above 15% of the mean).

## Extrapolation limit (why winners stop at 51 pcf)

A straight line through a few points is only trustworthy near those points. The search only tries
densities within `density_extrapolation_pcf` (default 4) of the tested range. The lightest tested
mix today is about 55 pcf, so nothing lighter than 51 pcf is searched. Lighter mixes are lighter
canoes, so the optimizer runs straight to that limit and the summary prints
`NOTE density 51 pcf is the lightest the lab data supports`. That is not a physics result: it means
**test lighter mixes** and the optimizer can go lower.

## Adding rows

Append one line per cylinder, e.g.

```
M3 A,7,48.2,120,0.6,set 2026-10-20
```

then run `jaguar mix` to see the new fit and `jaguar run` again. Bad numbers stop the run with an
error naming the row; nothing is silently skipped.
