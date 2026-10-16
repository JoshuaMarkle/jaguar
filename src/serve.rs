//! Optional localhost web UI.
//!
//! `jaguar serve [canoe.toml] [-p 8000]` starts tiny_http on 127.0.0.1:<port>.
//! The page shows the canoe in 3D, lets the user edit the parameters in a form
//! (the only editor; it builds canoe.toml text) and press Run, and watches the optimization live. The optimizer never depends
//! on this module.
//!
//! HTTP API (all JSON unless noted; this is the contract with web/index.html):
//! * `GET  /`              -> web/index.html (embedded with include_str!)
//! * `GET  /vendor/three.min.js` -> three.js r128 (web/vendor, MIT, web/vendor/three.LICENSE),
//!   embedded too, so the 3D view never depends on a CDN or the network.
//! * `POST /api/import-mesh?name=<file.stl|file.obj>[&units=in|mm|cm|m|ft][&length_axis=x|y|z]
//!   [&up_axis=..][&flip_up=1][&bow_at_max=1]` body = the mesh file's bytes (a canoe made in CAD,
//!   `meshimport.rs`). Writes `<stem>_hull.json` next to the CLI canoe.toml (refusing to
//!   overwrite a hull that is not a mesh import) and returns {"file", "measured"}; 400 if the
//!   mesh cannot be read.
//! * `[race]`: the form edits each event's crew (a load case or its own weights) and posts
//!   the whole `[race]`, which is used as posted. When the posted text or the saved prefs have
//!   no `[race]` (prefs saved before it existed), the CLI canoe.toml's, read fresh, is used:
//!   `/api/params` returns it and `/api/run`, `/api/race`, `/api/prefs` add it.
//! * `GET  /api/params`    -> {"toml": "<text>", "params": <Params as JSON, hull.file and
//!   mix_data made relative to the CLI canoe.toml dir when inside it>, "dir": "<that dir>",
//!   "source": "prefs"|"file"} (UI prefills its form). Read fresh each call: the saved
//!   prefs file when it exists and parses, else the CLI canoe.toml.
//! * `POST /api/prefs`     body = canoe.toml text (the form, sent ~500 ms after each edit).
//!   Validated with `Params::parse(text, <CLI canoe.toml dir>)`; 400 {"error"} if invalid
//!   (nothing written), else the parsed params are written with ABSOLUTE paths (so they
//!   work from any cwd) to the prefs file atomically (tmp + rename), 200 {"ok":true}.
//!   Prefs file: `jaguar serve --prefs <path>`, else `$XDG_CONFIG_HOME/jaguar/prefs.toml`,
//!   else `~/.config/jaguar/prefs.toml`. Also makes these params current for `/api/mix`.
//! * `DELETE /api/prefs`   removes the prefs file (missing is fine), 200 {"ok":true};
//!   `/api/params` then returns the CLI canoe.toml again ("Reset to canoe.toml").
//! * `GET  /api/hulls`     -> [{"file": "off1_hull.json", "name": ..., "length": in,
//!   "notes": hull.source}] for every *.json in the CLI canoe.toml's dir whose
//!   "format" is jaguar-hull/1 (sorted by file name). Feeds the hull picker.
//! * `GET  /api/hull`      -> jaguar-hull/1 of the current params' hull file;
//!   `?file=<name from /api/hulls>` returns that one instead (reject any path
//!   separator or `..` with 400: no arbitrary file reads).
//! * `POST /api/run`       body = canoe.toml text. Validates with
//!   `Params::parse(text, <dir of the CLI canoe.toml>)`; 400 {"error"} if
//!   invalid, 409 if a run is already going, else 202 {"ok":true} and starts
//!   `optimize::run_with_progress` on a background thread.
//! * `POST /api/pause`  /  `POST /api/resume`: hold the running search at its next candidate
//!   (fixed hull) or shape (shape search) / let it go on. 200 {"ok":true}, 409 if no run is going.
//! * `POST /api/stop`      end the running search (also releases a pause). Shape search: no new
//!   shapes; the best shape so far is verified and reported as usual, with `ended_early` in
//!   result.json (state "done"). Fixed hull, or no shape finished yet: state "stopped", no result
//!   written, the previous one kept. 200 {"ok":true}, 409 if no run is going.
//! * `GET  /api/status`    -> {"state": "idle"|"running"|"done"|"error"|"stopped",
//!   "paused": bool, "stopping": bool, "done": n, "total": n, "error": str|null,
//!   "elapsed_s": running time without pauses | null, "eta_s": estimated seconds left | null
//!   (running, not ending, the first wave done (one per worker thread): done per running second x
//!   what is left; a shape search
//!   counts every refine round at full size, `shapeopt::planned_shapes`, so it tends to finish
//!   sooner; the final verification is not included),
//!   "shape": {"index": i, "shapes": n} | null (shape search only), "candidates": [...new since ?since=k...]}
//!   Poll every ~250 ms with `?since=<count already seen>` to get only new
//!   candidates (cheap incremental updates; no websockets needed).
//! * `GET  /api/mix`       -> the mix::StrengthFit (lab points, fit line, tested
//!   range) for the most recently submitted params (startup params before any run).
//!   500 {"error"} if the mix data cannot be loaded or fitted.
//! * `POST /api/mix`       body = mix CSV text (docs/MIX_DATA.md format). Validated with
//!   `StrengthFit::parse` (same loader as runs); 400 {"error"} if it does not parse or fit.
//!   Else the current params' `material.mix_data` is copied to `<file>.bak` and replaced
//!   atomically (tmp + rename); 200 with the new StrengthFit plus "csv": the saved text.
//!   `GET /api/mix` also returns "csv": the raw file text (feeds the editable table).
//! * `GET  /api/result`    -> the last jaguar-result/1, 404 if none yet.
//!   Also written to `result.json` next to the canoe.toml when a run finishes.
//! * `GET  /api/export?format=step|stl` -> the last run's winner as a file download
//!   (Content-Disposition attachment; STEP AP214 solid or binary STL, inches, see
//!   `export.rs`). 404 if no result yet, 409 if that result has no winner, 400 bad format.
//! * `POST /api/race`      body = canoe.toml text (the form; its `[race]` section, else 400).
//!   Races the last result's winner (`race::run`, docs/SPEC.md "Race simulator") and returns
//!   the jaguar-race/1 JSON. Synchronous (a few seconds). 404 if no
//!   result yet, 409 if a run is going or the result has no winner, 422 if the race fails.
//!
//! * `GET  /api/session`   -> {"app": "jaguar", "version", "desktop": bool, "folder": str|null}.
//!   `desktop` = started by a double-click (`launch.rs`); `folder` = its canoe folder. The
//!   desktop page calls it every 30 s: any request keeps a desktop server alive.
//! * `POST /api/quit`      desktop only (403 otherwise): 200 {"ok":true}, then the server stops.
//! * `POST /api/open-folder` desktop only (403 otherwise): opens the canoe folder in
//!   Explorer / Finder, 200 {"ok":true}.
//!
//! Any panic or Err inside a run must land as state "error" with a message,
//! never kill the server.

use crate::export;
use crate::hull::Hull;
use crate::optimize::{self, Candidate, Progress};
use crate::params::Params;
use crate::report;
use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const INDEX_HTML: &str = include_str!("../web/index.html");
/// three.js r128, byte-identical to cdnjs (sha512 SRI checked when vendored).
const THREE_JS: &str = include_str!("../web/vendor/three.min.js");

/// Run state shared between the HTTP thread and the optimizer thread.
#[derive(Debug, Default)]
struct Shared {
    state: &'static str,
    done: usize,
    total: usize,
    candidates: Vec<Candidate>,
    /// Last jaguar-result/1, already serialized.
    result: Option<String>,
    error: Option<String>,
    /// Hull file of the most recently submitted params.
    hull_file: PathBuf,
    /// Shape search progress: (index, shapes) of the shape being evaluated.
    shape: Option<(usize, usize)>,
    /// Params of the most recent run (feeds /api/mix).
    params: Option<Params>,
    /// Pause / stop of the current run (a fresh one per run).
    ctl: Arc<optimize::RunControl>,
    /// Run start, start of the current pause, total paused seconds (time estimate).
    started: Option<Instant>,
    paused_at: Option<Instant>,
    paused_s: f64,
    /// Shape search: most shapes it can evaluate (`shapeopt::planned_shapes`).
    planned: Option<usize>,
    /// Size of the first wave of work (worker threads, at most the total): no estimate before.
    wave: usize,
}

impl Shared {
    /// Running seconds without pauses.
    fn active_s(&self) -> Option<f64> {
        let t = self.started?.elapsed().as_secs_f64() - self.paused_s;
        Some(t - self.paused_at.map_or(0.0, |p| p.elapsed().as_secs_f64()))
    }

    /// Seconds left at the rate so far (see `GET /api/status`).
    fn eta_s(&self) -> Option<f64> {
        let t = self.active_s()?;
        // The first wave (one shape or candidate per worker thread) finishes together, so the
        // rate means something only after it.
        if self.state != "running"
            || self.ctl.stopped()
            || self.done == 0
            || self.done < self.wave
            || t <= 0.0
        {
            return None;
        }
        let left = self.planned.unwrap_or(0).max(self.total).saturating_sub(self.done);
        Some(left as f64 * t / self.done as f64)
    }

    fn end_pause(&mut self) {
        if let Some(p) = self.paused_at.take() {
            self.paused_s += p.elapsed().as_secs_f64();
        }
    }
}

/// Request handler, independent of the socket so it can be unit tested.
struct App {
    /// The CLI canoe.toml (source of `[race]`, re-read on each request).
    params_path: PathBuf,
    toml_text: String,
    base_dir: PathBuf,
    /// Saved form settings (see `POST /api/prefs`).
    prefs_path: PathBuf,
    /// Params as loaded at startup (absolute paths).
    params: Params,
    result_path: PathBuf,
    shared: Arc<Mutex<Shared>>,
    /// Desktop mode (`jaguar` double-clicked): its canoe folder. None for `jaguar serve`.
    desktop: Option<PathBuf>,
    /// Time of the last request (desktop idle quit).
    seen: Arc<Mutex<Instant>>,
    /// Set by `POST /api/quit`: the server stops after replying.
    quit: AtomicBool,
}

type Reply = (u16, &'static str, String);

fn json_reply(status: u16, v: serde_json::Value) -> Reply {
    (status, "application/json", v.to_string())
}

fn err_reply(status: u16, msg: impl std::fmt::Display) -> Reply {
    json_reply(status, serde_json::json!({ "error": msg.to_string() }))
}

impl App {
    fn new(params_path: &Path, prefs_path: PathBuf) -> Result<App> {
        let toml_text = std::fs::read_to_string(params_path)
            .with_context(|| format!("reading {}", params_path.display()))?;
        let base_dir = base_dir_of(params_path);
        // Saved prefs win over the file when they still parse.
        let params = match load_prefs(&prefs_path, &base_dir) {
            Some((_, p)) => p,
            None => Params::load(params_path)?,
        };
        let startup = params.clone();
        let result_path = base_dir.join("result.json");
        let shared = Shared {
            state: "idle",
            hull_file: params.hull.file.clone().unwrap_or_default(),
            ..Default::default()
        };
        Ok(App {
            params_path: params_path.to_path_buf(),
            toml_text,
            base_dir,
            prefs_path,
            params: startup,
            result_path,
            shared: Arc::new(Mutex::new(shared)),
            desktop: None,
            seen: Arc::new(Mutex::new(Instant::now())),
            quit: AtomicBool::new(false),
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Shared> {
        self.shared.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn handle(&self, method: &str, url: &str, body: &str) -> Reply {
        let (path, query) = url.split_once('?').unwrap_or((url, ""));
        match (method, path) {
            ("GET", "/") | ("GET", "/index.html") => {
                (200, "text/html; charset=utf-8", INDEX_HTML.to_string())
            }
            ("GET", "/vendor/three.min.js") => (200, "text/javascript; charset=utf-8", THREE_JS.to_string()),
            ("GET", "/api/params") => self.get_params(),
            ("POST", "/api/prefs") => self.save_prefs(body),
            ("DELETE", "/api/prefs") => match std::fs::remove_file(&self.prefs_path) {
                Ok(()) => json_reply(200, serde_json::json!({ "ok": true })),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    json_reply(200, serde_json::json!({ "ok": true }))
                }
                Err(e) => err_reply(500, format!("removing {}: {e}", self.prefs_path.display())),
            },
            ("POST", "/api/mix") => self.save_mix(body),
            ("GET", "/api/hulls") => self.hulls(),
            ("GET", "/api/hull") => {
                let requested = query.split('&').find_map(|kv| kv.strip_prefix("file="));
                let file = match requested {
                    Some(name) => match safe_name(name) {
                        Some(n) => self.base_dir.join(n),
                        None => return err_reply(400, format!("bad hull file name: {name:?}")),
                    },
                    None => self.lock().hull_file.clone(),
                };
                match Hull::load(&file).and_then(|h| Ok(serde_json::to_string(&h)?)) {
                    Ok(s) => (200, "application/json", s),
                    Err(e) => err_reply(500, format!("{e:#}")),
                }
            }
            ("GET", "/api/status") => self.status(query),
            ("GET", "/api/mix") => {
                let p = self.current();
                let csv = std::fs::read_to_string(&p.material.mix_data);
                match optimize::strength(&p).and_then(|f| Ok(serde_json::to_value(&f)?)) {
                    Ok(mut v) => {
                        v["csv"] = serde_json::Value::String(csv.unwrap_or_default());
                        json_reply(200, v)
                    }
                    Err(e) => err_reply(500, format!("{e:#}")),
                }
            }
            ("GET", "/api/result") => match self.lock().result.clone() {
                Some(s) => (200, "application/json", s),
                None => err_reply(404, "no result yet"),
            },
            ("POST", "/api/run") => self.start_run(body),
            ("POST", "/api/race") => self.race(body),
            ("POST", "/api/pause") | ("POST", "/api/resume") | ("POST", "/api/stop") => {
                let mut s = self.lock();
                if s.state != "running" {
                    return err_reply(409, "no run is going");
                }
                match path {
                    "/api/pause" => {
                        s.ctl.set_paused(true);
                        s.paused_at.get_or_insert_with(Instant::now);
                    }
                    "/api/resume" => {
                        s.ctl.set_paused(false);
                        s.end_pause();
                    }
                    _ => {
                        s.ctl.stop();
                        s.end_pause();
                    }
                }
                json_reply(200, serde_json::json!({ "ok": true }))
            }
            ("GET", "/api/session") => json_reply(
                200,
                serde_json::json!({
                    "app": "jaguar",
                    "version": env!("CARGO_PKG_VERSION"),
                    "desktop": self.desktop.is_some(),
                    "folder": self.desktop.as_ref().map(|d| d.display().to_string()),
                }),
            ),
            ("POST", "/api/quit") | ("POST", "/api/open-folder") if self.desktop.is_none() => {
                err_reply(403, "only when JAGUAR was opened by double-click")
            }
            ("POST", "/api/quit") => {
                self.quit.store(true, Ordering::SeqCst);
                json_reply(200, serde_json::json!({ "ok": true }))
            }
            ("POST", "/api/open-folder") => {
                let dir = self.desktop.as_ref().expect("desktop checked above").display().to_string();
                match crate::launch::open(&dir) {
                    Ok(()) => json_reply(200, serde_json::json!({ "ok": true })),
                    Err(e) => err_reply(500, format!("{e:#}")),
                }
            }
            (
                _,
                "/"
                | "/index.html"
                | "/vendor/three.min.js"
                | "/api/params"
                | "/api/prefs"
                | "/api/hulls"
                | "/api/hull"
                | "/api/status"
                | "/api/mix"
                | "/api/result"
                | "/api/run"
                | "/api/export"
                | "/api/race"
                | "/api/pause"
                | "/api/resume"
                | "/api/stop"
                | "/api/session"
                | "/api/quit"
                | "/api/open-folder",
            ) => err_reply(405, "method not allowed"),
            _ => err_reply(404, "not found"),
        }
    }

    /// `GET /api/export?format=step|stl`: (status, content type, file name, bytes). Errors come
    /// back as JSON bodies with no file name.
    fn export(&self, query: &str) -> (u16, &'static str, Option<String>, Vec<u8>) {
        let fail = |(st, ct, body): Reply| (st, ct, None, body.into_bytes());
        let fmt = query.split('&').find_map(|kv| kv.strip_prefix("format=")).unwrap_or("step");
        let Some(result) = self.lock().result.clone() else {
            return fail(err_reply(404, "no result yet: run first"));
        };
        // Bulkheads need a station spacing below the air pocket length (short pockets on
        // beamy hulls), so refine past 161 when the pocket asks for it.
        let built = export::from_result(&result, 161, 41).and_then(|(hull, mut spec)| {
            if let Some(l) = spec.pocket_in.filter(|l| *l > 0.0) {
                let need = (hull.length / (0.5 * l)).ceil() as usize + 1;
                spec.stations = spec.stations.max(need.min(4001));
            }
            Ok((export::build_mesh(&hull, &spec)?, hull.name))
        });
        let (mesh, name) = match built {
            Ok(v) => v,
            Err(e) => return fail(err_reply(409, format!("{e:#}"))),
        };
        let stem: String = name
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect();
        match fmt {
            "step" | "stp" => (
                200,
                "application/step",
                Some(format!("{stem}.step")),
                export::to_step(&mesh, &name).into_bytes(),
            ),
            "stl" => (200, "model/stl", Some(format!("{stem}.stl")), export::to_stl(&mesh, &name)),
            f => fail(err_reply(400, format!("unknown export format '{f}' (step | stl)"))),
        }
    }

    /// Params of the most recent run or prefs save, else the startup params.
    /// `[race]` of the CLI canoe.toml as it is on disk now (None: the file has none).
    fn file_race(&self) -> Result<Option<toml::Value>, String> {
        let text = std::fs::read_to_string(&self.params_path)
            .map_err(|e| format!("reading {}: {e}", self.params_path.display()))?;
        let table: toml::Table =
            toml::from_str(&text).map_err(|e| format!("{}: {e}", self.params_path.display()))?;
        Ok(table.get("race").cloned())
    }

    /// The posted canoe.toml text, with the CLI canoe.toml's `[race]` added when it has none (the
    /// form's own `[race]` is used as posted). Text that does not parse is returned as is, for
    /// `Params::parse` to report.
    fn with_file_race(&self, body: &str) -> Result<String, String> {
        let Ok(mut table) = toml::from_str::<toml::Table>(body) else { return Ok(body.to_string()) };
        if table.contains_key("race") {
            return Ok(body.to_string());
        }
        let Some(race) = self.file_race()? else { return Ok(body.to_string()) };
        table.insert("race".into(), race);
        toml::to_string(&table).map_err(|e| e.to_string())
    }

    fn current(&self) -> Params {
        self.lock().params.clone().unwrap_or_else(|| self.params.clone())
    }

    fn get_params(&self) -> Reply {
        let (source, toml_text, params) = match load_prefs(&self.prefs_path, &self.base_dir) {
            Some((t, p)) => ("prefs", t, p),
            None => match Params::parse(&self.toml_text, &self.base_dir) {
                Ok(p) => ("file", self.toml_text.clone(), p),
                Err(e) => return err_reply(500, format!("{e:#}")),
            },
        };
        // Prefs hold absolute paths, so strip the canonical dir as well.
        let canon = absolute(&self.base_dir);
        let rel_to = |f: &Path| -> PathBuf {
            f.strip_prefix(&self.base_dir).or_else(|_| f.strip_prefix(&canon)).unwrap_or(f).to_path_buf()
        };
        let mut rel = params;
        // Saved prefs keep their own (form-edited) [race]; without one, the file's.
        if rel.race.is_none() {
            match self.file_race() {
                Ok(Some(v)) => match v.try_into() {
                    Ok(r) => rel.race = Some(r),
                    Err(e) => {
                        return err_reply(500, format!("{}: [race]: {e}", self.params_path.display()));
                    }
                },
                Ok(None) => {}
                Err(e) => return err_reply(500, e),
            }
        }
        rel.hull.file = rel.hull.file.as_deref().map(rel_to);
        rel.material.mix_data = rel_to(&rel.material.mix_data);
        match serde_json::to_value(&rel) {
            Ok(pj) => json_reply(
                200,
                serde_json::json!({
                    "toml": toml_text,
                    "params": pj,
                    "dir": self.base_dir.display().to_string(),
                    "source": source,
                }),
            ),
            Err(e) => err_reply(500, e),
        }
    }

    fn save_prefs(&self, body: &str) -> Reply {
        let body = match self.with_file_race(body) {
            Ok(b) => b,
            Err(e) => return err_reply(500, e),
        };
        let params = match Params::parse(&body, &self.base_dir) {
            Ok(p) => p,
            Err(e) => return err_reply(400, format!("{e:#}")),
        };
        let mut abs = params.clone();
        if let Some(f) = &abs.hull.file {
            abs.hull.file = Some(absolute(f));
        }
        abs.material.mix_data = absolute(&abs.material.mix_data);
        let text = match toml::to_string(&abs) {
            Ok(t) => format!("# JAGUAR web UI settings (autosaved). Delete to reset.\n{t}"),
            Err(e) => return err_reply(500, e),
        };
        if let Err(e) = write_atomic(&self.prefs_path, &text) {
            return err_reply(500, format!("{e:#}"));
        }
        let mut s = self.lock();
        if s.state != "running" {
            s.params = Some(params);
        }
        json_reply(200, serde_json::json!({ "ok": true }))
    }

    fn save_mix(&self, body: &str) -> Reply {
        let p = self.current();
        let fit = match crate::mix::StrengthFit::parse(body, p.material.flex_over_split) {
            Ok(f) => f,
            Err(e) => return err_reply(400, format!("{e:#}")),
        };
        let path = &p.material.mix_data;
        let mut bak = path.clone().into_os_string();
        bak.push(".bak");
        if path.exists()
            && let Err(e) = std::fs::copy(path, &bak)
        {
            return err_reply(500, format!("backing up {}: {e}", path.display()));
        }
        if let Err(e) = write_atomic(path, body) {
            return err_reply(500, format!("{e:#}"));
        }
        match serde_json::to_value(&fit) {
            Ok(mut v) => {
                v["csv"] = serde_json::Value::String(body.to_string());
                json_reply(200, v)
            }
            Err(e) => err_reply(500, e),
        }
    }

    /// Every jaguar-hull/1 JSON in `base_dir`, sorted by file name. Read fresh each call.
    /// `POST /api/import-mesh`: a CAD mesh into a hull table next to the canoe.toml.
    fn import_mesh(&self, query: &str, bytes: &[u8]) -> Reply {
        use crate::meshimport::{self as mi, MeshOptions};
        let q = |k: &str| {
            query.split('&').find_map(|kv| kv.strip_prefix(&format!("{k}=")[..])).map(|v| v.to_string())
        };
        let Some(name) = q("name").as_deref().and_then(safe_name) else {
            return err_reply(400, "give ?name=<file.stl|file.obj> (a bare file name)");
        };
        let path = Path::new(&name);
        let ext = path.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
        let stem: String = path
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect();
        if stem.is_empty() {
            return err_reply(400, "bad mesh file name");
        }
        let parsed = match ext.as_str() {
            "stl" => mi::parse_stl(bytes),
            "obj" => mi::parse_obj(&String::from_utf8_lossy(bytes)),
            e => {
                return err_reply(
                    400,
                    format!("mesh must be .stl or .obj (got .{e}): export one from your CAD"),
                );
            }
        };
        let opts = (|| -> Result<MeshOptions> {
            Ok(MeshOptions {
                units: q("units").filter(|u| u != "auto").as_deref().map(mi::unit_scale).transpose()?,
                length_axis: q("length_axis").as_deref().map(mi::axis_index).transpose()?,
                up_axis: q("up_axis").as_deref().map(mi::axis_index).transpose()?,
                flip_up: q("flip_up").is_some_and(|v| v == "1"),
                bow_at_max: q("bow_at_max").is_some_and(|v| v == "1"),
                ..MeshOptions::new()
            })
        })();
        let (hull, m) = match opts.and_then(|o| mi::hull_from_mesh(&parsed?, &stem, &o)) {
            Ok(v) => v,
            Err(e) => return err_reply(400, format!("{e:#}")),
        };
        let file = format!("{stem}_hull.json");
        let out = self.base_dir.join(&file);
        if let Ok(old) = Hull::load(&out)
            && !old.source.starts_with("mesh import")
        {
            return err_reply(409, format!("{file} exists and is not a mesh import: rename the mesh"));
        }
        if let Err(e) = hull.save(&out) {
            return err_reply(500, format!("{e:#}"));
        }
        json_reply(200, serde_json::json!({ "file": file, "measured": m }))
    }

    fn hulls(&self) -> Reply {
        let entries = match std::fs::read_dir(&self.base_dir) {
            Ok(e) => e,
            Err(e) => return err_reply(500, format!("reading {}: {e}", self.base_dir.display())),
        };
        let mut names: Vec<String> = entries
            .filter_map(|e| e.ok()?.file_name().into_string().ok())
            .filter(|n| n.ends_with(".json"))
            .collect();
        names.sort();
        let list: Vec<serde_json::Value> = names
            .into_iter()
            .filter_map(|n| {
                let text = std::fs::read_to_string(self.base_dir.join(&n)).ok()?;
                let v: serde_json::Value = serde_json::from_str(&text).ok()?;
                (v["format"] == crate::hull::FORMAT).then(|| {
                    serde_json::json!({
                        "file": n, "name": v["name"], "length": v["length"], "notes": v["source"],
                    })
                })
            })
            .collect();
        json_reply(200, serde_json::Value::Array(list))
    }

    fn status(&self, query: &str) -> Reply {
        let since = query
            .split('&')
            .find_map(|kv| kv.strip_prefix("since="))
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(0);
        let s = self.lock();
        let new = &s.candidates[since.min(s.candidates.len())..];
        json_reply(
            200,
            serde_json::json!({
                "state": s.state,
                "paused": s.state == "running" && s.ctl.paused(),
                "stopping": s.state == "running" && s.ctl.stopped(),
                "done": s.done,
                "total": s.total,
                "elapsed_s": s.active_s(),
                "eta_s": s.eta_s(),
                "error": s.error,
                "shape": s.shape.map(|(i, n)| serde_json::json!({ "index": i, "shapes": n })),
                "candidates": new,
            }),
        )
    }

    /// `POST /api/race`: race the last winner with the posted params' `[race]`.
    fn race(&self, body: &str) -> Reply {
        let body = match self.with_file_race(body) {
            Ok(b) => b,
            Err(e) => return err_reply(500, e),
        };
        let params = match Params::parse(&body, &self.base_dir) {
            Ok(p) => p,
            Err(e) => return err_reply(400, format!("{e:#}")),
        };
        let Some(rp) = params.race else {
            return err_reply(400, format!("no [race] section in {}: add one", self.params_path.display()));
        };
        let (state, result) = {
            let s = self.lock();
            (s.state, s.result.clone())
        };
        if state == "running" {
            return err_reply(409, "a run is going: race its winner when it is done");
        }
        let Some(result) = result else {
            return err_reply(404, "no result yet: run first");
        };
        let input = match report::race_input(&result) {
            Ok(i) => i,
            Err(e) => return err_reply(409, format!("{e:#}")),
        };
        match crate::race::run(&input.hull, input.hull_lb, input.depth_in, input.topsides, &rp)
            .and_then(|r| Ok(serde_json::to_string(&r)?))
        {
            Ok(s) => (200, "application/json", s),
            Err(e) => err_reply(422, format!("{e:#}")),
        }
    }

    fn start_run(&self, body: &str) -> Reply {
        let body = match self.with_file_race(body) {
            Ok(b) => b,
            Err(e) => return err_reply(500, e),
        };
        let params = match Params::parse(&body, &self.base_dir) {
            Ok(p) => p,
            Err(e) => return err_reply(400, format!("{e:#}")),
        };
        {
            let mut s = self.lock();
            if s.state == "running" {
                return err_reply(409, "a run is already going");
            }
            let result = s.result.take();
            *s = Shared {
                state: "running",
                hull_file: params.hull.file.clone().unwrap_or_default(),
                result,
                params: Some(params.clone()),
                started: Some(Instant::now()),
                planned: params.shape_search.as_ref().map(crate::shapeopt::planned_shapes),
                wave: rayon::current_num_threads(),
                ..Default::default()
            };
        }
        let shared = Arc::clone(&self.shared);
        let out = self.result_path.clone();
        let ctl = Arc::clone(&self.lock().ctl);
        std::thread::spawn(move || run_job(params, shared, out, &ctl));
        json_reply(202, serde_json::json!({ "ok": true }))
    }
}

/// Saved prefs (text, params) if the file exists and parses, else None.
/// The folder `canoe.toml` sits in (its hulls, mix data and result.json): `.` for a bare
/// file name (`jaguar serve canoe.toml` run in that folder), where `parent()` is empty and
/// listing it would fail.
fn base_dir_of(params_path: &Path) -> PathBuf {
    match params_path.parent() {
        Some(d) if !d.as_os_str().is_empty() => d.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

fn load_prefs(path: &Path, base_dir: &Path) -> Option<(String, Params)> {
    let text = std::fs::read_to_string(path).ok()?;
    // Prefs are written by this UI: drop the factor of the removed 0.9D + 1.6L combination so
    // settings saved before it was removed still load (a hand-written canoe.toml is refused).
    let text: String = text
        .lines()
        .filter(|l| !l.trim_start().starts_with("lrfd_dead_min"))
        .map(|l| format!("{l}\n"))
        .collect();
    let p = Params::parse(&text, base_dir).ok()?;
    Some((text, p))
}

/// Absolute form of `p` (cwd-joined if relative, `..` resolved when it exists).
fn absolute(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap_or_else(|_| std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf()))
}

/// Write via a temp file in the same dir and rename, creating parent dirs.
fn write_atomic(path: &Path, text: &str) -> Result<()> {
    if let Some(d) = path.parent().filter(|d| !d.as_os_str().is_empty()) {
        std::fs::create_dir_all(d).with_context(|| format!("creating {}", d.display()))?;
    }
    // Unique per write: requests run on their own threads.
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(format!(".tmp{}_{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
    let tmp = PathBuf::from(tmp);
    std::fs::write(&tmp, text).with_context(|| format!("writing {}", tmp.display()))?;
    std::fs::rename(&tmp, path).with_context(|| format!("renaming to {}", path.display()))?;
    Ok(())
}

/// Default prefs location: `$XDG_CONFIG_HOME/jaguar/prefs.toml`, else
/// `~/.config/jaguar/prefs.toml` (home = `$HOME`, else `%USERPROFILE%`). Err if none is set.
pub fn default_prefs_path() -> Result<PathBuf> {
    let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from);
    let dir = env("XDG_CONFIG_HOME")
        .or_else(|| crate::launch::home_dir().map(|h| h.join(".config")))
        .context("no XDG_CONFIG_HOME, HOME or USERPROFILE: pass --prefs <path>")?;
    Ok(dir.join("jaguar").join("prefs.toml"))
}

/// Percent-decode a query value and accept it only as a bare file name in the dir.
fn safe_name(raw: &str) -> Option<String> {
    let mut bytes = Vec::new();
    let b = raw.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let h = std::str::from_utf8(&b[i + 1..i + 3]).ok()?;
            bytes.push(u8::from_str_radix(h, 16).ok()?);
            i += 3;
        } else {
            bytes.push(if b[i] == b'+' { b' ' } else { b[i] });
            i += 1;
        }
    }
    let name = String::from_utf8(bytes).ok()?;
    let bad = name.is_empty() || name.contains(['/', '\\', '\0']) || name.contains("..");
    (!bad).then_some(name)
}

fn lock(s: &Mutex<Shared>) -> std::sync::MutexGuard<'_, Shared> {
    s.lock().unwrap_or_else(|e| e.into_inner())
}

fn run_job(params: Params, shared: Arc<Mutex<Shared>>, out: PathBuf, ctl: &optimize::RunControl) {
    let sh = Arc::clone(&shared);
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<String> {
        let on_progress = |p: Progress| {
            let mut s = lock(&sh);
            match p {
                Progress::Started { total } => s.total = total,
                Progress::Candidate { done, total, candidate } => {
                    s.done = s.done.max(done);
                    s.total = total;
                    s.candidates.push(candidate);
                }
                Progress::Shape { index, shapes, .. } => s.shape = Some((index, shapes)),
                Progress::Verifying { .. } => {}
            }
        };
        let result = crate::run_controlled(&params, &on_progress, ctl)?;
        report::write_json(&result, &out)?;
        Ok(serde_json::to_string_pretty(&result)?)
    }));
    let mut s = lock(&shared);
    match outcome {
        Ok(Ok(json)) => {
            s.result = Some(json);
            s.state = "done";
        }
        Ok(Err(e)) if e.downcast_ref::<optimize::RunEnded>().is_some() => s.state = "stopped",
        Ok(Err(e)) => {
            s.error = Some(format!("{e:#}"));
            s.state = "error";
        }
        Err(panic) => {
            let msg = panic
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| panic.downcast_ref::<&str>().map(|m| m.to_string()))
                .unwrap_or_else(|| "unknown panic".into());
            s.error = Some(format!("optimizer panicked: {msg}"));
            s.state = "error";
        }
    }
}

/// Answer requests until the server is unblocked (`POST /api/quit` in desktop mode, or the
/// desktop idle watcher). One thread per request: a local client that never finishes its request
/// (some vendor tools probe localhost ports) must not block the page's requests.
fn serve_on(server: Arc<tiny_http::Server>, app: Arc<App>) {
    for req in server.incoming_requests() {
        let (app, srv) = (Arc::clone(&app), Arc::clone(&server));
        std::thread::spawn(move || {
            respond(req, &app);
            if app.quit.load(Ordering::SeqCst) {
                srv.unblock();
            }
        });
    }
}

/// Read one request, answer it.
fn respond(mut req: tiny_http::Request, app: &App) {
    *app.seen.lock().unwrap_or_else(|e| e.into_inner()) = Instant::now();
    let mut raw = Vec::new();
    if req.as_reader().read_to_end(&mut raw).is_err() {
        raw.clear();
    }
    let url = req.url().to_string();
    // A mesh upload is binary; every other body is text.
    if req.method().as_str() == "POST" && url.split('?').next() == Some("/api/import-mesh") {
        let (status, ctype, text) = app.import_mesh(url.split_once('?').map_or("", |q| q.1), &raw);
        let header =
            tiny_http::Header::from_bytes(&b"Content-Type"[..], ctype.as_bytes()).expect("static header");
        let _ =
            req.respond(tiny_http::Response::from_string(text).with_status_code(status).with_header(header));
        return;
    }
    let body = String::from_utf8_lossy(&raw).into_owned();
    if req.method().as_str() == "GET" && url.split('?').next() == Some("/api/export") {
        let (status, ctype, file, bytes) = app.export(url.split_once('?').map_or("", |q| q.1));
        let mut resp = tiny_http::Response::from_data(bytes).with_status_code(status).with_header(
            tiny_http::Header::from_bytes(&b"Content-Type"[..], ctype.as_bytes()).expect("static header"),
        );
        if let Some(f) = file {
            let cd = format!("attachment; filename=\"{f}\"");
            if let Ok(h) = tiny_http::Header::from_bytes(&b"Content-Disposition"[..], cd.as_bytes()) {
                resp = resp.with_header(h);
            }
        }
        let _ = req.respond(resp);
        return;
    }
    let (status, ctype, text) = app.handle(req.method().as_str(), req.url(), &body);
    let header = tiny_http::Header::from_bytes(&b"Content-Type"[..], ctype.as_bytes())
        .expect("static header is valid");
    let resp = tiny_http::Response::from_string(text).with_status_code(status).with_header(header);
    let _ = req.respond(resp);
}

pub fn serve(params_path: &Path, port: u16, prefs: Option<PathBuf>) -> Result<()> {
    let prefs = match prefs {
        Some(p) => p,
        None => default_prefs_path()?,
    };
    let app = App::new(params_path, prefs.clone())?;
    let server = tiny_http::Server::http(("127.0.0.1", port))
        .map_err(|e| anyhow::anyhow!("binding 127.0.0.1:{port}: {e}"))?;
    println!("JAGUAR serving http://127.0.0.1:{port}/  (prefs {}, Ctrl-C to stop)", prefs.display());
    serve_on(Arc::new(server), Arc::new(app));
    Ok(())
}

/// Desktop mode (`launch.rs`): serve `folder`'s canoe.toml on an already bound `server` until
/// `POST /api/quit`, or until no request came for `idle` while no run is going.
pub fn serve_desktop(
    params_path: &Path,
    prefs: PathBuf,
    server: tiny_http::Server,
    folder: PathBuf,
    idle: Duration,
) -> Result<()> {
    let mut app = App::new(params_path, prefs)?;
    app.desktop = Some(folder);
    let server = Arc::new(server);
    let (srv, seen, shared) = (Arc::clone(&server), Arc::clone(&app.seen), Arc::clone(&app.shared));
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(idle.min(Duration::from_secs(10)));
            let quiet = seen.lock().unwrap_or_else(|e| e.into_inner()).elapsed() > idle;
            let running = shared.lock().unwrap_or_else(|e| e.into_inner()).state == "running";
            if quiet && !running {
                srv.unblock();
                return;
            }
        }
    });
    serve_on(server, Arc::new(app));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_dir_of_a_bare_file_name_is_the_current_folder() {
        assert_eq!(base_dir_of(Path::new("canoe.toml")), PathBuf::from("."));
        assert_eq!(base_dir_of(Path::new("examples/canoe.toml")), PathBuf::from("examples"));
        // and that folder can be listed (the hull picker reads it)
        assert!(std::fs::read_dir(base_dir_of(Path::new("canoe.toml"))).is_ok());
    }
    use std::io::{Read as _, Write as _};

    /// Fresh temp dir per call (tests run in parallel).
    fn tmpdir() -> PathBuf {
        static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let d = std::env::temp_dir().join(format!("jaguar_serve_{}_{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn app_with_prefs(prefs: PathBuf) -> App {
        App::new(Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/canoe.toml")), prefs).unwrap()
    }

    fn app() -> App {
        app_with_prefs(tmpdir().join("prefs.toml"))
    }

    fn params_of(a: &App) -> serde_json::Value {
        serde_json::from_str(&a.handle("GET", "/api/params", "").2).unwrap()
    }

    #[test]
    fn prefs_save_reject_reload_reset() {
        let dir = tmpdir();
        let prefs = dir.join("sub").join("prefs.toml");
        let a = app_with_prefs(prefs.clone());
        assert_eq!(params_of(&a)["source"], "file");
        // Invalid: 400, nothing written.
        let (st, _, body) = a.handle("POST", "/api/prefs", "bogus = 1");
        assert_eq!(st, 400, "{body}");
        assert!(body.contains("error"));
        assert!(!prefs.exists());
        // Valid (relative paths, as the form sends): saved with absolute paths.
        let edited = a.toml_text.replace("stations = 161", "stations = 41");
        assert_ne!(edited, a.toml_text);
        let (st, _, body) = a.handle("POST", "/api/prefs", &edited);
        assert_eq!(st, 200, "{body}");
        let saved = std::fs::read_to_string(&prefs).unwrap();
        let sp: Params =
            toml::from_str(saved.lines().skip(1).collect::<Vec<_>>().join("\n").as_str()).unwrap();
        assert!(sp.hull.file.unwrap().is_absolute());
        assert!(sp.material.mix_data.is_absolute());
        let v = params_of(&a);
        assert_eq!(v["source"], "prefs");
        assert_eq!(v["params"]["hull"]["stations"], 41);
        assert_eq!(v["params"]["hull"]["file"], "off1_hull.json");
        // A server restarted from another cwd still restores them.
        let b = app_with_prefs(prefs.clone());
        assert_eq!(params_of(&b)["params"]["hull"]["stations"], 41);
        assert_eq!(b.params.hull.stations, 41);
        // Invalid edit keeps the last good prefs.
        assert_eq!(a.handle("POST", "/api/prefs", "[hull]\nstations = 3\n").0, 400);
        assert_eq!(params_of(&a)["params"]["hull"]["stations"], 41);
        // Reset.
        assert_eq!(a.handle("DELETE", "/api/prefs", "").0, 200);
        assert!(!prefs.exists());
        let v = params_of(&a);
        assert_eq!(v["source"], "file");
        assert_eq!(v["params"]["hull"]["stations"], 161);
        assert_eq!(a.handle("DELETE", "/api/prefs", "").0, 200, "reset twice is fine");
        // A corrupt prefs file is ignored.
        std::fs::write(&prefs, "garbage").unwrap();
        assert_eq!(params_of(&a)["source"], "file");
    }

    #[test]
    fn mix_post_validates_backs_up_and_refits() {
        let a = app();
        let dir = tmpdir();
        let csv = dir.join("mix.csv");
        let orig =
            std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/mix_tests.csv")).unwrap();
        std::fs::write(&csv, &orig).unwrap();
        let mut p = a.params.clone();
        p.material.mix_data = csv.clone();
        a.lock().params = Some(p);
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/mix", "").2).unwrap();
        assert_eq!(v["csv"], orig.as_str());
        // Unfittable data: 400, file untouched, no backup.
        let one = "mix,density_pcf,split_tensile_psi,fraction_of_final\nA,60,300,1\n";
        assert_eq!(a.handle("POST", "/api/mix", one).0, 400);
        assert_eq!(a.handle("POST", "/api/mix", "nonsense").0, 400);
        assert_eq!(std::fs::read_to_string(&csv).unwrap(), orig);
        assert!(!dir.join("mix.csv.bak").exists());
        // Valid: saved, previous kept as .bak, new fit returned and served.
        let new = "mix,density_pcf,split_tensile_psi,fraction_of_final\nA,50,200,1\nB,60,300,1\n";
        let (st, _, body) = a.handle("POST", "/api/mix", new);
        assert_eq!(st, 200, "{body}");
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!((v["split_slope_psi_per_pcf"].as_f64().unwrap() - 10.0).abs() < 1e-9);
        assert_eq!(std::fs::read_to_string(&csv).unwrap(), new);
        assert_eq!(std::fs::read_to_string(dir.join("mix.csv.bak")).unwrap(), orig);
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/mix", "").2).unwrap();
        assert_eq!(v["points"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn routes() {
        let a = app();
        let (st, ct, body) = a.handle("GET", "/", "");
        assert_eq!((st, ct), (200, "text/html; charset=utf-8"));
        assert!(body.contains("three"));
        // PDF report: button next to the STEP/STL exports, built client side from the last result.
        assert!(body.contains(r#"id="exportpdf""#) && body.contains("function reportHtml("));
        assert!(body.contains("Generated by JAGUAR"));
        // Freeboard tab: every load case plus the empty canoe (winner.empty).
        assert!(body.contains("function plotFreeboard(") && body.contains("result.winner.empty"));
        // Report ends with the calculation appendix built from winner.calc.
        assert!(body.contains("function calcHtml(") && body.contains("${calcHtml(win)}"));
        let (st, _, body) = a.handle("GET", "/api/params", "");
        assert_eq!(st, 200);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(v["toml"].as_str().unwrap().contains("[hull]"));
        let (st, _, body) = a.handle("GET", "/api/hull", "");
        assert_eq!(st, 200, "{body}");
        assert!(body.contains("jaguar-hull/1"));
        assert_eq!(a.handle("GET", "/api/result", "").0, 404);
        assert_eq!(a.handle("GET", "/nope", "").0, 404);
        assert_eq!(a.handle("DELETE", "/api/run", "").0, 405);
    }

    #[test]
    fn params_hulls_and_hull_by_file() {
        let a = app();
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/params", "").2).unwrap();
        assert_eq!(v["params"]["hull"]["file"], "off1_hull.json");
        assert_eq!(v["params"]["search"]["thickness_in"][0], 0.375);
        assert!(v["dir"].as_str().unwrap().ends_with("examples"));
        let (st, _, body) = a.handle("GET", "/api/hulls", "");
        assert_eq!(st, 200);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        let list = v.as_array().unwrap();
        assert!(list.iter().any(|h| h["file"] == "off1_hull.json" && h["length"].as_f64().unwrap() > 0.0));
        assert!(list.iter().all(|h| h["file"] != "canoe.toml" && h["file"] != "result.json"));
        let (st, _, body) = a.handle("GET", "/api/hull?file=off1_hull.json", "");
        assert_eq!(st, 200, "{body}");
        assert!(body.contains("jaguar-hull/1"));
    }

    /// The optional sections the web form writes ([flotation], [turning], displacements, shape-search
    /// limits) survive prefs save -> /api/params, and stay absent when the form omits them.
    #[test]
    fn prefs_round_trip_optional_fields() {
        let dir = tmpdir();
        let prefs = dir.join("prefs.toml");
        let a = app_with_prefs(prefs.clone());
        let shape =
            std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/shape_opt.toml")).unwrap();
        // Same layout as the form's formToml(): inline displacements under [objective].
        let text = shape
            .replace("kind = \"weight\"", "kind = \"drag\"")
            .replace(
                "speed_mps = 2.5",
                "speed_mps = 2.5\nstraight_weight = 0.8\ndisplacements = [{ crew_lb = 350.0, weight = 0.6 }, \
                 { crew_lb = 525.0, weight = 0.2 }, { crew_lb = 700.0, weight = 0.2 }]",
            )
            .replace(
                "seed          = 1",
                "seed = 1\nlength_step_in = 6.0\nbeam_step_in = 0.5\ndraft_step_in = 0.5\n\
                 min_entry_angle_deg = 8.0\nrocker_in = [0.5, 3.0]\nsymmetric = true",
            )
            + "\n[flotation]\nair_pocket_max_in = 30.0\nswamp_factor = 1.2\n"
            + "\n[turning]\nspeed_mps = 2.0\nradius_in = 400.0\n";
        // The file's [race] names load case 2M: a form without it is refused, saying why.
        let renamed = text.replacen("name = \"2M\"", "name = \"2Mx\"", 1);
        let (st, _, body) = a.handle("POST", "/api/prefs", &renamed);
        assert_eq!(st, 400, "{body}");
        assert!(body.contains("no load case named '2M'"), "{body}");
        let (st, _, body) = a.handle("POST", "/api/prefs", &text);
        assert_eq!(st, 200, "{body}");
        let p = &params_of(&a)["params"];
        assert_eq!(p["flotation"]["air_pocket_max_in"], 30.0);
        assert_eq!(p["flotation"]["swamp_factor"], 1.2);
        assert_eq!(p["turning"]["radius_in"], 400.0);
        assert_eq!(p["objective"]["straight_weight"], 0.8);
        assert_eq!(p["objective"]["displacements"][1]["crew_lb"], 525.0);
        assert_eq!(p["objective"]["displacements"].as_array().unwrap().len(), 3);
        let ss = &p["shape_search"];
        assert_eq!(ss["length_step_in"], 6.0);
        assert_eq!(ss["beam_step_in"], 0.5);
        assert_eq!(ss["draft_step_in"], 0.5);
        assert_eq!(ss["min_entry_angle_deg"], 8.0);
        assert_eq!(ss["rocker_in"][1], 3.0);
        assert_eq!(ss["symmetric"], true);
        // A restarted server reads the saved prefs back unchanged.
        let b = app_with_prefs(prefs.clone());
        assert_eq!(params_of(&b)["params"], *p);
        // Omitted optional fields stay absent: null in /api/params, not in the prefs file.
        let plain = a.toml_text.split("[flotation]").next().unwrap().to_string()
            + "[objective]\nkind = \"drag\"\nspeed_mps = 2.5\n";
        let (st, _, body) = a.handle("POST", "/api/prefs", &plain);
        assert_eq!(st, 200, "{body}");
        let p = &params_of(&a)["params"];
        assert!(p["flotation"].is_null());
        assert!(p["turning"].is_null());
        assert!(p["objective"]["displacements"].is_null());
        assert!(p["shape_search"].is_null());
        let saved = std::fs::read_to_string(&prefs).unwrap();
        assert!(
            !saved.contains("flotation") && !saved.contains("displacements") && !saved.contains("turning"),
            "{saved}"
        );
    }

    #[test]
    fn mix_endpoint_returns_fit() {
        let a = app();
        let (st, _, body) = a.handle("GET", "/api/mix", "");
        assert_eq!(st, 200, "{body}");
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(!v["points"].as_array().unwrap().is_empty());
        assert!(v["split_slope_psi_per_pcf"].as_f64().unwrap().is_finite());
        assert!(v["density_min_pcf"].as_f64().unwrap() <= v["density_max_pcf"].as_f64().unwrap());
        assert_eq!(a.handle("PUT", "/api/mix", "").0, 405);
        assert_eq!(a.handle("PUT", "/api/prefs", "").0, 405);
        // A missing mix file is an error, never a made-up fit.
        let mut p = a.params.clone();
        p.material.mix_data = PathBuf::from("/nonexistent/mix.csv");
        a.lock().params = Some(p);
        assert_eq!(a.handle("GET", "/api/mix", "").0, 500);
    }

    #[test]
    fn status_reports_shape_progress() {
        let a = app();
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/status", "").2).unwrap();
        assert!(v["shape"].is_null());
        a.lock().shape = Some((3, 96));
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/status", "").2).unwrap();
        assert_eq!(v["shape"]["index"], 3);
        assert_eq!(v["shape"]["shapes"], 96);
    }

    #[test]
    fn hull_rejects_path_traversal() {
        let a = app();
        for q in ["../Cargo.toml", "..%2FCargo.toml", "%2Fetc%2Fpasswd", "/etc/passwd", "a\\b.json", ".."] {
            let (st, _, body) = a.handle("GET", &format!("/api/hull?file={q}"), "");
            assert_eq!(st, 400, "{q}: {body}");
        }
        assert_eq!(a.handle("GET", "/api/hull?file=missing.json", "").0, 500);
    }

    #[test]
    fn status_since_slices_candidates() {
        let a = app();
        {
            let mut s = a.lock();
            for i in 0..3 {
                s.candidates.push(Candidate {
                    length_in: 240.0,
                    beam_in: 20.7,
                    shape: None,
                    gm_in: None,
                    drag_n: None,
                    race_s: None,
                    air_pocket_in: None,
                    gm_cases_in: vec![],
                    gm_empty_in: None,
                    thickness_in: 0.5,
                    density_pcf: 60.0,
                    mix: None,
                    flare_deg: 0.0,
                    depth_in: 12.0 + i as f64,
                    hull_weight_lb: 100.0,
                    verdict: None,
                    governing_case: None,
                    error: Some("x".into()),
                    passes: false,
                    zones: None,
                    dense_mix: None,
                });
            }
        }
        let v: serde_json::Value =
            serde_json::from_str(&a.handle("GET", "/api/status?since=2", "").2).unwrap();
        assert_eq!(v["state"], "idle");
        assert_eq!(v["candidates"].as_array().unwrap().len(), 1);
        let v: serde_json::Value =
            serde_json::from_str(&a.handle("GET", "/api/status?since=99", "").2).unwrap();
        assert_eq!(v["candidates"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn run_rejects_bad_toml_and_concurrent_runs() {
        let a = app();
        let (st, _, body) = a.handle("POST", "/api/run", "bogus = 1");
        assert_eq!(st, 400);
        assert!(body.contains("error"));
        a.lock().state = "running";
        assert_eq!(a.handle("POST", "/api/run", &a.toml_text.clone()).0, 409);
    }

    #[test]
    fn import_mesh_writes_a_hull_next_to_the_canoe_toml() {
        let (a, _) = app_on(|_| {});
        let hull = Hull::load(Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/off1_hull.json")))
            .unwrap();
        let spec = export::ExportSpec {
            depth_in: 12.0,
            thickness_in: Some(0.375),
            flare_deg: 0.0,
            gunwale_radius_in: None,
            stations: 161,
            girth: 41,
            pocket_in: None,
            layers: None,
        };
        let stl = export::to_stl(&export::build_mesh(&hull, &spec).unwrap(), "off1");
        let (st, _, body) = a.import_mesh("name=My%20Canoe.stl&units=in", &stl);
        assert_eq!(st, 200, "{body}");
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["file"], "My_Canoe_hull.json");
        assert!((v["measured"]["thickness_in"].as_f64().unwrap() - 0.375).abs() < 0.02);
        assert!((v["measured"]["depth_in"].as_f64().unwrap() - 12.0).abs() < 0.05);
        // It shows up in the hull picker and loads.
        let list = a.handle("GET", "/api/hulls", "").2;
        assert!(list.contains("My_Canoe_hull.json"), "{list}");
        assert_eq!(a.handle("GET", "/api/hull?file=My_Canoe_hull.json", "").0, 200);
        assert_eq!(a.import_mesh("name=x.step", &stl).0, 400, "not a mesh");
        assert_eq!(a.import_mesh("name=bad.stl", b"nonsense").0, 400);
        // Never overwrites a hull that is not a mesh import.
        hull.save(&a.base_dir.join("keep_hull.json")).unwrap();
        assert_eq!(a.import_mesh("name=keep.stl", &stl).0, 409);
    }

    #[test]
    fn three_js_is_served_locally() {
        let a = app();
        let (st, ct, body) = a.handle("GET", "/vendor/three.min.js", "");
        assert_eq!((st, ct), (200, "text/javascript; charset=utf-8"));
        assert!(body.contains("Three.js Authors") && body.len() > 500_000);
        assert_eq!(a.handle("POST", "/vendor/three.min.js", "").0, 405);
        // The page loads it from here and from nowhere else.
        let page = a.handle("GET", "/", "").2;
        assert!(page.contains(r#"<script src="/vendor/three.min.js"></script>"#));
        assert!(!page.contains("<script src=\"http"), "no script from the network");
    }

    /// An App on a temporary canoe.toml: the example with `edit` applied (paths made absolute).
    fn app_on(edit: impl FnOnce(&mut Params)) -> (App, Params) {
        let dir = tmpdir();
        let mut p =
            Params::load(Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/canoe.toml"))).unwrap();
        edit(&mut p);
        let path = dir.join("canoe.toml");
        std::fs::write(&path, toml::to_string(&p).unwrap()).unwrap();
        (App::new(&path, dir.join("prefs.toml")).unwrap(), p)
    }

    #[test]
    fn race_needs_a_race_section_and_a_winner() {
        let one_sprint = |p: &mut Params| {
            let rp = p.race.as_mut().unwrap();
            rp.events.retain(|e| e.name == "Symposium men's 200 m sprint");
            rp.events[0].weight = 1.0;
        };
        let (a, params) = app_on(one_sprint);
        let mut form = params.clone();
        form.race = None; // what the form sends with prefs saved before [race] existed
        let body = toml::to_string(&form).unwrap();
        assert_eq!(a.handle("POST", "/api/race", "bogus = 1").0, 400);
        assert_eq!(a.handle("POST", "/api/race", &body).0, 404, "no result yet");
        let result = crate::run(&params, &|_| {}).unwrap();
        a.lock().result = Some(serde_json::to_string(&result).unwrap());
        // The form has no [race]: the CLI canoe.toml's is used.
        let (st, ct, out) = a.handle("POST", "/api/race", &body);
        assert_eq!((st, ct), (200, "application/json"), "{out}");
        let v: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["format"], crate::race::RACE_FORMAT);
        assert_eq!(v["events"].as_array().unwrap().len(), 1);
        assert!(v["events"][0]["speed_m"].as_array().unwrap().len() > 100);
        // The form edits [race] (each event's crew): its own [race] is used as posted.
        let mut edited = params.clone();
        let ev = &mut edited.race.as_mut().unwrap().events[0];
        (ev.name, ev.load_case, ev.weights_lb) = ("edited".into(), None, vec![150.0, 150.0]);
        let v: serde_json::Value =
            serde_json::from_str(&a.handle("POST", "/api/race", &toml::to_string(&edited).unwrap()).2)
                .unwrap();
        assert_eq!(v["events"][0]["name"], "edited");
        assert_eq!(v["events"][0]["crew_lb"], 300.0);
        assert!(v["events"][0].get("load_case").is_none());
        a.lock().state = "running";
        assert_eq!(a.handle("POST", "/api/race", &body).0, 409);
        assert_eq!(a.handle("GET", "/api/race", "").0, 405);
        // A canoe.toml with no [race] at all: a clear 400 naming the file.
        let (b, p2) = app_on(|p| p.race = None);
        b.lock().result = Some(serde_json::to_string(&result).unwrap());
        let (st, _, msg) = b.handle("POST", "/api/race", &toml::to_string(&p2).unwrap());
        assert!(
            st == 400 && msg.contains("no [race] section in") && msg.contains("canoe.toml"),
            "{st} {msg}"
        );
    }

    #[test]
    fn old_prefs_without_race_get_it_from_the_file() {
        let (a, params) = app_on(|_| {});
        let mut old = params.clone();
        old.race = None;
        // Saved prefs that predate [race]: params come back with the file's [race], and saving
        // them again stores it.
        std::fs::write(&a.prefs_path, toml::to_string(&old).unwrap()).unwrap();
        let v = params_of(&a);
        assert_eq!(v["source"], "prefs");
        assert_eq!(v["params"]["race"]["event"].as_array().unwrap().len(), 9);
        assert_eq!(a.handle("POST", "/api/prefs", &toml::to_string(&old).unwrap()).0, 200);
        let saved = std::fs::read_to_string(&a.prefs_path).unwrap();
        assert!(saved.contains("[race]"));
    }

    #[test]
    fn export_needs_a_winner_and_returns_files() {
        let a = app();
        assert_eq!(a.export("format=step").0, 404);
        let params = Params::parse(&a.toml_text, &a.base_dir).unwrap();
        let result = crate::run(&params, &|_| {}).unwrap();
        a.lock().result = Some(serde_json::to_string(&result).unwrap());
        let (st, ct, file, body) = a.export("format=step");
        assert_eq!((st, ct), (200, "application/step"));
        assert!(file.unwrap().ends_with(".step"));
        assert!(
            body.starts_with(b"ISO-10303-21;") && String::from_utf8_lossy(&body).contains("CLOSED_SHELL")
        );
        let (st, _, file, body) = a.export("format=stl");
        assert_eq!(st, 200);
        assert!(file.unwrap().ends_with(".stl"));
        let n = u32::from_le_bytes(body[80..84].try_into().unwrap()) as usize;
        assert_eq!(body.len(), 84 + 50 * n);
        assert_eq!(a.export("format=obj").0, 400);
        // A pocket shorter than the 161-station spacing (1.5 in) still exports: stations refine.
        let mut short: serde_json::Value = serde_json::to_value(&result).unwrap();
        short["winner"]["candidate"]["air_pocket_in"] = serde_json::json!(1.2);
        a.lock().result = Some(short.to_string());
        assert_eq!(a.export("format=stl").0, 200);
        let mut no_winner: serde_json::Value = serde_json::to_value(&result).unwrap();
        no_winner["winner"] = serde_json::Value::Null;
        a.lock().result = Some(no_winner.to_string());
        assert_eq!(a.export("format=step").0, 409);
    }

    #[test]
    fn run_job_never_panics_out() {
        // Whatever the optimizer does (result, Err, or panic), the job ends in done/error.
        let a = app();
        let params = Params::parse(&a.toml_text, &a.base_dir).unwrap();
        let dir = std::env::temp_dir().join(format!("jaguar_serve_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        a.lock().state = "running";
        run_job(params, Arc::clone(&a.shared), dir.join("result.json"), &optimize::RunControl::default());
        let s = a.lock();
        assert!(s.state == "done" || (s.state == "error" && s.error.is_some()), "{}", s.state);
    }

    #[test]
    fn time_left_is_the_rate_so_far_times_what_is_left_without_pauses() {
        let ago = |s: u64| Instant::now().checked_sub(Duration::from_secs(s)).unwrap();
        let mut s =
            Shared { state: "running", started: Some(ago(10)), done: 10, total: 20, ..Default::default() };
        let near = |a: Option<f64>, b: f64| a.is_some_and(|a| (a - b).abs() < 0.5);
        assert!(near(s.eta_s(), 10.0), "{:?}", s.eta_s()); // 10 left at 1 per second
        // A shape search counts what it may still plan (refine rounds at full size).
        s.planned = Some(100);
        assert!(near(s.eta_s(), 90.0), "{:?}", s.eta_s());
        // 4 s of the 10 were paused: 10 done in 6 running seconds.
        s.paused_s = 4.0;
        assert!(near(s.active_s(), 6.0) && near(s.eta_s(), 54.0), "{:?}", s.eta_s());
        // A pause going on now does not count either.
        s.paused_at = Some(ago(2));
        assert!(near(s.active_s(), 4.0));
        s.end_pause();
        assert!(s.paused_at.is_none() && (s.paused_s - 6.0).abs() < 0.5);
        // Fewer done than the first wave (they land together): no estimate yet.
        s.wave = 11;
        assert!(s.eta_s().is_none());
        s.wave = 0;
        // Nothing done yet, ending, or not running: no estimate.
        s.done = 0;
        assert!(s.eta_s().is_none());
        s.done = 10;
        s.ctl.stop();
        assert!(s.eta_s().is_none());
        assert!(Shared::default().eta_s().is_none());
    }

    #[test]
    fn pause_resume_stop_need_a_run_and_stop_ends_it() {
        let a = app();
        for path in ["/api/pause", "/api/resume", "/api/stop"] {
            assert_eq!(a.handle("POST", path, "").0, 409, "{path} with no run");
        }
        a.lock().state = "running";
        assert_eq!(a.handle("POST", "/api/pause", "").0, 200);
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/status", "").2).unwrap();
        assert_eq!((v["paused"].as_bool(), v["stopping"].as_bool()), (Some(true), Some(false)));
        assert_eq!(a.handle("POST", "/api/resume", "").0, 200);
        assert!(!a.lock().ctl.paused());
        assert_eq!(a.handle("POST", "/api/stop", "").0, 200);
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/status", "").2).unwrap();
        assert_eq!(v["stopping"].as_bool(), Some(true));
        // A fixed-hull run that is ended: state "stopped", the previous result kept, no error.
        let params = Params::parse(&a.toml_text, &a.base_dir).unwrap();
        let dir = tmpdir();
        a.lock().result = Some("previous".into());
        let ctl = Arc::clone(&a.lock().ctl);
        run_job(params, Arc::clone(&a.shared), dir.join("result.json"), &ctl);
        let s = a.lock();
        assert_eq!((s.state, s.error.as_deref(), s.result.as_deref()), ("stopped", None, Some("previous")));
        assert!(!dir.join("result.json").exists());
    }

    #[test]
    fn desktop_endpoints_only_in_desktop_mode() {
        let mut a = app();
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/session", "").2).unwrap();
        assert_eq!((v["app"].as_str(), v["desktop"].as_bool()), (Some("jaguar"), Some(false)));
        assert_eq!(a.handle("POST", "/api/quit", "").0, 403);
        assert_eq!(a.handle("POST", "/api/open-folder", "").0, 403);
        assert!(!a.quit.load(Ordering::SeqCst));
        a.desktop = Some(tmpdir());
        let v: serde_json::Value = serde_json::from_str(&a.handle("GET", "/api/session", "").2).unwrap();
        assert_eq!(v["desktop"].as_bool(), Some(true));
        assert!(v["folder"].is_string());
        assert_eq!(a.handle("POST", "/api/quit", "").0, 200);
        assert!(a.quit.load(Ordering::SeqCst));
    }

    #[test]
    fn desktop_server_is_found_and_quits_when_idle() {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let canoe = Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/canoe.toml")).to_path_buf();
        let prefs = tmpdir().join("prefs.toml");
        let t = std::thread::spawn(move || {
            serve_desktop(&canoe, prefs, server, tmpdir(), Duration::from_secs(2)).unwrap()
        });
        // The launcher recognises it (and the probe itself counts as activity) ...
        assert!(crate::launch::running_desktop(port));
        // ... and with no page asking, it stops by itself.
        let t0 = Instant::now();
        while !t.is_finished() {
            assert!(t0.elapsed() < Duration::from_secs(30), "desktop server did not quit when idle");
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(!crate::launch::running_desktop(port));
    }

    #[test]
    fn a_stuck_client_does_not_block_other_requests() {
        // Some local tools probe localhost ports and never finish their request.
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let a = app();
        std::thread::spawn(move || serve_on(Arc::new(server), Arc::new(a)));
        let mut stuck = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
        write!(stuck, "POST /api/prefs HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\npartial").unwrap();
        std::thread::sleep(Duration::from_millis(200));
        let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        write!(s, "GET /api/session HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").unwrap();
        let mut out = String::new();
        s.read_to_string(&mut out).expect("answered while another request hangs");
        assert!(out.starts_with("HTTP/1.1 200") && out.contains("\"app\":\"jaguar\""), "{out}");
        drop(stuck);
    }

    #[test]
    fn socket_smoke() {
        let a = app();
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        std::thread::spawn(move || serve_on(Arc::new(server), Arc::new(a)));
        let get = |path: &str| {
            let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
            write!(s, "GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").unwrap();
            let mut out = String::new();
            s.read_to_string(&mut out).unwrap();
            out
        };
        let r = get("/");
        assert!(r.starts_with("HTTP/1.1 200"), "{r}");
        assert!(r.contains("<html"));
        let r = get("/api/params");
        assert!(r.starts_with("HTTP/1.1 200"), "{r}");
        assert!(r.contains("\"toml\""));
    }
}
