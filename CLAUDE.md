# JAGUAR rewrite (in progress)

* `old/` is the previous full application (PyQt GUI, Godzilla, mix lab, reports). READ-ONLY reference
  and the oracle for equivalence checks. Its own guardrails are in `old/CLAUDE.md` (biomechanics,
  sentinels, Godzilla CRLF I/O, no ProcessPoolExecutor) - they still apply when porting that logic.
* `optimize.py` is a TEMPORARY single-file terminal optimizer: hull table -> bending D/C -> lightest
  passing wall, with a 2x-station recheck. Placeholder strength fit; bending only.
* Run: `.venv/bin/python optimize.py` (reads `old/off1.mlt`, `old/out.mlt`).
* Rule carried over: when a value cannot be computed, FAIL the gate - never default or skip it.
