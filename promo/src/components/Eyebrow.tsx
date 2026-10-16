import React from 'react';
import { useCurrentFrame } from 'remotion';
import { color, font, motion, space, type, type TypeName } from '../tokens';
import { progress } from '../motion';

type Props = {
  children: React.ReactNode;
  start?: number;
  typeName?: TypeName;
  /** A muted index before the label, e.g. "1/9". Optional. */
  index?: string;
  color?: string;
};

/** Small muted label above a headline or a panel (DESIGN.md award-lockup eyebrow). Fades and rises 8 px. */
export const Eyebrow: React.FC<Props> = ({ children, start = 0, typeName = 'body', index, color: c = color.muted }) => {
  const frame = useCurrentFrame();
  const p = progress(frame, start, motion.dur.md);
  const tk = type[typeName];
  return (
    <div
      style={{
        fontFamily: font.sans,
        fontSize: tk.size,
        lineHeight: tk.lineHeight,
        fontWeight: type.body.weight,
        letterSpacing: 0,
        color: c,
        opacity: p,
        transform: `translateY(${(1 - p) * space.xs}px)`,
        display: 'flex',
        gap: space.md,
      }}
    >
      {index ? <span style={{ color: color.faint, fontVariantNumeric: 'tabular-nums' }}>{index}</span> : null}
      <span>{children}</span>
    </div>
  );
};
