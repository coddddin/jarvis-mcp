// Desktop shell spike with Tauri v2 (decision evidence only, not for main).
// Starts the existing Python HUD server as a child process, opens /assistant with its one-time connect code,
// and adds: global hotkeys (Ctrl+Alt+J main, Ctrl+Alt+M mini), tray menu, close goes to tray, single instance,
// a transparent undecorated always-on-top mini window, and autostart (test mode only).
// Env: JARVIS_PYTHON, JARVIS_SERVE (serve_for_shell.py), JARVIS_SPIKE_PORT, JARVIS_SPIKE_EVENTS, JARVIS_SPIKE_OUT,
// JARVIS_SPIKE_AUTOSTART_TEST.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

struct ServerChild(Mutex<Option<Child>>);

fn log(line: &str) {
    if let Ok(path) = std::env::var("JARVIS_SPIKE_OUT") {
        if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(file, "{line}");
        }
    }
}

fn show(app: &AppHandle, label: &str) {
    if let Some(window) = app.get_webview_window(label) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn toggle(app: &AppHandle, label: &str) {
    if let Some(window) = app.get_webview_window(label) {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            show(app, label);
        }
    }
}

/// Start the Python server and wait for its `READY <url>` line; keep draining stdout afterwards.
fn start_server() -> (Child, String) {
    let python = std::env::var("JARVIS_PYTHON").expect("JARVIS_PYTHON");
    let script = std::env::var("JARVIS_SERVE").expect("JARVIS_SERVE");
    let port = std::env::var("JARVIS_SPIKE_PORT").unwrap_or_else(|_| "8797".into());
    let mut command = Command::new(python);
    command.arg(script).arg("--port").arg(port).stdout(Stdio::piped());
    if let Ok(events) = std::env::var("JARVIS_SPIKE_EVENTS") {
        command.arg("--events").arg(events);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().expect("start the Python server");
    let mut reader = BufReader::new(child.stdout.take().expect("server stdout"));
    let mut line = String::new();
    loop {
        line.clear();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            panic!("the Python server exited before it was ready");
        }
        if let Some(url) = line.trim().strip_prefix("READY ") {
            let url = url.to_string();
            std::thread::spawn(move || {
                let mut sink = String::new();
                while reader.read_line(&mut sink).unwrap_or(0) > 0 {
                    sink.clear();
                }
            });
            return (child, url);
        }
    }
}

fn main() {
    let started = Instant::now();
    let main_key = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyJ);
    let mini_key = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyM);
    let (main_check, mini_check) = (main_key.clone(), mini_key.clone());

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            log("SECOND_INSTANCE");
            show(app, "main");
        }))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        if shortcut == &main_check {
                            toggle(app, "main");
                        } else if shortcut == &mini_check {
                            toggle(app, "mini");
                        }
                    }
                })
                .build(),
        )
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .setup(move |app| {
            let (child, url) = start_server();
            log(&format!("SERVER_READY {:.2}", started.elapsed().as_secs_f64()));
            app.manage(ServerChild(Mutex::new(Some(child))));
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse().expect("server url")))
                .title("J.A.R.V.I.S. tauri spike")
                .inner_size(900.0, 760.0)
                .build()?;
            WebviewWindowBuilder::new(app, "mini", WebviewUrl::App("index.html".into()))
                .title("J.A.R.V.I.S. mini tauri")
                .inner_size(180.0, 180.0)
                .decorations(false)
                .transparent(true)
                .always_on_top(true)
                .skip_taskbar(true)
                .resizable(false)
                .shadow(false)
                .visible(false)
                .build()?;
            app.global_shortcut().register(main_key.clone())?;
            app.global_shortcut().register(mini_key.clone())?;
            log("HOTKEYS_REGISTERED");

            let open = MenuItem::with_id(app, "open", "열기", true, None::<&str>)?;
            let mini = MenuItem::with_id(app, "mini", "미니 고리", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "종료", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &mini, &quit])?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().expect("icon").clone())
                .tooltip("J.A.R.V.I.S.")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show(app, "main"),
                    "mini" => toggle(app, "mini"),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            log("TRAY_BUILT");

            if std::env::var("JARVIS_SPIKE_AUTOSTART_TEST").is_ok() {
                let manager = app.autolaunch();
                let enabled = manager.enable().is_ok();
                log(&format!("AUTOSTART enable_ok={enabled} is_enabled={}", manager.is_enabled().unwrap_or(false)));
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(4));
                    let manager = handle.autolaunch();
                    let disabled = manager.disable().is_ok();
                    log(&format!("AUTOSTART disable_ok={disabled} is_enabled={}", manager.is_enabled().unwrap_or(true)));
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                    log("CLOSE_TO_TRAY");
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("build the shell")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(server) = app.try_state::<ServerChild>() {
                    if let Some(mut child) = server.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
