JAGUAR finds the lightest concrete canoe wall (thickness, mix density, mold depth) that passes every
check under the crew, and can search the hull shape too. It runs in your web browser; no install,
no terminal.

## New in 0.5.1
* **Searched hulls are now as deep as their mold depth.** A shape-search hull with rounded
  sections (Section above 0.5) came out up to 0.13 in shallower than its mold depth, because its
  keel was drawn slightly above the bottom. A 14.875 in mold depth built a 14.75 in canoe, and the
  Manufacture tab showed a 14.37 in mold instead of 14.5 in. The keel now sits on the bottom.
  Re-run the shape search: freeboard and wall weight change a little.
* Mold depth and mold height are shown to 3 decimals (0.001 in).

## New in 0.5.0
* **Manufacture tab: water-jet files for the mold.** After a run, the Manufacture tab (or
  `jaguar mold result.json`) lays out hardboard for the winner's male foam mold, built upside down
  on the gunwale, and gives one DXF per hardboard sheet for the water jet.
  * **Tracing templates** (default): one hardboard template per foam piece; trace it on the foam,
    cut just outside the line, sand to shape. Templates cover everything their layer covers, so the
    foam is never cut short, and taper smoothly to a point at the bow and stern. Or choose
    **boards in the mold** (hardboard between the foam layers).
  * **Packing:** **blocks** (default; every layer is cut at the same joints, so the mold comes
    apart in whole blocks for demolding), **continuous** (fewest pieces), **tight** (least
    hardboard), **fewest shapes** (half templates flipped over for the other side, and shared by
    the bow and stern). **Nest inside** cuts small templates out of the middle of big ones.
  * **Your stock:** list foam thicknesses (e.g. 1.5, 2: the layers mix them to fit the mold),
    foam sheet sizes and hardboard sheet sizes. Layers lie flat or stand upright.
  * **Ready for the water jet:** files hold only cut lines (OMAX cuts every line it imports;
    tick DXF marks for names and reference lines), alignment holes at every 12 in station (the same
    places in every layer, to line up the stack), pieces in cutting order, each cut starting at a
    corner.
  * **Shop packet:** a printable page with the steps, the stack order and a drawing of each
    layer, what every template traces, and every sheet in cutting order, with boxes to tick.

## New in 0.4.2
* **No more kink at the bow and stern of searched hulls:** hulls with full ends (Bow/Stern
  waterplane below 1) were drawn with a straight wedge from the tip and a corner about 5 in in.
  Full ends now curve smoothly into a pointed tip, and the hull is stored with more stations near
  each end, so the export, the 3D view and the analysis all follow the curve. Re-run the shape
  search to get the new ends; saved designs keep their old shape.

## New in 0.4.1
* **Smoother bow and stern in top view:** the sharp ends from 0.4.0 bent the gunwale in sharply in
  the last few inches (a hook at each tip). The side flare now eases in over about the last 15 in,
  so the gunwale runs nearly straight into the tip. Re-run and re-export to get the new ends.

## New in 0.4.0
* **Freeboard is checked like the RFP:** where the hull lettering sits, with the crew at their
  marked seats (static, at staging), against the rule minimum plus a staging margin.
  * Search > Gates: **Min freeboard (rule)** 5.75 in (6 in less the 0.25 in tolerance),
    **Freeboard margin** (added on top), **Freeboard seat shift** (0 = marked seats; e.g. 2-3 in
    for slack).
  * Search > **Lettering**: school name (at the bow) and canoe name (at the stern), letter height,
    width, distance below the gunwale and from the ends. JAGUAR shows where they land.
  * The result, the PDF proof and the Freeboard chart show the freeboard at the lettering, the
    lowest point of the hull, and how far it moves if the crew shifts (not gated).
  * Updating keeps your settings: to use the new check, set Min freeboard 5.75, a margin, and
    turn Lettering on with your names.
* **Export options:** Export STEP and Export STL still give the complete shelled canoe with its
  air pockets; the arrow next to each also gives the shelled canoe without air pockets, or the
  unshelled canoe (the hull filled in to the gunwale, to check freeboard in CAD).
* **Sharp bow and stern above the water:** with side flare, the ends above the waterline were a
  flat face (about 3/4 in wide at 3 deg). The flare now eases in at the tips, so the stem is a
  sharp line from keel to gunwale, with no corner in the gunwale line. Weight and checks barely
  move; re-run to export the new ends.

## New in 0.3.0
* **Your work comes back:** reopening JAGUAR shows the last result again, not just your settings.
* **Run history:** every finished run is kept. **History** (above the result) lists them with date,
  pass/fail, weight and race time; **Open** brings back all of a run's charts, the export and the
  PDF; **Delete** removes it.
* **Freeboard: expected, best and worst.** Freeboard depends on where the crew kneels. The result,
  the load case table and the PDF proof now show it at the planned seats (expected) and over every
  seat shift (best, worst); the 6.2 in gate uses the worst. A design that only passed freeboard at
  coarse resolution is no longer marked Verified.
* **Race displacements follow the load cases**, like the race events. **Races** sets one per crew,
  weighted by the share of the score its races carry. Each race event shows its share (%).
* **Export PDF** is named like the STEP and STL export (the hull's name).
* Fixed: picking "custom weights" for a race event did not work.

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
