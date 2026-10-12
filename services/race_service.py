# services/race_service.py
"""
Orchestrates the Tier 1 (GA strategy) + Tier 2 (hull matching) race pipeline.

No Qt imports — callable from tests or a background thread.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional


class RaceService:
    """
    Runs the full race optimization pipeline:
      1. Tier 1 — evolutionary GA to find abstract speed/turning capability targets
      2. Tier 2 — differential evolution to match those targets to a physical hull
      3. Telemetry generation using the matched hull on every requested course
    """

    def run(
        self,
        course_names: List[str],
        course_weights: List[float],
        pop_size: int = 30,
        n_generations: int = 50,
        hull_length: float = 6.1,        # metres
        paddler_power_w: float = 75.0,
        ui_callback: Optional[Callable] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[dict]:
        """
        Run the complete race optimization pipeline.

        Parameters
        ----------
        course_names    : list of course identifiers, e.g. ["sprint", "slalom"]
        course_weights  : parallel weights for the weighted objective
        pop_size        : GA population size
        n_generations   : GA generation count
        hull_length     : design waterline length (m)
        paddler_power_w : sustained watts per paddler
        ui_callback     : called as ``ui_callback(gen, best_time, best_genome)``
        stop_event      : set to request a clean abort

        Returns
        -------
        dict with keys ``ideal_Cs``, ``ideal_Ct``, ``best_hull``, ``telemetry``;
        or None if no courses were provided or a stop was requested early.
        """
        if not course_names:
            return None

        import canoe_physics
        canoe_physics.PADDLER_POWER_W = paddler_power_w

        from race_simulator import run_strategy_evolution
        results = run_strategy_evolution(
            course_names=course_names,
            course_weights=course_weights,
            pop_size=pop_size,
            n_generations=n_generations,
            ui_callback=ui_callback,
            stop_event=stop_event,
        )

        if stop_event and stop_event.is_set():
            return results

        from hull_matcher import match_hull
        ideal_Cs = results["ideal_Cs"]
        ideal_Ct = results["ideal_Ct"]
        match_results = match_hull(
            ideal_Cs, ideal_Ct, L=hull_length, mass_target=420.0
        )
        results["best_hull"] = match_results

        # Generate telemetry on the matched physical hull
        from canoe_physics import CanoePerformance, simulate_race_with_telemetry
        from race_course import get_course

        telemetry = {}
        perf = CanoePerformance(
            factors=match_results["factors"],
            B=match_results["beam"],
            T=match_results["draft"],
            L=hull_length,
            n_paddlers=4,
        )
        for cname in course_names:
            course = get_course(cname)
            perf.n_paddlers = course.n_paddlers
            _, telem = simulate_race_with_telemetry(perf, course)
            telemetry[course.name] = telem

        results["telemetry"] = telemetry
        return results
