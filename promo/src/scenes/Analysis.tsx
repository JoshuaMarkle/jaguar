import React from 'react';
import { useCurrentFrame } from 'remotion';
import { data } from '../data';
import { report, SHORT } from '../report';
import { beat, sceneById } from '../storyboard';
import { chart, color, font, hairline, motion, radius, space, type } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';
import { KineticHeadline } from '../components/KineticHeadline';
import { ChartPanel, type Dot, type Series } from '../components/ChartPanel';
import { At, Stage, useLayout } from './common';

/**
 * Analyze: a fast, accelerating montage of the analysis views (30 frames down to 6), every view
 * at once in a 4 x 4 wall, then a dive into the race-time terrain. Along-hull views use the off1
 * winner's 2M load case; the search-space views use the explorer report's own run
 * (promo/data/search_space_report.html). Hard cuts; each chart draws in over most of its cut.
 */

type P = { f: number; d: number; w: number; h: number };
const HUGE = 1e4;

/** Draw-in of a cut of d frames: most of the cut, ease-out. */
const drawOf = (f: number, d: number) => progress(f, 0, Math.max(4, Math.round(d * 0.7)));
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const pad = { l: 176, r: space.xl, t: space.xl, b: 104 };

// ---------------------------------------------------------------- along the hull (off1, 2M)

const K = data.off1.cases[0];
const xs: [number, number] = [K.x_in[0], K.x_in[K.x_in.length - 1]];
const crew = `${K.name}: ${K.weights_lb.map((v) => v.toFixed(0)).join(' + ')} lb crew`;
const knees = (): Series[] => K.seats_in.map((x) => ({ kind: 'line' as const, x: [x, x], y: [-1e6, 1e6], color: color.faint, dash: [4, 8], width: 2 }));
const zero = (): Series => ({ kind: 'line', x: xs, y: [0, 0], color: color.hairline, width: 2 });
const xAxis = { domain: xs, label: 'x from bow, in' };

const Bending: React.FC<P> = ({ f, d, w, h }) => {
  const m = Math.max(...K.moment_lbin.map(Math.abs));
  const r = drawOf(f, d);
  return (
    <ChartPanel
      width={w}
      height={h}
      x={xAxis}
      y={{ domain: [-m * 0.25, m * 1.25], label: 'bending moment, lb·in', ticks: [0, 1000, 2000, 3000] }}
      series={[...knees(), zero(), { kind: 'area', x: K.x_in, y: K.moment_lbin, reveal: r }, { kind: 'line', x: K.x_in, y: K.moment_lbin, reveal: r }]}
      legend={[
        { color: color.ink, label: `${crew}, + sagging` },
        { color: color.faint, label: 'knees', dash: [4, 8] },
      ]}
      padding={pad}
    />
  );
};

const Shear: React.FC<P> = ({ f, d, w, h }) => {
  const m = Math.max(...K.shear_lb.map(Math.abs));
  const r = drawOf(f, d);
  return (
    <ChartPanel
      width={w}
      height={h}
      x={xAxis}
      y={{ domain: [-m * 1.3, m * 1.3], label: 'shear, lb', ticks: [-100, 0, 100] }}
      series={[...knees(), zero(), { kind: 'area', x: K.x_in, y: K.shear_lb, reveal: r }, { kind: 'line', x: K.x_in, y: K.shear_lb, reveal: r }]}
      legend={[
        { color: color.ink, label: crew },
        { color: color.faint, label: 'knees', dash: [4, 8] },
      ]}
      padding={pad}
    />
  );
};

const Stress: React.FC<P> = ({ f, d, w, h }) => {
  const r = drawOf(f, d);
  const allow = K.tension_allow_psi;
  return (
    <ChartPanel
      width={w}
      height={h}
      x={xAxis}
      y={{ domain: [0, allow * 1.2], label: 'stress, psi', ticks: [0, 50, 100, 150, 200] }}
      series={[
        ...knees(),
        { kind: 'area', x: K.x_in, y: K.tension_psi, reveal: r },
        { kind: 'line', x: K.x_in, y: K.tension_psi, reveal: r },
        { kind: 'line', x: K.x_in, y: K.shear_psi, color: color.muted, dash: [10, 8], reveal: r },
      ]}
      gates={[{ y: allow }]}
      legend={[
        { color: color.ink, label: 'bending tension' },
        { color: color.muted, label: 'shear', dash: [10, 8] },
        { color: color.error, label: `allowable tension ${allow.toFixed(0)} psi`, dash: [...chart.gateDash] },
      ]}
      padding={pad}
    />
  );
};

const DemandCapacity: React.FC<P> = ({ f, d, w, h }) => {
  const r = drawOf(f, d);
  return (
    <ChartPanel
      width={w}
      height={h}
      x={xAxis}
      y={{ domain: [0, 1.2], label: 'D/C (demand / capacity)', ticks: [0, 0.5, 1] }}
      series={[
        ...knees(),
        { kind: 'line', x: K.x_in, y: K.dc_bending_tension, reveal: r },
        { kind: 'line', x: K.x_in, y: K.dc_shear, color: color.muted, dash: [10, 8], reveal: r },
        { kind: 'line', x: K.x_in, y: K.dc_bending_compression, color: color.faint, reveal: r },
      ]}
      gates={[{ y: 1 }]}
      legend={[
        { color: color.ink, label: 'bending tension' },
        { color: color.muted, label: 'shear', dash: [10, 8] },
        { color: color.faint, label: 'bending compression' },
        { color: color.error, label: 'gate 1.0', dash: [...chart.gateDash] },
      ]}
      padding={pad}
    />
  );
};

const Material: React.FC<P> = ({ f, d, w, h }) => {
  const mx = data.mix;
  const fit = (r: number) => mx.split_slope_psi_per_pcf * r + mx.split_intercept_psi;
  const md: [number, number] = [50, 70];
  return (
    <ChartPanel
      width={w}
      height={h}
      x={{ domain: md, label: 'density, pcf' }}
      y={{ domain: [100, 400], label: 'split tensile strength, psi', ticks: [100, 200, 300, 400] }}
      series={[
        { kind: 'band', x0: mx.density_min_pcf, x1: mx.density_max_pcf, fill: color.ink, opacity: 0.04 },
        { kind: 'line', x: md, y: md.map(fit), reveal: drawOf(f, d) },
        { kind: 'scatter', dots: mx.points.map((p) => ({ x: p.density_pcf, y: p.final_psi, state: 'point' as const })), count: drawOf(f, d) * mx.points.length, radius: chart.dot * 1.8 },
      ]}
      legend={[
        { color: color.ink, label: `${mx.points.length} lab breaks`, dot: true },
        { color: color.ink, label: `fit: ${mx.split_slope_psi_per_pcf.toFixed(2)} × density − ${Math.abs(mx.split_intercept_psi).toFixed(0)} psi` },
      ]}
      padding={pad}
    />
  );
};

const Freeboard: React.FC<P> = ({ f, d, w, h }) => {
  const k = data.shapeOpt.cases[0];
  const depth = data.shapeOpt.winner.depth_in;
  const emp = data.shapeOpt.empty;
  const r = drawOf(f, d);
  const gate = data.shapeOpt.gates.freeboard_min_in;
  return (
    <ChartPanel
      width={w}
      height={h}
      x={{ domain: [k.x_in[0], k.x_in[k.x_in.length - 1]], label: 'x from bow, in' }}
      y={{ domain: [4, 12.5], label: 'freeboard, in', ticks: [4, 6, 8, 10, 12] }}
      series={[
        { kind: 'line', x: emp.x_in, y: emp.waterline_in.map((z) => depth - z), color: color.faint, reveal: r },
        { kind: 'line', x: k.x_in, y: k.waterline_in.map((z) => depth - z), reveal: r },
      ]}
      gates={[{ y: gate }]}
      legend={[
        { color: color.ink, label: `${k.name}: min ${k.freeboard_in.toFixed(2)} in` },
        { color: color.faint, label: `empty: min ${emp.freeboard_in.toFixed(2)} in` },
        { color: color.error, label: `gate ${gate.toFixed(0)} in`, dash: [...chart.gateDash] },
      ]}
      padding={pad}
    />
  );
};

// ---------------------------------------------------------------- search space report

const S = report.shapes;
const RACE = 7;
const VALID = 8;
const GM = 9;
const pIdx = (name: string) => report.names.indexOf(name);
const hasRace = S.filter((s) => s[RACE] != null);
const bestValid = hasRace.filter((s) => s[VALID]).reduce((a, b) => ((b[RACE] as number) < (a[RACE] as number) ? b : a));

const TradeOff: React.FC<P> = ({ f, d, w, h }) => {
  const pts = hasRace.filter((s) => s[GM] != null);
  const dots: Dot[] = [
    ...pts.filter((s) => !s[VALID]).map((s) => ({ x: s[GM] as number, y: s[RACE] as number, state: 'fail' as const })),
    ...pts.filter((s) => s[VALID]).map((s) => ({ x: s[GM] as number, y: s[RACE] as number, state: 'pass' as const })),
  ];
  return (
    <ChartPanel
      width={w}
      height={h}
      x={{ domain: [-4, 8], label: 'best reachable GM, in' }}
      y={{ domain: [100, 125], label: 'race time, s', ticks: [100, 110, 120] }}
      series={[{ kind: 'scatter', dots, count: dots.length * drawOf(f, d), radius: chart.dot }]}
      gates={[{ x: 3 }]}
      winner={{ x: bestValid[GM] as number, y: bestValid[RACE] as number, visible: progress(f, d * 0.5, 4) }}
      legend={[
        { color: color.success, label: 'valid', dot: true },
        { color: color.candidateFail, label: 'invalid', dot: true },
        { color: color.error, label: 'GM gate 3 in', dash: [...chart.gateDash] },
      ]}
      padding={pad}
    />
  );
};

/** Orthographic camera: azimuth about z, then tilted down by el. Larger d is farther away. */
type V3 = [number, number, number];
const camera = (az: number, el: number) => (p: V3) => {
  const ca = Math.cos(az);
  const sa = Math.sin(az);
  const X = p[0] * ca - p[1] * sa;
  const Y = p[0] * sa + p[1] * ca;
  const ce = Math.cos(el);
  const se = Math.sin(el);
  return { x: X, y: -(p[2] * ce + Y * se), d: Y * ce - p[2] * se };
};
const norm = (v: number, lo: number, hi: number) => ((v - lo) / (hi - lo)) * 2 - 1;

const Cloud: React.FC<P> = ({ f, d, w, h }) => {
  const az = -0.75 + f * 0.012;
  const cam = camera(az, 0.42);
  const s = h * 0.3;
  const cx = w / 2;
  const cy = h / 2 + space.lg;
  const P = (p: V3) => {
    const q = cam(p);
    return { x: cx + q.x * s, y: cy + q.y * s, d: q.d };
  };
  const b = pIdx('beam_in');
  const wp = pIdx('fore_wp');
  const tp = pIdx('fore_len_frac');
  const at = (sh: (typeof S)[number]): V3 => [norm(sh[b] as number, report.lo[b], report.hi[b]), norm(sh[wp] as number, report.lo[wp], report.hi[wp]), norm(sh[tp] as number, report.lo[tp], report.hi[tp])];
  const inv = S.filter((sh, i) => !sh[VALID] && i % 2 === 0);
  const val = S.filter((sh) => sh[VALID]);
  const shown = drawOf(f, d);
  const corners: V3[] = [-1, 1].flatMap((x) => [-1, 1].flatMap((y) => [-1, 1].map((z) => [x, y, z] as V3)));
  const edges = corners.flatMap((a, i) => corners.slice(i + 1).filter((c) => (a[0] !== c[0] ? 1 : 0) + (a[1] !== c[1] ? 1 : 0) + (a[2] !== c[2] ? 1 : 0) === 1).map((c) => [a, c] as [V3, V3]));
  const lbl = (p: V3, t: string, anchor: 'start' | 'middle' | 'end', dy = 0) => {
    const q = P(p);
    return (
      <text x={q.x} y={q.y + dy} textAnchor={anchor} fontSize={type.caption.size} fill={chart.tick}>
        {t}
      </text>
    );
  };
  return (
    <svg width={w} height={h} style={{ display: 'block', overflow: 'visible', fontFamily: font.sans }}>
      {edges.map(([a, c], i) => {
        const p = P(a);
        const q = P(c);
        return <line key={i} x1={p.x} y1={p.y} x2={q.x} y2={q.y} stroke={chart.frame} strokeWidth={hairline * 2} />;
      })}
      {inv.slice(0, Math.ceil(inv.length * shown)).map((sh, i) => {
        const q = P(at(sh));
        return <circle key={`i${i}`} cx={q.x} cy={q.y} r={3} fill={color.candidateFail} opacity={0.7} />;
      })}
      {val.slice(0, Math.ceil(val.length * shown)).map((sh, i) => {
        const q = P(at(sh));
        return <circle key={`v${i}`} cx={q.x} cy={q.y} r={chart.dot + 0.5} fill={color.success} />;
      })}
      {lbl([0, -1.25, -1], 'beam, in', 'middle', type.caption.size)}
      {lbl([1.2, 0, -1], 'bow fineness', 'start', type.caption.size)}
      {lbl([-1.12, -1.12, 0], 'taper', 'end')}
      <g transform={`translate(${space.lg}, ${space.xl})`}>
        <circle cx={18} cy={-9} r={chart.dot + 1} fill={color.success} />
        <text x={44} y={0} fontSize={type.caption.size} fill={color.inkSoft}>{`valid (${report.counts.valid})`}</text>
        <circle cx={278} cy={-9} r={chart.dot + 1} fill={color.candidateFail} />
        <text x={304} y={0} fontSize={type.caption.size} fill={color.inkSoft}>{`invalid, ${report.counts.n.toLocaleString('en-US')} shapes`}</text>
      </g>
    </svg>
  );
};

/** Small multiples, 4 x 2: one panel per free parameter, the 8th cell a note. */
const Multiples: React.FC<{ w: number; h: number; panel: (k: number, cw: number, ch: number) => React.ReactNode; note: string }> = ({ w, h, panel, note }) => {
  const gap = space.md;
  const cw = (w - 3 * gap) / 4;
  const ch = (h - gap) / 2;
  return (
    <div style={{ position: 'relative', width: w, height: h }}>
      {report.names.map((_, k) => (
        <div key={k} style={{ position: 'absolute', left: (k % 4) * (cw + gap), top: Math.floor(k / 4) * (ch + gap) }}>
          {panel(k, cw, ch)}
        </div>
      ))}
      <div style={{ position: 'absolute', left: 3 * (cw + gap) + 96, top: ch + gap + space.xl, width: cw - 96, fontSize: type.body.size, lineHeight: type.body.lineHeight, color: color.muted }}>
        {note}
      </div>
    </div>
  );
};
const smallPad = { l: 96, r: space.md, t: space.md, b: 80 };

const Trends: React.FC<P> = ({ f, d, w, h }) => {
  const shown = drawOf(f, d);
  return (
    <Multiples
      w={w}
      h={h}
      note={`${report.counts.n.toLocaleString('en-US')} random shapes. Line: median of the valid ones.`}
      panel={(k, cw, ch) => {
        const t = report.trends[k];
        const dots: Dot[] = [
          ...hasRace.filter((s, i) => !s[VALID] && i % 2 === 0).map((s) => ({ x: s[k] as number, y: s[RACE] as number, state: 'fail' as const })),
          ...hasRace.filter((s) => s[VALID]).map((s) => ({ x: s[k] as number, y: s[RACE] as number, state: 'pass' as const })),
        ];
        return (
          <ChartPanel
            width={cw}
            height={ch}
            x={{ domain: [t.lo, t.hi], label: SHORT[t.name], ticks: [t.lo, t.hi] }}
            y={{ domain: [100, 125], label: k % 4 === 0 ? 'race, s' : '', ticks: [100, 125] }}
            series={[
              { kind: 'scatter', dots, count: dots.length * shown, radius: 2.5 },
              { kind: 'line', x: t.median.map((p) => p[0]), y: t.median.map((p) => p[1]), reveal: shown, width: 4 },
            ]}
            padding={smallPad}
          />
        );
      }}
    />
  );
};

const Slices: React.FC<P> = ({ f, d, w, h }) => {
  const sl = report.slices;
  const n = sl.steps;
  const rr = sl.race.filter((v): v is number => v != null);
  const yd: [number, number] = [Math.floor(Math.min(...rr)), Math.ceil(Math.max(...rr))];
  const shown = drawOf(f, d);
  return (
    <Multiples
      w={w}
      h={h}
      note="One parameter at a time, the rest held at the best shape."
      panel={(k, cw, ch) => {
        const dim = report.free[k];
        const ii = Array.from({ length: n }, (_, i) => k * n + i).filter((i) => sl.race[i] != null);
        const val = ii.filter((i) => sl.valid[i]);
        const dots: Dot[] = ii.map((i) => ({ x: sl.v[i][dim], y: sl.race[i] as number, state: sl.valid[i] ? ('pass' as const) : ('fail' as const) }));
        return (
          <ChartPanel
            width={cw}
            height={ch}
            x={{ domain: [report.lo[k], report.hi[k]], label: SHORT[report.names[k]], ticks: [report.lo[k], report.hi[k]] }}
            y={{ domain: yd, label: k % 4 === 0 ? 'race, s' : '', ticks: yd }}
            series={[
              { kind: 'line', x: [sl.center[dim], sl.center[dim]], y: [-1e6, 1e6], color: color.faint, dash: [4, 8], width: 2 },
              { kind: 'line', x: val.map((i) => sl.v[i][dim]), y: val.map((i) => sl.race[i] as number), color: color.success, width: 3, reveal: shown },
              { kind: 'scatter', dots, count: dots.length * shown, radius: 3.5 },
            ]}
            padding={smallPad}
          />
        );
      }}
    />
  );
};

/** Horizontal bars with labels on the left, in the chart frame's language. */
const HBars: React.FC<{ w: number; h: number; items: Array<{ label: string; value: number; fill: string; text?: string }>; reveal: number; labelW: number }> = ({ w, h, items, reveal, labelW }) => {
  const top = pad.t;
  const plotW = w - labelW - 240;
  const rowH = Math.min(96, (h - pad.t - pad.b) / items.length);
  const max = Math.max(...items.map((it) => it.value));
  return (
    <svg width={w} height={h} style={{ display: 'block', overflow: 'visible', fontFamily: font.sans }}>
      <line x1={labelW} x2={labelW} y1={top} y2={top + rowH * items.length} stroke={chart.frame} strokeWidth={hairline * 2} />
      {items.map((it, i) => {
        const y = top + i * rowH;
        const bw = (plotW * it.value * reveal) / max;
        return (
          <g key={i}>
            <text x={labelW - space.md} y={y + rowH / 2 + type.body.size * 0.35} textAnchor="end" fontSize={type.body.size} fill={color.inkSoft}>
              {it.label}
            </text>
            <rect x={labelW} y={y + rowH * 0.22} width={Math.max(0, bw)} height={rowH * 0.56} rx={radius.xs} fill={it.fill} />
            {it.text ? (
              <text x={labelW + bw + space.md} y={y + rowH / 2 + type.caption.size * 0.35} fontSize={type.caption.size} fill={chart.tick} opacity={reveal} style={{ fontVariantNumeric: 'tabular-nums' }}>
                {it.text}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
};

const Importance: React.FC<P> = ({ f, d, w, h }) => (
  <HBars
    w={w}
    h={h}
    labelW={400}
    reveal={drawOf(f, d)}
    items={report.sens
      .slice()
      .sort((a, b) => b.drop - a.drop)
      .map((s, i) => ({ label: cap(SHORT[s.name]), value: s.drop, fill: i === 0 ? color.ink : color.faint }))}
  />
);

const Share: React.FC<P> = ({ f, d, w, h }) => {
  const tk = report.topk;
  const r = drawOf(f, d);
  return (
    <ChartPanel
      width={w}
      height={h}
      x={{ domain: [0.5, tk.length + 0.5], label: 'parameters, most telling first', ticks: tk.map((_, i) => i + 1), format: (v) => SHORT[tk[v - 1]?.name] ?? '' }}
      y={{ domain: [0, 1], label: 'race-time variance explained', ticks: [0, 0.5, 1], format: (v) => `${Math.round(v * 100)}%` }}
      series={[
        { kind: 'line', x: tk.map((_, i) => i + 1), y: tk.map((t) => t.share), reveal: r },
        { kind: 'scatter', dots: tk.map((t, i) => ({ x: i + 1, y: t.share, state: 'point' as const })), count: tk.length * r, radius: chart.dot * 1.6 },
      ]}
      padding={pad}
    />
  );
};

const Sweep: React.FC<P> = ({ f, d, w, h }) => {
  const g = report.gmsweep;
  const r = drawOf(f, d);
  const mine = g.find((s) => s.gate === 3);
  return (
    <ChartPanel
      width={w}
      height={h}
      x={{ domain: [1, 5.5], label: 'GM gate, in', ticks: g.map((s) => s.gate) }}
      y={{ domain: [101.5, 106], label: 'best race time, s', ticks: [102, 104, 106] }}
      series={[
        { kind: 'line', x: g.map((s) => s.gate), y: g.map((s) => s.race), reveal: r },
        { kind: 'scatter', dots: g.map((s) => ({ x: s.gate, y: s.race, state: 'point' as const })), count: g.length * r, radius: chart.dot * 1.6 },
      ]}
      winner={mine ? { x: mine.gate, y: mine.race, visible: progress(f, d * 0.4, 3) } : undefined}
      padding={pad}
    />
  );
};

const Volume: React.FC<P> = ({ f, d, w, h }) => {
  const g = report.gmsweep;
  const r = drawOf(f, d);
  return (
    <ChartPanel
      width={w}
      height={h}
      x={{ domain: [1, 5.5], label: 'GM gate, in', ticks: g.map((s) => s.gate) }}
      y={{ domain: [0, 20], label: 'share of the box valid, %', ticks: [0, 10, 20] }}
      series={g.map((s) => ({ kind: 'band' as const, x0: s.gate - 0.14, x1: s.gate + 0.14, y0: 0, y1: s.validFrac * 100 * r, fill: s.gate === 3 ? color.ink : color.faint }))}
      padding={pad}
    />
  );
};

const Failure: React.FC<P> = ({ f, d, w, h }) => (
  <HBars
    w={w}
    h={h}
    labelW={720}
    reveal={drawOf(f, d)}
    items={report.catcount.map((c) => ({
      label: cap(c.cat),
      value: c.count,
      fill: c.cat === 'valid' ? color.success : c.cat.startsWith('GM out') ? color.ink : color.faint,
      text: c.count.toLocaleString('en-US'),
    }))}
  />
);

// ---------------------------------------------------------------- the terrain

const T = report.terrain;
const TN = T.steps;
const tx = T.dims[0] === 3 ? pIdx('fore_wp') : 0;
const ty = T.dims[1] === 8 ? pIdx('fore_len_frac') : 1;
const rMin = Math.min(...T.race);
const rMax = Math.max(...T.race);
const tBest = T.race.reduce((b, r, i) => (T.valid[i] && (b < 0 || r < T.race[b]) ? i : b), -1);
/** Half-width of the terrain along bow fineness (taper is 1): a wide, low landscape. */
const TW = 1.5;
const tz = (r: number) => -0.5 + (1.05 * (r - rMin)) / (rMax - rMin);
const tp = (i: number): V3 => [TW * norm(T.v[i][0], report.lo[tx], report.hi[tx]), norm(T.v[i][1], report.lo[ty], report.hi[ty]), tz(T.race[i])];

const Terrain: React.FC<{ w: number; h: number; az: number; el: number; dots: number }> = ({ w, h, az, el, dots }) => {
  const cam = camera(az, el);
  // Fit the terrain's bounding box (floor to peak) to the panel, leaving room for the labels.
  const box = T.race.map((_, i) => cam(tp(i)));
  const bx = [Math.min(...box.map((q) => q.x)), Math.max(...box.map((q) => q.x))];
  const by = [Math.min(...box.map((q) => q.y)), Math.max(...box.map((q) => q.y))];
  const s = Math.min((w - 2 * 160) / (bx[1] - bx[0]), (h - space.xxl - 2 * space.xxl) / (by[1] - by[0]));
  const cx = w / 2 - ((bx[0] + bx[1]) / 2) * s;
  const cy = space.xxl + (h - space.xxl - 2 * space.xxl) / 2 - ((by[0] + by[1]) / 2) * s;
  const P = (p: V3) => {
    const q = cam(p);
    return { x: cx + q.x * s, y: cy + q.y * s, d: q.d };
  };
  const light: V3 = [-0.4, -0.5, 0.77];
  type Item = { d: number; el: React.ReactNode };
  const items: Item[] = [];
  for (let j = 0; j < TN - 1; j++)
    for (let k = 0; k < TN - 1; k++) {
      const ids = [j * TN + k, j * TN + k + 1, (j + 1) * TN + k + 1, (j + 1) * TN + k];
      const v = ids.map(tp);
      const q = v.map(P);
      // Facet normal from the two diagonals, for a little Lambert shading.
      const a: V3 = [v[2][0] - v[0][0], v[2][1] - v[0][1], v[2][2] - v[0][2]];
      const b: V3 = [v[3][0] - v[1][0], v[3][1] - v[1][1], v[3][2] - v[1][2]];
      const n: V3 = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
      const nl = Math.hypot(...n) || 1;
      const lam = Math.max(0, (n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / nl);
      const zt = (v[0][2] + v[1][2] + v[2][2] + v[3][2]) / 4;
      const t = (zt + 0.5) / 1.05;
      // Lower (faster) is darker. Shapes that fail a gate wash out toward the canvas.
      const ok = ids.every((i) => T.valid[i]);
      const lit = Math.min(0.96, lerp(0.2, 0.9, Math.sqrt(t)) * (0.78 + 0.26 * lam));
      const g = Math.round(255 * (ok ? lit : lerp(lit, 0.97, 0.75 * dots)));
      items.push({
        d: (q[0].d + q[1].d + q[2].d + q[3].d) / 4,
        el: <polygon key={`q${j}-${k}`} points={q.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')} fill={`rgb(${g},${g},${g})`} stroke="#ffffff" strokeOpacity={0.35} strokeWidth={hairline} />,
      });
    }
  items.sort((u, v) => v.d - u.d);
  const best = P([tp(tBest)[0], tp(tBest)[1], tp(tBest)[2] + 0.03]);
  /** Surface point at grid (k, j), nudged outward in x / y: where an axis label hangs. */
  const edge = (k: number, j: number, ox: number, oy: number): V3 => {
    const p = tp(j * TN + k);
    return [p[0] + ox, p[1] + oy, p[2]];
  };
  const lbl = (p: V3, t: string, anchor: 'start' | 'middle' | 'end', dy = 0) => {
    const q = P(p);
    return (
      <text x={q.x} y={q.y + dy} textAnchor={anchor} fontSize={type.caption.size} fill={chart.tick}>
        {t}
      </text>
    );
  };
  const dd = 16;
  return (
    <svg width={w} height={h} style={{ display: 'block', overflow: 'visible', fontFamily: font.sans }}>
      {items.map((it) => it.el)}
      {lbl(edge(Math.floor(TN / 2), 0, 0, -0.22), 'bow fineness', 'middle', type.caption.size + space.lg)}
      {lbl(edge(TN - 1, Math.floor(TN / 2), 0.08, 0), 'taper', 'start', type.caption.size)}
      <g opacity={dots}>
        <path d={`M${best.x},${best.y - dd}L${best.x + dd},${best.y}L${best.x},${best.y + dd}L${best.x - dd},${best.y}Z`} fill={color.accent} stroke={color.canvas} strokeWidth={3} />
        <text x={best.x + dd + space.sm} y={best.y - dd - space.xs} fontSize={type.body.size} fill={color.ink} fontWeight={type.title.weight} style={{ fontVariantNumeric: 'tabular-nums' }}>
          {`Fastest possible, ${T.race[tBest].toFixed(1)} s`}
        </text>
      </g>
    </svg>
  );
};

// ---------------------------------------------------------------- the montage

const PANELS: Record<string, React.FC<P>> = {
  bending: Bending,
  shear: Shear,
  stress: Stress,
  dc: DemandCapacity,
  material: Material,
  tradeoff: TradeOff,
  cloud: Cloud,
  trends: Trends,
  freeboard: Freeboard,
  importance: Importance,
  share: Share,
  gmsweep: Sweep,
  volume: Volume,
  slices: Slices,
  failure: Failure,
};
/** Mosaic slot the terrain takes (row 2, column 2 of 4 x 4), so the dive starts inside the wall. */
const TERRAIN_SLOT = 10;
/** Scale the last cut has shrunk to by the time the wall bursts out of it. */
const ZOOM_END = 0.42;
/** Frames for the wall to burst out of the centre, and for the dial-in on the terrain. */
const BURST = 16;
const DIAL = 22;

export const Analysis: React.FC<{ end: number }> = ({ end }) => {
  const f = useCurrentFrame();
  const { M, W, H } = useLayout();
  const sc = sceneById('analysis');
  const c = sc.copy;
  const cuts = c.cuts as string[];
  const labels = c.labels as string[];
  const starts = cuts.map((id) => beat(sc, id));
  const mosaic = beat(sc, 'mosaic');
  const dive = beat(sc, 'dive');
  const box = { x: M - space.xxl, y: 240, w: W - 2 * (M - space.xxl), h: H - 240 - space.xxl };

  const cut = f < mosaic ? starts.reduce((k, s, i) => (f >= s ? i : k), 0) : -1;
  const label = f < starts[0] ? '' : cut >= 0 ? labels[cut] : f < dive + DIAL / 2 ? (c.mosaic as string) : (c.terrain as string);

  // Terrain camera: one slow, continuous move from the wall to the end of the scene.
  const cm = progress(f, mosaic, end - mosaic, easeInOut);
  const az = lerp(-0.95, -0.5, cm);
  const el = lerp(0.3, 0.46, cm);

  // The cuts shrink toward the centre as they speed up: full size on the first, ZOOM_END on the last.
  const zoomAt = (t: number) => 1 - (1 - ZOOM_END) * Math.pow(Math.min(1, Math.max(0, (t - starts[0]) / (mosaic - starts[0]))), 1.4);
  const center = { x: box.x + box.w / 2, y: box.y + box.h / 2 };
  // The chart's name rides on the chart's top-left corner as it shrinks inward (type size fixed),
  // and glides back out to the full-size corner as the wall bursts open.
  const labelZ = cut >= 0 ? zoomAt(f) : lerp(zoomAt(mosaic), 1, progress(f, mosaic, BURST));
  const labelX = center.x - (box.w * labelZ) / 2 + (M - box.x) * labelZ - space.sm;
  const labelY = center.y - (box.h * labelZ) / 2 - (box.y - (M + space.md)) - space.sm;

  let body: React.ReactNode;
  if (f < starts[0]) {
    body = null;
  } else if (cut >= 0) {
    const C = PANELS[cuts[cut]];
    const d = (cut + 1 < starts.length ? starts[cut + 1] : mosaic) - starts[cut];
    const lf = f - starts[cut];
    const z = zoomAt(f);
    body = (
      <At x={box.x} y={box.y} style={{ transform: `scale(${z})`, transformOrigin: 'center' }}>
        <C f={lf} d={d} w={box.w} h={box.h} />
      </At>
    );
  } else {
    // Every view: the 16 panels burst out of the centre (where the last cut shrank to) into a 4 x 4
    // wall, hold, then the camera dials in on the terrain tile until it fills the chart box.
    const gap = space.md;
    const sc4 = Math.min((box.w - 3 * gap) / 4 / box.w, (box.h - 3 * gap) / 4 / box.h);
    const tw = box.w * sc4;
    const th = box.h * sc4;
    const ox = box.x + (box.w - (4 * tw + 3 * gap)) / 2;
    const oy = box.y + (box.h - (4 * th + 3 * gap)) / 2;
    const slots = Array.from({ length: 16 }, (_, i) => ({ x: ox + (i % 4) * (tw + gap), y: oy + Math.floor(i / 4) * (th + gap) }));
    const others = slots.filter((_, i) => i !== TERRAIN_SLOT);
    const burst = progress(f, mosaic, BURST);
    const dial = progress(f, dive, DIAL, easeInOut);
    const othersOut = 1 - progress(f, dive + 4, DIAL - 6, easeInOut);
    const z0 = zoomAt(mosaic);
    const s = lerp(z0, sc4, burst);
    const at = (slot: { x: number; y: number }) => ({
      x: lerp(center.x, slot.x + tw / 2, burst) - (box.w * s) / 2,
      y: lerp(center.y, slot.y + th / 2, burst) - (box.h * s) / 2,
    });
    const border = (o: number): React.CSSProperties => ({
      border: `${hairline}px solid rgba(224,224,224,${o})`,
      borderRadius: radius.md,
      background: color.canvas,
      overflow: 'hidden',
    });
    // Camera for the dial-in: maps the terrain slot onto the chart box.
    const ts = slots[TERRAIN_SLOT];
    const k = lerp(1, 1 / sc4, dial);
    const tx = lerp(ts.x, box.x, dial) - k * ts.x;
    const ty = lerp(ts.y, box.y, dial) - k * ts.y;
    const tPos = at(ts);
    body = (
      <>
        <div style={{ position: 'absolute', left: 0, top: 0, width: W, height: H, transform: `translate(${tx}px, ${ty}px) scale(${k})`, transformOrigin: '0 0' }}>
          <At x={tPos.x} y={tPos.y} w={box.w * s} h={box.h * s} style={{ ...border(burst * (1 - dial)), overflow: 'visible' }}>
            <div style={{ width: box.w, height: box.h, transform: `scale(${s})`, transformOrigin: '0 0' }}>
              <Terrain w={box.w} h={box.h} az={az} el={el} dots={progress(f, dive + DIAL - 4, motion.dur.md)} />
            </div>
          </At>
          {othersOut > 0
            ? cuts.map((id, i) => {
                const C = PANELS[id];
                const p = at(others[i]);
                return (
                  <At key={id} x={p.x} y={p.y} w={box.w * s} h={box.h * s} style={{ ...border(burst), opacity: othersOut }}>
                    <div style={{ width: box.w, height: box.h, transform: `scale(${s})`, transformOrigin: '0 0' }}>
                      <C f={HUGE} d={HUGE} w={box.w} h={box.h} />
                    </div>
                  </At>
                );
              })
            : null}
        </div>
        <At x={M} y={H - space.xxl - type.body.size * 1.4} style={{ opacity: progress(f, dive + DIAL, motion.dur.md), fontSize: type.body.size, color: color.muted }}>
          {`${c.terrainNote as string} ${c.caveat as string}`}
        </At>
      </>
    );
  }

  return (
    <Stage end={end}>
      {body}
      {/* Above the wall, so the tiles flying past during the dial-in pass under the title. */}
      {f < starts[0] ? (
        <At x={M} y={M}>
          <KineticHeadline lines={[c.title as string]} typeName="heading1" start={1} weightFrom={300} dur={motion.dur.sm} exitAt={starts[0] - motion.dur.md} />
          <div style={{ height: space.md }} />
          <KineticHeadline lines={[c.subtitle as string]} typeName="heading4" start={8} dur={motion.dur.md} color={color.muted} exitAt={starts[0] - motion.dur.md + 2} />
        </At>
      ) : null}
      <At x={labelX} y={labelY} style={{ fontSize: type.heading3.size, fontWeight: type.heading3.weight, lineHeight: type.heading3.lineHeight, whiteSpace: 'nowrap', background: color.canvas, padding: space.sm }}>
        {label}
      </At>
    </Stage>
  );
};
