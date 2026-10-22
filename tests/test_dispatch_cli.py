"""Dispatcher CLI glue: lead planner wiring and the daily digest, against a real board store."""
import json
import time
from pathlib import Path

from jarvis.agents.dispatch_policy import DispatchLedger, DispatcherConfig
from jarvis.cli import dispatch
from jarvis.core.guardrails import Guardrails
from jarvis.features.board.service import BoardStore


def config():
    return DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))


def test_lead_without_a_judge_stays_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, 'load_judge', lambda: None)
    guard = Guardrails(tmp_path)
    result = dispatch.run_lead(guard, BoardStore(guard), None, config(), time.time())
    assert result == {'action': 'none', 'reason': 'no_judge'}


def test_lead_errors_never_escape(tmp_path, monkeypatch):
    def broken_judge(payload):
        raise RuntimeError('model down')
    monkeypatch.setattr(dispatch, 'load_judge', lambda: broken_judge)
    monkeypatch.setattr(dispatch, 'plan_next', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(dispatch, 'log_service_error', lambda exc: None)
    guard = Guardrails(tmp_path)
    result = dispatch.run_lead(guard, BoardStore(guard), None, config(), time.time())
    assert result['reason'] == 'lead_error'


def test_digest_is_sent_once_per_day_to_the_user(tmp_path):
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    task = board.create('claude-code:main', '오늘 끝낸 일', '', ['a.py'])['id']
    board.claim('claude-code:main', task)
    board.release('claude-code:main', task, 'done', '완료', verification='pytest ok')
    late = time.mktime(time.localtime()[:3] + (23, 0, 0, 0, 0, -1))
    cfg = config()
    assert dispatch.send_digest_if_due(guard, board, ledger, cfg, late) is True
    assert dispatch.send_digest_if_due(guard, board, ledger, cfg, late + 60) is False
    inbox = board.inbox('user:heejun')['messages']
    assert len(inbox) == 1 and inbox[0]['from'] == 'local:dispatcher'
    assert '자비스 하루 요약' in inbox[0]['body'] and '오늘 끝낸 일' in inbox[0]['body']


def test_employee_spawn_does_not_inherit_inbox_token_or_ambient_config(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-inbox-token')
    monkeypatch.setenv('CUSTOM_SECRET', 'synthetic-secret')
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', 'untrusted ambient config')
    monkeypatch.setenv('JARVIS_WORKSPACE', '/wrong-workspace')
    monkeypatch.setattr(dispatch, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(dispatch, 'ensure_worktree', lambda employee: tmp_path)
    monkeypatch.setattr(dispatch, 'executable', lambda employee: ['synthetic-worker.exe'])
    monkeypatch.setattr(dispatch, 'build_command', lambda *a, **kw: ['synthetic-worker.exe'])
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, 'job', lambda: SimpleNamespace(assign=lambda process: True))
    monkeypatch.setattr(dispatch, 'git', lambda *a, **kw: SimpleNamespace(stdout='0'))
    seen = []
    class Process:
        pid = 999999
        returncode = 0
        def poll(self):
            return 0
        def wait(self):
            return 0
    def spawn(*args, **kwargs):
        seen.append(kwargs['env'])
        return Process()
    monkeypatch.setattr(dispatch.subprocess, 'Popen', spawn)
    guard = Guardrails(tmp_path)
    ledger = SimpleNamespace(set_pid=lambda *a: None, finish=lambda *a, **kw: None)
    assert dispatch.run_employee('opencode', 1, config(), guard, ledger, BoardStore(guard)) == 'ok'
    env = seen[0]
    assert 'GITHUB_TOKEN' not in env and 'CUSTOM_SECRET' not in env
    assert env['JARVIS_WORKSPACE'] == str(tmp_path)
    settings = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert settings['permission']['edit'] == 'deny'
    assert settings['mcp']['jarvis']['environment']['JARVIS_WORKSPACE'] == str(tmp_path)

