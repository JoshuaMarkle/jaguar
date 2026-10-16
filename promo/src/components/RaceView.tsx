import React from 'react';
import { color, font, hairline, space, type } from '../tokens';
import type { RaceEvent } from '../data';
import type { RaceState } from '../race';

// The web UI Race tab's course view (plotRace) at video scale, with a free camera: the course at
// equal scale (lane, start / finish line, buoys with clearance rings), the planned path faint, the
// part paddled in ink, and the winner's top-view outline in accent, yawed by the turn drift.
// Everything is drawn in screen space, so lines keep their width at any zoom.

export type RaceCamera = {
  /** World point at the centre of `rect`, m. */
  X: number;
  Y: number;
  /** Pixels per metre. */
  sc: number;
  /** 0: course left to right (+x right, +y up). 90: course bottom to top (+x up, +y left). */
  rot: 0 | 90;
};

type Props = {
  width: number;
  height: number;
  /** Visible panel, px. Content outside is clipped. */
  rect: { x: number; y: number; w: number; h: number };
  cam: RaceCamera;
  event: RaceEvent;
  laneWidth: number;
  clearance: number;
  state: RaceState;
  outline: Array<[number, number]>;
  /** 0..1: lane, line and planned path drawing on. */
  draw?: number;
  /** 0..1: buoys appearing in course order. */
  buoys?: number;
  /** Opacity of the canoe and paddled path. */
  canoe?: number;
  /** Opacity of the 5 m reference grid (shows motion when the camera follows). */
  grid?: number;
  /** Opacity of the hairline panel frame. */
  frame?: number;
  /** Opacity of the start / finish label, buoy numbers and scale bar. */
  labels?: number;
};

export const RaceView: React.FC<Props> = ({
  width,
  height,
  rect,
  cam,
  event,
  laneWidth,
  clearance,
  state,
  outline,
  draw = 1,
  buoys = 1,
  canoe = 1,
  grid = 0,
  frame = 1,
  labels = 1,
}) => {
  const cx = rect.x + rect.w / 2;
  const cy = rect.y + rect.h / 2;
  const S = (x: number, y: number): [number, number] =>
    cam.rot === 0 ? [cx + cam.sc * (x - cam.X), cy - cam.sc * (y - cam.Y)] : [cx - cam.sc * (y - cam.Y), cy - cam.sc * (x - cam.X)];
  // World bounds of the panel (for the grid).
  const corners = [
    [rect.x, rect.y],
    [rect.x + rect.w, rect.y],
    [rect.x, rect.y + rect.h],
    [rect.x + rect.w, rect.y + rect.h],
  ].map(([px, py]) => (cam.rot === 0 ? [cam.X + (px - cx) / cam.sc, cam.Y - (py - cy) / cam.sc] : [cam.X - (py - cy) / cam.sc, cam.Y - (px - cx) / cam.sc]));
  const wx0 = Math.min(...corners.map((c) => c[0]));
  const wx1 = Math.max(...corners.map((c) => c[0]));
  const wy0 = Math.min(...corners.map((c) => c[1]));
  const wy1 = Math.max(...corners.map((c) => c[1]));
  const half = laneWidth / 2;
  const marks = event.course.marks;
  const lx1 = Math.max(100, ...marks.filter((m) => Math.abs(m.y_m) <= half).map((m) => m.x_m)) + 5;
  const line = (pts: Array<[number, number]>) => pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('');
  // Catmull-Rom through the path points (about 1 m apart), so it stays a curve when zoomed in.
  const curve = (pts: Array<[number, number]>) => {
    if (pts.length < 3) return line(pts);
    const f = (v: number) => v.toFixed(1);
    let d = `M${f(pts[0][0])},${f(pts[0][1])}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[Math.min(pts.length - 1, i + 2)];
      d += `C${f(p1[0] + (p2[0] - p0[0]) / 6)},${f(p1[1] + (p2[1] - p0[1]) / 6)} ${f(p2[0] - (p3[0] - p1[0]) / 6)},${f(p2[1] - (p3[1] - p1[1]) / 6)} ${f(p2[0])},${f(p2[1])}`;
    }
    return d;
  };
  const path = event.path_m;
  // Planned path up to `draw`; paddled path up to the canoe.
  const nDraw = Math.max(2, Math.round(path.length * draw));
  const planned = path.slice(0, nDraw).map(([x, y]) => S(x, y));
  let acc = 0;
  const cum = path.map((p, k) => (acc += k ? Math.hypot(p[0] - path[k - 1][0], p[1] - path[k - 1][1]) : 0));
  const sc2 = (state.s / event.distance_m) * cum[cum.length - 1];
  const done = path.filter((_, k) => cum[k] <= sc2).map(([x, y]) => S(x, y));
  done.push(S(state.pos[0], state.pos[1]));
  // Canoe outline at true scale (never shorter than 24 px so it stays visible at course scale).
  const Lm = Math.max(...outline.map((p) => p[0])) * 2;
  const k = Math.max(1, 24 / (Lm * cam.sc));
  const ch = Math.cos(state.hd);
  const sh = Math.sin(state.hd);
  const hull = outline.map(([u, v]) => {
    const uu = u * k;
    const vv = v * Math.max(1, k * 0.6);
    return S(state.pos[0] + uu * ch - vv * sh, state.pos[1] + uu * sh + vv * ch);
  });
  // Grid every 5 m.
  const gl: Array<[[number, number], [number, number]]> = [];
  if (grid > 0) {
    for (let x = Math.ceil(wx0 / 5) * 5; x <= wx1; x += 5) gl.push([S(x, wy0), S(x, wy1)]);
    for (let y = Math.ceil(wy0 / 5) * 5; y <= wy1; y += 5) gl.push([S(wx0, y), S(wx1, y)]);
  }
  const bar = [5, 10, 20, 50].filter((v) => v * cam.sc <= rect.w * 0.45).find((v) => v * cam.sc >= 100) ?? 5;
  const cur = state.seg && state.seg.mark != null && state.seg.radius_m != null ? state.seg.mark : -1;
  const clipId = React.useId().replace(/:/g, '');
  const tick = type.caption.size;
  const fx = event.course.finish_x_m;
  const [la, lb] = [S(fx, half), S(fx, -half)];
  const lab =
    cam.rot === 0
      ? { x: la[0] + space.md, y: la[1] + tick + space.sm, anchor: 'start' as const }
      : { x: (la[0] + lb[0]) / 2, y: la[1] + tick + space.sm, anchor: 'middle' as const };
  return (
    <svg width={width} height={height} style={{ position: 'absolute', left: 0, top: 0, fontFamily: font.sans, letterSpacing: 0 }}>
      <defs>
        <clipPath id={`rv-${clipId}`}>
          <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} />
        </clipPath>
      </defs>
      <g clipPath={`url(#rv-${clipId})`}>
        <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} fill={color.canvas} />
        {gl.map(([a, b], i) => (
          <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={color.field} strokeWidth={hairline * 2} opacity={grid} />
        ))}
        <g opacity={Math.min(1, draw * 3)}>
          {[-half, half].map((y) => {
            const [a, b] = [S(-2, y), S(lx1, y)];
            return <line key={y} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={color.faint} strokeWidth={2} strokeDasharray="12 10" />;
          })}
          {(() => {
            const [a, b] = [S(event.course.finish_x_m, -half), S(event.course.finish_x_m, half)];
            return <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={color.ink} strokeWidth={4} />;
          })()}
        </g>
        <path d={curve(planned)} fill="none" stroke={color.hairline} strokeWidth={3} strokeLinejoin="round" strokeLinecap="round" />
        <path d={curve(done)} fill="none" stroke={color.ink} strokeWidth={3} strokeLinejoin="round" strokeLinecap="round" opacity={canoe} />
        {marks.map((m, i) => {
          const a = Math.min(1, Math.max(0, buoys * (marks.length + 2) - i));
          const [bx, by] = S(m.x_m, m.y_m);
          const rr = Math.max(5, 0.25 * cam.sc);
          return (
            <g key={i} opacity={a}>
              <circle cx={bx} cy={by} r={Math.max(rr + 3, clearance * cam.sc)} fill="none" stroke={i === cur ? color.ink : color.faint} strokeWidth={i === cur ? 2.5 : 1.5} />
              <circle cx={bx} cy={by} r={rr * (0.6 + 0.4 * a)} fill={color.ink} />
              <text x={bx + rr + space.xs} y={by - rr - space.xxs} fontSize={tick} fill={color.muted} opacity={labels}>
                {i + 1}
              </text>
            </g>
          );
        })}
        <g opacity={canoe}>
          <path d={line(hull) + 'Z'} fill={color.accent} stroke={color.ink} strokeWidth={1.5} strokeLinejoin="round" />
        </g>
        <text x={lab.x} y={lab.y} fontSize={tick} fill={color.muted} textAnchor={lab.anchor} opacity={labels * Math.min(1, draw * 2)}>
          start / finish
        </text>
        <g opacity={labels * Math.min(1, draw * 2)}>
          <line x1={rect.x + rect.w - space.lg - bar * cam.sc} x2={rect.x + rect.w - space.lg} y1={rect.y + rect.h - space.lg} y2={rect.y + rect.h - space.lg} stroke={color.ink} strokeWidth={3} />
          <text x={rect.x + rect.w - space.lg} y={rect.y + rect.h - space.lg - space.sm} fontSize={tick} fill={color.ink} textAnchor="end">
            {bar} m
          </text>
        </g>
      </g>
      <rect x={rect.x + 0.5} y={rect.y + 0.5} width={rect.w - 1} height={rect.h - 1} fill="none" stroke={color.hairline} strokeWidth={hairline} opacity={frame} />
    </svg>
  );
};
