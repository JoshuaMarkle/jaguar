# Mix Lab

## What it does
Stores your team's lab test results and fits a density-to-strength regression so the structural sweep automatically uses the latest mix data.

## Adding a batch
1. Mix and cure your test cylinders.
2. In the **④ Mix Lab** tab fill in **Log New Lab Result**.
3. Enter: test date, mix name, curing time, density (pcf), and whichever strengths you broke for — f'c (compressive), ft (split-tensile), fr (flexural / MOR).
4. Click **+ Log Result**, then **Publish** when the regression looks good — the structural sweep will use the new relationship on the next run.

Leave a strength at 0 when you did not measure it. Zero means *not measured*, never *measured zero*: a beam contributes to the flexural fit only, a cylinder to the compressive fit only.

## Importing the test-results spreadsheet
**Import CSV…** reads the team's Google Sheet exports directly — the flexural
tab, the compressive tab, or a CSV this suite wrote. Ctrl-click to select both
tabs at once; they merge in one pass and the regression refits once at the end.

**One test type per file.** A sheet with both tables pasted side by side (the
workbook's "import" tab) is refused rather than read: its two halves share one
set of mix-name and date columns while carrying their own curing times, so the
rows don't correspond and each break would be filed under its neighbour's mix.
Export the two tabs separately — that's where the numbers come from anyway.
A refused file never stops the others you selected with it.

What it fills in for you, from the constants recorded in the sheet itself:

- **Density**, when the sheet left it blank but recorded a weight — `weight ÷ specimen volume`.
- **Flexural strength**, when only the break load is recorded — `f_r = P·L / (b·d²)` (ASTM C78, third-point loading).

Every computed number is written into the row's **Notes**, so the table says
where it came from. Specimen constants default to the values in
`canoe_suite.cfg` (`mix.cylinder_volume_ft3`, `mix.flexure_span_in`, …) and are
overridden by any labelled constant found in the file.

Re-importing an updated sheet is safe: a break already on file has its blanks
filled rather than being logged twice, and a genuinely different number is kept
as its own row with a warning rather than overwriting what you had. The summary
dialog after each import is worth reading — it reports how each file was read
and anything suspicious, including a file that was refused and results whose
numbers repeat under a different mix name.

## Curing age and the fit
Breaks younger than the design age (`mix.design_age_days`, 28 by default) are
recorded and shown, but held **out** of the regression: a 7-day cylinder is the
same mix at a fraction of its strength, and averaging it in makes every mix look
weaker than it is. The **In fit** column says which rows the fit used, and the
charts draw the held-out points as hollow markers.

## Regression model
The linear regression fits: `f'c = a × density + b`

The fit is updated automatically every time you add new data. The **Regression Version** shown in reports corresponds to the fit that was active when the sweep ran — this ensures reproducibility.

## Shared database
Point the database path (set in the first-run wizard or `canoe_suite.cfg`) to a shared OneDrive or network folder so all team members contribute to and read from the same dataset.
