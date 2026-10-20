"""Fail-closed input policies. This is an application boundary, not an OS sandbox."""

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import threading


class GuardrailError(ValueError):
    """An operation was rejected by policy."""


@dataclass(frozen=True)
class SafeCommand:
    operation: str
    argument: str


class Guardrails:
    MAX_CALLS = 20

    def __init__(self, workspace: Path):
        if not workspace.is_absolute():
            raise GuardrailError("Workspace must be an absolute path.")
        try:
            self.workspace = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GuardrailError("Workspace must already exist.") from exc
        if not self.workspace.is_dir():
            raise GuardrailError("Workspace must be a directory.")
        self._calls = 0
        self._lock = threading.Lock()

    def enter_call(self) -> None:
        # Process-wide limit is intentionally stricter than a caller-selected task ID.
        with self._lock:
            self.check_running()
            if self._calls >= self.MAX_CALLS:
                raise GuardrailError("Server tool-call budget exhausted; operator restart required.")
            self._calls += 1

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
