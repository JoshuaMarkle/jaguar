import React from 'react';
import { useCurrentFrame, useVideoConfig } from 'remotion';
import { color, font, motion, radius, space, type } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';

type Props = {
  title: string;
  tagline: string;
  /** Frame the band starts rising. */
  start?: number;
  /** Frame the band starts sinking back out (so the last frame is the empty white canvas of frame 0). */
  exitAt?: number;
  /** Band height as a share of the frame height. */
  share?: number;
};

/**
 * DESIGN.md footer: a full-width ink band with {rounded.md} top corners, white wordmark at display
 * scale and a faint-ink tagline. It slides up from the bottom edge; with exitAt it slides back down
 * so the video can loop to its white first frame.
 */
export const InkFooter: React.FC<Props> = ({ title, tagline, start = 0, exitAt, share = 0.5 }) => {
  const frame = useCurrentFrame();
  const { height, width } = useVideoConfig();
  const bandH = Math.round((height * share) / 8) * 8;
  const rise = 1 - progress(frame, start, motion.dur.xl);
  const sink = exitAt === undefined ? 0 : progress(frame, exitAt, motion.dur.lg, easeInOut);
  const y = (rise + sink) * (bandH + 8);
  const textIn = progress(frame, start + motion.dur.md, motion.dur.lg);
  const margin = width > height ? space.sectionLg : space.section;
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        bottom: 0,
        width,
        height: bandH,
        transform: `translateY(${y}px)`,
        background: color.ink,
        borderTopLeftRadius: radius.md,
        borderTopRightRadius: radius.md,
        fontFamily: font.sans,
        letterSpacing: 0,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        padding: `0 ${margin}px`,
        boxSizing: 'border-box',
      }}
    >
      <div
        style={{
          fontSize: type.display.size * 1.5,
          lineHeight: type.display.lineHeight,
          fontWeight: type.display.weight,
          color: color.onPrimary,
          opacity: textIn,
          transform: `translateY(${lerp(space.lg, 0, textIn)}px)`,
        }}
      >
        {title}
      </div>
      <div
        style={{
          marginTop: space.lg,
          fontSize: type.bodyLg.size,
          lineHeight: type.bodyLg.lineHeight,
          fontWeight: type.bodyLg.weight,
          color: color.faint,
          opacity: progress(frame, start + motion.dur.lg, motion.dur.lg),
        }}
      >
        {tagline}
      </div>
    </div>
  );
};
