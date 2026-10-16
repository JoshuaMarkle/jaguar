// Race playback state, ported from web/index.html raceState() / canoeOutline(): where the canoe
// is on its planned path at race time t, how fast, which way it points (with the turn model's
// drift in arcs) and which segment it is on. Pure functions of JAGUAR's race.json event.
import type { RaceEvent, RaceSegment } from './data';
import type { Hull } from './hull/loft';

export type RaceState = {
  t: number;
  s: number;
  v: number;
  pos: [number, number];
  /** Heading, rad, from +x (counter-clockwise, +y = port). */
  hd: number;
  seg: RaceSegment | null;
};

const cums = new WeakMap<RaceEvent, number[]>();
function cumOf(e: RaceEvent): number[] {
  let c = cums.get(e);
  if (c) return c;
  let acc = 0;
  c = e.path_m.map((p, k) => (acc += k ? Math.hypot(p[0] - e.path_m[k - 1][0], p[1] - e.path_m[k - 1][1]) : 0));
  cums.set(e, c);
  return c;
}

function bracket(rows: Array<[number, number, number]>, k: number, v: number): [number, number] {
  let lo = 0;
  let hi = rows.length - 1;
  if (v <= rows[0][k]) return [0, 0];
  if (v >= rows[hi][k]) return [hi - 1, 1];
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (rows[m][k] <= v) lo = m;
    else hi = m;
  }
  return [lo, (v - rows[lo][k]) / (rows[hi][k] - rows[lo][k] || 1)];
}

/** Point on the planned path at distance s (m along the race distance). */
export function posAt(e: RaceEvent, s: number): [number, number] {
  const cum = cumOf(e);
  const sc = (Math.max(0, Math.min(e.distance_m, s)) / e.distance_m) * cum[cum.length - 1];
  let k = 1;
  while (k < cum.length - 1 && cum[k] < sc) k++;
  const g = (sc - cum[k - 1]) / (cum[k] - cum[k - 1] || 1);
  const P = e.path_m;
  return [P[k - 1][0] + g * (P[k][0] - P[k - 1][0]), P[k - 1][1] + g * (P[k][1] - P[k - 1][1])];
}

/** Half-length of the chord the heading is taken over, m: smooths the 1 m path polyline. */
const HEADING_CHORD = 1.6;
/** Distance over which the turn drift eases in and out at each end of an arc, m. */
const DRIFT_EASE = 3;

export function raceState(e: RaceEvent, tIn: number): RaceState {
  const t = Math.max(0, Math.min(e.time_s, tIn));
  const sp = e.speed_m;
  const [i, f] = bracket(sp, 2, t);
  const j = Math.min(i + 1, sp.length - 1);
  const s = sp[i][0] + f * (sp[j][0] - sp[i][0]);
  const v = sp[i][1] + f * (sp[j][1] - sp[i][1]);
  const pos = posAt(e, s);
  // Heading from a centred chord, so it turns continuously instead of stepping point to point.
  const a = posAt(e, s - HEADING_CHORD);
  const b = posAt(e, s + HEADING_CHORD);
  let hd = Math.atan2(b[1] - a[1], b[0] - a[0]);
  let at = 0;
  let seg: RaceSegment | null = null;
  let segAt = 0;
  for (const sg of e.segments) {
    if (s <= at + sg.length_m + 1e-9) {
      seg = sg;
      segAt = at;
      break;
    }
    at += sg.length_m;
  }
  if (seg && seg.radius_m != null && seg.drift_deg != null && seg.mark != null) {
    // In a steady turn the bow points into the turn by the drift angle (web UI), eased in and
    // out over the ends of the arc so the canoe yaws smoothly into and out of the skid.
    const into = s - segAt;
    const left = segAt + seg.length_m - s;
    const r = Math.max(0, Math.min(1, Math.min(into, left) / DRIFT_EASE));
    const ease = r * r * (3 - 2 * r);
    const turnLeft = e.course.marks[seg.mark].keep === 'port';
    hd += ((turnLeft ? 1 : -1) * Math.abs(seg.drift_deg) * ease * Math.PI) / 180;
  }
  return { t, s, v, pos, hd, seg };
}

/** Distance along the path where each turn starts and ends, m. */
export function turnSpans(e: RaceEvent): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  let at = 0;
  for (const sg of e.segments) {
    if (sg.radius_m != null) out.push([at, at + sg.length_m]);
    at += sg.length_m;
  }
  return out;
}

/** Winner's top-view outline (widest offset per station), metres, bow at +u, centred on midships. */
export function canoeOutline(h: Hull): Array<[number, number]> {
  const L = h.x[h.x.length - 1] - h.x[0];
  const m = 0.0254;
  const half = h.half_breadth.map((r) => Math.max(...r));
  const pts: Array<[number, number]> = h.x.map((x, i) => [(L / 2 - (x - h.x[0])) * m, half[i] * m]);
  return pts.concat(
    pts
      .slice()
      .reverse()
      .map(([u, v]) => [u, -v] as [number, number]),
  );
}
