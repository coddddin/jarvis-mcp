// J.A.R.V.I.S. desktop app (Tauri v2, Windows).
// Starts the HUD server from this repository as a child in a kill-on-close job object, opens /assistant with the
// server's one-time connect code, and restarts the server if it stops on its own (watch.rs). Adds a tray,
// close-to-tray, a single instance, a global show/hide hotkey, an opt-in start at login, a voice switch (settings.rs)
// and a transparent mini ring that follows the assistant's state.
// IPC is narrow: the server page may only call `presence` (a capability added at startup for the exact server port),
// the bundled mini ring only `expand` and `drag` (capabilities/mini.json). Navigation in the main window is held to
// the local server.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod server;
mod settings;
mod watch;
mod win;

use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::ipc::CapabilityBuilder;
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

/// The running server, and whether the user is quitting (then nothing restarts it).
struct Supervisor {
    server: Mutex<Option<server::Server>>,
    quitting: AtomicBool,
    /// The tray's "서버 다시 시작" item: enabled only after the app gave up restarting.
    restart_item: Mutex<Option<MenuItem<tauri::Wry>>>,
    /// The normal tray tooltip (it names a hotkey registration failure, if any).
    tooltip: Mutex<String>,
    /// Saved settings, and their folder (None when Windows gave no app data folder: then nothing is saved).
    settings: Mutex<settings::Settings>,
    settings_dir: Option<PathBuf>,
}

impl Supervisor {
    fn quitting(&self) -> bool {
        self.quitting.load(Ordering::SeqCst)
    }

    fn voice(&self) -> bool {
        self.settings.lock().unwrap().voice
    }

    /// Flip the voice setting and save it. Returns the new value, and an error text if it could not be saved.
    fn toggle_voice(&self) -> (bool, Option<String>) {
        let mut settings = self.settings.lock().unwrap();
        settings.voice = !settings.voice;
        let saved = match &self.settings_dir {
            Some(dir) => settings.save(dir).err().map(|e| e.to_string()),
            None => Some("앱 데이터 폴더를 찾지 못했습니다".to_string()),
        };
        (settings.voice, saved)
    }

    fn quit(&self) {
        self.quitting.store(true, Ordering::SeqCst);
        if let Some(mut running) = self.server.lock().unwrap().take() {
            running.stop();
        }
    }
}

/// What the main window and the tray say while the server is not running normally.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Status {
    Running,
    Restarting(usize),
    Stopped,
}

/// The bundled loading page shows the status from its fragment (dist/loading.js).
fn status_page(status: Status) -> Option<Url> {
    let fragment = match status {
        Status::Running => return None,
        Status::Restarting(attempt) => format!("restart-{attempt}"),
        Status::Stopped => "stopped".to_string(),
    };
    Url::parse(&format!("http://tauri.localhost/loading.html#{fragment}")).ok()
}

fn tray_text(base: &str, status: Status) -> String {
    match status {
        Status::Running => base.to_string(),
        Status::Restarting(0) => "J.A.R.V.I.S. (서버 다시 시작 중)".to_string(),
        Status::Restarting(attempt) => format!("J.A.R.V.I.S. (서버 다시 시작 중 {attempt}/{})", watch::DELAYS.len()),
        Status::Stopped => "J.A.R.V.I.S. (서버 멈춤: 트레이 메뉴에서 서버 다시 시작)".to_string(),
    }
}

fn show_status(app: &AppHandle, status: Status) {
    let supervisor = app.state::<Supervisor>();
    if let Some(tray) = app.tray_by_id("tray") {
        let _ = tray.set_tooltip(Some(tray_text(&supervisor.tooltip.lock().unwrap(), status)));
    }
    if let Some(item) = supervisor.restart_item.lock().unwrap().as_ref() {
        let _ = item.set_enabled(status == Status::Stopped);
    }
    if status != Status::Running {
        // The page that reported presence is gone; the mini ring shows offline, or error once the app gave up.
        let state = if status == Status::Stopped { "error" } else { "offline" };
        *app.state::<Presence>().0.lock().unwrap() = state;
        send_to_mini(app, state);
    }
    if let (Some(url), Some(window)) = (status_page(status), app.get_webview_window("main")) {
        let _ = window.navigate(url);
    }
}

/// Sleep in short steps so quitting is not held up by a long backoff.
fn pause(supervisor: &Supervisor, total: Duration) -> bool {
    let until = Instant::now() + total;
    while Instant::now() < until {
        if supervisor.quitting() {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    !supervisor.quitting()
}

/// Start the server, show it, and restart it when it stops on its own. A failed first start ends the app (for
/// example: the port is in use); later failures back off and finally wait for the tray's restart item.
/// When the voice setting changes, the running server is stopped and started again with the new setting at once;
/// that restart is not counted as a failure.
fn supervise(app: AppHandle, root: PathBuf, python: PathBuf, port: u16, started: Instant, hidden: bool,
             retry: mpsc::Receiver<()>) {
    let supervisor = app.state::<Supervisor>();
    let mut restarts = watch::Restarts::default();
    let mut first = true;
    loop {
        if supervisor.quitting() {
            return;
        }
        let voice = supervisor.voice();
        let next = match server::start(&root, &python, port, voice) {
            Ok(running) => {
                if first {
                    record_start(&app, started.elapsed(), hidden);
                }
                first = false;
                let url = Url::parse(&running.url).expect("validated READY url");
                let up = Instant::now();
                *supervisor.server.lock().unwrap() = Some(running);
                show_status(&app, Status::Running);
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.navigate(url);
                }
                let mut switched = false;
                loop {
                    std::thread::sleep(Duration::from_millis(500));
                    let mut guard = supervisor.server.lock().unwrap();
                    match guard.as_mut() {
                        None => return, // taken by quit
                        Some(running) if running.exited().is_some() => break,
                        Some(running) if supervisor.voice() != voice => {
                            running.stop();
                            switched = true;
                            break;
                        }
                        Some(_) => {}
                    }
                }
                // Dropping the server closes its job, which ends anything it left running (the speech worker too).
                drop(supervisor.server.lock().unwrap().take());
                if supervisor.quitting() {
                    return;
                }
                if switched {
                    restarts.reset();
                    show_status(&app, Status::Restarting(0));
                    continue;
                }
                restarts.after_exit(up.elapsed())
            }
            Err(text) if first => {
                fail(&app, &text);
                return;
            }
            Err(_) => restarts.after_start_failure(),
        };
        match next {
            watch::Next::Retry { attempt, delay } => {
                show_status(&app, Status::Restarting(attempt));
                if !pause(&supervisor, delay) {
                    return;
                }
            }
            watch::Next::GiveUp => {
                show_status(&app, Status::Stopped);
                while retry.try_recv().is_ok() {}
                if retry.recv().is_err() || supervisor.quitting() {
                    return;
                }
                restarts.reset();
                show_status(&app, Status::Restarting(0));
            }
        }
    }
}

/// The server page may call `presence` only from the exact local server origin, and never from bundled pages.
fn server_page_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/*")
}
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
    if cfg!(debug_assertions) {
        println!("PRESENCE {state}"); // read by scripts/desktop_smoke.py; release builds have no console
    }
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

fn build_tray(app: &tauri::App, retry: mpsc::Sender<()>) -> tauri::Result<()> {
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let open = MenuItem::with_id(app, "open", "열기", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "숨기기", true, None::<&str>)?;
    let mini = CheckMenuItem::with_id(app, "mini", "미니 고리", true, false, None::<&str>)?;
    let login = CheckMenuItem::with_id(app, "login", "로그인 시 자동 실행", true, autostart_on, None::<&str>)?;
    let voice_on = app.state::<Supervisor>().voice();
    let voice = CheckMenuItem::with_id(app, "voice", "음성", true, voice_on, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", "서버 다시 시작", false, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "종료", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &hide, &mini, &separator, &login, &voice, &restart, &separator, &quit])?;
    *app.state::<Supervisor>().restart_item.lock().unwrap() = Some(restart);
    let toggle = login.clone();
    let mini_toggle = mini.clone();
    let voice_toggle = voice.clone();
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
            "voice" => {
                let (on, failed) = app.state::<Supervisor>().toggle_voice();
                let _ = voice_toggle.set_checked(on);
                if let Some(e) = failed {
                    win::error_box(&format!("음성 설정을 저장하지 못했습니다. 이번 실행에만 적용됩니다: {e}"));
                }
                // supervise() restarts a running server with the new setting; this wakes one the app gave up on.
                let _ = retry.send(());
            }
            "restart" => {
                let _ = retry.send(());
            }
            "quit" => {
                app.state::<Supervisor>().quit();
                app.exit(0);
            }
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
            let settings_dir = app.path().app_local_data_dir().ok();
            let settings = settings_dir.as_deref().map(settings::Settings::load).unwrap_or_default();
            app.manage(Supervisor {
                server: Mutex::new(None),
                quitting: AtomicBool::new(false),
                restart_item: Mutex::new(None),
                tooltip: Mutex::new(format!("J.A.R.V.I.S. ({} 보이기/숨기기)", HOTKEY.2)),
                settings: Mutex::new(settings),
                settings_dir,
            });
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
            app.add_capability(
                CapabilityBuilder::new("server-page")
                    .window("main")
                    .remote(server_page_url(port))
                    .local(false)
                    .permission("allow-presence"),
            )?;
            let (retry_send, retry) = mpsc::channel();
            build_tray(app, retry_send)?;
            // Debug builds only: lets scripts/desktop_smoke.py check the mini ring without clicking the tray.
            if cfg!(debug_assertions) && std::env::var_os("JARVIS_DESKTOP_SHOW_MINI").is_some() {
                set_mini(app.handle(), true);
            }
            if let Err(e) = app.global_shortcut().register(hotkey) {
                let text = format!("J.A.R.V.I.S. (단축키 {} 등록 실패: {e})", HOTKEY.2);
                *app.state::<Supervisor>().tooltip.lock().unwrap() = text.clone();
                if let Some(tray) = app.tray_by_id("tray") {
                    let _ = tray.set_tooltip(Some(text));
                }
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || supervise(handle, root, python, port, started, hidden, retry));
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
                app.state::<Supervisor>().quit();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_presence_capability_names_the_exact_server_origin() {
        assert_eq!(server_page_url(8787), "http://127.0.0.1:8787/*");
        assert_eq!(server_page_url(8797), "http://127.0.0.1:8797/*");
    }

    #[test]
    fn status_pages_stay_in_the_app_and_running_shows_the_server() {
        assert_eq!(status_page(Status::Running), None);
        for status in [Status::Restarting(1), Status::Restarting(5), Status::Stopped] {
            let url = status_page(status).unwrap();
            assert!(stays_in_app(&url, 8787), "{url}");
            assert_eq!(url.path(), "/loading.html");
        }
        assert_eq!(status_page(Status::Restarting(3)).unwrap().fragment(), Some("restart-3"));
        assert_eq!(status_page(Status::Stopped).unwrap().fragment(), Some("stopped"));
    }

    #[test]
    fn the_tray_says_when_the_server_is_restarting_or_stopped() {
        assert_eq!(tray_text("base", Status::Running), "base");
        assert!(tray_text("base", Status::Restarting(2)).contains("2/5"));
        assert!(tray_text("base", Status::Stopped).contains("서버 다시 시작"));
    }

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
