import React from 'react';
import { useCurrentFrame } from 'remotion';
import { color, font, motion, space, type, type TypeName } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';

type Props = {
  /** Value shown before the count starts (a real value too, e.g. the baseline). */
  from: number;
  /** Value it settles on. */
  to: number;
  start?: number;
  duration?: number;
  decimals?: number;
  unit?: string;
  label?: string;
  typeName?: TypeName;
  /** Number color. Use color.accent only when the number IS the winner. */
  color?: string;
  /** Frame the whole counter fades in (defaults to start - motion.dur.md). */
  appear?: number;
};

/** A number that counts from one real value to another, tabular figures, light unit, muted label. */
export const MetricCounter: React.FC<Props> = ({
  from,
  to,
  start = 0,
  duration = motion.dur.xl,
  decimals = 1,
  unit,
  label,
  typeName = 'display',
  color: c = color.ink,
  appear,
}) => {
  const frame = useCurrentFrame();
  const tk = type[typeName];
  const fade = progress(frame, appear ?? start - motion.dur.md, motion.dur.md);
  const v = lerp(from, to, progress(frame, start, duration, easeInOut));
  return (
    <div style={{ fontFamily: font.sans, letterSpacing: 0, opacity: fade }}>
      {label ? (
        <div style={{ fontSize: type.body.size, lineHeight: type.body.lineHeight, fontWeight: type.body.weight, color: color.muted, marginBottom: space.xs }}>
          {label}
        </div>
      ) : null}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: space.sm }}>
        <span style={{ fontSize: tk.size, lineHeight: 1, fontWeight: tk.weight, color: c, fontVariantNumeric: 'tabular-nums' }}>
          {v.toFixed(decimals)}
        </span>
        {unit ? (
          <span style={{ fontSize: type.bodyLg.size * 1.4, lineHeight: 1, fontWeight: type.bodyLg.weight, color: color.muted }}>{unit}</span>
        ) : null}
      </div>
    </div>
  );
};
