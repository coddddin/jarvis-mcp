"""Fail-closed input policies. This is an application boundary, not an OS sandbox."""

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import threading
import time


class GuardrailError(ValueError):
    """An operation was rejected by policy."""


@dataclass(frozen=True)
class SafeCommand:
    operation: str
    argument: str


class Guardrails:
    # Process-wide budgets in fixed windows; they recover with time instead of needing a restart.
    WINDOW_SECONDS = 600
    MAX_CALLS = 20          # standard + sensitive calls per window
    MAX_SENSITIVE = 5       # state-changing, privacy or external-model calls per window
    MAX_LIGHT = 60          # coordination/listing calls per window, outside MAX_CALLS
    DAILY_MAX = 600         # all tiers per local calendar day
    DAILY_SENSITIVE = 40
    TIERS = ('light', 'standard', 'sensitive')

    def __init__(self, workspace: Path, *, clock=time.time):
        if not workspace.is_absolute():
            raise GuardrailError("Workspace must be an absolute path.")
        try:
            self.workspace = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GuardrailError("Workspace must already exist.") from exc
        if not self.workspace.is_dir():
            raise GuardrailError("Workspace must be a directory.")
        self._clock = clock
        self._lock = threading.Lock()
        self._window_start = clock()
        self._calls = self._sensitive = self._light = 0
        self._day = self._day_key(self._window_start)
        self._daily = self._daily_sensitive = 0

    @staticmethod
    def _day_key(now):
        return time.localtime(now)[:3]

    def _roll(self, now):
        if now - self._window_start >= self.WINDOW_SECONDS:
            self._window_start = now
            self._calls = self._sensitive = self._light = 0
        if self._day_key(now) != self._day:
            self._day = self._day_key(now)
            self._daily = self._daily_sensitive = 0

    def _retry(self):
        return time.strftime('%H:%M:%S', time.localtime(self._window_start + self.WINDOW_SECONDS))

    def enter_call(self, tier: str = 'standard') -> None:
        if tier not in self.TIERS:
            raise GuardrailError("Unknown tool tier.")
        with self._lock:
            self.check_running()
            self._roll(self._clock())
            if self._daily >= self.DAILY_MAX or (tier == 'sensitive' and self._daily_sensitive >= self.DAILY_SENSITIVE):
                raise GuardrailError("Server daily tool-call budget exhausted; it resets at local midnight.")
            if tier == 'light':
                if self._light >= self.MAX_LIGHT:
                    raise GuardrailError(f"Server light tool-call budget exhausted; retry after {self._retry()}.")
                self._light += 1
            else:
                if self._calls >= self.MAX_CALLS:
                    raise GuardrailError(f"Server tool-call budget exhausted for this window; retry after {self._retry()}.")
                if tier == 'sensitive':
                    if self._sensitive >= self.MAX_SENSITIVE:
                        raise GuardrailError(f"Server sensitive tool-call budget exhausted; retry after {self._retry()}.")
                    self._sensitive += 1
                    self._daily_sensitive += 1
                self._calls += 1
            self._daily += 1

    def budget_status(self) -> dict:
        with self._lock:
            self._roll(self._clock())
            return {'window': {'seconds': self.WINDOW_SECONDS, 'used': self._calls, 'limit': self.MAX_CALLS,
                               'sensitive_used': self._sensitive, 'sensitive_limit': self.MAX_SENSITIVE,
                               'light_used': self._light, 'light_limit': self.MAX_LIGHT,
                               'resets_at': self._window_start + self.WINDOW_SECONDS},
                    'daily': {'used': self._daily, 'limit': self.DAILY_MAX,
                              'sensitive_used': self._daily_sensitive, 'sensitive_limit': self.DAILY_SENSITIVE}}

    def check_running(self) -> None:
        """Poll cancellation inside long operations without spending another tool call."""
        if (self.workspace / '.jarvis-stop').exists():
            raise GuardrailError("Server stopped by workspace stop marker.")

    def validate_path(self, value: str, *, must_exist: bool = True) -> Path:
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise GuardrailError("Path must be a nonempty string of at most 1024 characters.")
        # Reject Windows device paths, UNC paths, ADS, environment expansion and globs.
        if any(ord(c) < 32 for c in value) or any(c in value for c in '*?%$'):
            raise GuardrailError("Unsupported path syntax.")
        if value.startswith(('\\\\', '//')):
            raise GuardrailError("Network and device paths are forbidden.")
        candidate = Path(value)
        if ':' in value[2:] or (':' in value and not candidate.is_absolute()):
            raise GuardrailError("Alternate streams and drive-relative paths are forbidden.")
        if '..' in candidate.parts:
            raise GuardrailError("Parent traversal is forbidden.")
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve(strict=must_exist)
            # realpath can return an extended-length (\\?\C:\...) path when the file is replaced
            # mid-call. Network/device input is rejected above, so this only normalizes the result.
            text = str(resolved)
            if text.startswith('\\\\?\\') and not text.upper().startswith('\\\\?\\UNC\\'):
                resolved = Path(text[4:])
            if not resolved.is_relative_to(self.workspace):
                raise GuardrailError("Path is outside JarvisWorkspace.")
            # Reject links/junctions even when they currently target the workspace.
            cursor = candidate
            while cursor != self.workspace:
                if cursor.is_symlink() or cursor.is_junction():
                    raise GuardrailError("Symbolic links and junctions are forbidden.")
                if cursor.parent == cursor:
                    raise GuardrailError("Path is outside JarvisWorkspace.")
                cursor = cursor.parent
            return resolved
        except (OSError, RuntimeError) as exc:
            raise GuardrailError("Path cannot be resolved or does not exist.") from exc

    def validate_command(self, command: str) -> SafeCommand:
        if not isinstance(command, str) or not command.strip() or len(command) > 1024:
            raise GuardrailError("Command must contain 1 to 1024 characters.")
        if any(ord(c) < 32 for c in command) or any(c in command for c in ';&|><`$%(){}!'):
            raise GuardrailError("Shell operators, expansion and control characters are forbidden.")
        try:
            parts = shlex.split(command, posix=True)
        except ValueError as exc:
            raise GuardrailError("Malformed command quoting.") from exc
        if not parts:
            raise GuardrailError("Empty command.")
        op, *args = parts
        # Deny known dangerous families for useful diagnostics; the allowlist is authoritative.
        if re.fullmatch(r'(?i)(rm|del|erase|rmdir|rd|format|diskpart|sudo|su|chmod|chown|'
                        r'powershell|pwsh|cmd|bash|sh|python|python3|curl|wget)(\.exe)?', op):
            raise GuardrailError("Destructive commands and general-purpose interpreters are forbidden.")
        if op in {'pwd', 'whoami', 'hostname'} and not args:
            return SafeCommand(op, '')
        if op == 'echo':
            return SafeCommand(op, ' '.join(args))
        if op == 'ls' and len(args) <= 1:
            path = self.validate_path(args[0] if args else '.')
            if not path.is_dir():
                raise GuardrailError("ls requires a directory.")
            return SafeCommand(op, str(path))
        raise GuardrailError("Allowed commands: pwd, whoami, hostname, echo TEXT, ls [directory].")
