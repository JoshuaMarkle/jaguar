//! Build stamp for the drawing sheets (src/sheets.rs): `JAGUAR_GIT` = "<sha>[-dirty] (<branch>)"
//! from the checkout being built, "unknown" without git. Dirty = tracked files differ from HEAD.
//! Re-run when the sources, the index or HEAD change (not on every file in the folder).

use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn main() {
    for p in
        ["src", "web", "Cargo.toml", "Cargo.lock", "build.rs", ".git/HEAD", ".git/index", ".git/refs/heads"]
    {
        println!("cargo:rerun-if-changed={p}");
    }
    let stamp = match git(&["rev-parse", "--short=10", "HEAD"]) {
        Some(sha) => {
            let dirty = git(&["status", "--porcelain", "--untracked-files=no"]).is_none_or(|s| !s.is_empty());
            let branch = git(&["rev-parse", "--abbrev-ref", "HEAD"]).unwrap_or_else(|| "?".into());
            format!("{sha}{} ({branch})", if dirty { "-dirty" } else { "" })
        }
        None => "unknown".into(),
    };
    println!("cargo:rustc-env=JAGUAR_GIT={stamp}");
}
