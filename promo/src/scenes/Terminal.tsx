import React from 'react';
import { data } from '../data';
import { beat, sceneById } from '../storyboard';
import { space } from '../tokens';
import { KineticHeadline } from '../components/KineticHeadline';
import { TerminalWindow } from '../components/TerminalWindow';
import { At, Stage, useLayout } from './common';

export const Terminal: React.FC<{ end: number }> = ({ end }) => {
  const { M, W } = useLayout();
  const sc = sceneById('terminal');
  const c = sc.copy;
  const lines = data.off1.terminal.replace(/\n$/, '').split('\n');
  const winner = lines.findIndex((l) => l.startsWith('WINNER'));
  const verified = lines.findIndex((l) => l.trim().startsWith('VERIFIED'));
  return (
    <Stage end={end}>
      <At x={M} y={space.section}>
        <TerminalWindow
          title={c.title as string}
          command={c.command as string}
          output={lines}
          start={beat(sc, 'type')}
          typeRate={1.6}
          lineRate={2.2}
          width={W - 2 * M}
          height={700}
          focusLine={winner}
          focusAt={beat(sc, 'focus')}
          successLines={[verified]}
        />
      </At>
      <At x={M} y={space.section + 700 + space.xxl}>
        <KineticHeadline lines={[c.caption as string]} typeName="heading2" start={beat(sc, 'caption')} weightFrom={300} />
      </At>
    </Stage>
  );
};
