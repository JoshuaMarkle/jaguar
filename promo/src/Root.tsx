import React from 'react';
import { Composition } from 'remotion';
import { loadFonts } from './font';
import { frame } from './tokens';
import { scenes, FPS, totalFrames } from './storyboard';
import { Promo, SceneById } from './Promo';

loadFonts();

const L = frame.landscape;

export const Root: React.FC = () => (
  <>
    <Composition id="JaguarPromo" component={Promo} durationInFrames={totalFrames} fps={FPS} width={L.width} height={L.height} />
    {/* One composition per scene, for stills while building. */}
    {scenes.map((s) => (
      <Composition
        key={s.id}
        id={`Scene-${s.id}`}
        component={SceneById}
        defaultProps={{ id: s.id }}
        durationInFrames={Math.round(s.seconds * FPS)}
        fps={FPS}
        width={L.width}
        height={L.height}
      />
    ))}
  </>
);
