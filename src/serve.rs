//! Optional localhost web UI. PLAN task T7. Owner: web agent.
//!
//! `jaguar serve [canoe.toml] [-p 8000]` starts tiny_http on 127.0.0.1:<port>.
//! The page shows the canoe in 3D, lets the user edit canoe.toml text and
//! press Run, and watches the optimization live. The optimizer never depends
//! on this module.
//!
//! HTTP API (all JSON unless noted; this is the contract with web/index.html):
//! * `GET  /`              -> web/index.html (embedded with include_str!)
//! * `GET  /api/params`    -> {"toml": "<text of the canoe.toml given on the CLI>"}
//! * `GET  /api/hull`      -> jaguar-hull/1 of the current params' hull file
//! * `POST /api/run`       body = canoe.toml text. Validates with
//!   `Params::parse(text, <dir of the CLI canoe.toml>)`; 400 {"error"} if
//!   invalid, 409 if a run is already going, else 202 {"ok":true} and starts
//!   `optimize::run_with_progress` on a background thread.
//! * `GET  /api/status`    -> {"state": "idle"|"running"|"done"|"error",
//!   "done": n, "total": n, "error": str|null, "candidates": [...new since ?since=k...]}
//!   Poll every ~250 ms with `?since=<count already seen>` to get only new
//!   candidates (cheap incremental updates; no websockets needed).
//! * `GET  /api/result`    -> the last jaguar-result/1, 404 if none yet.
//!   Also written to `result.json` next to the canoe.toml when a run finishes.
//!
//! Any panic or Err inside a run must land as state "error" with a message,
//! never kill the server.

use anyhow::Result;
use std::path::Path;

pub fn serve(params_path: &Path, port: u16) -> Result<()> {
    let _ = (params_path, port);
    todo!("T7")
}
