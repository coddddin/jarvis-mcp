// J.A.R.V.I.S. desktop app (Tauri v2, Windows).
// Starts the HUD server from this repository as a child in a kill-on-close job object, opens /assistant with the
// server's one-time connect code, and adds a tray, close-to-tray, a single instance, a global show/hide hotkey and
// an opt-in start at login, plus a transparent mini ring that follows the assistant's state.
// IPC is narrow: the server page may only call `presence` (capabilities/server-page.json), the bundled mini ring only
// `expand` and `drag` (capabilities/mini.json). Navigation in the main window is held to the local server.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod server;
mod win;

use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, PhysicalPosition, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

/// Started at login: stay in the tray until the user opens the window.
const HIDDEN_ARG: &str = "--hidden";
/// Ctrl+Alt+J: not a Windows or common app shortcut, and the spike verified it registers.
const HOTKEY: (Modifiers, Code, &str) = (Modifiers::CONTROL.union(Modifiers::ALT), Code::KeyJ, "Ctrl+Alt+J");

/// The presence states of frontend/assistant/presence.js.
const STATES: [&str; 7] = ["offline", "connecting", "idle", "listening", "thinking", "speaking", "error"];
const MINI_SIZE: f64 = 128.0;

struct Running(Mutex<Option<server::Server>>);
/// The last state the main window reported, so a mini ring shown later starts right.
struct Presence(Mutex<&'static str>);

fn known_state(state: &str) -> Option<&'static str> {
    STATES.iter().copied().find(|known| *known == state)
}

fn send_to_mini(app: &AppHandle, state: &'static str) {
    if let Some(mini) = app.get_webview_window("mini") {
        // `state` is one of STATES, so the script is fixed text.
        let _ = mini.eval(format!("window.jarvisMini && window.jarvisMini.set('{state}')"));
    }
}

#[tauri::command]
fn presence(app: AppHandle, window: WebviewWindow, state: String) -> Result<(), String> {
    let state = known_state(&state).filter(|_| window.label() == "main").ok_or("rejected")?;
    *app.state::<Presence>().0.lock().unwrap() = state;
    send_to_mini(&app, state);
    Ok(())
}

#[tauri::command]
fn expand(app: AppHandle, window: WebviewWindow) {
    if window.label() == "mini" {
        show_main(&app);
    }
}

#[tauri::command]
fn drag(window: WebviewWindow) {
    if window.label() == "mini" {
        let _ = window.start_dragging();
    }
}

fn set_mini(app: &AppHandle, visible: bool) {
    if let Some(mini) = app.get_webview_window("mini") {
        if visible {
            send_to_mini(app, *app.state::<Presence>().0.lock().unwrap());
            let _ = mini.show();
        } else {
            let _ = mini.hide();
        }
    }
}

/// Bottom right of the primary work area, clear of the taskbar.
fn place_mini(app: &tauri::App, mini: &WebviewWindow) {
    if let Ok(Some(monitor)) = app.primary_monitor() {
        let (area, scale) = (monitor.work_area(), monitor.scale_factor());
        let (side, margin) = ((MINI_SIZE * scale) as i32, (24.0 * scale) as i32);
        let x = area.position.x + area.size.width as i32 - side - margin;
        let y = area.position.y + area.size.height as i32 - side - margin;
        let _ = mini.set_position(PhysicalPosition::new(x, y));
    }
}

/// The app window may show only the local server (and the bundled loading page).
fn stays_in_app(url: &Url, port: u16) -> bool {
    match (url.scheme(), url.host_str(), url.port()) {
        ("http", Some("127.0.0.1"), Some(p)) => p == port,
        ("http" | "https", Some("tauri.localhost"), None) => true,
        ("tauri", Some("localhost"), _) => true,
        _ => false,
    }
}

fn port() -> u16 {
    std::env::var("JARVIS_DESKTOP_PORT").ok().and_then(|p| p.parse().ok()).filter(|p| *p >= 1024).unwrap_or(server::DEFAULT_PORT)
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn toggle_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let shown = window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false);
        if shown {
            let _ = window.hide();
        } else {
            show_main(app);
        }
    }
}

fn fail(app: &AppHandle, text: &str) {
    win::error_box(text);
    app.exit(1);
}

fn locate() -> Result<(PathBuf, PathBuf), String> {
    let exe = std::env::current_exe().map_err(|e| format!("실행 파일 위치를 알 수 없습니다: {e}"))?;
    let root = server::find_root(&exe, std::env::var_os("JARVIS_ROOT").map(PathBuf::from)).ok_or(
        "J.A.R.V.I.S. 저장소를 찾지 못했습니다. 앱을 저장소 안에서 실행하거나 JARVIS_ROOT를 지정하세요.",
    )?;
    let python = server::find_python(&root, std::env::var_os("JARVIS_PYTHON").map(PathBuf::from))
        .ok_or("저장소의 .venv Python을 찾지 못했습니다. JARVIS_PYTHON으로 지정할 수 있습니다.")?;
    Ok((root, python))
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let open = MenuItem::with_id(app, "open", "열기", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "숨기기", true, None::<&str>)?;
    let mini = CheckMenuItem::with_id(app, "mini", "미니 고리", true, false, None::<&str>)?;
    let login = CheckMenuItem::with_id(app, "login", "로그인 시 자동 실행", true, autostart_on, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "종료", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &hide, &mini, &separator, &login, &separator, &quit])?;
    let toggle = login.clone();
    let mini_toggle = mini.clone();
    TrayIconBuilder::with_id("tray")
        .icon(app.default_window_icon().expect("bundled icon").clone())
        .tooltip(format!("J.A.R.V.I.S. ({} 보이기/숨기기)", HOTKEY.2))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "hide" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }
            "mini" => {
                let shown = app.get_webview_window("mini").and_then(|m| m.is_visible().ok()).unwrap_or(false);
                set_mini(app, !shown);
                let _ = mini_toggle.set_checked(!shown);
            }
            "login" => {
                let manager = app.autolaunch();
                let result = if manager.is_enabled().unwrap_or(false) { manager.disable() } else { manager.enable() };
                if let Err(e) = result {
                    win::error_box(&format!("자동 실행 설정을 바꾸지 못했습니다: {e}"));
                }
                // Show what Windows actually has, whatever the click did to the check mark.
                let _ = toggle.set_checked(manager.is_enabled().unwrap_or(false));
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                toggle_main(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

/// One line per start in %LOCALAPPDATA%/dev.jarvis.desktop/startup.log, so start time right after a cold boot can be
/// read later (uptime_s small = just booted). It never contains the server URL or code. Kept under 64 KB.
fn record_start(app: &AppHandle, ready: Duration, hidden: bool) {
    let Ok(dir) = app.path().app_local_data_dir() else { return };
    let path = dir.join("startup.log");
    let _ = std::fs::create_dir_all(&dir);
    let full = std::fs::metadata(&path).map(|m| m.len() > 64 * 1024).unwrap_or(false);
    let uptime_s = unsafe { windows_sys::Win32::System::SystemInformation::GetTickCount64() } / 1000;
    let unix = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let line = format!("unix={unix} uptime_s={uptime_s} ready_ms={} hidden={hidden}\n", ready.as_millis());
    let file = std::fs::OpenOptions::new().create(true).write(true).append(!full).truncate(full).open(path);
    if let Ok(mut file) = file {
        let _ = file.write_all(line.as_bytes());
    }
}

fn main() {
    let started = Instant::now();
    let hidden = std::env::args().any(|arg| arg == HIDDEN_ARG);
    let port = port();
    let hotkey = Shortcut::new(Some(HOTKEY.0), HOTKEY.1);
    let hotkey_check = hotkey;

    tauri::Builder::default()
        // Registered first so a second launch exits before it starts anything.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    if shortcut == &hotkey_check && event.state() == ShortcutState::Pressed {
                        toggle_main(app);
                    }
                })
                .build(),
        )
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec![HIDDEN_ARG])))
        .setup(move |app| {
            app.manage(Running(Mutex::new(None)));
            app.manage(Presence(Mutex::new("offline")));
            let (root, python) = match locate() {
                Ok(found) => found,
                Err(text) => {
                    fail(app.handle(), &text);
                    return Ok(());
                }
            };
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("loading.html".into()))
                .title("J.A.R.V.I.S.")
                .inner_size(960.0, 780.0)
                .min_inner_size(420.0, 520.0)
                .visible(!hidden)
                .on_navigation(move |url| {
                    if stays_in_app(url, port) {
                        return true;
                    }
                    win::open_in_browser(url);
                    false
                })
                .on_new_window(|url, _features| {
                    win::open_in_browser(&url);
                    NewWindowResponse::Deny
                })
                .build()?;
            let mini = WebviewWindowBuilder::new(app, "mini", WebviewUrl::App("mini.html".into()))
                .title("J.A.R.V.I.S. mini")
                .inner_size(MINI_SIZE, MINI_SIZE)
                .decorations(false)
                .transparent(true)
                .shadow(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .resizable(false)
                .visible(false)
                .on_navigation(|url| url.host_str() == Some("tauri.localhost") || url.scheme() == "tauri")
                .build()?;
            place_mini(app, &mini);
            build_tray(app)?;
            // Debug builds only: lets scripts/desktop_smoke.py check the mini ring without clicking the tray.
            if cfg!(debug_assertions) && std::env::var_os("JARVIS_DESKTOP_SHOW_MINI").is_some() {
                set_mini(app.handle(), true);
            }
            if let Err(e) = app.global_shortcut().register(hotkey) {
                if let Some(tray) = app.tray_by_id("tray") {
                    let _ = tray.set_tooltip(Some(format!("J.A.R.V.I.S. (단축키 {} 등록 실패: {e})", HOTKEY.2)));
                }
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || match server::start(&root, &python, port) {
                Ok(running) => {
                    record_start(&handle, started.elapsed(), hidden);
                    let url = Url::parse(&running.url).expect("validated READY url");
                    *handle.state::<Running>().0.lock().unwrap() = Some(running);
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.navigate(url);
                    }
                }
                Err(text) => fail(&handle, &text),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![presence, expand, drag])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("build the desktop app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut running) = app.state::<Running>().0.lock().unwrap().take() {
                    running.stop();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_presence_states_reach_the_mini_ring() {
        for state in STATES {
            assert_eq!(known_state(state), Some(state));
        }
        for bad in ["", "Idle", "idle'); alert(1); ('", "unknown"] {
            assert_eq!(known_state(bad), None, "{bad}");
        }
    }

    #[test]
    fn presence_states_match_the_frontend_state_machine() {
        let source = include_str!("../../frontend/assistant/presence.js");
        let mut states: Vec<&str> = STATES.to_vec();
        states.sort_unstable();
        let line = source.lines().find(|l| l.contains("// States:")).expect("presence.js lists its states");
        let list = line.split("States:").nth(1).unwrap().split('.').next().unwrap();
        let mut listed: Vec<&str> = list.split(',').map(str::trim).collect();
        listed.sort_unstable();
        assert_eq!(states, listed);
    }

    #[test]
    fn the_window_stays_on_the_local_server_and_bundled_pages() {
        for good in ["http://127.0.0.1:8787/assistant", "http://127.0.0.1:8787/", "http://tauri.localhost/loading.html"] {
            assert!(stays_in_app(&Url::parse(good).unwrap(), 8787), "{good}");
        }
        for bad in [
            "http://127.0.0.1:8788/assistant",
            "http://localhost:8787/assistant",
            "https://127.0.0.1:8787/",
            "https://example.com/",
            "http://tauri.localhost:9000/",
            "file:///C:/",
        ] {
            assert!(!stays_in_app(&Url::parse(bad).unwrap(), 8787), "{bad}");
        }
    }
}
