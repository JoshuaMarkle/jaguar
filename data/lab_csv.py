# data/lab_csv.py
"""
Reader for the mix team's test-results spreadsheet (CSV export).

The team keeps break results in Google Sheets, one tab per test type, and
exports each tab as its own CSV.  Two shapes land here:

  * COMPRESSIVE  — ``Mix Name, Date set, Date tested, Curing time (days),
    Dry Weight (lbs), Wet Weight (lbs), Dry Density (pcf), Wet Density (pcf),
    Compressive Strength (psi)``
  * FLEXURAL     — the same identity columns, then ``Compressive Load (lbs)``
    (the beam's BREAK load, despite the name) and ``Flexural Strength (psi)``

Plus the suite's own export format (``test_date,batch_id,density_pcf,…``),
which still round-trips.

ONE TEST TYPE PER FILE.  A sheet that pastes both tables side by side (the
workbook's "import" tab does this, under a ``Flexural Data`` … ``Compressive
Data`` banner) is REFUSED rather than read: its two halves share one set of
mix-name and date columns while carrying their own curing times, so the rows
do not actually correspond, and reading it would file each break under its
neighbour's name.  Import the two tabs separately instead — nothing is lost,
since that is where the numbers come from.

What the reader does beyond splitting cells
-------------------------------------------
The sheet leaves cells blank whenever the spreadsheet formula had nothing to
chew on, so the numbers that matter are often derivable but absent:

  * **density** — ``weight ÷ specimen volume``.  The specimen volumes live in
    the sheet itself, in labelled cells off to the right (``Cylinder Volume``,
    ``Cube Volume``); those win when present, else the configured defaults.
  * **flexural strength** — ``f_r = P·L / (b·d²)``, third-point loading
    (ASTM C78), from the break load and the labelled ``Length``/``Base``/
    ``Depth`` constants.  This is the formula the sheet uses: its own
    2349 lb → 3306 psi row reproduces exactly.

Every derived value is recorded in the row's notes, so the Lab Results table
says where a number came from rather than presenting it as measured.

Beam and cylinder specimens are kept as SEPARATE records even when they share
a mix name: they are different specimens with different densities, and the
regression fits each strength tier over the rows that actually measured it.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ── Specimen geometry ────────────────────────────────────────────────
# Defaults match the constants embedded in the team's own sheet:
#   cylinder 4x8 in  = 0.0581481 ft^3      cube 2 in = 0.0046296 ft^3
#   flexure beam     = 0.0084635 ft^3      (6.5 x 1.5 x 1.5 in)
#   flexure span/base/depth = 4.75 / 1.5 / 1.5 in
# A labelled constant inside the CSV always overrides these: it is the record
# of the specimen that was actually broken.
_DEF_CYLINDER_FT3 = 0.058148148
_DEF_CUBE_FT3     = 0.00462963
_DEF_BEAM_FT3     = 0.008463542
_DEF_SPAN_IN      = 4.75
_DEF_BASE_IN      = 1.5
_DEF_DEPTH_IN     = 1.5

# A computed density outside this band means the wrong specimen volume was
# assumed; used only to pick cylinder-vs-cube when the sheet defines both.
_PLAUSIBLE_PCF = (20.0, 200.0)


@dataclass
class SpecimenConstants:
    """Specimen volumes [ft^3] and flexure geometry [in]."""
    cylinder_volume_ft3: float = _DEF_CYLINDER_FT3
    cube_volume_ft3: float = _DEF_CUBE_FT3
    beam_volume_ft3: float = _DEF_BEAM_FT3
    flexure_span_in: float = _DEF_SPAN_IN
    flexure_base_in: float = _DEF_BASE_IN
    flexure_depth_in: float = _DEF_DEPTH_IN

    @classmethod
    def from_settings(cls) -> "SpecimenConstants":
        """Read the defaults from canoe_suite.cfg, falling back to built-ins."""
        try:
            from config.settings import get_settings
            m = get_settings().mix
            return cls(
                cylinder_volume_ft3=float(m.cylinder_volume_ft3),
                cube_volume_ft3=float(m.cube_volume_ft3),
                beam_volume_ft3=float(m.beam_volume_ft3),
                flexure_span_in=float(m.flexure_span_in),
                flexure_base_in=float(m.flexure_base_in),
                flexure_depth_in=float(m.flexure_depth_in),
            )
        except ImportError:
            return cls()


@dataclass
class LabRecord:
    """One broken specimen, ready for ``MixDatabase.add_lab_result``."""
    test_date: str = ""
    batch_id: str = ""
    curing_days: int = 0
    rejected_notes: list = field(default_factory=list)
    specimen_type: str = ""          # cylinder | cube | beam | "" (unknown)
    density_pcf: float = 0.0
    dry_density_pcf: float = 0.0
    wet_density_pcf: float = 0.0
    fc_psi: float = 0.0
    ft_psi: float = 0.0
    fr_psi: float = 0.0
    fiber_type: str = "PVA"
    fiber_vol_pct: float = 2.0
    specimen_mass_g: float = 0.0
    mix_designer: str = ""
    notes: str = ""
    source_row: int = 0              # 1-based line in the CSV, for messages

    @property
    def density_basis(self) -> str:
        """Which weighing the regression density came from: dry | wet | as-given."""
        if self.density_pcf <= 0:
            return "none"
        if self.dry_density_pcf > 0 and \
                abs(self.density_pcf - self.dry_density_pcf) < 1e-6:
            return "dry"
        if self.wet_density_pcf > 0 and \
                abs(self.density_pcf - self.wet_density_pcf) < 1e-6:
            return "wet"
        return "as-given"


@dataclass
class ParseResult:
    records: list[LabRecord] = field(default_factory=list)
    # flexural | compressive | plain, or 'unsupported' when the file was
    # refused (see ``warnings`` for why) and 'unknown' when it is not a lab
    # sheet at all.
    layout: str = "unknown"
    notes: list[str] = field(default_factory=list)      # how it was read
    warnings: list[str] = field(default_factory=list)   # what to go check
    blank_rows: int = 0              # empty spreadsheet filler, ignored quietly
    invalid_rows: int = 0            # had content but no usable number
    dropped: list[str] = field(default_factory=list)   # why, line by line
    constants: SpecimenConstants = field(default_factory=SpecimenConstants)


# ── Header vocabulary ────────────────────────────────────────────────
def _norm(text: str) -> str:
    """Collapse a header cell to bare lowercase words.

    The sheet's headers carry typos and stray punctuation (``Wet Weight
    (lbs(``), so everything that is not a letter or digit becomes a space.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


_ALIASES = {
    # identity
    "mix name": "name", "mix": "name", "mix id": "name",
    "batch": "name", "batch id": "name", "sample": "name", "specimen": "name",
    "date set": "date_set", "set date": "date_set",
    "date cast": "date_set", "cast date": "date_set", "date poured": "date_set",
    "date tested": "date_tested", "test date": "date_tested",
    "date broken": "date_tested", "break date": "date_tested",
    "curing time days": "curing_days", "curing time": "curing_days",
    "curing days": "curing_days", "cure days": "curing_days",
    "cure time days": "curing_days", "age days": "curing_days",
    # masses and densities
    "dry weight lbs": "dry_weight_lb", "dry weight": "dry_weight_lb",
    "wet weight lbs": "wet_weight_lb", "wet weight": "wet_weight_lb",
    "dry density pcf": "dry_density", "dry density": "dry_density",
    "dry density pcf 1": "dry_density",
    "wet density pcf": "wet_density", "wet density": "wet_density",
    "density pcf": "density", "density": "density",
    # strengths
    "compressive strength psi": "fc", "compressive strength": "fc",
    "fc psi": "fc", "f c psi": "fc", "fc": "fc",
    "compressive load lbs": "flex_load", "compressive load": "flex_load",
    "flexural load lbs": "flex_load", "break load lbs": "flex_load",
    "peak load lbs": "flex_load", "failure load lbs": "flex_load",
    "flexural strength psi": "fr", "flexural strength": "fr",
    "modulus of rupture psi": "fr", "mor psi": "fr",
    "fr psi": "fr", "f r psi": "fr", "fr": "fr",
    "tensile strength psi": "ft", "split tensile strength psi": "ft",
    "split tensile psi": "ft", "ft psi": "ft", "f t psi": "ft", "ft": "ft",
    # provenance
    "fiber type": "fiber_type",
    "fiber vol pct": "fiber_vol_pct", "fiber vol": "fiber_vol_pct",
    "fiber volume": "fiber_vol_pct", "fiber volume pct": "fiber_vol_pct",
    "specimen mass g": "specimen_mass_g", "specimen mass": "specimen_mass_g",
    "specimen type": "specimen_type",
    "mix designer": "mix_designer", "designer": "mix_designer",
    "notes": "notes", "note": "notes", "comment": "notes",
    "comments": "notes",
}

# Fields whose presence proves a row is a header, not data
_MEASUREMENT_FIELDS = {"fc", "ft", "fr", "flex_load", "density",
                       "dry_density", "wet_density",
                       "dry_weight_lb", "wet_weight_lb"}

# A second copy of one of these in the same header row means two test tables
# were pasted side by side — see _find_header.  A repeated identity column
# ("Mix Name" and "Batch ID" in one sheet) is harmless and does not count.
_BLOCK_MARKER_FIELDS = _MEASUREMENT_FIELDS | {"curing_days"}

_CONSTANT_LABELS = {
    "cylinder volume": "cylinder_volume_ft3",
    "cube volume": "cube_volume_ft3",
    "beam volume": "beam_volume_ft3",
    "prism volume": "beam_volume_ft3",
    "length": "flexure_span_in", "span": "flexure_span_in",
    "base": "flexure_base_in", "width": "flexure_base_in",
    "depth": "flexure_depth_in", "height": "flexure_depth_in",
}

_BAD_CELLS = {"", "-", "—", "n/a", "na", "#div/0!", "#value!", "#ref!",
              "#name?", "#n/a", "#num!", "#null!", "null", "none", "tbd"}


def _num(cell: str) -> Optional[float]:
    """Parse a spreadsheet cell as a number, or None if it holds nothing.

    Excel error text (``#DIV/0!``) is a blank formula, not a value.
    """
    if cell is None:
        return None
    s = str(cell).strip().replace(",", "").replace("$", "").rstrip("%")
    if s.lower() in _BAD_CELLS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _has_content(cell: str) -> bool:
    """True when a cell holds something a formula did NOT leave behind.

    Blank, ``#DIV/0!`` and a bare 0 all mean "the spreadsheet had nothing to
    compute with" — a row of those is a test not yet run, not a bad row.
    """
    v = _num(cell)
    if v is not None:
        return v != 0.0
    return str(cell or "").strip().lower() not in _BAD_CELLS


def _pos(cell: str) -> Optional[float]:
    """``_num`` but treating zero as 'the formula produced nothing'.

    The sheet writes 0 into a density cell whenever its weight cell is empty,
    so a zero density is missing data — never a 0 pcf specimen.
    """
    v = _num(cell)
    return v if (v is not None and v > 0) else None


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
                 "%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%b %d %Y")


def _iso_date(cell: str) -> str:
    """Normalise any of the team's date styles to YYYY-MM-DD for storage."""
    s = (cell or "").strip()
    if not s:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s          # unparseable: keep the team's text rather than drop it


# ── Layout detection ─────────────────────────────────────────────────
def _map_header(row: list[str]) -> dict:
    """Map recognised header cells to {field: column}."""
    out: dict[str, int] = {}
    for col, cell in enumerate(row):
        fld = _ALIASES.get(_norm(cell))
        if fld and fld not in out:          # first column of a name wins
            out[fld] = col
    return out


def _is_header(row: list[str]) -> bool:
    cols = _map_header(row)
    return len(cols) >= 3 and bool(_MEASUREMENT_FIELDS & set(cols))


def _repeated_fields(row: list[str]) -> list[str]:
    """Measurement columns that appear more than once in one header row.

    One test table per file: a second ``Curing time (days)`` or
    ``Compressive Strength (psi)`` means two tables were pasted side by side.
    """
    seen: dict[str, int] = {}
    for cell in row:
        fld = _ALIASES.get(_norm(cell))
        if fld in _BLOCK_MARKER_FIELDS:
            seen[fld] = seen.get(fld, 0) + 1
    return sorted(f for f, n in seen.items() if n > 1)


def _side_by_side_reason(rows: list[list[str]], hdr: int) -> str:
    """Why this file holds two test tables rather than one, or '' if it doesn't.

    Two tells, either of which is conclusive: a banner row naming both test
    types above the headers, and a measurement column that appears twice.
    """
    if hdr > 0:
        banners = [c.strip() for c in rows[hdr - 1] if _norm(c) in
                   ("flexural data", "compressive data", "flexural",
                    "compressive")]
        if len(banners) >= 2:
            return ("its header row sits under section banners for "
                    + " and ".join(f"'{b}'" for b in banners[:2]))
    repeated = _repeated_fields(rows[hdr])
    if repeated:
        return ("the header row carries two copies of "
                + ", ".join(repeated))
    return ""


def _infer_kind(cols: dict) -> str:
    """Classify a sheet by the measurements its header carries."""
    if "flex_load" in cols or ("fr" in cols and "fc" not in cols):
        return "flexural"
    if "density" in cols:
        return "plain"          # this suite's own export: one density column
    if "fc" in cols:
        return "compressive"
    return "plain"


def _harvest_constants(rows: list[list[str]], claimed: set,
                       base: SpecimenConstants) -> tuple[SpecimenConstants, list[str]]:
    """Pick up ``label, value`` constant pairs parked outside the data columns.

    Only unclaimed columns are searched, so a data column legitimately named
    ``Depth`` can never be mistaken for the flexure-geometry constant.
    """
    consts = SpecimenConstants(**vars(base))
    found: list[str] = []
    for row in rows:
        for col, cell in enumerate(row):
            if col in claimed:
                continue
            attr = _CONSTANT_LABELS.get(_norm(cell))
            if not attr or col + 1 >= len(row):
                continue
            val = _pos(row[col + 1])
            if val is None:
                continue
            setattr(consts, attr, val)
            found.append(f"{cell.strip()}={val:g}")
    return consts, found


def _row_notes(row: list[str], claimed: set) -> str:
    """Free-text observations the team typed into a spare column.

    Anything of three or more words in an unclaimed cell is a comment
    ("Good break right in middle"); one- and two-word cells are constant
    labels and are left alone.
    """
    out = []
    for col, cell in enumerate(row):
        if col in claimed:
            continue
        text = (cell or "").strip()
        if len(text.split()) >= 3 and _num(text) is None \
                and _norm(text) not in _CONSTANT_LABELS:
            out.append(text)
    return "; ".join(out)


# ── Row → record ─────────────────────────────────────────────────────
def _get(row: list[str], cols: dict, field_name: str) -> str:
    col = cols.get(field_name)
    if col is None or col >= len(row):
        return ""
    return row[col] or ""


def _density_from_weight(weight_lb: float, kind: str,
                         consts: SpecimenConstants) -> tuple[float, str]:
    """Convert a specimen weight to pcf, returning (density, specimen_type)."""
    if kind == "flexural":
        vol = consts.beam_volume_ft3
        return (weight_lb / vol if vol > 0 else 0.0), "beam"
    lo, hi = _PLAUSIBLE_PCF
    cyl = weight_lb / consts.cylinder_volume_ft3 if consts.cylinder_volume_ft3 > 0 else 0.0
    if lo <= cyl <= hi or consts.cube_volume_ft3 <= 0:
        return cyl, "cylinder"
    cube = weight_lb / consts.cube_volume_ft3
    # Only switch when the cube reading is the plausible one
    if lo <= cube <= hi:
        return cube, "cube"
    return cyl, "cylinder"


def _parse_row(row: list[str], cols: dict, kind: str, line: int,
               consts: SpecimenConstants, basis: str,
               notes_extra: str) -> Optional[LabRecord]:
    """Build one record from one CSV line; None if the line measured nothing."""
    rec = LabRecord(source_row=line)
    rejected: list[str] = []
    derived: list[str] = []

    rec.batch_id = _get(row, cols, "name").strip()
    rec.test_date = (_iso_date(_get(row, cols, "date_tested"))
                     or _iso_date(_get(row, cols, "date_set")))

    cure = _num(_get(row, cols, "curing_days"))
    rec.curing_days = int(cure) if cure is not None and cure >= 0 else 0

    # ── densities: measured first, computed from weight only if absent ──
    dry = _pos(_get(row, cols, "dry_density"))
    wet = _pos(_get(row, cols, "wet_density"))
    plain = _pos(_get(row, cols, "density"))
    dry_w = _pos(_get(row, cols, "dry_weight_lb"))
    wet_w = _pos(_get(row, cols, "wet_weight_lb"))
    spec_type = {"flexural": "beam", "compressive": "cylinder"}.get(kind, "")

    # A DERIVED density must clear the plausibility band before it is used.
    # _PLAUSIBLE_PCF existed only to choose cylinder-vs-cube and was never
    # applied as a CHECK, so a row whose Dry Density cell held a typed 0 fell
    # through to weight/volume and stored 384 pcf -- physically impossible for
    # concrete, and it was admitted straight into the regression.
    _lo, _hi = _PLAUSIBLE_PCF
    dry_measured, wet_measured = dry is not None, wet is not None
    if dry is None and dry_w is not None:
        _d, _st = _density_from_weight(dry_w, kind, consts)
        if _d is not None and _lo <= _d <= _hi:
            dry, spec_type = _d, _st
            derived.append(f"dry rho from {dry_w:g} lb {spec_type}")
        elif _d is not None:
            rejected.append(
                f"dry rho {_d:.0f} pcf derived from {dry_w:g} lb is outside "
                f"{_lo:g}-{_hi:g} pcf -- the specimen volume assumed for this "
                f"row must be wrong; the value was DISCARDED, not stored")
    if wet is None and wet_w is not None:
        _d, _st = _density_from_weight(wet_w, kind, consts)
        if _d is not None and _lo <= _d <= _hi:
            wet, spec_type = _d, _st
            derived.append(f"wet rho from {wet_w:g} lb {spec_type}")
        elif _d is not None:
            rejected.append(
                f"wet rho {_d:.0f} pcf derived from {wet_w:g} lb is outside "
                f"{_lo:g}-{_hi:g} pcf -- DISCARDED")

    rec.dry_density_pcf = dry or 0.0
    rec.wet_density_pcf = wet or 0.0
    # An explicit single density column (this suite's own export) is already
    # the value the regression was fitted on — never re-derive it.
    #
    # MEASURED BEATS DERIVED, whichever basis it is on.  With a typed 0 in the
    # dry cell and a real number in the wet cell, the old order handed the
    # regression a value computed from weight in preference to one somebody
    # actually weighed on a scale.
    _measured = [d for d, m in ((dry, dry_measured), (wet, wet_measured))
                 if d and m]
    if basis == "wet":
        _measured = [d for d, m in ((wet, wet_measured), (dry, dry_measured))
                     if d and m]
    order = (wet, dry) if basis == "wet" else (dry, wet)
    rec.density_pcf = (plain or next(iter(_measured), None)
                       or next((d for d in order if d), None) or 0.0)

    typed = (_get(row, cols, "specimen_type") or "").strip()
    rec.specimen_type = typed or spec_type

    # ── strengths ──
    rec.fc_psi = _num(_get(row, cols, "fc")) or 0.0
    rec.ft_psi = _num(_get(row, cols, "ft")) or 0.0
    rec.fr_psi = _num(_get(row, cols, "fr")) or 0.0
    load = _pos(_get(row, cols, "flex_load"))
    if rec.fr_psi <= 0 and load is not None:
        b, d, L = consts.flexure_base_in, consts.flexure_depth_in, consts.flexure_span_in
        if b > 0 and d > 0:
            # ASTM C78 third-point loading: f_r = P*L / (b*d^2)
            rec.fr_psi = load * L / (b * d * d)
            derived.append(
                f"f_r from {load:g} lb break load (L={L:g}, b={b:g}, d={d:g} in)")

    fiber = (_get(row, cols, "fiber_type") or "").strip()
    if fiber:
        rec.fiber_type = fiber
    fvol = _num(_get(row, cols, "fiber_vol_pct"))
    if fvol is not None:
        rec.fiber_vol_pct = fvol
    mass = _num(_get(row, cols, "specimen_mass_g"))
    if mass is not None:
        rec.specimen_mass_g = mass
    rec.mix_designer = (_get(row, cols, "mix_designer") or "").strip()

    if rec.density_pcf <= 0 and rec.fc_psi <= 0 and rec.ft_psi <= 0 \
            and rec.fr_psi <= 0:
        return None                       # nothing measured in this block

    parts = [p for p in (_get(row, cols, "notes").strip(), notes_extra) if p]
    if derived:
        parts.append("computed: " + ", ".join(derived))
    if rejected:
        # On the RECORD, so the row itself carries why a number it looks like
        # it should have is absent -- not only in a report someone may skip.
        parts.append("DISCARDED: " + "; ".join(rejected))
    rec.notes = "; ".join(parts)
    rec.rejected_notes = list(rejected)
    return rec


def parse_lab_csv(path: str, *, density_basis: str = "dry",
                  constants: SpecimenConstants | None = None) -> ParseResult:
    """Read a lab-results CSV of any supported shape.

    ``density_basis`` picks which measurement feeds the regression when a row
    has both: ``"dry"`` (default) or ``"wet"``; the other is stored alongside.
    """
    basis = "wet" if str(density_basis).lower().startswith("w") else "dry"
    consts = constants or SpecimenConstants.from_settings()

    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as fh:
        rows = [list(r) for r in csv.reader(fh)]

    res = ParseResult(constants=consts)
    hdr = next((i for i, r in enumerate(rows) if _is_header(r)), -1)
    if hdr < 0:
        res.warnings.append(
            "no recognised header row — expected columns such as 'Mix Name', "
            "'Date tested', 'Compressive Strength (psi)' or "
            "'Flexural Strength (psi)'")
        return res

    # One test type per file.  Two tables pasted side by side share their
    # mix-name and date columns without their rows corresponding, so reading
    # them would file breaks under the wrong mix — refuse instead.
    both = _side_by_side_reason(rows, hdr)
    if both:
        res.layout = "unsupported"
        res.warnings.append(
            f"this file holds two test tables side by side ({both}), and the "
            "two halves share one set of mix-name and date columns without "
            "their rows lining up — a break would be filed under its "
            "neighbour's mix. Export and import the flexural tab and the "
            "compressive tab as separate files instead.")
        return res

    cols = _map_header(rows[hdr])
    kind = _infer_kind(cols)
    claimed = set(cols.values())

    # AN UNRECOGNISED HEADER IS SILENTLY DROPPED, and that is how a MEASUREMENT
    # disappears.  A sheet whose compressive column reads "Compression (psi)"
    # instead of "Compressive Strength (psi)" imported perfectly -- added=2,
    # fit_admitted=2, no warnings -- while every f'c value was thrown away and
    # the compressive fit ran on fewer points than anyone believed.
    #
    # Only complain about columns that LOOK like data: a sheet legitimately
    # carries labels, blanks and scratch notes, and warning about those would
    # train the reader to ignore the warning.
    _DATA_HINTS = ("psi", "strength", "density", "load", "weight", "mass",
                   "days", "age", "cure", "curing", "modulus", "mpa", "pcf",
                   "tensile", "flexural", "compress")
    _unmapped = []
    for _c, _cell in enumerate(rows[hdr]):
        if _c in claimed or not _has_content(_cell):
            continue
        _n = _norm(_cell)
        if any(h in _n for h in _DATA_HINTS):
            _unmapped.append(_cell.strip())
    if _unmapped:
        res.warnings.append(
            "COLUMN(S) NOT RECOGNISED and therefore IGNORED: "
            + "; ".join(repr(c) for c in _unmapped)
            + ". They look like measurements. Any values under them were "
              "DROPPED -- the fit never saw them. Rename to a spelling the "
              "importer knows (see the README's column table) and re-import.")

    res.constants, found = _harvest_constants(rows, claimed, consts)
    consts = res.constants
    if found:
        res.notes.append("specimen constants taken from the sheet: "
                         + ", ".join(found))
    res.layout = kind

    bases: set[str] = set()

    for i in range(hdr + 1, len(rows)):
        row = rows[i]
        line = i + 1
        if not any((c or "").strip() for c in row):
            res.blank_rows += 1
            continue

        rec = _parse_row(row, cols, kind, line, consts, basis,
                         _row_notes(row, claimed))
        if rec is None:
            # A row whose MEASUREMENT cells are all empty is a test that has
            # not been run yet (the team pre-fills mix names and dates) — not
            # an error.  Only content that failed to parse is worth reporting.
            if any(_has_content(row[c]) for f, c in cols.items()
                   if f in _MEASUREMENT_FIELDS and c < len(row)):
                res.invalid_rows += 1
                res.dropped.append(f"line {line}: no numbers could be read")
            else:
                res.blank_rows += 1
            continue
        if rec.density_pcf <= 0:
            # A strength with no density cannot be placed on the density
            # axis, which is the only axis this database has.
            res.invalid_rows += 1
            res.dropped.append(
                f"line {line} ({rec.batch_id or 'unnamed'}): "
                "a strength but no weight or density")
            continue
        bases.add(rec.density_basis)
        res.records.append(rec)

    # Sanity band.  A modulus of rupture in the thousands is not concrete —
    # it is a wrong span, a wrong specimen, or a load in the wrong units, and
    # it would drag the bending gate into passing sections that cannot carry.
    wild = [r for r in res.records if r.fr_psi > 1500.0]
    if wild:
        res.warnings.append(
            f"{len(wild)} row(s) give a flexural strength above 1500 psi "
            f"(highest {max(r.fr_psi for r in wild):.0f} psi), which is far "
            "beyond what a lightweight canoe mix reaches — check the break "
            "load's units and the span/base/depth constants before the "
            "bending gate is run against these.")
    if len(bases) > 1:
        res.warnings.append(
            "some rows carry a dry density and others only a wet one, so the "
            "regression will mix the two bases (wet runs a few pcf heavier). "
            "Weigh dry where you can.")
    return res
