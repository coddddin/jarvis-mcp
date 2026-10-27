"""Smoke-check a built desktop app on this PC (Windows only; development command, not part of pytest).

  .venv\\Scripts\\python.exe scripts\\desktop_smoke.py desktop\\target\\release\\jarvis-desktop.exe [out-dir]

Launches the app on a spare port, then checks: server child started, window shown, hotkey hide/show, close goes
to tray, second launch exits and shows the first window, and killing the app ends the server (job object).
With a debug build it also shows the mini ring, checks that it follows the page state (idle, then thinking after a
typed question) and that a click on it opens the hidden main window.
Writes result.json and screenshots to out-dir. The server uses the workspace of the repository the exe is in.
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import psutil
from PIL import ImageGrab

TITLE, MINI, PORT, WM_CLOSE = 'J.A.R.V.I.S.', 'J.A.R.V.I.S. mini', '8797', 0x0010
user32 = ctypes.windll.user32
ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


APP_PID = None


def window(title=TITLE):
    """The app's own top-level window with this title (other programs may use the same title)."""
    found = []

    def each(hwnd, _):
        pid, text = wintypes.DWORD(), ctypes.create_unicode_buffer(64)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        user32.GetWindowTextW(hwnd, text, 64)
        if pid.value == APP_PID and text.value == title:
            found.append(hwnd)
        return True
    user32.EnumWindows(ENUM(each), 0)
    visible = [hwnd for hwnd in found if user32.IsWindowVisible(hwnd)]
    hwnd = (visible or found or [None])[0]
    return hwnd, bool(visible)


def hotkey():
    for vk, up in ((0x11, 0), (0x12, 0), (ord('J'), 0), (ord('J'), 2), (0x12, 2), (0x11, 2)):
        user32.keybd_event(vk, 0, up, 0)
        time.sleep(.02)


def grab(title, path):
    rect = wintypes.RECT()
    user32.GetWindowRect(window(title)[0], ctypes.byref(rect))
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    image.save(path)
    return image, rect


def warmth(image):
    """Red minus blue over the bright pixels: the thinking look is orange, idle is blue."""
    bright = [px for px in image.convert('RGB').get_flattened_data() if max(px) > 120]
    return round(sum(r - b for r, _, b in bright) / max(1, len(bright)), 1)


def keys(*codes):
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
        user32.keybd_event(code, 0, 2, 0)
        time.sleep(.03)


def click(x, y):
    user32.SetCursorPos(x, y)
    user32.mouse_event(2, 0, 0, 0, 0)
    time.sleep(.05)
    user32.mouse_event(4, 0, 0, 0, 0)


def servers(app):
    found = []
    for proc in app.children(recursive=True):
        try:
            if 'jarvis.cli.hud' in proc.cmdline():
                found.append(proc)
        except psutil.Error:
            pass
    return found


def private_mb(app):
    total = 0
    for proc in [app] + app.children(recursive=True):
        try:
            total += proc.memory_info().private
        except psutil.Error:
            pass
    return round(total / 2**20, 1)


def wait(check, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(.1)
    return False


def main():
    exe = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2] if len(sys.argv) > 2 else '.').resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, JARVIS_DESKTOP_PORT=PORT, JARVIS_PYTHON=os.environ.get('JARVIS_PYTHON', sys.executable),
               JARVIS_DESKTOP_SHOW_MINI='1')  # ignored by release builds
    result = {}
    started = time.time()
    global APP_PID
    app = psutil.Process(subprocess.Popen([str(exe)], env=env).pid)
    APP_PID = app.pid
    result['window_s'] = round(time.time() - started, 2) if wait(lambda: window()[1], 30) else None
    listening = lambda: any(c.laddr and c.laddr[1] == int(PORT) and c.status == 'LISTEN'
                            for c in psutil.net_connections('tcp'))
    result['server_listening_s'] = round(time.time() - started, 2) if wait(listening, 120) else None
    result['server_children'] = len(servers(app))
    time.sleep(5)
    result['private_mb_idle'] = private_mb(app)
    hotkey()
    hidden = wait(lambda: not window()[1], 2)
    hotkey()  # shows and focuses the window, so the screenshot is not covered by other windows
    result['hotkey_toggle'] = hidden and wait(lambda: window()[1], 2)
    time.sleep(.8)
    _, main_rect = grab(TITLE, out / 'window.png')
    if window(MINI)[1]:
        idle, _ = grab(MINI, out / 'mini-idle.png')
        # The composer sits at about 89% of the window height; click it, then type "hi" and Enter.
        click(main_rect.left + (main_rect.right - main_rect.left) * 2 // 5, main_rect.top + (main_rect.bottom - main_rect.top) * 89 // 100)
        time.sleep(.3)
        keys(0x48, 0x49, 0x0D)
        time.sleep(.6)
        thinking, rect = grab(MINI, out / 'mini-thinking.png')
        result['mini_follows_state'] = {'idle_warmth': warmth(idle), 'thinking_warmth': warmth(thinking)}
        hotkey()
        wait(lambda: not window()[1], 2)
        click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        result['mini_click_opens_main'] = wait(lambda: window()[1], 3)
    user32.PostMessageW(window()[0], WM_CLOSE, 0, 0)
    result['close_to_tray'] = wait(lambda: not window()[1], 2) and app.is_running()
    second_start = time.time()
    second = subprocess.run([str(exe)], env=env, timeout=60)
    result['second_instance'] = {'exit_code': second.returncode, 'exit_s': round(time.time() - second_start, 2),
                                 'first_shown': wait(lambda: window()[1], 3)}
    children = servers(app)
    app.kill()  # like Task Manager "End task": no cleanup code runs in the app
    _, alive = psutil.wait_procs(children, timeout=5)
    result['server_ended_with_app'] = bool(children) and not alive
    for proc in alive:
        proc.kill()
    (out / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
