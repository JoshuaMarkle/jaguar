// Assemble the phone app (docs/SPEC.md "Phone app") into a static folder for any HTTPS host:
//   node web/pwa/build.mjs <jaguar_wasm.wasm> <out dir>
// (`make pwa` builds the wasm and runs this.) No dependencies beyond Node itself.
//
// out/index.html           web/index.html + the shim, manifest and icon links (no other change)
// out/vendor/three.min.js  as served by `jaguar serve`
// out/pwa/*.js             the shim and its workers
// out/jaguar.wasm          the engine (jaguar-wasm)
// out/starter/*            first-launch canoe folder, as the desktop app's (launch.rs)
// out/icons/*.png          app icons, drawn here
// out/manifest.webmanifest, out/sw.js (versioned by a hash of everything above)

import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..');
const [wasmPath, out] = process.argv.slice(2);
if (!wasmPath || !out) {
  console.error('usage: node web/pwa/build.mjs <jaguar_wasm.wasm> <out dir>');
  process.exit(2);
}
const version = /^version\s*=\s*"([^"]+)"/m.exec(readFileSync(join(repo, 'Cargo.toml'), 'utf8'))[1];

rmSync(out, { recursive: true, force: true });
const files = [];
const put = (rel, data) => {
  mkdirSync(dirname(join(out, rel)), { recursive: true });
  writeFileSync(join(out, rel), data);
  files.push(rel);
};

// The page: same as the desktop's, with the shim first (it must patch fetch before the page's
// scripts run) and three.js relative (the app may live in a sub-folder of its host).
let html = readFileSync(join(repo, 'web', 'index.html'), 'utf8');
const three = '<script src="/vendor/three.min.js"></script>';
if (!html.includes(three)) throw new Error('web/index.html: three.js tag not found');
const head = [
  '<link rel="manifest" href="manifest.webmanifest">',
  '<meta name="theme-color" content="#ffffff">',
  '<meta name="apple-mobile-web-app-capable" content="yes">',
  '<meta name="mobile-web-app-capable" content="yes">',
  '<meta name="apple-mobile-web-app-title" content="JAGUAR">',
  '<link rel="icon" type="image/png" href="icons/icon-192.png">',
  '<link rel="apple-touch-icon" href="icons/icon-180.png">',
  '<script src="pwa/shim.js"></script>',
].join('\n');
html = html.replace('<title>JAGUAR</title>', `<title>JAGUAR</title>\n${head}`);
html = html.replace(three, '<script src="vendor/three.min.js"></script>');
put('index.html', html);

put('vendor/three.min.js', readFileSync(join(repo, 'web', 'vendor', 'three.min.js')));
for (const f of ['shim.js', 'wasi.js', 'engine.js', 'api-worker.js', 'compute-worker.js', 'store.js', 'zip.js']) {
  put(`pwa/${f}`, readFileSync(join(here, f)));
}
put('jaguar.wasm', readFileSync(wasmPath));
put('starter/canoe.toml', readFileSync(join(repo, 'packaging', 'starter', 'canoe.toml')));
put('starter/off1_hull.json', readFileSync(join(repo, 'examples', 'off1_hull.json')));
put('starter/mix_tests.csv', readFileSync(join(repo, 'data', 'mix_tests.csv')));

for (const s of [180, 192, 512]) put(`icons/icon-${s}.png`, icon(s, false));
put('icons/maskable-512.png', icon(512, true));
put('manifest.webmanifest', JSON.stringify({
  name: 'JAGUAR',
  short_name: 'JAGUAR',
  description: 'Lightest concrete canoe wall that does not crack',
  start_url: './',
  scope: './',
  display: 'standalone',
  orientation: 'any', // portrait or landscape: the layout follows (index.html phone rules)
  background_color: '#ffffff',
  theme_color: '#ffffff',
  icons: [
    { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
    { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png' },
    { src: 'icons/maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
  ],
}, null, 2));

const hash = createHash('sha256');
for (const f of files) hash.update(f).update(readFileSync(join(out, f)));
const tag = `${version}-${hash.digest('hex').slice(0, 10)}`;
const cached = ['./', ...files];
const sw = readFileSync(join(here, 'sw.js'), 'utf8')
  .replace('__VERSION__', tag)
  .replace('__FILES__', JSON.stringify(cached));
writeFileSync(join(out, 'sw.js'), sw);
writeFileSync(join(out, '.nojekyll'), '');
const wasmMb = (readFileSync(wasmPath).length / 1048576).toFixed(1);
console.log(`JAGUAR phone app ${tag} -> ${out} (${files.length + 1} files, jaguar.wasm ${wasmMb} MB)`);

// ---------- icon: a white canoe (plan view) on an ink squircle; maskable = full bleed ----------
function icon(size, maskable) {
  const ss = 4, n = size * ss;
  const ink = [0x14, 0x14, 0x14], white = [0xff, 0xff, 0xff];
  const px = new Uint8Array(size * size * 4);
  const r = maskable ? 0 : 0.3 * n; // squircle-ish corner radius (rounded square)
  const inBg = (x, y) => {
    if (maskable) return true;
    const cx = Math.min(Math.max(x, r), n - r), cy = Math.min(Math.max(y, r), n - r);
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
  };
  // Canoe: two circular arcs meeting at bow and stern, laid diagonally.
  const L = (maskable ? 0.56 : 0.72) * n, B = 0.27 * L;
  const R = (L * L / 4 + B * B / 4) / B; // arc radius through the ends and the half-beam
  const inCanoe = (x, y) => {
    const u = x - n / 2, v = y - n / 2;
    const a = (u + v) / Math.SQRT2, b = (v - u) / Math.SQRT2;
    if (Math.abs(a) > L / 2) return false;
    const off = R - B / 2;
    return (b + off) ** 2 + a * a <= R * R && (b - off) ** 2 + a * a <= R * R;
  };
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let bg = 0, fg = 0;
      for (let j = 0; j < ss; j++) for (let i = 0; i < ss; i++) {
        const sx = x * ss + i + 0.5, sy = y * ss + j + 0.5;
        if (inBg(sx, sy)) { bg++; if (inCanoe(sx, sy)) fg++; }
      }
      const k = (y * size + x) * 4, cov = bg / ss / ss, f = bg ? fg / bg : 0;
      for (let c = 0; c < 3; c++) px[k + c] = Math.round(ink[c] * (1 - f) + white[c] * f);
      px[k + 3] = Math.round(255 * cov);
    }
  }
  return png(size, size, px);
}

function png(w, h, rgba) {
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0;
    Buffer.from(rgba.buffer, y * w * 4, w * 4).copy(raw, y * (w * 4 + 1) + 1);
  }
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 6;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })), chunk('IEND', Buffer.alloc(0))]);
}

function crc32(b) {
  let c = 0xffffffff;
  for (const x of b) {
    c ^= x;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  }
  return (c ^ 0xffffffff) >>> 0;
}
