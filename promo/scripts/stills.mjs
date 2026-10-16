#!/usr/bin/env node
// Render review stills in one bundle: node scripts/stills.mjs Kit-Type:8,20,60 Kit-Data:30,119 ...
// Writes out/stills/<composition>-<frame>.png. Honors REMOTION_BROWSER and REMOTION_GL like the CLI config.
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { bundle } from '@remotion/bundler';
import { renderStill, selectComposition } from '@remotion/renderer';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outDir = path.join(root, 'out/stills');
fs.mkdirSync(outDir, { recursive: true });
const jobs = process.argv.slice(2).map((a) => {
  const [id, frames] = a.split(':');
  return { id, frames: (frames || '0').split(',').map(Number) };
});
const serveUrl = await bundle({ entryPoint: path.join(root, 'src/index.ts'), publicDir: path.join(root, 'public') });
const chromiumOptions = { gl: process.env.REMOTION_GL || 'angle' };
const browserExecutable = process.env.REMOTION_BROWSER || null;
for (const { id, frames } of jobs) {
  const composition = await selectComposition({ serveUrl, id, chromiumOptions, browserExecutable });
  for (const frame of frames) {
    const output = path.join(outDir, `${id}-${frame}.png`);
    await renderStill({ composition, serveUrl, frame, output, chromiumOptions, browserExecutable, overwrite: true });
    console.log(output);
  }
}
