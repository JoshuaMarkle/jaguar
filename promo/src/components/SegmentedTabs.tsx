import React from 'react';
import { useCurrentFrame } from 'remotion';
import { color, font, hairline, motion, radius, space, type } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';

type Props = {
  options: string[];
  /** Active option index before `switchAt`. */
  from: number;
  /** Active option index after the switch (defaults to `from`: static). */
  to?: number;
  switchAt?: number;
  /** Width of one option (equal widths, like the app's group control). */
  optionWidth: number;
  /** Muted label before the track, like the app's "Optimize for". */
  label?: string;
  start?: number;
  /** Fixed width of the label, px (so the control's size is known for a camera move). */
  labelWidth?: number;
};

/**
 * DESIGN.md segmented-control as in web/index.html .seg: a canvas-soft {rounded.sm} track, options
 * at {rounded.xs}, the active one a white segment with a 1 px hairline ring (no shadow), inactive
 * labels muted. The white segment slides to the new option.
 */
export const SegmentedTabs: React.FC<Props> = ({ options, from, to = from, switchAt = 0, optionWidth, label, start = 0, labelWidth }) => {
  const frame = useCurrentFrame();
  const appear = progress(frame, start, motion.dur.md);
  const s = progress(frame, switchAt, motion.dur.lg, easeInOut);
  const pos = lerp(from, to, s);
  const inset = 4; // the app's 2 px track padding at video scale
  const h = 64;
  const tk = type.body;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: space.lg, fontFamily: font.sans, letterSpacing: 0, opacity: appear }}>
      {label ? <span style={{ fontSize: tk.size, color: color.muted, fontWeight: tk.weight, whiteSpace: 'nowrap', width: labelWidth }}>{label}</span> : null}
      <div
        style={{
          position: 'relative',
          display: 'flex',
          gap: inset,
          padding: inset,
          height: h,
          background: color.canvasSoft,
          borderRadius: radius.sm,
          boxSizing: 'border-box',
        }}
      >
        <div
          style={{
            position: 'absolute',
            top: inset,
            left: inset + pos * (optionWidth + inset),
            width: optionWidth,
            height: h - 2 * inset,
            background: color.canvas,
            borderRadius: radius.xs,
            boxShadow: `inset 0 0 0 ${hairline}px ${color.hairline}`,
          }}
        />
        {options.map((o, i) => {
          const on = Math.max(0, 1 - Math.abs(pos - i));
          return (
            <div
              key={o}
              style={{
                position: 'relative',
                width: optionWidth,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: tk.size,
                fontWeight: tk.weight,
                color: on > 0.5 ? color.ink : color.muted,
                whiteSpace: 'nowrap',
              }}
            >
              {o}
            </div>
          );
        })}
      </div>
    </div>
  );
};
