import React from 'react';
import { useCurrentFrame } from 'remotion';
import { color, font, motion, radius, space, type } from '../tokens';
import { lerp, progress } from '../motion';

type Status = 'success' | 'warning' | 'error';
type Props = {
  status: Status;
  /** Sentence case, as the web UI badge ("Verified"). */
  label: string;
  /** Muted text after the badge (e.g. "at 321 stations"). */
  note?: string;
  start?: number;
  size?: 'md' | 'lg';
  /** 'stamp': a short press from 112%. 'grow': a slow fade while it scales up from 80%. */
  enter?: 'stamp' | 'grow';
  /** Transform origin of the entrance (CSS), e.g. 'right center' for a badge in a right corner. */
  origin?: string;
};

const fill: Record<Status, string> = { success: color.success, warning: color.warning, error: color.error };
// Warning yellow is too light for white text (DESIGN.md): ink label on it.
const ink: Record<Status, string> = { success: color.onPrimary, warning: color.ink, error: color.onPrimary };

/** DESIGN.md status-badge: compact {rounded.xs} chip, solid semantic fill. Stamps on: a short press, no bounce. */
export const StatusBadge: React.FC<Props> = ({ status, label, note, start = 0, size = 'lg', enter = 'stamp', origin = 'left center' }) => {
  const frame = useCurrentFrame();
  const grow = enter === 'grow';
  const p = progress(frame, start, grow ? motion.dur.lg : motion.dur.sm);
  const sp = grow ? progress(frame, start, motion.dur.xl) : p;
  const tk = size === 'lg' ? type.title : type.label;
  const h = size === 'lg' ? 64 : 48;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: space.md, fontFamily: font.sans, letterSpacing: 0 }}>
      <div
        style={{
          height: h,
          padding: `0 ${size === 'lg' ? space.lg : space.md}px`,
          display: 'flex',
          alignItems: 'center',
          borderRadius: radius.xs,
          background: fill[status],
          color: ink[status],
          fontSize: tk.size,
          fontWeight: weightFor(size),
          lineHeight: 1,
          opacity: p,
          transform: `scale(${grow ? lerp(0.8, 1, sp) : lerp(1.12, 1, p)})`,
          transformOrigin: origin,
        }}
      >
        {label}
      </div>
      {note ? (
        <span style={{ fontSize: type.body.size, color: color.muted, fontWeight: type.body.weight, opacity: progress(frame, start + motion.dur.xs, motion.dur.md) }}>
          {note}
        </span>
      ) : null}
    </div>
  );
};

const weightFor = (size: 'md' | 'lg') => (size === 'lg' ? type.title.weight : type.label.weight);
