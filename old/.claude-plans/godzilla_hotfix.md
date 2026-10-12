# Godzilla `in.mlt` Fortran Formatting Hotfix Plan

**Status:** Diagnostic — DO NOT modify production code until Phase 1 confirms the exact byte deviation.
**Target:** `services/hull_service.py::write_seed_hull` (writer for `in.mlt` consumed by `mlt933w.exe`).
**Guardrails (from CLAUDE.md):** strict character columns, exact line counts, CRLF (`\r\n`) line endings, no variable-length float formatting, no Python file mutation for config.

---

## Phase 0 — Establish ground truth (read-only)

0.1 Locate the **known-working** reference `in.mlt` file. Candidate locations to check, in order:
   - `tests/fixtures/godzilla/in.mlt` (golden file from B1)
   - `tests/golden/in.mlt`
   - `services/godzilla/in.mlt.reference`
   - Any file matching `**/in.mlt*` excluding the live runtime copy
   Use `Glob` with `**/in.mlt*` to enumerate all candidates. Record the absolute path of the chosen reference.

0.2 Locate the **broken** generated `in.mlt`. This is the file produced by the current `write_seed_hull` on its most recent run (typically the working directory or `services/godzilla/in.mlt`). If no recent broken copy exists, regenerate one by invoking `write_seed_hull` with a known seed hull from the test fixtures and writing to a temp path — **do not** overwrite the reference.

0.3 Read both files in **binary mode** (`open(path, "rb")`) — not text mode. Text mode silently normalises line endings and will mask the exact bug. Record:
   - Total byte length
   - SHA256 hash of each
   - Number of `\r\n` vs lone `\n` vs lone `\r` occurrences (count each separately)
   - Whether the file ends with a trailing newline (and which kind)

**Exit Phase 0 only when:** both file paths are confirmed, both files have been read as bytes, and the four metrics above are recorded for both.

---

## Phase 1 — Byte-for-byte diff via temporary script

1.1 Create a **temporary** diagnostic script at `scripts/_diag_in_mlt.py` (prefix `_diag_` marks it disposable; delete after Phase 3). The script must:
   - Accept two CLI args: `<reference_path> <broken_path>`
   - Read both files with `open(p, "rb").read()` then decode as `latin-1` for diff display only (preserves every byte 1:1, never raises on non-ASCII).
   - Use `difflib.ndiff` line-by-line on the decoded strings split by `splitlines(keepends=True)`.
   - For every differing line pair, print:
     - 1-based line index
     - Reference line as `repr()` (shows `\r\n`, spaces, etc. unambiguously)
     - Broken line as `repr()`
     - A column-aligned caret marker (`^`) under each differing column
     - The hex of any non-printable bytes in the differing region

1.2 Additionally, the script must perform four **targeted scans** independent of `difflib` — these catch the four failure modes most common with Fortran fixed-format I/O:

   **Scan A — Float precision / column drift.**
   For each line that contains numeric tokens, parse fixed-width float fields by column slice (do not split on whitespace — Fortran cares about columns, not tokens). Flag any field where:
   - The decimal point is in a different column index than the reference, OR
   - The mantissa or exponent has a different character width, OR
   - A leading sign (`+`/`-`/space) shifted columns.
   Report the column range and both values.

   **Scan B — Whitespace after commas/separators.**
   Walk both files and report any position where the reference has `, ` (comma + space) and the broken file has `,` (or vice versa), and the same for tab/space substitutions. Fortran list-directed reads tolerate this; fixed-format reads do not.

   **Scan C — Line endings.**
   Report counts of `\r\n`, `\n`-only, and `\r`-only in each file. Any mismatch is a bug — Godzilla requires CRLF on every line.

   **Scan D — Trailing newline / line count.**
   Report:
   - Whether each file ends with `\r\n`
   - Total line count (using `count(b"\n")`)
   - Whether there are any blank trailing lines
   A line-count mismatch alone is sufficient to break the Fortran `READ` loop, which is sized by record count.

1.3 Run the script with both paths. **Save the full stdout** to `scripts/_diag_in_mlt_report.txt`. Do not summarise or truncate — the next phase needs every byte.

**Exit Phase 1 only when:** at least one concrete deviation is identified with file path, line number, column range, reference bytes, and broken bytes all recorded. If the script reports no differences, stop and re-verify Phase 0 — the wrong "broken" file was likely captured.

---

## Phase 2 — Categorise the deviation and locate the writer code

2.1 Map the deviation found in Phase 1 to one of the four categories (A/B/C/D). If multiple categories fire, fix in this order: D (line count) → C (line endings) → A (float columns) → B (separator whitespace). Line count and line endings are structural; float formatting is per-field.

2.2 Open `services/hull_service.py` and locate `write_seed_hull`. Identify:
   - The exact `open(...)` call. Note `mode` and `newline` arguments. **Required:** `open(path, "w", newline="")` so Python does not translate `\n`→`\r\n` a second time, combined with explicit `\r\n` in every written string. Or `open(path, "wb")` writing pre-encoded bytes. Anything else is wrong on Windows.
   - The format string(s) used for floats. Required pattern: explicit width + precision (e.g. `f"{value:10.4f}"`), never `str(value)`, never `repr(value)`, never `f"{value}"` without format spec, never `%.4g` (variable width).
   - Any use of `","`, `", "`, `"\t"` as separators — these must match the reference byte-for-byte from Phase 1.
   - Any loop that writes records — confirm the loop bound matches the record count the Fortran reader expects (often defined in the header line of `in.mlt` itself).

2.3 Cross-reference the writer code against the deviation. Write down (in the report file from 1.3) the specific line(s) of `hull_service.py` that produce the wrong bytes, and the specific characters they need to produce instead.

**Exit Phase 2 only when:** the broken Python expression and its required replacement are both written down explicitly. Do not proceed to Phase 3 on intuition.

---

## Phase 3 — Apply the fix

3.1 Edit `services/hull_service.py::write_seed_hull` to produce the exact bytes identified in Phase 2.

   **Forbidden approaches** (CLAUDE.md guardrails):
   - Do not use `str.replace()` to patch output after it is written ("fix-up" passes hide root causes and break on the next field added).
   - Do not flatten multi-line records onto a single line.
   - Do not use `repr()`, `str()`, `%g`, `%e` without explicit width, or `f"{x}"` without a format spec on any numeric field.
   - Do not change `canoe_suite.cfg` defaults to mask the issue.

   **Required approach:**
   - Use Fortran-style fixed-width format specifiers: `f"{value:{W}.{P}f}"` where `W` and `P` match the reference column widths from Phase 1 Scan A.
   - Open the file with `open(path, "w", newline="")` and end every line with explicit `"\r\n"`. Or open with `"wb"` and write `bytes` with `b"\r\n"` terminators.
   - If the reference has a trailing CRLF after the last record, write one. If it does not, do not write one. Match exactly.
   - Preserve the exact separator bytes (space vs comma vs tab) found in Phase 1 Scan B.

3.2 Re-run the writer against the same seed hull used in Phase 0.2. Re-run the Phase 1 diagnostic script against the **new** output vs the reference. Required result: zero differences across all four scans and zero `difflib` deltas.

3.3 Run the Godzilla-dependent test suite: `pytest tests/ -k godzilla` (these are the 10 skipped tests noted in `memory/project_progress.md` — they will execute if `mlt933w.exe` is available). All must pass. If `mlt933w.exe` is unavailable in the environment, document that the diff-based equivalence check from 3.2 is the acceptance criterion and flag for manual verification on a machine with the executable.

3.4 Run the full suite: `pytest tests/`. Required: 177 passing (or current baseline) with no new failures.

**Exit Phase 3 only when:** 3.2 reports zero diffs AND 3.4 reports no regressions.

---

## Phase 4 — Cleanup

4.1 Delete `scripts/_diag_in_mlt.py` and `scripts/_diag_in_mlt_report.txt`. They were diagnostic scaffolding and must not ship.

4.2 If a golden-file test for `in.mlt` byte-equivalence does not already exist under `tests/`, add one: read the reference, call `write_seed_hull` to a temp path, assert `open(out, "rb").read() == open(ref, "rb").read()`. This prevents regression. If such a test already exists (it should from B1), confirm it now passes — if it was previously passing and the bug still occurred, the test fixture itself may be stale and needs review against the real reference.

4.3 Report back to the user with:
   - The category (A/B/C/D) of the bug
   - The exact line of `hull_service.py` that was changed
   - Before/after snippet of the format string
   - Test results from 3.3 and 3.4

---

## Non-negotiable constraints (recap)

- **CRLF on every line.** No `\n`-only, no mixed.
- **Fixed column widths.** Every float uses `f"{x:W.Pf}"` with W and P from the reference.
- **Exact line count.** The Fortran reader is record-count-bound.
- **No `replace()` post-processing.** Fix the writer, not the output.
- **Binary-mode comparison only.** Text mode hides line-ending bugs.
- **Reference file is authoritative.** If reference and code disagree, the code is wrong.
