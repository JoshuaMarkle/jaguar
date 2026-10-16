# promo/data: JAGUAR output behind the promo

Everything on screen comes from these files. Nothing here is hand-edited. Regenerate everything with
`node promo/scripts/export-data.mjs` (it runs `cargo build --release` first, then each command
below from the repo root; about 10 min on 2 cores). `node promo/scripts/extract.mjs` then
compacts them into `promo/src/generated/promo-data.json`, the only file the video imports.

Run on 2026-10-16 against the working tree (including the uncommitted `src/` changes present
at the time). The results match docs/HULLS.md.

| file | produced by |
|---|---|
| `off1_result.json`, `terminal_run_canoe.txt` | `jaguar run examples/canoe.toml -o promo/data/off1_result.json` (off1 baseline: 686 candidates, winner 98.18 lb, punching D/C 0.904, VERIFIED at 321 stations) |
| `canoe_zones.toml` | `examples/canoe.toml` with its commented `[zones]` block switched on, paths pointed back at `examples/`. Nothing else changed |
| `off1_zones_result.json`, `terminal_run_canoe_zones.txt` | `jaguar run promo/data/canoe_zones.toml -o promo/data/off1_zones_result.json` (two mixes: 86.74 lb, dense 58 pcf only under the knees) |
| `shape_opt_result.json`, `terminal_run_shape_opt.txt` | `jaguar run examples/shape_opt.toml -o promo/data/shape_opt_result.json` (shape search, weight objective: 508 shapes, winner 84.78 lb, 216 x 23.6 in) |
| `shape_race_result.json`, `terminal_run_shape_race.txt` | `jaguar run examples/shape_race.toml -o promo/data/shape_race_result.json` (race-time objective: winner 99.84 lb, 256.3 in) |
| `race_shape_race.json`, `terminal_race_shape_race.txt` | `jaguar race promo/data/shape_race_result.json -o promo/data/race_shape_race.json` (weighted 99.0 s; paths re-planned for the winner) |
| `race_off1.json`, `terminal_race_off1.txt` | `jaguar race promo/data/off1_result.json -o promo/data/race_off1.json` (weighted 100.1 s) |
| `race_shape_opt.json`, `terminal_race_shape_opt.txt` | `jaguar race promo/data/shape_opt_result.json --params examples/shape_race.toml -o promo/data/race_shape_opt.json` (weight pick: weighted 104.1 s) |
| `terminal_mix.txt` | `jaguar mix data/mix_tests.csv` |
| `mix_fit.json` | `jaguar serve` on a temporary copy of `examples/canoe.toml` whose `material.mix_data` is `data/mix_tests.csv`, then `GET /api/mix` (`mix::StrengthFit`; the `csv` text field dropped) |
| `search_path_hulls.json` | The shape search's own path. `shape_opt_result.json` `candidates` holds one entry per shape in evaluation order (96 Latin-hypercube samples, then pattern search). `incumbents` = every shape that became the best passing design so far (22, 102.17 lb to 84.78 lb). `spread` = 6 of the Latin-hypercube samples, picked by farthest-point spread over length, beam, draft and fore rocker. Each hull is `jaguar gen-hull` (41 x 21, the search's grid) of that shape's exact factors. The last incumbent is byte-identical to the result's `winner_hull` table (checked by the script) |

## What the video uses

* Wall grid scatter, terminal: `off1_result.json`, `terminal_run_canoe.txt`.
* Parameterize, search path, converge counters, checks, bending and freeboard charts:
  `shape_opt_result.json`, `search_path_hulls.json` (stations 161 / 321 / 641 are the winner's
  `convergence.stations`).
* Race: `race_shape_race.json`, event "Symposium co-ed 200 m slalom" (path, speed, turns), the
  race-time winner's hull from `shape_race_result.json`.
* Mix chart: `mix_fit.json`.
* Not shown (kept for reference): the two-mix zones run and the race times of off1 and the weight pick.

## Caveats

* **Race numbers are illustrative.** Race crews and paddler power in `[race]` are placeholders
  (docs/HULLS.md: "placeholder crews and paddler power"; `paddler_power_w = 100` is not calibrated).
  Use the race times to compare hulls, not as predictions. The video labels them "illustrative crews".
* **Two lab data sets.** The example runs read `examples/mix_tests.csv`, the frozen copy of
  2026-10-15 (3 tests, split = 12.37 rho - 446.4 psi). `jaguar mix` and `mix_fit.json` read the
  team's current `data/mix_tests.csv` (5 tests, split = 11.83 rho - 465.1 psi). The mix chart shows
  the current lab data; every weight, D/C and hull comes from the frozen copy, like docs/HULLS.md.
* The search path shows the incumbent (best so far) shapes in the order the search found them.
  Frames between two of them in the video are interpolated for the transition and say so.
