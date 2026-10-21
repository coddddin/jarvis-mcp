"""Shared test isolation."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_codex_sessions(tmp_path_factory, monkeypatch):
    """The dispatcher tick reads Codex usage from ~/.codex/sessions, which holds the user's conversations.
    Tests must never touch it: point the default at an empty folder."""
    from jarvis.agents import usage_collector
    empty = tmp_path_factory.mktemp('codex-sessions')
    monkeypatch.setattr(usage_collector, 'SESSIONS_ROOT', empty)
