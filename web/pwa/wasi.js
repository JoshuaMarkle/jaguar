// WASI preview1 for JAGUAR's engine (jaguar-wasm) on an in-memory file system.
// Only the calls the engine imports are implemented (see `imports()`); any other import is a
// link error at instantiate, never a silent no-op. Works in browsers, workers and Node.
//
// MemFS: absolute paths ("/JAGUAR/canoe.toml") -> { data: Uint8Array, size, mtime (ns, BigInt) },
// plus a set of directories. `changes` collects every path written, renamed or removed since
// the last `takeChanges()` (the page persists those to IndexedDB).

const E = { SUCCESS: 0, BADF: 8, EXIST: 20, INVAL: 28, ISDIR: 31, NOENT: 44, NOSYS: 52, NOTDIR: 54, NOTEMPTY: 55 };
const FILETYPE = { CHAR: 2, DIR: 3, FILE: 4 };
const O = { CREAT: 1, DIRECTORY: 2, EXCL: 4, TRUNC: 8 };
const FD_APPEND = 1;
const PREOPEN = 3; // fd of "/"

export class ProcExit extends Error {
  constructor(code) { super(`exit ${code}`); this.code = code; }
}

const now = () => BigInt(Math.round((performance.timeOrigin + performance.now()) * 1e6));

export function normalize(path) {
  const out = [];
  for (const part of path.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') out.pop(); else out.push(part);
  }
  return '/' + out.join('/');
}

const parent = p => p.slice(0, p.lastIndexOf('/')) || '/';

export class MemFS {
  constructor() {
    this.files = new Map();
    this.dirs = new Set(['/']);
    this.changes = new Set();
  }

  mkdirs(path) {
    const p = normalize(path);
    for (let d = p; !this.dirs.has(d); d = parent(d)) this.dirs.add(d);
  }

  /** Put a file (creating its folders); `mtime` in ns (BigInt), default now. */
  write(path, data, mtime) {
    const p = normalize(path);
    this.mkdirs(parent(p));
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : new Uint8Array(data);
    this.files.set(p, { data: bytes, size: bytes.length, mtime: mtime ?? now() });
    this.changes.add(p);
  }

  read(path) {
    const f = this.files.get(normalize(path));
    return f ? f.data.subarray(0, f.size) : null;
  }

  readText(path) {
    const b = this.read(path);
    return b ? new TextDecoder().decode(b) : null;
  }

  remove(path) {
    const p = normalize(path);
    if (this.files.delete(p)) this.changes.add(p);
  }

  /** Every file under `dir` (absolute paths, sorted). */
  list(dir = '/') {
    const d = normalize(dir);
    const pre = d === '/' ? '/' : d + '/';
    return [...this.files.keys()].filter(p => p.startsWith(pre)).sort();
  }

  /** Paths changed since the last call: [{ path, data, mtime } | { path, removed: true }]. */
  takeChanges() {
    const out = [...this.changes].sort().map(path => {
      const f = this.files.get(path);
      return f ? { path, data: f.data.slice(0, f.size), mtime: f.mtime } : { path, removed: true };
    });
    this.changes.clear();
    return out;
  }

  /** A copy of the files under `dir` for another instance: [{ path, data, mtime }]. */
  snapshot(dir = '/', skip = () => false) {
    return this.list(dir).filter(p => !skip(p)).map(path => {
      const f = this.files.get(path);
      return { path, data: f.data.slice(0, f.size), mtime: f.mtime };
    });
  }

  load(entries) {
    for (const e of entries) this.write(e.path, e.data, e.mtime);
  }
}

/**
 * The `wasi_snapshot_preview1` imports over `fs`. `getMemory()` returns the instance's memory
 * (known only after instantiate). stdout / stderr text goes to `out(text)` / `err(text)`.
 */
export function wasiImports(fs, getMemory, { out = () => {}, err = () => {} } = {}) {
  const fds = new Map([[0, { kind: 'stdio' }], [1, { kind: 'stdio', sink: out }], [2, { kind: 'stdio', sink: err }],
    [PREOPEN, { kind: 'dir', path: '/' }]]);
  let nextFd = 4;
  const dec = new TextDecoder();
  const enc = new TextEncoder();
  const view = () => new DataView(getMemory().buffer);
  const bytes = () => new Uint8Array(getMemory().buffer);
  const str = (ptr, len) => dec.decode(bytes().subarray(ptr, ptr + len));
  const resolve = (dirfd, ptr, len) => {
    const d = fds.get(dirfd);
    if (!d || d.kind !== 'dir') return null;
    const rel = str(ptr, len);
    return normalize(rel.startsWith('/') ? rel : d.path + '/' + rel);
  };
  const iovs = (ptr, n) => {
    const v = view(), list = [];
    for (let i = 0; i < n; i++) list.push([v.getUint32(ptr + 8 * i, true), v.getUint32(ptr + 8 * i + 4, true)]);
    return list;
  };
  const filestat = (ptr, path) => {
    const v = view();
    const f = fs.files.get(path);
    const isDir = !f && fs.dirs.has(path);
    if (!f && !isDir) return E.NOENT;
    for (let k = 0; k < 64; k += 8) v.setBigUint64(ptr + k, 0n, true);
    v.setBigUint64(ptr + 8, BigInt(hash(path)), true);
    v.setUint8(ptr + 16, isDir ? FILETYPE.DIR : FILETYPE.FILE);
    v.setBigUint64(ptr + 24, 1n, true);
    v.setBigUint64(ptr + 32, BigInt(f ? f.size : 0), true);
    const t = f ? f.mtime : 0n;
    v.setBigUint64(ptr + 40, t, true);
    v.setBigUint64(ptr + 48, t, true);
    v.setBigUint64(ptr + 56, t, true);
    return E.SUCCESS;
  };
  const grow = (f, need) => {
    if (need <= f.data.length) return;
    const n = new Uint8Array(Math.max(need, f.data.length * 2, 64));
    n.set(f.data.subarray(0, f.size));
    f.data = n;
  };

  return {
    args_get: () => E.SUCCESS,
    args_sizes_get: (argc, size) => { view().setUint32(argc, 0, true); view().setUint32(size, 0, true); return E.SUCCESS; },
    environ_get: () => E.SUCCESS,
    environ_sizes_get: (count, size) => { view().setUint32(count, 0, true); view().setUint32(size, 0, true); return E.SUCCESS; },
    clock_time_get: (id, _precision, ptr) => {
      const t = id === 0 ? now() : BigInt(Math.round(performance.now() * 1e6));
      view().setBigUint64(ptr, t, true);
      return E.SUCCESS;
    },
    random_get: (ptr, len) => {
      for (let o = 0; o < len; o += 65536) crypto.getRandomValues(bytes().subarray(ptr + o, ptr + Math.min(len, o + 65536)));
      return E.SUCCESS;
    },
    sched_yield: () => E.SUCCESS,
    proc_exit: code => { throw new ProcExit(code); },

    fd_prestat_get: (fd, ptr) => {
      if (fd !== PREOPEN) return E.BADF;
      view().setUint8(ptr, 0);
      view().setUint32(ptr + 4, 1, true);
      return E.SUCCESS;
    },
    fd_prestat_dir_name: (fd, ptr, len) => {
      if (fd !== PREOPEN) return E.BADF;
      bytes().set(enc.encode('/').subarray(0, len), ptr);
      return E.SUCCESS;
    },
    fd_fdstat_get: (fd, ptr) => {
      const d = fds.get(fd);
      if (!d) return E.BADF;
      const v = view();
      v.setUint8(ptr, d.kind === 'dir' ? FILETYPE.DIR : d.kind === 'file' ? FILETYPE.FILE : FILETYPE.CHAR);
      v.setUint16(ptr + 2, d.append ? FD_APPEND : 0, true);
      v.setBigUint64(ptr + 8, 0xffffffffffffffffn, true);
      v.setBigUint64(ptr + 16, 0xffffffffffffffffn, true);
      return E.SUCCESS;
    },
    fd_filestat_get: (fd, ptr) => {
      const d = fds.get(fd);
      if (!d) return E.BADF;
      if (d.kind === 'stdio') {
        for (let k = 0; k < 64; k += 8) view().setBigUint64(ptr + k, 0n, true);
        view().setUint8(ptr + 16, FILETYPE.CHAR);
        return E.SUCCESS;
      }
      return filestat(ptr, d.path);
    },
    path_filestat_get: (dirfd, _flags, p, len, ptr) => {
      const path = resolve(dirfd, p, len);
      return path === null ? E.BADF : filestat(ptr, path);
    },
    path_open: (dirfd, _dirflags, p, len, oflags, _rb, _ri, fdflags, fdPtr) => {
      const path = resolve(dirfd, p, len);
      if (path === null) return E.BADF;
      const isDir = fs.dirs.has(path);
      let f = fs.files.get(path);
      if (isDir) {
        if (oflags & (O.CREAT | O.TRUNC) && oflags & O.EXCL) return E.EXIST;
        const fd = nextFd++;
        fds.set(fd, { kind: 'dir', path });
        view().setUint32(fdPtr, fd, true);
        return E.SUCCESS;
      }
      if (oflags & O.DIRECTORY) return f ? E.NOTDIR : E.NOENT;
      if (f && oflags & O.CREAT && oflags & O.EXCL) return E.EXIST;
      if (!f) {
        if (!(oflags & O.CREAT)) return E.NOENT;
        if (!fs.dirs.has(parent(path))) return E.NOENT;
        f = { data: new Uint8Array(0), size: 0, mtime: now() };
        fs.files.set(path, f);
        fs.changes.add(path);
      } else if (oflags & O.TRUNC) {
        f.size = 0;
        f.mtime = now();
        fs.changes.add(path);
      }
      const fd = nextFd++;
      fds.set(fd, { kind: 'file', path, pos: 0, append: !!(fdflags & FD_APPEND) });
      view().setUint32(fdPtr, fd, true);
      return E.SUCCESS;
    },
    fd_close: fd => (fds.delete(fd) ? E.SUCCESS : E.BADF),
    fd_read: (fd, iovPtr, n, nreadPtr) => {
      const d = fds.get(fd);
      if (!d) return E.BADF;
      let total = 0;
      if (d.kind === 'file') {
        const f = fs.files.get(d.path);
        if (!f) return E.BADF;
        for (const [ptr, len] of iovs(iovPtr, n)) {
          const k = Math.max(0, Math.min(len, f.size - d.pos));
          bytes().set(f.data.subarray(d.pos, d.pos + k), ptr);
          d.pos += k;
          total += k;
          if (k < len) break;
        }
      } else if (d.kind === 'dir') return E.ISDIR;
      view().setUint32(nreadPtr, total, true);
      return E.SUCCESS;
    },
    fd_write: (fd, iovPtr, n, nwrittenPtr) => {
      const d = fds.get(fd);
      if (!d) return E.BADF;
      let total = 0;
      if (d.kind === 'stdio') {
        for (const [ptr, len] of iovs(iovPtr, n)) {
          d.sink?.(dec.decode(bytes().subarray(ptr, ptr + len)));
          total += len;
        }
      } else if (d.kind === 'file') {
        const f = fs.files.get(d.path);
        if (!f) return E.BADF;
        for (const [ptr, len] of iovs(iovPtr, n)) {
          if (d.append) d.pos = f.size;
          grow(f, d.pos + len);
          f.data.set(bytes().subarray(ptr, ptr + len), d.pos);
          d.pos += len;
          f.size = Math.max(f.size, d.pos);
          total += len;
        }
        f.mtime = now();
        fs.changes.add(d.path);
      } else return E.ISDIR;
      view().setUint32(nwrittenPtr, total, true);
      return E.SUCCESS;
    },
    fd_readdir: (fd, buf, bufLen, cookie, usedPtr) => {
      const d = fds.get(fd);
      if (!d || d.kind !== 'dir') return E.BADF;
      const pre = d.path === '/' ? '/' : d.path + '/';
      const names = new Map();
      for (const p of fs.files.keys()) if (p.startsWith(pre) && !p.slice(pre.length).includes('/')) names.set(p.slice(pre.length), FILETYPE.FILE);
      for (const p of fs.dirs) if (p !== d.path && p.startsWith(pre) && !p.slice(pre.length).includes('/')) names.set(p.slice(pre.length), FILETYPE.DIR);
      const list = [...names].sort((a, b) => (a[0] < b[0] ? -1 : 1));
      const v = view(), mem = bytes();
      let used = 0;
      for (let i = Number(cookie); i < list.length && used < bufLen; i++) {
        const [name, type] = list[i];
        const nb = enc.encode(name);
        const ent = new Uint8Array(24 + nb.length);
        const ev = new DataView(ent.buffer);
        ev.setBigUint64(0, BigInt(i + 1), true);
        ev.setBigUint64(8, BigInt(hash(pre + name)), true);
        ev.setUint32(16, nb.length, true);
        ev.setUint8(20, type);
        ent.set(nb, 24);
        const k = Math.min(ent.length, bufLen - used);
        mem.set(ent.subarray(0, k), buf + used);
        used += k;
      }
      v.setUint32(usedPtr, used, true);
      return E.SUCCESS;
    },
    path_create_directory: (dirfd, p, len) => {
      const path = resolve(dirfd, p, len);
      if (path === null) return E.BADF;
      if (fs.dirs.has(path) || fs.files.has(path)) return E.EXIST;
      if (!fs.dirs.has(parent(path))) return E.NOENT;
      fs.dirs.add(path);
      return E.SUCCESS;
    },
    path_readlink: (dirfd, p, len) => {
      const path = resolve(dirfd, p, len);
      if (path === null) return E.BADF;
      return fs.files.has(path) || fs.dirs.has(path) ? E.INVAL : E.NOENT;
    },
    path_rename: (fd1, p1, l1, fd2, p2, l2) => {
      const from = resolve(fd1, p1, l1), to = resolve(fd2, p2, l2);
      if (from === null || to === null) return E.BADF;
      const f = fs.files.get(from);
      if (!f) return fs.dirs.has(from) ? E.NOSYS : E.NOENT;
      if (fs.dirs.has(to)) return E.ISDIR;
      if (!fs.dirs.has(parent(to))) return E.NOENT;
      fs.files.delete(from);
      fs.files.set(to, f);
      fs.changes.add(from);
      fs.changes.add(to);
      return E.SUCCESS;
    },
    path_unlink_file: (dirfd, p, len) => {
      const path = resolve(dirfd, p, len);
      if (path === null) return E.BADF;
      if (fs.dirs.has(path)) return E.ISDIR;
      if (!fs.files.delete(path)) return E.NOENT;
      fs.changes.add(path);
      return E.SUCCESS;
    },
  };
}

function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}
