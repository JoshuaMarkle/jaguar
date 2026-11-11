// IndexedDB for the phone app: `files` (the canoe folder and prefs, by absolute path) and `state`
// (small values by key: the run checkpoint). Browsers may clear it (iOS after weeks unused when
// not on the home screen): "Download project" is the backup.

const DB = 'jaguar';

let opening = null;
function db() {
  opening ??= new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => {
      r.result.createObjectStore('files', { keyPath: 'path' });
      r.result.createObjectStore('state');
    };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
  return opening;
}

function tx(store, mode, fn) {
  return db().then(d => new Promise((resolve, reject) => {
    const t = d.transaction(store, mode);
    const out = fn(t.objectStore(store));
    t.oncomplete = () => resolve(out?.result ?? out);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  }));
}

/** Every stored file: [{ path, data: Uint8Array, mtime: BigInt }]. */
export const allFiles = () => tx('files', 'readonly', s => s.getAll()).then(list =>
  list.map(f => ({ path: f.path, data: new Uint8Array(f.data), mtime: BigInt(f.mtime) })));

/** Apply MemFS changes ([{ path, data, mtime } | { path, removed }]) in one transaction. */
export function applyChanges(changes) {
  if (!changes.length) return Promise.resolve();
  return tx('files', 'readwrite', s => {
    for (const c of changes) {
      if (c.removed) s.delete(c.path);
      else s.put({ path: c.path, data: c.data.buffer.slice(c.data.byteOffset, c.data.byteOffset + c.data.byteLength), mtime: String(c.mtime) });
    }
  });
}

export const getState = key => tx('state', 'readonly', s => s.get(key));
export const setState = (key, value) => tx('state', 'readwrite', s => { s.put(value, key); });
export const delState = key => tx('state', 'readwrite', s => { s.delete(key); });
const prefixed = prefix => IDBKeyRange.bound(prefix, prefix + '\uffff');
/** Every value whose key starts with `prefix`, in key order. */
export const statesWith = prefix => tx('state', 'readonly', s => s.getAll(prefixed(prefix)));
export const delStatesWith = prefix => tx('state', 'readwrite', s => { s.delete(prefixed(prefix)); });
