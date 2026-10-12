# report_generator.py — compatibility shim
"""
Re-exports SubmittalReport from its canonical location in reporting/.

Import from `reporting.report_generator` in new code.
This shim stays so existing callers don't break during the migration.
"""
from reporting.report_generator import SubmittalReport, _fig_to_png  # noqa: F401

__all__ = ["SubmittalReport"]

if __name__ == "__main__":
    from reporting.report_generator import SubmittalReport as _R
    print(_R().generate())
