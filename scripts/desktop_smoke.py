"""Smoke-check a built desktop app on this PC (Windows only; development command, not part of pytest).

  .venv\\Scripts\\python.exe scripts\\desktop_smoke.py desktop\\target\\release\\jarvis-desktop.exe [out-dir]

Launches the app on a spare port, then checks: server child started, window shown, hotkey hide/show, close goes
to tray, second launch exits and shows the first window, and killing the app ends the server (job object).
With a debug build it also shows the mini ring and checks that a click on it opens the hidden main window, and
records the presence states the page reports over IPC (debug builds print them), before and after a restart.
Server watch: it kills the server once and checks the app restarts it and reconnects. Then, in a second launch, it
keeps killing each new server until the app gives up (5 restarts in a row) and checks no server is left. The whole
run takes about a minute and a half.
Writes result.json and screenshots to out-dir. The server uses the workspace of the repository the exe is in.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
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


def click(x, y):
    user32.SetCursorPos(x, y)
    user32.mouse_event(2, 0, 0, 0, 0)
    time.sleep(.05)
    user32.mouse_event(4, 0, 0, 0, 0)


def front():
    """Hide and show with the hotkey: the window comes to the front for a screenshot."""
    hotkey()
    wait(lambda: not window()[1], 2)
    hotkey()
    wait(lambda: window()[1], 2)
    time.sleep(.8)


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
    launched = subprocess.Popen([str(exe)], env=env, stdout=subprocess.PIPE, text=True, encoding='utf-8')
    app = psutil.Process(launched.pid)
    APP_PID = app.pid
    reported = []
    threading.Thread(target=lambda: [reported.append(line.split()[1]) for line in launched.stdout
                                     if line.startswith('PRESENCE ')], daemon=True).start()
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
    grab(TITLE, out / 'window.png')
    if window(MINI)[1]:
        _, rect = grab(MINI, out / 'mini.png')
        hotkey()
        wait(lambda: not window()[1], 2)
        click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        result['mini_click_opens_main'] = wait(lambda: window()[1], 3)
    user32.PostMessageW(window()[0], WM_CLOSE, 0, 0)
    result['close_to_tray'] = wait(lambda: not window()[1], 2) and app.is_running()
    # Watch: one crash is restarted and the page reconnects.
    old = {proc.pid for proc in servers(app)}
    for proc in servers(app):
        proc.kill()
    fresh = lambda: listening() and servers(app) and not old & {proc.pid for proc in servers(app)}
    before_crash = len(reported)
    crash = time.time()
    result['restart_after_crash_s'] = round(time.time() - crash, 2) if wait(fresh, 30) else None
    reconnected = wait(lambda: 'idle' in reported[before_crash:], 20)
    result['presence_reported'] = {'first_start': reported[:before_crash], 'after_restart': reported[before_crash:],
                                   'reconnected_after_restart': reconnected}
    time.sleep(2)
    front()
    grab(TITLE, out / 'window-after-restart.png')
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
    # Watch, second launch: crashes in a row end in giving up, with no server left behind.
    app = psutil.Process(subprocess.Popen([str(exe)], env=env).pid)
    APP_PID = app.pid
    restarts = 0
    for _ in range(8):
        if not wait(lambda: listening() and servers(app), 40):
            break
        restarts += 1
        for proc in servers(app):
            proc.kill()
        wait(lambda: not listening(), 5)
    time.sleep(3)
    result['gave_up_after_restarts'] = restarts - 1 if not servers(app) and not listening() else None
    front()
    grab(TITLE, out / 'window-stopped.png')
    app.kill()
    (out / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
