//! Where the paddlers kneel (`[seats]`, PLAN B2).
//!
//! A load case's seats are, in order of precedence: its `seats_in` (given),
//! evenly spaced L(i+1)/(k+1) (`seats.auto = false`), or chosen here (auto).
//!
//! **Auto.** Seats only move the global V(x), M(x) (and through trim the
//! freeboard and GM); knee punching does not depend on them. So the seats and
//! crew order (paddlers pinned by `positions` stay in their seat) minimize the highest
//! utilization of the seat-dependent gates
//! ([`score`]: bending tension, bending compression, shear and biaxial D/C
//! against dc_search, freeboard and GM against their minimums). Constraints: knees at least `spacing_min_in`
//! apart, within [`bow_clear_in`, L - `stern_clear_in`]. Every distinct crew
//! order (<= 4 paddlers, <= 24 orders) is tried.
//!
//! The search runs on a surrogate that is exact up to one interpolation:
//! equilibrium depends on the crew only through its total weight and its
//! centroid, so the buoyancy-minus-self-weight V0(x), M0(x) are solved at 9
//! crew centroids across the allowed range (per load combination) and
//! interpolated linearly; each paddler's knee patch is then superposed in
//! closed form (`equilibrium::patch_vm`). A compass search per crew order
//! (from even spacing, steps down to 0.25 in) minimizes the surrogate score;
//! the best few orders and the even seats are then scored EXACTLY (`exact`,
//! a full equilibrium + check) and the lowest exact score wins (ties keep
//! even spacing). So auto seats are never worse than even spacing when even
//! spacing satisfies the constraints.
//!
//! **Variance.** Whatever the source, a case is checked at the nominal seats
//! and at all 2^k corners with each knee moved by +/- `variance_in`
//! ([`samples`]); the case verdict is the worst sample. So auto seats are scored
//! (surrogate and exact) as the worst of the nominal seats and the two corners
//! that move the whole crew forward or aft by the variance ([`shifted`]): the
//! float, freeboard and GM depend on the crew only through its centroid, so those
//! two are their worst corners, and seats that only pass at nominal are not picked.

use crate::check::{Verdict, check, first_moment_below, hoop_psi, kupfer_dc};
use crate::equilibrium::{CrewLoad, patch_vm, solve};
use crate::hull::Grid;
use crate::mix::StrengthFit;
use crate::params::{LoadCase, Params};
use crate::section::Sections;
use anyhow::{Result, bail};
use serde::Serialize;

/// Where a case's seats came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SeatSource {
    Given,
    Even,
    Auto,
}

/// Resolved seats of one load case.
#[derive(Debug, Clone, Serialize)]
pub struct CaseSeats {
    /// Knee x per paddler, in from the bow, in `weights_lb` order.
    pub seats_in: Vec<f64>,
    /// Paddler indices (into `weights_lb`) bow to stern.
    pub crew_order: Vec<usize>,
    pub source: SeatSource,
}

/// Evenly spaced knees: x0 + L (i+1)/(k+1).
pub fn even(k: usize, grid: &Grid) -> Vec<f64> {
    let (x0, l) = (grid.x[0], grid.x[grid.nx() - 1] - grid.x[0]);
    (0..k).map(|i| x0 + l * (i + 1) as f64 / (k + 1) as f64).collect()
}

/// Seat (slot, bow to stern) -> paddler for the entered order: pinned paddlers
/// (`positions`) in their seat, the others filling the free seats in `weights_lb` order.
pub fn default_order(case: &LoadCase) -> Vec<usize> {
    let k = case.weights_lb.len();
    let pos = |i: usize| case.positions.as_ref().map_or(0, |p| p[i]);
    let mut free = (0..k).filter(|&i| pos(i) == 0);
    (0..k)
        .map(|slot| {
            (0..k).find(|&i| pos(i) == slot + 1).unwrap_or_else(|| free.next().expect("one per seat"))
        })
        .collect()
}

/// Seats per paddler (`weights_lb` order) from slot positions bow to stern and a slot order.
pub fn place(order: &[usize], slots: &[f64]) -> Vec<f64> {
    let mut s = vec![0.0; order.len()];
    for (slot, &who) in order.iter().enumerate() {
        s[who] = slots[slot];
    }
    s
}

/// Evenly spaced seats for `case` in its [`default_order`].
pub fn even_placed(case: &LoadCase, grid: &Grid) -> Vec<f64> {
    place(&default_order(case), &even(case.weights_lb.len(), grid))
}

/// Paddler indices sorted bow to stern (stable on ties).
pub fn order_of(seats: &[f64]) -> Vec<usize> {
    let mut o: Vec<usize> = (0..seats.len()).collect();
    o.sort_by(|&a, &b| seats[a].total_cmp(&seats[b]));
    o
}

/// Given or evenly spaced seats; None = the case needs [`auto`].
pub fn fixed(params: &Params, case: &LoadCase, grid: &Grid) -> Option<CaseSeats> {
    let (seats_in, source) = match &case.seats_in {
        Some(s) => (s.clone(), SeatSource::Given),
        None if !params.seats.auto => (even_placed(case, grid), SeatSource::Even),
        None => return None,
    };
    Some(CaseSeats { crew_order: order_of(&seats_in), seats_in, source })
}

/// Seat sets a case is checked at: the nominal seats first, then (v > 0) the
/// 2^k corners, corner `m` moving knee j aft by v when bit j of m is set,
/// forward otherwise. Knees are kept on the hull [x0, xL].
pub fn samples(seats: &[f64], v: f64, grid: &Grid) -> Vec<Vec<f64>> {
    let mut out = vec![seats.to_vec()];
    if v > 0.0 {
        let (lo, hi) = (grid.x[0], grid.x[grid.nx() - 1]);
        for m in 0..1usize << seats.len() {
            out.push(
                seats
                    .iter()
                    .enumerate()
                    .map(|(j, &s)| (s + if m >> j & 1 == 1 { v } else { -v }).clamp(lo, hi))
                    .collect(),
            );
        }
    }
    out
}

/// Seat objective: the highest utilization of a seat-dependent gate,
/// max(D/C / dc_search, 1 - (freeboard - min) / s, 1 - (GM - min) / s) with
/// s = max(min, 1 in), where D/C is every check but punching. <= 1 = those
/// gates pass; finite for any finite freeboard and GM, so a search that
/// starts out failing a gate still sees which way is better. Minimizing it keeps
/// the most margin on whichever gate binds, so the seats do not trade a
/// passing gate's margin for a check that does not govern. Non-finite = +inf.
pub fn score(params: &Params, v: &Verdict, gm_in: f64) -> f64 {
    let dc = v.checks.iter().filter(|c| c.name != "punching").map(|c| c.dc).fold(0.0, f64::max);
    utilization(params, dc, v.freeboard_in, gm_in)
}

fn utilization(params: &Params, dc: f64, fb: f64, gm: f64) -> f64 {
    let margin = |min: f64, v: f64| 1.0 - (v - min) / min.max(1.0);
    let u = (dc / params.gates.dc_search)
        .max(margin(params.gates.freeboard_required_in(), fb))
        .max(margin(params.stability.gm_min_in, gm));
    if u.is_finite() { u } else { f64::INFINITY }
}

/// Crew centroids the surrogate is solved at.
const NODES: usize = 9;

/// One surrogate node: per load combination, V0 and M0 without the crew
/// patches; service freeboard and GM.
struct Node {
    v0: Vec<Vec<f64>>,
    m0: Vec<Vec<f64>>,
    /// Transverse turning stress (`check::hoop_psi`), 0 without `[turning]`.
    hoop: Vec<Vec<f64>>,
    fb: f64,
    gm: f64,
}

struct Surrogate<'a> {
    params: &'a Params,
    x: Vec<f64>,
    /// Per station with I > 1e-3 Imax: (index, top/I, bottom/I, Q/(2 t I)).
    st: Vec<(usize, f64, f64, f64)>,
    lo: f64,
    hi: f64,
    nodes: Vec<Option<Node>>,
    /// Paddler weights x dynamic factor, `weights_lb` order.
    lb: Vec<f64>,
    combos: Vec<(f64, f64)>,
    /// Allowables: tension, compression, shear, Kupfer f'c.
    cap: [f64; 4],
}

impl<'a> Surrogate<'a> {
    #[allow(clippy::too_many_arguments)]
    fn build(
        params: &'a Params,
        fit: &StrengthFit,
        grid: &Grid,
        sec: &Sections,
        t: f64,
        rho: f64,
        case: &LoadCase,
        (lo, hi): (f64, f64),
    ) -> Self {
        let mat = &params.material;
        let lb: Vec<f64> = case.weights_lb.iter().map(|w| w * mat.dynamic_load_factor).collect();
        let w: f64 = lb.iter().sum();
        // The float (freeboard, GM) carries the real crew; the stresses the dynamic factor.
        let w_real: f64 = case.weights_lb.iter().sum();
        let (patch, cg) = (mat.knee_patch_in, params.stability.crew_cg_in);
        let combos = mat.combos();
        let n = grid.nx();
        let x = grid.x.clone();
        let mut cap = [f64::NAN; 4];
        // Freeboard where the gate looks. Lettering that does not fit makes the exact check
        // fail the candidate; the surrogate (a seat heuristic) then just uses the whole hull.
        let spans: Vec<[f64; 2]> = params
            .freeboard_spans(x[n - 1])
            .map(|s| s.iter().map(|s| [s.x0_in, s.x1_in]).collect())
            .unwrap_or_default();
        let nn = if hi > lo { NODES } else { 1 };
        let nodes = (0..nn)
            .map(|j| {
                let xe = if nn == 1 { lo } else { lo + (hi - lo) * j as f64 / (nn - 1) as f64 };
                let service = solve(grid, sec, rho, &[CrewLoad { x_in: xe, lb: w_real }], patch, cg).ok()?;
                let fb = crate::check::freeboard_over(grid, &service, &spans).0;
                let (mut v0, mut m0, mut hoop) = (Vec::new(), Vec::new(), Vec::new());
                for &(dead, live) in &combos {
                    let ld = if (dead, live * mat.dynamic_load_factor) == (1.0, 1.0) {
                        service.clone()
                    } else {
                        solve(grid, sec, rho * dead, &[CrewLoad { x_in: xe, lb: w * live }], patch, cg)
                            .ok()?
                    };
                    if cap[0].is_nan() {
                        let v = check(
                            grid,
                            sec,
                            &ld,
                            rho,
                            mat,
                            fit,
                            0.0,
                            &params.punching,
                            params.turning.as_ref(),
                        );
                        let c = |name: &str| {
                            v.checks.iter().find(|c| c.name == name).map_or(f64::NAN, |c| c.capacity)
                        };
                        cap = [
                            c("bending_tension"),
                            c("bending_compression"),
                            c("shear"),
                            c("bending_compression") / 0.85,
                        ];
                    }
                    hoop.push(hoop_psi(grid, &ld, t, params.turning.as_ref()));
                    let (mut v, mut m) = (ld.shear, ld.moment);
                    for i in 0..n {
                        let (fv, fm) = patch_vm(x[i], xe, patch);
                        v[i] += w * live * fv;
                        m[i] += w * live * fm;
                    }
                    v0.push(v);
                    m0.push(m);
                }
                Some(Node { v0, m0, hoop, fb, gm: service.gm_in })
            })
            .collect();
        let imax = sec.inertia.iter().cloned().fold(0.0, f64::max);
        let st = (0..n)
            .filter(|&i| sec.inertia[i] > 1e-3 * imax)
            .map(|i| {
                let ii = sec.inertia[i];
                let q = first_moment_below(grid.row(i), &grid.z, t, sec.zbar[i]);
                (i, (grid.depth - sec.zbar[i]) / ii, sec.zbar[i] / ii, q / (2.0 * t * ii))
            })
            .collect();
        Surrogate { params, x, st, lo, hi, nodes, lb, combos, cap }
    }

    /// Surrogate score of knees at `seats` (`weights_lb` order): D/C and GM the worst of the
    /// nominal seats and the whole crew moved forward / aft by the seat variance ([`shifted`]),
    /// freeboard the worst over `gates.freeboard_seat_shift_in` (what the gate sees).
    fn score(&self, seats: &[f64]) -> f64 {
        let (x0, xl) = (self.x[0], self.x[self.x.len() - 1]);
        let at = |d: f64| self.parts(&seats.iter().map(|&s| (s + d).clamp(x0, xl)).collect::<Vec<_>>());
        let shifts = |v: f64| if v > 0.0 { vec![-v, 0.0, v] } else { vec![0.0] };
        let (mut dc, mut gm, mut fb) = (0.0_f64, f64::INFINITY, f64::INFINITY);
        for d in shifts(self.params.seats.variance_in) {
            let Some((a, _, g)) = at(d) else { return f64::INFINITY };
            (dc, gm) = (dc.max(a), gm.min(g));
        }
        for d in shifts(self.params.gates.freeboard_seat_shift_in) {
            let Some((_, f, _)) = at(d) else { return f64::INFINITY };
            fb = fb.min(f);
        }
        utilization(self.params, dc, fb, gm)
    }

    /// (D/C, freeboard, GM) at `seats`; None where the float could not be solved.
    fn parts(&self, seats: &[f64]) -> Option<(f64, f64, f64)> {
        let w: f64 = self.lb.iter().sum();
        let xe = self.lb.iter().zip(seats).map(|(l, s)| l * s).sum::<f64>() / w;
        let (j, a) = if self.nodes.len() == 1 {
            (0, 0.0)
        } else {
            let u =
                ((xe - self.lo) / (self.hi - self.lo) * (NODES - 1) as f64).clamp(0.0, (NODES - 1) as f64);
            let j = (u.floor() as usize).min(NODES - 2);
            (j, u - j as f64)
        };
        let (Some(n0), Some(n1)) = (&self.nodes[j], &self.nodes[(j + 1).min(self.nodes.len() - 1)]) else {
            return None;
        };
        let patch = self.params.material.knee_patch_in;
        let [ft, fc, vc, fk] = self.cap;
        let mut dc: f64 = 0.0;
        for (c, &(_, live)) in self.combos.iter().enumerate() {
            for &(i, top, bot, sh) in &self.st {
                let (mut v, mut m) =
                    ((1.0 - a) * n0.v0[c][i] + a * n1.v0[c][i], (1.0 - a) * n0.m0[c][i] + a * n1.m0[c][i]);
                for (l, &s) in self.lb.iter().zip(seats) {
                    let (fv, fm) = patch_vm(self.x[i], s, patch);
                    v -= l * live * fv;
                    m -= l * live * fm;
                }
                let (ct, cc) = if m >= 0.0 { (bot, top) } else { (top, bot) };
                let (s_t, s_c, tau) = (m.abs() * ct, m.abs() * cc, v.abs() * sh);
                let sy = (1.0 - a) * n0.hoop[c][i] + a * n1.hoop[c][i];
                dc = dc.max(s_t / ft).max(s_c / fc).max(tau / vc).max(kupfer_dc(s_t, s_c, sy, tau, ft, fk));
            }
        }
        Some((dc, (1.0 - a) * n0.fb + a * n1.fb, (1.0 - a) * n0.gm + a * n1.gm))
    }
}

/// The seat sets auto seats are scored at: nominal, then (v > 0) every knee moved forward
/// by v and every knee moved aft by v, kept on the hull [x0, xL] like [`samples`].
pub fn shifted(seats: &[f64], v: f64, grid: &Grid) -> Vec<Vec<f64>> {
    let mut out = vec![seats.to_vec()];
    if v > 0.0 {
        let (lo, hi) = (grid.x[0], grid.x[grid.nx() - 1]);
        for d in [-v, v] {
            out.push(seats.iter().map(|&s| (s + d).clamp(lo, hi)).collect());
        }
    }
    out
}

/// Distinct crew orders (slot -> paddler index, bow to stern) with each pinned paddler
/// (`pos[i]` = seat 1..k, 0 = free) in its seat; orders that only swap free paddlers of equal
/// weight are dropped. Lexicographic.
fn orders(w: &[f64], pos: &[usize]) -> Vec<Vec<usize>> {
    fn rec(w: &[f64], pos: &[usize], cur: &mut Vec<usize>, used: &mut Vec<bool>, out: &mut Vec<Vec<usize>>) {
        let slot = cur.len();
        if slot == w.len() {
            out.push(cur.clone());
            return;
        }
        if let Some(p) = (0..w.len()).find(|&i| pos[i] == slot + 1) {
            cur.push(p);
            rec(w, pos, cur, used, out);
            cur.pop();
            return;
        }
        let mut tried: Vec<f64> = Vec::new();
        for i in 0..w.len() {
            if used[i] || pos[i] != 0 || tried.contains(&w[i]) {
                continue;
            }
            tried.push(w[i]);
            used[i] = true;
            cur.push(i);
            rec(w, pos, cur, used, out);
            cur.pop();
            used[i] = false;
        }
    }
    let mut out = Vec::new();
    rec(w, pos, &mut Vec::new(), &mut vec![false; w.len()], &mut out);
    out
}

/// Auto seats for `case` on this wall (see the module docs). `exact` scores
/// a seat set (`weights_lb` order) with the full analysis, Err = analysis
/// failed. Err when the constraints leave no room for the crew, or when every
/// exactly scored seat set failed (the first error).
#[allow(clippy::too_many_arguments)]
pub fn auto(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case: &LoadCase,
    exact: &dyn Fn(&[f64]) -> Result<f64>,
) -> Result<CaseSeats> {
    let sp = &params.seats;
    let k = case.weights_lb.len();
    let (x0, xl) = (grid.x[0], grid.x[grid.nx() - 1]);
    let (lo, hi) = (x0 + sp.bow_clear_in, xl - sp.stern_clear_in);
    let span = (k - 1) as f64 * sp.spacing_min_in;
    if hi < lo || span > hi - lo + 1e-9 {
        bail!(
            "auto seats: {k} paddlers {} in apart do not fit between bow_clear_in {} and stern_clear_in {} on a \
             {:.0} in hull; relax [seats] or give seats_in",
            sp.spacing_min_in,
            sp.bow_clear_in,
            sp.stern_clear_in,
            xl - x0
        );
    }
    let feasible = |p: &[f64]| {
        p[0] >= lo - 1e-9
            && p[k - 1] <= hi + 1e-9
            && p.windows(2).all(|w| w[1] - w[0] >= sp.spacing_min_in - 1e-9)
    };
    let ev = even(k, grid);
    let start = if feasible(&ev) {
        ev.clone()
    } else if k == 1 {
        vec![0.5 * (lo + hi)]
    } else {
        (0..k).map(|j| lo + (hi - lo) * j as f64 / (k - 1) as f64).collect()
    };
    // Nodes cover every crew centroid the shifted seats can reach.
    let v = sp.variance_in;
    let sur = Surrogate::build(params, fit, grid, sec, t, rho, case, ((lo - v).max(x0), (hi + v).min(xl)));
    let pos = case.positions.clone().unwrap_or_else(|| vec![0; k]);
    let mut best: Vec<(f64, Vec<usize>, Vec<f64>)> = Vec::new();
    for order in orders(&case.weights_lb, &pos) {
        let mut p = start.clone();
        let mut f = sur.score(&place(&order, &p));
        let mut step = 0.25 * (hi - lo).max(sp.spacing_min_in);
        let mut evals = 0;
        while step >= 0.25 && evals < 2000 {
            let mut moved = false;
            for mv in 0..=k {
                for sign in [1.0, -1.0] {
                    let mut q = p.clone();
                    match q.get_mut(mv) {
                        Some(v) => *v += sign * step,
                        None => q.iter_mut().for_each(|v| *v += sign * step),
                    }
                    if !feasible(&q) {
                        continue;
                    }
                    evals += 1;
                    let fq = sur.score(&place(&order, &q));
                    if fq < f {
                        (p, f, moved) = (q, fq, true);
                    }
                }
            }
            if !moved {
                step *= 0.5;
            }
        }
        best.push((f, order.clone(), place(&order, &p)));
    }
    best.sort_by(|a, b| a.0.total_cmp(&b.0));
    // Exact scoring: even spacing in the entered order (pins kept) first (wins ties), then the
    // 3 best orders.
    let mut tries: Vec<Vec<f64>> = Vec::new();
    if feasible(&ev) {
        tries.push(place(&default_order(case), &ev));
    }
    tries.extend(best.into_iter().take(3).map(|b| b.2));
    let mut pick: Option<(f64, Vec<f64>)> = None;
    let mut first_err = None;
    for s in tries {
        match exact(&s) {
            Ok(f) => {
                if pick.as_ref().is_none_or(|(g, _)| f < *g) {
                    pick = Some((f, s));
                }
            }
            Err(e) => {
                first_err.get_or_insert(e);
            }
        }
    }
    match pick {
        Some((_, s)) => Ok(CaseSeats { crew_order: order_of(&s), seats_in: s, source: SeatSource::Auto }),
        None => Err(first_err.unwrap_or_else(|| anyhow::anyhow!("auto seats: nothing to evaluate"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn orders_dedupe_equal_weights() {
        assert_eq!(orders(&[1.0, 2.0], &[0, 0]), vec![vec![0, 1], vec![1, 0]]);
        assert_eq!(orders(&[1.0, 1.0, 2.0], &[0, 0, 0]).len(), 3);
        assert_eq!(orders(&[1.0, 2.0, 3.0, 4.0], &[0; 4]).len(), 24);
    }

    #[test]
    fn pinned_paddlers_stay_in_their_seat() {
        // Paddler 3 steers in the stern (seat 4): the other three take every order of seats 1-3.
        let o = orders(&[1.0, 2.0, 3.0, 4.0], &[0, 0, 0, 4]);
        assert_eq!(o.len(), 6);
        assert!(o.iter().all(|x| x[3] == 3));
        // Two pinned: bow and stern fixed, the middle two swap.
        let o = orders(&[1.0, 2.0, 3.0, 4.0], &[0, 4, 1, 0]);
        assert_eq!(o, vec![vec![2, 0, 3, 1], vec![2, 3, 0, 1]]);
        // A pinned paddler is not interchangeable with a free one of the same weight.
        let o = orders(&[5.0, 5.0, 2.0], &[3, 0, 0]);
        assert_eq!(o, vec![vec![1, 2, 0], vec![2, 1, 0]]);
        // All pinned: exactly that order.
        assert_eq!(orders(&[1.0, 2.0, 3.0], &[2, 3, 1]), vec![vec![2, 0, 1]]);
    }

    #[test]
    fn default_order_fills_free_seats_in_entered_order() {
        let case = |pos: Option<Vec<usize>>| LoadCase {
            name: "x".into(),
            weights_lb: vec![1.0, 2.0, 3.0, 4.0],
            seats_in: None,
            positions: pos,
        };
        assert_eq!(default_order(&case(None)), vec![0, 1, 2, 3]);
        assert_eq!(default_order(&case(Some(vec![0, 0, 1, 0]))), vec![2, 0, 1, 3]);
        assert_eq!(default_order(&case(Some(vec![0, 4, 0, 0]))), vec![0, 2, 3, 1]);
        let g = Grid { x: vec![0.0, 100.0], z: vec![0.0, 1.0], y: vec![1.0; 4], depth: 1.0 };
        assert_eq!(even_placed(&case(Some(vec![0, 4, 0, 0])), &g), vec![20.0, 80.0, 40.0, 60.0]);
    }

    #[test]
    fn samples_are_nominal_then_corners() {
        let g = Grid { x: vec![0.0, 100.0], z: vec![0.0, 1.0], y: vec![1.0; 4], depth: 1.0 };
        let s = samples(&[30.0, 98.0], 5.0, &g);
        assert_eq!(s.len(), 5);
        assert_eq!(s[0], vec![30.0, 98.0]);
        assert_eq!(s[1], vec![25.0, 93.0]);
        assert_eq!(s[4], vec![35.0, 100.0], "clamped to the hull");
        assert_eq!(samples(&[30.0], 0.0, &g).len(), 1);
    }
}
