import React from 'react';
import { useCurrentFrame } from 'remotion';
import { chart, color, font, hairline, motion, space, type } from '../tokens';
import { easeInOut, progress } from '../motion';

/** A D/C check: the bar fills to demand / capacity; it must stay left of the gate. */
export type DcRow = { kind: 'dc'; label: string; dc: number; governs?: boolean; note?: string };
/** A minimum gate (freeboard, GM): the bar fills to the value; it must reach past the minimum. */
export type MinRow = { kind: 'min'; label: string; value: number; min: number; unit: string; scaleMax: number; decimals?: number };
export type CheckRow = DcRow | MinRow;

type Props = {
  rows: CheckRow[];
  /** dc_search gate (0.93 in every example). */
  gate: number;
  start?: number;
  width: number;
  /** D/C axis maximum. */
  dcMax?: number;
  labelWidth?: number;
  valueWidth?: number;
  rowHeight?: number;
};

// web/index.html dcShade(): green = margin, yellow = within 15% of the gate, red = over the gate.
const dcShade = (dc: number, gate: number) => {
  const m = gate - dc;
  return m < 0 ? color.error : m < 0.15 * gate ? color.warning : color.success;
};

/**
 * D/C bars with the gate line, in the app's Checks-tab language: one bar per check, the gate as a
 * dashed red line, the governing check in heading weight. Minimum gates (freeboard, GM) get their
 * own scale and a tick at the minimum. Bars fill one after another.
 */
export const CheckBars: React.FC<Props> = ({ rows, gate, start = 0, width, dcMax = 1.0, labelWidth = 400, valueWidth = 360, rowHeight = 64 }) => {
  const frame = useCurrentFrame();
  const track = width - labelWidth - valueWidth;
  const barH = 20;
  const gateIn = progress(frame, start, motion.dur.md);
  const dcRows = rows.filter((r) => r.kind === 'dc');
  const dcBlockH = dcRows.length * rowHeight;
  return (
    <div style={{ position: 'relative', width, fontFamily: font.sans, letterSpacing: 0, color: color.ink }}>
      {rows.map((r, i) => {
        const p = progress(frame, start + motion.dur.sm + i * motion.stagger.item * 2, motion.dur.lg, easeInOut);
        const appear = progress(frame, start + i * motion.stagger.item, motion.dur.md);
        let fillW: number;
        let fillC: string;
        let valueText: string;
        let tick: number | null = null;
        if (r.kind === 'dc') {
          fillW = (Math.min(r.dc, dcMax) / dcMax) * track * p;
          fillC = dcShade(r.dc, gate);
          valueText = r.dc.toFixed(3);
        } else {
          fillW = (Math.min(r.value, r.scaleMax) / r.scaleMax) * track * p;
          fillC = r.value >= r.min ? color.success : color.error;
          valueText = `${r.value.toFixed(r.decimals ?? 2)} ${r.unit}`;
          tick = (r.min / r.scaleMax) * track;
        }
        const strong = r.kind === 'dc' && r.governs;
        const gap = r.kind === 'min' && rows[i - 1]?.kind === 'dc' ? space.xl : 0;
        return (
          <div key={r.label} style={{ display: 'flex', alignItems: 'center', height: rowHeight, marginTop: gap, opacity: appear }}>
            <div style={{ width: labelWidth, fontSize: type.body.size, fontWeight: strong ? type.heading4.weight : type.body.weight, color: strong ? color.ink : color.inkSoft }}>
              {r.label}
            </div>
            <div style={{ position: 'relative', width: track, height: barH }}>
              <div style={{ position: 'absolute', left: 0, top: 0, width: track, height: barH, background: color.canvasSoft }} />
              <div style={{ position: 'absolute', left: 0, top: 0, width: fillW, height: barH, background: fillC }} />
              {tick !== null ? (
                <div style={{ position: 'absolute', left: tick - hairline, top: -space.xs, width: 2 * hairline + 2, height: barH + 2 * space.xs, background: color.error }} />
              ) : null}
            </div>
            <div style={{ width: valueWidth, paddingLeft: space.lg, fontSize: type.body.size, fontVariantNumeric: 'tabular-nums', display: 'flex', gap: space.md, whiteSpace: 'nowrap' }}>
              <span style={{ fontWeight: strong ? type.heading4.weight : type.body.weight, opacity: Math.min(1, Math.max(0, (p - 0.6) / 0.4)) }}>{valueText}</span>
              {r.kind === 'dc' && r.governs ? <span style={{ color: color.muted, opacity: progress(frame, start + motion.dur.lg + i * motion.stagger.item * 2, motion.dur.md) }}>governs</span> : null}
              {r.kind === 'min' ? <span style={{ color: color.muted }}>min {r.min.toFixed(r.decimals ?? 2)}</span> : null}
            </div>
          </div>
        );
      })}
      {/* Gate line over the D/C rows. */}
      <svg
        width={track}
        height={dcBlockH + space.xxl}
        style={{ position: 'absolute', left: labelWidth, top: -space.xxl, overflow: 'visible', opacity: gateIn }}
      >
        <line
          x1={(gate / dcMax) * track}
          x2={(gate / dcMax) * track}
          y1={space.xxl - space.sm}
          y2={dcBlockH + space.xxl}
          stroke={color.error}
          strokeWidth={2}
          strokeDasharray={chart.gateDash.join(' ')}
        />
        <text x={(gate / dcMax) * track} y={space.xxl - space.xl} textAnchor="middle" fontSize={type.caption.size} fill={color.muted} fontFamily={font.sans}>
          gate {gate}
        </text>
      </svg>
    </div>
  );
};
