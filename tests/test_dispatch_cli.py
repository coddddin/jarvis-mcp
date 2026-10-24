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
    ledger = SimpleNamespace(set_pid=lambda *a: None, finish=lambda *a, **kw: None,
                             pick_model=lambda employee, models: models[0], set_model=lambda *a: None)
    assert dispatch.run_employee('opencode', 1, config(), guard, ledger, BoardStore(guard)) == 'ok'
    env = seen[0]
    assert 'GITHUB_TOKEN' not in env and 'CUSTOM_SECRET' not in env
    assert env['JARVIS_WORKSPACE'] == str(tmp_path)
    settings = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert settings['permission']['edit'] == 'deny'
    assert settings['mcp']['jarvis']['environment']['JARVIS_WORKSPACE'] == str(tmp_path)



# --- OpenCode model rotation through run_employee (no real OpenCode; Popen is faked) -----------------------

def _fake_worker(tmp_path, monkeypatch, outputs, seen):
    from types import SimpleNamespace
    monkeypatch.setattr(dispatch, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(dispatch, 'ensure_worktree', lambda employee: tmp_path)
    monkeypatch.setattr(dispatch, 'executable', lambda employee: ['synthetic-opencode.exe'])
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, 'job', lambda: SimpleNamespace(assign=lambda process: True))
    monkeypatch.setattr(dispatch, 'git', lambda *a, **kw: SimpleNamespace(stdout='0'))

    class Process:
        pid = 999999
        def __init__(self, code):
            self.returncode = code
        def poll(self):
            return self.returncode
        def wait(self):
            return self.returncode

    def spawn(argv, **kwargs):
        text, code = outputs.pop(0)
        kwargs['stdout'].write(text)
        kwargs['stdout'].flush()
        seen.append(argv)
        return Process(code)
    monkeypatch.setattr(dispatch.subprocess, 'Popen', spawn)


def _model_of(argv):
    return argv[argv.index('-m') + 1]


def test_rotation_moves_to_the_next_model_after_limit_or_missing_model(tmp_path, monkeypatch):
    models = config().employees['opencode'].models
    seen = []
    _fake_worker(tmp_path, monkeypatch, [
        ('{"type":"error","error":{"name":"ProviderModelNotFoundError"}}\nModel not found: ' + models[0], 1),
        ('Error: rate limit exceeded', 1),
        ('{"type":"result","usage":{"input_tokens":10,"output_tokens":5}}', 0),
    ], seen)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    statuses = []
    for _ in range(3):
        run_id = ledger.try_start('opencode', daily_runs=10)
        statuses.append(dispatch.run_employee('opencode', run_id, config(), guard, ledger, board))
    assert statuses == ['model_missing', 'limit', 'ok']
    assert [_model_of(argv) for argv in seen] == list(models[:3])
    recent = ledger.summary()['recent']
    assert [row['model'] for row in reversed(recent)] == list(models[:3])
    assert set(ledger.models_backed_off('opencode')) == {models[0], models[1]}
    assert ledger.exhausted_until('opencode') is None          # the employee itself is not waiting
    assert ledger.models_exhausted_until('opencode', models) is None
    assert ledger.pick_model('opencode', models) == models[2]  # the model that just worked stays first choice
    # Per-model backoff is routine while another model is usable: no per-run alert to the user.
    assert board.inbox('user:heejun')['messages'] == []


def test_rotation_with_every_model_backed_off_does_not_launch(tmp_path, monkeypatch):
    models = config().employees['opencode'].models
    seen = []
    _fake_worker(tmp_path, monkeypatch, [], seen)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    for model in models:
        run_id = ledger.try_start('opencode', daily_runs=100)
        ledger.set_model(run_id, model)
        ledger.finish(run_id, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='')
    assert ledger.models_exhausted_until('opencode', models) is not None
    run_id = ledger.try_start('opencode', daily_runs=100)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'error'
    assert seen == []
    assert ledger.summary()['recent'][0]['note'] == 'all models backed off'


def test_digest_shows_the_current_and_backed_off_models(tmp_path):
    models = config().employees['opencode'].models
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    run_id = ledger.try_start('opencode', daily_runs=10)
    ledger.set_model(run_id, models[0])
    ledger.finish(run_id, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='')
    late = time.mktime(time.localtime()[:3] + (23, 0, 0, 0, 0, -1))
    assert dispatch.send_digest_if_due(guard, board, ledger, config(), late) is True
    body = board.inbox('user:heejun')['messages'][0]['body']
    line = next(x for x in body.splitlines() if x.startswith('- opencode 모델:'))
    assert line.startswith(f'- opencode 모델: 지금 {models[1]}, 소진 {models[0]}(')


def test_one_alert_when_the_last_usable_model_backs_off(tmp_path, monkeypatch):
    models = config().employees['opencode'].models
    seen = []
    _fake_worker(tmp_path, monkeypatch, [('Error: rate limit exceeded', 1)], seen)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    for model in models[:-1]:
        run_id = ledger.try_start('opencode', daily_runs=100)
        ledger.set_model(run_id, model)
        ledger.finish(run_id, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='')
    run_id = ledger.try_start('opencode', daily_runs=100)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'limit'
    assert _model_of(seen[0]) == models[-1]
    [message] = board.inbox('user:heejun')['messages']
    assert f'모델 {models[-1]}' in message['body']
