//! Desktop launch: `jaguar` with no arguments (a double-click on JAGUAR.exe or JAGUAR.app).
//!
//! Makes the canoe folder `Documents/JAGUAR` (filled with the starter canoe.toml, hull and lab
//! data the first time; existing files are never overwritten), starts the web UI on the first
//! free port from 8000 and opens it in the default browser. A JAGUAR already running from a
//! double-click is reused instead of starting a second one. The server quits on the page's Quit
//! button or after [`IDLE`] with no page open (and no run going): there is no window to close.
//! Errors are shown in a dialog, since there may be no terminal to print them to.
//!
//! OS glue only (folders, browser, dialogs); the physics never depends on it.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Folder name under Documents.
pub const FOLDER: &str = "JAGUAR";
/// Ports tried in order.
pub const PORTS: std::ops::RangeInclusive<u16> = 8000..=8009;
/// No request for this long and no run going: the desktop server quits. Longer than a
/// background tab's throttled timer (browsers fire them once a minute), so an open page keeps
/// it alive with its 30 s ping.
pub const IDLE: Duration = Duration::from_secs(300);

/// Starter files written into a new canoe folder: (file name, contents). canoe.toml is the
/// team's settings (packaging/starter/canoe.toml), not examples/canoe.toml (tests pin that one).
pub fn starter_files() -> [(&'static str, String); 3] {
    [
        ("canoe.toml", include_str!("../packaging/starter/canoe.toml").to_string()),
        ("off1_hull.json", include_str!("../examples/off1_hull.json").to_string()),
        ("mix_tests.csv", include_str!("../data/mix_tests.csv").to_string()),
    ]
}

/// Create `dir` and write each starter file that is missing. Returns the canoe.toml path.
pub fn ensure_folder(dir: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    for (name, text) in starter_files() {
        let p = dir.join(name);
        if !p.exists() {
            std::fs::write(&p, text.replace("\r\n", "\n"))
                .with_context(|| format!("writing {}", p.display()))?;
        }
    }
    Ok(dir.join("canoe.toml"))
}

/// The user's home folder: `$HOME`, else `%USERPROFILE%` (Windows outside a Unix shell).
pub fn home_dir() -> Option<PathBuf> {
    ["HOME", "USERPROFILE"]
        .iter()
        .find_map(|k| std::env::var_os(k).filter(|v| !v.is_empty()))
        .map(PathBuf::from)
}

/// The user's Documents folder (on Windows the real one, also when OneDrive moved it), else home.
pub fn documents_dir() -> Result<PathBuf> {
    #[cfg(windows)]
    if let Some(d) = win::known_documents() {
        return Ok(d);
    }
    let home = home_dir().context("cannot find your home folder (no HOME or USERPROFILE)")?;
    let docs = home.join("Documents");
    Ok(if docs.is_dir() { docs } else { home })
}

/// Open a URL or folder with the system default (browser, Explorer, Finder).
pub fn open(target: &str) -> Result<()> {
    let mut cmd = if cfg!(windows) {
        if target.starts_with("http") {
            let mut c = std::process::Command::new("rundll32");
            c.args(["url.dll,FileProtocolHandler", target]);
            c
        } else {
            let mut c = std::process::Command::new("explorer");
            c.arg(target);
            c
        }
    } else if cfg!(target_os = "macos") {
        let mut c = std::process::Command::new("open");
        c.arg(target);
        c
    } else {
        let mut c = std::process::Command::new("xdg-open");
        c.arg(target);
        c
    };
    // No inherited stdio: after a double-click the console is gone and passing its dead handles
    // fails the spawn ("The request is not supported").
    use std::process::Stdio;
    cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    // explorer.exe exits 1 even when it opened the folder: only a failed spawn is an error.
    cmd.spawn().with_context(|| format!("opening {target}"))?;
    Ok(())
}

/// Show an error where the user can see it: a dialog (Windows, macOS), else stderr.
pub fn alert(msg: &str) {
    eprintln!("JAGUAR: {msg}");
    #[cfg(windows)]
    win::message_box("JAGUAR", msg);
    if cfg!(target_os = "macos") {
        let quote = |s: &str| format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""));
        let script = format!("display alert \"JAGUAR\" message {} as critical", quote(msg));
        let _ = std::process::Command::new("osascript").args(["-e", &script]).status();
    }
}

/// True when a double-clicked JAGUAR (desktop mode) answers on `port`.
pub fn running_desktop(port: u16) -> bool {
    use std::io::{Read, Write};
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    if write!(s, "GET /api/session HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").is_err() {
        return false;
    }
    let mut out = String::new();
    let _ = s.read_to_string(&mut out);
    out.contains("\"app\":\"jaguar\"") && out.contains("\"desktop\":true")
}

/// `jaguar` with no arguments. Errors are shown in a dialog by the caller ([`alert`]).
pub fn launch() -> Result<()> {
    #[cfg(windows)]
    win::hide_own_console();
    let folder = documents_dir()?.join(FOLDER);
    let canoe = ensure_folder(&folder)?;
    let prefs = home_dir()
        .context("cannot find your home folder (no HOME or USERPROFILE)")?
        .join(".config")
        .join("jaguar")
        .join("app-prefs.toml");
    let mut server = None;
    for port in PORTS {
        if running_desktop(port) {
            println!("JAGUAR is already running: opening http://127.0.0.1:{port}/");
            return open(&format!("http://127.0.0.1:{port}/"));
        }
        if let Ok(s) = tiny_http::Server::http(("127.0.0.1", port)) {
            server = Some((s, port));
            break;
        }
    }
    let (server, port) = server.with_context(|| {
        format!("ports {}-{} are all in use: close another program and try again", PORTS.start(), PORTS.end())
    })?;
    let url = format!("http://127.0.0.1:{port}/");
    println!("JAGUAR running at {url}  (canoe folder {})", folder.display());
    open(&url)?;
    crate::serve::serve_desktop(&canoe, prefs, server, folder, IDLE)
}

#[cfg(windows)]
mod win {
    use std::ffi::c_void;
    use std::os::windows::ffi::{OsStrExt, OsStringExt};
    use std::path::PathBuf;

    #[repr(C)]
    struct Guid(u32, u16, u16, [u8; 8]);
    const FOLDERID_DOCUMENTS: Guid =
        Guid(0xFDD3_9AD0, 0x238F, 0x46AF, [0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7]);

    #[link(name = "shell32")]
    unsafe extern "system" {
        fn SHGetKnownFolderPath(id: *const Guid, flags: u32, token: *mut c_void, path: *mut *mut u16) -> i32;
    }
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(p: *mut c_void);
    }
    #[link(name = "user32")]
    unsafe extern "system" {
        fn MessageBoxW(hwnd: *mut c_void, text: *const u16, caption: *const u16, kind: u32) -> i32;
    }
    unsafe extern "system" {
        fn GetConsoleProcessList(list: *mut u32, count: u32) -> u32;
        fn FreeConsole() -> i32;
    }

    /// Documents as Explorer shows it (follows OneDrive folder redirection).
    pub fn known_documents() -> Option<PathBuf> {
        let mut p: *mut u16 = std::ptr::null_mut();
        // SAFETY: valid GUID pointer and out-pointer; the returned buffer is NUL-terminated
        // and freed with CoTaskMemFree, as the API requires (also on failure).
        unsafe {
            let hr = SHGetKnownFolderPath(&FOLDERID_DOCUMENTS, 0, std::ptr::null_mut(), &mut p);
            let out = (hr == 0 && !p.is_null()).then(|| {
                let len = (0..).take_while(|&i| *p.add(i) != 0).count();
                PathBuf::from(std::ffi::OsString::from_wide(std::slice::from_raw_parts(p, len)))
            });
            CoTaskMemFree(p.cast());
            out.filter(|d| d.is_dir())
        }
    }

    fn wide(s: &str) -> Vec<u16> {
        std::ffi::OsStr::new(s).encode_wide().chain(Some(0)).collect()
    }

    pub fn message_box(title: &str, msg: &str) {
        const MB_ICONERROR: u32 = 0x10;
        // SAFETY: both strings are NUL-terminated UTF-16 that outlive the call.
        unsafe {
            MessageBoxW(std::ptr::null_mut(), wide(msg).as_ptr(), wide(title).as_ptr(), MB_ICONERROR);
        }
    }

    /// A double-click gives the program a console window of its own: close it (the page is the
    /// interface). Started from a terminal, the console is shared and stays.
    pub fn hide_own_console() {
        let mut ids = [0u32; 2];
        // SAFETY: the buffer holds `ids.len()` entries.
        unsafe {
            if GetConsoleProcessList(ids.as_mut_ptr(), ids.len() as u32) == 1 {
                FreeConsole();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("jaguar_launch_{}_{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn starter_folder_runs_on_its_own_and_keeps_user_files() {
        let d = tmpdir("starter");
        let canoe = ensure_folder(&d).unwrap();
        // Self-contained: the canoe.toml loads with only the starter files next to it.
        let p = crate::params::Params::load(&canoe).unwrap();
        assert!(p.hull.file.as_ref().is_none_or(|h| h.starts_with(&d)));
        assert!(p.material.mix_data.starts_with(&d));
        crate::optimize::strength(&p).unwrap();
        let text = std::fs::read_to_string(&canoe).unwrap();
        assert!(
            !text.contains("examples/canoe") && !text.contains("../data") && !text.contains(":\\"),
            "starter refers outside its folder"
        );
        // A second launch never overwrites what the user edited.
        std::fs::write(&canoe, "# mine\n").unwrap();
        ensure_folder(&d).unwrap();
        assert_eq!(std::fs::read_to_string(&canoe).unwrap(), "# mine\n");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn documents_folder_exists() {
        assert!(documents_dir().unwrap().is_dir());
    }

    #[test]
    fn nothing_listening_is_not_a_running_jaguar() {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        assert!(!running_desktop(port));
    }
}
