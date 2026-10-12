//! Optional localhost viewer. PLAN task T7.
//!
//! `jaguar serve result.json` starts tiny_http on 127.0.0.1:<port> and serves:
//! * `GET /`             -> `web/index.html` (embedded with include_str!)
//! * `GET /result.json`  -> the result file, re-read on every request so a
//!   new `jaguar run` shows up on browser refresh
//! * `GET /hull.json`    -> the hull referenced by result.params.hull.file
//!
//! The viewer is read-only and has no build step: one HTML file, three.js
//! from a CDN. The optimizer must never depend on this module.

use anyhow::Result;
use std::path::Path;

pub fn serve(result_path: &Path, port: u16) -> Result<()> {
    let _ = (result_path, port);
    todo!("T7")
}
