# mix_database.py
"""
SQLite-backed Concrete Mix Formulation Database & Predictive Engine.

Tracks physical mix designs (density, compressive/tensile strength,
fiber reinforcement) and trains a dynamic linear regression model.
This allows the structural sweep to test a continuous spectrum of
theoretical densities and predict their strengths based on real lab data.

Schema (v2)
-----------
- mixes            : named mix designs (used for the structural-sweep grid)
- test_cylinders   : cylinder break tests tied to a mix design
- lab_results      : standalone cylinder breaks — source-of-truth for regression
- regression_versions : named snapshots of the fit; one is marked published
"""
import logging
import os
import sqlite3
import time
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from errors import domain_only

logger = logging.getLogger(__name__)


@dataclass
class FitFinding:
    """One reason a strength fit is not fit to gate a design.

    ``headline`` states the problem with THIS run's numbers in it — the user
    has to be able to judge the call, and a generic "implausible fit" does not
    let them.  ``row_ids`` names the lab results behind it so they can be found
    in the sheet.
    """
    tier: str                    # fc | ft | fr
    code: str                    # negative_slope | fr_exceeds_fc | weak_fit
    headline: str
    detail: str = ""
    row_ids: List[int] = field(default_factory=list)
    blocking: bool = True

    def as_text(self) -> str:
        bits = [self.headline]
        if self.detail:
            bits.append(self.detail)
        if self.row_ids:
            bits.append("Lab result id(s): "
                        + ", ".join(str(i) for i in self.row_ids))
        return " ".join(bits)


@dataclass
class LabImportReport:
    """What one lab-results CSV did to the database.

    ``updated`` counts breaks already on file whose blanks this import filled
    — the flexural sheet completing rows the compressive sheet started — as
    opposed to ``added``, which are new specimens.
    """
    path: str = ""
    layout: str = "unknown"          # flexural | compressive | plain
    added: int = 0
    updated: int = 0
    skipped: int = 0                 # already on file, nothing new to add
    invalid: int = 0                 # had content, but no density to place it
    blank: int = 0                   # empty spreadsheet filler
    notes: List[str] = field(default_factory=list)      # how the file was read
    warnings: List[str] = field(default_factory=list)   # what to go check
    density_basis: str = "dry"

    # ADDED IS NOT ADMITTED.  A row can import cleanly and still be excluded
    # from every regression -- most often for a missing curing age, which the
    # fit treats as inadmissible because a break of unknown age could be a
    # 7-day break.  Before these fields the report said "added=4, skipped=0,
    # invalid=0" while ZERO rows reached the fit, the tensile tier silently
    # fell back to the seeded mixes table, and the density floor came from
    # design targets instead of measurements.  A teammate would have got
    # defensible-LOOKING designs built on nothing.
    fit_admitted: int = 0                       # rows this file put INTO a fit
    fit_excluded: dict = field(default_factory=dict)   # reason -> count
    fit_sources: dict = field(default_factory=dict)    # tier -> source in force
    # DEFAULT FALSE.  "Not yet checked" must not read as "checked and fine":
    # _account_for_fit_eligibility returns early if it cannot read the rows,
    # and with a True default that early return CLAIMED the import was usable
    # for fitting -- suppressing the "NOT USABLE FOR FITTING" dialog on the
    # strength of a check that never ran. Exactly the {"loaded": True} shape
    # this project removed from _settings_source. Unknown is not usable.
    fit_usable: bool = False

    LAYOUT_NAMES = {
        "flexural":    "flexural sheet",
        "compressive": "compressive sheet",
        "plain":       "Canoe Suite lab-result export",
        "unknown":     "unrecognised layout",
    }

    @property
    def imported(self) -> bool:
        """False when the file was refused rather than read."""
        return self.layout not in ("unsupported", "unknown")

    def summary(self) -> str:
        """One line for the log: what was read and what landed."""
        name = os.path.basename(self.path) or self.path
        if not self.imported:
            return f"{name}: not imported — see the note below"
        layout = self.LAYOUT_NAMES.get(self.layout, self.layout)
        bits = [f"{self.added} added"]
        if self.updated:
            bits.append(f"{self.updated} completed")
        bits.append(f"{self.skipped} already on file")
        if self.invalid:
            bits.append(f"{self.invalid} unusable")
        return f"{name}: read as a {layout} — " + ", ".join(bits)


# ═══════════════════════════════════════════════════════════════════
#  EMPIRICAL FALLBACK
#    Governing equations from team cylinder break tests:
#      Density [pcf] = 0.0062 × f'c [psi] + 59.309
#      ⇒  f'c [psi]  = (ρ − 59.309) / 0.0062
#      f_t [psi]     ≈ 0.20 × f'c  (with ~2 % PVA fiber by vol.)
# ═══════════════════════════════════════════════════════════════════

def _empirical_fc(density_pcf: float) -> float:
    return max((density_pcf - 59.309) / 0.0062, 1.0)


def _empirical_ft(fc_psi: float) -> float:
    return 0.20 * fc_psi


class MixDatabase:
    """
    SQLite-backed concrete mix formulation tracker.

    Thread-safety: all write operations retry on SQLITE_BUSY (WAL mode +
    retry-on-lock) so multiple users sharing a DB over OneDrive will not
    corrupt the database under typical access patterns.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = self._configured_db_path()
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Generous busy timeout so concurrent OneDrive writers retry rather than error
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

        # Dynamic regression state (in-memory; always set from DB on init)
        self.fc_regression = None
        self.ft_regression = None
        # Flexural / Modulus-of-Rupture fit, trained INDEPENDENTLY from whatever
        # 3-point-bending points the team has logged (fr_psi > 0).  None until
        # there are >= 2 such points; the structural gate falls back to ft then.
        self.fr_regression = None
        self.r_squared_fc = 0.0
        self.r_squared_ft = 0.0
        self.r_squared_fr = 0.0
        self._regression_source: str = "empirical"
        self._regression_n: int = 0
        self._regression_n_ft: int = 0
        self._regression_n_fr: int = 0
        # Strength-fit sanity guard.  Overrides are per-process and are NEVER
        # persisted: a headless run, the CLI and the tests must all start from
        # REJECT and stay there unless a person decides otherwise in session.
        self._raw_fr = None
        self._raw_fits: dict = {}
        self._fit_findings: List[FitFinding] = []
        self._blocked_tiers: set = set()
        self._fit_overrides: set = set()
        self._fit_override_source: str = ""

        if self._is_empty():
            self.seed_defaults()

        self._train_regression_curves()

    @staticmethod
    def _configured_db_path() -> str:
        """
        Where the mix database lives: canoe_suite.cfg's mix.db_path when it
        names a file that EXISTS, otherwise the repo's own concrete_mixes.db.

        The existence check is the point.  canoe_suite.cfg is generated per
        machine and carries an absolute path; on a fresh clone (or next year's
        laptop) that path is stale, and silently opening a NEW EMPTY database
        there would look like "the Mix Lab lost all our data".  Falling back to
        the repo copy keeps a checked-out project working unconfigured.
        """
        repo_db = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "concrete_mixes.db")
        try:
            from config.settings import get_settings
            configured = get_settings().mix.db_path
        except ImportError:
            return repo_db
        if not configured:
            return repo_db
        if os.path.exists(configured):
            return configured
        logger.warning(
            "canoe_suite.cfg mix.db_path points at %s, which does not exist — "
            "using the project's own concrete_mixes.db instead.", configured)
        return repo_db

    # ──────────────────────  SCHEMA  ──────────────────────────────────
    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS mixes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    UNIQUE NOT NULL,
                density_pcf     REAL    NOT NULL,
                fc_psi          REAL    NOT NULL,
                ft_psi          REAL    NOT NULL,
                fiber_type      TEXT    DEFAULT 'PVA',
                fiber_vol_pct   REAL    DEFAULT 2.0,
                description     TEXT    DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS test_cylinders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mix_id          INTEGER NOT NULL REFERENCES mixes(id)
                                    ON DELETE CASCADE,
                test_date       TEXT,
                fc_measured     REAL,
                ft_measured     REAL,
                density_measured REAL,
                notes           TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS lab_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                test_date       TEXT    NOT NULL,
                batch_id        TEXT    DEFAULT '',
                density_pcf     REAL    NOT NULL,
                fc_psi          REAL    NOT NULL,
                ft_psi          REAL    DEFAULT 0.0,
                fr_psi          REAL    DEFAULT 0.0,
                curing_days     INTEGER DEFAULT NULL,   -- NULL = not recorded
                specimen_type   TEXT    DEFAULT '',
                dry_density_pcf REAL    DEFAULT 0.0,
                wet_density_pcf REAL    DEFAULT 0.0,
                fiber_type      TEXT    DEFAULT 'PVA',
                fiber_vol_pct   REAL    DEFAULT 2.0,
                specimen_mass_g REAL    DEFAULT 0.0,
                mix_designer    TEXT    DEFAULT '',
                notes           TEXT    DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS regression_versions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                version_tag     TEXT    NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                slope_fc        REAL    NOT NULL,
                intercept_fc    REAL    NOT NULL,
                r2_fc           REAL    NOT NULL,
                slope_ft        REAL    NOT NULL,
                intercept_ft    REAL    NOT NULL,
                r2_ft           REAL    NOT NULL,
                num_points      INTEGER NOT NULL,
                data_source     TEXT    DEFAULT 'lab_results',
                is_published    INTEGER DEFAULT 0,
                notes           TEXT    DEFAULT ''
            );
        """)
        self._conn.commit()
        self._migrate_schema()

    # Columns added to lab_results after the table shipped, with their
    # declarations.  Everything here must have a DEFAULT: ADD COLUMN back-fills
    # existing rows with it.
    _LAB_ADDED_COLUMNS = (
        # Flexural / Modulus-of-Rupture strength (3-point bending), tracked
        # in parallel with the direct-tensile ft_psi.
        ("fr_psi",           "REAL DEFAULT 0.0"),
        # Age at break.  0 = not recorded (every row predating the column),
        # which the regression treats as "assume design age".
        ("curing_days",      "INTEGER DEFAULT NULL"),
        # cylinder | cube | beam | '' — what was broken, so a beam's flexural
        # density is never mistaken for a cylinder's compressive one.
        ("specimen_type",    "TEXT DEFAULT ''"),
        # Both weighings from the team's sheet.  density_pcf stays the single
        # value the regression reads; these record where it came from and keep
        # the other measurement rather than discarding it.
        ("dry_density_pcf",  "REAL DEFAULT 0.0"),
        ("wet_density_pcf",  "REAL DEFAULT 0.0"),
    )

    def _migrate_schema(self):
        """Additive migrations for DBs created before a column existed.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a new
        column has to be added explicitly.  ADD COLUMN is safe and cheap; guard
        it by column presence so it runs at most once per database.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(lab_results)").fetchall()}
        added = False
        for name, decl in self._LAB_ADDED_COLUMNS:
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE lab_results ADD COLUMN {name} {decl}")
                added = True
        if added:
            self._conn.commit()

    # ──────────────────────  CONCURRENCY-SAFE WRITE  ──────────────────
    def _exec_with_retry(self, sql: str, params: tuple = (), max_retries: int = 5):
        """Execute a write statement, retrying on SQLITE_BUSY with back-off."""
        delay = 0.1
        for attempt in range(max_retries):
            try:
                return self._conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < max_retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 2.0)
                else:
                    raise

    # ──────────────────────  DYNAMIC REGRESSION  ──────────────────────
    def design_age_days(self) -> int:
        """Age at which a break counts as the mix's strength (config, def. 28)."""
        try:
            from config.settings import get_settings
            return max(int(get_settings().mix.design_age_days), 0)
        except ImportError:
            return 28

    def fit_eligibility(self, row: dict) -> str:
        """Why a lab row is (or is not) in the regression: '' when it is.

        THREE exclusions, all of which the Lab Results table shows:
          * no density — nothing to place it on the density axis
          * an early-age break (1-day, 7-day) — the same mix at a fraction of
            its strength.  Averaging those in drags every prediction down.
          * NO RECORDED AGE — see below.

        THE HOLE THIS CLOSES.  This used to read

            age = int(row.get("curing_days") or 0)
            if age and design and age < design:   # 0 short-circuits

        so a row with no age was silently ADMITTED, on the stated assumption
        that it "is what every row logged before the column existed is".  The
        column defaults to 0, so a row imported yesterday without an age is
        indistinguishable from a legacy one, and the assumption quietly
        extended to new data.  The check read as enforcing a 28-day basis
        while enforcing nothing -- the same shape as a sentinel that is never
        compared and a name that is never bound.

        An unrecorded age is NOT admissible.  A regression described as
        28-day must contain only breaks known to be at design age; a break of
        unknown age could be 7-day, and 7-day breaks are exactly what the
        early-age exclusion exists to keep out.  Set
        ``mix.admit_unrecorded_age`` to opt back in, which is deliberately
        awkward and reports itself (see ``fit_provenance``).

        0 and NULL both mean UNRECORDED: an age of zero days is not a
        physically meaningful break, so there is no third state to preserve.
        New rows store NULL so the two are distinguishable at the schema
        level going forward.
        """
        if float(row.get("density_pcf") or 0.0) <= 0.0:
            return "no density"
        raw = row.get("curing_days")
        age = 0 if raw is None else int(raw or 0)
        design = self.design_age_days()
        if age <= 0:
            if self._admit_unrecorded_age():
                return ""
            return "age not recorded"
        if design and age < design:
            return f"{age}-day break"
        return ""

    @staticmethod
    def _admit_unrecorded_age() -> bool:
        """Opt-in escape hatch for age-unrecorded rows. Default: NO."""
        try:
            from config.settings import get_settings
            return bool(getattr(get_settings().mix, "admit_unrecorded_age",
                                False))
        except ImportError:
            return False

    def fit_provenance(self, key: str = "fc_psi") -> dict:
        """
        What the regression for ``key`` is ACTUALLY built from.

        Printed rather than implied, because "a 4-point fit" and "a 2-point
        fit with 2 rows held out for unrecorded age" are very different
        statements about how much the number can be leaned on.
        """
        rows = [r for r in self.get_lab_results(limit=100000)
                if float(r.get("density_pcf") or 0.0) > 0.0
                and float(r.get(key) or 0.0) > 0.0]
        used, excluded = [], {}
        for r in rows:
            why = self.fit_eligibility(r)
            if why:
                excluded.setdefault(why, []).append(
                    (float(r["density_pcf"]), float(r[key])))
            else:
                used.append((float(r["density_pcf"]), float(r[key])))
        used.sort()
        return {
            "key": key,
            "n_used": len(used),
            "used": used,
            "n_excluded": sum(len(v) for v in excluded.values()),
            "excluded": {k: sorted(v) for k, v in excluded.items()},
            "age_confirmed": all(
                int(r.get("curing_days") or 0) >= self.design_age_days()
                for r in rows if not self.fit_eligibility(r)),
            "design_age_days": self.design_age_days(),
            "admitting_unrecorded": self._admit_unrecorded_age(),
            "span_pcf": ((used[0][0], used[-1][0]) if used else None),
        }

    def get_fit_rows(self) -> List[dict]:
        """Lab rows the regression is allowed to use."""
        return [r for r in self.get_lab_results(limit=100000)
                if not self.fit_eligibility(r)]

    @staticmethod
    def _fit_line(pts) -> tuple:
        """Least-squares line through (x, y) points → (coeffs, R²)."""
        x = np.array([p[0] for p in pts], dtype=float)
        y = np.array([p[1] for p in pts], dtype=float)
        coeffs = np.polyfit(x, y, 1)
        var = np.sum((y - np.mean(y)) ** 2)
        r2 = (1 - np.sum((y - np.polyval(coeffs, x)) ** 2) / var
              if var > 0 else 0.0)
        return coeffs, float(r2)

    def _fit_tier(self, lab_rows: List[dict], mix_rows: List[dict], key: str):
        """Fit one strength tier from the best source that has ≥2 points.

        Only rows that MEASURED this tier count.  A flexural-only CSV logs
        beams with no cylinder strength, and a compressive-only CSV the
        reverse; counting those zeros as data would drag both fits toward the
        origin.  Returns ``(coeffs|None, r2, n, source)``.
        """
        pts = [(float(r["density_pcf"]), float(r[key] or 0.0))
               for r in lab_rows if float(r[key] or 0.0) > 0.0]
        if len(pts) >= 2:
            coeffs, r2 = self._fit_line(pts)
            return coeffs, r2, len(pts), "lab_results"
        mix_pts = [(float(r["density_pcf"]), float(r[key] or 0.0))
                   for r in mix_rows
                   if float(r.get(key) or 0.0) > 0.0
                   and float(r["density_pcf"]) > 0.0]
        if len(mix_pts) >= 2:
            coeffs, r2 = self._fit_line(mix_pts)
            return coeffs, r2, len(mix_pts), "mixes"
        return None, 0.0, len(pts), "empirical"

    def _train_regression_curves(self):
        """
        Fit the linear density → strength regressions.

        Preference order, applied PER TIER (f'c, f_t, f_r) so that a
        compressive-only or flexural-only season still fits what it measured:
          0. published MANUAL version (user-entered expression — pinned
             until superseded; survives restarts and lab-data edits)
          1. lab_results  (actual breaks — authoritative)
          2. mixes        (design targets — fallback if no lab data yet)
          3. empirical    (hardcoded constants — last resort)

        Only rows that recorded a given strength feed that tier's fit, and
        early-age breaks are held out (see ``fit_eligibility``).  The flexural
        (f_r / MOR) fit is trained in _fit_fr_regression() and is independent
        of the fc/ft source above — including a pinned manual expression,
        which never carries f_r.
        """
        self._fit_fr_regression()
        pub = self.get_published_regression()
        if pub and pub.get("data_source") == "manual":
            # R² undefined for a typed expression; 0.0 keeps the NOT NULL
            # column of auto-snapshots satisfied.  n = 0 also keeps the
            # weak-fit test (which needs points) off a typed line, while the
            # negative-slope test still applies — a pinned expression that says
            # lighter is stronger is as wrong as a fitted one that does.
            self.r_squared_fc = self.r_squared_ft = 0.0
            self._regression_source = f"manual ({pub['version_tag']})"
            self._regression_n = 0
            self._raw_fits = {
                "fc": (np.array([pub["slope_fc"], pub["intercept_fc"]]), 0.0,
                       2, "manual"),
                "ft": (np.array([pub["slope_ft"], pub["intercept_ft"]]), 0.0,
                       2, "manual"),
                "fr": (self._raw_fr, self.r_squared_fr,
                       self._regression_n_fr, "lab_results"),
            }
            self._apply_fit_guard()
            return

        lab_rows = self.get_fit_rows()
        mix_rows = self.get_all_mixes()

        fc_coef, fc_r2, fc_n, fc_src = self._fit_tier(lab_rows, mix_rows, "fc_psi")
        ft_coef, ft_r2, ft_n, ft_src = self._fit_tier(lab_rows, mix_rows, "ft_psi")

        # The fits AS MEASURED, before the sanity guard decides which of them is
        # allowed to be the tier in force.  Kept so the audit can describe a
        # blocked fit concretely and so an override can restore it.
        self._raw_fits = {
            "fc": (fc_coef, fc_r2, fc_n, fc_src),
            "ft": (ft_coef, ft_r2, ft_n, ft_src),
            "fr": (self._raw_fr, self.r_squared_fr, self._regression_n_fr,
                   "lab_results"),
        }
        self._regression_n, self._regression_n_ft = fc_n, ft_n
        self.r_squared_fc, self.r_squared_ft = fc_r2, ft_r2
        self._regression_source = (
            fc_src if fc_src == ft_src else f"f'c {fc_src} / ft {ft_src}")
        self._apply_fit_guard()

    def _fit_fr_regression(self):
        """Fit density → flexural strength (f_r / MOR) from the lab points that
        recorded a 3-point-bending result (fr_psi > 0).  Needs >= 2 such points;
        otherwise fr_regression stays None and the gate falls back to ft."""
        pts = [(float(r["density_pcf"]), float(r["fr_psi"]))
               for r in self.get_fit_rows() if float(r.get("fr_psi") or 0.0) > 0.0]
        if len(pts) >= 2:
            self._raw_fr, self.r_squared_fr = self._fit_line(pts)
            self._regression_n_fr = len(pts)
        else:
            self._raw_fr = None
            self.r_squared_fr = 0.0
            self._regression_n_fr = len(pts)
        # fr_regression is set by the guard, never here: an implausible MOR fit
        # must not reach the bending gate just because it exists.
        self.fr_regression = self._raw_fr

    # ══════════════════  STRENGTH-FIT SANITY GUARD  ═══════════════════
    # A fit that contradicts how concrete behaves is an ERROR IN THE DATA, not
    # a design input, and it must not become the tier the sweep gates on.  The
    # guard BLOCKS such a fit by default — everywhere, including headless runs,
    # the CLI and tests.  Only an explicit interactive override releases it,
    # and an override is recorded so the design carries its provenance.
    FIT_MIN_R2 = 0.50        # below this, with few points, the line is noise
    FIT_SMALL_N = 5          # "few points"
    _TIER_LABEL = {"fc": "compressive f'c", "ft": "split-tensile f_t",
                   "fr": "flexural f_r (MOR)"}
    _TIER_COLUMN = {"fc": "fc_psi", "ft": "ft_psi", "fr": "fr_psi"}
    _TIER_SYMBOL = {"fc": "f'c", "ft": "f_t", "fr": "f_r"}

    def _tier_row_ids(self, tier: str) -> List[int]:
        """Lab-result ids feeding a tier's fit, so findings can name them."""
        col = self._TIER_COLUMN[tier]
        return sorted(int(r["id"]) for r in self.get_fit_rows()
                      if float(r.get(col) or 0.0) > 0.0)

    def _search_densities(self) -> List[float]:
        """The density range the sweep will actually ask about [pcf]."""
        lo, hi = 60.0, 80.0
        try:
            from config.settings import get_settings
            m = get_settings().mix
            lo, hi = float(m.min_density_pcf), float(m.max_density_pcf)
        except ImportError:
            pass
        if hi < lo:
            lo, hi = hi, lo
        return [lo + (hi - lo) * k / 4.0 for k in range(5)]

    def audit_regression(self) -> List["FitFinding"]:
        """Physical-plausibility findings against the fits AS MEASURED.

        Three conditions, all blocking:

        * ``negative_slope`` — the fit says lighter concrete is stronger.  That
          is not a data point, it is an error: strength rises with density for
          every concrete.  Checked on every tier.
        * ``fr_exceeds_fc`` — the modulus of rupture meets or exceeds the
          compressive strength at the same density.  MOR runs ~10-20% of f'c;
          a MOR above it means a wrong break load, span or specimen.
        * ``weak_fit`` — R² below FIT_MIN_R2 on fewer than FIT_SMALL_N points:
          a line drawn through scatter, carrying no trend to extrapolate.

        Returns findings ordered fr, fc, ft (the flexural tier gates bending,
        so it leads).
        """
        out: List[FitFinding] = []
        for tier in ("fr", "fc", "ft"):
            coef, r2, n, src = self._raw_fits.get(tier, (None, 0.0, 0, ""))
            if coef is None or n < 2:
                continue
            label = self._TIER_LABEL[tier]
            eq = (f"{self._TIER_SYMBOL[tier]} = {coef[0]:.2f}*rho "
                  f"{coef[1]:+.2f}")
            ids = self._tier_row_ids(tier)
            if float(coef[0]) <= 0.0:
                out.append(FitFinding(
                    tier=tier, code="negative_slope",
                    headline=(f"{label} fit has a NEGATIVE slope ({eq}): the "
                              f"model says lighter concrete is stronger."),
                    detail=(f"R² = {r2:.3f}, n = {n}. Strength rises with "
                            f"density in every concrete, so this is a data "
                            f"error, not a trend."),
                    row_ids=ids))
            elif (src != "manual" and r2 < self.FIT_MIN_R2
                    and n < self.FIT_SMALL_N):
                # A typed expression has no R² to judge — only its slope.
                out.append(FitFinding(
                    tier=tier, code="weak_fit",
                    headline=(f"{label} fit explains almost none of its data "
                              f"(R² = {r2:.3f} on n = {n} points)."),
                    detail=(f"{eq}. Below R² {self.FIT_MIN_R2:.2f} on "
                            f"fewer than {self.FIT_SMALL_N} points the line is "
                            f"scatter, and the sweep extrapolates along it."),
                    row_ids=ids))
        # f_r above f'c anywhere in the range the sweep will search
        fr_c = self._raw_fits.get("fr", (None,))[0]
        fc_c = self._raw_fits.get("fc", (None,))[0]
        if fr_c is not None and fc_c is not None:
            worst = None
            for rho in self._search_densities():
                fr = float(np.polyval(fr_c, rho))
                fc = float(np.polyval(fc_c, rho))
                if fr >= fc > 0.0 and (worst is None or fr / fc > worst[3]):
                    worst = (rho, fr, fc, fr / fc)
            if worst is not None:
                rho, fr, fc, ratio = worst
                out.insert(0, FitFinding(
                    tier="fr", code="fr_exceeds_fc",
                    headline=(f"Flexural strength at {rho:.0f} pcf is "
                              f"{fr:.0f} psi, which is {ratio:.1f}x the "
                              f"compressive strength ({fc:.0f} psi)."),
                    detail=("MOR normally runs 10-20% of f'c. A modulus of "
                            "rupture above f'c means the break load, the span "
                            "or the specimen dimensions are wrong."),
                    row_ids=self._tier_row_ids("fr")))
        return out

    def _apply_fit_guard(self):
        """Publish each raw fit as the tier in force, unless it is blocked."""
        self._fit_findings = self.audit_regression()
        blocked = {f.tier for f in self._fit_findings
                   if f.blocking and f.code not in self._fit_overrides}
        self._blocked_tiers = blocked
        fc_coef = self._raw_fits["fc"][0]
        ft_coef = self._raw_fits["ft"][0]
        self.fc_regression = None if "fc" in blocked else fc_coef
        self.ft_regression = None if "ft" in blocked else ft_coef
        self.fr_regression = None if "fr" in blocked else self._raw_fr

    @property
    def blocked_tiers(self) -> set:
        """Tiers held out of force by the guard (empty when all are sane)."""
        return set(getattr(self, "_blocked_tiers", set()))

    @property
    def fit_findings(self) -> List["FitFinding"]:
        return list(getattr(self, "_fit_findings", []))

    def blocking_findings(self) -> List["FitFinding"]:
        """Findings that are currently suppressing a tier (override excluded)."""
        return [f for f in self.fit_findings
                if f.blocking and f.code not in self._fit_overrides]

    def set_fit_override(self, findings, source: str) -> None:
        """Release specific findings, on an explicit interactive decision only.

        ``source`` describes WHO decided; it travels into the sweep banner, the
        report and the saved design state.  Never persisted to the database: a
        later run must make the choice again rather than inherit it.
        """
        codes = {f.code if isinstance(f, FitFinding) else str(f) for f in findings}
        if not codes:
            return
        self._fit_overrides |= codes
        self._fit_override_source = str(source)
        self._train_regression_curves()

    def clear_fit_override(self) -> None:
        self._fit_overrides = set()
        self._fit_override_source = ""
        self._train_regression_curves()

    def fit_override_record(self) -> Optional[dict]:
        """Provenance for a design built on an overridden fit, or None."""
        if not self._fit_overrides:
            return None
        released = [f for f in self.fit_findings if f.code in self._fit_overrides]
        return {
            "source": getattr(self, "_fit_override_source", "unknown"),
            "codes": sorted(self._fit_overrides),
            "tiers": sorted({f.tier for f in released}),
            "findings": [f.headline for f in released],
            "row_ids": sorted({i for f in released for i in f.row_ids}),
        }

    def lab_result_count(self) -> int:
        """
        How many LAB RESULTS this database holds.

        Exists so a run can say it out loud.  A freshly created database is
        indistinguishable from a populated one at a glance -- same schema, same
        seeded `mixes` table, same file size to within a few KB -- and the
        frozen build opened exactly such a database for months without anything
        saying so.
        """
        try:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM lab_results").fetchone()[0])
        except sqlite3.Error:
            return 0

    def fit_guard_banner(self) -> List[str]:
        """Lines describing the guard's current state, for logs and banners."""
        lines: List[str] = []
        for f in self.blocking_findings():
            lines.append(f"BLOCKED [{self._TIER_LABEL[f.tier]}]: {f.headline}")
            lines.append(f"    {f.detail}")
            if f.row_ids:
                lines.append(f"    Lab result id(s): "
                             + ", ".join(str(i) for i in f.row_ids))
        rec = self.fit_override_record()
        if rec:
            lines.append("OVERRIDDEN STRENGTH FIT — this design is built on a "
                         "fit the sanity guard rejected.")
            lines.append(f"    Released by: {rec['source']}")
            for h in rec["findings"]:
                lines.append(f"    Flagged: {h}")
            if rec["row_ids"]:
                lines.append(f"    Lab result id(s): "
                             + ", ".join(str(i) for i in rec["row_ids"]))
        return lines

    def tensile_viability_floor(self, min_ft_psi: float = 1.0):
        """
        Lightest density worth searching, and the basis for it.

        Returns ``(density_pcf, basis_str)``.

        DERIVED FROM THE TENSILE TIER IN FORCE -- MOR (f_r) when a 3-point
        bending fit exists, otherwise split-tensile (f_t).  Never from f'c.

        WHY NOT f'c.  This floor used to be ``(10 - intercept_fc)/slope_fc``,
        the density at which COMPRESSIVE strength reaches 10 psi.  The two fits
        do not cross zero together: on the shipped lab data f'c reaches 10 psi
        at 57.5 pcf while f_t is still zero until 59.3 pcf.  The grid therefore
        began 1.8 pcf below any usable tensile capacity, and since the
        branch-and-bound walks LIGHTEST-FIRST it evaluated that dead band on
        every run, before the incumbent existed to prune against it.  Every
        bending gate in this suite limits on TENSION (Kupfer's sigma_1/f_t
        term), so a density with no tensile capacity cannot pass whatever its
        compressive strength.

        Stays dynamic: the fit is re-trained from the lab table on load and
        after every lab edit, so this floor follows new batches automatically.
        """
        import numpy as _np
        if self.fr_regression is not None:
            slope, intercept = self.fr_regression
            tier = f"flexural MOR (n={getattr(self, '_regression_n_fr', 0)})"
        elif self.ft_regression is not None:
            slope, intercept = self.ft_regression
            # n of the TENSILE tier, not _regression_n (which counts the f'c
            # points) — the two diverge whenever a season measures more
            # cylinders than beams, and this string is the floor's provenance.
            tier = f"split-tensile (n={getattr(self, '_regression_n_ft', 0)})"
        else:
            return 59.4, "empirical fallback (no fit; need >=2 lab points)"
        if slope <= 0:
            return 59.4, f"{tier}, non-positive slope -- fallback"
        floor = (float(min_ft_psi) - float(intercept)) / float(slope)
        zero = -float(intercept) / float(slope)
        floor = max(round(floor, 1), 40.0)
        return floor, (f"{tier}: zero-strength at {zero:.2f} pcf, "
                       f"{min_ft_psi:.0f} psi at {floor:.1f} pcf")

    def get_regression_stats(self) -> str:
        """Returns a formatted string of the current regression for the UI."""
        if self.fc_regression is None and self.ft_regression is None:
            return "Status: Using empirical defaults (need ≥ 2 data points to train)"

        src = f"{self._regression_source} (n={self._regression_n})"
        if self.fc_regression is not None:
            fc_eq = (
                f"f'c = {self.fc_regression[0]:.2f}ρ + {self.fc_regression[1]:.2f}"
                f"  (R²={self.r_squared_fc:.3f}, n={self._regression_n})"
            )
        else:
            fc_eq = (f"f'c = (empirical — need ≥2 compressive breaks; "
                     f"have {self._regression_n})")
        if self.ft_regression is not None:
            ft_eq = (
                f"ft = {self.ft_regression[0]:.2f}ρ + {self.ft_regression[1]:.2f}"
                f"  (R²={self.r_squared_ft:.3f}, n={self._regression_n_ft})"
            )
        else:
            ft_eq = (f"ft = (empirical — need ≥2 tensile breaks; "
                     f"have {self._regression_n_ft})")
        if self.fr_regression is not None:
            fr_eq = (
                f"fr = {self.fr_regression[0]:.2f}ρ + {self.fr_regression[1]:.2f}"
                f"  (R²={self.r_squared_fr:.3f}, n={self._regression_n_fr})"
                "  ← sweep bending gate"
            )
        else:
            fr_eq = (f"fr = (no MOR fit — need ≥2 fr points; "
                     f"have {self._regression_n_fr}) — bending gate uses ft")
        held = sum(1 for r in self.get_lab_results(limit=100000)
                   if self.fit_eligibility(r))
        out = f"Source: {src} | {fc_eq} | {ft_eq} | {fr_eq}"
        for f in self.blocking_findings():
            out += (f" | ⛔ {self._TIER_LABEL[f.tier]} BLOCKED by the sanity "
                    f"guard: {f.headline}")
        if self.fit_override_record():
            out += (" | ⚠ OVERRIDDEN FIT IN FORCE — this design is built on a "
                    "fit the sanity guard rejected")
        if held:
            out += (f" | {held} lab row(s) held out of the fit (early-age "
                    f"break or no density) — see the Lab Results table")
        return out

    # ──────────────────────  REGRESSION VERSIONING  ───────────────────
    def save_regression_version(self, notes: str = "", version_tag: str = "") -> int:
        """
        Snapshot the current in-memory regression into regression_versions.

        Called automatically after each lab_result insert; also callable
        explicitly by the user via the Mix Lab tab "Publish" button.

        Returns the new row id, or 0 if no regression is currently fit.
        """
        if self.fc_regression is None or self.ft_regression is None:
            return 0

        if not version_tag:
            version_tag = f"auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        cur = self._exec_with_retry(
            """INSERT INTO regression_versions
               (version_tag, slope_fc, intercept_fc, r2_fc,
                slope_ft, intercept_ft, r2_ft,
                num_points, data_source, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_tag,
                float(self.fc_regression[0]), float(self.fc_regression[1]),
                float(self.r_squared_fc),
                float(self.ft_regression[0]), float(self.ft_regression[1]),
                float(self.r_squared_ft),
                self._regression_n,
                self._regression_source,
                notes,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def publish_regression(self, version_id: int, notes: str = "") -> None:
        """
        Mark a specific regression version as published (is_published=1).

        Clears the published flag from all other versions first so there is
        exactly one published version at any time.
        """
        self._exec_with_retry(
            "UPDATE regression_versions SET is_published=0"
        )
        self._exec_with_retry(
            "UPDATE regression_versions SET is_published=1, notes=? WHERE id=?",
            (notes, version_id),
        )
        self._conn.commit()
        # Re-train so the ACTIVE fit reflects the published state at once:
        # publishing a manual version pins it; publishing anything else
        # unpins a previous manual and returns to the live lab-data fit.
        self._train_regression_curves()

    def publish_current_regression(self, version_tag: str = "", notes: str = "") -> int:
        """
        Convenience: save the current in-memory fit as a named version and
        immediately publish it.  Returns the new version id.
        """
        if not version_tag:
            # Number sequentially: v1, v2, …
            count = self._conn.execute(
                "SELECT COUNT(*) FROM regression_versions"
            ).fetchone()[0]
            version_tag = f"v{count + 1}-{datetime.now().strftime('%Y-%m-%d')}"

        version_id = self.save_regression_version(notes=notes, version_tag=version_tag)
        if version_id:
            self.publish_regression(version_id, notes=notes)
        return version_id

    def set_manual_regression(self, slope_fc: float, intercept_fc: float,
                              slope_ft: float, intercept_ft: float,
                              version_tag: str = "", notes: str = "",
                              publish: bool = True) -> int:
        """
        Store a USER-ENTERED linear relationship (fix plan Tab-4):

            f'c = slope_fc·ρ + intercept_fc      ft = slope_ft·ρ + intercept_ft

        for teams that have an equation (e.g. from a previous season or a
        reference paper) but no raw cylinder data.  Saved as a regression
        version with data_source='manual'.  When *publish* is True it becomes
        the ACTIVE relationship: _train_regression_curves pins a published
        manual version above lab-data fits until another version is
        published, and it survives restarts and lab-result edits.

        Returns the new version id.
        """
        if not version_tag:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM regression_versions").fetchone()[0]
            version_tag = f"manual-v{count + 1}-{datetime.now().strftime('%Y-%m-%d')}"

        cur = self._exec_with_retry(
            """INSERT INTO regression_versions
               (version_tag, slope_fc, intercept_fc, r2_fc,
                slope_ft, intercept_ft, r2_ft,
                num_points, data_source, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (version_tag, float(slope_fc), float(intercept_fc), 0.0,
             float(slope_ft), float(intercept_ft), 0.0,
             0, "manual", notes),
        )
        self._conn.commit()
        vid = cur.lastrowid
        if publish:
            self.publish_regression(vid, notes=notes)
        self._train_regression_curves()
        return vid

    # ──────────────────────  LAB-RESULT CSV EXCHANGE  ─────────────────
    # Spreadsheet-based sharing between teammates (fix plan Tab-4): CSV
    # opens directly in Excel / Google Sheets; import de-duplicates so the
    # same file can be merged from several members without double entries.
    LAB_CSV_FIELDS = ["test_date", "batch_id", "curing_days", "specimen_type",
                      "density_pcf", "dry_density_pcf", "wet_density_pcf",
                      "fc_psi", "ft_psi", "fr_psi",
                      "fiber_type", "fiber_vol_pct",
                      "specimen_mass_g", "mix_designer", "notes"]

    def export_lab_results_csv(self, path: str) -> int:
        """Write every lab result to *path* (CSV). Returns the row count."""
        import csv
        rows = self.get_lab_results(limit=100000)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=self.LAB_CSV_FIELDS,
                               extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return len(rows)

    # A break is the same break when it shares this identity; strengths are
    # then MERGED into the existing row rather than logged twice.  Density is
    # rounded to 0.1 pcf so a spreadsheet's extra decimals do not fork a row.
    @staticmethod
    def _lab_identity(test_date, batch_id, specimen_type,
                      curing_days, density_pcf) -> tuple:
        return (str(test_date or "").strip(),
                str(batch_id or "").strip().lower(),
                str(specimen_type or "").strip().lower(),
                int(curing_days or 0),
                round(float(density_pcf or 0.0), 1))

    _STRENGTH_FIELDS = ("fc_psi", "ft_psi", "fr_psi")

    @staticmethod
    def _strengths_agree(row: dict, rec) -> bool:
        """True when nothing measured in both places contradicts.

        Only then may the incoming record fill the existing row's gaps; a real
        disagreement means two different specimens (or a corrected number), and
        neither may be silently overwritten.
        """
        for f in MixDatabase._STRENGTH_FIELDS:
            a = float(row.get(f) or 0.0)
            b = float(getattr(rec, f, 0.0) or 0.0)
            if a > 0 and b > 0 and abs(a - b) > max(1.0, 0.005 * a):
                return False
        return True

    def _import_records(self, records) -> dict:
        """Merge parsed :class:`LabRecord`s into lab_results, no refit.

        Returns counts plus the conflict messages.  A record either fills the
        gaps of the break already on file (a flexural sheet completing a
        compressive one), duplicates it exactly, or is logged as its own row.
        """
        def _numbers(row_or_rec, get) -> tuple:
            """The measurements alone, with no name attached."""
            return tuple(round(float(get(row_or_rec, f) or 0.0), 1)
                         for f in ("density_pcf", "fc_psi", "ft_psi", "fr_psi"))

        index: dict = {}
        by_numbers: dict = {}
        for r in self.get_lab_results(limit=100000):
            index.setdefault(self._lab_identity(
                r["test_date"], r["batch_id"], r.get("specimen_type"),
                r.get("curing_days"), r["density_pcf"]), []).append(dict(r))
            by_numbers.setdefault(
                _numbers(r, lambda o, f: o.get(f)), set()).add(
                    (r["batch_id"] or "").strip().lower())

        added = updated = skipped = 0
        conflicts: List[str] = []
        renamed: List[str] = []

        for rec in records:
            key = self._lab_identity(rec.test_date, rec.batch_id,
                                     rec.specimen_type, rec.curing_days,
                                     rec.density_pcf)
            siblings = index.get(key, [])
            match = next((row for row in siblings
                          if self._strengths_agree(row, rec)), None)

            # Identical numbers filed under a different mix name mean the
            # same break was logged twice — the usual cause is a sheet whose
            # mix names were edited between exports, or a row that slipped a
            # line while the numbers stayed put.
            nums = _numbers(rec, lambda o, f: getattr(o, f, 0.0))
            names = by_numbers.get(nums, set())
            if nums[0] > 0 and names and \
                    (rec.batch_id or "").strip().lower() not in names:
                renamed.append(
                    f"line {rec.source_row}: '{rec.batch_id or 'unnamed'}' "
                    f"repeats numbers already on file under "
                    f"'{sorted(names)[0]}'")

            if match is None:
                if siblings:
                    conflicts.append(
                        f"line {rec.source_row} ({rec.batch_id or 'unnamed'}): "
                        "same specimen and date already on file with a "
                        "different strength — logged as a separate row, "
                        "delete whichever is wrong")
                new_id = self.add_lab_result(
                    test_date=rec.test_date, density_pcf=rec.density_pcf,
                    fc_psi=rec.fc_psi, ft_psi=rec.ft_psi, fr_psi=rec.fr_psi,
                    batch_id=rec.batch_id, fiber_type=rec.fiber_type,
                    fiber_vol_pct=rec.fiber_vol_pct,
                    specimen_mass_g=rec.specimen_mass_g,
                    mix_designer=rec.mix_designer, notes=rec.notes,
                    curing_days=rec.curing_days,
                    specimen_type=rec.specimen_type,
                    dry_density_pcf=rec.dry_density_pcf,
                    wet_density_pcf=rec.wet_density_pcf,
                    refit=False)
                row = {"id": new_id, "test_date": rec.test_date,
                       "batch_id": rec.batch_id,
                       "specimen_type": rec.specimen_type,
                       "curing_days": rec.curing_days,
                       "density_pcf": rec.density_pcf,
                       "dry_density_pcf": rec.dry_density_pcf,
                       "wet_density_pcf": rec.wet_density_pcf,
                       "fc_psi": rec.fc_psi, "ft_psi": rec.ft_psi,
                       "fr_psi": rec.fr_psi, "notes": rec.notes}
                index.setdefault(key, []).append(row)
                by_numbers.setdefault(nums, set()).add(
                    (rec.batch_id or "").strip().lower())
                added += 1
                continue

            fills = {f: float(getattr(rec, f) or 0.0)
                     for f in ("fc_psi", "ft_psi", "fr_psi",
                               "dry_density_pcf", "wet_density_pcf")
                     if float(getattr(rec, f) or 0.0) > 0.0
                     and float(match.get(f) or 0.0) <= 0.0}
            if rec.notes and not (match.get("notes") or "").strip():
                fills["notes"] = rec.notes
            if not fills:
                skipped += 1
                continue
            assigns = ", ".join(f"{f}=?" for f in fills)
            self._exec_with_retry(
                f"UPDATE lab_results SET {assigns} WHERE id=?",
                (*fills.values(), match["id"]))
            match.update(fills)
            updated += 1

        if added or updated:
            self._conn.commit()
        if renamed:
            conflicts.append(
                f"{len(renamed)} result(s) carry numbers already on file under "
                "a different mix name. Either the same break was recorded "
                "twice under two names, or a row in the sheet slipped out of "
                "line with its mix name. Check: " + "; ".join(renamed[:4])
                + (" …" if len(renamed) > 4 else ""))
        return {"added": added, "updated": updated, "skipped": skipped,
                "conflicts": conflicts}

    def import_lab_csv(self, path: str, *, density_basis: Optional[str] = None,
                       refit: bool = True) -> "LabImportReport":
        """
        Import one lab-results CSV — the team's flexural sheet, its
        compressive sheet, or this suite's own export format.  ONE TEST TYPE
        PER FILE: a sheet holding both tables side by side is refused (the
        report's ``imported`` is False and ``warnings`` says why), because its
        two halves share one set of mix-name and date columns without their
        rows lining up.

        The layout is detected from the file (see :mod:`data.lab_csv`), missing
        densities and flexural strengths are computed from the weights and
        break loads the sheet does record, and results already on file have
        their gaps filled instead of being duplicated.

        Returns a :class:`LabImportReport`.  The regression is refit ONCE
        (one auto snapshot) unless ``refit=False``.
        """
        from data.lab_csv import parse_lab_csv
        if density_basis is None:
            try:
                from config.settings import get_settings
                density_basis = get_settings().mix.density_basis
            except ImportError:
                density_basis = "dry"

        parsed = parse_lab_csv(path, density_basis=density_basis)
        counts = self._import_records(parsed.records)
        report = LabImportReport(
            path=path, layout=parsed.layout,
            added=counts["added"], updated=counts["updated"],
            skipped=counts["skipped"], invalid=parsed.invalid_rows,
            blank=parsed.blank_rows, notes=list(parsed.notes),
            warnings=list(parsed.warnings) + counts["conflicts"]
                     + list(parsed.dropped),
            density_basis=density_basis)
        if refit and (report.added or report.updated):
            self._train_regression_curves()
            self.save_regression_version(
                notes=f"auto-refit after CSV import of "
                      f"{os.path.basename(path)} "
                      f"(+{report.added} new, {report.updated} completed)")
        self._account_for_fit_eligibility(report)
        return report

    def _account_for_fit_eligibility(self, report: "LabImportReport") -> None:
        """
        Say what the import actually bought the FIT, not just the table.

        Loudly, and by reason: a teammate reading "added=4" has no way to know
        those four rows were excluded from every regression.  If NOTHING is
        admitted, that is a visible failure -- never a quiet fallback to the
        seeded mixes table, which is design intent rather than measurement.
        """
        admitted, excluded = 0, {}
        try:
            rows = self.get_lab_results(limit=100000)
        except Exception as _e:
            # Say that the CHECK failed. Silence here plus a True default was
            # a confidence claim manufactured from an error.
            report.fit_usable = False
            report.warnings.append(
                "!! COULD NOT VERIFY what this import gave the strength fit "
                "(%s: %s). Treat the fit as UNCONFIRMED rather than fine."
                % (type(_e).__name__, _e))
            return
        for r in rows:
            d = dict(r)
            if not any(float(d.get(k) or 0.0) > 0.0
                       for k in ("fc_psi", "ft_psi", "fr_psi")):
                continue                       # no measurement to contribute
            why = self.fit_eligibility(d)
            if why:
                excluded[why] = excluded.get(why, 0) + 1
            else:
                admitted += 1
        report.fit_admitted = admitted
        report.fit_excluded = excluded
        report.fit_sources = {
            "strength": getattr(self, "_regression_source", None) or "unknown",
            "n": getattr(self, "_regression_n", 0),
        }
        report.fit_usable = admitted > 0

        for why, n in sorted(excluded.items()):
            report.warnings.append(
                "%d row(s) EXCLUDED FROM EVERY FIT: %s. They are in the table "
                "but no regression can see them." % (n, why))
            if why == "age not recorded":
                report.warnings.append(
                    "  -> add a curing-age column to the sheet (any of: "
                    "'curing days', 'curing time days', 'cure days', "
                    "'age days'). A break of unknown age could be a 7-day "
                    "break, which is what the early-age exclusion holds out.")
        if not report.fit_usable:
            report.warnings.append(
                "!! NO IMPORTED ROW REACHED ANY REGRESSION. The strength fit "
                "is running on %s -- for the seeded 'mixes' source that is "
                "DESIGN INTENT, not measurement, and every density floor and "
                "predicted strength below is built on it. Fix the sheet and "
                "re-import before trusting any weight."
                % (report.fit_sources.get("strength"),))

    def import_lab_csv_files(self, paths, *,
                             density_basis: Optional[str] = None) -> list:
        """Import several CSVs (e.g. the flexural AND compressive sheets) with
        ONE regression refit at the end.  Returns a report per file."""
        reports = [self.import_lab_csv(p, density_basis=density_basis,
                                       refit=False) for p in paths]
        if any(r.added or r.updated for r in reports):
            self._train_regression_curves()
            total = sum(r.added for r in reports)
            done = sum(r.updated for r in reports)
            self.save_regression_version(
                notes=f"auto-refit after importing {len(reports)} CSV file(s) "
                      f"(+{total} new, {done} completed)")
        return reports

    def import_lab_results_csv(self, path: str) -> tuple:
        """Back-compatible wrapper: returns ``(added, skipped)``.

        Rows that only completed an existing break count as added, since the
        caller's "did anything land?" question is yes.
        """
        rep = self.import_lab_csv(path)
        return rep.added + rep.updated, rep.skipped + rep.invalid

    def get_regression_versions(self) -> List[dict]:
        """Return all regression snapshots, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM regression_versions ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_published_regression(self) -> Optional[dict]:
        """Return the currently published regression version dict, or None."""
        row = self._conn.execute(
            "SELECT * FROM regression_versions WHERE is_published=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_active_version_tag(self) -> str:
        """Return the version tag of the published regression, or 'unpublished'."""
        pub = self.get_published_regression()
        return pub["version_tag"] if pub else "unpublished"

    # ──────────────────────  LAB RESULTS CRUD  ────────────────────────
    def add_lab_result(
        self,
        test_date: str,
        density_pcf: float = 0.0,
        fc_psi: float = 0.0,
        ft_psi: float = 0.0,
        fr_psi: float = 0.0,
        batch_id: str = "",
        fiber_type: str = "PVA",
        fiber_vol_pct: float = 2.0,
        specimen_mass_g: float = 0.0,
        mix_designer: str = "",
        notes: str = "",
        curing_days: int = 0,
        specimen_type: str = "",
        dry_density_pcf: float = 0.0,
        wet_density_pcf: float = 0.0,
        refit: bool = True,
    ) -> int:
        """
        Record a cylinder / beam break result and immediately refit the
        regressions (ft and, when 3-point-bending is supplied, fr).

        ``density_pcf`` is the single value the regression reads.  Pass it
        directly, or pass ``dry_density_pcf`` / ``wet_density_pcf`` and let the
        configured basis choose between them.

        ``refit=False`` skips the retrain and snapshot for bulk loads — the
        caller must refit once at the end.

        Returns the new row id.
        """
        density_pcf = float(density_pcf or 0.0)
        if density_pcf <= 0.0:
            basis = "dry"
            try:
                from config.settings import get_settings
                basis = get_settings().mix.density_basis
            except ImportError:
                pass
            order = ((wet_density_pcf, dry_density_pcf) if basis == "wet"
                     else (dry_density_pcf, wet_density_pcf))
            density_pcf = float(next((d for d in order if d and d > 0), 0.0))

        cur = self._exec_with_retry(
            """INSERT INTO lab_results
               (test_date, batch_id, density_pcf, fc_psi, ft_psi, fr_psi,
                curing_days, specimen_type, dry_density_pcf, wet_density_pcf,
                fiber_type, fiber_vol_pct, specimen_mass_g,
                mix_designer, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (test_date, batch_id, density_pcf, fc_psi, ft_psi, fr_psi,
             int(curing_days or 0), specimen_type,
             float(dry_density_pcf or 0.0), float(wet_density_pcf or 0.0),
             fiber_type, fiber_vol_pct, specimen_mass_g,
             mix_designer, notes),
        )
        self._conn.commit()
        if refit:
            self._train_regression_curves()
            self.save_regression_version(
                notes=f"auto-refit after lab result id={cur.lastrowid}"
            )
        return cur.lastrowid

    def get_lab_results(self, limit: int = 500) -> List[dict]:
        """Return lab results newest first, up to *limit* rows."""
        rows = self._conn.execute(
            "SELECT * FROM lab_results ORDER BY test_date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_lab_result(self, result_id: int) -> None:
        self._exec_with_retry("DELETE FROM lab_results WHERE id=?", (result_id,))
        self._conn.commit()
        self._train_regression_curves()

    def delete_lab_results(self, result_ids) -> int:
        """Delete several lab results in ONE transaction with ONE regression
        refit (fix plan Tab-4 multi-delete).  Returns rows removed."""
        ids = [int(i) for i in result_ids]
        if not ids:
            return 0
        for rid in ids:
            self._exec_with_retry("DELETE FROM lab_results WHERE id=?", (rid,))
        self._conn.commit()
        self._train_regression_curves()
        return len(ids)

    # ──────────────────────  MIXES CRUD  ──────────────────────────────
    def _is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) FROM mixes").fetchone()
        return row[0] == 0

    def add_mix(self, name: str, density_pcf: float,
                fc_psi: float, ft_psi: float,
                fiber_type: str = "PVA", fiber_vol_pct: float = 2.0,
                description: str = "") -> int:
        cur = self._exec_with_retry(
            """INSERT INTO mixes
               (name, density_pcf, fc_psi, ft_psi,
                fiber_type, fiber_vol_pct, description)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, density_pcf, fc_psi, ft_psi,
             fiber_type, fiber_vol_pct, description)
        )
        self._conn.commit()
        self._train_regression_curves()
        return cur.lastrowid

    def update_mix(self, mix_id: int, **kwargs):
        allowed = {"name", "density_pcf", "fc_psi", "ft_psi",
                   "fiber_type", "fiber_vol_pct", "description"}
        cols = {k: v for k, v in kwargs.items() if k in allowed}
        if not cols:
            return
        set_clause = ", ".join(f"{k}=?" for k in cols)
        vals = list(cols.values()) + [mix_id]
        self._exec_with_retry(
            f"UPDATE mixes SET {set_clause} WHERE id=?", tuple(vals)
        )
        self._conn.commit()
        self._train_regression_curves()

    def delete_mix(self, mix_id: int):
        self._exec_with_retry("DELETE FROM mixes WHERE id=?", (mix_id,))
        self._conn.commit()
        self._train_regression_curves()

    def get_all_mixes(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mixes ORDER BY density_pcf DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_existing_mix_densities(self) -> List[float]:
        """
        Every density the team actually HAS a mix record for [pcf], ascending.

        The union of the mix library (``mixes``) and the tested batches
        (``lab_results``) — both are real, batchable mixes from the Mix Lab's
        point of view, so both count as "existing".  Densities are rounded to
        0.1 pcf before de-duplication so a design and its lab batch recorded at
        the same nominal density collapse to one entry.

        This is what the sweep's "existing mixes only" constraint searches
        instead of the continuous predicted-density spectrum.
        """
        vals = [r["density_pcf"] for r in self.get_all_mixes()]
        vals += [r["density_pcf"] for r in self.get_lab_results(limit=100000)]
        return sorted({round(float(v), 1) for v in vals if v is not None})

    def get_mix_by_id(self, mix_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM mixes WHERE id=?", (mix_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_mix_by_name(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM mixes WHERE name=?", (name,)
        ).fetchone()
        return dict(row) if row else None

    # ──────────────────────  TEST CYLINDERS  ──────────────────────────
    def add_test_result(self, mix_id: int, test_date: str,
                        fc: float, ft: float, density: float,
                        notes: str = "") -> int:
        cur = self._exec_with_retry(
            """INSERT INTO test_cylinders
               (mix_id, test_date, fc_measured, ft_measured,
                density_measured, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (mix_id, test_date, fc, ft, density, notes)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_test_results(self, mix_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM test_cylinders WHERE mix_id=? ORDER BY test_date",
            (mix_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────  DEFAULT SEED  ────────────────────────────
    def seed_defaults(self):
        defaults = [
            ("Mix 1 – Impact (Ends)", 80.0,
             "Densest pour zone: bow/stern impact regions"),
            ("Mix 2 – Tension (Below NA)", 70.0,
             "Moderate density: tension fiber below neutral axis"),
            ("Mix 3 – Compression (Above NA)", 60.0,
             "Lightest viable: compression zone above neutral axis"),
        ]
        for name, density, desc in defaults:
            fc = _empirical_fc(density)
            ft = _empirical_ft(fc)
            try:
                self.add_mix(name, density, fc, ft,
                             fiber_type="PVA", fiber_vol_pct=2.0,
                             description=desc)
            except sqlite3.IntegrityError:
                pass

    # ──────────────────────  STRUCTURAL SWEEP GRID  ───────────────────
    def get_structural_sweep_grid(self, fos: float = 1.5,
                                  max_density: float = 80.0,
                                  n_mixes: int = 2,
                                  step: float = None,
                                  existing_only: bool = False,
                                  min_density: float = None) -> List[List[float]]:
        """
        Generate density tuples for the structural sweep.

        n_mixes=1 : single-element lists — uniform hull
        n_mixes=2 : (heavy, light) pairs,  m1 >= m2 + 2  pcf spacing
        n_mixes=3 : (heavy, medium, light) triples, each step >= 2 pcf;
                    uses a coarser 2 pcf step to keep the grid tractable.

        step : override the density resolution [pcf].  The multi-zone sweep
               passes a coarse step (the vertical stacking permutations and
               pour-line elevations already provide the design richness, so a
               fine density grid would just explode the config count).

        min_density : the team's density FLOOR [pcf], or None for no floor of
               their own.  A manufacturing constraint like ``existing_only``:
               "we will not batch below this", not a claim about strength.  It
               is applied ON TOP of the tensile viability floor -- whichever is
               higher wins -- because a user floor BELOW the viable one would
               only admit mixes the regression says have no strength.  Like
               that floor it FILTERS the anchored grid and never re-phases it
               (see the anchor note below).

        existing_only : a MANUFACTURING constraint, alongside N_mixes_max.  The
               sweep normally searches a continuous density spectrum and trusts
               the regression to predict strengths for mixes the team has never
               batched.  With this set it searches ONLY densities that exist in
               the Mix Lab (get_existing_mix_densities), so the winner is a mix
               that can actually be poured rather than an interpolated one.
               ``step`` is meaningless in this mode and is ignored; the density
               ceiling still applies.  Falls back to the continuous grid if the
               Mix Lab holds fewer than ``n_mixes`` usable densities — an empty
               grid would report as "0 configurations" and blame the physics.
        """
        _user_floor = None if min_density is None else float(min_density)

        if existing_only:
            densities = [d for d in self.get_existing_mix_densities()
                         if d <= float(max_density) + 1e-9
                         and (_user_floor is None or d >= _user_floor - 1e-9)]
            if len(densities) >= max(int(n_mixes), 1):
                return self._combos_from_densities(densities, n_mixes)
            logger.warning(
                "existing-mixes-only: the Mix Lab holds %d mix densities at or "
                "below %.1f pcf, fewer than the %d required — falling back to "
                "the continuous density grid for this sweep.",
                len(densities), float(max_density), int(n_mixes))

        min_viable, _floor_basis = self.tensile_viability_floor()
        # The team's floor can only RAISE the viable one.  Taking the user's
        # value when it is lower would admit densities the regression predicts
        # no strength for -- every one of which is a config that gets built,
        # evaluated and rejected, so it costs sweep time and buys nothing.
        if _user_floor is not None:
            min_viable = max(min_viable, _user_floor)

        if step is None:
            step = 2.0 if n_mixes == 3 else 0.5

        # The floor FILTERS an anchored grid; it must never DEFINE the grid.
        #
        # np.arange(min_viable, ...) re-phases every point: moving the floor
        # 57.5 -> 59.3 turned the grid 57.5/59.5/61.5/.../71.5 into
        # 59.3/61.3/63.3/.../73.3, so the winning stack [71.5, 63.5, 71.5]
        # stopped EXISTING and the winner moved +0.53 lb (167.5118 -> 168.0412).
        # Pruning a dead band is only free if the surviving densities are
        # untouched, so the anchor stays fixed and the floor merely drops the
        # points below it.
        # The anchor is the HISTORICAL grid phase (the old compressive-derived
        # floor), kept solely so the surviving densities keep their values.
        # It is arbitrary but FIXED: any other phase is a different search grid
        # and moves winners, so it is not a knob.  The tensile floor filters
        # this grid; it does not re-phase it.
        _ANCHOR_PCF = 57.5
        _grid = np.arange(_ANCHOR_PCF, max_density + 0.01, step)
        # Round to 4 dp: np.arange accumulates float error, so a 0.1 step
        # yields 59.300000000000026 rather than 59.3.  Those values reach
        # mix lookups, equality tests against the Mix Lab densities, and
        # the report.  Every step this grid supports (>=0.05) is exact at
        # 4 dp, so this cleans the noise without moving any grid point.
        densities = [round(float(d), 4) for d in _grid
                     if d >= min_viable - 1e-9]
        # Always include the strongest allowed mix — a coarse step can overshoot
        # max_density and drop it, starving thin-wall designs of capacity.
        if not densities or abs(densities[-1] - max_density) > 1e-6:
            densities.append(float(max_density))

        return self._combos_from_densities(densities, n_mixes)

    @staticmethod
    def _combos_from_densities(densities, n_mixes: int) -> List[List[float]]:
        """
        Density tuples from a list of available densities — the ONE place the
        combo rule lives, so the continuous grid and the existing-mixes-only
        grid can never drift apart.

        Descending within a tuple with >= 2 pcf between neighbours: two mixes
        closer than that are not distinguishable batches on the pour floor.
        """
        if n_mixes == 1:
            return [[m] for m in densities]
        if n_mixes == 2:
            return [[m1, m2] for m1 in densities for m2 in densities
                    if m1 >= m2 + 2.0]
        # n_mixes == 3
        return [
            [m1, m2, m3]
            for m1 in densities
            for m2 in densities
            for m3 in densities
            if m1 >= m2 + 2.0 and m2 >= m3 + 2.0
        ]

    # ──────────────────────  MIX PROPERTIES LOOKUP  ──────────────────
    def get_mix_properties(self, density_pcf: float,
                           fos: float = 1.5) -> dict:
        """
        Predict allowable structural properties for any theoretical density.
        Uses the current in-memory regression; falls back to empirical formula.

        Flexural allowable (the one the sweep's bending gate uses):
          * strength_r   — from the f_r / MOR fit, or None if no MOR fit yet.
          * strength_flexural — MOR when available, else the direct-tensile
            allowable (a conservative fallback, since f_t <= f_r for concrete).
          * flexural_source — "fr" or "ft", so callers/reports can say which
            limit governed.
        All three are already divided by the factor of safety, like strength_t.
        """
        # Per-tier fallback: a blocked or unfittable f'c must not drag a sound
        # f_t back to the empirical constants with it.
        if self.fc_regression is not None:
            fc = float(np.polyval(self.fc_regression, density_pcf))
        else:
            fc = _empirical_fc(density_pcf)
        if self.ft_regression is not None:
            ft = float(np.polyval(self.ft_regression, density_pcf))
        else:
            ft = _empirical_ft(fc)

        fc = max(fc, 10.0)
        ft = max(ft, 5.0)
        allow_t = ft / fos

        if self.fr_regression is not None:
            fr = max(float(np.polyval(self.fr_regression, density_pcf)), 5.0)
            allow_r = fr / fos
            flexural = allow_r
            source = "fr"
        else:
            allow_r = None
            flexural = allow_t              # fallback: direct-tensile allowable
            source = "ft"

        return {
            "density":          density_pcf,
            "strength_c":       fc / fos,
            "strength_t":       allow_t,
            "strength_r":       allow_r,
            "strength_flexural": flexural,
            "flexural_source":  source,
        }

    # ──────────────────────  MIX DEVELOPMENT RECOMMENDATIONS  ─────────
    def recommend_mix_targets(self, *, zone_densities=None, bend_dc=None,
                              hoop_dc=None, density_at_floor=False,
                              fos: float = 1.5) -> dict:
        """
        What density / strength the mix team should develop and hit NEXT.

        Two modes, chosen by whether a structural design is supplied:

        * DESIGN mode (``zone_densities`` given): for each distinct target
          density in the winning I-beam, the RAW strengths the design was
          checked against (``ft = slope·ρ + int`` etc., BEFORE the factor of
          safety, because the lab measures raw cylinder strength), the margin
          the design leaves (its D/C), and whether that density sits inside or
          outside the measured lab-data range.  Plus improvement levers: when
          the light web is pinned at the viability floor, a mix that keeps
          tensile strength below that floor lets the whole hull get lighter.

        * DATA-GAP mode (no design yet): guidance built from the regression and
          the lab-data coverage alone --- which densities to break next so the
          fit stops extrapolating.

        Pure and Qt-free: the caller extracts ``zone_densities`` / D-C /
        ``density_at_floor`` from the sweep result and passes primitives.
        Returns a dict: ``mode``, ``targets`` [], ``improvements`` [], ``notes`` [].
        """
        out = {"mode": "data_gap", "targets": [], "improvements": [], "notes": []}
        if self.ft_regression is None or self.fc_regression is None:
            out["notes"].append(
                "No density→strength fit yet — log a few cylinder breaks to seed "
                "the regression before targets can be recommended.")
            return out

        ft_m, ft_b = float(self.ft_regression[0]), float(self.ft_regression[1])
        fc_m, fc_b = float(self.fc_regression[0]), float(self.fc_regression[1])
        # Flexural (f_r / MOR) fit, if the team has logged >= 2 bending points.
        # It is what the sweep's bending gate uses; otherwise the gate falls back
        # to f_t.  Report both, and mark which one currently governs.
        fr_m = fr_b = None
        if self.fr_regression is not None:
            fr_m, fr_b = float(self.fr_regression[0]), float(self.fr_regression[1])
        governing = "fr" if self.fr_regression is not None else "ft"
        out["governing_strength"] = governing
        # Density where the fit predicts zero tensile strength — the physical
        # light-mix floor (nothing lighter carries tension per the current fit).
        ft_floor = (-ft_b / ft_m) if ft_m > 0 else None

        def _fr_raw(rho):
            return None if fr_m is None else max(fr_m * rho + fr_b, 0.0)

        lab = self.get_lab_results()
        lab_dens = sorted(float(r["density_pcf"]) for r in lab)
        lo, hi = (min(lab_dens), max(lab_dens)) if lab_dens else (None, None)
        n = len(lab_dens)

        def _raw(rho):
            return (max(fc_m * rho + fc_b, 0.0), max(ft_m * rho + ft_b, 0.0))

        def _confidence(rho):
            if not lab_dens:
                return "no lab data — measure to confirm"
            gap = min(abs(rho - d) for d in lab_dens)
            if lo <= rho <= hi:
                return ("interpolation (within lab data)" if gap <= 3.0
                        else f"sparse — nearest lab point {gap:.1f} pcf away")
            return "EXTRAPOLATION — outside lab data; test to confirm"

        if n <= 2:
            out["notes"].append(
                f"The fit rests on {n} lab point(s): it is a two-point line "
                "(R²=1 by construction), not a validated trend. Breaking "
                "specimens at a third, intermediate density is the single most "
                "valuable next test.")

        if zone_densities:
            out["mode"] = "design"
            if governing == "fr":
                out["notes"].insert(0,
                    "Bending gate governs on the flexural strength f_r (MOR) — "
                    "the correct limit for the bending stress σ=My/I. Develop "
                    "mixes to the f_r target; f_t is shown for reference.")
            else:
                out["notes"].insert(0,
                    "No MOR (f_r) fit yet, so the bending gate falls back to the "
                    "direct-tensile f_t (conservative, f_t ≤ f_r). Log ≥2 "
                    "3-point-bending results to switch the gate to f_r.")
            zds = [float(d) for d in zone_densities]
            n_zones = len(zds)
            # Group identical densities and tag their structural role.  The
            # extreme zones (first/last) are the flanges (bending-critical
            # extreme fibres); interior zones are the web (hoop-critical, at the
            # hull's widest point).
            groups = {}
            for i, d in enumerate(zds):
                groups.setdefault(round(d, 2), []).append(i)
            uniform = len(groups) == 1          # single mix across the whole hull
            for d, idxs in sorted(groups.items(), reverse=True):
                if uniform:
                    role = "all zones (uniform mix)"
                    _dcs = [v for v in (bend_dc, hoop_dc) if v is not None]
                    dc = max(_dcs) if _dcs else None
                else:
                    is_flange = any(i in (0, n_zones - 1) for i in idxs)
                    role = ("flanges (bottom+top)" if is_flange and len(idxs) > 1
                            else "flange" if is_flange else "web (core)")
                    dc = (bend_dc if is_flange else hoop_dc)
                fc_raw, ft_raw = _raw(d)
                fr_raw = _fr_raw(d)
                # The gate compares σ=My/I to the governing flexural allowable.
                gov_raw = fr_raw if (governing == "fr" and fr_raw is not None) else ft_raw
                out["targets"].append({
                    "density": d, "role": role,
                    "ft_achieve_psi": ft_raw, "fr_achieve_psi": fr_raw,
                    "fc_achieve_psi": fc_raw,
                    "governing_strength": governing,
                    # the design still passes as long as the real (governing)
                    # strength is at least (its D/C) × the assumed strength.
                    "min_flexural_to_pass_psi": (gov_raw * float(dc) if dc else gov_raw),
                    "min_ft_to_pass_psi": (ft_raw * float(dc) if dc else ft_raw),
                    "utilization": (float(dc) if dc is not None else None),
                    "confidence": _confidence(d),
                })
                # A target the fit is extrapolating to is the highest-value test.
                if lab_dens and not (lo <= d <= hi):
                    out["improvements"].append({
                        "kind": "verify_target", "density": d,
                        "text": (f"Break a {d:.1f} pcf specimen: the design leans "
                                 f"on {ft_raw:.0f} psi tensile there, but that is "
                                 "extrapolated beyond the lab data.")})

            # Improvement lever: a web pinned at the viability floor is the main
            # weight lever — measurable tension below it lightens the whole hull.
            if density_at_floor and ft_floor is not None:
                wd = min(zds)
                out["improvements"].insert(0, {
                    "kind": "lighten_web", "density": wd, "floor": ft_floor,
                    "text": (f"The web is pinned at the lightest viable mix "
                             f"({wd:.1f} pcf); the current fit predicts zero "
                             f"tensile strength at ρ≈{ft_floor:.1f} pcf. "
                             "A mix that keeps measurable tensile strength below "
                             f"{wd:.1f} pcf would let the whole hull get lighter "
                             "— the single biggest weight lever left.")})
        else:
            # DATA-GAP mode: suggest densities to break next.  Cover the viable
            # range and specifically probe below the current light floor.
            out["mode"] = "data_gap"
            if not lab_dens:
                out["improvements"].append({
                    "kind": "seed", "text": (
                        "No lab results yet — break specimens across the density "
                        "range (e.g. 60, 70, 80 pcf) to seed the fit.")})
            else:
                lo_probe = (ft_floor if ft_floor is not None else lo)
                span = [round(lo_probe, 1), round((lo_probe + hi) / 2.0, 1),
                        round(hi, 1)]
                for target in sorted(set(span)):
                    gap = min(abs(target - d) for d in lab_dens)
                    if gap > 4.0:
                        out["improvements"].append({
                            "kind": "data_point", "density": target,
                            "text": (f"Break a {target:.1f} pcf specimen — the "
                                     f"nearest lab point is {gap:.1f} pcf away, so "
                                     "the fit is thin here.")})
                if ft_floor is not None:
                    out["improvements"].append({
                        "kind": "probe_floor", "density": round(ft_floor, 1),
                        "text": (f"Probe near ρ≈{ft_floor:.1f} pcf (the "
                                 "fit's zero-tension floor): a lighter mix that "
                                 "still carries tension moves the whole design "
                                 "lighter.")})

        # Flexural-data coverage: the bending gate prefers f_r, so nudge the
        # team to log 3-point-bending results until the MOR fit forms.
        if self.fr_regression is None:
            out["improvements"].append({
                "kind": "log_fr",
                "text": (f"Log 3-point-bending (f_r / MOR) results — "
                         f"{self._regression_n_fr} so far. With ≥2 the sweep's "
                         "bending gate switches from the conservative f_t to the "
                         "physically correct f_r (σ=My/I vs modulus of rupture).")})
        return out

    # ──────────────────────  CLEANUP  ────────────────────────────────
    def close(self):
        self._conn.close()

    def __del__(self):
        try:
            self._conn.close()
        except Exception as _exc:
            domain_only(_exc)   # a bug is not a skippable condition
            pass
