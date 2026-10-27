"""Drive the Tauri spike build and record the same checks as pywebview_shell.py --selftest (spike only).

  .venv\\Scripts\\python.exe spikes\\desktop_shell\\tauri_driver.py <exe> <out.json>
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import winreg
from ctypes import wintypes
from pathlib import Path

import psutil
from PIL import ImageGrab

HERE = Path(__file__).resolve().parent
user32 = ctypes.windll.user32
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
TITLE, MINI_TITLE, WM_CLOSE = 'J.A.R.V.I.S. tauri spike', 'J.A.R.V.I.S. mini tauri', 0x0010
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def visible(title):
    hwnd = user32.FindWindowW(None, title)
    return bool(hwnd and user32.IsWindowVisible(hwnd)), hwnd


def press(letter):
    for vk, up in ((0x11, 0), (0x12, 0), (ord(letter), 0), (ord(letter), 2), (0x12, 2), (0x11, 2)):
        user32.keybd_event(vk, 0, up, 0)
        time.sleep(.02)


def run_values():
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        for i in range(winreg.QueryInfoKey(key)[1]):
            name, data, _ = winreg.EnumValue(key, i)
            values[name] = data
    return values


def memory(root):
    rows = []
    for proc in [root] + root.children(recursive=True):
        try:
            rows.append((proc.name(), proc.memory_info().rss, proc.memory_info().private))
        except psutil.Error:
            pass
    by_name = {name: round(sum(r[2] for r in rows if r[0] == name) / 2**20, 1) for name in {r[0] for r in rows}}
    return {'processes': len(rows), 'rss_mb': round(sum(r[1] for r in rows) / 2**20, 1),
            'private_mb': round(sum(r[2] for r in rows) / 2**20, 1), 'by_name': by_name}


def lines(path):
    return Path(path).read_text(encoding='utf-8').splitlines() if Path(path).exists() else []


def main():
    exe, out = sys.argv[1], Path(sys.argv[2]).resolve()
    work = Path(tempfile.mkdtemp(prefix='tauri-spike-'))
    events, log = work / 'events.jsonl', work / 'shell.log'
    env = dict(os.environ, JARVIS_PYTHON=sys.executable, JARVIS_SERVE=str(HERE / 'serve_for_shell.py'),
               JARVIS_SPIKE_PORT='8797', JARVIS_SPIKE_EVENTS=str(events), JARVIS_SPIKE_OUT=str(log),
               JARVIS_SPIKE_AUTOSTART_TEST='1')
    before_run = run_values()
    launched = time.time()
    shell = subprocess.Popen([exe], env=env)
    root = psutil.Process(shell.pid)
    result = {}
    for _ in range(600):
        names = [json.loads(line)['event'] for line in lines(events)]
        if 'listing' in names:
            break
        time.sleep(.05)
    stamps = {json.loads(line)['event']: json.loads(line)['time'] for line in lines(events)}
    result['server_ready_s'] = round(stamps.get('ready', 0) - launched, 2)
    result['auto_connected'] = 'connect' in stamps and 'listing' in stamps
    result['connected_s'] = round(stamps.get('listing', 0) - launched, 2)
    time.sleep(1)
    added = {k: v for k, v in run_values().items() if k not in before_run}
    time.sleep(2)
    result['memory_idle'] = memory(root)
    result['hotkey_registered'] = 'HOTKEYS_REGISTERED' in lines(log)
    press('J'); time.sleep(.6)
    hidden_after = not visible(TITLE)[0]
    press('J'); time.sleep(.6)
    result['hotkey_toggle'] = hidden_after and visible(TITLE)[0]
    user32.PostMessageW(visible(TITLE)[1], WM_CLOSE, 0, 0); time.sleep(.6)
    result['close_to_tray'] = (not visible(TITLE)[0]) and shell.poll() is None and 'CLOSE_TO_TRAY' in lines(log)
    result['tray_built'] = 'TRAY_BUILT' in lines(log)
    t = time.time()
    second = subprocess.run([exe], env=env, timeout=60)
    time.sleep(.8)
    result['single_instance'] = {'second_exit_code': second.returncode, 'second_exit_s': round(time.time() - t, 2),
                                 'first_shown_again': visible(TITLE)[0], 'first_notified': 'SECOND_INSTANCE' in lines(log)}

    def mini_check(name):
        press('M'); time.sleep(1.2)
        shown, hwnd = visible(MINI_TITLE)
        rect = wintypes.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(rect))
        box = (rect.left, rect.top, rect.right, rect.bottom)
        with_mini = ImageGrab.grab(bbox=box, all_screens=True)
        with_mini.save(out.parent / f'tauri-{name}.png')
        press('M'); time.sleep(.8)
        behind = ImageGrab.grab(bbox=box, all_screens=True)
        behind.save(out.parent / f'tauri-{name}-behind.png')
        corners = [(4, 4), (with_mini.width - 5, 4), (4, with_mini.height - 5)]
        diff = max(sum(abs(a - b) for a, b in zip(with_mini.getpixel(p)[:3], behind.getpixel(p)[:3])) for p in corners)
        return {'shown': shown, 'size': [rect.right - rect.left, rect.bottom - rect.top],
                'corner_max_diff_vs_behind': diff, 'transparent': diff < 30}
    result['mini'] = mini_check('mini')
    time.sleep(2)
    result['autostart'] = {'log': [line for line in lines(log) if line.startswith('AUTOSTART')],
                           'run_value_added': added,
                           'removed_after': not any(k in run_values() for k in added)}
    result['memory_end'] = memory(root)
    children = root.children(recursive=True)
    for proc in [root] + children:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs([root] + children, timeout=10)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
