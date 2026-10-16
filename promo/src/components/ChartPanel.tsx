import React from 'react';
import { chart, color, font, hairline, space, type } from '../tokens';

// The web UI chart language (web/index.html axes(), line(), marker(), ring(), legend()) as SVG at
// video scale: hairline frame, faint grid, muted ticks, ink data lines, green / grey dots, the red
// dashed gate and the one blue winner ring. Animation is passed in (reveal, count) by the caller.

export type LineSeries = {
  kind: 'line';
  x: number[];
  y: number[];
  color?: string;
  dash?: number[];
  width?: number;
  /** 0..1: how much of the line is drawn, left to right. */
  reveal?: number;
  opacity?: number;
};
export type Dot = { x: number; y: number; state: 'pass' | 'fail' | 'error' | 'point' };
export type ScatterSeries = {
  kind: 'scatter';
  dots: Dot[];
  /** How many dots are shown (in order); fractional fades the last one in. */
  count?: number;
  radius?: number;
};
export type BandSeries = { kind: 'band'; x0: number; x1: number; y0?: number; y1?: number; fill: string; opacity?: number };
/** Filled area between a line and a base value (sagging / hogging, shear), revealed like a line. */
export type AreaSeries = { kind: 'area'; x: number[]; y: number[]; base?: number; fill?: string; opacity?: number; reveal?: number };
export type Series = LineSeries | ScatterSeries | BandSeries | AreaSeries;

type Props = {
  width: number;
  height: number;
  x: { domain: [number, number]; label: string; ticks?: number[]; format?: (v: number) => string };
  y: { domain: [number, number]; label: string; ticks?: number[]; format?: (v: number) => string };
  series: Series[];
  /** Horizontal / vertical gate lines (red dashed), with an optional label. */
  gates?: Array<{ y?: number; x?: number; label?: string; opacity?: number }>;
  /** A position marker (ink vertical line + dot), e.g. "canoe now" on a speed trace. */
  cursor?: { x: number; y: number; opacity?: number };
  /** The winner: one accent ring. visible 0..1. */
  winner?: { x: number; y: number; visible: number };
  /** Legend items drawn top left, inside the frame. */
  legend?: Array<{ color: string; label: string; dash?: number[]; dot?: boolean }>;
  /** 0..1 fade of frame, ticks and labels. */
  chrome?: number;
  padding?: { l: number; r: number; t: number; b: number };
};

export function niceTicks(a: number, b: number, n = 5): number[] {
  const span = b - a || 1;
  const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= step0) as number;
  const out: number[] = [];
  for (let v = Math.ceil(a / step) * step; v <= b + 1e-9 * span; v += step) out.push(+v.toPrecision(10));
  return out;
}
const fmt = (v: number) => (Math.abs(v) >= 1e5 || (Math.abs(v) < 1e-2 && v !== 0) ? v.toExponential(1) : String(+v.toPrecision(4)));

export const ChartPanel: React.FC<Props> = ({ width, height, x, y, series, gates = [], cursor, winner, legend, chrome = 1, padding }) => {
  const pad = padding ?? { l: 136, r: space.xl, t: space.xl, b: 104 };
  const r = { l: pad.l, t: pad.t, w: width - pad.l - pad.r, h: height - pad.t - pad.b };
  const px = (v: number) => r.l + ((v - x.domain[0]) / (x.domain[1] - x.domain[0] || 1)) * r.w;
  const py = (v: number) => r.t + r.h - ((v - y.domain[0]) / (y.domain[1] - y.domain[0] || 1)) * r.h;
  const xt = x.ticks ?? niceTicks(x.domain[0], x.domain[1]);
  const yt = y.ticks ?? niceTicks(y.domain[0], y.domain[1], 4);
  const tick = type.caption.size;
  const clipId = React.useId().replace(/:/g, '');
  return (
    <svg width={width} height={height} style={{ display: 'block', overflow: 'visible', fontFamily: font.sans, letterSpacing: 0 }}>
      <defs>
        <clipPath id={`plot-${clipId}`}>
          <rect x={r.l} y={r.t} width={r.w} height={r.h} />
        </clipPath>
      </defs>
      <g opacity={chrome}>
        {xt.map((v) => (
          <g key={`x${v}`}>
            <line x1={px(v)} x2={px(v)} y1={r.t} y2={r.t + r.h} stroke={chart.grid} strokeWidth={hairline} />
            <text x={px(v)} y={r.t + r.h + tick + space.sm} textAnchor="middle" fontSize={tick} fill={chart.tick} style={{ fontVariantNumeric: 'tabular-nums' }}>
              {(x.format ?? fmt)(v)}
            </text>
          </g>
        ))}
        {yt.map((v) => (
          <g key={`y${v}`}>
            <line x1={r.l} x2={r.l + r.w} y1={py(v)} y2={py(v)} stroke={chart.grid} strokeWidth={hairline} />
            <text x={r.l - space.md} y={py(v) + tick * 0.35} textAnchor="end" fontSize={tick} fill={chart.tick} style={{ fontVariantNumeric: 'tabular-nums' }}>
              {(y.format ?? fmt)(v)}
            </text>
          </g>
        ))}
        <text x={r.l + r.w / 2} y={r.t + r.h + 2 * tick + space.lg} textAnchor="middle" fontSize={tick} fill={chart.tick}>
          {x.label}
        </text>
        <text transform={`translate(${r.l - pad.l + tick}, ${r.t + r.h / 2}) rotate(-90)`} textAnchor="middle" fontSize={tick} fill={chart.tick}>
          {y.label}
        </text>
      </g>
      <g clipPath={`url(#plot-${clipId})`}>
        {series.map((s, i) => {
          if (s.kind === 'band') {
            const y0 = s.y0 ?? y.domain[0];
            const y1 = s.y1 ?? y.domain[1];
            return <rect key={i} x={px(s.x0)} y={py(y1)} width={px(s.x1) - px(s.x0)} height={py(y0) - py(y1)} fill={s.fill} opacity={s.opacity ?? 1} />;
          }
          if (s.kind === 'area') {
            const b = py(s.base ?? 0);
            const pts = s.x.map((xv, k) => `${px(xv).toFixed(2)},${py(s.y[k]).toFixed(2)}`);
            const poly = [`${px(s.x[0]).toFixed(2)},${b}`, ...pts, `${px(s.x[s.x.length - 1]).toFixed(2)},${b}`].join(' ');
            return (
              <g key={i}>
                <clipPath id={`rv-${clipId}-${i}`}>
                  <rect x={r.l - 4} y={r.t - 40} width={r.w * (s.reveal ?? 1) + 4} height={r.h + 80} />
                </clipPath>
                <polygon points={poly} fill={s.fill ?? color.ink} opacity={s.opacity ?? 0.08} clipPath={`url(#rv-${clipId}-${i})`} />
              </g>
            );
          }
          if (s.kind === 'line') {
            const pts = s.x.map((xv, k) => `${px(xv).toFixed(2)},${py(s.y[k]).toFixed(2)}`).join(' ');
            const reveal = s.reveal ?? 1;
            // Reveal left to right with a clip, so dashes stay put while the line draws.
            const clipW = r.w * reveal;
            return (
              <g key={i} opacity={s.opacity ?? 1}>
                <clipPath id={`rv-${clipId}-${i}`}>
                  <rect x={r.l - 4} y={r.t - 40} width={clipW + 4} height={r.h + 80} />
                </clipPath>
                <polyline
                  points={pts}
                  fill="none"
                  stroke={s.color ?? color.ink}
                  strokeWidth={s.width ?? chart.line}
                  strokeLinejoin="round"
                  strokeDasharray={s.dash?.join(' ')}
                  clipPath={`url(#rv-${clipId}-${i})`}
                />
              </g>
            );
          }
          const count = s.count ?? s.dots.length;
          const rad = s.radius ?? chart.dot;
          const full = Math.floor(count);
          const shown = s.dots.slice(0, Math.min(s.dots.length, Math.ceil(count)));
          // Failing dots first, passing on top (as plotCands).
          const order = shown.map((d, k) => ({ d, k })).sort((a, b) => Number(a.d.state === 'pass') - Number(b.d.state === 'pass'));
          return (
            <g key={i}>
              {order.map(({ d, k }) => {
                const op = k < full ? 1 : count - full;
                const cx = px(d.x);
                const cy = py(d.y);
                if (d.state === 'error')
                  return (
                    <path key={k} d={`M${cx - rad},${cy - rad}L${cx + rad},${cy + rad}M${cx + rad},${cy - rad}L${cx - rad},${cy + rad}`} stroke={color.error} strokeWidth={2.4} opacity={op} />
                  );
                const fill = d.state === 'pass' ? color.success : d.state === 'fail' ? color.candidateFail : color.ink;
                return <circle key={k} cx={cx} cy={cy} r={rad} fill={fill} opacity={op} />;
              })}
            </g>
          );
        })}
      </g>
      {gates.map((g, i) => (
        <g key={`g${i}`} opacity={g.opacity ?? 1}>
          {g.y !== undefined ? (
            <line x1={r.l} x2={r.l + r.w} y1={py(g.y)} y2={py(g.y)} stroke={color.error} strokeWidth={2} strokeDasharray={chart.gateDash.join(' ')} />
          ) : null}
          {g.x !== undefined ? (
            <line x1={px(g.x)} x2={px(g.x)} y1={r.t} y2={r.t + r.h} stroke={color.error} strokeWidth={2} strokeDasharray={chart.gateDash.join(' ')} />
          ) : null}
        </g>
      ))}
      {cursor ? (
        <g opacity={cursor.opacity ?? 1}>
          <line x1={px(cursor.x)} x2={px(cursor.x)} y1={r.t} y2={r.t + r.h} stroke={color.ink} strokeWidth={hairline * 2} />
          <circle cx={px(cursor.x)} cy={py(cursor.y)} r={chart.dot * 1.6} fill={color.ink} />
        </g>
      ) : null}
      {winner && winner.visible > 0 ? (
        <circle
          cx={px(winner.x)}
          cy={py(winner.y)}
          r={chart.winnerRing * (1.6 - 0.6 * winner.visible)}
          fill="none"
          stroke={color.accent}
          strokeWidth={chart.winnerRingWidth}
          opacity={winner.visible}
        />
      ) : null}
      <rect x={r.l + 0.5} y={r.t + 0.5} width={r.w} height={r.h} fill="none" stroke={chart.frame} strokeWidth={hairline} opacity={chrome} />
      {legend ? <Legend x={r.l + space.lg} y={r.t + space.lg + tick * 0.8} items={legend} opacity={chrome} /> : null}
    </svg>
  );
};

const Legend: React.FC<{ x: number; y: number; items: NonNullable<Props['legend']>; opacity: number }> = ({ x, y, items, opacity }) => {
  const tick = type.caption.size;
  // Approximate advance (Inter at 450 averages ~0.55 em); the legend sits on a white backdrop.
  const widths = items.map((it) => 36 + space.sm + it.label.length * tick * 0.55 + space.lg);
  const total = widths.reduce((a, b) => a + b, 0);
  let cx = x;
  return (
    <g opacity={opacity}>
      <rect x={x - space.sm} y={y - tick} width={total + space.sm} height={tick * 1.5} fill={color.canvas} opacity={0.92} />
      {items.map((it, i) => {
        const gx = cx;
        cx += widths[i];
        return (
          <g key={i}>
            {it.dot ? (
              <circle cx={gx + 18} cy={y - tick * 0.32} r={chart.dot + 1} fill={it.color} />
            ) : (
              <line x1={gx} x2={gx + 36} y1={y - tick * 0.32} y2={y - tick * 0.32} stroke={it.color} strokeWidth={chart.line} strokeDasharray={it.dash?.join(' ')} />
            )}
            <text x={gx + 36 + space.sm} y={y} fontSize={tick} fill={color.inkSoft}>
              {it.label}
            </text>
          </g>
        );
      })}
    </g>
  );
};
