"""Per-call audit log of MCP tool calls (board 204bc125 part 5). No project feature imports: mcp_client writes it.

One JSONL file per day in JarvisWorkspace/.jarvis-mcp-audit/ (outside .jarvis-mcp, whose sub-folders are server
working folders, and a dot folder, so no approved root or path argument can reach it). Every call writes an
'attempt' line before the worker is asked and an 'outcome' line after, so a call that kills the worker still leaves
its attempt. A call refused before that (blocked argument, path outside the approved folders, confirmation needed)
writes one 'refused' line.

Lines hold names, never content: server, tool, effect, argument names, path arguments as workspace-relative paths,
the outcome, seconds and result size. Argument text, result text, fixed settings and environment are never written.

Bounds: a line is at most LINE_BYTES (path lists are shortened first), a day file at most DAY_BYTES (one 'cap' line,
then nothing more that day), and files older than KEEP_DAYS are deleted. A write that fails is counted; the next
line that is written carries the count as 'missed', and doctor shows it. Whether a failed write stops the call is
decided by the caller (mcp_client: read pins go on, auto/confirm pins and write paths are refused).
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading

FOLDER = '.jarvis-mcp-audit'
LINE_BYTES = 2048
DAY_BYTES = 5 * 1024 * 1024
KEEP_DAYS = 30
READ_LINES = 20000                 # lines read per day file by entries()
_LOCK = threading.Lock()
_MISSED = {'pending': 0, 'total': 0}      # this process: failed writes not yet reported / since start


def missed_writes():
    """Failed audit writes in this process since it started."""
    return _MISSED['total']


class AuditLog:
    def __init__(self, guard, *, now=None):
        self.guard = guard
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._pruned = None

    def folder(self):
        folder = self.guard.validate_path(FOLDER, must_exist=False)
        folder.mkdir(exist_ok=True)
        return self.guard.validate_path(FOLDER)

    def write(self, entry):
        """Append one line. Returns True when it is on disk; False (and counted) when it could not be written."""
        with _LOCK:
            try:
                moment = self.now()
                folder = self.folder()
                self._prune(folder, moment)
                path = folder / f'{moment:%Y-%m-%d}.jsonl'
                size = path.stat().st_size if path.exists() else 0
                record = {'t': moment.isoformat(timespec='milliseconds'), **entry}
                if _MISSED['pending']:
                    record['missed'] = _MISSED['pending']
                data = bounded(record)
                full = size + len(data) > DAY_BYTES - LINE_BYTES        # the last LINE_BYTES hold the 'cap' line
                if full:
                    if size > DAY_BYTES - LINE_BYTES:
                        raise OSError('day file limit reached')
                    data = bounded({'t': record['t'], 'ev': 'cap', 'note': 'day file limit reached; later calls '
                                    'today are not logged', 'missed': _MISSED['pending']})
                    # Padded to a full line so the file is then past the reserve and nothing more is written.
                    data = data.rstrip(b'\n').ljust(LINE_BYTES - 1, b' ') + b'\n'
                with path.open('ab') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if full:                                     # the cap line is written; this entry is not
                    raise OSError('day file limit reached')
                _MISSED['pending'] = 0
                return True
            except Exception:  # noqa: BLE001 - any failure is a missed line, never a crash; the caller decides
                _MISSED['pending'] += 1
                _MISSED['total'] += 1
                return False

    def _prune(self, folder, moment):
        if self._pruned == moment.date():
            return
        self._pruned = moment.date()
        oldest = (moment - timedelta(days=KEEP_DAYS)).date()
        for path in folder.glob('*.jsonl'):
            try:
                if datetime.strptime(path.stem, '%Y-%m-%d').date() < oldest and not path.is_symlink():
                    path.unlink()
            except (ValueError, OSError):
                continue

    def entries(self, *, days=7, server=None):
        """Lines of the last `days` days, oldest first; unreadable lines are skipped."""
        try:
            folder = self.folder()
        except Exception:  # noqa: BLE001 - no readable log folder is an empty log
            return []
        today = self.now().date()
        rows = []
        for back in range(days - 1, -1, -1):
            path = folder / f'{today - timedelta(days=back):%Y-%m-%d}.jsonl'
            if not path.is_file() or path.is_symlink():
                continue
            with path.open('rb') as stream:
                for number, line in enumerate(stream):
                    if number >= READ_LINES:
                        break
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and (server is None or row.get('server') == server):
                        rows.append(row)
        return rows


def bounded(record):
    """The record as one JSON line of at most LINE_BYTES: path lists, then paths, then names are shortened."""
    data = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
    if len(data) <= LINE_BYTES:
        return data
    record = dict(record)
    paths = record.get('paths') or {}
    record['paths'] = {name: (value[:3] if isinstance(value, list) else value) for name, value in paths.items()}
    record['shortened'] = True
    for limit in (200, 80, 0):
        record['paths'] = {name: ([item[:limit] for item in value] if isinstance(value, list) else value[:limit])
                           for name, value in record['paths'].items()} if limit else {}
        record['args'] = [str(name)[:64] for name in record.get('args', ())][:32]
        data = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        if len(data) <= LINE_BYTES:
            return data
    keep = {key: str(record[key])[:64] for key in ('t', 'ev', 'id', 'server', 'tool', 'outcome') if key in record}
    return (json.dumps({**keep, 'shortened': True}, ensure_ascii=False) + '\n').encode('utf-8')


def relative_paths(workspace, arguments, names):
    """{name: workspace-relative path or list of them} for the checked path arguments in `names`."""
    workspace = Path(workspace)

    def short(value):
        try:
            return Path(value).relative_to(workspace).as_posix()
        except (TypeError, ValueError):
            return '<outside>'
    result = {}
    for name in names:
        value = arguments.get(name)
        if isinstance(value, list):
            result[name] = [short(item) for item in value]
        elif value is not None:
            result[name] = short(value)
    return result
