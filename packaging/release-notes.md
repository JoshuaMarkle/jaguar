JAGUAR finds the lightest concrete canoe wall (thickness, mix density, mold depth) that passes every
check under the crew, and can search the hull shape too. It runs in your web browser; no install,
no terminal.

## New in 0.2.0
* **Check a canoe drawn in CAD:** Hull > **Import CAD file** takes a STEP file (.step/.stp) as well
  as STL or OBJ. JAGUAR measures the hull, wall and depth, turns shape search off, and Run checks
  that design: strength, floating, stability, flotation, stands; the Race tab races it.
* **Failing designs are shown, not hidden:** when nothing passes, the closest design is analysed in
  full and marked **Fails**, with every limit it misses (e.g. "GM 0.23 in < 3 in, 2M, 2F").
* **Pin paddlers to seats** (e.g. the steerer in the stern); JAGUAR orders the rest.
* **Time left** shows while a run goes; a stuck network probe (Dell Peripheral Manager) can no
  longer freeze the app.
* **Surface roughness** (Hull): your sanding level raises skin friction in the drag and race numbers.
* **The PDF is a full design proof:** every check of every crew worked out, plus how the drag
  (friction, Reynolds number, friction coefficient, wave drag) and the weighted race time are
  calculated, event by event.
* Fixed: CAD imports carried a phantom strip of wall at the gunwale (about 1% heavy, D/C slightly
  low).

## Download
* **Windows:** `JAGUAR-windows.zip`. Extract it, double-click **JAGUAR.exe**.
* **Mac** (Apple silicon and Intel, macOS 11+): `JAGUAR-mac.zip`. Drag **JAGUAR** to
  Applications, double-click it.

First open: the app is not signed by a paid certificate, so the system warns once.
Windows: **More info** > **Run anyway**. Mac: System Settings > Privacy & Security > **Open Anyway**
(macOS 14 and earlier: Control-click > Open). Each zip has a `Read me.txt` with the details.

## Use
* Your files live in **Documents/JAGUAR** (button **Open folder**): `canoe.toml`, the hull, the lab
  data `mix_tests.csv`, `result.json` (the last run, shown again when you reopen JAGUAR) and
  `runs/` (every finished run: **History** above the result opens or deletes one). Updating
  JAGUAR keeps them.
* It starts with the team's settings: a hull shape search of about 12 minutes on a recent laptop
  (the status line shows the time left). For a quick look set **Samples** to 96 (about a minute).
  **Pause** holds a run; **End** finishes it early with the best hull
  found so far.
* **Quit** (top right) stops it; closing the tab stops it after about 5 minutes.
