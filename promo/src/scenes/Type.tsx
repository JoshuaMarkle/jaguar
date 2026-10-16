// Typographic beats: title card, end card (with the glitch out).
import React from 'react';
import { AbsoluteFill, random, useCurrentFrame } from 'remotion';
import { beat, sceneById } from '../storyboard';
import { color, font, motion, space, type } from '../tokens';
import { progress } from '../motion';
import { KineticHeadline } from '../components/KineticHeadline';
import { Stage, useLayout } from './common';

export const Title: React.FC<{ end: number }> = () => {
  const { M, vertical } = useLayout();
  const sc = sceneById('title');
  const out = beat(sc, 'out');
  return (
    <Stage>
      <AbsoluteFill style={{ justifyContent: 'center', paddingLeft: M, paddingRight: M }}>
        <KineticHeadline
          lines={[sc.copy.wordmark as string]}
          typeName="display"
          size={vertical ? type.display.size * 1.25 : type.display.size * 1.5}
          start={beat(sc, 'wordmark')}
          weightFrom={300}
          dur={motion.dur.lg}
          exitAt={out}
        />
        <div style={{ height: space.xl }} />
        <KineticHeadline lines={[sc.copy.subtitle as string]} typeName="bodyLg" size={type.heading4.size} mode="lines" start={beat(sc, 'subtitle')} exitAt={out} />
      </AbsoluteFill>
    </Stage>
  );
};

/**
 * End card: the wordmark centred on white, then a short digital glitch: horizontal slices of the
 * word jump sideways, an accent ghost splits off, and it is gone, leaving the white first frame.
 * Deterministic (remotion random() seeded by frame).
 */
export const End: React.FC<{ end: number }> = () => {
  const f = useCurrentFrame();
  const { vertical } = useLayout();
  const sc = sceneById('end');
  const g0 = beat(sc, 'glitch');
  const g1 = beat(sc, 'gone');
  const word = sc.copy.wordmark as string;
  const size = vertical ? type.display.size * 1.25 : type.display.size * 1.5;
  const inP = progress(f, beat(sc, 'in'), motion.dur.lg);
  const glitching = f >= g0 && f < g1;
  const gone = f >= g1;
  // Intensity ramps up across the glitch; a few frames are calm in between (it stutters).
  const k = glitching ? (f - g0) / (g1 - g0) : 0;
  const calm = glitching && random(`calm-${f}`) < 0.18;
  const amp = glitching && !calm ? 12 + 140 * k * k : 0;
  const N = 9;
  const textStyle: React.CSSProperties = {
    fontFamily: font.sans,
    fontSize: size,
    lineHeight: 1,
    fontWeight: type.display.weight,
    letterSpacing: 0,
    whiteSpace: 'nowrap',
    position: 'absolute',
    left: '50%',
    top: '50%',
  };
  if (gone) return <AbsoluteFill style={{ background: color.canvas }} />;
  const ghostDx = glitching ? (random(`gx-${f}`) - 0.5) * (24 + 60 * k) : 0;
  const ghostOn = glitching && random(`go-${f}`) > 0.25;
  // A frame or two near the end where the whole word blinks out.
  const blink = glitching && k > 0.55 && random(`bl-${f}`) < 0.35;
  return (
    <AbsoluteFill style={{ background: color.canvas, overflow: 'hidden' }}>
      {ghostOn ? (
        <div style={{ ...textStyle, color: color.accent, opacity: 0.85, transform: `translate(calc(-50% + ${ghostDx}px), calc(-50% + ${(random(`gy-${f}`) - 0.5) * 8}px))` }}>{word}</div>
      ) : null}
      {blink
        ? null
        : Array.from({ length: N }, (_, i) => {
            const top = (100 * i) / N;
            const bot = 100 - (100 * (i + 1)) / N;
            const r = random(`s-${f}-${i}`);
            const dx = amp ? (r - 0.5) * 2 * amp * (r > 0.55 || r < 0.2 ? 1 : 0.15) : 0;
            return (
              <div
                key={i}
                style={{
                  ...textStyle,
                  color: color.ink,
                  opacity: inP,
                  clipPath: `inset(${top}% -20% ${bot}% -20%)`,
                  transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${(1 - inP) * space.lg}px))`,
                }}
              >
                {word}
              </div>
            );
          })}
      {/* Scan blocks: thin ink and soft bars across the word on a few frames. */}
      {glitching && random(`bar-${f}`) > 0.45
        ? Array.from({ length: 2 }, (_, i) => {
            const y = 0.5 + (random(`by-${f}-${i}`) - 0.5) * 0.22;
            const h = 4 + random(`bh-${f}-${i}`) * 18;
            return (
              <div
                key={`b${i}`}
                style={{ position: 'absolute', left: 0, right: 0, top: `${y * 100}%`, height: h, background: i ? color.canvasSoft : color.ink, opacity: i ? 1 : 0.85 }}
              />
            );
          })
        : null}
    </AbsoluteFill>
  );
};
