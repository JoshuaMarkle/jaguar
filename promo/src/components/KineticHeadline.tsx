import React from 'react';
import { useCurrentFrame } from 'remotion';
import { color, font, motion, type, type TypeName } from '../tokens';
import { easeInOut, lerp, progress } from '../motion';

type Props = {
  /** One entry per line. Sentence case with the terminal period, as it should read. */
  lines: string[];
  typeName?: TypeName;
  /** First frame of the reveal (relative to the enclosing Sequence). */
  start?: number;
  /** 'words': each word rises out of its line mask in turn. 'lines': each line rises as one. */
  mode?: 'words' | 'lines';
  /** Weight-axis entrance (e.g. 300 -> the token's 650). Tracking is never animated. */
  weightFrom?: number;
  /** Frames for one word or line to settle. */
  dur?: number;
  /** Frame at which the lines leave (rise out of their masks), or undefined to stay. */
  exitAt?: number;
  color?: string;
  align?: 'left' | 'center';
  maxWidth?: number;
  /** Override the token size (px) for a title card; weight and line height stay the token's. */
  size?: number;
  /** Frames between words (default motion.stagger.word) and between lines (motion.stagger.line). */
  wordStagger?: number;
  lineStagger?: number;
  /** Frames for the weight to settle (default dur + motion.dur.sm). */
  weightDur?: number;
};

/**
 * Word-by-word or line-by-line reveal. Each word rises out of a mask the height of its line and,
 * optionally, moves along the weight axis from light to heading weight while it settles.
 * Layout never moves: every word reserves its final-weight width.
 */
export const KineticHeadline: React.FC<Props> = ({
  lines,
  typeName = 'display',
  start = 0,
  mode = 'words',
  weightFrom,
  dur = motion.dur.md,
  exitAt,
  color: ink = color.ink,
  align = 'left',
  maxWidth,
  size,
  wordStagger = motion.stagger.word,
  lineStagger = motion.stagger.line,
  weightDur,
}) => {
  const frame = useCurrentFrame();
  const tk = type[typeName];
  const finalWeight = tk.weight;
  // Room below the baseline so descenders are not clipped by the line mask (line-height is 1.0).
  const pad = 0.16;
  let k = 0;
  return (
    <div
      style={{
        fontFamily: font.sans,
        fontSize: size ?? tk.size,
        lineHeight: tk.lineHeight,
        fontWeight: finalWeight,
        letterSpacing: 0,
        color: ink,
        textAlign: align,
        maxWidth,
        fontVariantNumeric: 'tabular-nums',
      }}
    >
      {lines.map((line, li) => {
        const words = mode === 'words' ? line.split(' ') : [line];
        const lineExit = exitAt === undefined ? 0 : progress(frame, exitAt + li * motion.stagger.word, motion.dur.md, easeInOut);
        return (
          <div
            key={li}
            style={{
              overflow: 'hidden',
              paddingBottom: `${pad}em`,
              marginBottom: `${-pad}em`,
            }}
          >
            <div style={{ transform: `translateY(${-lineExit * (1 + pad) * 100}%)` }}>
              {words.map((w, wi) => {
                const delay = mode === 'words' ? k++ * wordStagger : li * lineStagger;
                const p = progress(frame, start + delay + (mode === 'words' ? li * lineStagger : 0), dur);
                const wgt = weightFrom === undefined ? finalWeight : lerp(weightFrom, finalWeight, progress(frame, start + delay, weightDur ?? dur + motion.dur.sm));
                return (
                  <React.Fragment key={wi}>
                    <span style={{ position: 'relative', display: 'inline-block' }}>
                      <span style={{ visibility: 'hidden' }}>{w}</span>
                      <span
                        style={{
                          position: 'absolute',
                          left: 0,
                          top: 0,
                          whiteSpace: 'pre',
                          fontWeight: wgt,
                          transform: `translateY(${(1 - p) * (100 + pad * 100)}%)`,
                          opacity: Math.min(1, p * 1.5),
                        }}
                      >
                        {w}
                      </span>
                    </span>
                    {wi < words.length - 1 ? ' ' : null}
                  </React.Fragment>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
};
