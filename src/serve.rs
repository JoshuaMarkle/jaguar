//! Optional localhost web UI. PLAN task T7. Owner: web agent.
//!
//! `jaguar serve [canoe.toml] [-p 8000]` starts tiny_http on 127.0.0.1:<port>.
//! The page shows the canoe in 3D, lets the user edit canoe.toml text and
//! press Run, and watches the optimization live. The optimizer never depends
//! on this module.
//!
//! HTTP API (all JSON unless noted; this is the contract with web/index.html):
//! * `GET  /`              -> web/index.html (embedded with include_str!)
//! * `GET  /api/params`    -> {"toml": "<text>", "params": <Params as JSON, hull.file
//!   made relative to the CLI canoe.toml dir>, "dir": "<that dir>"} (UI prefills its form)
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
//! * `GET  /api/status`    -> {"state": "idle"|"running"|"done"|"error",
//!   "done": n, "total": n, "error": str|null,
//!   "shape": {"index": i, "shapes": n} | null (shape search only), "candidates": [...new since ?since=k...]}
//!   Poll every ~250 ms with `?since=<count already seen>` to get only new
//!   candidates (cheap incremental updates; no websockets needed).
//! * `GET  /api/mix`       -> the mix::StrengthFit (lab points, fit line, tested
//!   range) for the most recently submitted params (startup params before any run).
//!   500 {"error"} if the mix data cannot be loaded or fitted.
//! * `GET  /api/result`    -> the last jaguar-result/1, 404 if none yet.
//!   Also written to `result.json` next to the canoe.toml when a run finishes.
//!
//! Any panic or Err inside a run must land as state "error" with a message,
//! never kill the server.

use crate::hull::Hull;
use crate::optimize::{self, Candidate, Progress};
use crate::params::Params;
use crate::report;
use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

const INDEX_HTML: &str = include_str!("../web/index.html");

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
}

/// Request handler, independent of the socket so it can be unit tested.
struct App {
    toml_text: String,
    /// Params as loaded at startup, hull.file relative to `base_dir` (for the form).
    params_json: serde_json::Value,
    base_dir: PathBuf,
    /// Params as loaded at startup (absolute paths).
    params: Params,
    result_path: PathBuf,
    shared: Arc<Mutex<Shared>>,
}

type Reply = (u16, &'static str, String);

fn json_reply(status: u16, v: serde_json::Value) -> Reply {
    (status, "application/json", v.to_string())
}

fn err_reply(status: u16, msg: impl std::fmt::Display) -> Reply {
    json_reply(status, serde_json::json!({ "error": msg.to_string() }))
}

impl App {
    fn new(params_path: &Path) -> Result<App> {
        let toml_text = std::fs::read_to_string(params_path)
            .with_context(|| format!("reading {}", params_path.display()))?;
        let params = Params::load(params_path)?;
        let base_dir = params_path.parent().unwrap_or(Path::new("")).to_path_buf();
        let mut rel = params.clone();
        if let Some(f) = &rel.hull.file
            && let Ok(r) = f.strip_prefix(&base_dir)
        {
            rel.hull.file = Some(r.to_path_buf());
        }
        if let Ok(r) = rel.material.mix_data.strip_prefix(&base_dir) {
            rel.material.mix_data = r.to_path_buf();
        }
        let params_json = serde_json::to_value(&rel)?;
        let startup = params.clone();
        let result_path = base_dir.join("result.json");
        let shared = Shared {
            state: "idle",
            hull_file: params.hull.file.clone().unwrap_or_default(),
            ..Default::default()
        };
        Ok(App {
            toml_text,
            params_json,
            base_dir,
            params: startup,
            result_path,
            shared: Arc::new(Mutex::new(shared)),
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
            ("GET", "/api/params") => json_reply(
                200,
                serde_json::json!({
                    "toml": self.toml_text,
                    "params": self.params_json,
                    "dir": self.base_dir.display().to_string(),
                }),
            ),
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
                let p = self.lock().params.clone().unwrap_or_else(|| self.params.clone());
                match optimize::strength(&p).and_then(|f| Ok(serde_json::to_string(&f)?)) {
                    Ok(s) => (200, "application/json", s),
                    Err(e) => err_reply(500, format!("{e:#}")),
                }
            }
            ("GET", "/api/result") => match self.lock().result.clone() {
                Some(s) => (200, "application/json", s),
                None => err_reply(404, "no result yet"),
            },
            ("POST", "/api/run") => self.start_run(body),
            (
                _,
                "/" | "/index.html" | "/api/params" | "/api/hulls" | "/api/hull" | "/api/status" | "/api/mix"
                | "/api/result" | "/api/run",
            ) => err_reply(405, "method not allowed"),
            _ => err_reply(404, "not found"),
        }
    }

    /// Every jaguar-hull/1 JSON in `base_dir`, sorted by file name. Read fresh each call.
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
                "done": s.done,
                "total": s.total,
                "error": s.error,
                "shape": s.shape.map(|(i, n)| serde_json::json!({ "index": i, "shapes": n })),
                "candidates": new,
            }),
        )
    }

    fn start_run(&self, body: &str) -> Reply {
        let params = match Params::parse(body, &self.base_dir) {
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
                ..Default::default()
            };
        }
        let shared = Arc::clone(&self.shared);
        let out = self.result_path.clone();
        std::thread::spawn(move || run_job(params, shared, out));
        json_reply(202, serde_json::json!({ "ok": true }))
    }
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

fn run_job(params: Params, shared: Arc<Mutex<Shared>>, out: PathBuf) {
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
        let result = optimize::run_params(&params, &on_progress)?;
        report::write_json(&result, &out)?;
        Ok(serde_json::to_string_pretty(&result)?)
    }));
    let mut s = lock(&shared);
    match outcome {
        Ok(Ok(json)) => {
            s.result = Some(json);
            s.state = "done";
        }
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

fn serve_on(server: tiny_http::Server, app: App) {
    for mut req in server.incoming_requests() {
        let mut body = String::new();
        if req.as_reader().read_to_string(&mut body).is_err() {
            body.clear();
        }
        let (status, ctype, text) = app.handle(req.method().as_str(), req.url(), &body);
        let header = tiny_http::Header::from_bytes(&b"Content-Type"[..], ctype.as_bytes())
            .expect("static header is valid");
        let resp = tiny_http::Response::from_string(text).with_status_code(status).with_header(header);
        let _ = req.respond(resp);
    }
}

pub fn serve(params_path: &Path, port: u16) -> Result<()> {
    let app = App::new(params_path)?;
    let server = tiny_http::Server::http(("127.0.0.1", port))
        .map_err(|e| anyhow::anyhow!("binding 127.0.0.1:{port}: {e}"))?;
    println!("JAGUAR serving http://127.0.0.1:{port}/  (Ctrl-C to stop)");
    serve_on(server, app);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read as _, Write as _};

    fn app() -> App {
        App::new(Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/examples/canoe.toml"))).unwrap()
    }

    #[test]
    fn routes() {
        let a = app();
        let (st, ct, body) = a.handle("GET", "/", "");
        assert_eq!((st, ct), (200, "text/html; charset=utf-8"));
        assert!(body.contains("three"));
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

    #[test]
    fn mix_endpoint_returns_fit() {
        let a = app();
        let (st, _, body) = a.handle("GET", "/api/mix", "");
        assert_eq!(st, 200, "{body}");
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(!v["points"].as_array().unwrap().is_empty());
        assert!(v["split_slope_psi_per_pcf"].as_f64().unwrap().is_finite());
        assert!(v["density_min_pcf"].as_f64().unwrap() <= v["density_max_pcf"].as_f64().unwrap());
        assert_eq!(a.handle("POST", "/api/mix", "").0, 405);
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
                    thickness_in: 0.5,
                    density_pcf: 60.0,
                    depth_in: 12.0 + i as f64,
                    hull_weight_lb: 100.0,
                    verdict: None,
                    governing_case: None,
                    error: Some("x".into()),
                    passes: false,
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
    fn run_job_never_panics_out() {
        // Whatever the optimizer does (result, Err, or panic), the job ends in done/error.
        let a = app();
        let params = Params::parse(&a.toml_text, &a.base_dir).unwrap();
        let dir = std::env::temp_dir().join(format!("jaguar_serve_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        a.lock().state = "running";
        run_job(params, Arc::clone(&a.shared), dir.join("result.json"));
        let s = a.lock();
        assert!(s.state == "done" || (s.state == "error" && s.error.is_some()), "{}", s.state);
    }

    #[test]
    fn socket_smoke() {
        let a = app();
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        std::thread::spawn(move || serve_on(server, a));
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
