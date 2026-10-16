import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { color, font, frame as F, motion } from '../tokens';
import { easeInOut, progress } from '../motion';

export function useLayout() {
  const { width, height } = useVideoConfig();
  const vertical = height > width;
  return { vertical, W: width, H: height, M: vertical ? F.vertical.margin : F.landscape.margin };
}

/** White stage. Everything fades out over the last `exit` frames of [0, end). */
export const Stage: React.FC<{ children: React.ReactNode; end?: number; exit?: number; background?: string }> = ({
  children,
  end,
  exit = motion.dur.sm,
  background = color.canvas,
}) => {
  const frame = useCurrentFrame();
  const o = end === undefined ? 1 : 1 - progress(frame, end - exit, exit, easeInOut);
  return (
    <AbsoluteFill style={{ background, fontFamily: font.sans, letterSpacing: 0, color: color.ink }}>
      <AbsoluteFill style={{ opacity: o }}>{children}</AbsoluteFill>
    </AbsoluteFill>
  );
};

/** Absolutely positioned box. */
export const At: React.FC<{ x: number; y: number; w?: number; h?: number; children: React.ReactNode; style?: React.CSSProperties }> = ({ x, y, w, h, children, style }) => (
  <div style={{ position: 'absolute', left: x, top: y, width: w, height: h, ...style }}>{children}</div>
);
