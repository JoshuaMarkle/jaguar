import React, { useMemo } from 'react';
import { useCurrentFrame } from 'remotion';
import { data, type PathShape } from '../data';
import { beat, sceneById } from '../storyboard';
import { color, motion, space, type } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';
import { Eyebrow } from '../components/Eyebrow';
import { KineticHeadline } from '../components/KineticHeadline';
import { HullView3D, type HullInput } from '../components/HullView3D';
import { At, Stage, useLayout } from './common';

/** Which Latin-hypercube samples to show, by evaluation index (all in search_path_hulls.json "spread"). */
const SHOW = [28, 9, 70, 0];
/** Side flare per shown shape, deg. Illustrative: the shape search holds flare fixed ([hull] flare_deg). */
const FLARE = [10, 3, 14, 6];
/** Frames the first shape holds once everything has loaded, then each later shape. */
const FIRST_HOLD = 36;
const HOLD = 24;
const MORPH = 16;
/** Hull turntable: starting azimuth, deg, and turn rate, deg per frame, from the moment it fades in. */
const AZ0 = 20;
const AZ_RATE = 0.1;

export const Parameterize: React.FC<{ end: number }> = ({ end }) => {
  const f = useCurrentFrame();
  const { W, M } = useLayout();
  const sc = sceneById('parameterize');
  const c = sc.copy;
  const shapes = useMemo(() => SHOW.map((i) => data.shapeOpt.spread.find((s) => s.index === i) as PathShape), []);
  const hulls: HullInput[] = useMemo(() => shapes.map((s, i) => ({ hull: s.hull, depth: s.depth_in, flare: FLARE[i] })), [shapes]);
  const h0 = beat(sc, 'hull');
  // Hold each shape, then blend to the next (an interpolation between two real samples).
  const P = HOLD + MORPH;
  const local = f - h0 - FIRST_HOLD;
  const morph = local < 0 ? 0 : Math.min(shapes.length - 1, Math.floor(local / P) + progress(local - Math.floor(local / P) * P, 0, MORPH, easeInOut));
  const i0 = Math.floor(morph);
  const i1 = Math.min(shapes.length - 1, i0 + 1);
  const t = morph - i0;
  const val = (g: (s: PathShape) => number) => lerp(g(shapes[i0]), g(shapes[i1]), t);
  const m = (s: PathShape) => s.shape.metrics as NonNullable<PathShape['shape']['metrics']>;
  const factors = c.factors as string[];
  const rows: Array<[string, string]> = [
    [factors[0], `${val((s) => s.shape.length_in).toFixed(1)} in`],
    [factors[1], `${val((s) => s.shape.beam_in).toFixed(1)} in`],
    [factors[2], `${val((s) => m(s).entry_angle_deg).toFixed(1)}°`],
    [factors[3], `${val((s) => m(s).rocker_bow_in).toFixed(1)} / ${val((s) => m(s).rocker_stern_in).toFixed(1)} in`],
    [factors[4], `${lerp(FLARE[i0], FLARE[i1], t).toFixed(1)}°`],
  ];
  const hullIn = progress(f, h0, motion.dur.md);
  const sampleNo = shapes[Math.round(morph)].index + 1;
  return (
    <Stage end={end}>
      <At x={M} y={M}>
        <Eyebrow start={beat(sc, 'headline')}>{c.eyebrow as string}</Eyebrow>
        <div style={{ height: space.md }} />
        <KineticHeadline lines={c.headline as string[]} typeName="heading1" start={beat(sc, 'headline') + 1} weightFrom={300} dur={motion.dur.xs} weightDur={motion.dur.sm} wordStagger={1} lineStagger={0} />
      </At>
      <At x={M} y={480} w={620}>
        {rows.map(([k2, v], i) => (
          <div
            key={k2}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'baseline',
              height: 72,
              borderBottom: `1px solid ${color.hairlineSoft}`,
              opacity: progress(f, h0 + 2 + i * 2, motion.dur.sm),
            }}
          >
            <span style={{ fontSize: type.body.size, color: color.muted }}>{k2}</span>
            <span style={{ fontSize: type.heading4.size, fontWeight: type.heading4.weight, fontVariantNumeric: 'tabular-nums' }}>{v}</span>
          </div>
        ))}
        <div style={{ marginTop: space.lg, fontSize: type.caption.size, color: color.faint, opacity: progress(f, h0 + 2 + rows.length * 2 + motion.dur.sm, motion.dur.sm), fontVariantNumeric: 'tabular-nums' }}>
          {c.caption as string} Sample {sampleNo}.
        </div>
      </At>
      <At x={760} y={170} style={{ opacity: hullIn }}>
        <HullView3D width={W - 760} height={640} hulls={hulls} morph={morph} solid={0} wire={2} edge="ink" azimuth={AZ0 + AZ_RATE * Math.max(0, f - h0)} elevation={30} frameLength={270} zoom={1.0} fov={10} ringEvery={6} girthEvery={3} />
      </At>
    </Stage>
  );
};
