//! Terminal output and result.json. PLAN task T6.
//!
//! * `print_summary`: one line per load case header, a compact table of the
//!   passing frontier (lightest passing design per thickness), then the
//!   winner block with VERIFIED / NOT VERIFIED and any grid-edge warning.
//!   No colours unless stdout is a TTY. Keep it under ~40 lines.
//! * `write_json`: `jaguar-result/1`, pretty-printed, deterministic.

use crate::optimize::RunResult;
use anyhow::Result;
use std::path::Path;

pub fn print_summary(result: &RunResult) {
    let _ = result;
    todo!("T6")
}

pub fn write_json(result: &RunResult, path: &Path) -> Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(result)?)?;
    Ok(())
}
