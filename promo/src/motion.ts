import { Easing, interpolate } from 'remotion';
import { motion } from './tokens';

export const easeOut = Easing.bezier(...motion.easeOut);
export const easeInOut = Easing.bezier(...motion.easeInOut);

/** 0..1 over [start, start + dur] frames, clamped, eased (ease-out by default). */
export function progress(frame: number, start: number, dur: number, easing: (t: number) => number = easeOut): number {
  if (dur <= 0) return frame >= start ? 1 : 0;
  return interpolate(frame, [start, start + dur], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing,
  });
}

export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Piecewise-linear lookup of y(x) on increasing xs (web/index.html interp()). */
export function interp(xs: number[], ys: number[], x: number): number {
  if (x <= xs[0]) return ys[0];
  if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
  let i = 1;
  while (xs[i] < x) i++;
  const t = (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
  return ys[i - 1] + t * (ys[i] - ys[i - 1]);
}
