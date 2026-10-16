import React, { useMemo } from 'react';
import { useCurrentFrame } from 'remotion';
import { data } from '../data';
import { beat, sceneById, FPS } from '../storyboard';
import { color, motion, radius, space, type } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';
import { RaceView, type RaceCamera } from '../components/RaceView';
import { canoeOutline, posAt, raceState } from '../race';
import { At, Stage, useLayout } from './common';

/** Playback speed, race seconds per video second (constant, before and after the zoom). */
const RATE = 3;
/** Follow-camera scale, px per metre. */
const TRACK_SC = { landscape: 80, vertical: 72 };
/** While following, the camera looks this far ahead along the path (half way to it), m. */
const LOOK_AHEAD_M = 4;

type StatItem = { label: string; value: string; sub?: string };
const Stat: React.FC<StatItem & { w: number }> = ({ label, value, sub, w }) => (
  <div style={{ width: w }}>
    <div style={{ fontSize: type.caption.size, color: color.muted, fontWeight: type.caption.weight, lineHeight: type.caption.lineHeight }}>{label}</div>
    <div style={{ fontSize: type.heading2.size, fontWeight: type.heading2.weight, lineHeight: 1.1, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>{value}</div>
    <div style={{ fontSize: type.caption.size, color: color.muted, lineHeight: type.caption.lineHeight, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap', height: type.caption.size * 1.33 }}>
      {sub ?? ''}
    </div>
  </div>
);

const Stats: React.FC<{ items: StatItem[]; cols: number; w: number; opacity: number; card?: boolean; footer?: string }> = ({ items, cols, w, opacity, card, footer }) => (
  <div style={{ opacity }}>
  <div
    style={{
      display: 'grid',
      gridTemplateColumns: `repeat(${cols}, ${w}px)`,
      rowGap: space.lg,
      padding: card ? `${space.md}px ${space.lg}px` : 0,
      marginLeft: card ? -space.lg : 0,
      background: card ? 'rgba(255,255,255,0.92)' : undefined,
      borderRadius: radius.md,
    }}
  >
    {items.map((s) => (
      <Stat key={s.label} {...s} w={w} />
    ))}
    {footer ? (
      <div style={{ gridColumn: `1 / span ${cols}`, fontSize: type.caption.size, color: color.muted, marginTop: -space.xs }}>{footer}</div>
    ) : null}
  </div>
  </div>
);

export const RaceScene: React.FC<{ end: number }> = ({ end }) => {
  const f = useCurrentFrame();
  const { vertical, W, H, M } = useLayout();
  const sc = sceneById('race');
  const c = sc.copy;
  const e = data.race.slalom;
  const B = (n: string) => beat(sc, n);
  const outline = useMemo(() => canoeOutline(data.shapeRace.winner_hull), []);

  // Race clock: integrate the playback rate frame by frame (still before 'go', x10 racing,
  // easing to x3 across the zoom, x3 while following).
  const rateAt = (i: number) => (i < B('go') ? 0 : RATE);
  const clock = useMemo(() => {
    const out: number[] = [0];
    for (let i = 0; i < sc.seconds * FPS + 1; i++) out.push(out[i] + rateAt(i) / FPS);
    return out;
  }, []);
  const t = clock[Math.max(0, Math.min(clock.length - 1, f))];
  const st = raceState(e, t);

  // Course panel before the zoom: the whole course at equal scale.
  const fitW = 111; // m: from behind the start line round buoy 8
  // Lane plus open water above and below, so the course sits in the panel with room around it.
  const fitH = data.race.lane_width_m + 12;
  const rect0 = vertical
    ? (() => {
        const h = H - 360 - 280;
        return { x: M, y: 360, w: Math.round((fitH * h) / fitW), h };
      })()
    : (() => {
        // Header, course and stats as one block, centred in the frame above the "Optimize for" bar.
        const w = W - 2 * M;
        const h = Math.round((w * fitH) / fitW / 8) * 8;
        const block = 64 + h + space.xxl + 176;
        const above = H - space.xxl - 64 - space.xxl;
        return { x: M, y: Math.round(((above - block) / 2 + 64) / 8) * 8, w, h };
      })();
  const sc0 = vertical ? rect0.h / fitW : rect0.w / fitW;
  const cam0: RaceCamera = { X: fitW / 2 - 3, Y: 0, sc: sc0, rot: vertical ? 90 : 0 };

  // Zoom: the panel grows to the full width and down to just above the stats, the scale goes to
  // the follow scale (log-linear) and the centre moves onto the canoe, looking a little ahead
  // along its path (a smooth function of distance, so the camera never jumps).
  const z = progress(f, B('zoom'), B('track') - B('zoom'), easeInOut);
  const statsY0 = rect0.y + rect0.h + space.xxl;
  const statsY1 = H - space.xxl - 196;
  const rect1 = vertical ? { x: 0, y: 0, w: W, h: H } : { x: 0, y: 0, w: W, h: statsY1 - space.xl };
  const rect = { x: lerp(rect0.x, rect1.x, z), y: lerp(rect0.y, rect1.y, z), w: lerp(rect0.w, rect1.w, z), h: lerp(rect0.h, rect1.h, z) };
  const ahead = posAt(e, st.s + LOOK_AHEAD_M);
  const focus: [number, number] = [lerp(st.pos[0], ahead[0], 0.5), lerp(st.pos[1], ahead[1], 0.5)];
  const cam: RaceCamera = {
    X: lerp(cam0.X, focus[0], z),
    Y: lerp(cam0.Y, focus[1], z),
    sc: Math.exp(lerp(Math.log(sc0), Math.log(vertical ? TRACK_SC.vertical : TRACK_SC.landscape), z)),
    rot: cam0.rot,
  };

  const headerOut = 1 - progress(f, B('zoom'), motion.dur.lg, easeInOut);
  const draw = progress(f, B('course'), motion.dur.xl, easeInOut);
  const buoys = progress(f, B('course') + motion.dur.sm, motion.dur.xl);
  const canoeIn = progress(f, B('course') + motion.dur.lg, motion.dur.md);
  const statsIn = progress(f, B('course') + motion.dur.md, motion.dur.lg);

  const turning = st.seg && st.seg.radius_m != null && st.seg.mark != null;
  const items: StatItem[] = [
    { label: c.time as string, value: `${st.t.toFixed(1)} s`, sub: `of ${e.time_s.toFixed(1)} s, ×${RATE}` },
    { label: c.speed as string, value: `${st.v.toFixed(2)} m/s` },
    { label: c.distance as string, value: `${st.s.toFixed(0)} m`, sub: `of ${e.distance_m.toFixed(0)} m` },
    {
      label: c.now as string,
      value: turning ? `${c.buoy} ${(st.seg!.mark as number) + 1}` : (c.straight as string),
      sub: turning ? `R ${st.seg!.radius_m!.toFixed(1)} m, ${Math.round(st.seg!.angle_deg ?? 0)}°` : '',
    },
  ];

  return (
    <Stage end={end}>
      <RaceView
        width={W}
        height={H}
        rect={rect}
        cam={cam}
        event={e}
        laneWidth={data.race.lane_width_m}
        clearance={data.race.buoy_clearance_m}
        state={st}
        outline={outline}
        draw={draw}
        buoys={buoys}
        canoe={canoeIn}
        grid={z}
        frame={draw}
      />
      <At x={M} y={vertical ? 248 : rect0.y - 64} w={W - 2 * M} style={{ opacity: draw * headerOut }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: space.md, flexWrap: 'wrap' }}>
          <span style={{ fontSize: type.title.size, fontWeight: type.title.weight }}>{c.event as string}</span>
          <span style={{ fontSize: type.body.size, color: color.muted }}>{c.crew as string}</span>
        </div>
      </At>
      {/* Live stats: under the course, gliding down to the bottom as the view zooms. */}
      <At x={M} y={lerp(statsY0, statsY1, z)}>
        <Stats items={items} cols={4} w={448} opacity={statsIn} />
      </At>
    </Stage>
  );
};
