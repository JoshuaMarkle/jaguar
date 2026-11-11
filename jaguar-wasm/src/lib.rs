//! JAGUAR's engine for the phone app (docs/SPEC.md "Phone app"): built for `wasm32-wasip1` as a
//! reactor and run by `web/pwa/engine.js` on an in-memory file system (`web/pwa/wasi.js`). No
//! physics here: every call is a thin wrapper over the `jaguar` crate.
//!
//! One module, two roles, one instance each:
//! * **API** (`api_*`): `serve::Embedded`, the web UI's HTTP API answered in-process. The fetch
//!   shim (`web/pwa/shim.js`) sends every `/api/...` request here.
//! * **Compute** (`run_fixed`, `shape_*`): runs. A fixed hull is one call ([`run_fixed`], its
//!   progress through the `jaguar.progress` import); a shape search is replayed by
//!   [`shape_step`] while the shapes it asks for are scored by [`shape_score`] in other workers.
//!
//! ABI: `jaguar_alloc` / `jaguar_free` manage buffers. Every call takes one buffer (freed by
//! the call) and returns `(ptr << 32) | len` of a new one (freed by the caller). A buffer is a
//! frame: u32 little-endian header length, the header (JSON), then the body (raw bytes). A call
//! that fails returns the header `{"error": "..."}`.

use anyhow::{Context, Result};
use jaguar::optimize::Progress;
use jaguar::params::Params;
use jaguar::serve::{Embedded, RunOutcome};
use jaguar::shapeopt::{Prep, ShapeEval, ShapeJob, Step};
use serde::Deserialize;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::path::{Path, PathBuf};

thread_local! {
    static API: RefCell<Option<Embedded>> = const { RefCell::new(None) };
    /// The shape search being stepped or scored here: its canoe.toml text and folder, params, prep.
    static SHAPES: RefCell<Option<(String, String, Params, Prep)>> = const { RefCell::new(None) };
}

#[unsafe(no_mangle)]
pub extern "C" fn jaguar_alloc(len: usize) -> *mut u8 {
    let mut v = Vec::<u8>::with_capacity(len.max(1));
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}

/// # Safety
/// `ptr` and `len` must come from [`jaguar_alloc`] (or a call's result) and not be freed twice.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn jaguar_free(ptr: *mut u8, len: usize) {
    drop(unsafe { Vec::from_raw_parts(ptr, 0, len.max(1)) });
}

/// The input frame of a call (taken back from the caller's buffer).
fn input(ptr: *mut u8, len: usize) -> (Value, Vec<u8>) {
    let buf = unsafe { Vec::from_raw_parts(ptr, len, len.max(1)) };
    let parsed = (|| -> Option<(Value, Vec<u8>)> {
        let n = u32::from_le_bytes(buf.get(..4)?.try_into().ok()?) as usize;
        let head = serde_json::from_slice(buf.get(4..4 + n)?).ok()?;
        Some((head, buf.get(4 + n..)?.to_vec()))
    })();
    parsed.unwrap_or((Value::Null, Vec::new()))
}

/// An output frame, handed to the caller.
fn output(head: &Value, body: &[u8]) -> u64 {
    let h = head.to_string().into_bytes();
    let mut v = Vec::with_capacity(4 + h.len() + body.len());
    v.extend_from_slice(&(h.len() as u32).to_le_bytes());
    v.extend_from_slice(&h);
    v.extend_from_slice(body);
    let mut v = std::mem::ManuallyDrop::new(v.into_boxed_slice());
    ((v.as_mut_ptr() as u64) << 32) | v.len() as u64
}

/// A call with a JSON answer and no body: Err becomes `{"error"}`.
fn call(ptr: *mut u8, len: usize, f: impl FnOnce(Value, Vec<u8>) -> Result<Value>) -> u64 {
    let (head, body) = input(ptr, len);
    match f(head, body) {
        Ok(v) => output(&v, &[]),
        Err(e) => output(&json!({ "error": format!("{e:#}") }), &[]),
    }
}

fn field<'a, T: Deserialize<'a>>(v: &'a Value, k: &str) -> Result<T> {
    T::deserialize(v.get(k).with_context(|| format!("missing {k}"))?).with_context(|| format!("bad {k}"))
}

fn with_api<T>(f: impl FnOnce(&Embedded) -> T) -> Result<T> {
    API.with(|a| a.borrow().as_ref().map(f)).context("api_init first")
}

/// `{params_path, prefs_path, workers}`: serve that canoe.toml (its folder holds the hulls, the
/// mix data, result.json and runs/).
#[unsafe(no_mangle)]
pub extern "C" fn api_init(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |h, _| {
        let params: PathBuf = field(&h, "params_path")?;
        let prefs: PathBuf = field(&h, "prefs_path")?;
        let workers: usize = field(&h, "workers")?;
        let e = Embedded::new(&params, prefs, workers)?;
        API.with(|a| *a.borrow_mut() = Some(e));
        Ok(json!({ "ok": true }))
    })
}

/// `{method, url}` + body -> `serve::Response` (status, content_type, filename, warnings) + body.
#[unsafe(no_mangle)]
pub extern "C" fn api_request(ptr: *mut u8, len: usize) -> u64 {
    let (h, body) = input(ptr, len);
    let r = (|| -> Result<_> {
        let method: String = field(&h, "method")?;
        let url: String = field(&h, "url")?;
        with_api(|e| e.request(&method, &url, &body))
    })();
    match r {
        Ok(r) => output(&serde_json::to_value(&r).unwrap_or(Value::Null), &r.body),
        Err(e) => output(&json!({ "error": format!("{e:#}") }), &[]),
    }
}

/// -> `{job: canoe.toml text | null, shape_search: bool}`: the run `POST /api/run` just started.
#[unsafe(no_mangle)]
pub extern "C" fn api_take_job(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |_, _| {
        let job = with_api(|e| e.take_job())?;
        let shape = job.as_deref().and_then(|t| toml::from_str::<toml::Table>(t).ok()).is_some_and(|t| {
            t.contains_key("shape_search") && !t.get("hull").and_then(|h| h.get("file")).is_some()
        });
        Ok(json!({ "job": job, "shape_search": shape }))
    })
}

/// A progress event of the run going: a `Progress` (fixed hull), or `{done, total, shape}` with
/// the `ShapeEval` of a finished shape (shape search).
#[unsafe(no_mangle)]
pub extern "C" fn api_progress(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |h, _| {
        let p = match h.get("shape") {
            Some(s) => Progress::Candidate {
                done: field(&h, "done")?,
                total: field(&h, "total")?,
                candidate: ShapeEval::deserialize(s)?.candidate()?,
            },
            None => Progress::deserialize(&h)?,
        };
        with_api(|e| e.progress(p))?;
        Ok(json!({ "ok": true }))
    })
}

/// `serve::RunOutcome`: the run's end (result.json and the history are written here).
#[unsafe(no_mangle)]
pub extern "C" fn api_finish(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |h, _| {
        let o = RunOutcome::deserialize(&h)?;
        with_api(|e| e.finish(o))?;
        Ok(json!({ "ok": true }))
    })
}

/// -> `{paused, stopped}` of the run going.
#[unsafe(no_mangle)]
pub extern "C" fn api_control(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |_, _| {
        let (paused, stopped) = with_api(|e| e.control())?;
        Ok(json!({ "paused": paused, "stopped": stopped }))
    })
}

fn now() -> std::time::SystemTime {
    std::time::SystemTime::now()
}

#[cfg(target_family = "wasm")]
mod host {
    #[link(wasm_import_module = "jaguar")]
    unsafe extern "C" {
        pub fn progress(ptr: *const u8, len: usize);
    }
}

/// A fixed-hull progress event to the page (JSON of `Progress`).
fn send_progress(p: Progress) {
    let Ok(text) = serde_json::to_vec(&p) else { return };
    #[cfg(target_family = "wasm")]
    unsafe {
        host::progress(text.as_ptr(), text.len())
    };
    #[cfg(not(target_family = "wasm"))]
    let _ = text;
}

/// `{toml, base_dir}` -> `serve::RunOutcome` of the whole run (`jaguar::run`), progress
/// through the `jaguar.progress` import. Stopping = the page ends the worker.
#[unsafe(no_mangle)]
pub extern "C" fn run_fixed(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |h, _| {
        let toml: String = field(&h, "toml")?;
        let base: String = field(&h, "base_dir")?;
        let run = Params::parse(&toml, Path::new(&base)).and_then(|p| jaguar::run(&p, &send_progress));
        Ok(serde_json::to_value(RunOutcome::of(run, now()))?)
    })
}

/// Params and prep of the shape search `toml` in `base` (worked out once per worker).
fn with_shapes<T>(toml: &str, base: &str, f: impl FnOnce(&Params, &Prep) -> Result<T>) -> Result<T> {
    SHAPES.with(|s| {
        let mut s = s.borrow_mut();
        if !s.as_ref().is_some_and(|(t, b, _, _)| t == toml && b == base) {
            let p = Params::parse(toml, Path::new(base))?;
            let prep = jaguar::shapeopt::prepare(&p)?;
            *s = Some((toml.to_string(), base.to_string(), p, prep));
        }
        let (_, _, p, prep) = s.as_ref().expect("set above");
        f(p, prep)
    })
}

/// `{toml, base_dir, evals: [ShapeEval], stopped}` -> `{need: [ShapeJob]}` (score these, call
/// again with them added) or `{outcome: RunOutcome}`.
#[unsafe(no_mangle)]
pub extern "C" fn shape_step(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |h, _| {
        let toml: String = field(&h, "toml")?;
        let base: String = field(&h, "base_dir")?;
        let evals: Vec<ShapeEval> = field(&h, "evals")?;
        let stopped: bool = field(&h, "stopped")?;
        let step = with_shapes(&toml, &base, |p, prep| jaguar::run_shape_step(p, prep, &evals, stopped));
        Ok(match step {
            Ok(Step::Need(jobs)) => json!({ "need": jobs }),
            Ok(Step::Done(r)) => json!({ "outcome": RunOutcome::of(Ok(*r), now()) }),
            Err(e) => json!({ "outcome": RunOutcome::of(Err(e), now()) }),
        })
    })
}

/// `{toml, base_dir, job: ShapeJob}` -> `ShapeEval`.
#[unsafe(no_mangle)]
pub extern "C" fn shape_score(ptr: *mut u8, len: usize) -> u64 {
    call(ptr, len, |h, _| {
        let toml: String = field(&h, "toml")?;
        let base: String = field(&h, "base_dir")?;
        let job: ShapeJob = field(&h, "job")?;
        let e = with_shapes(&toml, &base, |p, prep| jaguar::shapeopt::score_shape(p, prep, &job))?;
        Ok(serde_json::to_value(e)?)
    })
}
