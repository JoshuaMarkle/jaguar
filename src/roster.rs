//! Roster: who races which event (`jaguar serve` Roster view, docs/SPEC.md "Roster").
//!
//! For every `[[race.event]]` the fastest eligible crew that is safe on the finished canoe:
//! * eligible = the event's gender mix (men, women), people marked out of it excluded, people
//!   marked in it included;
//! * power per paddler from the gym tests (1-min watts for courses under [`SPRINT_M`], 4-min
//!   watts otherwise; a missing test is estimated from mass, P = c m^(2/3) with c fitted to the
//!   same gender's tests (else everyone's, else `race.paddler_power_w`), and flagged);
//! * synergy: sides alternate bow to stern and the crew switches sides together, so the power on
//!   each side is fixed by the seat order; the steerer's correcting (ruddering) costs
//!   k_track |P_left - P_right| and holding a common pace k_pace sum |P_i - mean P|, effective
//!   power = sum P minus both (seat orders that track straight, so rudder least, win);
//! * a steerer rated >= 1 for the steering seat (stern; bow too where the event allows the
//!   rudder), each rating step below 3 costing `steer_penalty` x the event's turning time;
//! * time on the event's planned path (`race::plan`, `race::crew_time`) with power per paddler =
//!   effective power / n;
//! * safety: the crew in that order at its best knees (`seating::best`, everyone pinned) must be
//!   safe (`seating::assess`). Crews are tried fastest first; the first safe one wins, and a safe
//!   crew within [`TIE_S`] of it with a lower D/C replaces it. No safe crew = the event says so.
//!
//! Events are solved independently: a paddler may race any number of events.

use crate::hull::Hull;
use crate::params::RaceParams;
use crate::race::{self, Plan};
use crate::seating::{self, Canoe, Request as SeatRequest, Rules};
use anyhow::{Result, bail, ensure};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

/// Courses shorter than this use the 1-min test, longer the 4-min test, m.
pub const SPRINT_M: f64 = 300.0;
/// Crews this close in time are ties: the lower D/C wins, s.
pub const TIE_S: f64 = 0.1;
/// Highest steering rating.
pub const STEER_MAX: u8 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
pub enum Gender {
    M,
    F,
}

/// The side a seat starts the race on. Sides alternate bow to stern and the whole crew switches
/// together, so a paddler has no fixed side and nobody's preference is modelled (most paddle
/// either way); what matters is the power on each side at any moment.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PaddleSide {
    Left,
    Right,
}

/// One person on the roster.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Paddler {
    pub name: String,
    pub gender: Gender,
    pub weight_lb: f64,
    /// Recorded for the seating sheet; not used by the model yet.
    #[serde(default)]
    pub height_in: Option<f64>,
    /// Gym erg tests: 1-minute max and 4-minute average power, W.
    #[serde(default)]
    pub watts_1min: Option<f64>,
    #[serde(default)]
    pub watts_4min: Option<f64>,
    /// Steering from the stern (paddle) and the bow (rudder): 0 can't, 1 learning, 2 solid, 3 best.
    #[serde(default)]
    pub steer_stern: u8,
    #[serde(default)]
    pub steer_bow: u8,
}

/// One event's crew rules (defaults from the event's load case name, see [`default_events`]).
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EventSpec {
    /// Must match a `[[race.event]]` name.
    pub name: String,
    pub men: usize,
    pub women: usize,
    /// The bow paddler may steer (rudder).
    #[serde(default)]
    pub bow_rudder: bool,
    /// Roster indices that must race / may not race this event.
    #[serde(default)]
    pub include: Vec<usize>,
    #[serde(default)]
    pub exclude: Vec<usize>,
}

/// Synergy and steering weights (rules of thumb until fitted from timed practice runs).
#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub k_track: f64,
    pub k_pace: f64,
    pub steer_penalty: f64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { k_track: 0.5, k_pace: 0.1, steer_penalty: 0.02 }
    }
}

/// `POST /api/roster`.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub paddlers: Vec<Paddler>,
    pub events: Vec<EventSpec>,
    #[serde(default)]
    pub settings: Settings,
    /// Seat rules for the seating check (absent = the run's `[seats]`).
    #[serde(default)]
    pub rules: Option<Rules>,
}

/// One paddler in an assigned crew, bow to stern.
#[derive(Debug, Clone, Serialize)]
pub struct Member {
    /// Roster index.
    pub paddler: usize,
    pub name: String,
    pub weight_lb: f64,
    pub seat: usize,
    /// Side at the start (the crew switches together).
    pub side: PaddleSide,
    pub knee_in: f64,
    pub watts: f64,
    /// `watts` estimated from mass (no gym test for this event's length).
    pub estimated: bool,
    pub steerer: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct Assignment {
    pub event: String,
    pub course: String,
    pub weight: f64,
    /// "1min" | "4min": the test the event's power came from.
    pub test: &'static str,
    /// Bow to stern. Empty when no crew could be assigned (`error` says why).
    pub crew: Vec<Member>,
    pub time_s: Option<f64>,
    /// sum P, the synergy losses, and what is left, W.
    pub raw_power_w: Option<f64>,
    pub tracking_loss_w: Option<f64>,
    pub pace_loss_w: Option<f64>,
    pub effective_power_w: Option<f64>,
    /// Time added for a steerer rated below 3, s (inside `time_s`).
    pub steer_penalty_s: Option<f64>,
    /// The seating check of the crew at its knees.
    pub dc: Option<f64>,
    pub freeboard_in: Option<f64>,
    pub gm_in: Option<f64>,
    /// Crews timed and how many were checked for safety before the answer.
    pub crews_timed: usize,
    pub crews_checked: usize,
    pub notes: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Roster {
    pub assignments: Vec<Assignment>,
    /// Sum of event weight x time over the events that got a crew, s.
    pub weighted_time_s: f64,
    /// The mass estimates P = c m^(2/3) per test, for paddlers without that test.
    pub estimate_1min: Estimate,
    pub estimate_4min: Estimate,
}

/// Mass estimates of one test, per gender.
#[derive(Debug, Clone, Copy, Serialize)]
pub struct Estimate {
    pub men: Fit,
    pub women: Fit,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Fit {
    /// W / lb^(2/3).
    pub c: f64,
    /// "gender" (that gender's tests) | "everyone" (no test in that gender) | "race power"
    /// (no tests at all: `race.paddler_power_w` at the roster's mean mass).
    pub from: &'static str,
    /// Tests the fit used.
    pub tests: usize,
}

impl Estimate {
    fn c(&self, g: Gender) -> f64 {
        match g {
            Gender::M => self.men.c,
            Gender::F => self.women.c,
        }
    }
}

/// Gender counts from a load case name such as "2M, 2F", "1M, 1F", "2M" or "3F" (a count
/// of 1 to 8 directly followed by M or F/W and then not a letter); None when the name has none
/// ("200 m" and "200m" are distances, not 200 men).
pub fn mix_of(name: &str) -> Option<(usize, usize)> {
    let b = name.as_bytes();
    let (mut men, mut women, mut any, mut i) = (0, 0, false, 0);
    while i < b.len() {
        if !b[i].is_ascii_digit() || (i > 0 && b[i - 1].is_ascii_alphanumeric()) {
            i += 1;
            continue;
        }
        let j = (i..b.len()).find(|&j| !b[j].is_ascii_digit()).unwrap_or(b.len());
        let after_ok = b.get(j + 1).is_none_or(|c| !c.is_ascii_alphabetic());
        // A crew is at most 8: a bigger number is a distance ("200m").
        if let (Some(c), true, Ok(n @ 1..=8)) = (b.get(j), after_ok, name[i..j].parse::<usize>()) {
            match c.to_ascii_uppercase() {
                b'M' => (men, any) = (men + n, true),
                b'F' | b'W' => (women, any) = (women + n, true),
                _ => {}
            }
        }
        i = j;
    }
    any.then_some((men, women))
}

/// Gender counts of an event: from its load case name, else its own name ([`mix_of`]), else
/// guessed from its crew size and the words in its name ("women's" all women, "men's" all men,
/// "co-ed" / "coed" / "mixed" half each, men taking the odd seat); None = set it on the page.
pub fn event_mix(e: &crate::params::RaceEvent) -> Option<(usize, usize)> {
    if let Some(m) = e.load_case.as_deref().and_then(mix_of).or_else(|| mix_of(&e.name)) {
        return Some(m);
    }
    let n = e.weights_lb.len();
    let low = e.name.to_lowercase();
    if n == 0 {
        None
    } else if low.contains("co-ed") || low.contains("coed") || low.contains("mixed") {
        Some((n - n / 2, n / 2))
    } else if low.contains("women") {
        Some((0, n))
    } else if low.contains("men") {
        Some((n, 0))
    } else {
        None
    }
}

/// One [`EventSpec`] per race event, gender mix from [`event_mix`] (0 / 0 when unknown: the
/// page asks for it).
pub fn default_events(rp: &RaceParams) -> Vec<EventSpec> {
    rp.events
        .iter()
        .map(|e| {
            let (men, women) = event_mix(e).unwrap_or((0, 0));
            EventSpec {
                name: e.name.clone(),
                men,
                women,
                bow_rudder: false,
                include: vec![],
                exclude: vec![],
            }
        })
        .collect()
}

/// Least-squares c in P = c m^(2/3) per gender through that gender's paddlers with the test;
/// a gender with none uses everyone's tests; no tests at all, `fallback_w` at the mean mass.
fn estimate(ps: &[Paddler], test: fn(&Paddler) -> Option<f64>, fallback_w: f64) -> Estimate {
    let fit = |keep: &dyn Fn(&Paddler) -> bool| -> Option<(f64, usize)> {
        let pts: Vec<(f64, f64)> = ps
            .iter()
            .filter(|p| keep(p))
            .filter_map(|p| test(p).map(|w| (p.weight_lb.powf(2.0 / 3.0), w)))
            .collect();
        let (sxy, sxx) = pts.iter().fold((0.0, 0.0), |(a, b), (x, y)| (a + x * y, b + x * x));
        (!pts.is_empty()).then(|| (sxy / sxx, pts.len()))
    };
    let m = ps.iter().map(|p| p.weight_lb).sum::<f64>() / ps.len() as f64;
    let all = fit(&|_| true);
    let one = |g: Gender| match (fit(&|p: &Paddler| p.gender == g), all) {
        (Some((c, n)), _) => Fit { c, from: "gender", tests: n },
        (None, Some((c, n))) => Fit { c, from: "everyone", tests: n },
        (None, None) => Fit { c: fallback_w / m.powf(2.0 / 3.0), from: "race power", tests: 0 },
    };
    Estimate { men: one(Gender::M), women: one(Gender::F) }
}

fn validate(req: &Request, rp: &RaceParams) -> Result<()> {
    ensure!(!req.paddlers.is_empty(), "the roster is empty");
    for (i, p) in req.paddlers.iter().enumerate() {
        let who = if p.name.trim().is_empty() { format!("paddler {}", i + 1) } else { p.name.clone() };
        ensure!(
            p.weight_lb.is_finite() && p.weight_lb > 0.0 && p.weight_lb < 1000.0,
            "{who}: weight 1 to 999 lb"
        );
        for (w, t) in [(p.watts_1min, "1-min"), (p.watts_4min, "4-min")] {
            if let Some(w) = w {
                ensure!(w.is_finite() && w > 0.0 && w < 3000.0, "{who}: {t} watts {w}: give 1 to 2999 W");
            }
        }
        if let Some(h) = p.height_in {
            ensure!(h.is_finite() && h > 0.0 && h < 120.0, "{who}: height {h} in");
        }
        ensure!(p.steer_stern <= STEER_MAX && p.steer_bow <= STEER_MAX, "{who}: steering ratings are 0 to 3");
    }
    let s = &req.settings;
    for (v, n) in [(s.k_track, "k_track"), (s.k_pace, "k_pace"), (s.steer_penalty, "steer_penalty")] {
        ensure!(v.is_finite() && (0.0..=5.0).contains(&v), "{n} {v}: give 0 to 5");
    }
    for e in &req.events {
        ensure!(rp.events.iter().any(|r| r.name == e.name), "no race event named '{}'", e.name);
        for &i in e.include.iter().chain(&e.exclude) {
            ensure!(i < req.paddlers.len(), "event '{}': no paddler {}", e.name, i + 1);
        }
    }
    Ok(())
}

/// Every k-subset of `pool` (ascending), lexicographic.
fn subsets(pool: &[usize], k: usize) -> Vec<Vec<usize>> {
    let mut out = vec![];
    let mut cur = vec![];
    fn go(pool: &[usize], k: usize, start: usize, cur: &mut Vec<usize>, out: &mut Vec<Vec<usize>>) {
        if cur.len() == k {
            out.push(cur.clone());
            return;
        }
        for i in start..pool.len() {
            if pool.len() - i < k - cur.len() {
                break;
            }
            cur.push(pool[i]);
            go(pool, k, i + 1, cur, out);
            cur.pop();
        }
    }
    go(pool, k, 0, &mut cur, &mut out);
    out
}

/// Every permutation of `v`, lexicographic by position.
fn permutations(v: &[usize]) -> Vec<Vec<usize>> {
    if v.len() <= 1 {
        return vec![v.to_vec()];
    }
    let mut out = vec![];
    for i in 0..v.len() {
        let mut rest = v.to_vec();
        let x = rest.remove(i);
        for mut p in permutations(&rest) {
            p.insert(0, x);
            out.push(p);
        }
    }
    out
}

/// One way to sit a crew: order bow to stern, steering seat, side of the bow seat.
#[derive(Debug, Clone)]
struct Arrangement {
    order: Vec<usize>,
    steer_seat: usize,
    raw: f64,
    track: f64,
    pace: f64,
    eff: f64,
    steer_rating: u8,
}

/// Starting side of a seat: the bow on the left, then alternating (which side the bow starts on
/// does not change |left - right|).
fn side_at(seat: usize) -> PaddleSide {
    if seat.is_multiple_of(2) { PaddleSide::Left } else { PaddleSide::Right }
}

/// Best arrangements of one crew, most effective power first (ties: better steerer, then a
/// fixed order), at most `keep`.
fn arrangements(
    crew: &[usize],
    watts: &[f64],
    ps: &[Paddler],
    bow_rudder: bool,
    s: &Settings,
    keep: usize,
) -> Vec<Arrangement> {
    let n = crew.len();
    let mut out = vec![];
    for order in permutations(crew) {
        let seats: Vec<usize> = if bow_rudder && n > 1 { vec![0, n - 1] } else { vec![n - 1] };
        for &steer_seat in &seats {
            let r = if steer_seat == n - 1 {
                ps[order[steer_seat]].steer_stern
            } else {
                ps[order[steer_seat]].steer_bow
            };
            if r == 0 {
                continue;
            }
            let (mut l, mut rt) = (0.0, 0.0);
            for (seat, &i) in order.iter().enumerate() {
                if side_at(seat) == PaddleSide::Left { l += watts[i] } else { rt += watts[i] }
            }
            let raw = l + rt;
            let mean = raw / n as f64;
            let track = s.k_track * (l - rt).abs();
            let pace = s.k_pace * order.iter().map(|&i| (watts[i] - mean).abs()).sum::<f64>();
            out.push(Arrangement {
                order: order.clone(),
                steer_seat,
                raw,
                track,
                pace,
                eff: raw - track - pace,
                steer_rating: r,
            });
        }
    }
    out.sort_by(|a, b| {
        b.eff
            .total_cmp(&a.eff)
            .then(b.steer_rating.cmp(&a.steer_rating))
            .then(a.order.cmp(&b.order))
            .then(a.steer_seat.cmp(&b.steer_seat))
    });
    out.truncate(keep);
    out
}

/// A timed candidate: one crew in one arrangement.
struct Timed {
    arr: Arrangement,
    time: f64,
    steer_s: f64,
}

/// The seating check of a crew sat in `order` (everyone pinned): (knees, assessment), or why not.
fn seat_crew(
    c: &Canoe,
    ps: &[Paddler],
    order: &[usize],
    rules: Option<Rules>,
) -> Result<(Vec<f64>, seating::Assessment)> {
    let weights: Vec<f64> = order.iter().map(|&i| ps[i].weight_lb).collect();
    let pins: Vec<usize> = (1..=order.len()).collect();
    let s = seating::best(c, &weights, Some(&pins), rules)?;
    let a = seating::assess(
        c,
        &SeatRequest {
            weights_lb: weights,
            seats_in: s.seats_in.clone(),
            rules,
            positions: Some(pins),
            bands: false,
            plots: false,
        },
    )?;
    Ok((s.seats_in, a))
}

#[allow(clippy::too_many_arguments)]
fn assign_event(
    c: &Canoe,
    top: &Hull,
    rp: &RaceParams,
    plan: &Plan,
    i: usize,
    spec: &EventSpec,
    ps: &[Paddler],
    est: (Estimate, Estimate),
    req: &Request,
) -> Assignment {
    let e = &rp.events[i];
    let sprint = plan.length_m(i) < SPRINT_M;
    let (test, est) = if sprint { ("1min", est.0) } else { ("4min", est.1) };
    let measured = |p: &Paddler| if sprint { p.watts_1min } else { p.watts_4min };
    let watts: Vec<f64> =
        ps.iter().map(|p| measured(p).unwrap_or(est.c(p.gender) * p.weight_lb.powf(2.0 / 3.0))).collect();
    let mut a = Assignment {
        event: e.name.clone(),
        course: e.course.clone(),
        weight: e.weight,
        test,
        crew: vec![],
        time_s: None,
        raw_power_w: None,
        tracking_loss_w: None,
        pace_loss_w: None,
        effective_power_w: None,
        steer_penalty_s: None,
        dc: None,
        freeboard_in: None,
        gm_in: None,
        crews_timed: 0,
        crews_checked: 0,
        notes: vec![],
        error: None,
    };
    match solve_event(c, top, rp, plan, i, spec, ps, &watts, req, &mut a) {
        Ok(()) => {
            if a.crew.iter().any(|m| m.estimated) {
                a.notes.push(format!(
                    "power estimated from body mass for: {} (no {test} test)",
                    a.crew
                        .iter()
                        .filter(|m| m.estimated)
                        .map(|m| m.name.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            }
        }
        Err(err) => a.error = Some(format!("{err:#}")),
    }
    a
}

#[allow(clippy::too_many_arguments)]
fn solve_event(
    c: &Canoe,
    top: &Hull,
    rp: &RaceParams,
    plan: &Plan,
    i: usize,
    spec: &EventSpec,
    ps: &[Paddler],
    watts: &[f64],
    req: &Request,
    a: &mut Assignment,
) -> Result<()> {
    let n = spec.men + spec.women;
    ensure!(n > 0, "set how many men and women race this event");
    for &j in &spec.include {
        ensure!(!spec.exclude.contains(&j), "{} is both in and out of this event", ps[j].name);
    }
    let sprint = a.test == "1min";
    let pool = |g: Gender| -> Vec<usize> {
        (0..ps.len()).filter(|&j| ps[j].gender == g && !spec.exclude.contains(&j)).collect()
    };
    let (men, women) = (pool(Gender::M), pool(Gender::F));
    ensure!(men.len() >= spec.men, "needs {} men, {} available", spec.men, men.len());
    ensure!(women.len() >= spec.women, "needs {} women, {} available", spec.women, women.len());
    let mut crews = vec![];
    for m in subsets(&men, spec.men) {
        for w in subsets(&women, spec.women) {
            let crew: Vec<usize> = m.iter().chain(&w).copied().collect();
            if spec.include.iter().all(|j| crew.contains(j)) {
                crews.push(crew);
            }
        }
    }
    ensure!(!crews.is_empty(), "no crew of {}M {}F holds everyone marked in", spec.men, spec.women);
    // Time every crew in its most powerful arrangements (a steerer for every seat it allows).
    let keep = 6;
    let timed: Vec<Vec<Timed>> = crews
        .par_iter()
        .map(|crew| -> Result<Vec<Timed>> {
            let arrs = arrangements(crew, watts, ps, spec.bow_rudder, &req.settings, keep);
            let weights: Vec<f64> = crew.iter().map(|&j| ps[j].weight_lb).collect();
            arrs.into_iter()
                .map(|arr| {
                    ensure!(arr.eff > 0.0, "synergy losses leave no power");
                    let (t, turn) =
                        race::crew_time(top, c.hull_weight_lb, rp, plan, i, &weights, arr.eff / n as f64)?;
                    let steer_s = (STEER_MAX - arr.steer_rating) as f64 * req.settings.steer_penalty * turn;
                    Ok(Timed { arr, time: t + steer_s, steer_s })
                })
                .collect()
        })
        .collect::<Result<_>>()?;
    let mut all: Vec<Timed> = timed.into_iter().flatten().collect();
    a.crews_timed = crews.len();
    if all.is_empty() {
        bail!(
            "no crew has a steerer rated 1 or more for the {}",
            if spec.bow_rudder { "stern or bow" } else { "stern" }
        );
    }
    all.sort_by(|x, y| x.time.total_cmp(&y.time).then(x.arr.order.cmp(&y.arr.order)));
    // Fastest first; the first safe one, then any safe one within TIE_S with a lower D/C.
    let mut best: Option<(&Timed, Vec<f64>, seating::Assessment)> = None;
    let mut last_err = None;
    for t in &all {
        if let Some((b, _, _)) = &best
            && t.time > b.time + TIE_S
        {
            break;
        }
        a.crews_checked += 1;
        match seat_crew(c, ps, &t.arr.order, req.rules) {
            Ok((knees, s)) if s.safe => {
                if best.as_ref().is_none_or(|(_, _, b)| s.dc < b.dc) {
                    best = Some((t, knees, s));
                }
            }
            Ok((_, s)) => last_err = Some(s.reasons.join(", ")),
            Err(e) => last_err = Some(format!("{e:#}")),
        }
    }
    let Some((t, knees, s)) = best else {
        bail!(
            "no safe crew: every eligible crew breaks a check on this canoe{}",
            last_err.map_or(String::new(), |e| format!(" (last: {e})"))
        );
    };
    let arr = &t.arr;
    a.crew = arr
        .order
        .iter()
        .enumerate()
        .map(|(seat, &j)| {
            let side = side_at(seat);
            Member {
                paddler: j,
                name: ps[j].name.clone(),
                weight_lb: ps[j].weight_lb,
                seat: seat + 1,
                side,
                knee_in: knees[seat],
                watts: watts[j],
                estimated: if sprint { ps[j].watts_1min.is_none() } else { ps[j].watts_4min.is_none() },
                steerer: seat == arr.steer_seat,
            }
        })
        .collect();
    a.time_s = Some(t.time);
    a.raw_power_w = Some(arr.raw);
    a.tracking_loss_w = Some(arr.track);
    a.pace_loss_w = Some(arr.pace);
    a.effective_power_w = Some(arr.eff);
    a.steer_penalty_s = Some(t.steer_s);
    a.dc = Some(s.dc);
    a.freeboard_in = Some(s.freeboard_in);
    a.gm_in = Some(s.gm_in);
    if arr.steer_rating < STEER_MAX {
        a.notes.push(format!("steerer rated {} of 3", arr.steer_rating));
    }
    Ok(())
}

/// Assign a crew to every event in `req.events` on the canoe `c` with `rp`'s courses.
/// `Roster::weighted_time_s` of (time, event weight) per event: the events that got a crew.
/// Also joins the phone app's per-worker parts (`serve::roster_join`).
pub fn weighted_time(events: impl Iterator<Item = (Option<f64>, f64)>) -> f64 {
    events.filter_map(|(t, w)| t.map(|t| t * w)).sum()
}

pub fn assign(c: &Canoe, rp: &RaceParams, req: &Request) -> Result<Roster> {
    validate(req, rp)?;
    let fallback = rp.paddler_power_w;
    let est = (
        estimate(&req.paddlers, |p| p.watts_1min, fallback),
        estimate(&req.paddlers, |p| p.watts_4min, fallback),
    );
    let top = race::topsides(&c.hull, c.depth_in, c.topsides);
    let plan = race::plan(&top, c.hull_weight_lb, rp)?;
    let assignments: Vec<Assignment> = req
        .events
        .iter()
        .map(|spec| {
            let i = rp.events.iter().position(|e| e.name == spec.name).expect("validated");
            assign_event(c, &top, rp, &plan, i, spec, &req.paddlers, est, req)
        })
        .collect();
    let weighted_time_s = weighted_time(assignments.iter().map(|a| (a.time_s, a.weight)));
    Ok(Roster { assignments, weighted_time_s, estimate_1min: est.0, estimate_4min: est.1 })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gender_mix_from_names() {
        assert_eq!(mix_of("2M, 2F"), Some((2, 2)));
        assert_eq!(mix_of("1M, 1F"), Some((1, 1)));
        assert_eq!(mix_of("2M"), Some((2, 0)));
        assert_eq!(mix_of("3F"), Some((0, 3)));
        assert_eq!(mix_of("2M,1W"), Some((2, 1)));
        assert_eq!(mix_of("4_coed"), None);
        assert_eq!(mix_of("Symposium co-ed 200 m slalom"), None, "200 m is a distance");
        assert_eq!(mix_of("Finals men's 200m sprint"), None);
        assert_eq!(mix_of("2Mx"), None);
        let ev = |name: &str, n: usize, lc: Option<&str>| crate::params::RaceEvent {
            name: name.into(),
            course: "c".into(),
            weights_lb: vec![150.0; n],
            load_case: lc.map(String::from),
            weight: 1.0,
            paddler_power_w: None,
        };
        assert_eq!(event_mix(&ev("Symposium co-ed 200 m slalom", 2, None)), Some((1, 1)));
        assert_eq!(event_mix(&ev("Finals co-ed 600 m endurance", 3, None)), Some((2, 1)));
        assert_eq!(event_mix(&ev("Symposium women's 200 m sprint", 2, None)), Some((0, 2)));
        assert_eq!(event_mix(&ev("Symposium men's 200 m sprint", 2, None)), Some((2, 0)));
        assert_eq!(event_mix(&ev("Heat 3", 4, Some("2M, 2F"))), Some((2, 2)));
        assert_eq!(event_mix(&ev("Heat 3", 4, None)), None);
    }

    #[test]
    fn subsets_and_permutations_are_complete() {
        assert_eq!(subsets(&[1, 2, 3, 4], 2).len(), 6);
        assert_eq!(subsets(&[1, 2, 3], 0), vec![Vec::<usize>::new()]);
        assert!(subsets(&[1], 2).is_empty());
        assert_eq!(permutations(&[1, 2, 3]).len(), 6);
    }

    #[test]
    fn mass_estimate_fits_each_gender_then_falls_back() {
        let p = |g: Gender, w: f64, w1: Option<f64>| Paddler {
            name: String::new(),
            gender: g,
            weight_lb: w,
            height_in: None,
            watts_1min: w1,
            watts_4min: None,
            steer_stern: 0,
            steer_bow: 0,
        };
        let ps = vec![p(Gender::M, 180.0, Some(300.0)), p(Gender::M, 160.0, None), p(Gender::F, 140.0, None)];
        let e = estimate(&ps, |q| q.watts_1min, 100.0);
        assert_eq!((e.men.from, e.men.tests), ("gender", 1));
        assert!((e.men.c * 180f64.powf(2.0 / 3.0) - 300.0).abs() < 1e-9, "one test: exact through it");
        assert_eq!(e.women.from, "everyone");
        let none = estimate(&ps, |q| q.watts_4min, 100.0);
        assert_eq!(none.women.from, "race power");
        let mean = (180.0f64 + 160.0 + 140.0) / 3.0;
        assert!((none.men.c * mean.powf(2.0 / 3.0) - 100.0).abs() < 1e-9);
    }
}
