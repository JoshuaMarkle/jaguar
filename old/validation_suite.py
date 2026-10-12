# validation_suite.py
"""
Side-by-side validation of the Python wave resistance engine
(wave_resistance.py — Michell thin-ship integral) against the legacy
Godzilla/Michlet Fortran engine (mlt933w.exe).

Workflow
--------
1. Generate a test hull from parametric factors.
2. Run the Python engine at each speed in [1.0, 4.5] m/s  (Δ = 0.1 m/s).
3. Write the hull into a Fortran-compatible in.mlt, launch Godzilla,
   and parse its drag output.
4. Compute point-by-point and aggregate error deltas.
5. Log timestamped results to validation_results.json.
6. Produce a matplotlib comparison chart (dark theme).

Guardrails
----------
- NO ProcessPoolExecutor — subprocess.run() only for Godzilla.
- .mlt offset formatting: strict 10-char width, right-aligned, space-delimited.
- All physics equations documented inline.

Speed range
-----------
1.0 – 4.5 m/s at 0.1 m/s increments (race-relevant regime).
"""
import os
import json
import subprocess
import time
import numpy as np
from datetime import datetime
from typing import Optional, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from hull_generator import generate_hull, compute_hydrostatics
from wave_resistance import total_resistance
from errors import domain_only


# ═══════════════════════════════════════════════════════════════════
#  DEFAULT VALIDATION PARAMETERS
# ═══════════════════════════════════════════════════════════════════
DEFAULT_SPEEDS = np.arange(1.0, 4.51, 0.1)   # 1.0, 1.1, ... 4.5 m/s
DEFAULT_FACTORS = [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05]
DEFAULT_RHO = 998.0     # freshwater  [kg/m³]
DEFAULT_G   = 9.807     # gravity     [m/s²]
DEFAULT_NU  = 1.139e-6  # kinematic viscosity [m²/s]


class ValidationSuite:
    """
    Side-by-side comparison of the Python wave-resistance engine
    vs. the legacy Godzilla/Michlet Fortran engine.
    """

    def __init__(self, project_dir: Optional[str] = None):
        self.project_dir = project_dir or os.path.dirname(
            os.path.abspath(__file__)
        )

    # ──────────────────────  HULL GENERATION  ────────────────────
    def generate_test_hull(self, factors, L, B, T,
                           n_st=81, n_wl=51) -> np.ndarray:
        """
        Generate a half-breadth offset matrix for validation.
        Higher resolution than the race simulator (81×51 vs 31×15)
        to better match Michlet's internal grid.

        Parameters
        ----------
        factors : array-like, len 7 — shape factors
        L, B, T : float — length, beam, draft [m]
        n_st, n_wl : int — station and waterline counts
        """
        return generate_hull(factors, L, B, T, n_st, n_wl)

    # ──────────────────────  PYTHON ENGINE  ──────────────────────
    def run_python_engine(self, offsets, L, B, T, speeds=None,
                          rho=DEFAULT_RHO, g=DEFAULT_G,
                          nu=DEFAULT_NU) -> dict:
        """
        Run wave_resistance.total_resistance() at each speed.

        Governing equation — Michell's thin-ship integral:
            R_w = (4ρg² / πV²) ∫₀^{π/2} |A(θ)|² sec³θ dθ

            where the amplitude function is:
            A(θ) = ∫∫ (∂y/∂x) · exp(κ₀ sec²θ · z) ·
                     exp(i κ₀ secθ · x) dz dx

            κ₀ = g / V²   (fundamental wave number)

        Friction via ITTC-1957:
            C_f = 0.075 / (log₁₀ Re − 2)²
            R_f = ½ ρ V² S C_f

        Total: R_t = R_w + (1+k) R_f
        where k is the Holtrop–Mennen viscous form factor.

        Returns
        -------
        dict with 'speeds', 'Rw', 'Rf', 'Rt', 'Rv', 'k', 'S' arrays.
        """
        if speeds is None:
            speeds = DEFAULT_SPEEDS

        results = {
            "speeds": [], "Rw": [], "Rf": [], "Rt": [],
            "Rv": [], "k": [], "S": []
        }
        disp = compute_hydrostatics(offsets, L, T, rho, g)["volume"]

        for V in speeds:
            res = total_resistance(
                offsets, L, B, T, V=float(V), disp=disp,
                rho=rho, g=g, nu=nu,
                ntheta=256,           # finer quadrature for validation
                friction_method=1,    # ITTC-1957
            )
            results["speeds"].append(float(V))
            for key in ("Rw", "Rf", "Rt", "Rv", "k", "S"):
                results[key].append(float(res[key]))

        return results

    # ──────────────────────  GODZILLA ENGINE  ────────────────────
    def write_godzilla_input(self, offsets, L, T, volume) -> str:
        """
        Write offsets into in.mlt using strict Fortran-compatible formatting:
          - 10-character field width per value, RIGHT-ALIGNED
          - Space-delimited (NOT comma-separated)
          - Matches the legacy Michlet .mlt file spec

        This mirrors app.py L587-588 formatting to prevent Godzilla crashes.

        Returns path to the written file.
        """
        n_st, n_wl = offsets.shape
        tpl_path = os.path.join(self.project_dir, "inTemplate.mlt")
        out_path = os.path.join(self.project_dir, "in.mlt")

        if not os.path.exists(tpl_path):
            raise FileNotFoundError(
                f"inTemplate.mlt not found in {self.project_dir}. "
                "Cannot generate Godzilla input."
            )

        with open(tpl_path, "r") as f:
            lines = f.readlines()

        # Locate the offsets insertion point
        offsets_start = None
        for i, line in enumerate(lines):
            if line.strip() == "# Offsets":
                offsets_start = i + 1
                break
        if offsets_start is None:
            raise ValueError("'# Offsets' marker not found in inTemplate.mlt")

        # Build offset rows in strict Michlet 9.33 Fortran format:
        #   - Each value MUST be exactly 10 characters (right-aligned float)
        #   - Fields separated by exactly ONE space
        #   - NO trailing spaces at end of line (Fortran parser is strict)
        #   BUG 4 FIX: Previous code used double-space delimiter ("  ".join)
        #   and variable-width zero strings, causing Godzilla parse failures.
        new_rows = []
        for ri in range(n_st):
            formatted_vals = []
            for j in range(n_wl):
                val = offsets[ri, j]
                # Zero-floor for numerical stability, then format to
                # exactly 10 characters with 5 decimal places
                if abs(val) < 1e-6:
                    val = 0.0
                formatted_vals.append(f"{val:10.5f}")
            # Single space between 10-char fields, strip trailing whitespace
            new_rows.append(" ".join(formatted_vals).rstrip() + "\n")

        lines[offsets_start:offsets_start + n_st] = new_rows

        # Update header fields
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "# Displacement Volume (cubic metres)":
                lines[i + 1] = f"{volume:.5f}\n"
            elif stripped == "# Length (metres)":
                lines[i + 1] = f"{L:.4f}\n"
            elif stripped == "# Draft (metres)":
                lines[i + 1] = f"{T:.4f}\n"
            elif stripped.startswith("# Number of Offset Stations"):
                lines[i + 1] = f"{n_st}\n"
            elif stripped.startswith("# Number of Offset Waterlines"):
                lines[i + 1] = f"{n_wl}\n"

        with open(out_path, "w") as f:
            f.writelines(lines)

        return out_path

    def run_godzilla_engine(self, timeout_s: int = 120) -> Optional[dict]:
        """
        Execute mlt933w.exe and parse its drag output.

        NOTE: Uses subprocess.run() — NOT ProcessPoolExecutor.
        Safe for both main-thread and daemon-thread invocation on Windows.

        Returns
        -------
        dict with 'speeds', 'Rw', 'Rt' arrays, or None if Godzilla
        is not available or fails.
        """
        exe_path = os.path.join(self.project_dir, "mlt933w.exe")
        if not os.path.exists(exe_path):
            print("  ⚠  mlt933w.exe not found — skipping Godzilla validation.")
            return None

        try:
            result = subprocess.run(
                [exe_path, "-godz"],
                cwd=self.project_dir,
                timeout=timeout_s,
                capture_output=True,
            )
            if result.returncode != 0:
                print(f"  ⚠  Godzilla exited with code {result.returncode}")
                # Check for Fortran error logs
                for log_file in ["firm.log", "michlet.err", "error.log"]:
                    log_path = os.path.join(self.project_dir, log_file)
                    if os.path.exists(log_path):
                        with open(log_path, "r") as lf:
                            content = lf.read().strip()
                        if content:
                            print(f"      {log_file}: {content}")
                return None
        except subprocess.TimeoutExpired:
            print(f"  ⚠  Godzilla timed out after {timeout_s}s.")
            return None
        except Exception as e:
            domain_only(e)   # a bug is not a skippable condition
            print(f"  ⚠  Godzilla launch failed: {e}")
            return None

        # Parse output
        drag_file = os.path.join(self.project_dir, "ship_output_by_speed.mlt")
        if not os.path.exists(drag_file):
            print("  ⚠  ship_output_by_speed.mlt not generated.")
            return None

        return self.parse_godzilla_drag_output(drag_file)

    def parse_godzilla_drag_output(self, filepath: str) -> Optional[dict]:
        """
        Parse the Michlet drag output file (ship_output_by_speed.mlt).

        Expected format (space/comma-delimited, comment lines start with #):
            Speed(m/s)  Fn   Rw(N)   Rf(N)   Rt(N)  ...

        We parse: speed, Rw, Rt per row.  If the column layout is different,
        this parser falls back to positional heuristics.
        """
        try:
            with open(filepath, "r") as f:
                lines = f.readlines()
        except Exception as e:
            domain_only(e)   # a bug is not a skippable condition
            print(f"  ⚠  Cannot read {filepath}: {e}")
            return None

        speeds, Rw_vals, Rt_vals = [], [], []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            # Replace commas with spaces for uniform parsing
            parts = stripped.replace(",", " ").split()
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                continue

            if len(vals) >= 5:
                # Assume: Speed, Fn, Rw, Rf, Rt
                speeds.append(vals[0])
                Rw_vals.append(vals[2])
                Rt_vals.append(vals[4])
            elif len(vals) >= 3:
                # Minimal: Speed, Rw, Rt
                speeds.append(vals[0])
                Rw_vals.append(vals[1])
                Rt_vals.append(vals[2])

        if not speeds:
            print("  ⚠  No valid data rows in Godzilla drag output.")
            return None

        return {
            "speeds": speeds,
            "Rw": Rw_vals,
            "Rt": Rt_vals,
        }

    # ──────────────────────  DELTA COMPUTATION  ──────────────────
    def compute_deltas(self, python_results: dict,
                       godzilla_results: dict) -> dict:
        """
        Compute point-by-point and aggregate error metrics between
        the Python Michell integral and the legacy Godzilla engine.

        Metrics
        -------
        - Absolute delta: |Rw_python − Rw_godzilla| at each common speed
        - Relative delta: 100 × |ΔRw| / max(Rw_godzilla, 1e-6) per point
        - RMSE: √(mean(ΔRw²))  across all common speeds
        - Max absolute error and the speed at which it occurs
        - Mean percentage error

        Returns
        -------
        dict with 'per_speed' list and aggregate scalars.
        """
        py_speeds = np.array(python_results["speeds"])
        py_Rw = np.array(python_results["Rw"])
        py_Rt = np.array(python_results["Rt"])

        gz_speeds = np.array(godzilla_results["speeds"])
        gz_Rw = np.array(godzilla_results["Rw"])
        gz_Rt = np.array(godzilla_results["Rt"])

        # Interpolate Godzilla results onto the Python speed grid
        gz_Rw_interp = np.interp(py_speeds, gz_speeds, gz_Rw)
        gz_Rt_interp = np.interp(py_speeds, gz_speeds, gz_Rt)

        # Point-by-point deltas (wave resistance only — most diagnostic)
        abs_delta_Rw = np.abs(py_Rw - gz_Rw_interp)
        rel_delta_Rw = 100.0 * abs_delta_Rw / np.maximum(gz_Rw_interp, 1e-6)

        abs_delta_Rt = np.abs(py_Rt - gz_Rt_interp)
        rel_delta_Rt = 100.0 * abs_delta_Rt / np.maximum(gz_Rt_interp, 1e-6)

        # Aggregate metrics
        rmse_Rw = float(np.sqrt(np.mean(abs_delta_Rw ** 2)))
        max_abs_err_Rw = float(np.max(abs_delta_Rw))
        max_err_idx = int(np.argmax(abs_delta_Rw))
        max_err_speed = float(py_speeds[max_err_idx])
        mean_pct_err_Rw = float(np.mean(rel_delta_Rw))

        rmse_Rt = float(np.sqrt(np.mean(abs_delta_Rt ** 2)))

        per_speed = []
        for i in range(len(py_speeds)):
            per_speed.append({
                "speed": float(py_speeds[i]),
                "py_Rw": float(py_Rw[i]),
                "gz_Rw": float(gz_Rw_interp[i]),
                "abs_delta_Rw": float(abs_delta_Rw[i]),
                "pct_delta_Rw": float(rel_delta_Rw[i]),
                "py_Rt": float(py_Rt[i]),
                "gz_Rt": float(gz_Rt_interp[i]),
                "abs_delta_Rt": float(abs_delta_Rt[i]),
            })

        return {
            "per_speed": per_speed,
            "rmse_Rw": rmse_Rw,
            "rmse_Rt": rmse_Rt,
            "max_abs_err_Rw": max_abs_err_Rw,
            "max_err_speed": max_err_speed,
            "mean_pct_err_Rw": mean_pct_err_Rw,
        }

    # ──────────────────────  FULL VALIDATION  ────────────────────
    def run_full_validation(self, factors=None, L=6.1, B=0.61, T=0.20,
                            speeds=None, rho=DEFAULT_RHO, g=DEFAULT_G,
                            nu=DEFAULT_NU) -> dict:
        """
        End-to-end validation run:
          1. Generate hull from parametric factors.
          2. Run Python Michell engine at each speed.
          3. Run Godzilla engine (if available).
          4. Compute error deltas (if both engines produced results).
          5. Log results to validation_results.json.

        Returns full results dict.
        """
        if factors is None:
            factors = DEFAULT_FACTORS
        if speeds is None:
            speeds = DEFAULT_SPEEDS

        print("=" * 60)
        print("🔬 WAVE RESISTANCE VALIDATION PIPELINE")
        print("=" * 60)
        print(f"   Hull: L={L:.2f}m  B={B:.3f}m  T={T:.3f}m")
        print(f"   Factors: {factors}")
        print(f"   Speed range: {speeds[0]:.1f} – {speeds[-1]:.1f} m/s "
              f"({len(speeds)} points)")
        print()

        # 1. Generate hull
        print("[1/5] Generating test hull...")
        offsets = self.generate_test_hull(factors, L, B, T)
        hydro = compute_hydrostatics(offsets, L, T, rho, g)
        n_st, n_wl = offsets.shape
        print(f"      Grid: {n_st}×{n_wl}  |  Vol={hydro['volume']:.4f} m³  "
              f"|  Beam={hydro['beam']:.3f} m")

        # 2. Python engine
        print("[2/5] Running Python Michell engine...")
        t0 = time.time()
        py_results = self.run_python_engine(
            offsets, L, B, T, speeds, rho, g, nu
        )
        py_time = time.time() - t0
        print(f"      Done in {py_time:.1f}s  |  "
              f"Rt range: {min(py_results['Rt']):.2f} – "
              f"{max(py_results['Rt']):.2f} N")

        # 3. Godzilla engine
        print("[3/5] Attempting Godzilla engine...")
        gz_results = None
        try:
            self.write_godzilla_input(offsets, L, T, hydro["volume"])
            gz_results = self.run_godzilla_engine()
        except Exception as e:
            domain_only(e)   # a bug is not a skippable condition
            print(f"      Godzilla skipped: {e}")

        # 4. Compute deltas
        deltas = None
        if gz_results is not None:
            print("[4/5] Computing error deltas...")
            deltas = self.compute_deltas(py_results, gz_results)
            print(f"      RMSE (Rw): {deltas['rmse_Rw']:.3f} N")
            print(f"      RMSE (Rt): {deltas['rmse_Rt']:.3f} N")
            print(f"      Max |ΔRw|: {deltas['max_abs_err_Rw']:.3f} N "
                  f"@ {deltas['max_err_speed']:.1f} m/s")
            print(f"      Mean %err (Rw): {deltas['mean_pct_err_Rw']:.1f}%")
        else:
            print("[4/5] Deltas skipped (Godzilla not available).")

        # 5. Log
        print("[5/5] Logging results...")
        full_results = {
            "timestamp": datetime.now().isoformat(),
            "hull": {
                "factors": list(factors), "L": L, "B": B, "T": T,
                "volume": hydro["volume"],
                "n_stations": n_st, "n_waterlines": n_wl,
            },
            "python": py_results,
            "godzilla": gz_results,
            "deltas": deltas,
            "python_elapsed_s": round(py_time, 2),
        }
        self.log_results(full_results)

        print()
        if deltas:
            print("✅ Validation complete — Python vs. Godzilla comparison logged.")
        else:
            print("✅ Validation complete — Python-only results logged "
                  "(Godzilla unavailable).")

        return full_results

    # ──────────────────────  LOGGING  ────────────────────────────
    def log_results(self, results: dict,
                    filepath: str = "validation_results.json"):
        """
        Append timestamped results to a JSON log file for regression tracking.
        The file stores an array of result objects.
        """
        path = os.path.join(self.project_dir, filepath)
        existing = []
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = [existing]
            except (json.JSONDecodeError, IOError):
                existing = []

        existing.append(results)

        with open(path, "w") as f:
            json.dump(existing, f, indent=2, default=str)

    # ──────────────────────  PLOTTING  ───────────────────────────
    def plot_comparison(self, results: dict):
        """
        Generate a comparison chart with dark theme matching the
        existing visualization.py style.

        Top panel:  R_w vs speed for both engines (or Python-only)
        Bottom panel: |ΔR_w| error vs speed (if Godzilla data exists)

        Returns matplotlib Figure.
        """
        py = results["python"]
        gz = results.get("godzilla")
        deltas = results.get("deltas")

        has_godzilla = gz is not None and deltas is not None
        n_panels = 2 if has_godzilla else 1

        fig, axes = plt.subplots(n_panels, 1,
                                  figsize=(9, 4 * n_panels),
                                  facecolor="#1a1a2e")
        if n_panels == 1:
            axes = [axes]

        # ── Panel 1: Wave Resistance vs Speed ────────────────────
        ax = axes[0]
        ax.set_facecolor("#1a1a2e")

        ax.plot(py["speeds"], py["Rw"],
                color="#00d2ff", linewidth=2, label="Python (Michell)")
        ax.plot(py["speeds"], py["Rt"],
                color="#00d2ff", linewidth=1, linestyle="--",
                alpha=0.5, label="Python (Total)")

        if has_godzilla:
            ax.plot(gz["speeds"], gz["Rw"],
                    color="#e74c3c", linewidth=2, marker=".",
                    markersize=4, label="Godzilla (Michlet)")
            if "Rt" in gz:
                ax.plot(gz["speeds"], gz["Rt"],
                        color="#e74c3c", linewidth=1, linestyle="--",
                        alpha=0.5, label="Godzilla (Total)")

        ax.set_title("Wave Resistance Comparison",
                      fontsize=13, fontweight="bold", color="white")
        ax.set_xlabel("Speed (m/s)", color="white")
        ax.set_ylabel("Resistance (N)", color="white")
        ax.tick_params(colors="gray")
        ax.grid(True, linestyle="--", alpha=0.2, color="gray")
        ax.legend(fontsize=9, facecolor="#222", edgecolor="#555",
                  labelcolor="white", loc="upper left")

        # ── Panel 2: Error Delta (only if Godzilla exists) ──────
        if has_godzilla:
            ax2 = axes[1]
            ax2.set_facecolor("#1a1a2e")

            speeds = [p["speed"] for p in deltas["per_speed"]]
            abs_err = [p["abs_delta_Rw"] for p in deltas["per_speed"]]
            pct_err = [p["pct_delta_Rw"] for p in deltas["per_speed"]]

            ax2.bar(speeds, abs_err, width=0.08,
                    color="#f39c12", alpha=0.8, label="|ΔRw| (N)")

            ax2_pct = ax2.twinx()
            ax2_pct.plot(speeds, pct_err,
                         color="#2ecc71", linewidth=1.5,
                         marker="o", markersize=3,
                         label="% Error")
            ax2_pct.set_ylabel("Relative Error (%)", color="#2ecc71")
            ax2_pct.tick_params(axis="y", colors="#2ecc71")

            # RMSE annotation
            ax2.axhline(deltas["rmse_Rw"], color="#e74c3c",
                        linestyle=":", alpha=0.6)
            ax2.annotate(f'RMSE = {deltas["rmse_Rw"]:.2f} N',
                         xy=(speeds[-1], deltas["rmse_Rw"]),
                         fontsize=8, color="#e74c3c",
                         ha="right", va="bottom")

            ax2.set_title("Python vs. Godzilla — Error Delta",
                           fontsize=13, fontweight="bold", color="white")
            ax2.set_xlabel("Speed (m/s)", color="white")
            ax2.set_ylabel("|ΔRw| (N)", color="white")
            ax2.tick_params(colors="gray")
            ax2.grid(True, linestyle="--", alpha=0.15, color="gray")
            ax2.legend(fontsize=8, facecolor="#222", edgecolor="#555",
                       labelcolor="white", loc="upper left")

        fig.tight_layout()
        return fig


# ═══════════════════════════════════════════════════════════════════
#  STANDALONE ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    suite = ValidationSuite()
    results = suite.run_full_validation()
    fig = suite.plot_comparison(results)
    fig.savefig("validation_comparison.pdf", format="pdf", dpi=300)
    print("📊 Chart saved to validation_comparison.pdf")
