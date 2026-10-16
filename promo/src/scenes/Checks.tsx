import React from 'react';
import { data } from '../data';
import { beat, sceneById } from '../storyboard';
import { space } from '../tokens';
import { Eyebrow } from '../components/Eyebrow';
import { KineticHeadline } from '../components/KineticHeadline';
import { CheckBars, type CheckRow } from '../components/CheckBars';
import { StatusBadge } from '../components/StatusBadge';
import { At, Stage, useLayout } from './common';

export const Checks: React.FC<{ end: number }> = ({ end }) => {
  const { M, W } = useLayout();
  const sc = sceneById('checks');
  const c = sc.copy;
  const w = data.shapeOpt.winner;
  const g = data.shapeOpt.gates;
  const labels = c.labels as string[];
  const name: Record<string, string> = {
    bending_tension: labels[0],
    bending_compression: labels[1],
    shear: labels[2],
    punching: labels[3],
    biaxial: labels[4],
  };
  const rows: CheckRow[] = [
    ...w.checks.map((k) => ({ kind: 'dc' as const, label: name[k.name] ?? k.name, dc: k.dc, governs: k.name === w.governing_check })),
    { kind: 'min', label: labels[5], value: w.freeboard_in, min: g.freeboard_min_in, unit: 'in', scaleMax: 8 },
    { kind: 'min', label: labels[6], value: w.gm_in, min: g.gm_min_in, unit: 'in', scaleMax: 2 },
  ];
  const bx = 790;
  return (
    <Stage end={end}>
      <At x={M} y={M - space.xxl + space.sm}>
        <Eyebrow start={beat(sc, 'headline')}>{c.eyebrow as string}</Eyebrow>
      </At>
      <At x={M} y={M + space.md}>
        <KineticHeadline lines={c.headline as string[]} typeName="heading2" start={beat(sc, 'headline') + 4} weightFrom={300} />
      </At>
      <At x={M} y={M + 260}>
        <StatusBadge status="success" label={c.badge as string} start={beat(sc, 'badge')} />
        <div style={{ height: space.md }} />
        <div style={{ maxWidth: 520 }}>
          <Eyebrow start={beat(sc, 'badge') + 6} typeName="body">
            {c.badgeNote as string}
          </Eyebrow>
        </div>
      </At>
      <At x={bx} y={M + space.xxl}>
        <CheckBars rows={rows} gate={g.dc_search} start={beat(sc, 'bars')} width={W - bx - M + space.xl} labelWidth={360} valueWidth={330} rowHeight={72} />
      </At>
    </Stage>
  );
};
