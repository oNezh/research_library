use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri::window::Color;

const BACKEND_PORT: u16 = 8230;
const DEFAULT_REPO: &str = "/Users/zenn/program/research_library_exploration";

struct BackendProcess(Mutex<Option<Child>>);

fn repo_root() -> PathBuf {
    std::env::var("RESEARCH_COMPANION_REPO")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_REPO))
}

fn find_uv() -> Option<PathBuf> {
    // GUI apps on macOS do not inherit the shell PATH.
    let home = std::env::var("HOME").unwrap_or_default();
    let candidates = [
        format!("{home}/.local/bin/uv"),
        "/opt/homebrew/bin/uv".to_string(),
        "/usr/local/bin/uv".to_string(),
    ];
    for c in candidates {
        let p = PathBuf::from(&c);
        if p.is_file() {
            return Some(p);
        }
    }
    which_uv_from_path()
}

fn which_uv_from_path() -> Option<PathBuf> {
    let path = std::env::var("PATH").unwrap_or_default();
    for dir in path.split(':') {
        let p = PathBuf::from(dir).join("uv");
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

fn backend_alive() -> bool {
    TcpStream::connect_timeout(
        &format!("127.0.0.1:{BACKEND_PORT}").parse().unwrap(),
        Duration::from_millis(400),
    )
    .is_ok()
}

/// TCP open does not mean FastAPI is serving /api/health yet.
fn backend_http_ready() -> bool {
    let mut stream = match TcpStream::connect_timeout(
        &format!("127.0.0.1:{BACKEND_PORT}").parse().unwrap(),
        Duration::from_millis(500),
    ) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let req = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 1024];
    let n = match stream.read(&mut buf) {
        Ok(n) if n > 12 => n,
        _ => return false,
    };
    let resp = String::from_utf8_lossy(&buf[..n]);
    resp.starts_with("HTTP/1.1 200") || resp.starts_with("HTTP/1.0 200")
}

/// Kill any process bound to the backend port so each app launch serves fresh assets.
fn stop_stale_backend() {
    #[cfg(unix)]
    {
        let output = Command::new(if cfg!(target_os = "macos") {
            "/usr/sbin/lsof"
        } else {
            "lsof"
        })
            .args(["-ti", &format!("tcp:{BACKEND_PORT}")])
            .output();
        if let Ok(out) = output {
            for line in String::from_utf8_lossy(&out.stdout).lines() {
                if let Ok(pid) = line.trim().parse::<i32>() {
                    if pid > 0 {
                        unsafe {
                            libc::kill(pid, libc::SIGTERM);
                        }
                    }
                }
            }
        }
        for _ in 0..10 {
            if !backend_alive() {
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        if backend_alive() {
            let output = Command::new(if cfg!(target_os = "macos") {
                "/usr/sbin/lsof"
            } else {
                "lsof"
            })
                .args(["-ti", &format!("tcp:{BACKEND_PORT}")])
                .output();
            if let Ok(out) = output {
                for line in String::from_utf8_lossy(&out.stdout).lines() {
                    if let Ok(pid) = line.trim().parse::<i32>() {
                        if pid > 0 {
                            unsafe {
                                libc::kill(pid, libc::SIGKILL);
                            }
                        }
                    }
                }
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
}

/// Spawn a fresh FastAPI backend (clears stale listeners on :8230 first).
fn ensure_backend() -> Result<Child, String> {
    stop_stale_backend();
    if backend_alive() {
        return Err(format!(
            "port {BACKEND_PORT} is still in use; quit other research-lib serve processes"
        ));
    }
    let uv = find_uv().ok_or("uv executable not found (install: https://docs.astral.sh/uv/)")?;
    let root = repo_root();
    if !root.join("pyproject.toml").is_file() {
        return Err(format!(
            "repo not found at {} (set RESEARCH_COMPANION_REPO)",
            root.display()
        ));
    }
    let log_path = std::env::temp_dir().join("research_companion_backend.log");
    let log_file = std::fs::File::create(&log_path)
        .map_err(|e| format!("cannot create backend log: {e}"))?;
    let err_file = log_file.try_clone().map_err(|e| e.to_string())?;

    let mut cmd = Command::new(uv);
    cmd.args([
        "run",
        "research-lib",
        "serve",
        "--port",
        &BACKEND_PORT.to_string(),
    ])
    .current_dir(&root)
    .stdout(Stdio::from(log_file))
    .stderr(Stdio::from(err_file));

    // Own process group so we can kill uv + python together on exit.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }

    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn backend: {e}"))?;

    for _ in 0..120 {
        if backend_http_ready() {
            return Ok(child);
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    Err(format!(
        "backend did not become ready within 60s; see {}",
        std::env::temp_dir()
            .join("research_companion_backend.log")
            .display()
    ))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(BackendProcess(Mutex::new(None)))
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let url: WebviewUrl = match ensure_backend() {
                Ok(child) => {
                    let state = app.state::<BackendProcess>();
                    *state.0.lock().unwrap() = Some(child);
                    format!("http://127.0.0.1:{BACKEND_PORT}/?v={}", std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_secs())
                        .unwrap_or(0))
                    .parse()
                    .map(WebviewUrl::External)?
                }
                Err(msg) => {
                    log::error!("backend startup failed: {msg}");
                    let encoded = urlencoding_encode(&msg);
                    format!("data:text/html;charset=utf-8,<h2>后端启动失败</h2><pre>{encoded}</pre>")
                        .parse()
                        .map(WebviewUrl::External)?
                }
            };

            let win_builder = {
                let b = WebviewWindowBuilder::new(app, "main", url)
                    .title("一问")
                    .inner_size(1280.0, 840.0)
                    .min_inner_size(900.0, 600.0)
                    .decorations(false);
                #[cfg(target_os = "macos")]
                let b = b
                    .transparent(true)
                    .background_color(Color(0, 0, 0, 0))
                    .shadow(true);
                b
            };
            let window = win_builder.build()?;
            #[cfg(target_os = "macos")]
            {
                use objc2_app_kit::{NSColor, NSWindow, NSWindowButton, NSWindowStyleMask};
                use objc2_foundation::{NSObjectNSKeyValueCoding, NSNumber, NSString};
                use objc2_web_kit::WKWebView;

                window.with_webview(|webview| {
                    unsafe {
                        let ns_window: &NSWindow = &*webview.ns_window().cast();
                        let mask = NSWindowStyleMask::Borderless
                            | NSWindowStyleMask::Resizable
                            | NSWindowStyleMask::Miniaturizable;
                        ns_window.setStyleMask(mask);
                        ns_window.setOpaque(false);
                        let clear = NSColor::clearColor();
                        ns_window.setBackgroundColor(Some(&clear));

                        for button in [
                            NSWindowButton::CloseButton,
                            NSWindowButton::MiniaturizeButton,
                            NSWindowButton::ZoomButton,
                        ] {
                            if let Some(btn) = ns_window.standardWindowButton(button) {
                                btn.setHidden(true);
                            }
                        }

                        let wk: &WKWebView = &*webview.inner().cast();
                        let no = NSNumber::numberWithBool(false);
                        wk.setValue_forKey(Some(&no), &NSString::from_str("drawsBackground"));
                        wk.setUnderPageBackgroundColor(Some(&clear));
                    }
                })?;
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                let state = app_handle.state::<BackendProcess>();
                let child_opt = state.0.lock().unwrap().take();
                if let Some(mut child) = child_opt {
                    #[cfg(unix)]
                    unsafe {
                        libc::kill(-(child.id() as i32), libc::SIGTERM);
                    }
                    let _ = child.kill();
                    let _ = child.wait();
                }
                stop_stale_backend();
            }
        });
}

fn urlencoding_encode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}
