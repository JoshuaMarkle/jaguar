"""
Tier 2: Physical Hull Matcher (Inner Loop)
Takes the ideal abstract capabilities (Cs, Ct) from the Tier 1 Strategy Optimizer
and finds the physical hull dimensions that best match those capabilities.
"""
import numpy as np
from scipy.optimize import differential_evolution

from hull_generator import generate_hull, compute_hydrostatics, check_constraints
from wave_resistance import total_resistance, wetted_surface
from physics.constants import WATER_RHO, GRAVITY, NU, NTHETA_DEFAULT as NTHETA

# Genome Bounds for Physical Hull Matching
# 7 factors + B + T
GENE_BOUNDS = [
    (0.3, 3.0),   # factor 0: entrance
    (0.3, 3.0),   # factor 1: run
    (0.1, 3.0),   # factor 2: fullness
    (0.1, 3.0),   # factor 3: keel_rise
    (0.3, 3.0),   # factor 4: shoulder
    (0.0, 0.75),  # factor 5: flare
    (0.0, 0.50),  # factor 6: tuck
    (0.45, 0.75), # B: beam (m)
    (0.13, 0.30)  # T: draft (m)
]

def evaluate_physical_hull(genome, L, ideal_Cs, ideal_Ct, bounds_dict):
    factors = genome[:7]
    B = float(genome[7])
    T = float(genome[8])

    # Generate hull
    offsets = generate_hull(factors, L, B, T, n_stations=41, n_waterlines=21)
    
    # Compute hydrostatics
    try:
        hydro = compute_hydrostatics(offsets, L, T, WATER_RHO, GRAVITY)
    except ValueError as _e:
        # Same contract as the optimizer: non-finite geometry was already
        # rejected via NaN constraint violations; it is now rejected in the
        # open.  Returning a large penalty keeps the caller's ranking intact.
        import logging
        logging.getLogger(__name__).warning("hull rejected: %s", _e)
        return 1e6          # same shape as the penalty path: a large SCORE
    
    # Check constraints
    violations = check_constraints(hydro, bounds_dict)
    penalty = sum(max(0, v) for v in violations) * 10.0
    if penalty > 0:
        if np.random.rand() < 0.01:
            print(f"  [Tier 2 log] Invalid | Penalty: {penalty:.2f} | violations: {[round(v, 2) for v in violations if v > 0]}")
        return 2.0 + penalty
        
    # Calculate Straight-Line Proxy (Cs_phys)
    # total_resistance already includes (1+k)*Rf internally; do not add Rf again.
    res = total_resistance(offsets, L, B, T, V=3.0, disp=hydro["volume"],
                           rho=WATER_RHO, g=GRAVITY, nu=NU, ntheta=NTHETA, friction_method=1)
    Rt = res["Rt"]

    # Empirical bounds for ASCE concrete canoes at 3 m/s (single-counted friction)
    D_max, D_min = 300.0, 80.0
    S_straight = (D_max - Rt) / (D_max - D_min)
    S_straight = np.clip(S_straight, 0.01, 1.0)

    # Calculate Turning Proxy (Ct_phys)
    Iwp_yaw = hydro["Iwp_yaw"]
    S_wet = hydro["S"]

    S_baseline = 4.0
    D_turn = (Iwp_yaw / L**3) * (S_wet / S_baseline)

    # Empirical bounds: D_turn ranges ~0.01–0.10 for ASCE canoe hulls
    T_max, T_min = 0.10, 0.01
    S_turn = (T_max - D_turn) / (T_max - T_min)
    S_turn = np.clip(S_turn, 0.01, 1.0)
    
    # Normalize to Cs + Ct = 1.0
    total_S = S_straight + S_turn
    if total_S < 1e-6:
        total_S = 1e-6
    Cs_phys = S_straight / total_S
    Ct_phys = S_turn / total_S
    
    # Cost is squared error
    error = (Cs_phys - ideal_Cs)**2 + (Ct_phys - ideal_Ct)**2
    
    if np.random.rand() < 0.02:
        print(f"  [Tier 2 log] Valid | Error: {error:.5f} | Cs_phys: {Cs_phys:.3f}, Ct_phys: {Ct_phys:.3f} | Rt: {Rt:.1f}N, Iwp_yaw: {Iwp_yaw:.3f}")
        
    return error

def match_hull(ideal_Cs, ideal_Ct, L=6.1, mass_target=400.0):
    """
    Tier 2 Optimizer: Matches a physical hull to the ideal strategy.
    mass_target: Canoe mass + paddlers (kg)
    """
    print("=" * 60)
    print("🔨 TIER 2: PHYSICAL HULL MATCHER")
    print("=" * 60)
    print(f"   Target Cs: {ideal_Cs:.3f}")
    print(f"   Target Ct: {ideal_Ct:.3f}")
    print(f"   Hull Length: {L:.2f} m")
    print("   Running Differential Evolution inner loop...")
    
    bounds_dict = {
        "disp_min": mass_target - 20.0,  # kg
        "disp_max": mass_target + 80.0,
        "beam_min": 0.45,
        "beam_max": 0.75,
        "gmt_min": 0.03,  # Stability
        "lcb_min": -0.05,
        "lcb_max": 0.05
    }

    # Use Differential Evolution to find the best matching hull
    result = differential_evolution(
        func=evaluate_physical_hull,
        bounds=GENE_BOUNDS,
        args=(L, ideal_Cs, ideal_Ct, bounds_dict),
        maxiter=30,
        popsize=15,
        mutation=(0.5, 1.0),
        recombination=0.7,
        disp=True,
    )
    
    best_genome = result.x
    best_error = result.fun
    
    print()
    print("🏆 TIER 2 MATCHING COMPLETE")
    print(f"   Final Error Score: {best_error:.5f}")
    print(f"   Best Hull Factors: {np.round(best_genome[:7], 3)}")
    print(f"   Best Beam:         {best_genome[7]:.3f} m")
    print(f"   Best Draft:        {best_genome[8]:.3f} m")
    
    return {
        "factors": best_genome[:7].tolist(),
        "beam": float(best_genome[7]),
        "draft": float(best_genome[8]),
        "error": best_error
    }
