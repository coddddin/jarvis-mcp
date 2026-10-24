"""Minimal inherited environment for worker subprocesses; no credential fallback.

This limits environment inheritance, not access to account files or OS resources.
"""
import os
from collections.abc import Mapping


INHERITED_KEYS = frozenset({
    'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH',
    'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'CODEX_HOME',
    'USERNAME', 'USERDOMAIN', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ',
    'TERM', 'NO_COLOR', 'PYTHONIOENCODING', 'PYTHONUTF8',
})
MANAGED_KEYS = frozenset({'JARVIS_WORKSPACE', 'JARVIS_DOWNLOADS_READONLY', 'OPENCODE_CONFIG_CONTENT'})


def worker_environment(*, source: Mapping[str, str] | None = None,
                       overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy only OS/CLI essentials; accept only named, trusted adapter settings.

    Tokens, API keys, proxy credentials, interpreter injection flags and ambient
    adapter settings are deliberately absent. Missing authentication must fail
    normally in the CLI, never trigger broader inheritance or paid fallback.
    """
    source = os.environ if source is None else source
    result = {}
    for key, value in source.items():
        if isinstance(key, str) and key.upper() in INHERITED_KEYS:
            if not isinstance(value, str) or '\x00' in value:
                raise ValueError('Invalid inherited worker environment.')
            if key.upper() in result:
                raise ValueError('Ambiguous worker environment.')
            result[key.upper()] = value
    for key, value in (overrides or {}).items():
        if (key not in MANAGED_KEYS or not isinstance(value, str) or '\x00' in value):
            raise ValueError('Invalid managed worker environment.')
        result[key] = value
    return result
