# Mix data: where concrete strength comes from

JAGUAR does not guess concrete strength. It reads the team's lab breaks and fits a line.

## The file

`data/mix_tests.csv` (path set by `material.mix_data` in canoe.toml, relative to the toml).
The original lab sheet is kept untouched in `data/raw/Splitting_Tensile_Testing.csv`; the CSV is
a cleaned copy of it. Lines starting with `#` are comments. Columns (found by header name, order
does not matter, extra columns are ignored):

| column | meaning |
|---|---|
| `mix` | name of the mix / cylinder, free text. A trailing replicate token (one letter or a number after a space) is stripped to get the tested mix: `M2 A`, `M2 B` -> `M2` |
| `mix_id` | optional; when present and non-empty it overrides the grouping above |
| `cured_days` | age at the break (informational) |
| `density_pcf` | wet density of the cylinder, pcf (the raw sheet's "Wet Density" column; the "final target" row's 60 pcf is also in that column). Hull weight uses the same density, so if your mold-day concrete is lighter once dry, reported weights are conservative. |
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

## Choosing between tested mixes (`strength_model = "tested_mixes"`)

`material.strength_model = "fit"` (default in `examples/canoe.toml`) uses the line above at any
density in the trusted range. `"tested_mixes"` (see `examples/canoe_mixes.toml`) instead lets the
optimizer choose only among the mixes actually tested:

* rows are grouped into mixes by the rule in the `mix` column above;
* per mix: n, mean density, mean and min FINAL split strength (each row scaled by its
  `fraction_of_final`). The **design strength is the mean**; min and n are reported so you can see
  the scatter;
* the density axis is the list of mixes (ordered by density then name). `search.density_pcf` and
  `density_extrapolation_pcf` are ignored and there are no grid-edge/mix-edge flags for density;
* flexural and f'c come from the mix's mean split strength exactly as in fit mode.

The winner line reads e.g. `mix M2 (55.6 pcf)`. `jaguar mix` prints the per-mix table. The
"final target" row (60 pcf, 300 psi, final) has no replicate token so it is its own mix; it is the
team's target mix, kept under the name the raw sheet implies.

## Extrapolation limit

A straight line through a few points is only trustworthy near those points. The search only tries
densities within `density_extrapolation_pcf` (default 4) of the tested range: today 54.9-60 pcf
tested, so 51-64 pcf searched. With only 3 lab points `jaguar check` warns that the fit is weak.
If the winner sits at the low end, the summary prints
`NOTE density 51 pcf is the lightest the lab data supports`; that means **test lighter mixes**.

In practice knee punching (docs/PUNCHING.md) needs f'c of about 1640 psi, so the example winners
sit at about 58 pcf: the lightest mix that is strong enough, not the lightest the data allows.
What would lower weight is a light mix with higher strength (more data around 55-60 pcf), or a
knee pad.

## Adding rows

Append one line per cylinder, e.g.

```
M3 A,7,48.2,120,0.6,set 2026-10-20
```

then run `jaguar mix` to see the new fit and `jaguar run` again. Bad numbers stop the run with an
error naming the row; nothing is silently skipped.

## Raw sheet notes

`data/raw/Splitting_Tensile_Testing.csv` is the team's original export, kept unchanged. Its
unlabeled trailing columns (203 / 3.70 and 5 / 0.083 on the last rows) look like spreadsheet
side calculations and are not used. Its third row has only a wet density (60) and a strength
(300): per the team, that is a final (28-day) strength, so it is entered with
`fraction_of_final = 1.0`.
