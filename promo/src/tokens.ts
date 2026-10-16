// docs/DESIGN.md as code. Every component in src/components uses only these values.
//
// Web px are scaled for a 1920 x 1080 frame (VIDEO_SCALE) with a floor of MIN_TEXT_PX so the
// smallest text stays legible when the video plays on a phone. Radii and hairlines stay at their
// literal DESIGN.md px: the small, precise corners are the point.

export const VIDEO_SCALE = 2;
export const MIN_TEXT_PX = 28;

/** Colors. Ink on canvas; structure from the tint ladder; one accent; Carbon semantics for state only. */
export const color = {
  ink: '#141414', // {colors.ink} / {colors.primary}
  inkSoft: '#262626', // {colors.ink-soft}
  muted: '#707070', // {colors.text-muted}
  faint: '#adadad', // {colors.text-faint}
  canvas: '#ffffff', // {colors.canvas}
  canvasSoft: '#f3f3f3', // {colors.canvas-soft}: 6% ink wash, segmented tracks, featured surfaces
  field: '#f0f0f0', // {colors.field}: 8% ink wash, chart grid lines (web/index.html C.grid)
  hairlineSoft: '#f0f0f0', // {colors.hairline-soft}
  hairline: '#e0e0e0', // {colors.hairline}: 1 px control and frame edges
  onPrimary: '#ffffff', // text on ink
  /** The winner. Only one blue thing on screen at a time. */
  accent: '#0066ff',
  /** Carbon green-50: pass, verified, D/C with margin. */
  success: '#24a148',
  /** Carbon yellow-30: near the limit (D/C within 15% of the gate). Fill only, never text on white. */
  warning: '#f1c21b',
  /** Carbon red-60: fail, over the gate, the gate line itself. */
  error: '#da1e28',
  /** Failing candidate dot (web/index.html marker()). */
  candidateFail: '#c8c8c8',
} as const;

export const font = {
  /** Saans substitute (DESIGN.md "Note on Font Substitutes"): Inter variable, loaded in src/font.ts. */
  sans: '"Inter", -apple-system, "Helvetica Neue", Arial, sans-serif',
  mono: '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace',
} as const;

/** Saans 652 / 456 / 300 mapped to Inter 650 / 450 / 300. */
export const weight = { heading: 650, title: 600, text: 450, light: 300 } as const;

type TypeToken = { size: number; weight: number; lineHeight: number };
const t = (webPx: number, w: number, lineHeight: number): TypeToken => ({
  size: Math.max(webPx * VIDEO_SCALE, MIN_TEXT_PX),
  weight: w,
  lineHeight,
});

/**
 * DESIGN.md hierarchy at video scale. Letter-spacing is 0 everywhere and is not a token on
 * purpose: nothing in the promo may set it. Sentence case, terminal periods on headlines.
 */
export const type = {
  display: t(80, weight.heading, 1.0),
  heading1: t(56, weight.heading, 1.0),
  heading2: t(44, weight.heading, 1.13),
  heading3: t(32, weight.heading, 1.13),
  heading4: t(24, weight.heading, 1.25),
  title: t(20, weight.title, 1.3),
  bodyLg: t(20, weight.light, 1.38),
  body: t(16, weight.text, 1.38),
  bodySm: t(14, weight.text, 1.43),
  link: t(16, weight.title, 1.38),
  label: t(12, weight.title, 1.33),
  caption: t(12, weight.text, 1.33),
  mono: t(13, weight.text, 1.5),
} as const;
export type TypeName = keyof typeof type;

/** Radii, literal px. Never a stadium pill except switches and progress tracks. */
export const radius = { none: 0, xs: 4, sm: 6, md: 8, full: 9999 } as const;

/** 1 px hairlines. */
export const hairline = 1;

/** 8 px grid with 4 px half steps. Layout positions and paddings use multiples of these. */
export const space = { xxs: 4, xs: 8, sm: 12, md: 16, lg: 24, xl: 32, xxl: 48, section: 80, sectionLg: 120 } as const;

/** Frame layout: safe margins on the 8 px grid. */
export const frame = {
  landscape: { width: 1920, height: 1080, margin: space.sectionLg },
  vertical: { width: 1080, height: 1920, margin: space.section },
  fps: 30,
} as const;

/**
 * Motion: precise and calm. Ease-out only, no overshoot, no bounce, no idle spin.
 * Durations in frames at 30 fps.
 */
export const motion = {
  dur: { xs: 6, sm: 10, md: 15, lg: 24, xl: 36 },
  /** Gap between words in a kinetic line, and between lines. */
  stagger: { word: 3, line: 8, item: 4 },
  /** Cubic-bezier ease-out (fast start, long settle), the only curve for entrances. */
  easeOut: [0.16, 1, 0.3, 1] as const,
  /** For moves between two resting states (camera, morphs, tab slide). */
  easeInOut: [0.65, 0, 0.35, 1] as const,
  /** Critically damped spring: no overshoot. */
  spring: { damping: 200, mass: 1, stiffness: 120, overshootClamping: true },
} as const;

/** 3D hull: a soft-gray solid under hairline wireframe; the winner's edges in accent. */
export const hull3d = {
  solid: '#dedede',
  wire: color.ink,
  wireOpacity: 0.28,
  edge: color.ink,
  /** three.js r155+ light units: the web UI's r128 ambient 0.55 / sun 0.65, times pi. */
  ambient: 0.55 * Math.PI,
  sun: 0.65 * Math.PI,
} as const;

/** Chart style from web/index.html (axes(), line(), marker(), ring(), legend()) at video scale. */
export const chart = {
  frame: color.hairline,
  grid: color.field,
  tick: color.muted,
  line: 1.5 * VIDEO_SCALE,
  dot: 2.5 * VIDEO_SCALE,
  winnerRing: 8 * VIDEO_SCALE,
  winnerRingWidth: 2 * VIDEO_SCALE,
  gateDash: [5 * VIDEO_SCALE, 4 * VIDEO_SCALE],
  /** Load-case greys + dashes (web/index.html CASE_STYLE). */
  caseStyle: [
    [color.ink, []],
    ['#5a5a5a', [12, 6]],
    ['#8a8a8a', [4, 6]],
  ] as Array<[string, number[]]>,
} as const;
