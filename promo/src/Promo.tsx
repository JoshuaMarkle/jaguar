import React from 'react';
import { AbsoluteFill, Sequence, useCurrentFrame } from 'remotion';
import { color, frame as F, motion, space } from './tokens';
import { beat, sceneById, timedById, timeline, FPS } from './storyboard';
import { easeInOut, progress } from './motion';
import { SegmentedTabs } from './components/SegmentedTabs';
import { RaceScene } from './scenes/Race';
import { End, Title } from './scenes/Type';
import { Parameterize } from './scenes/Parameterize';
import { Optimize } from './scenes/Optimize';
import { Analysis } from './scenes/Analysis';

/** Scene components by storyboard id. Each gets its length in frames (`end`) for its exit. */
const SCENES: Record<string, React.FC<{ end: number }>> = {
  title: Title,
  parameterize: Parameterize,
  optimize: Optimize,
  race: RaceScene,
  analysis: Analysis,
  end: End,
};

export const SceneById: React.FC<{ id: string }> = ({ id }) => {
  const C = SCENES[id];
  if (!C) return <AbsoluteFill style={{ background: color.canvas }} />;
  return <C end={Math.round(sceneById(id).seconds * FPS)} />;
};

const TAB_OPTION_W = 280;
const TAB_LABEL_W = 200;
const TAB_H = 64;
/** Control width: label, gap, track (3 options, 4 px gaps, 4 px padding each side). */
const TAB_W = TAB_LABEL_W + space.lg + 3 * TAB_OPTION_W + 2 * 4 + 2 * 4;
/**
 * How far the control zooms at the centre. 2x would be 2,200 px wide, wider than the frame, so it
 * stops where it fills the frame inside the margins.
 */
const TAB_ZOOM = Math.min(2, (1920 - 2 * space.section) / TAB_W);

/**
 * "Optimize for" along the bottom, held across optimize and check on min weight. Once the check
 * has faded out it moves to the centre and zooms in, slides to min race time, goes back to its
 * place, and stays there while the race comes in; it leaves as the race view zooms.
 */
const OptimizeFor: React.FC<{ focusAt: number; switchAt: number; backAt: number; leaveAt: number }> = ({ focusAt, switchAt, backAt, leaveAt }) => {
  const f = useCurrentFrame();
  const c = sceneById('race').copy;
  const o = progress(f, 0, motion.dur.md) * (1 - progress(f, leaveAt, motion.dur.lg, easeInOut));
  const z = progress(f, focusAt, motion.dur.lg, easeInOut) * (1 - progress(f, backAt, motion.dur.lg, easeInOut));
  const W = 1920;
  const H = 1080;
  const restX = F.landscape.margin;
  const restY = H - space.xxl - TAB_H;
  const dx = (W - TAB_W) / 2 - restX;
  const dy = (H - TAB_H) / 2 - restY;
  return (
    <AbsoluteFill style={{ pointerEvents: 'none' }}>
      <div
        style={{
          position: 'absolute',
          left: restX,
          top: restY,
          width: TAB_W,
          opacity: o,
          transform: `translate(${dx * z}px, ${dy * z}px) scale(${1 + (TAB_ZOOM - 1) * z})`,
          transformOrigin: 'center',
        }}
      >
        <SegmentedTabs label={c.control as string} labelWidth={TAB_LABEL_W} options={c.options as string[]} from={0} to={2} switchAt={switchAt} optionWidth={TAB_OPTION_W} />
      </div>
    </AbsoluteFill>
  );
};

export const Promo: React.FC = () => {
  const opt = timedById('optimize');
  const race = timedById('race');
  return (
    <AbsoluteFill style={{ background: color.canvas }}>
      {timeline.map((s) => {
        const C = SCENES[s.id];
        return (
          <Sequence key={s.id} from={s.from} durationInFrames={s.durationInFrames} name={s.id}>
            <C end={s.durationInFrames} />
          </Sequence>
        );
      })}
      <Sequence from={opt.from} durationInFrames={race.from + beat(race, 'track') - opt.from} name="optimize for">
        <OptimizeFor focusAt={beat(opt, 'focus')} switchAt={beat(opt, 'switch')} backAt={beat(opt, 'back')} leaveAt={race.from - opt.from + beat(race, 'zoom')} />
      </Sequence>
    </AbsoluteFill>
  );
};
