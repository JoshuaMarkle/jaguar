# optimizer.py
"""
Hull optimisation via pymoo.
  • CMA-ES  → single-objective  (minimise total drag)
  • NSGA-II → multi-objective   (drag vs. stability)
Writes off1.mlt and out.mlt for the downstream 5-D structural sweep.
"""
import os, gc, time, threading
import numpy as np

from pymoo.core.problem import ElementwiseProblem
from pymoo.algorithms.soo.nonconvex.cmaes import CMAES
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.optimize import minimize as pymoo_minimize
from pymoo.core.callback import Callback
from pymoo.termination import get_termination

from hull_generator import generate_hull, compute_hydrostatics, check_constraints
from wave_resistance import total_resistance


# ═════════════════  PYMOO PROBLEM  ══════════════════════════════
class HullProblem(ElementwiseProblem):
    """
    Decision variables (9):
      x[0..6] = shape factors,  x[7] = beam B,  x[8] = draft T
    """

    def __init__(self, cfg, multi_objective=False):
        self.cfg = cfg
        self.multi = multi_objective
        n_obj = 2 if multi_objective else 1
        n_constr = 30

        xl = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.0, 0.0,
                       cfg["constraints"].get("beam_min", 0.05),
                       cfg["constraints"].get("draft_min", 0.01)])
        xu = np.array([3.0, 3.0, 3.0, 3.0, 3.0, 0.75, 0.75,
                       cfg["constraints"].get("beam_max", 0.85),
                       cfg["constraints"].get("draft_max", 1.0)])

        super().__init__(n_var=9, n_obj=n_obj, n_ieq_constr=n_constr,
                         xl=xl, xu=xu)

    def _evaluate(self, x, out, *args, **kwargs):
        factors = x[:7]
        B, T = float(x[7]), float(x[8])
        c = self.cfg
        L = c["length_m"]

        offsets = generate_hull(factors, L, B, T,
                                c.get("n_stations", 41),
                                c.get("n_waterlines", 21))
        try:
            hydro = compute_hydrostatics(offsets, L, T, c["rho"], c["g"])
        except ValueError as _e:
            # compute_hydrostatics now RAISES on non-finite geometry where it
            # used to return NaN-bearing fields.  That NaN was already acting
            # as a reject here -- pymoo treats `NaN <= 0` as violated -- so the
            # OUTCOME is unchanged; what changes is that the rejection is now
            # deliberate and visible instead of an accident of IEEE
            # comparison.  A search generating non-finite geometry is worth
            # seeing in the log.
            import logging
            logging.getLogger(__name__).warning(
                "infeasible candidate: %s", _e)
            out["F"] = np.full(self.n_obj, 1e9)
            out["G"] = np.full(self.n_ieq_constr, 1.0)
            return

        bounds = dict(c["constraints"])
        bounds["disp_min"] = c["disp_lo"]
        bounds["disp_max"] = c["disp_hi"]

        G = check_constraints(hydro, bounds)
        while len(G) < self.n_ieq_constr:
            G.append(-1.0)
        out["G"] = np.array(G[:self.n_ieq_constr])

        res = total_resistance(
            offsets, L, B, T, V=c["design_speed"],
            disp=hydro["volume"],
            rho=c["rho"], g=c["g"], nu=c["nu"],
            ntheta=c.get("ntheta", 128),
            friction_method=c.get("friction_method", 1),
        )

        if self.multi:
            out["F"] = [res["Rt"], -hydro.get("gmt", 0)]
        else:
            out["F"] = [res["Rt"]]


# ═════════════════  PROGRESS CALLBACK  ══════════════════════════
class OptCallback(Callback):

    def __init__(self, cfg, ui_callback=None, stop_event=None,
                 pause_event=None, checkpoint_dir=".", checkpoint_every=5):
        super().__init__()
        self.cfg = cfg
        self.ui_cb = ui_callback
        self.stop_event = stop_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self.ckpt_dir = checkpoint_dir
        self.ckpt_every = checkpoint_every
        self.best_Rt = float("inf")
        self.best_offsets = None
        self.best_hydro = None

    def notify(self, algorithm):
        gen = algorithm.n_gen
        pop = algorithm.pop
        F = pop.get("F")

        best_idx = np.argmin(F[:, 0])
        Rt = float(F[best_idx, 0])
        x = pop[best_idx].X

        if Rt < self.best_Rt:
            self.best_Rt = Rt
            cfg = self.cfg
            factors, B, T = x[:7], float(x[7]), float(x[8])
            L = cfg["length_m"]
            self.best_offsets = generate_hull(factors, L, B, T,
                                              cfg.get("n_stations", 41),
                                              cfg.get("n_waterlines", 21))
            self.best_hydro = compute_hydrostatics(
                self.best_offsets, L, T, cfg["rho"], cfg["g"])

        if self.ui_cb:
            self.ui_cb(gen, self.best_Rt, self.best_hydro, self.best_offsets)

        if gen % self.ckpt_every == 0 and self.best_offsets is not None:
            _write_off1(self.best_offsets, self.ckpt_dir)

        while self.pause_event.is_set():
            time.sleep(0.25)
            if self.stop_event.is_set():
                break
        if self.stop_event.is_set():
            algorithm.termination.force_termination = True

        if gen % 10 == 0:
            gc.collect()


# ═════════════════  SMART INITIAL GUESS  ════════════════════════
def _estimate_beam(L, T, disp_target):
    """
    Estimate an initial beam that roughly hits the target displacement
    assuming a block coefficient ~0.40 for a canoe hull.
    """
    Cb_guess = 0.40
    if L * T * Cb_guess > 0:
        B = disp_target / (L * T * Cb_guess)
    else:
        B = 0.6
    return np.clip(B, 0.3, 1.2)


# ═════════════════  PUBLIC API  ═════════════════════════════════
def run_optimization(cfg, mode="CMA-ES", max_gen=100,
                     ui_callback=None, stop_event=None, pause_event=None):
    multi = (mode == "NSGA-II")
    problem = HullProblem(cfg, multi_objective=multi)
    cwd = cfg.get("cwd", ".")

    cb = OptCallback(cfg, ui_callback, stop_event, pause_event,
                     checkpoint_dir=cwd, checkpoint_every=5)

    # Smart initial guess: beam sized to hit target displacement
    B0 = _estimate_beam(cfg["length_m"], 0.2, cfg["disp_target"])
    x0 = np.array([1.5, 1.5, 1.5, 1.5, 1.5, 0.2, 0.1, B0, 0.2])

    if mode == "NSGA-II":
        algo = NSGA2(pop_size=max(cfg.get("pop_size", 40), 20))
    else:
        algo = CMAES(x0=x0, sigma=0.3,
                     restarts=2)

    termination = get_termination("n_gen", max_gen)

    result = pymoo_minimize(
        problem, algo,
        termination=termination,
        callback=cb,
        seed=cfg.get("random_seed", 1),
        verbose=False,
    )

    offsets = cb.best_offsets
    hydro = cb.best_hydro

    if offsets is None:
        best_x = result.X if result.X is not None else x0
        if best_x.ndim > 1:
            best_x = best_x[0]
        factors, B, T = best_x[:7], float(best_x[7]), float(best_x[8])
        L = cfg["length_m"]
        offsets = generate_hull(factors, L, B, T,
                                cfg.get("n_stations", 41),
                                cfg.get("n_waterlines", 21))
        hydro = compute_hydrostatics(offsets, L, T, cfg["rho"], cfg["g"])

    _write_off1(offsets, cwd)
    _write_out(hydro, cwd)

    return offsets, hydro


# ═════════════════  FILE WRITERS  ═══════════════════════════════
def _write_off1(offsets, folder="."):
    path = os.path.join(folder, "off1.mlt")
    with open(path, "w") as f:
        for row in offsets:
            f.write(", ".join(f"{v:.5f}" for v in row) + "\n")


def _write_out(hydro, folder="."):
    path = os.path.join(folder, "out.mlt")
    with open(path, "w") as f:
        f.write("# Optimised Hull Report\n")
        f.write(f"LOA (m), {hydro['L']:.4f}\n")
        f.write(f"Tmax (m), {hydro['T']:.4f}\n")
        f.write(f"Beam (m), {hydro['beam']:.4f}\n")
        f.write(f"Displacement (m3), {hydro['volume']:.5f}\n")
        f.write(f"LCB, {hydro['lcb']:.4f}\n")
        f.write(f"VCB (m), {hydro['vcb']:.4f}\n")
        f.write(f"Wetted Surface (m2), {hydro['S']:.4f}\n")
        f.write(f"Cb, {hydro['Cb']:.4f}\n")
        f.write(f"GMT (m), {hydro['gmt']:.4f}\n")
