# race_simulator.py
"""
Tier 1: Strategy Optimizer (Outer Loop)
Evolutionary (genetic algorithm) race simulator operating on abstract capability.
Finds the ideal Straight-Line Capability (Cs) and Turning Capability (Ct).
"""
import numpy as np
import time
import concurrent.futures
import os
from typing import List, Optional, Callable

from canoe_physics import AbstractCanoePerformance, simulate_race, WATER_RHO, GRAVITY
from race_course import get_course, RaceCourse
from physics.race_path import analyse_course, DEFAULT_MIN_TURN_RADIUS_M

def _evaluate_worker(args):
    genome, courses, course_weights = args
    return evaluate_individual(genome, courses, course_weights)


def geometric_capability_targets(
    courses: List[RaceCourse],
    course_weights: Optional[List[float]] = None,
    min_turn_radius: float = DEFAULT_MIN_TURN_RADIUS_M,
) -> dict:
    """Run the Dubins racing-line solver on each course and aggregate.

    Returns a dict with the per-course Cs/Ct from ``physics.race_path`` and
    a length-weighted, course-weighted mean Cs/Ct.  Used as the geometric
    seed/target for the Tier-1 GA — the GA still optimises freely, but the
    geometric targets feed the initial population and the result summary.
    """
    if not courses:
        return {"per_course": {}, "Cs_geom": 0.5, "Ct_geom": 0.5}
    if course_weights is None:
        course_weights = [1.0] * len(courses)

    per_course: dict = {}
    weighted_tan = 0.0
    weighted_arc = 0.0
    for course, w in zip(courses, course_weights):
        if w <= 0:
            continue
        a = analyse_course(course, min_turn_radius=min_turn_radius)
        per_course[course.name] = {
            "Cs": float(a["Cs"]),
            "Ct": float(a["Ct"]),
            "total_path_length_m":    float(a["total_path_length_m"]),
            "total_arc_length_m":     float(a["total_arc_length_m"]),
            "total_tangent_length_m": float(a["total_tangent_length_m"]),
        }
        weighted_tan += w * float(a["total_tangent_length_m"])
        weighted_arc += w * float(a["total_arc_length_m"])

    total = weighted_tan + weighted_arc
    if total > 0.0:
        Cs_geom = weighted_tan / total
        Ct_geom = weighted_arc / total
    else:
        Cs_geom, Ct_geom = 0.5, 0.5

    return {
        "per_course":       per_course,
        "Cs_geom":          float(Cs_geom),
        "Ct_geom":          float(Ct_geom),
        "min_turn_radius_m": float(min_turn_radius),
    }

# ═══════════════════  GENOME BOUNDS  ═════════════════════════════
# 1 gene: Cs (Straight-Line Capability)
GENE_NAMES = ["Cs"]
GENE_LABELS = {"Cs": "Straight-Line Capability"}
GENE_LO = np.array([0.0])
GENE_HI = np.array([1.0])


# ═══════════════════  GA OPERATORS  ══════════════════════════════
def random_individual():
    return GENE_LO + np.random.rand(1) * (GENE_HI - GENE_LO)

def tournament_select(pop, fits, k=3):
    idxs = np.random.choice(len(pop), size=k, replace=False)
    best = idxs[np.argmin(fits[idxs])]
    return pop[best].copy()

def sbx_crossover(p1, p2, eta=2.0):
    child1, child2 = p1.copy(), p2.copy()
    for i in range(len(p1)):
        if np.random.rand() < 0.5:
            continue
        u = np.random.rand()
        if u < 0.5:
            beta = (2.0 * u) ** (1.0 / (eta + 1.0))
        else:
            beta = (1.0 / (2.0 * (1.0 - u))) ** (1.0 / (eta + 1.0))
        child1[i] = 0.5 * ((1 + beta) * p1[i] + (1 - beta) * p2[i])
        child2[i] = 0.5 * ((1 - beta) * p1[i] + (1 + beta) * p2[i])
    child1 = np.clip(child1, GENE_LO, GENE_HI)
    child2 = np.clip(child2, GENE_LO, GENE_HI)
    return child1, child2

def polynomial_mutation(x, eta_m=20.0, prob=0.15):
    child = x.copy()
    for i in range(len(x)):
        if np.random.rand() > prob:
            continue
        u = np.random.rand()
        lo, hi = GENE_LO[i], GENE_HI[i]
        delta_max = hi - lo
        if u < 0.5:
            delta = ((2.0 * u) ** (1.0 / (eta_m + 1.0)) - 1.0) * delta_max
        else:
            delta = (1.0 - (2.0 * (1.0 - u)) ** (1.0 / (eta_m + 1.0))) * delta_max
        child[i] = np.clip(child[i] + delta, lo, hi)
    return child

# ═══════════════════  FITNESS EVALUATION  ════════════════════════
def evaluate_individual(genome, courses, course_weights):
    Cs = float(genome[0])

    total_time = 0.0
    for course, weight in zip(courses, course_weights):
        if weight <= 0:
            continue
        try:
            perf_for_race = AbstractCanoePerformance(Cs=Cs, n_paddlers=course.n_paddlers)
            race_time = simulate_race(perf_for_race, course)
            total_time += weight * race_time
        except Exception:
            total_time += weight * 9999.0

    return total_time


# ═══════════════════  MAIN GA LOOP  ══════════════════════════════
def run_strategy_evolution(
    course_names: List[str] = None,
    course_weights: List[float] = None,
    pop_size: int = 40,
    n_generations: int = 30,
    elite_frac: float = 0.10,
    ui_callback: Optional[Callable] = None,
    stop_event=None,
):
    """
    Tier 1 Optimizer: Find the ideal Cs/Ct ratio.
    """
    if course_names is None:
        course_names = ["sprint", "slalom"]
    if course_weights is None:
        course_weights = [1.0] * len(course_names)

    courses = [get_course(name) for name in course_names]
    n_elite = max(2, int(pop_size * elite_frac))

    # Geometric Cs/Ct targets from the Dubins racing-line solver.
    geom = geometric_capability_targets(courses, course_weights)
    Cs_geom = geom["Cs_geom"]

    print("=" * 60)
    print("🧬 TIER 1: STRATEGY OPTIMIZER")
    print("=" * 60)
    print(f"   Courses: {', '.join(c.name for c in courses)}")
    print(f"   Weights: {course_weights}")
    print(f"   Population: {pop_size}  |  Generations: {n_generations}")
    print(f"   Geometric target (Dubins, ρ={geom['min_turn_radius_m']:.2f} m):"
          f"  Cs={Cs_geom:.3f}  Ct={geom['Ct_geom']:.3f}")
    for cname, pc in geom["per_course"].items():
        print(f"     · {cname:18s} Cs={pc['Cs']:.3f}  Ct={pc['Ct']:.3f}  "
              f"({pc['total_path_length_m']:.1f} m driven)")
    print()

    pop = np.array([random_individual() for _ in range(pop_size)])
    # Seed two individuals at the geometric target so the GA starts with a
    # warm Pareto anchor.  The remaining pop stays uniformly random.
    if pop_size >= 2:
        pop[0] = np.array([np.clip(Cs_geom, GENE_LO[0], GENE_HI[0])])
        pop[1] = np.array([np.clip(
            Cs_geom + 0.05 * (np.random.rand() - 0.5),
            GENE_LO[0], GENE_HI[0])])
    
    # ThreadPoolExecutor avoids Windows deadlocks in GUI daemon threads
    # (ProcessPoolExecutor requires freeze_support() and a spawn context that
    # conflicts with the PyQt6 event loop on Windows).
    n_workers = os.cpu_count() or 1
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
    args_list = [(pop[i], courses, course_weights) for i in range(pop_size)]

    _t_bench = time.time()
    fits = np.array(list(executor.map(_evaluate_worker, args_list)))
    _bench_parallel = time.time() - _t_bench
    _bench_serial_est = _bench_parallel * n_workers
    print(f"   ThreadPool ({n_workers} workers): initial eval in {_bench_parallel:.2f}s "
          f"(~{_bench_serial_est:.1f}s serial est., "
          f"~{_bench_serial_est / max(_bench_parallel, 1e-6):.1f}× speedup)")

    best_ever = np.inf
    best_genome = pop[0].copy()

    t0 = time.time()

    for gen in range(1, n_generations + 1):
        if stop_event and stop_event.is_set():
            print("   ⏹ Stopped by user.")
            break

        order = np.argsort(fits)
        pop = pop[order]
        fits = fits[order]

        if fits[0] < best_ever:
            best_ever = fits[0]
            best_genome = pop[0].copy()

        elapsed = time.time() - t0
        Cs_best = best_genome[0]
        msg = (f"  Gen {gen:>3d}/{n_generations}  "
               f"BestTime={best_ever:>7.1f}s  "
               f"Ideal Cs={Cs_best:>5.3f}, Ct={1.0-Cs_best:>5.3f}  "
               f"[{elapsed:.0f}s elapsed]")
        print(msg)

        if ui_callback:
            ui_callback(gen, best_ever, best_genome)

        new_pop = [pop[i].copy() for i in range(n_elite)]

        while len(new_pop) < pop_size:
            p1 = tournament_select(pop, fits, k=3)
            p2 = tournament_select(pop, fits, k=3)
            c1, c2 = sbx_crossover(p1, p2)
            c1 = polynomial_mutation(c1)
            c2 = polynomial_mutation(c2)
            new_pop.append(c1)
            if len(new_pop) < pop_size:
                new_pop.append(c2)

        pop = np.array(new_pop[:pop_size])

        args_list = [(pop[i], courses, course_weights) for i in range(pop_size)]
        fits = np.array(list(executor.map(_evaluate_worker, args_list)))

    order = np.argsort(fits)
    pop = pop[order]
    fits = fits[order]
    if fits[0] < best_ever:
        best_ever = fits[0]
        best_genome = pop[0].copy()

    executor.shutdown(wait=True)

    final_Cs = float(best_genome[0])
    final_Ct = 1.0 - final_Cs

    total_elapsed = time.time() - t0
    evals = pop_size * n_generations
    print()
    print("=" * 60)
    print("🏆 TIER 1 EVOLUTION COMPLETE")
    print("=" * 60)
    print(f"   Best abstract race time: {best_ever:.1f} s")
    print(f"   Ideal Straight Capability (Cs): {final_Cs:.3f}")
    print(f"   Ideal Turning Capability (Ct):  {final_Ct:.3f}")
    print(f"   Benchmark: {evals} evals in {total_elapsed:.1f}s "
          f"({evals / max(total_elapsed, 1e-6):.0f} evals/s, "
          f"{n_workers} threads)")
    print()
    
    return {
        "ideal_Cs": final_Cs,
        "ideal_Ct": final_Ct,
        "baseline_time": best_ever,
        "geometric_Cs": geom["Cs_geom"],
        "geometric_Ct": geom["Ct_geom"],
        "geometric_per_course": geom["per_course"],
        "min_turn_radius_m": geom["min_turn_radius_m"],
    }
