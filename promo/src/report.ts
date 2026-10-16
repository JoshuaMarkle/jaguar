// Typed access to src/generated/report-data.json (made by scripts/extract-report.mjs from
// promo/data/search_space_report.html, the explorer's "Canoe Search Space" report).
import raw from './generated/report-data.json';

/** One Latin-hypercube shape: 7 free parameters, race time s, valid, best reachable GM in. */
export type Shape = [number, number, number, number, number, number, number, number | null, number, number | null];

export type Report = {
  names: string[];
  /** Index of each free parameter in the 11-parameter list (slices.v rows use that list). */
  free: number[];
  counts: { n: number; valid: number };
  lo: number[];
  hi: number[];
  shapes: Shape[];
  trends: Array<{ name: string; lo: number; hi: number; median: Array<[number, number]> }>;
  sens: Array<{ name: string; drop: number }>;
  topk: Array<{ name: string; share: number }>;
  gmsweep: Array<{ gate: number; race: number; validFrac: number }>;
  catcount: Array<{ cat: string; count: number }>;
  slices: { steps: number; v: number[][]; race: Array<number | null>; valid: number[]; center: number[] };
  terrain: { dims: number[]; steps: number; race: number[]; valid: number[]; v: number[][] };
};

export const report = raw as unknown as Report;

/** The report's short parameter names. */
export const SHORT: Record<string, string> = {
  length_in: 'length',
  beam_in: 'beam',
  draft_in: 'draft',
  fore_wp: 'bow fineness',
  section: 'section',
  fore_rocker: 'rocker',
  fore_len_frac: 'taper',
};
