//! Race simulator: race time of a designed canoe over the ASCE courses, how
//! much of it is straight running vs turning, and how sensitive it is to each
//! (the straight / turning and displacement weightings of the drag objective).
//!
//! Pure functions, no I/O. Units at this boundary: hull in inches and pounds,
//! everything else SI (m, m/s, N, W, s) like `resistance`.
//!
//! Model (quasi-steady, one degree of freedom along a planned path):
//! * Path: standing start on x = 0 heading +x, a circular arc around each
//!   mark (radius R and centre direction are free; the buoy sits inside the
//!   circle `buoy_clearance_m` in from the arc), common tangent lines between
//!   arcs, then straight to the finish line once heading at it. A tangent line
//!   to a circle that holds the buoy never passes closer than the clearance,
//!   so marks are rounded on the right side by construction. Lane edges and
//!   the other buoys are penalised.
//! * Speed: m_eff dv/dt = T(v) - D(v) along the path, m_eff = (hull + crew)
//!   x (1 + `ADDED_MASS`). Thrust T = min(n F_max, n eta P / v) for n paddlers.
//!   D = straight resistance (Michell wave + ITTC friction, `resistance`) at the
//!   level draft of hull + crew, tabulated over speed, plus on an arc the
//!   steady-turn drag of `resistance::turn` (cross-flow; scales as v^2 at a
//!   given R, so it is tabulated per radius as drag / v^2). Integrated in
//!   distance on u = v^2 (no singularity at the standing start), RK4, step
//!   `DS_M` split at segment ends.
//! * Tightest radius: R >= `min_turn_radius_m` and >= the buoy clearance. The
//!   turn model needs no small-angle limit: the strip lateral velocity
//!   v + r x is exact rigid-body kinematics and cross-flow drag is the
//!   large-angle term, so tight turns are limited by their own drag.
//! * Path optimum: compass search over the start y and each mark's (R, centre
//!   angle), from deterministic starts. Deterministic, no randomness.
//! * Sensitivity: S = d ln(time) / d ln(drag) for the straight resistance and
//!   for the turning drag, path held (it is optimal, so first-order exact).
//!   Recommended `objective.straight_weight` = sum w t S_straight / sum w t
//!   (S_straight + S_turn); `objective.displacements` the same shares by crew.
//!
//! Neglected: wind, current, acceleration in yaw, paddle stroke pulsation,
//! wave drag transients, the hull's heel in a turn.

use crate::hull::{Grid, Hull, Topsides};
use crate::params::{Course, Displacement, RaceEvent, RaceParams, Side};
use crate::resistance::{level_draft_in, resistance, turn};
use crate::units::M_TO_IN;
use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;
use serde::Serialize;
use std::f64::consts::{PI, TAU};

pub const RACE_FORMAT: &str = "jaguar-race/1";
/// Surge added mass as a share of displacement (slender hull, ~0.05).
pub const ADDED_MASS: f64 = 0.05;
/// Integration step along the path, m (halving it moves a 200 m time < 0.01 s, tested).
const DS_M: f64 = 0.25;
/// Straight resistance table: speeds k * SPEED_STEP for k = 1..=SPEEDS.
const SPEED_STEP: f64 = 0.25;
const SPEEDS: usize = 24; // to 6 m/s (above Olympic K4 sprint speed); faster is an error
/// Stations of the underwater lateral profile for the turn model.
const PROFILE_STATIONS: usize = 101;
/// Largest turn radius tried, m.
const R_MAX_M: f64 = 60.0;
/// Turn drag table entries, log-spaced r_min..R_MAX_M.
const TURN_TABLE: usize = 241;
/// Penalty continuation, s per m^2 of integrated lane / buoy overshoot: each
/// stage re-polishes the previous optimum (the optimum must end with none).
const PENALTIES: [f64; 3] = [1.0e2, 1.0e4, 1.0e6];
/// Overshoot the optimum may keep, m (sampling scale; the clearance holds the margin).
const VIOLATION_TOL_M: f64 = 0.01;
const KG_PER_LB: f64 = 0.453_592_37;

/// The canoe as the simulator sees it for one crew.
pub struct Boat {
    pub mass_kg: f64,
    pub draft_in: f64,
    paddlers: f64,
    power_w: f64,
    params_eta: f64,
    max_thrust_n: f64,
    /// Straight resistance / v^2 at speeds k * SPEED_STEP, N s^2/m^2.
    straight_c: Vec<f64>,
    profile_x_in: Vec<f64>,
    profile_depth_in: Vec<f64>,
    displacement_lb: f64,
    /// Tightest radius the turn model is used at, m.
    pub r_min_m: f64,
    /// ln(turn drag / v^2) and drift at `TURN_TABLE` radii log-spaced r_min..R_MAX_M.
    turn_table: Vec<(f64, f64)>,
}

impl Boat {
    /// `topsides`: the hull extruded to its mold depth (see [`topsides`]).
    /// Tabulates the turn drag for the path search.
    pub fn new(
        topsides: &Hull,
        hull_lb: f64,
        crew_lb: &[f64],
        power_w: f64,
        rp: &RaceParams,
    ) -> Result<Boat> {
        let mut boat = Self::untabled(topsides, hull_lb, crew_lb, power_w, rp)?;
        boat.turn_table = (0..TURN_TABLE)
            .map(|k| {
                let (c, d) = boat.turn_exact(boat.table_radius(k as f64))?;
                ensure!(c > 0.0, "race: zero turning drag at a buoy turn");
                Ok((c.ln(), d))
            })
            .collect::<Result<_>>()?;
        Ok(boat)
    }

    /// Like [`Boat::new`] without the turn table: every turn is solved exactly
    /// (cheaper when the path is given, as in [`planned_time`]).
    pub fn untabled(
        topsides: &Hull,
        hull_lb: f64,
        crew_lb: &[f64],
        power_w: f64,
        rp: &RaceParams,
    ) -> Result<Boat> {
        let lb = hull_lb + crew_lb.iter().sum::<f64>();
        let draft_in = level_draft_in(topsides, lb)?;
        let straight_c = (1..=SPEEDS)
            .map(|k| {
                let v = k as f64 * SPEED_STEP;
                Ok(resistance(topsides, draft_in, v)?.total_n / (v * v))
            })
            .collect::<Result<Vec<f64>>>()?;
        let g = Grid::build(topsides, topsides.draft_design, PROFILE_STATIONS);
        let depth: Vec<f64> = (0..g.nx()).map(|i| (draft_in - keel_z(g.row(i), &g.z)).max(0.0)).collect();
        let mut boat = Boat {
            mass_kg: lb * KG_PER_LB,
            draft_in,
            paddlers: crew_lb.len() as f64,
            power_w,
            params_eta: rp.propulsive_efficiency,
            max_thrust_n: rp.max_thrust_n,
            straight_c,
            profile_x_in: g.x.clone(),
            profile_depth_in: depth,
            displacement_lb: lb,
            r_min_m: 0.0,
            turn_table: Vec::new(),
        };
        boat.r_min_m = rp.min_turn_radius_m.max(rp.buoy_clearance_m);
        ensure!(boat.r_min_m < R_MAX_M, "race: min_turn_radius_m must be < {R_MAX_M} m");
        Ok(boat)
    }

    /// Straight resistance at `v` m/s, N (v^2 times the tabulated coefficient,
    /// linear between table speeds, held below the first and above the last).
    pub fn straight_n(&self, v: f64) -> f64 {
        let f = (v / SPEED_STEP - 1.0).clamp(0.0, (SPEEDS - 1) as f64);
        let i = (f.floor() as usize).min(SPEEDS - 2);
        let a = f - i as f64;
        v * v * (self.straight_c[i] * (1.0 - a) + self.straight_c[i + 1] * a)
    }

    pub fn thrust_n(&self, v: f64) -> f64 {
        let cap = self.paddlers * self.max_thrust_n;
        if v <= 0.0 { cap } else { cap.min(self.paddlers * self.params_eta * self.power_w / v) }
    }

    /// Radius of turn table entry `k` (fractional), m.
    fn table_radius(&self, k: f64) -> f64 {
        self.r_min_m * (R_MAX_M / self.r_min_m).powf(k / (TURN_TABLE - 1) as f64)
    }

    /// Steady-turn drag / v^2 and drift angle at radius `r_m` (`resistance::turn`).
    fn turn_exact(&self, r_m: f64) -> Result<(f64, f64)> {
        let t = turn(&self.profile_x_in, &self.profile_depth_in, self.displacement_lb, 1.0, r_m * M_TO_IN)?;
        Ok((t.drag_n, t.drift_deg))
    }

    /// [`Boat::turn_exact`] from the table: ln(drag) linear in ln(R) (< 0.1%
    /// off, tested). Err outside r_min..R_MAX_M. Untabled: exact.
    pub fn turn_coeff(&self, r_m: f64) -> Result<(f64, f64)> {
        if self.turn_table.is_empty() {
            return self.turn_exact(r_m);
        }
        ensure!(
            r_m >= self.r_min_m * (1.0 - 1e-12) && r_m <= R_MAX_M * (1.0 + 1e-12),
            "race: turn radius {r_m} m outside {}..{R_MAX_M} m",
            self.r_min_m
        );
        let f = ((r_m / self.r_min_m).ln() / (R_MAX_M / self.r_min_m).ln() * (TURN_TABLE - 1) as f64)
            .clamp(0.0, (TURN_TABLE - 1) as f64);
        let i = (f.floor() as usize).min(TURN_TABLE - 2);
        let a = f - i as f64;
        let (p, q) = (self.turn_table[i], self.turn_table[i + 1]);
        Ok(((p.0 * (1.0 - a) + q.0 * a).exp(), p.1 * (1.0 - a) + q.1 * a))
    }
}

/// Keel height of one grid station: the waterline below the first non-zero
/// half-breadth (the wall rule of `section::segment_len`).
fn keel_z(y: &[f64], z: &[f64]) -> f64 {
    match y.iter().position(|v| *v > 0.0) {
        Some(0) | None => z[0],
        Some(k) => z[k - 1],
    }
}

/// `hull` extruded to the mold depth (flare, gunwale outline), as the wall
/// search builds it, so a heavy crew floats inside the table.
pub fn topsides(hull: &Hull, depth_in: f64, ts: Topsides) -> Hull {
    let g = Grid::build_topsides(hull, depth_in, hull.x.len().max(41), ts).0;
    Hull {
        name: hull.name.clone(),
        draft_design: g.z[g.nz() - 1],
        length: hull.length,
        x: g.x.clone(),
        z: g.z.clone(),
        half_breadth: (0..g.nx()).map(|i| g.row(i).to_vec()).collect(),
        ..hull.clone()
    }
}

// ---------------------------------------------------------------- path

type P2 = [f64; 2];

fn sub(a: P2, b: P2) -> P2 {
    [a[0] - b[0], a[1] - b[1]]
}
fn norm(a: P2) -> f64 {
    a[0].hypot(a[1])
}
fn left(a: P2) -> P2 {
    [-a[1], a[0]]
}

/// Circle travelled counter-clockwise (s = 1) or clockwise (s = -1); s r = 0 is a point.
#[derive(Clone, Copy)]
struct Circle {
    c: P2,
    r: f64,
    s: f64,
}

/// Directed common tangent from `a` to `b` consistent with both travel
/// directions: (leave point on a, arrive point on b), or None if they overlap.
/// The circle a travel direction d sees on its left (s = 1) has its centre at
/// T + r n, n = left(d); so n . (cb - ca) = sb rb - sa ra.
fn tangent(a: Circle, b: Circle) -> Option<(P2, P2)> {
    let d = sub(b.c, a.c);
    let l = norm(d);
    if l < 1e-12 {
        return None;
    }
    let u = [d[0] / l, d[1] / l];
    let k = (b.s * b.r - a.s * a.r) / l;
    if k.abs() > 1.0 {
        return None;
    }
    let up = left(u);
    let h = (1.0 - k * k).sqrt();
    let n = [k * u[0] + h * up[0], k * u[1] + h * up[1]];
    let ta = [a.c[0] - a.s * a.r * n[0], a.c[1] - a.s * a.r * n[1]];
    let tb = [b.c[0] - b.s * b.r * n[0], b.c[1] - b.s * b.r * n[1]];
    Some((ta, tb))
}

/// Angle swept from `p` to `q` around `c` in travel direction `s`, [0, 2 pi).
fn sweep(c: P2, p: P2, q: P2, s: f64) -> f64 {
    let a0 = (p[1] - c[1]).atan2(p[0] - c[0]);
    let a1 = (q[1] - c[1]).atan2(q[0] - c[0]);
    let a = (s * (a1 - a0)).rem_euclid(TAU);
    if a > TAU - 1e-9 { 0.0 } else { a }
}

#[derive(Debug, Clone, Serialize)]
pub struct Segment {
    pub length_m: f64,
    /// Arc around this mark (index into the course marks), or None for a straight.
    pub mark: Option<usize>,
    pub radius_m: Option<f64>,
    /// Arc centre and start angle, travel direction (for drawing / sampling).
    #[serde(skip)]
    arc: Option<(P2, f64, f64)>,
    #[serde(skip)]
    start: P2,
    #[serde(skip)]
    dir: P2,
}

impl Segment {
    fn point(&self, s: f64) -> P2 {
        match (self.arc, self.radius_m) {
            (Some((c, a0, sg)), Some(r)) => {
                let a = a0 + sg * s / r;
                [c[0] + r * a.cos(), c[1] + r * a.sin()]
            }
            _ => [self.start[0] + s * self.dir[0], self.start[1] + s * self.dir[1]],
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct Violation {
    integral_m2: f64,
    worst_m: f64,
}

/// Free path variables: start y, then (radius, centre angle) per mark.
#[derive(Debug, Clone, PartialEq)]
pub struct PathVars(pub Vec<f64>);

/// Build the path, or None when two arcs overlap / the finish is behind the
/// last arc. Also returns the worst lane / buoy violation, m.
fn build_path(course: &Course, rp: &RaceParams, v: &PathVars) -> Option<(Vec<Segment>, Violation)> {
    let clr = rp.buoy_clearance_m;
    let circles: Vec<Circle> = course
        .marks
        .iter()
        .enumerate()
        .map(|(i, m)| {
            let (r, th) = (v.0[1 + 2 * i], v.0[2 + 2 * i]);
            let off = r - clr;
            Circle {
                c: [m.x_m + off * th.cos(), m.y_m + off * th.sin()],
                r,
                s: if m.keep == Side::Port { 1.0 } else { -1.0 },
            }
        })
        .collect();
    let start = Circle { c: [0.0, v.0[0]], r: 0.0, s: 1.0 };
    let mut segs = Vec::new();
    let line = |p: P2, q: P2, segs: &mut Vec<Segment>| {
        let d = sub(q, p);
        let l = norm(d);
        if l > 0.0 {
            segs.push(Segment {
                length_m: l,
                mark: None,
                radius_m: None,
                arc: None,
                start: p,
                dir: [d[0] / l, d[1] / l],
            });
        }
    };
    let arc = |i: usize, c: Circle, p: P2, q: P2, segs: &mut Vec<Segment>| {
        let a = sweep(c.c, p, q, c.s);
        segs.push(Segment {
            length_m: a * c.r,
            mark: Some(i),
            radius_m: Some(c.r),
            arc: Some((c.c, (p[1] - c.c[1]).atan2(p[0] - c.c[0]), c.s)),
            start: p,
            dir: [0.0, 0.0],
        });
    };
    let (_, mut entry) = tangent(start, circles[0])?;
    line(start.c, entry, &mut segs);
    for i in 0..circles.len() {
        let c = circles[i];
        let exit = if i + 1 < circles.len() {
            let (ex, next_entry) = tangent(c, circles[i + 1])?;
            arc(i, c, entry, ex, &mut segs);
            line(ex, next_entry, &mut segs);
            entry = next_entry;
            continue;
        } else {
            // Leave heading straight at the finish line: direction f, exit T = c - s r left(f).
            let f = if course.finish_x_m >= c.c[0] { [1.0, 0.0] } else { [-1.0, 0.0] };
            let n = left(f);
            [c.c[0] - c.s * c.r * n[0], c.c[1] - c.s * c.r * n[1]]
        };
        arc(i, c, entry, exit, &mut segs);
        let run = (course.finish_x_m - exit[0]) * if course.finish_x_m >= c.c[0] { 1.0 } else { -1.0 };
        if run < 0.0 {
            return None;
        }
        line(exit, [course.finish_x_m, exit[1]], &mut segs);
    }
    // Overshoot of the centreline past a lane edge or inside a buoy's clearance,
    // sampled every <= 0.25 m: integrated over the path (m^2, smooth, for the
    // search penalty) and worst (m, for acceptance).
    let half = course.lane_half_m(rp);
    let mut bad = Violation { integral_m2: 0.0, worst_m: 0.0 };
    for sg in &segs {
        let n = (sg.length_m / 0.25).ceil().max(1.0) as usize;
        let ds = sg.length_m / n as f64;
        for j in 0..=n {
            let p = sg.point(ds * j as f64);
            let lane = (p[1].abs() - half).max(0.0);
            // Squared test first: almost every sample is far from every buoy.
            let buoy = course
                .marks
                .iter()
                .map(|m| {
                    let d = sub(p, [m.x_m, m.y_m]);
                    let d2 = d[0] * d[0] + d[1] * d[1];
                    if d2 >= clr * clr { 0.0 } else { clr - d2.sqrt() }
                })
                .sum::<f64>();
            bad.integral_m2 += (lane + buoy) * ds;
            bad.worst_m = bad.worst_m.max(lane.max(buoy));
        }
    }
    Some((segs, bad))
}

// ---------------------------------------------------------------- speed

#[derive(Debug, Clone, Serialize)]
pub struct SegmentRun {
    pub mark: Option<usize>,
    pub radius_m: Option<f64>,
    pub length_m: f64,
    pub time_s: f64,
    pub entry_mps: f64,
    pub min_mps: f64,
    pub exit_mps: f64,
    /// Turn angle swept, deg (arcs only).
    pub angle_deg: Option<f64>,
    pub drift_deg: Option<f64>,
}

/// Run `segs` from a standing start; drags scaled by (straight, turn) for sensitivities.
fn simulate(boat: &Boat, segs: &[Segment], scale: (f64, f64), ds: f64) -> Result<Vec<SegmentRun>> {
    simulate_traced(boat, segs, scale, ds, None)
}

/// [`simulate`], also pushing (distance along the path m, speed m/s, time s) to
/// `trace` about every metre and at every segment end.
fn simulate_traced(
    boat: &Boat,
    segs: &[Segment],
    scale: (f64, f64),
    ds: f64,
    mut trace: Option<&mut Vec<[f64; 3]>>,
) -> Result<Vec<SegmentRun>> {
    let v_max = SPEEDS as f64 * SPEED_STEP;
    let m_eff = boat.mass_kg * (1.0 + ADDED_MASS);
    let mut u: f64 = 0.0; // v^2
    let mut out = Vec::with_capacity(segs.len());
    let (mut s0, mut t0, mut last) = (0.0, 0.0, f64::NEG_INFINITY);
    if let Some(tr) = trace.as_deref_mut() {
        tr.push([0.0, 0.0, 0.0]);
        last = 0.0;
    }
    for sg in segs {
        let (ct, drift) = match sg.radius_m {
            Some(r) => boat.turn_coeff(r)?,
            None => (0.0, 0.0),
        };
        // du/ds = 2 a(v).
        let f = |u: f64| {
            let v = u.max(0.0).sqrt();
            2.0 * (boat.thrust_n(v) - scale.0 * boat.straight_n(v) - scale.1 * ct * v * v) / m_eff
        };
        let n = (sg.length_m / ds).ceil().max(1.0) as usize;
        let h = sg.length_m / n as f64;
        let entry = u.sqrt();
        let (mut t, mut vmin) = (0.0, entry);
        let mut k1 = f(u);
        for j in 0..n {
            let k2 = f(u + 0.5 * h * k1);
            let k3 = f(u + 0.5 * h * k2);
            let k4 = f(u + h * k3);
            let u1 = u + h / 6.0 * (k1 + 2.0 * k2 + 2.0 * k3 + k4);
            ensure!(u1 > 0.0 && u1.is_finite(), "race: the canoe stalls (thrust < drag at rest?)");
            ensure!(u1 <= v_max * v_max, "race: faster than the {v_max} m/s drag table");
            let k1n = f(u1);
            t += if u > 0.0 {
                // Simpson on 1/v, midpoint from the cubic Hermite of u(s).
                let um = 0.5 * (u + u1) + h / 8.0 * (k1 - k1n);
                h / 6.0 * (1.0 / u.sqrt() + 4.0 / um.sqrt() + 1.0 / u1.sqrt())
            } else {
                // Standing start: u ~ s g(s), 1/v singular. With s = sigma^2,
                // dt = 2 dsigma / sqrt(g), smooth: Simpson in sigma, g(0) = k1,
                // g(h/4) from the Hermite of u at theta = 1/4.
                let uq = 0.140_625 * h * k1 + 0.156_25 * u1 - 0.046_875 * h * k1n;
                h.sqrt() / 6.0 * (2.0 / k1.sqrt() + 8.0 / (4.0 * uq / h).sqrt() + 2.0 / (u1 / h).sqrt())
            };
            (u, k1) = (u1, k1n);
            vmin = vmin.min(u.sqrt());
            let at = s0 + h * (j + 1) as f64;
            if let Some(tr) = trace.as_deref_mut()
                && at > last
                && (at - last >= 1.0 || j + 1 == n)
            {
                tr.push([at, u.sqrt(), t0 + t]);
                last = at;
            }
        }
        s0 += sg.length_m;
        t0 += t;
        out.push(SegmentRun {
            mark: sg.mark,
            radius_m: sg.radius_m,
            length_m: sg.length_m,
            time_s: t,
            entry_mps: entry,
            min_mps: vmin,
            exit_mps: u.sqrt(),
            angle_deg: sg.radius_m.map(|r| (sg.length_m / r).to_degrees()),
            drift_deg: sg.radius_m.map(|_| drift),
        });
    }
    Ok(out)
}

fn total_time(runs: &[SegmentRun]) -> f64 {
    runs.iter().map(|r| r.time_s).sum()
}

/// Penalised time of a path (infinite when it cannot be built).
fn cost(boat: &Boat, course: &Course, rp: &RaceParams, v: &PathVars, penalty: f64) -> f64 {
    let half = 0.5 * rp.lane_width_m;
    if v.0[0].abs() > half
        || (0..course.marks.len()).any(|i| !(boat.r_min_m..=R_MAX_M).contains(&v.0[1 + 2 * i]))
    {
        return f64::INFINITY;
    }
    match build_path(course, rp, v) {
        Some((segs, bad)) => match simulate(boat, &segs, (1.0, 1.0), DS_M) {
            Ok(runs) => total_time(&runs) + penalty * bad.integral_m2,
            Err(_) => f64::INFINITY,
        },
        None => f64::INFINITY,
    }
}

/// Deterministic starting path: radius `r0` and each centre toward the
/// midpoint of the neighbouring marks (the inside of the turn).
fn initial(course: &Course, r0: f64) -> PathVars {
    let n = course.marks.len();
    let pt = |i: isize| -> P2 {
        if i < 0 {
            [0.0, 0.0]
        } else if i as usize >= n {
            [course.finish_x_m, 0.0]
        } else {
            [course.marks[i as usize].x_m, course.marks[i as usize].y_m]
        }
    };
    let mut v = vec![0.0];
    for i in 0..n as isize {
        let (a, b, c) = (pt(i - 1), pt(i), pt(i + 1));
        let mid = [0.5 * (a[0] + c[0]) - b[0], 0.5 * (a[1] + c[1]) - b[1]];
        let th = if norm(mid) > 1e-9 { mid[1].atan2(mid[0]) } else { -PI / 2.0 };
        v.push(r0);
        v.push(th);
    }
    PathVars(v)
}

/// Compass search from `x` with start `steps` until every step < `tol`.
fn compass(
    boat: &Boat,
    course: &Course,
    rp: &RaceParams,
    mut x: PathVars,
    mut step: Vec<f64>,
    pen: f64,
    tol: f64,
) -> (f64, PathVars) {
    let mut fx = cost(boat, course, rp, &x, pen);
    let mut evals = 0;
    while step.iter().any(|s| *s > tol) && evals < 20_000 {
        let mut moved = false;
        for (i, &st) in step.iter().enumerate() {
            for sgn in [1.0, -1.0] {
                let mut y = x.clone();
                y.0[i] += sgn * st;
                let fy = cost(boat, course, rp, &y, pen);
                evals += 1;
                if fy < fx - 1e-9 {
                    (x, fx, moved) = (y, fy, true);
                    break;
                }
            }
        }
        if !moved {
            step.iter_mut().for_each(|s| *s *= 0.5);
        }
    }
    (fx, x)
}

/// Fastest path for `boat` on `course`: compass search with penalty
/// continuation from a few deterministic starts; the fastest that keeps every
/// lane edge and buoy clearance (within `VIOLATION_TOL_M`) wins.
pub fn optimal_path(boat: &Boat, course: &Course, rp: &RaceParams) -> Result<PathVars> {
    // Starts in parallel; results in start order, ties keep the earlier start (deterministic).
    // Each: Some((worst violation, time if within tolerance, path)).
    let runs: Vec<Option<(f64, Option<f64>, PathVars)>> = [2.0f64, 5.0, 10.0]
        .par_iter()
        .map(|&r0| -> Result<Option<(f64, Option<f64>, PathVars)>> {
            let mut x = initial(course, r0.max(boat.r_min_m + 0.1));
            if !cost(boat, course, rp, &x, 0.0).is_finite() {
                // Unbuildable start: skip it; a canoe the model cannot run: say why.
                if let Some((segs, _)) = build_path(course, rp, &x) {
                    simulate(boat, &segs, (1.0, 1.0), DS_M)?;
                }
                return Ok(None);
            }
            // Start steps: 1 m for the start y and radii, 0.3 rad for centre angles
            // (odd i = radius); later stages restart at a tenth.
            let step0: Vec<f64> =
                (0..x.0.len()).map(|i| if i > 0 && i % 2 == 0 { 0.3 } else { 1.0 }).collect();
            for (k, &pen) in PENALTIES.iter().enumerate() {
                let sc = if k == 0 { 1.0 } else { 0.1 };
                // Early stages stop at 1 cm / 0.01 rad; the last converges to 1e-3.
                let tol = if k + 1 == PENALTIES.len() { 1e-3 } else { 1e-2 };
                x = compass(boat, course, rp, x, step0.iter().map(|s| s * sc).collect(), pen, tol).1;
            }
            let Some((segs, bad)) = build_path(course, rp, &x) else { return Ok(None) };
            let t = if bad.worst_m > VIOLATION_TOL_M {
                None
            } else {
                Some(total_time(&simulate(boat, &segs, (1.0, 1.0), DS_M)?))
            };
            Ok(Some((bad.worst_m, t, x)))
        })
        .collect::<Result<_>>()?;
    let mut best: Option<(f64, PathVars)> = None;
    let mut worst_seen = f64::INFINITY;
    for (worst, t, x) in runs.into_iter().flatten() {
        worst_seen = worst_seen.min(worst);
        if let Some(t) = t
            && best.as_ref().is_none_or(|b| t < b.0)
        {
            best = Some((t, x));
        }
    }
    match best {
        Some((_, x)) => Ok(x),
        None if worst_seen.is_finite() => bail!(
            "race course '{}': best path leaves the lane or hits a buoy ({worst_seen:.3} m)",
            course.name
        ),
        None => bail!("race course '{}': no feasible path", course.name),
    }
}

// ---------------------------------------------------------------- results

#[derive(Debug, Clone, Serialize)]
pub struct EventResult {
    pub name: String,
    pub course: String,
    pub crew_lb: f64,
    /// The `[[load_case]]` the crew came from (None: the event's own `weights_lb`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub load_case: Option<String>,
    pub paddlers: usize,
    pub paddler_power_w: f64,
    pub weight: f64,
    pub draft_in: f64,
    pub r_min_m: f64,
    pub time_s: f64,
    pub distance_m: f64,
    pub straight_m: f64,
    pub straight_s: f64,
    pub turn_m: f64,
    pub turn_s: f64,
    pub mean_mps: f64,
    pub top_mps: f64,
    /// d ln(time) / d ln(straight resistance), path held.
    pub sens_straight: f64,
    /// d ln(time) / d ln(turning drag), path held.
    pub sens_turn: f64,
    pub start_y_m: f64,
    pub segments: Vec<SegmentRun>,
    /// Centreline every ~1 m, (x, y) m, for drawing.
    pub path_m: Vec<[f64; 2]>,
    /// (distance along the path m, speed m/s, time s) about every metre and at
    /// segment ends: replays the run (web Race tab).
    pub speed_m: Vec<[f64; 3]>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Recommended {
    /// For `objective.straight_weight`.
    pub straight_weight: f64,
    /// For `objective.displacements` (crew_lb rounded to 1 lb).
    pub displacements: Vec<Displacement>,
    /// Turn-time weighted mean arc radius, in (for `objective.turn_radius_in`).
    pub turn_radius_in: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct RaceResult {
    pub format: String,
    pub hull: String,
    pub hull_lb: f64,
    pub params: RaceParams,
    /// Sum of event weight x time, s.
    pub weighted_time_s: f64,
    pub events: Vec<EventResult>,
    pub recommended: Recommended,
}

/// One event on its optimal path.
pub fn run_event(top: &Hull, hull_lb: f64, rp: &RaceParams, e: &RaceEvent) -> Result<EventResult> {
    let power = e.paddler_power_w.unwrap_or(rp.paddler_power_w);
    let course = rp.courses.iter().find(|c| c.name == e.course).context("course")?;
    let boat = Boat::new(top, hull_lb, &e.weights_lb, power, rp)
        .with_context(|| format!("race event '{}'", e.name))?;
    let x = optimal_path(&boat, course, rp).with_context(|| format!("race event '{}'", e.name))?;
    let (segs, _) = build_path(course, rp, &x).context("path")?;
    let mut speed_m = Vec::new();
    let runs = simulate_traced(&boat, &segs, (1.0, 1.0), DS_M, Some(&mut speed_m))?;
    let t = total_time(&runs);
    let d: f64 = 0.01;
    let sens = |sc: (f64, f64), sc2: (f64, f64)| -> Result<f64> {
        let (a, b) =
            (total_time(&simulate(&boat, &segs, sc, DS_M)?), total_time(&simulate(&boat, &segs, sc2, DS_M)?));
        Ok((a / b).ln() / ((1.0 + d) / (1.0 - d)).ln())
    };
    let sens_straight = sens((1.0 + d, 1.0), (1.0 - d, 1.0))?;
    let sens_turn = sens((1.0, 1.0 + d), (1.0, 1.0 - d))?;
    let arc = |r: &&SegmentRun| r.radius_m.is_some();
    let mut path_m = Vec::new();
    for sg in &segs {
        let n = sg.length_m.ceil().max(1.0) as usize;
        path_m.extend((0..n).map(|j| sg.point(sg.length_m * j as f64 / n as f64)));
    }
    if let Some(last) = segs.last() {
        path_m.push(last.point(last.length_m));
    }
    let distance_m: f64 = runs.iter().map(|r| r.length_m).sum();
    Ok(EventResult {
        name: e.name.clone(),
        course: e.course.clone(),
        crew_lb: e.weights_lb.iter().sum(),
        load_case: e.load_case.clone(),
        paddlers: e.weights_lb.len(),
        paddler_power_w: power,
        weight: e.weight,
        draft_in: boat.draft_in,
        r_min_m: boat.r_min_m,
        time_s: t,
        distance_m,
        straight_m: runs.iter().filter(|r| !arc(r)).map(|r| r.length_m).sum(),
        straight_s: runs.iter().filter(|r| !arc(r)).map(|r| r.time_s).sum(),
        turn_m: runs.iter().filter(arc).map(|r| r.length_m).sum(),
        turn_s: runs.iter().filter(arc).map(|r| r.time_s).sum(),
        mean_mps: distance_m / t,
        top_mps: runs.iter().map(|r| r.exit_mps.max(r.entry_mps)).fold(0.0, f64::max),
        sens_straight,
        sens_turn,
        start_y_m: x.0[0],
        segments: runs,
        path_m,
        speed_m,
    })
}

/// Each event's path, planned once for a reference canoe and then held while
/// many hulls are timed on it ([`planned_time`], the shape search's race
/// objective). The path is optimal for the reference, so a nearby hull's time
/// on it is off by second order only; `run` re-plans for the winner.
#[derive(Debug, Clone)]
pub struct Plan {
    paths: Vec<Vec<Segment>>,
}

/// Plan every event of `rp` on the reference canoe (topsides `top`, wall `hull_lb`).
pub fn plan(top: &Hull, hull_lb: f64, rp: &RaceParams) -> Result<Plan> {
    rp.validate()?;
    let paths = rp
        .events
        .par_iter()
        .map(|e| {
            let course = rp.courses.iter().find(|c| c.name == e.course).context("course")?;
            let power = e.paddler_power_w.unwrap_or(rp.paddler_power_w);
            let boat = Boat::new(top, hull_lb, &e.weights_lb, power, rp)
                .with_context(|| format!("race event '{}' (reference canoe)", e.name))?;
            let x = optimal_path(&boat, course, rp).with_context(|| format!("race event '{}'", e.name))?;
            Ok(build_path(course, rp, &x).context("path")?.0)
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Plan { paths })
}

/// Weighted race time (sum of event weight x time, s) of the canoe with
/// topsides `top` and wall `hull_lb` on the planned paths. Events with the
/// same crew and power share one boat.
pub fn planned_time(top: &Hull, hull_lb: f64, rp: &RaceParams, plan: &Plan) -> Result<f64> {
    ensure!(plan.paths.len() == rp.events.len(), "race plan is for other events");
    let mut boats: Vec<(&RaceEvent, Boat)> = Vec::new();
    let mut sum = 0.0;
    for (e, segs) in rp.events.iter().zip(&plan.paths) {
        let power = e.paddler_power_w.unwrap_or(rp.paddler_power_w);
        let same = |o: &RaceEvent| {
            o.weights_lb == e.weights_lb && o.paddler_power_w.unwrap_or(rp.paddler_power_w) == power
        };
        let i = match boats.iter().position(|(o, _)| same(o)) {
            Some(i) => i,
            None => {
                let b = Boat::untabled(top, hull_lb, &e.weights_lb, power, rp)
                    .with_context(|| format!("race event '{}'", e.name))?;
                boats.push((e, b));
                boats.len() - 1
            }
        };
        sum += e.weight * total_time(&simulate(&boats[i].1, segs, (1.0, 1.0), DS_M)?);
    }
    Ok(sum)
}

/// Every event of `rp` for the canoe `hull` (outer mold line) with wall weight
/// `hull_lb`, mold depth and flare as built.
pub fn run(hull: &Hull, hull_lb: f64, depth_in: f64, ts: Topsides, rp: &RaceParams) -> Result<RaceResult> {
    rp.validate()?;
    let top = topsides(hull, depth_in, ts);
    // Parallel over events; collect keeps input order (deterministic).
    let events = rp.events.par_iter().map(|e| run_event(&top, hull_lb, rp, e)).collect::<Result<Vec<_>>>()?;
    let weighted_time_s = events.iter().map(|e| e.weight * e.time_s).sum();
    Ok(RaceResult {
        format: RACE_FORMAT.into(),
        hull: hull.name.clone(),
        hull_lb,
        params: rp.clone(),
        weighted_time_s,
        recommended: recommend(&events)?,
        events,
    })
}

/// Weightings for the drag objective from the events' time sensitivities.
pub fn recommend(events: &[EventResult]) -> Result<Recommended> {
    let a: f64 = events.iter().map(|e| e.weight * e.time_s * e.sens_straight).sum();
    let b: f64 = events.iter().map(|e| e.weight * e.time_s * e.sens_turn).sum();
    ensure!(a + b > 0.0, "race: time does not depend on drag (no events with weight?)");
    let mut ds: Vec<Displacement> = Vec::new();
    for e in events {
        let crew = e.crew_lb.round();
        let w = e.weight * e.time_s * (e.sens_straight + e.sens_turn) / (a + b);
        match ds.iter_mut().find(|d| d.crew_lb == crew) {
            Some(d) => d.weight += w,
            None => ds.push(Displacement { crew_lb: crew, weight: w }),
        }
    }
    ds.sort_by(|p, q| p.crew_lb.total_cmp(&q.crew_lb));
    let (mut rt, mut tt) = (0.0, 0.0);
    for e in events {
        for s in e.segments.iter().filter(|s| s.radius_m.is_some()) {
            rt += e.weight * s.time_s * s.radius_m.unwrap_or(0.0);
            tt += e.weight * s.time_s;
        }
    }
    if tt <= 0.0 {
        bail!("race: no turns in any weighted event");
    }
    Ok(Recommended { straight_weight: a / (a + b), displacements: ds, turn_radius_in: rt / tt * M_TO_IN })
}

/// Paddler power (W) at which `event` takes `measured_s`, path re-optimised
/// at each trial (time falls monotonically with power). Bisection in [5, 400] W (400 W ~ an elite kayaker for a minute).
pub fn calibrate_power(
    top: &Hull,
    hull_lb: f64,
    rp: &RaceParams,
    event: &str,
    measured_s: f64,
) -> Result<f64> {
    let e = rp
        .events
        .iter()
        .find(|e| e.name == event)
        .with_context(|| format!("no race event named '{event}'"))?;
    ensure!(measured_s.is_finite() && measured_s > 0.0, "measured time must be > 0");
    let time = |p: f64| -> Result<f64> {
        let mut ev = e.clone();
        ev.paddler_power_w = Some(p);
        Ok(run_event(top, hull_lb, rp, &ev)?.time_s)
    };
    let (mut lo, mut hi) = (5.0, 400.0);
    ensure!(
        time(lo)? >= measured_s && time(hi)? <= measured_s,
        "measured {measured_s} s is outside what 5-400 W per paddler gives"
    );
    for _ in 0..40 {
        let mid = 0.5 * (lo + hi);
        if time(mid)? > measured_s {
            lo = mid
        } else {
            hi = mid
        }
        if hi - lo < 0.05 {
            break;
        }
    }
    Ok(0.5 * (lo + hi))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::params::Mark;
    use std::path::Path;

    fn params() -> RaceParams {
        RaceParams {
            paddler_power_w: 100.0,
            propulsive_efficiency: 0.7,
            max_thrust_n: 100.0,
            buoy_clearance_m: 0.6,
            lane_width_m: 15.0,
            min_turn_radius_m: 0.6,
            courses: vec![Course {
                name: "sprint".into(),
                finish_x_m: 0.0,
                marks: vec![Mark { x_m: 100.0, y_m: 0.0, keep: Side::Port }],
                lane_width_m: None,
            }],
            events: vec![RaceEvent {
                name: "2M".into(),
                course: "sprint".into(),
                weights_lb: vec![175.0, 165.0],
                load_case: None,
                weight: 1.0,
                paddler_power_w: None,
            }],
        }
    }

    fn off1() -> Hull {
        topsides(&Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap(), 12.0, Topsides::default())
    }

    #[test]
    fn tangents_touch_both_circles_in_travel_direction() {
        for (sa, sb) in [(1.0, 1.0), (1.0, -1.0), (-1.0, 1.0), (-1.0, -1.0)] {
            let a = Circle { c: [0.0, 0.0], r: 2.0, s: sa };
            let b = Circle { c: [10.0, 3.0], r: 1.5, s: sb };
            let (ta, tb) = tangent(a, b).unwrap();
            let d = sub(tb, ta);
            assert!((norm(sub(ta, a.c)) - a.r).abs() < 1e-12 && (norm(sub(tb, b.c)) - b.r).abs() < 1e-12);
            // Perpendicular to both radii, and each centre on the side its direction turns to.
            for (t, c) in [(ta, a), (tb, b)] {
                let rad = sub(t, c.c);
                assert!((rad[0] * d[0] + rad[1] * d[1]).abs() < 1e-9);
                let side = left(d)[0] * (c.c[0] - t[0]) + left(d)[1] * (c.c[1] - t[1]);
                assert!(side * c.s > 0.0, "centre on the wrong side for s = {}", c.s);
            }
        }
    }

    #[test]
    fn turn_table_matches_the_model() {
        let rp = params();
        let boat = Boat::new(&off1(), 100.0, &[175.0, 165.0], 100.0, &rp).unwrap();
        assert!(boat.r_min_m >= rp.buoy_clearance_m);
        let mut r = boat.r_min_m;
        while r < R_MAX_M {
            let (a, b) = (boat.turn_coeff(r).unwrap(), boat.turn_exact(r).unwrap());
            assert!((a.0 / b.0 - 1.0).abs() < 1e-3, "R {r}: {} vs {}", a.0, b.0);
            r *= 1.37;
        }
        assert!(boat.turn_coeff(0.5 * boat.r_min_m).is_err());
    }

    #[test]
    fn min_turn_radius_is_a_floor() {
        let mut rp = params();
        rp.min_turn_radius_m = 6.0;
        let boat = Boat::new(&off1(), 100.0, &[175.0, 165.0], 100.0, &rp).unwrap();
        assert_eq!(boat.r_min_m, 6.0);
        let r = run_event(&off1(), 100.0, &rp, &rp.events[0]).unwrap();
        let free = run_event(&off1(), 100.0, &params(), &params().events[0]).unwrap();
        let radius = |r: &EventResult| r.segments.iter().find_map(|s| s.radius_m).unwrap();
        assert!(radius(&free) < 6.0, "unconstrained turn {}", radius(&free));
        assert!((radius(&r) - 6.0).abs() < 1e-2 && r.time_s > free.time_s);
    }

    #[test]
    fn planned_time_matches_the_optimal_run_on_the_reference() {
        let rp = params();
        let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let top = topsides(&hull, 12.0, Topsides::default());
        let plan = plan(&top, 100.0, &rp).unwrap();
        let full = run(&hull, 100.0, 12.0, Topsides::default(), &rp).unwrap();
        let t = planned_time(&top, 100.0, &rp, &plan).unwrap();
        // Same path; exact vs tabulated turn drag only.
        assert!((t / full.weighted_time_s - 1.0).abs() < 1e-3, "{t} vs {}", full.weighted_time_s);
        // A heavier wall is slower on the same paths, and re-planning can only help it.
        let heavy = planned_time(&top, 140.0, &rp, &plan).unwrap();
        let replanned = run(&hull, 140.0, 12.0, Topsides::default(), &rp).unwrap().weighted_time_s;
        assert!(heavy > t && replanned <= heavy + 1e-3, "{heavy} {replanned}");
        // Second order: the held path costs < 0.2% here.
        assert!(heavy / replanned - 1.0 < 2e-3, "{heavy} vs {replanned}");
    }

    #[test]
    fn speed_trace_follows_the_run() {
        let rp = params();
        let r = run_event(&off1(), 100.0, &rp, &rp.events[0]).unwrap();
        let sp = &r.speed_m;
        assert_eq!(sp[0], [0.0, 0.0, 0.0]);
        assert!(sp.windows(2).all(|w| w[1][2] > w[0][2]), "time increases");
        assert!((sp.last().unwrap()[2] - r.time_s).abs() < 1e-9, "ends at the race time");
        assert!(sp.windows(2).all(|w| w[1][0] > w[0][0] && w[1][0] - w[0][0] <= 1.0 + DS_M + 1e-9));
        assert!((sp.last().unwrap()[0] - r.distance_m).abs() < 1e-9);
        // Segment end speeds are in the trace at the segment ends.
        let mut at = 0.0;
        for sg in &r.segments {
            at += sg.length_m;
            if sg.length_m > 0.0 {
                let p = sp.iter().find(|p| (p[0] - at).abs() < 1e-9).expect("segment end sampled");
                assert!((p[1] - sg.exit_mps).abs() < 1e-12);
            }
        }
    }

    #[test]
    fn a_course_lane_opens_water_for_a_loop() {
        // Turn buoy at 100 m, then an open-water mark 40 m off the lane, back to the line.
        let mut rp = params();
        rp.courses[0].marks.push(Mark { x_m: 140.0, y_m: 40.0, keep: Side::Port });
        assert!(rp.validate().is_err(), "outside the 15 m race lane");
        rp.courses[0].lane_width_m = Some(200.0);
        rp.validate().unwrap();
        let r = run_event(&off1(), 100.0, &rp, &rp.events[0]).unwrap();
        assert!(r.path_m.iter().any(|p| p[1] > 0.5 * rp.lane_width_m), "the path leaves the race lane");
        assert!(r.start_y_m.abs() <= 0.5 * rp.lane_width_m, "the start stays in the race lane");
        assert!(r.distance_m > 300.0, "{}", r.distance_m);
    }

    #[test]
    fn sweep_follows_direction() {
        let (c, p, q) = ([0.0, 0.0], [1.0, 0.0], [0.0, 1.0]);
        assert!((sweep(c, p, q, 1.0) - PI / 2.0).abs() < 1e-12);
        assert!((sweep(c, p, q, -1.0) - 1.5 * PI).abs() < 1e-12);
    }

    #[test]
    fn straight_run_matches_closed_form() {
        // Constant thrust F, drag k v^2: v^2(s) = (F/k)(1 - exp(-2 k s / m)).
        let rp = params();
        let mut boat = Boat::new(&off1(), 100.0, &[175.0, 165.0], 100.0, &rp).unwrap();
        boat.straight_c = vec![20.0; SPEEDS];
        boat.max_thrust_n = 50.0;
        boat.power_w = 1e9; // thrust capped at 2 x 50 N
        let seg = Segment {
            length_m: 100.0,
            mark: None,
            radius_m: None,
            arc: None,
            start: [0.0, 0.0],
            dir: [1.0, 0.0],
        };
        let run = &simulate(&boat, &[seg], (1.0, 1.0), DS_M).unwrap()[0];
        let m = boat.mass_kg * (1.0 + ADDED_MASS);
        let (f, k) = (100.0, 20.0);
        let v = ((f / k) * (1.0 - (-2.0 * k * 100.0 / m).exp())).sqrt();
        assert!((run.exit_mps - v).abs() < 1e-6, "{} vs {v}", run.exit_mps);
        // t(s) = (2 / (V alpha)) atanh(sqrt(1 - exp(-alpha s))), V^2 = F/k, alpha = 2k/m.
        let (vt, al) = ((f / k).sqrt(), 2.0 * k / m);
        let t = 2.0 / (vt * al) * (1.0 - (-al * 100.0).exp()).sqrt().atanh();
        assert!((run.time_s - t).abs() < 5e-4, "{} vs {t}", run.time_s);
    }

    #[test]
    fn sprint_is_sane_and_converged() {
        let rp = params();
        let r = run_event(&off1(), 100.0, &rp, &rp.events[0]).unwrap();
        // ~200 m out and back with one 180 deg turn.
        assert!(r.distance_m > 200.0 && r.distance_m < 215.0, "{}", r.distance_m);
        let turn = r.segments.iter().find(|s| s.radius_m.is_some()).unwrap();
        // 180 deg less the small angle between the converging legs (179.0 deg on the linear
        // table interpolation, 178.9 on the smooth-keel loft: radius 5.97 -> 5.66 m).
        assert!((turn.angle_deg.unwrap() - 180.0).abs() < 2.0, "{:?}", turn.angle_deg);
        assert!(r.time_s > 40.0 && r.time_s < 150.0, "{}", r.time_s);
        assert!(r.sens_straight > 0.0 && r.sens_turn > 0.0 && r.sens_straight + r.sens_turn < 1.0);
        // Path optimum: no other radius at the turn is faster.
        let boat = Boat::new(&off1(), 100.0, &[175.0, 165.0], 100.0, &rp).unwrap();
        let x = optimal_path(&boat, &rp.courses[0], &rp).unwrap();
        let best = cost(&boat, &rp.courses[0], &rp, &x, PENALTIES[2]);
        for dr in [-0.5, 0.5] {
            let mut y = x.clone();
            y.0[1] += dr;
            assert!(cost(&boat, &rp.courses[0], &rp, &y, PENALTIES[2]) >= best - 1e-9);
        }
        // Halving the step moves the time < 0.01 s.
        let (segs, _) = build_path(&rp.courses[0], &rp, &x).unwrap();
        let fine: Vec<Segment> = segs
            .iter()
            .flat_map(|s| {
                let mut a = s.clone();
                a.length_m *= 0.5;
                let mut b = a.clone();
                b.start = s.point(a.length_m);
                if let (Some((c, a0, sg)), Some(rr)) = (s.arc, s.radius_m) {
                    b.arc = Some((c, a0 + sg * a.length_m / rr, sg));
                }
                [a, b]
            })
            .collect();
        let t1 = total_time(&simulate(&boat, &segs, (1.0, 1.0), DS_M).unwrap());
        let t2 = total_time(&simulate(&boat, &fine, (1.0, 1.0), DS_M).unwrap());
        assert!((t1 - t2).abs() < 0.01, "{t1} vs {t2}");
    }

    #[test]
    fn slalom_rounds_every_buoy_on_its_side() {
        let mut rp = params();
        let xs = [15.0, 25.0, 35.0, 55.0, 65.0, 75.0, 85.0];
        let mut marks: Vec<Mark> = xs
            .iter()
            .enumerate()
            .map(|(i, &x)| {
                let up = i % 2 == 0;
                Mark {
                    x_m: x,
                    y_m: if up { 2.5 } else { -2.5 },
                    keep: if up { Side::Starboard } else { Side::Port },
                }
            })
            .collect();
        marks.push(Mark { x_m: 100.0, y_m: 0.0, keep: Side::Port });
        rp.courses[0].marks = marks;
        let r = run_event(&off1(), 100.0, &rp, &rp.events[0]).unwrap();
        let sprint = run_event(&off1(), 100.0, &params(), &params().events[0]).unwrap();
        assert!(r.time_s > sprint.time_s && r.turn_s > sprint.turn_s);
        assert!(r.sens_turn > sprint.sens_turn);
        // Each slalom buoy: the path passes it on the keep side (port = buoy to the left).
        let turn = r.path_m.iter().position(|p| p[0] > 99.0).unwrap();
        for m in &rp.courses[0].marks[..7] {
            let near = r.path_m[..turn]
                .iter()
                .min_by(|a, b| (a[0] - m.x_m).abs().total_cmp(&(b[0] - m.x_m).abs()))
                .unwrap();
            let above = near[1] > m.y_m;
            assert_eq!(above, m.keep == Side::Starboard, "buoy at {} {}", m.x_m, m.y_m);
        }
    }

    #[test]
    fn recommendation_and_calibration() {
        let rp = params();
        let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let r = run(&hull, 100.0, 12.0, Topsides::default(), &rp).unwrap();
        let rec = &r.recommended;
        assert!(rec.straight_weight > 0.5 && rec.straight_weight < 1.0, "{}", rec.straight_weight);
        assert_eq!(rec.displacements.len(), 1);
        assert!((rec.displacements[0].weight - 1.0).abs() < 1e-12);
        let p = calibrate_power(
            &topsides(&hull, 12.0, Topsides::default()),
            100.0,
            &rp,
            "2M",
            r.events[0].time_s * 0.9,
        )
        .unwrap();
        assert!(p > 100.0, "faster measured time needs more power, got {p}");
    }
}
