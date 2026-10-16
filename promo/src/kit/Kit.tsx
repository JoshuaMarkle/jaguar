// Component kit sheets: each component on its own, fed real data, for stills review.
// `npx remotion still Kit-Type out/kit-type-60.png --frame=60` etc.
import React from 'react';
import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { data } from '../data';
import { sceneById } from '../storyboard';
import { color, font, frame as F, space, type } from '../tokens';
import { easeInOut, progress } from '../motion';
import { KineticHeadline } from '../components/KineticHeadline';
import { Eyebrow } from '../components/Eyebrow';
import { MetricCounter } from '../components/MetricCounter';
import { StatusBadge } from '../components/StatusBadge';
import { CheckBars, type CheckRow } from '../components/CheckBars';
import { ChartPanel, type Dot } from '../components/ChartPanel';
import { SegmentedTabs } from '../components/SegmentedTabs';
import { HullView3D } from '../components/HullView3D';
import { TerminalWindow } from '../components/TerminalWindow';
import { InkFooter } from '../components/InkFooter';

const M = F.landscape.margin;

const Sheet: React.FC<{ children: React.ReactNode; label: string }> = ({ children, label }) => (
  <AbsoluteFill style={{ background: color.canvas, fontFamily: font.sans }}>
    <div style={{ position: 'absolute', left: M, top: space.xxl, fontSize: type.caption.size, color: color.faint }}>{label}</div>
    {children}
  </AbsoluteFill>
);

/** KineticHeadline, Eyebrow, MetricCounter, StatusBadge. */
export const KitType: React.FC = () => {
  const cold = sceneById('coldOpen').copy.lines as string[];
  const title = sceneById('title').copy;
  const off1 = data.off1.winner;
  const opt = data.shapeOpt.winner;
  return (
    <Sheet label="Kit: type. KineticHeadline, Eyebrow, MetricCounter, StatusBadge">
      <div style={{ position: 'absolute', left: M, top: 160 }}>
        <Eyebrow start={0}>{sceneById('parameterize').copy.eyebrow as string}</Eyebrow>
        <div style={{ height: space.md }} />
        <KineticHeadline lines={[cold[1]]} typeName="display" start={4} weightFrom={300} />
        <div style={{ height: space.lg }} />
        <KineticHeadline lines={[title.subtitle as string]} typeName="bodyLg" start={18} mode="lines" />
      </div>
      <div style={{ position: 'absolute', left: M, top: 560, display: 'flex', gap: 160, alignItems: 'flex-end' }}>
        <MetricCounter from={off1.hull_weight_lb} to={opt.hull_weight_lb} start={36} duration={36} decimals={1} unit="lb" label="Hull weight, off1 to shape search" typeName="display" appear={24} />
        <MetricCounter from={0} to={opt.verify_stations} start={40} duration={30} decimals={0} unit="stations" label="Verified at" typeName="heading1" appear={28} />
      </div>
      <div style={{ position: 'absolute', left: M, top: 860 }}>
        <StatusBadge status="success" label="Verified" note={`D/C ${opt.verify_dc.toFixed(3)} at ${opt.verify_stations} stations`} start={76} />
      </div>
    </Sheet>
  );
};

/** CheckBars, ChartPanel (scatter + line), SegmentedTabs. */
export const KitData: React.FC = () => {
  const f = useCurrentFrame();
  const opt = data.shapeOpt.winner;
  const g = data.shapeOpt.gates;
  const labels = sceneById('checks').copy.labels as string[];
  const name: Record<string, string> = {
    bending_tension: labels[0],
    bending_compression: labels[1],
    shear: labels[2],
    punching: labels[3],
    biaxial: labels[4],
  };
  const rows: CheckRow[] = [
    ...opt.checks.map((c) => ({ kind: 'dc' as const, label: name[c.name], dc: c.dc, governs: c.name === opt.governing_check })),
    { kind: 'min', label: labels[5], value: opt.freeboard_in, min: g.freeboard_min_in, unit: 'in', scaleMax: 8 },
    { kind: 'min', label: labels[6], value: opt.gm_in, min: g.gm_min_in, unit: 'in', scaleMax: 2 },
  ];
  // off1 wall grid on the app's Candidates chart: hull weight vs D/C, gate, winner ring.
  const cands = data.off1.candidates;
  const dots: Dot[] = cands.filter((c) => c[1] !== null).map((c) => ({ x: c[0], y: Math.min(c[1] as number, 1.4), state: c[2] ? 'pass' : 'fail' }));
  const shown = progress(f, 10, 70, easeInOut) * dots.length;
  const w = data.off1.winner;
  const options = sceneById('race').copy.options as string[];
  return (
    <Sheet label="Kit: data. CheckBars, ChartPanel, SegmentedTabs">
      <div style={{ position: 'absolute', left: M, top: 200 }}>
        <CheckBars rows={rows} gate={g.dc_search} start={4} width={860} labelWidth={330} valueWidth={300} rowHeight={60} />
      </div>
      <div style={{ position: 'absolute', left: 1040, top: 150 }}>
        <ChartPanel
          width={800}
          height={600}
          x={{ domain: [70, 280], label: 'hull weight, lb' }}
          y={{ domain: [0, 1.45], label: 'D/C', ticks: [0, 0.5, 1] }}
          series={[{ kind: 'scatter', dots, count: shown }]}
          gates={[{ y: data.off1.gates.dc_search, opacity: progress(f, 6, 15) }]}
          winner={{ x: w.hull_weight_lb, y: w.dc, visible: progress(f, 84, 12) }}
          legend={[{ color: color.error, label: `D/C gate ${data.off1.gates.dc_search}`, dash: [10, 8] }]}
          chrome={progress(f, 0, 12)}
        />
      </div>
      <div style={{ position: 'absolute', left: M, top: 880 }}>
        <SegmentedTabs label={sceneById('race').copy.control as string} options={options} from={0} to={2} switchAt={60} optionWidth={280} start={0} />
      </div>
    </Sheet>
  );
};

/** HullView3D: off1 wireframe, morph to the shape-search winner, accent edge on the winner. */
export const KitHull: React.FC = () => {
  const f = useCurrentFrame();
  const off1 = { hull: data.off1.hull, depth: data.off1.winner.depth_in, flare: data.off1.winner.flare_deg };
  const win = { hull: data.shapeOpt.winner_hull, depth: data.shapeOpt.winner.depth_in, flare: data.shapeOpt.winner.flare_deg };
  const hulls = React.useMemo(() => [off1, win], []);
  const m = progress(f, 30, 45, easeInOut);
  const solid = progress(f, 80, 20);
  return (
    <Sheet label="Kit: hull. HullView3D, off1 to shape-search winner (blend of two real hulls), accent edge = winner">
      <div style={{ position: 'absolute', left: 0, top: 120 }}>
        <HullView3D width={1920} height={760} hulls={hulls} morph={m} solid={solid} wire={1} edge={m >= 1 ? 'accent' : 'ink'} azimuth={28} elevation={26} frameLength={250} zoom={1} />
      </div>
      <div style={{ position: 'absolute', left: M, bottom: 96, display: 'flex', gap: 120, fontSize: type.body.size, color: color.muted }}>
        <span>off1, {data.off1.winner.length_in.toFixed(0)} x {data.off1.winner.beam_in.toFixed(1)} in</span>
        <span>shape search, {data.shapeOpt.winner.length_in.toFixed(0)} x {data.shapeOpt.winner.beam_in.toFixed(1)} in</span>
        <span style={{ color: color.faint }}>{m > 0 && m < 1 ? 'Comparison, not the search path.' : ''}</span>
      </div>
    </Sheet>
  );
};

/** TerminalWindow and InkFooter. */
export const KitEnd: React.FC = () => {
  const lines = data.off1.terminal.replace(/\n$/, '').split('\n');
  const winnerLine = lines.findIndex((l) => l.startsWith('WINNER'));
  const verified = lines.findIndex((l) => l.trim().startsWith('VERIFIED'));
  const end = sceneById('end').copy;
  return (
    <Sheet label="Kit: end. TerminalWindow, InkFooter">
      <div style={{ position: 'absolute', left: M, top: 120 }}>
        <TerminalWindow
          title="~/jaguar"
          command={data.off1.command}
          output={lines}
          start={2}
          width={1680}
          height={560}
          focusLine={winnerLine}
          focusAt={70}
          successLines={[verified]}
        />
      </div>
      <InkFooter title={end.wordmark as string} tagline={sceneById('title').copy.subtitle as string} start={96} share={0.42} />
    </Sheet>
  );
};
