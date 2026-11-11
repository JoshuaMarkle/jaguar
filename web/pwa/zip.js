// Zip files for "Download project" / "Open project": written stored (no compression, so no
// library), read stored or deflated (DecompressionStream). Names are UTF-8.

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(b) {
  let c = 0xffffffff;
  for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** DOS time and date of a JS Date (local time, 2 s steps). */
function dos(d) {
  return [(d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1),
    ((Math.max(1980, d.getFullYear()) - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate()];
}

/** [{ name, data: Uint8Array, date?: Date }] -> zip bytes. */
export function writeZip(entries) {
  const enc = new TextEncoder();
  const parts = [], central = [];
  let off = 0;
  for (const e of entries) {
    const name = enc.encode(e.name), crc = crc32(e.data), [tm, dt] = dos(e.date ?? new Date());
    const h = new DataView(new ArrayBuffer(30));
    h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true);
    h.setUint16(8, 0, true); h.setUint16(10, tm, true); h.setUint16(12, dt, true);
    h.setUint32(14, crc, true); h.setUint32(18, e.data.length, true); h.setUint32(22, e.data.length, true);
    h.setUint16(26, name.length, true); h.setUint16(28, 0, true);
    const c = new DataView(new ArrayBuffer(46));
    c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true); c.setUint16(8, 0x0800, true);
    c.setUint16(10, 0, true); c.setUint16(12, tm, true); c.setUint16(14, dt, true); c.setUint32(16, crc, true);
    c.setUint32(20, e.data.length, true); c.setUint32(24, e.data.length, true); c.setUint16(28, name.length, true);
    c.setUint32(42, off, true);
    parts.push(new Uint8Array(h.buffer), name, e.data);
    central.push(new Uint8Array(c.buffer), name);
    off += 30 + name.length + e.data.length;
  }
  const size = central.reduce((s, p) => s + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, entries.length, true); end.setUint16(10, entries.length, true);
  end.setUint32(12, size, true); end.setUint32(16, off, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(all.reduce((s, p) => s + p.length, 0));
  let k = 0;
  for (const p of all) { out.set(p, k); k += p.length; }
  return out;
}

async function inflate(data) {
  const s = new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
  return new Uint8Array(await new Response(s).arrayBuffer());
}

/** zip bytes -> [{ name, data }] (folders left out). Throws on anything it cannot read. */
export async function readZip(bytes) {
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let e = bytes.length - 22;
  while (e >= 0 && v.getUint32(e, true) !== 0x06054b50) e--;
  if (e < 0) throw new Error('not a zip file');
  const n = v.getUint16(e + 10, true);
  let c = v.getUint32(e + 16, true);
  const dec = new TextDecoder(), out = [];
  for (let i = 0; i < n; i++) {
    if (v.getUint32(c, true) !== 0x02014b50) throw new Error('damaged zip file');
    const method = v.getUint16(c + 10, true), crc = v.getUint32(c + 16, true);
    const csize = v.getUint32(c + 20, true), nlen = v.getUint16(c + 28, true);
    const xlen = v.getUint16(c + 30, true), klen = v.getUint16(c + 32, true), local = v.getUint32(c + 42, true);
    const name = dec.decode(bytes.subarray(c + 46, c + 46 + nlen));
    c += 46 + nlen + xlen + klen;
    if (name.endsWith('/')) continue;
    const start = local + 30 + v.getUint16(local + 26, true) + v.getUint16(local + 28, true);
    const raw = bytes.subarray(start, start + csize);
    let data;
    if (method === 0) data = raw.slice();
    else if (method === 8) data = await inflate(raw);
    else throw new Error(`${name}: compression method ${method} is not supported`);
    if (crc32(data) !== crc) throw new Error(`${name}: damaged (checksum)`);
    out.push({ name, data });
  }
  return out;
}
