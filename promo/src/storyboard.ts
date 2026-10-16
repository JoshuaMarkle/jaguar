// Every duration and every word of on-screen copy lives here, so the cut can be re-timed or
// re-worded without touching a scene. Numbers in copy are formatted from src/data.ts (JAGUAR
// output), never typed in. Times in seconds at 30 fps; `beats` are offsets inside a scene.
import { data } from './data';

const opt = data.shapeOpt.winner;
const slalom = data.race.slalom;

const innerGrid = opt.search_space.filter((s) => s.count !== null).reduce((n, s) => n * (s.count as number), 1);
const stations = opt.convergence?.stations ?? [opt.verify_stations];

export const FPS = 30;

export type Scene = {
  id: string;
  /** Length in seconds. Starts are cumulative (see `timeline`). */
  seconds: number;
  /** Beat start times inside the scene, seconds. */
  beats: Record<string, number>;
  /** What is on screen and which data feeds it. */
  notes: string;
  copy: Record<string, string | string[]>;
};

export const scenes: Scene[] = [
  {
    id: 'title',
    seconds: 3,
    beats: { wordmark: 0.15, subtitle: 0.65, out: 2.7 },
    notes: 'Wordmark at display scale, light subtitle. Near-empty white beat.',
    copy: { wordmark: 'JAGUAR.', subtitle: 'Advanced concrete canoe hull optimizer' },
  },
  {
    id: 'parameterize',
    seconds: 6.5,
    beats: { headline: 0.1, hull: 0.9 },
    notes:
      "Wireframe hull reshapes through 4 of the shape search's Latin-hypercube samples (search_path_hulls.json spread), with length, beam, entry angle and rocker read out; the flare per shape is illustrative (flare is a fixed [hull] value in the search).",
    copy: {
      eyebrow: 'Parameterize.',
      headline: ['Every hull is a', 'set of numbers.'],
      factors: ['Length', 'Beam', 'Entry angle', 'Rocker, bow / stern', 'Flare'],
      caption: `Shapes from the search's first ${data.shapeOpt.latin_hypercube_samples} samples.`,
    },
  },
  {
    id: 'optimize',
    seconds: 14.1,
    // The title comes in first, the chart and counters at content. The points finish at runEnd; the
    // best canoe is picked a second later (winner). badge: Verified, top right in the chart, a second
    // after the check values have loaded. out: everything fades away, then
    // the "Optimize for" control (overlay in Promo.tsx) moves to the centre (focus), switches to
    // min race time (switch) and goes back to its place (back) before the race comes in.
    beats: { content: 0.5, searchRun: 0.7, runEnd: 2.7, winner: 3.7, checks: 5.5, bars: 5.9, badge: 9.3, out: 11.3, focus: 11.8, switch: 12.5, back: 13.4 },
    notes:
      "One working view. Left: shapes checked and best hull weight count through the shape search's real incumbents, then the left becomes the D/C check bars and the Verified badge. Right: the winning hull's 686-wall grid (shape_opt_winner_walls_result.json) streams onto weight vs D/C, the best canoe ringed. 'Optimize for' sits at the bottom on min weight (overlay in Promo.tsx); everything here fades out, then the control moves to the centre, switches to min race time and returns before the race comes in.",
    copy: {
      eyebrow: 'Optimize.',
      checkEyebrow: 'Check.',
      shapesLabel: 'Shapes checked',
      shapeOf: `of ${data.shapeOpt.shapes_evaluated}`,
      scale: `${innerGrid} walls each.`,
      weightLabel: 'Best hull weight',
      bestLabel: 'Best canoe',
      labels: ['Tension', 'Compression', 'Shear', 'Punching', 'Biaxial', 'Freeboard', 'Stability, GM'],
      badge: 'Verified',
      badgeNote: `D/C ${opt.verify_dc.toFixed(3)} at ${stations.join(', ').replace(/, (\d+)$/, ' and $1')} stations`,
    },
  },
  {
    id: 'race',
    seconds: 13,
    beats: { course: 0.15, go: 1.4, zoom: 6.4, track: 8.4 },
    notes:
      "The Optimize-for control switches to min race time and the app's Race tab builds over the top: the race-time winner on the Symposium co-ed 200 m slalom (race_shape_race.json), at x3 throughout, with time, speed, distance and the current turn live and the speed trace under it; then the view zooms smoothly onto the canoe and follows it through buoys 4 and 5.",
    copy: {
      eyebrow: 'Optimize for racing.',
      control: 'Optimize for',
      options: ['min weight', 'min drag', 'min race time'],
      event: slalom.name,
      crew: `${slalom.paddlers} paddlers, ${slalom.crew_lb.toFixed(0)} lb`,
      time: 'Race time',
      speed: 'Speed',
      distance: 'Distance',
      now: 'Now',
      straight: 'Straight',
      buoy: 'Buoy',
      caveat: 'Illustrative crews and paddler power.',
    },
  },
  {
    id: 'analysis',
    seconds: 14.7,
    // A big "Analysis." title first, then cut starts, s: an accelerating montage (30, 27, 24 ... 6 frames) shrinking toward the centre,
    // then every view bursts out into a wall (held about 1 s) and the camera dials in on the
    // race-time terrain, which holds about 2.5 s before the cut.
    beats: {
      title: 0,
      bending: 2.5,
      shear: 3.5,
      stress: 4.4,
      dc: 5.2,
      material: 5.9,
      tradeoff: 6.5,
      cloud: 7.0,
      trends: 7.467,
      freeboard: 7.867,
      importance: 8.233,
      share: 8.533,
      gmsweep: 8.8,
      volume: 9.067,
      slices: 9.3,
      failure: 9.533,
      mosaic: 9.733,
      dive: 11.2,
    },
    notes: 'Fast, accelerating montage of the analysis views: along-hull bending, shear, stress and D/C (off1 winner, 2M), the lab strength fit, then the search space report (trade-offs, 3D cloud, trends, freeboard, importance, explained share, GM sweep, valid volume, slices, why shapes fail). All of them at once, then a dive into the race-time terrain.',
    copy: {
      title: 'Analysis.',
      subtitle: 'See your canoe from all angles.',
      cuts: ['bending', 'shear', 'stress', 'dc', 'material', 'tradeoff', 'cloud', 'trends', 'freeboard', 'importance', 'share', 'gmsweep', 'volume', 'slices', 'failure'],
      labels: [
        'Bending moment.',
        'Shear.',
        'Stress.',
        'Demand over capacity.',
        'Material strength.',
        'Trade-offs.',
        'Search space.',
        'Trends.',
        'Freeboard.',
        'What matters.',
        'Explained variance.',
        'Stability sweep.',
        'Valid volume.',
        'Slices.',
        'Why shapes fail.',
      ],
      mosaic: 'Every view.',
      terrain: 'Find your best race time.',
      terrainNote: '30 × 30 shapes, bow fineness × taper.',
      caveat: 'Illustrative crews and paddler power.',
    },
  },
  {
    id: 'end',
    seconds: 3,
    beats: { in: 0.1, glitch: 1.8, gone: 2.45 },
    notes: 'JAGUAR. centred on white, then a short digital glitch takes it out. The last frames are the empty white first frame, so the video loops.',
    copy: { wordmark: 'JAGUAR.' },
  },
];

export type TimedScene = Scene & { from: number; durationInFrames: number };

/** Scene start / duration in frames, in order. */
export const timeline: TimedScene[] = (() => {
  let at = 0;
  return scenes.map((s) => {
    const from = at;
    const durationInFrames = Math.round(s.seconds * FPS);
    at += durationInFrames;
    return { ...s, from, durationInFrames };
  });
})();

export const sceneById = (id: string) => scenes.find((s) => s.id === id) as Scene;
export const timedById = (id: string) => timeline.find((s) => s.id === id) as TimedScene;
export const totalFrames = timeline.reduce((n, s) => n + s.durationInFrames, 0);

/** Beat time in frames. */
export const beat = (s: Scene, name: string) => Math.round((s.beats[name] ?? 0) * FPS);
