import React, { useMemo } from 'react';
import { useCurrentFrame } from 'remotion';
import { data } from '../data';
import { beat, sceneById } from '../storyboard';
import { chart, color, motion, space, type } from '../tokens';
import { easeInOut, progress } from '../motion';
import { KineticHeadline } from '../components/KineticHeadline';
import { ChartPanel, type Dot } from '../components/ChartPanel';
import { CheckBars, type CheckRow } from '../components/CheckBars';
import { StatusBadge } from '../components/StatusBadge';
import { At, Stage, useLayout } from './common';

/**
 * Optimize and check as one working view. Left: the shape number and best hull weight count
 * through the shape search, then the left becomes the check bars. Right, throughout: the winning
 * hull's wall grid on the Candidates chart (weight vs D/C), the best canoe ringed.
 */
/** Check bars: placed so the last row lines up with the bottom of the chart's plot frame. */
const BARS_Y = 324;

export const Optimize: React.FC<{ end: number }> = ({ end }) => {
  const f = useCurrentFrame();
  const { W, M } = useLayout();
  const sc = sceneById('optimize');
  const c = sc.copy;
  const B = (n: string) => beat(sc, n);
  const opt = data.shapeOpt;
  const w = opt.winner;
  const g = opt.gates;

  // Shape search: evaluation number by frame (ease-in so the fast early drops can be seen).
  const inc = opt.incumbents;
  const run0 = B('searchRun');
  const runN = B('runEnd') - B('searchRun');
  const total = opt.shapes_evaluated;
  const u = progress(f, run0, runN, (t) => t);
  const ev = 1 + (total - 1) * u * u;
  const foundAt = inc.map((p) => run0 + runN * Math.sqrt(Math.max(0, p.index) / (total - 1)));
  const weight = inc.slice(1).reduce((s, p, k) => s + (p.hull_weight_lb - inc[k].hull_weight_lb) * progress(f, foundAt[k + 1], 6, easeInOut), inc[0].hull_weight_lb);

  const dots: Dot[] = useMemo(
    () => opt.winner_walls.filter((k) => k[1] !== null).map((k) => ({ x: k[0], y: k[1] as number, state: k[2] ? ('pass' as const) : ('fail' as const) })),
    [],
  );
  const best = progress(f, B('winner'), motion.dur.md);

  const allIn = progress(f, B('content'), motion.dur.md);
  const allOut = 1 - progress(f, B('out'), motion.dur.md, easeInOut);
  const numbersOut = 1 - progress(f, B('checks'), motion.dur.sm, easeInOut);
  const checksOn = f >= B('checks');

  const labels = c.labels as string[];
  const name: Record<string, string> = { bending_tension: labels[0], bending_compression: labels[1], shear: labels[2], punching: labels[3], biaxial: labels[4] };
  const rows: CheckRow[] = [
    ...w.checks.map((k) => ({ kind: 'dc' as const, label: name[k.name] ?? k.name, dc: k.dc, governs: k.name === w.governing_check })),
    { kind: 'min', label: labels[5], value: w.freeboard_in, min: g.freeboard_min_in, unit: 'in', scaleMax: 8 },
    { kind: 'min', label: labels[6], value: w.gm_in, min: g.gm_min_in, unit: 'in', scaleMax: 3 },
  ];

  const left = { x: M, w: 720 };
  const ch = { x: 880, y: 160, w: W - 880 - space.xl, h: 776 };
  const pad = { l: 120, r: space.md, t: space.md, b: 96 };
  const xd: [number, number] = [55, 230];
  const yd: [number, number] = [0, 1.4];
  const rx = ch.x + pad.l + ((w.hull_weight_lb - xd[0]) / (xd[1] - xd[0])) * (ch.w - pad.l - pad.r);
  const ry = ch.y + pad.t + (1 - (w.dc - yd[0]) / (yd[1] - yd[0])) * (ch.h - pad.t - pad.b);
  const numStyle: React.CSSProperties = { fontSize: type.heading2.size, fontWeight: type.heading2.weight, lineHeight: 1, fontVariantNumeric: 'tabular-nums' };
  const ofStyle: React.CSSProperties = { fontSize: type.heading4.size, color: color.muted, fontWeight: type.bodyLg.weight };
  const label = (t: string) => <div style={{ fontSize: type.body.size, color: color.muted, marginBottom: space.sm }}>{t}</div>;

  return (
    <Stage end={end}>
      {/* Titles first, at the size of the parameterize headline; "Optimize." rises out as "Check." comes in. */}
      <At x={M} y={M} style={{ opacity: allOut }}>
        {checksOn ? (
          <KineticHeadline lines={[c.checkEyebrow as string]} typeName="heading1" start={B('checks') + 4} weightFrom={300} dur={motion.dur.sm} />
        ) : (
          <KineticHeadline lines={[c.eyebrow as string]} typeName="heading1" start={1} weightFrom={300} dur={motion.dur.sm} exitAt={B('checks') - motion.dur.md} />
        )}
      </At>
      <div style={{ opacity: allIn * allOut }}>

        {numbersOut > 0 ? (
          <div style={{ opacity: numbersOut }}>
            <At x={left.x} y={340} w={left.w}>
              {label(c.shapesLabel as string)}
              <div style={{ display: 'flex', alignItems: 'baseline', gap: space.md }}>
                <span style={numStyle}>{Math.round(ev)}</span>
                <span style={ofStyle}>{c.shapeOf as string}</span>
              </div>
              <div style={{ marginTop: space.sm, fontSize: type.body.size, color: color.muted }}>{c.scale as string}</div>
            </At>
            <At x={left.x} y={600} w={left.w}>
              {label(c.weightLabel as string)}
              <div style={{ display: 'flex', alignItems: 'baseline', gap: space.md }}>
                <span style={numStyle}>{weight.toFixed(1)}</span>
                <span style={ofStyle}>lb</span>
              </div>
            </At>
          </div>
        ) : null}

        {checksOn ? (
          <>
            <At x={left.x} y={BARS_Y}>
              <CheckBars rows={rows} gate={g.dc_search} start={B('bars')} width={left.w} labelWidth={250} valueWidth={270} rowHeight={72} />
            </At>
          </>
        ) : null}

        <At x={ch.x} y={ch.y}>
          <ChartPanel
            width={ch.w}
            height={ch.h}
            x={{ domain: xd, label: 'hull weight, lb' }}
            y={{ domain: yd, label: 'D/C (demand / capacity)', ticks: [0, 0.5, 1] }}
            series={[{ kind: 'scatter', dots, count: dots.length * Math.min(1, ev / total), radius: chart.dot * 1.2 }]}
            gates={[{ y: g.dc_search }]}
            winner={{ x: w.hull_weight_lb, y: w.dc, visible: best }}
            legend={[
              { color: color.success, label: 'passes', dot: true },
              { color: color.candidateFail, label: 'fails', dot: true },
              { color: color.error, label: `gate ${g.dc_search}`, dash: [...chart.gateDash] },
            ]}
            padding={pad}
          />
        </At>
        <At x={rx + 40} y={ry + 24} style={{ opacity: best, fontSize: type.body.size, whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums', background: 'rgba(255,255,255,0.92)', padding: `${space.xxs}px ${space.xs}px`, borderRadius: 4 }}>
          <div style={{ fontWeight: type.title.weight }}>{c.bestLabel as string}</div>
          <div style={{ color: color.muted }}>
            {w.hull_weight_lb.toFixed(1)} lb, D/C {w.dc.toFixed(3)}
          </div>
        </At>
        {/* Verified: the badge alone, inside the chart's top-right corner. */}
        <At x={ch.x + ch.w - pad.r - space.md} y={ch.y + pad.t + space.md} style={{ transform: 'translateX(-100%)' }}>
          <StatusBadge status="success" label={c.badge as string} start={B('badge')} size="md" enter="grow" origin="right center" />
        </At>
      </div>
    </Stage>
  );
};
