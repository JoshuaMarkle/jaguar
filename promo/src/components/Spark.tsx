import React from 'react';
import { color, font, hairline, space, type } from '../tokens';

type Props = {
  width: number;
  height: number;
  x: number[];
  y: number[];
  xDomain: [number, number];
  yDomain: [number, number];
  /** Draw the line up to this x. */
  upTo: number;
  /** Marked points (ink dots) with an optional label under the axis. */
  marks?: Array<{ x: number; y: number; label?: string; on: number }>;
  /** Current point (ink dot). */
  head?: { x: number; y: number };
  /** Muted axis note at the right end of the baseline. */
  axis?: string;
};

/** A small trend line on a hairline baseline: the shape of a number over time, no grid, no ticks. */
export const Spark: React.FC<Props> = ({ width, height, x, y, xDomain, yDomain, upTo, marks = [], head, axis }) => {
  const padB = type.caption.size + space.md;
  const h = height - padB;
  const px = (v: number) => ((v - xDomain[0]) / (xDomain[1] - xDomain[0] || 1)) * width;
  const py = (v: number) => h - ((v - yDomain[0]) / (yDomain[1] - yDomain[0] || 1)) * h;
  const pts: string[] = [];
  for (let i = 0; i < x.length; i++) {
    if (x[i] > upTo) {
      if (i > 0) {
        const t = (upTo - x[i - 1]) / (x[i] - x[i - 1] || 1);
        pts.push(`${px(upTo).toFixed(1)},${py(y[i - 1] + t * (y[i] - y[i - 1])).toFixed(1)}`);
      }
      break;
    }
    pts.push(`${px(x[i]).toFixed(1)},${py(y[i]).toFixed(1)}`);
  }
  return (
    <svg width={width} height={height} style={{ display: 'block', overflow: 'visible', fontFamily: font.sans, letterSpacing: 0 }}>
      <line x1={0} x2={width} y1={h + 0.5} y2={h + 0.5} stroke={color.hairline} strokeWidth={hairline} />
      <polyline points={pts.join(' ')} fill="none" stroke={color.ink} strokeWidth={3} strokeLinejoin="round" />
      {marks.map((m, i) => (
        <g key={i} opacity={m.on}>
          <circle cx={px(m.x)} cy={py(m.y)} r={6} fill={color.ink} />
          {m.label ? (
            <text x={px(m.x)} y={h + padB - space.xxs} fontSize={type.caption.size} fill={color.muted} textAnchor={i === 0 ? 'start' : i === marks.length - 1 ? 'end' : 'middle'}>
              {m.label}
            </text>
          ) : null}
        </g>
      ))}
      {head ? <circle cx={px(head.x)} cy={py(head.y)} r={7} fill={color.ink} /> : null}
      {axis ? (
        <text x={width} y={h + padB - space.xxs} fontSize={type.caption.size} fill={color.muted} textAnchor="end">
          {axis}
        </text>
      ) : null}
    </svg>
  );
};
