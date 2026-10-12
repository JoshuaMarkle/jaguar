# canoe_physics.py
"""
High-fidelity canoe dynamics model for the evolutionary race simulator.
Uses the existing Michell integral (wave_resistance.py) and hull generator
(hull_generator.py) to compute realistic performance metrics.
"""
import numpy as np
from hull_generator import generate_hull, compute_hydrostatics
from wave_resistance import total_resistance
from physics.constants import WATER_RHO, GRAVITY, NU, NTHETA_DEFAULT


# ═══════════════════  CONSTANTS  ═════════════════════════════════
PADDLER_POWER_W     = 150.0    # sustained watts per paddler
PADDLE_EFFICIENCY   = 0.75    # fraction of power that becomes thrust
NTHETA              = NTHETA_DEFAULT   # angular quadrature (lower for speed in sim)
FRICTION_METHOD     = 1        # ITTC-1957

# Turning physics
PADDLE_STEERING_FRAC = 0.35   # fraction of power diverted to steering
YAW_DAMPING_COEFF   = 0.08    # empirical yaw damping per m³ of Iwp


# ═══════════════════  HULL → PERFORMANCE  ════════════════════════
class CanoePerformance:
    """
    Computes all race-relevant performance metrics for a given hull.
    """

    def __init__(self, factors, B, T, L,
                 n_paddlers=4,
                 n_stations=41, n_waterlines=21):

        self.factors = np.array(factors, dtype=float)
        self.B = float(B)
        self.T = float(T)
        self.L = float(L)
        self.n_paddlers = n_paddlers

        # Generate hull and hydrostatics
        self.offsets = generate_hull(factors, L, B, T, n_stations, n_waterlines)
        self.hydro = compute_hydrostatics(self.offsets, L, T, WATER_RHO, GRAVITY)

        # Cache frequently-used values
        self.mass = self.hydro["displacement"]   # kg
        self.S = self.hydro["S"]                 # wetted surface m²
        self.Cwp = self.hydro["Cwp"]
        self.Awp = self.hydro["awp"]             # waterplane area m²
        self.GMT = self.hydro["gmt"]
        self.LB  = self.hydro["L_B"]

        # Longitudinal second moment of waterplane area (yaw resistance)
        self.Iwp_yaw = self.hydro["Iwp_yaw"]

        # Precompute drag curve: R(V) for 0.1 .. 5 m/s
        # The Michell thin-ship integral is unreliable at very low Froude
        # numbers (Fr < ~0.15).  Below that range we use friction-only drag.
        # Above Fr = 0.25 we use the full Michell result.  In between we
        # blend smoothly.
        from wave_resistance import wetted_surface, friction_resistance
        S_wet = wetted_surface(self.offsets, L, T)
        Fr_lo, Fr_hi = 0.15, 0.25

        self._v_table = np.linspace(0.1, 5.0, 80)
        raw_drag = np.zeros(80)
        for idx, v in enumerate(self._v_table):
            Fr = v / np.sqrt(GRAVITY * L)
            # Friction-only component (always valid)
            Rf = friction_resistance(v, L, S_wet, NU, WATER_RHO, FRICTION_METHOD)
            if Fr < Fr_lo:
                raw_drag[idx] = Rf
            else:
                # Full Michell result
                res = total_resistance(
                    self.offsets, L, B, T, V=v,
                    disp=self.hydro["volume"],
                    rho=WATER_RHO, g=GRAVITY, nu=NU,
                    ntheta=NTHETA, friction_method=FRICTION_METHOD
                )
                Rt_michell = res["Rt"]
                if Fr < Fr_hi:
                    # Smooth blend
                    alpha = (Fr - Fr_lo) / (Fr_hi - Fr_lo)
                    raw_drag[idx] = (1 - alpha) * Rf + alpha * Rt_michell
                else:
                    raw_drag[idx] = Rt_michell

        # Smooth with a 5-point moving average for residual oscillations
        kernel = 5
        pad = kernel // 2
        padded = np.pad(raw_drag, pad, mode="edge")
        self._r_table = np.convolve(padded, np.ones(kernel)/kernel, mode="valid")
        self._r_table = np.maximum(self._r_table, 0.0)

    # ── drag interpolation ───────────────────────────────────────
    def drag_at_speed(self, V):
        """Total resistance (N) at speed V (m/s) via interpolation."""
        if V <= 0:
            return 0.0
        return float(np.interp(V, self._v_table, self._r_table))

    def drag_derivative(self, V, dv=0.05):
        """dR/dV at speed V."""
        return (self.drag_at_speed(V + dv) - self.drag_at_speed(V - dv)) / (2 * dv)

    # ── top speed ────────────────────────────────────────────────
    def top_speed(self):
        """
        Steady-state speed where paddle thrust = total drag.
        Thrust = n_paddlers × power × efficiency / V.
        Solve: P_eff / V = R(V).
        """
        P_eff = self.n_paddlers * PADDLER_POWER_W * PADDLE_EFFICIENCY
        # Binary search for equilibrium speed
        lo, hi = 0.5, 5.0
        for _ in range(50):
            mid = (lo + hi) / 2.0
            thrust = P_eff / mid
            drag = self.drag_at_speed(mid)
            if thrust > drag:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    # ── acceleration from rest ───────────────────────────────────
    def time_to_speed(self, V_target, dt=0.05):
        """
        Integrate F_net = F_paddle - R(V) from rest to V_target.
        Returns time in seconds.
        """
        P_eff = self.n_paddlers * PADDLER_POWER_W * PADDLE_EFFICIENCY
        V = 0.3    # initial creep speed
        t = 0.0
        while V < V_target and t < 120.0:
            thrust = P_eff / max(V, 0.3)
            drag = self.drag_at_speed(V)
            F_net = thrust - drag
            if F_net <= 0:
                break
            accel = F_net / self.mass
            V += accel * dt
            t += dt
        return t

    # ── deceleration / coasting ──────────────────────────────────
    def coast_time(self, V_start, V_end, dt=0.05):
        """
        Time to decelerate from V_start to V_end with no paddling.
        """
        V = V_start
        t = 0.0
        while V > V_end and t < 60.0:
            drag = self.drag_at_speed(V)
            decel = drag / self.mass
            V -= decel * dt
            t += dt
        return t

    # ── time constant (describes how quickly speed decays) ───────
    def speed_time_constant(self, V):
        """τ = mass / (dR/dV) — how sluggish the hull is."""
        dRdV = self.drag_derivative(V)
        if dRdV <= 0:
            return 10.0
        return self.mass / dRdV

    # ── turning ──────────────────────────────────────────────────
    def turn_time(self, turn_angle_deg, turn_radius_m, V_entry):
        """
        Time to execute a turn of given angle around a buoy.

        Physics:
        - Steering requires diverting power from propulsion
        - Yaw damping resists turning (proportional to Iwp_yaw / L³)
        - The canoe decelerates during the turn due to increased drag
          from the effective angle of attack
        - Very tight turns (radius < 2×beam) get a penalty
        """
        if turn_angle_deg <= 0 or turn_radius_m <= 0:
            return 0.0

        arc_length = turn_radius_m * np.radians(turn_angle_deg)

        # Turn speed is reduced from entry speed
        # Yaw resistance factor: long hulls with large Iwp_yaw turn slowly
        yaw_resistance = 1.0 + YAW_DAMPING_COEFF * self.Iwp_yaw / max(self.L**3, 1.0)

        # Tight-turn penalty: if radius < 2 × beam, the canoe struggles
        beam = self.hydro["beam"]
        tightness = max(1.0, (2.0 * beam) / max(turn_radius_m, 0.5))

        # Drag increase during turn — hull at angle creates more resistance
        # Empirical: drag multiplier = 1 + k × (angle/180)² × (L/B)^0.5
        angle_frac = min(turn_angle_deg / 180.0, 1.0)
        drag_mult = 1.0 + 0.5 * angle_frac**2 * np.sqrt(max(self.LB, 1.0))

        # Average speed through the turn
        P_steer = self.n_paddlers * PADDLER_POWER_W * PADDLE_EFFICIENCY * PADDLE_STEERING_FRAC
        V_turn = V_entry * 0.6  # initial estimate
        for _ in range(20):
            thrust = P_steer / max(V_turn, 0.3)
            drag = self.drag_at_speed(V_turn) * drag_mult * tightness
            F_net = thrust - drag
            accel = F_net / self.mass
            V_turn = max(V_turn + accel * 0.1, 0.3)

        V_avg = V_turn / yaw_resistance
        V_avg = max(V_avg, 0.3)

        turn_time = arc_length / V_avg

        # Stability check: if GMT is marginal, add capsize-risk penalty
        if self.GMT < 0.02:
            turn_time *= 2.0   # massive penalty — near capsize
        elif self.GMT < 0.05:
            turn_time *= 1.3   # cautious turning

        return turn_time, V_avg   # return exit speed too

    # ── straight segment time ────────────────────────────────────
    def straight_time(self, distance, V_entry, dt=0.05):
        """
        Time to traverse a straight segment starting at V_entry.
        Integrates acceleration/drag to compute distance over time.
        Returns (time, V_exit).
        """
        P_eff = self.n_paddlers * PADDLER_POWER_W * PADDLE_EFFICIENCY
        V = max(V_entry, 0.3)
        t = 0.0
        d = 0.0
        while d < distance and t < 300.0:
            thrust = P_eff / max(V, 0.3)
            drag = self.drag_at_speed(V)
            F_net = thrust - drag
            accel = F_net / self.mass
            V += accel * dt
            V = max(V, 0.3)
            d += V * dt
            t += dt
        return t, V


# ═══════════════════  RACE SIMULATION  ═══════════════════════════

class AbstractCanoePerformance:
    """
    Abstract canoe performance model based purely on Straight-Line Capability (Cs)
    and Turning Capability (Ct).
    Uses anchored linear penalties to guarantee a stable Pareto tradeoff gradient.
    """
    def __init__(self, Cs, n_paddlers=4):
        self.Cs = np.clip(Cs, 0.0, 1.0)
        self.Ct = 1.0 - self.Cs
        self.n_paddlers = n_paddlers

    def straight_time(self, distance, V_entry, dt=0.05):
        # Anchor: Perfect sprint boat (Cs=1) takes 60s per 200m. Terrible (Cs=0) takes 80s.
        # We use a quadratic penalty (1 - Cs)^2 to create a convex Pareto frontier.
        # If it were strictly linear, the GA would always pick a 1.0/0.0 corner solution.
        time = (distance / 200.0) * (60.0 + (1.0 - self.Cs)**2 * 20.0)
        V_exit = distance / time if time > 0 else 0.3
        return time, V_exit

    def turn_time(self, turn_angle_deg, turn_radius_m, V_entry):
        if turn_angle_deg <= 0 or turn_radius_m <= 0:
            return 0.0, V_entry
            
        # Anchor: Slalom has ~200m straight and ~900 degrees of turning.
        # Perfect slalom boat takes 130s. Terrible takes 165s.
        # 130s - 60s = 70s (for perfect Ct=1)
        # Using a 45s max penalty with a quadratic curve (1 - Ct)^2.
        # (45s balances against the combined 40s sprint penalty from both courses).
        time = (turn_angle_deg / 900.0) * (70.0 + 45.0 * (1.0 - self.Ct)**2)
        return time, V_entry


def simulate_race(canoe_perf, course):
    """
    Simulate a canoe racing through a course.
    Returns total race time in seconds.
    """
    total_time = 0.0
    V = 0.3  # standing start

    for a, b, seg_dist, seg_type in course.segments():
        if seg_dist < 0.1 and b.turn_radius > 0:
            # Pure turn at same location (e.g., buoy turn)
            result = canoe_perf.turn_time(b.turn_angle, b.turn_radius, V)
            t_turn, V = result
            total_time += t_turn
        elif b.turn_radius > 0:
            # Straight segment followed by a turn
            t_straight, V = canoe_perf.straight_time(seg_dist, V)
            total_time += t_straight
            result = canoe_perf.turn_time(b.turn_angle, b.turn_radius, V)
            t_turn, V = result
            total_time += t_turn
        else:
            # Pure straight segment
            t_straight, V = canoe_perf.straight_time(seg_dist, V)
            total_time += t_straight

    return total_time


def simulate_race_with_telemetry(canoe_perf, course, dt=0.05):
    """
    Simulate race and record [t, x, y, heading, speed] telemetry.

    Spatial position is projected exactly onto the Dubins racing line
    produced by analyse_course(), so the replay canoe follows the same
    geometric path drawn in the Racing Line tab.  Speed dynamics use the
    same thrust/drag model as simulate_race(): full power on straight
    segments, PADDLE_STEERING_FRAC of power on arc sections (detected via
    local path curvature).

    Requires a CanoePerformance instance (not AbstractCanoePerformance)
    because it calls drag_at_speed() and reads the mass attribute directly.
    """
    if not isinstance(canoe_perf, CanoePerformance):
        raise TypeError(
            "simulate_race_with_telemetry requires a CanoePerformance instance, "
            "not AbstractCanoePerformance — it needs drag_at_speed() and .mass."
        )

    if len(course.waypoints) < 2:
        return 0.0, np.array([])

    # ── 1. Get the Dubins path geometry ───────────────────────────
    from physics.race_path import analyse_course
    pa = analyse_course(course)
    path_xy = pa["path_xy"]        # (N, 2) sampled at ~0.5 m intervals

    if len(path_xy) < 2:
        return 0.0, np.array([])

    # ── 2. Arc-length parameterisation of path_xy ─────────────────
    diffs = np.diff(path_xy, axis=0)                      # (N-1, 2)
    seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])         # (N-1,)
    cum_dist = np.concatenate([[0.0], np.cumsum(seg_lens)])  # (N,)
    total_path_len = float(cum_dist[-1])

    # Forward tangent heading at each sample point.
    # Point i takes the heading of the outgoing segment; the last point
    # reuses the last arriving segment heading.
    seg_headings = np.arctan2(diffs[:, 1], diffs[:, 0])   # (N-1,)
    headings_at_pts = np.concatenate([seg_headings, [seg_headings[-1]]])  # (N,)

    # ── 3. Curvature array — identifies arc sections ───────────────
    # κ[i] = |Δθ| / Δs at each interior point; 0 at endpoints.
    # On Dubins arcs (buoy turns with r ≈ 5–8 m): κ ≈ 0.12–0.20 rad/m.
    # On tangent straights: κ ≈ 0 by construction (G1 continuity).
    kappa = np.zeros(len(path_xy))
    if len(seg_headings) > 1:
        dtheta = np.diff(seg_headings)
        dtheta = (dtheta + np.pi) % (2.0 * np.pi) - np.pi   # wrap to [-π, π]
        ds_mid = 0.5 * (seg_lens[:-1] + seg_lens[1:])
        kappa[1:-1] = np.abs(dtheta) / np.maximum(ds_mid, 1e-9)

    # Curvature threshold: r < 20 m → turning section.
    ARC_KAPPA_THRESH = 0.05   # rad/m

    # ── 4. Position lookup: arc-length d → (x, y, heading, κ) ────
    def _position_at(d: float):
        d = float(np.clip(d, 0.0, total_path_len))
        idx = int(np.searchsorted(cum_dist, d, side="right")) - 1
        idx = int(np.clip(idx, 0, len(cum_dist) - 2))
        seg_len = cum_dist[idx + 1] - cum_dist[idx]
        frac = (d - cum_dist[idx]) / seg_len if seg_len > 1e-9 else 0.0
        x_p = float(path_xy[idx, 0] + frac * (path_xy[idx + 1, 0] - path_xy[idx, 0]))
        y_p = float(path_xy[idx, 1] + frac * (path_xy[idx + 1, 1] - path_xy[idx, 1]))
        h_p = float(headings_at_pts[idx + 1])   # forward tangent
        k_p = float(kappa[idx] + frac * (kappa[idx + 1] - kappa[idx]))
        return x_p, y_p, h_p, k_p

    # ── 5. 1-D physics integration projected onto the Dubins path ─
    V = 0.3             # standing start [m/s]
    d_path = 0.0        # arc-length position along path_xy [m]
    total_time = 0.0

    P_full = canoe_perf.n_paddlers * PADDLER_POWER_W * PADDLE_EFFICIENCY
    P_turn = P_full * PADDLE_STEERING_FRAC  # power diverted to steering on arcs

    telemetry = []
    x0, y0, h0, _ = _position_at(0.0)
    telemetry.append([0.0, x0, y0, h0, V])

    while d_path < total_path_len and total_time < 1000.0:
        _, _, _, kappa_here = _position_at(d_path)
        P_eff = P_turn if kappa_here > ARC_KAPPA_THRESH else P_full

        thrust = P_eff / max(V, 0.3)
        drag   = canoe_perf.drag_at_speed(V)
        accel  = (thrust - drag) / canoe_perf.mass

        V = max(V + accel * dt, 0.3)
        d_path = min(d_path + V * dt, total_path_len)
        total_time += dt

        x_n, y_n, h_n, _ = _position_at(d_path)
        telemetry.append([total_time, x_n, y_n, h_n, V])

    return total_time, np.array(telemetry)
