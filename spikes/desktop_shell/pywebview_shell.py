"""Desktop shell spike with pywebview (decision evidence only, not for main).

Runs the existing assistant view in a native window, with the HUD server in the same Python process:
auto connect (one-time code), global hotkey Ctrl+Alt+J, tray icon (pystray), close goes to tray, single instance
(named mutex + event), a frameless always-on-top transparent mini window, and autostart through the HKCU Run key.

Run with the project venv; extra packages (pywebview, pystray) come from SPIKE_SITE so the shared venv is untouched:
  set SPIKE_SITE=<scratch venv>\\Lib\\site-packages
  .venv\\Scripts\\python.exe spikes\\desktop_shell\\pywebview_shell.py --selftest out.json
"""
import argparse
import ctypes
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from ctypes import wintypes
from pathlib import Path

STARTED = time.perf_counter()
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for extra in os.environ.get('SPIKE_SITE', '').split(os.pathsep):
    if extra:
        sys.path.append(extra)

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

TITLE, MINI_TITLE = 'J.A.R.V.I.S. spike', 'J.A.R.V.I.S. mini spike'
MUTEX, SHOW_EVENT = 'Local\\JarvisDesktopSpike', 'Local\\JarvisDesktopSpikeShow'
ERROR_ALREADY_EXISTS, EVENT_MODIFY_STATE, WAIT_OBJECT_0 = 183, 0x0002, 0
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY, WM_CLOSE = 0x1, 0x2, 0x4000, 0x0312, 0x0010
RUN_KEY, RUN_VALUE = r'Software\Microsoft\Windows\CurrentVersion\Run', 'JarvisDesktopSpikeTest'
MINI_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:transparent;overflow:hidden}
.ring{position:absolute;inset:18px;border-radius:50%;border:6px solid #6fe3ff;box-shadow:0 0 18px #6fe3ff,inset 0 0 18px #3b8cff}
</style></head><body><div class="ring"></div></body></html>"""


def single_instance():
    """Return the mutex for the first instance; a second instance wakes the first and returns None."""
    handle = kernel32.CreateMutexW(None, False, MUTEX)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT)
        if event:
            kernel32.SetEvent(event)
        return None
    return handle


def start_server(port):
    """The HUD server stays Python: same app as `hud --open`, on a temp workspace for the spike."""
    import uvicorn
    from jarvis.agents.models import TaskRequest
    from jarvis.agents.store import TaskStore
    from jarvis.app.chat import ChatService
    from jarvis.app.events import create_event_app
    from jarvis.core.guardrails import Guardrails
    workspace = Path(tempfile.mkdtemp(prefix='jarvis-spike-'))
    store = TaskStore(Guardrails(workspace))
    task = store.create(TaskRequest(summary='Spike task'))
    url, code = f'http://127.0.0.1:{port}', secrets.token_urlsafe(32)
    app = create_event_app(store, token=secrets.token_urlsafe(32), task_ids=[task.task_id], origins=[url],
                           chat=ChatService(store.guard), connect_code=code)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(.02)
    return server, f'{url}/assistant#connect={code}'


def tray_image():
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), outline=(111, 227, 255, 255), width=7)
    return image


class Shell:
    def __init__(self, url):
        import webview
        self.webview, self.quitting, self.visible = webview, False, True
        self.window = webview.create_window(TITLE, url, width=900, height=760, background_color='#040a14')
        self.mini = webview.create_window(MINI_TITLE, html=MINI_HTML, width=180, height=180, min_size=(100, 100),
                                          frameless=True, on_top=True, transparent=True, easy_drag=True, hidden=True)
        self.window.events.closing += self.on_closing
        self.mini_visible = False
        self.icon = None

    # --- window ---
    def on_closing(self):
        if self.quitting:
            return True
        self.hide()
        return False  # pywebview cancels the close when the handler returns False

    def show(self, *_):
        self.window.show(); self.window.restore(); self.visible = True

    def hide(self, *_):
        self.window.hide(); self.visible = False

    def toggle(self, *_):
        self.hide() if self.visible else self.show()

    def toggle_mini(self, *_):
        (self.mini.hide if self.mini_visible else self.mini.show)()
        self.mini_visible = not self.mini_visible

    def color_key_mini(self):
        """WinForms workaround: paint the form one key color and let Windows cut that color out."""
        from System import Func, Type
        from System.Drawing import Color
        form = self.mini.native

        def apply():
            key = Color.FromArgb(255, 1, 2, 3)
            form.BackColor = key
            form.TransparencyKey = key
        form.Invoke(Func[Type](lambda: apply()))

    def quit(self, *_):
        self.quitting = True
        if self.icon:
            self.icon.stop()
        self.mini.destroy(); self.window.destroy()

    # --- OS hooks ---
    def hotkey_loop(self, ready):
        ok = user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord('J'))
        ready.append(bool(ok))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.toggle()

    def wake_loop(self, event):
        while not self.quitting:
            if kernel32.WaitForSingleObject(event, 500) == WAIT_OBJECT_0:
                self.show()

    def start_tray(self):
        import pystray
        menu = pystray.Menu(pystray.MenuItem('열기', self.show, default=True),
                            pystray.MenuItem('미니 고리', self.toggle_mini), pystray.MenuItem('종료', self.quit))
        self.icon = pystray.Icon('jarvis-spike', tray_image(), 'J.A.R.V.I.S.', menu)
        self.icon.run_detached()


def autostart_roundtrip(command):
    """Write, read back and remove an HKCU Run value; the real app would keep it."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
        stored = winreg.QueryValueEx(key, RUN_VALUE)[0]
        winreg.DeleteValue(key, RUN_VALUE)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        try:
            winreg.QueryValueEx(key, RUN_VALUE); left = True
        except FileNotFoundError:
            left = False
    return {'written': stored == command, 'removed': not left}


def press_hotkey():
    for vk, up in ((0x11, 0), (0x12, 0), (ord('J'), 0), (ord('J'), 2), (0x12, 2), (0x11, 2)):
        user32.keybd_event(vk, 0, up, 0)
        time.sleep(.02)


def visible(title):
    hwnd = user32.FindWindowW(None, title)
    return bool(hwnd and user32.IsWindowVisible(hwnd)), hwnd


def memory():
    import psutil
    me = psutil.Process()
    procs = [me] + me.children(recursive=True)
    rows = []
    for proc in procs:
        try:
            info = proc.memory_info()
            rows.append((proc.name(), info.rss, getattr(info, 'private', 0)))
        except psutil.Error:
            pass
    return {'processes': len(rows), 'rss_mb': round(sum(r[1] for r in rows) / 2**20, 1),
            'private_mb': round(sum(r[2] for r in rows) / 2**20, 1),
            'by_name': {name: round(sum(r[2] for r in rows if r[0] == name) / 2**20, 1) for name in {r[0] for r in rows}}}


def selftest(shell, out, hotkey_ready, scratch):
    from PIL import ImageGrab
    result = {}
    shell.window.events.loaded.wait(30)
    result['page_loaded_s'] = round(time.perf_counter() - STARTED, 2)
    for _ in range(300):
        dot = shell.window.evaluate_js("document.getElementById('presenceDot').dataset.state")
        if dot == 'online':
            break
        time.sleep(.05)
    result['auto_connected'] = dot == 'online'
    result['connected_s'] = round(time.perf_counter() - STARTED, 2)
    result['presence_text'] = shell.window.evaluate_js("document.getElementById('presenceText').textContent")
    result['webgl'] = shell.window.evaluate_js("document.getElementById('orb').dataset.fallback !== 'true'")
    time.sleep(2)
    result['memory_idle'] = memory()
    result['hotkey_registered'] = bool(hotkey_ready and hotkey_ready[0])
    press_hotkey(); time.sleep(.6)
    hidden_after = not visible(TITLE)[0]
    press_hotkey(); time.sleep(.6)
    result['hotkey_toggle'] = hidden_after and visible(TITLE)[0]
    user32.PostMessageW(visible(TITLE)[1], WM_CLOSE, 0, 0); time.sleep(.6)
    result['close_to_tray'] = (not visible(TITLE)[0]) and not shell.quitting
    result['tray_visible'] = bool(shell.icon and shell.icon.visible)
    t = time.perf_counter()
    second = subprocess.run([sys.executable, __file__, '--second'], env=os.environ, timeout=60)
    time.sleep(.8)
    result['single_instance'] = {'second_exit_code': second.returncode, 'second_exit_s': round(time.perf_counter() - t, 2),
                                 'first_shown_again': visible(TITLE)[0]}
    # Mini ring over the main window: if the corners show the page behind, the window is transparent.
    def mini_check(name):
        shell.toggle_mini(); time.sleep(1.2)
        shown, hwnd = visible(MINI_TITLE)
        rect = wintypes.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(rect))
        box = (rect.left, rect.top, rect.right, rect.bottom)
        with_mini = ImageGrab.grab(bbox=box, all_screens=True)
        with_mini.save(scratch / f'pywebview-{name}.png')
        shell.toggle_mini(); time.sleep(.8)
        behind = ImageGrab.grab(bbox=box, all_screens=True)
        behind.save(scratch / f'pywebview-{name}-behind.png')
        corners = [(4, 4), (with_mini.width - 5, 4), (4, with_mini.height - 5)]
        diff = max(sum(abs(a - b) for a, b in zip(with_mini.getpixel(p)[:3], behind.getpixel(p)[:3])) for p in corners)
        return {'shown': shown, 'size': [rect.right - rect.left, rect.bottom - rect.top],
                'corner_max_diff_vs_behind': diff, 'transparent': diff < 30}
    result['mini'] = mini_check('mini')
    shell.color_key_mini()
    result['mini_color_key'] = mini_check('mini-colorkey')
    result['autostart'] = autostart_roundtrip(f'"{sys.executable}" "{__file__}"')
    result['memory_end'] = memory()
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    shell.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--selftest', metavar='OUT')
    parser.add_argument('--second', action='store_true', help='internal: second instance probe')
    parser.add_argument('--port', type=int, default=8796)
    args = parser.parse_args()
    mutex = single_instance()
    if mutex is None:
        print('already running: asked the first instance to show its window', flush=True)
        sys.exit(3)
    show_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT)
    import webview
    _, url = start_server(args.port)
    shell = Shell(url)
    hotkey_ready = []
    threading.Thread(target=shell.hotkey_loop, args=(hotkey_ready,), daemon=True).start()
    threading.Thread(target=shell.wake_loop, args=(show_event,), daemon=True).start()
    shell.start_tray()
    scratch = Path(args.selftest).resolve().parent if args.selftest else None
    func = (lambda: selftest(shell, args.selftest, hotkey_ready, scratch)) if args.selftest else None
    webview.start(func, private_mode=True)


if __name__ == '__main__':
    main()
