import React from 'react';
import { useCurrentFrame } from 'remotion';
import { color, font, hairline, motion, radius, space, type } from '../tokens';
import { easeInOut, progress } from '../motion';

type Props = {
  /** Title bar text (muted), e.g. the working directory. */
  title: string;
  /** Typed after the prompt, character by character. */
  command: string;
  /** Real output, one entry per line, printed after the command. */
  output: string[];
  start?: number;
  /** Characters typed per frame. */
  typeRate?: number;
  /** Output lines printed per frame. */
  lineRate?: number;
  width: number;
  height: number;
  /** Line index to end on: scrolled into view and marked (accent for the winner). */
  focusLine?: number;
  focusAt?: number;
  /** Lines (by index) set in success green, e.g. the VERIFIED line. */
  successLines?: number[];
};

/**
 * A canvas-soft terminal window (no traffic lights, no shadow): title bar on a hairline, prompt,
 * the command typed out, then JAGUAR's real output scrolling. Long lines are clipped, not wrapped.
 */
export const TerminalWindow: React.FC<Props> = ({
  title,
  command,
  output,
  start = 0,
  typeRate = 1.2,
  lineRate = 1.5,
  width,
  height,
  focusLine,
  focusAt,
  successLines = [],
}) => {
  const frame = useCurrentFrame();
  const f = frame - start;
  const tk = type.mono;
  const lh = tk.size * tk.lineHeight;
  const typed = Math.max(0, Math.min(command.length, Math.floor(f * typeRate)));
  const typingDone = Math.ceil(command.length / typeRate) + motion.dur.sm;
  const shown = Math.max(0, Math.min(output.length, Math.floor((f - typingDone) * lineRate)));
  const barH = 64;
  const bodyH = height - barH - 2 * space.lg;
  const visibleRows = Math.floor(bodyH / lh);
  const rowsUsed = 1 + shown; // prompt line + output
  const follow = Math.max(0, rowsUsed - visibleRows);
  // Optionally settle with the focus line in the lower third of the window.
  let scroll = follow;
  if (focusLine !== undefined && focusAt !== undefined) {
    const target = Math.max(0, 1 + focusLine - Math.floor(visibleRows * 0.66));
    scroll = follow + (target - follow) * progress(frame, focusAt, motion.dur.lg, easeInOut);
  }
  const focus = focusLine !== undefined && focusAt !== undefined ? progress(frame, focusAt + motion.dur.sm, motion.dur.md) : 0;
  const caretOn = Math.floor(f / 15) % 2 === 0 || typed < command.length;
  return (
    <div
      style={{
        width,
        height,
        background: color.canvasSoft,
        borderRadius: radius.md,
        overflow: 'hidden',
        fontFamily: font.mono,
        letterSpacing: 0,
        opacity: progress(frame, start - motion.dur.md, motion.dur.md),
      }}
    >
      <div
        style={{
          height: barH,
          display: 'flex',
          alignItems: 'center',
          padding: `0 ${space.lg}px`,
          borderBottom: `${hairline}px solid ${color.hairline}`,
          fontFamily: font.sans,
          fontSize: type.caption.size,
          color: color.muted,
        }}
      >
        {title}
      </div>
      <div style={{ position: 'relative', height: height - barH, padding: `${space.lg}px ${space.xl}px`, boxSizing: 'border-box' }}>
        <div style={{ height: visibleRows * lh, overflow: 'hidden' }}>
        <div style={{ transform: `translateY(${-scroll * lh}px)`, fontSize: tk.size, lineHeight: `${lh}px`, whiteSpace: 'pre', color: color.ink }}>
          <div>
            <span style={{ color: color.muted }}>$ </span>
            {command.slice(0, typed)}
            {shown === 0 && caretOn ? <span style={{ display: 'inline-block', width: tk.size * 0.6, height: tk.size * 1.1, background: color.ink, verticalAlign: 'text-bottom' }} /> : null}
          </div>
          {output.slice(0, shown).map((l, i) => {
            const isFocus = i === focusLine;
            const ok = successLines.includes(i);
            return (
              <div
                key={i}
                style={{
                  color: isFocus && focus > 0 ? color.accent : ok ? color.success : color.ink,
                  fontWeight: isFocus && focus > 0.5 ? 600 : 400,
                  opacity: focusLine !== undefined && focus > 0 && !isFocus ? 1 - 0.55 * focus : 1,
                }}
              >
                {l || ' '}
              </div>
            );
          })}
        </div>
        </div>
      </div>
    </div>
  );
};
