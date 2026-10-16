// The canoe surface exactly as the web UI draws it: a TypeScript port of web/index.html loftOf(),
// hbAt(), stemLevels(), girthAt() and resampleLine() (themselves ports of hull.rs Loft /
// build_flared / Grid::cut_stem). Only the line-arc-line gunwale is left out: none of the promo
// runs uses one (gw is always null here, as in the web UI for these results).

export type Hull = {
  name: string;
  length: number;
  draft_design: number;
  x: number[];
  z: number[];
  half_breadth: number[][];
  /** Stem radius arcs (hull.rs prepared hull); radius and per end [stem x, arc top, x where the arc meets the keel]. */
  stem: { radius_in: number; bow: [number, number, number] | null; stern: [number, number, number] | null } | null;
};

type Loft = {
  breadth: (x: number, z: number) => number;
  keelAt: (x: number) => number | null;
  tableKeel: (x: number) => number | null;
};

function interp(xs: number[], ys: number[], x: number): number {
  if (x <= xs[0]) return ys[0];
  if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
  let i = 1;
  while (xs[i] < x) i++;
  const t = (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
  return ys[i - 1] + t * (ys[i] - ys[i - 1]);
}

const lofts = new WeakMap<Hull, Loft>();

export function loftOf(h: Hull): Loft {
  const cached = lofts.get(h);
  if (cached) return cached;
  const z = h.z;
  const nz = z.length;
  const colKeel = (row: number[]): number | null => {
    const f = row.findIndex((v) => v > 1e-9);
    if (f < 0) return null;
    if (f === 0) return z[0];
    let k = z[f - 1];
    if (f + 1 < nz && row[f + 1] > row[f] + 1e-9) {
      const a = row[f] ** 2;
      const b = row[f + 1] ** 2;
      k = (z[f] * b - z[f + 1] * a) / (b - a);
    }
    return Math.min(Math.max(k, z[f - 1]), z[f]);
  };
  const keel = h.half_breadth.map(colKeel);
  const first = h.half_breadth.map((row) => row.findIndex((v) => v > 1e-9));
  const kx: number[] = [];
  const kz: number[] = [];
  keel.forEach((k, j) => {
    if (k !== null) {
      kx.push(h.x[j]);
      kz.push(k);
    }
  });
  const n = kx.length;
  const m = new Array<number>(n).fill(0);
  if (n > 1) {
    const hh: number[] = [];
    const d: number[] = [];
    for (let i = 0; i < n - 1; i++) {
      hh.push(kx[i + 1] - kx[i]);
      d.push((kz[i + 1] - kz[i]) / hh[i]);
    }
    m[0] = d[0];
    m[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++)
      if (d[i - 1] * d[i] > 0) {
        const w1 = 2 * hh[i] + hh[i - 1];
        const w2 = hh[i] + 2 * hh[i - 1];
        m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
      }
  }
  const clampZ = (v: number) => Math.min(Math.max(v, z[0]), z[nz - 1]);
  const tableKeel = (x: number): number | null => {
    if (!n) return null;
    if (n === 1) return kz[0];
    if (x < kx[0]) return clampZ(kz[0] + m[0] * (x - kx[0]));
    if (x > kx[n - 1]) return clampZ(kz[n - 1] + m[n - 1] * (x - kx[n - 1]));
    let i = 0;
    while (i < n - 2 && kx[i + 1] <= x) i++;
    const hi = kx[i + 1] - kx[i];
    const t = (x - kx[i]) / hi;
    const t2 = t * t;
    const t3 = t2 * t;
    return clampZ(kz[i] * (2 * t3 - 3 * t2 + 1) + hi * m[i] * (t3 - 2 * t2 + t) + kz[i + 1] * (3 * t2 - 2 * t3) + hi * m[i + 1] * (t3 - t2));
  };
  const station = (j: number, zq: number): number => {
    const k = keel[j];
    if (k === null) return 0;
    const row = h.half_breadth[j];
    const f = first[j];
    if (f > 0) {
      if (zq <= k) return 0;
      if (zq < z[f]) return z[f] > k ? (row[f] * (zq - k)) / (z[f] - k) : row[f];
    }
    return interp(z, row, zq);
  };
  const top = z[nz - 1];
  const xs = h.x;
  const nx = xs.length;
  const breadth = (x: number, zq: number): number => {
    const k = tableKeel(x);
    if (k === null || zq < k) return 0;
    let j = 1;
    while (j < nx - 1 && xs[j] <= x) j++;
    const t = Math.min(1, Math.max(0, (x - xs[j - 1]) / (xs[j] - xs[j - 1])));
    const ka = tableKeel(xs[j - 1]) as number;
    const kb = tableKeel(xs[j]) as number;
    let za = zq;
    let zb = zq;
    if (top - k > 1e-9) {
      const s = Math.min(1, Math.max(0, (zq - k) / (top - k)));
      za = ka + s * (top - ka);
      zb = kb + s * (top - kb);
    }
    const ya = station(j - 1, za);
    const yb = station(j, zb);
    return t <= 0 ? ya : t >= 1 ? yb : ya * (1 - t) + yb * t;
  };
  const keelAt = (x: number): number | null => {
    const k = tableKeel(x);
    if (k === null || !h.stem) return k;
    const st = h.stem;
    const xa = xs[0];
    const xb = xs[nx - 1];
    const bow = x - xa <= xb - x;
    const end = bow ? st.bow : st.stern;
    const d = bow ? x - xa : xb - x;
    const r = st.radius_in;
    if (end && d >= end[0] - 1e-12 && d <= end[2]) {
      const u = d - end[0] - r;
      return Math.max(k, end[1] - Math.sqrt(Math.max(r * r - u * u, 0)));
    }
    return k;
  };
  const L = { breadth, keelAt, tableKeel };
  lofts.set(h, L);
  return L;
}

/** Half-breadth at (x, z): the loft up to the table top, then flared topsides to the mold depth; stem arcs cut the forefoot. */
export function hbAt(h: Hull, x: number, z: number, flare: number, depth: number): number {
  const L = loftOf(h);
  const dd = h.draft_design;
  const uncut = (xx: number, zz: number) => {
    if (zz <= dd) return L.breadth(xx, zz);
    const t = L.breadth(xx, dd);
    return t > 0 ? t + (zz - dd) * Math.tan((flare * Math.PI) / 180) : t;
  };
  if (h.stem) {
    const k = L.tableKeel(x);
    const kc = L.keelAt(x) as number;
    if (k !== null && kc > k + 1e-12) return z < kc || depth - kc <= 1e-9 ? 0 : uncut(x, k + ((z - kc) * (depth - k)) / (depth - kc));
  }
  return uncut(x, z);
}

/** Extra heights every r/8 over the stem arcs, so they draw round (zs sorted in place). */
export function stemLevels(h: Hull, zs: number[], depth: number): void {
  const st = h.stem;
  if (!st) return;
  const r = st.radius_in;
  const ends = [st.bow, st.stern].filter(Boolean) as Array<[number, number, number]>;
  const lo = Math.max(0, Math.min(...ends.map((e) => e[1] - Math.sqrt(Math.max(r * r - (e[2] - e[0] - r) ** 2, 0)))));
  const top = Math.min(Math.max(...ends.map((e) => e[1])), depth);
  for (let z = lo; z < top; z += r / 8) zs.push(z);
  zs.push(top);
  zs.sort((a, b) => a - b);
  for (let k = zs.length - 1; k > 0; k--) if (zs[k] - zs[k - 1] < 1e-3) zs.splice(k, 1);
}

/** Starboard half girth at x, sheer to keel centreline: [half-breadth, height] points. */
export function girthAt(h: Hull, x: number, zs: number[], flare: number, depth: number): Array<[number, number]> | null {
  const k = loftOf(h).keelAt(x);
  if (k === null) return null;
  const pts: Array<[number, number]> = [];
  for (let q = zs.length - 1; q >= 0; q--) if (zs[q] > k + 1e-6) pts.push([hbAt(h, x, zs[q], flare, depth), zs[q]]);
  pts.push([0, Math.min(k, zs[zs.length - 1])]);
  return pts;
}

/** A polyline resampled to n points evenly by arc length (ends kept). */
export function resampleLine(p: Array<[number, number]>, n: number): Array<[number, number]> {
  const c = [0];
  for (let i = 1; i < p.length; i++) c.push(c[i - 1] + Math.hypot(p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]));
  const L = c[c.length - 1];
  if (!(L > 0)) return Array.from({ length: n }, () => [p[0][0], p[0][1]] as [number, number]);
  const out: Array<[number, number]> = [];
  let j = 1;
  for (let i = 0; i < n; i++) {
    const s = (L * i) / (n - 1);
    while (j < p.length - 1 && c[j] < s) j++;
    const t = c[j] > c[j - 1] ? (s - c[j - 1]) / (c[j] - c[j - 1]) : 0;
    out.push([p[j - 1][0] + t * (p[j][0] - p[j - 1][0]), p[j - 1][1] + t * (p[j][1] - p[j - 1][1])]);
  }
  return out;
}

/**
 * The surface as N station rings x NG girth points (starboard half), on a FIXED parameterisation
 * (station fraction along the hull, girth fraction sheer to keel), so two hulls can be blended
 * vertex for vertex. Stations are closer toward the ends (where the stem arcs are). Ring k point g
 * is at x = xs[k], half-breadth = ring[(k * NG + g) * 2], height = ring[(k * NG + g) * 2 + 1].
 * Rings past the hull (no breadth) are the stem line from the gunwale to the keel, as the web UI.
 */
export type Rings = { N: number; NG: number; xs: Float64Array; ring: Float64Array; depth: number; length: number };

export function hullRings(h: Hull, depth: number, flare: number, N = 161, NG = 25): Rings {
  const nx = h.x.length;
  const x0 = h.x[0];
  const x1 = h.x[nx - 1];
  const zs = h.z.slice();
  const nTop = 4;
  for (let k = 1; k <= nTop; k++) zs.push(h.draft_design + ((depth - h.draft_design) * k) / nTop);
  stemLevels(h, zs, depth);
  const lev = zs.filter((z, q) => q === 0 || z > zs[q - 1]);
  const xs = new Float64Array(N);
  const ring = new Float64Array(N * NG * 2);
  for (let i = 0; i < N; i++) {
    const u = i / (N - 1);
    const f = 0.6 * u + 0.4 * (0.5 - 0.5 * Math.cos(Math.PI * u));
    const x = x0 + (x1 - x0) * f;
    xs[i] = x;
    const has = lev.some((z) => hbAt(h, x, z, flare, depth) > 1e-9);
    const p = girthAt(h, x, lev, flare, depth);
    const line: Array<[number, number]> = p
      ? has
        ? p
        : [
            [0, depth],
            [0, p[p.length - 1][1]],
          ]
      : [
          [0, depth],
          [0, depth],
        ];
    const rs = resampleLine(line, NG);
    for (let g = 0; g < NG; g++) {
      ring[(i * NG + g) * 2] = rs[g][0];
      ring[(i * NG + g) * 2 + 1] = rs[g][1];
    }
  }
  return { N, NG, xs, ring, depth, length: x1 - x0 };
}

/** Blend two ring sets (same N, NG) at t in [0, 1]. Interpolation between two real hulls. */
export function blendRings(a: Rings, b: Rings, t: number): Rings {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const xs = new Float64Array(a.N);
  const ring = new Float64Array(a.ring.length);
  for (let i = 0; i < a.N; i++) xs[i] = a.xs[i] + (b.xs[i] - a.xs[i]) * t;
  for (let i = 0; i < ring.length; i++) ring[i] = a.ring[i] + (b.ring[i] - a.ring[i]) * t;
  return { N: a.N, NG: a.NG, xs, ring, depth: a.depth + (b.depth - a.depth) * t, length: a.length + (b.length - a.length) * t };
}
