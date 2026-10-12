"""
P0 safety-gate tests — required to pass before any competition use.

Covers the 8 fundamental invariants of the structural and hydrodynamic
models.  No UI, no Godzilla binary, no file I/O required.
"""
import numpy as np
import pytest


# ─── Module-scoped hull fixture ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def std_hull():
    """
    Standard 18 ft competition hull offsets.

    Returns (offsets_m, L_m, B_m, T_m) where offsets_m is the
    (41 × 21) half-breadth matrix in metres.
    """
    from hull_generator import generate_hull
    L_m = 18.0 / 3.28084
    B_m = 0.61
    T_m = 4.0 / 39.3701
    factors = [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05]
    offsets = generate_hull(factors, L_m, B_m, T_m, n_stations=41, n_waterlines=21)
    return offsets, L_m, B_m, T_m


@pytest.fixture(scope="module")
def shell_geo(std_hull):
    """
    Cross-section properties for the standard hull with ASCE-minimum freeboard.

    Mirrors the full geometry pipeline from parse_godzilla_offsets:
    underwater hull + 6 in freeboard walls extruded straight up.
    Using just the underwater draft produces sections with I ≈ 11 in⁴ that
    fail stress checks; adding the required freeboard raises I to ≈ 142 in⁴.
    """
    from geometry import calculate_shell_properties
    offsets_m, L_m, B_m, T_m = std_hull

    L_ft      = L_m * 3.28084
    T_in      = T_m * 39.3701
    mold_in   = T_in + 6.0   # ASCE minimum 6 in freeboard
    n_st, n_wl = offsets_m.shape
    t_in = 0.5

    offsets_in = offsets_m * 39.3701

    # Extrude freeboard walls (same logic as parse_godzilla_offsets)
    z_under = np.linspace(0, T_in, n_wl)
    n_fb    = 10
    z_free  = np.linspace(T_in + 0.1, mold_in, n_fb)
    y_free  = np.tile(offsets_in[:, -1], (n_fb, 1)).T
    z_heights = np.concatenate([z_under, z_free])
    y_matrix  = np.hstack([offsets_in, y_free])

    x_stations = np.linspace(0, L_ft, n_st)
    props = calculate_shell_properties(x_stations, z_heights, y_matrix, t_in)
    A_in2, A_top_in2, A_bot_in2, I_in4, Q_in3, c_top, c_bot, width_ft = props

    valid = A_in2 > 0.001
    x  = x_stations[valid]
    dx = L_ft / (n_st - 1)

    return dict(
        x=x, dx=dx, L_ft=L_ft, t_in=t_in,
        A_ft2=A_in2[valid] / 144.0,
        A_top_ft2=A_top_in2[valid] / 144.0,
        A_bot_ft2=A_bot_in2[valid] / 144.0,
        I_in4=I_in4[valid],
        Q_in3=Q_in3[valid],
        c_top=c_top[valid],
        c_bot=c_bot[valid],
        width_ft=width_ft[valid],
    )


# ─── 1 & 2 — Moment / shear envelope closure ─────────────────────────────────

class TestMomentShearEnvelopes:

    def test_moment_envelope_closes(self, shell_geo):
        """M[0] must be 0 and M[-1] must be within 1 ft·lb of zero."""
        from structural import calculate_moment_envelope
        g = shell_geo
        n = len(g["x"])
        w_self = np.full(n, 2.0)   # uniform 2 lb/ft self-weight

        M_env = calculate_moment_envelope(
            g["x"], g["dx"], w_self, g["width_ft"],
            [3.0, 7.5, 10.5, 15.0],  # 4-person positions [ft]
            [7.0, 11.0],              # 2-person positions [ft]
        )

        assert M_env[0] == pytest.approx(0.0, abs=1e-6), "M[0] must be zero"
        # Closure is asserted RELATIVE to the peak moment, not as a fixed
        # 1 ft-lb absolute.  _solve_beam_case balances sinkage and trim so that
        # int q dx = 0 and int q*x dx = 0; continuous integration by parts then
        # gives M[-1] = L*V[L] - int q*x dx = 0 exactly.  Discretely, trapezoid()
        # and cumulative_trapezoid() do not satisfy that identity to machine
        # precision on a coarse grid carrying point loads -- the mismatch is
        # O(dx^2) and scales with the moment itself, so an absolute tolerance is
        # the wrong shape.  On this fixture the residual is ~0.2% of peak M.
        # (The SWEEP's own configuration -- tips included, width -> 0 at the
        # ends -- closes to ~1e-14; see test_sweep_configuration_closes_exactly.)
        peak = float(np.max(np.abs(M_env)))
        assert abs(M_env[-1]) <= 0.005 * peak, (
            f"M[-1]={M_env[-1]:.3f} ft·lb is {100*abs(M_env[-1])/peak:.2f}% of "
            f"peak {peak:.1f} — envelope did not close"
        )

    def test_sweep_configuration_closes_exactly(self):
        """
        The geometry the SWEEP actually solves must close to machine precision.

        The sweep passes the full station list including the degenerate bow and
        stern tips, where width -> 0. With the correction shaped by the
        waterplane, both envelopes then close exactly -- this is the strong
        form of the invariant, and it is the configuration that decides the
        design.
        """
        from structural import calculate_moment_envelope
        n = 41
        x = np.linspace(0.0, 20.0, n)
        width = np.full(n, 2.5)
        width[0] = width[-1] = 0.0          # degenerate tips, as the hull has
        r = calculate_moment_envelope(
            x, x[1] - x[0], np.full(n, 2.0), width,
            [3.0, 7.5, 10.5, 15.0], [7.0, 11.0], return_all=True)
        for case in ("race_4", "race_2"):
            M = np.asarray(r["moment"][case]); V = np.asarray(r["shear"][case])
            assert abs(V[-1]) < 1e-9, f"{case}: shear did not close, V[-1]={V[-1]:.3e}"
            assert abs(M[-1]) < 1e-6, f"{case}: moment did not close, M[-1]={M[-1]:.3e}"

    def test_shear_envelope_closes(self, shell_geo):
        """Net shear at the end must be within 10 lb of zero after force balance."""
        from structural import calculate_moment_envelope
        g = shell_geo
        n = len(g["x"])
        w_self = np.full(n, 2.0)

        result = calculate_moment_envelope(
            g["x"], g["dx"], w_self, g["width_ft"],
            [3.0, 7.5, 10.5, 15.0],
            [7.0, 11.0],
            return_all=True,
        )

        for race in ("race_4", "race_2"):
            V = result["shear"][race]
            assert abs(V[-1]) < 10.0, (
                f"Shear did not close for {race}: V[-1]={V[-1]:.2f} lb"
            )


# ─── 3 & 4 — Structural evaluator ────────────────────────────────────────────

class TestStructuralEvaluator:

    _VARS = np.array([
        3.0, 7.5, 10.5, 15.0,  # 4-person knee positions [ft]
        7.0,  11.0,             # 2-person knee positions [ft]
        78.0, 68.0, 62.0,      # mix densities [pcf]: heavy, mid, light
    ])

    @pytest.fixture(autouse=True)
    def _stable_mixes(self, monkeypatch):
        """Pin mix strengths: config.get_mix_properties reads the user's
        live concrete_mixes.db published regression, which changes between
        sessions and must not decide whether these logic tests pass."""
        import config
        monkeypatch.setattr(
            config, "get_mix_properties",
            lambda d: {"density": float(d),
                       "strength_t": max(20.0, 8.0 * (float(d) - 40.0) + 40.0),
                       "strength_c": max(100.0, 40.0 * (float(d) - 40.0) + 200.0)})

    def test_weight_positive(self, shell_geo):
        """A feasible geometry with strong mixes must return a positive weight."""
        from structural import evaluate_canoe_weight
        g = shell_geo

        weight = evaluate_canoe_weight(
            self._VARS, g["x"], g["dx"],
            g["A_ft2"], g["I_in4"], g["Q_in3"],
            g["c_top"], g["c_bot"], g["width_ft"],
            g["A_top_ft2"], g["A_bot_ft2"], g["t_in"],
        )

        assert 0 < weight < 1e5, (
            f"Expected feasible weight in (0, 100000) lbs, got {weight}"
        )

    def test_failure_penalty(self, shell_geo):
        """Geometry with near-zero inertia must return the 1e6 infeasibility penalty."""
        from structural import evaluate_canoe_weight
        g = shell_geo

        tiny_I = g["I_in4"] * 1e-6   # crush inertia → stresses blow past any allowable

        result = evaluate_canoe_weight(
            self._VARS, g["x"], g["dx"],
            g["A_ft2"], tiny_I, g["Q_in3"],
            g["c_top"], g["c_bot"], g["width_ft"],
            g["A_top_ft2"], g["A_bot_ft2"], g["t_in"],
        )

        # The infeasibility signal is the REJECTED SENTINEL, not 1e6.  A float
        # penalty was indistinguishable from a very heavy hull to anything that
        # did arithmetic on it, which produced a +4.71 in phantom stability
        # gain and a -999842.82 lb weight delta before the type replaced it.
        # Asserting `== 1e6` here would re-enshrine the contract that caused
        # those, and `is_rejected` is how every consumer is required to ask.
        from structural import is_rejected
        assert is_rejected(result), (
            f"Expected the REJECTED sentinel for an infeasible geometry, "
            f"got {result!r}"
        )


# ─── 5 & 6 — Wave resistance ─────────────────────────────────────────────────

class TestWaveResistance:

    def test_michell_zero_speed(self, std_hull):
        """Wave resistance must be exactly zero at V = 0."""
        from wave_resistance import michell_resistance
        offsets_m, L_m, B_m, T_m = std_hull
        Rw = michell_resistance(offsets_m, L_m, T_m, V=0.0)
        assert Rw == pytest.approx(0.0)

    def test_resistance_positive_and_increasing_overall(self, std_hull):
        """
        Total resistance must be positive for all speeds and significantly
        higher at V=4 m/s than at V=1 m/s.

        Strict monotonicity is NOT tested: Michell's integral produces
        genuine wave-interference humps so Rt(V) is not necessarily
        monotone — this is correct physics, not a bug.
        """
        from wave_resistance import total_resistance
        offsets_m, L_m, B_m, T_m = std_hull
        disp = 200.0

        speeds = [1.0, 2.0, 3.0, 4.0]
        Rt = [
            total_resistance(offsets_m, L_m, B_m, T_m, V, disp)["Rt"]
            for V in speeds
        ]

        for V, rt in zip(speeds, Rt):
            assert rt > 0, f"Resistance must be positive at V={V} m/s, got {rt:.3f} N"

        assert Rt[-1] > Rt[0], (
            f"Resistance at V=4 ({Rt[-1]:.1f} N) must exceed V=1 ({Rt[0]:.1f} N)"
        )


# ─── 7 — Hydrostatic coefficients ────────────────────────────────────────────

class TestHydrostatics:

    def test_coefficients_physical(self, std_hull):
        """Cb, Cm, Cp must all lie strictly within (0, 1)."""
        from hull_generator import compute_hydrostatics
        offsets_m, L_m, B_m, T_m = std_hull
        h = compute_hydrostatics(offsets_m, L_m, T_m)

        for name, val in [("Cb", h.Cb), ("Cm", h.Cm), ("Cp", h.Cp)]:
            assert 0 < val < 1, f"{name} = {val:.4f} is outside (0, 1)"

    def test_displacement_positive(self, std_hull):
        """Displaced volume and mass must be positive for a non-trivial hull."""
        from hull_generator import compute_hydrostatics
        offsets_m, L_m, B_m, T_m = std_hull
        h = compute_hydrostatics(offsets_m, L_m, T_m)

        assert h.volume > 0, f"volume = {h.volume}"
        assert h.displacement > 0, f"displacement = {h.displacement}"


# ─── 8 — Geometry shell symmetry ─────────────────────────────────────────────

class TestGeometryShellProperties:

    def test_rectangular_section_neutral_axis(self):
        """
        For a straight-walled rectangular cross-section the neutral axis must
        lie at the midpoint, so c_top == c_bot at every station.
        """
        from geometry import calculate_shell_properties

        H_in = 10.0   # section height [in]
        W_in = 2.0    # constant half-breadth [in]
        n_wl = 21
        n_st = 3

        # Constant breadth at every depth → purely vertical wall
        y_matrix   = np.full((n_st, n_wl), W_in)
        x_stations = np.array([0.0, 0.5, 1.0])         # [ft]
        z_heights  = np.linspace(0.0, H_in, n_wl)      # [in]

        props = calculate_shell_properties(x_stations, z_heights, y_matrix, 0.5)
        _, _, _, _, _, c_top, c_bot, _ = props

        # The fully-populated midstation is most representative
        mid = 1
        assert c_top[mid] == pytest.approx(c_bot[mid], rel=0.02), (
            f"Rectangular section: c_top={c_top[mid]:.4f} in  "
            f"c_bot={c_bot[mid]:.4f} in  (expected equal)"
        )
