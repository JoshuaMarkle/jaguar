// One instance of JAGUAR's engine (jaguar-wasm/src/lib.rs) on its own MemFS.
// `call(name, head, body)` frames the input (u32 header length, header JSON, body bytes), calls
// the export and unframes the answer into { head, body }. A panic in Rust aborts the instance
// (wasm32-wasip1 has no unwinding): `call` then throws EngineCrash with what Rust printed to
// stderr, and the instance is dead: make a new Engine.

import { MemFS, wasiImports } from './wasi.js';

export class EngineCrash extends Error {}

/** Compile jaguar.wasm from `url` (streaming when the server says application/wasm). */
export async function compileUrl(url) {
  try {
    return await WebAssembly.compileStreaming(fetch(url));
  } catch (e) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return WebAssembly.compile(await r.arrayBuffer());
  }
}

export class Engine {
  /** `module`: a compiled WebAssembly.Module of jaguar_wasm.wasm. `onProgress(text)`: the
   * `jaguar.progress` import (a fixed-hull run's Progress JSON). */
  static async create(module, { fs = new MemFS(), onProgress = () => {}, log = () => {} } = {}) {
    const e = new Engine();
    e.fs = fs;
    e.stderr = '';
    let memory = null;
    const dec = new TextDecoder();
    const wasi = wasiImports(fs, () => memory, {
      out: t => log(t),
      err: t => { e.stderr = (e.stderr + t).slice(-4000); log(t); },
    });
    const jaguar = {
      progress: (ptr, len) => onProgress(dec.decode(new Uint8Array(memory.buffer, ptr, len))),
    };
    e.instance = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi, jaguar });
    memory = e.instance.exports.memory;
    e.instance.exports._initialize?.();
    e.dead = false;
    return e;
  }

  call(name, head = {}, body = new Uint8Array(0)) {
    if (this.dead) throw new EngineCrash('the engine stopped after an earlier error');
    const x = this.instance.exports;
    const h = new TextEncoder().encode(JSON.stringify(head));
    const b = typeof body === 'string' ? new TextEncoder().encode(body) : new Uint8Array(body);
    const len = 4 + h.length + b.length;
    let packed;
    try {
      const ptr = x.jaguar_alloc(len);
      const mem = new Uint8Array(x.memory.buffer, ptr, len);
      new DataView(mem.buffer, ptr, 4).setUint32(0, h.length, true);
      mem.set(h, 4);
      mem.set(b, 4 + h.length);
      this.stderr = '';
      packed = BigInt.asUintN(64, BigInt(x[name](ptr, len)));
    } catch (err) {
      this.dead = true;
      const said = this.stderr.trim().split('\n').filter(Boolean).slice(-3).join(' ');
      throw new EngineCrash(said || `engine stopped: ${err.message ?? err}`);
    }
    const optr = Number(packed >> 32n), olen = Number(packed & 0xffffffffn);
    const out = new Uint8Array(x.memory.buffer, optr, olen).slice();
    x.jaguar_free(optr, olen);
    const n = new DataView(out.buffer).getUint32(0, true);
    return { head: JSON.parse(new TextDecoder().decode(out.subarray(4, 4 + n))), body: out.subarray(4 + n) };
  }

  /** `call` for a JSON answer; `{error}` throws. */
  json(name, head) {
    const r = this.call(name, head).head;
    if (r && typeof r === 'object' && 'error' in r && Object.keys(r).length === 1) throw new Error(r.error);
    return r;
  }
}
