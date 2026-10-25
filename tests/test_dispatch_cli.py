"""Dispatcher CLI glue: lead planner wiring and the daily digest, against a real board store."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(dispatch, 'worktree_path', lambda employee: tmp_path)
    monkeypatch.setattr(dispatch, 'executable', lambda employee: ['synthetic-worker.exe'])
    monkeypatch.setattr(dispatch, 'build_command', lambda *a, **kw: ['synthetic-worker.exe'])
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, 'new_job', lambda: SimpleNamespace(assign=lambda process: True, terminate=lambda: True))
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
    ledger = SimpleNamespace(set_pid=lambda *a: None, finish=lambda *a, **kw: None, finished_runs=lambda: 0,
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
    monkeypatch.setattr(dispatch, 'worktree_path', lambda employee: tmp_path)
    monkeypatch.setattr(dispatch, 'executable', lambda employee: ['synthetic-opencode.exe'])
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, 'new_job', lambda: SimpleNamespace(assign=lambda process: True, terminate=lambda: True))
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
    # The earlier runs below would have left a git record; without one the run is refused as 'record deleted'.
    dispatch.record_git_after_run(guard, 'opencode', dispatch.git_snapshot(dispatch.git_targets(tmp_path)))
    for model in models[:-1]:
        run_id = ledger.try_start('opencode', daily_runs=100)
        ledger.set_model(run_id, model)
        ledger.finish(run_id, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='')
    run_id = ledger.try_start('opencode', daily_runs=100)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'limit'
    assert _model_of(seen[0]) == models[-1]
    [message] = board.inbox('user:heejun')['messages']
    assert f'모델 {models[-1]}' in message['body']


def test_run_employee_keeps_the_parser_quality_and_marks_a_silent_run_missing(tmp_path, monkeypatch):
    seen = []
    _fake_worker(tmp_path, monkeypatch, [
        ('done, no usage printed', 0),
        ('{"type":"result","usage":{"output_tokens":5}}', 0),
        ('{"type":"result","usage":{"input_tokens":10,"output_tokens":5}}', 0),
    ], seen)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    for _ in range(3):
        run_id = ledger.try_start('opencode', daily_runs=10)
        assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'ok'
    rows = list(reversed(ledger.summary()['recent']))
    assert [row['usage_quality'] for row in rows] == ['missing', 'partial', 'measured']
    assert [(row['input_tokens'], row['output_tokens']) for row in rows] == [(None, None), (None, 5), (10, 5)]


def test_a_run_that_never_launched_records_no_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, 'executable', lambda employee: None)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    run_id = ledger.try_start('opencode', daily_runs=10)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'error'
    assert ledger.summary()['recent'][0]['usage_quality'] is None


# --- Wake input by id (48bea649): the run reads only the messages that woke it -----------------------------

def _reading_worker(tmp_path, monkeypatch, board, prompts):
    """A fake employee that does what the wake prompt says: board_inbox by the ids it was given."""
    import re
    from types import SimpleNamespace
    monkeypatch.setattr(dispatch, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(dispatch, 'ensure_worktree', lambda employee: tmp_path)
    monkeypatch.setattr(dispatch, 'worktree_path', lambda employee: tmp_path)
    monkeypatch.setattr(dispatch, 'executable', lambda employee: ['synthetic-opencode.exe'])
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, 'new_job', lambda: SimpleNamespace(assign=lambda process: True, terminate=lambda: True))
    monkeypatch.setattr(dispatch, 'git', lambda *a, **kw: SimpleNamespace(stdout='0'))

    class Process:
        pid, returncode = 999999, 0
        def poll(self):
            return 0
        def wait(self):
            return 0

    def spawn(argv, **kwargs):
        prompt = ' '.join(argv)
        prompts.append(prompt)
        ids = [int(i) for i in re.search(r'ids=\[([0-9, ]+)\]', prompt).group(1).split(',')]
        board.inbox('opencode:auto', ids=ids)
        kwargs['stdout'].write('{"type":"result","usage":{"input_tokens":1,"output_tokens":1}}')
        kwargs['stdout'].flush()
        return Process()
    monkeypatch.setattr(dispatch.subprocess, 'Popen', spawn)


def _wake_once(guard, board, ledger):
    run_id = ledger.try_start('opencode', daily_runs=100)
    return dispatch.run_employee('opencode', run_id, config(), guard, ledger, board)


def test_the_run_is_given_the_oldest_twenty_pending_ids_and_reads_only_those(tmp_path, monkeypatch):
    guard = Guardrails(tmp_path)
    board, ledger, prompts = BoardStore(guard), DispatchLedger(guard), []
    _reading_worker(tmp_path, monkeypatch, board, prompts)
    sent = [board.send('user:heejun', 'opencode', f'request {n}')['id'] for n in range(25)]
    assert _wake_once(guard, board, ledger) == 'ok'
    assert f'ids={sent[:20]}' in prompts[0]
    assert board.pending_ids('opencode') == sent[20:]           # the rest waits, under a new input key
    assert _wake_once(guard, board, ledger) == 'ok'
    assert f'ids={sent[20:]}' in prompts[1]
    assert board.pending_ids('opencode') == []                  # a 25-message backlog drains in two runs


def test_an_old_broadcast_backlog_does_not_hide_the_waking_message(tmp_path, monkeypatch):
    guard = Guardrails(tmp_path)
    board, ledger, prompts = BoardStore(guard), DispatchLedger(guard), []
    _reading_worker(tmp_path, monkeypatch, board, prompts)
    for n in range(30):
        board.send('claude-code:main', '*', f'old broadcast {n}')
    request = board.send('user:heejun', 'opencode', 'the real request')['id']
    assert _wake_once(guard, board, ledger) == 'ok'
    assert f'ids=[{request}]' in prompts[0]
    assert board.pending_ids('opencode') == []                  # read, so the next tick has nothing to wake for
    assert board.inbox('opencode:auto', mark_read=False)['unread'] == 30   # broadcasts untouched, not deleted


# --- claude/employee-safety: a run cannot get code run by the dispatcher's own git ------------------------

def test_dispatcher_git_runs_without_hooks_or_fsmonitor(tmp_path, monkeypatch):
    import subprocess as sp
    calls = []
    monkeypatch.setattr(dispatch, 'NO_HOOKS', tmp_path / 'no-hooks')
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda argv, **kw: calls.append(argv) or sp.CompletedProcess(argv, 0, '', ''))
    dispatch.git('push', '-q', 'origin', 'auto/codex', check=False)
    assert calls[0][:5] == ['git', '-c', f'core.hooksPath={tmp_path / "no-hooks"}', '-c', 'core.fsmonitor=false']
    assert calls[0][5:] == ['push', '-q', 'origin', 'auto/codex']
    (tmp_path / 'no-hooks' / 'pre-push').write_text('evil', encoding='utf-8')
    with pytest.raises(RuntimeError, match='must stay empty'):
        dispatch.git('status')
    assert len(calls) == 1


def _git_layout(tmp_path):
    """A shared .git and one worktree linked to it, like JarvisWorktrees/<employee>."""
    common = tmp_path / 'repo.git'
    (common / 'hooks').mkdir(parents=True)
    (common / 'hooks' / 'pre-push.sample').write_text('sample', encoding='utf-8')
    (common / 'config').write_text('[core]\n\tbare = false\n', encoding='utf-8')
    admin = common / 'worktrees' / 'wt'
    admin.mkdir(parents=True)
    (admin / 'commondir').write_text('../..\n', encoding='utf-8')
    (admin / 'gitdir').write_text(str(tmp_path / 'wt' / '.git') + '\n', encoding='utf-8')
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    (worktree / '.git').write_text(f'gitdir: {admin}\n', encoding='utf-8')
    return common, admin, worktree


def _tamper_worker(tmp_path, monkeypatch, worktree, change, git_calls, contained=True):
    from types import SimpleNamespace
    monkeypatch.setattr(dispatch, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(dispatch, 'worktree_path', lambda employee: worktree)
    monkeypatch.setattr(dispatch, 'ensure_worktree', lambda employee: git_calls.append(('ensure_worktree',)) or worktree)
    monkeypatch.setattr(dispatch, 'executable', lambda employee: ['synthetic-opencode.exe'])
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, 'new_job', lambda: SimpleNamespace(assign=lambda process: True,
                                                                     terminate=lambda: contained))
    monkeypatch.setattr(dispatch, 'git', lambda *a, **kw: git_calls.append(a) or SimpleNamespace(stdout='1'))

    class Process:
        pid, returncode = 999999, 0
        def poll(self):
            return 0
        def wait(self):
            return 0

    def spawn(argv, **kwargs):
        change()
        kwargs['stdout'].write('{"type":"result","usage":{"input_tokens":1,"output_tokens":1}}')
        kwargs['stdout'].flush()
        return Process()
    monkeypatch.setattr(dispatch.subprocess, 'Popen', spawn)


def _changes(common, admin, worktree):
    return {
        'hook planted': lambda: (common / 'hooks' / 'pre-push').write_text('#!/bin/sh\nevil', encoding='utf-8'),
        'config edited': lambda: (common / 'config').write_text('[core]\n\tsshCommand = evil\n', encoding='utf-8'),
        'commondir moved': lambda: (admin / 'commondir').write_text(str(worktree / 'fake') + '\n', encoding='utf-8'),
        'worktree link moved': lambda: (worktree / '.git').write_text(f'gitdir: {worktree / "fake"}\n', encoding='utf-8'),
    }


@pytest.mark.parametrize('kind', ['hook planted', 'config edited', 'commondir moved', 'worktree link moved'])
def test_a_run_that_changes_git_config_or_hooks_is_not_pushed_and_pauses_the_dispatcher(tmp_path, monkeypatch, kind):
    from jarvis.agents.dispatch_service import read_pause
    common, admin, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    git_calls = []
    _tamper_worker(tmp_path, monkeypatch, worktree, _changes(common, admin, worktree)[kind], git_calls)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    run_id = ledger.try_start('opencode', daily_runs=10)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'ok'
    assert git_calls == [('ensure_worktree',)]  # neither rev-list nor push
    assert read_pause(guard)['paused'] is True
    [message] = board.inbox('user:heejun')['messages']
    assert '[경고]' in message['body'] and 'push를 건너뛰고' in message['body']


def test_an_unchanged_git_layout_still_pushes(tmp_path, monkeypatch):
    from jarvis.agents.dispatch_service import read_pause
    common, _, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    git_calls = []
    _tamper_worker(tmp_path, monkeypatch, worktree, lambda: (worktree / 'work.py').write_text('x', encoding='utf-8'),
                   git_calls)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    run_id = ledger.try_start('opencode', daily_runs=10)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'ok'
    assert [call[0] for call in git_calls] == ['ensure_worktree', 'rev-list', 'push']
    assert read_pause(guard)['paused'] is False and board.inbox('user:heejun')['messages'] == []


def test_git_targets_follow_the_worktree_link_read_before_the_run(tmp_path, monkeypatch):
    common, admin, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    targets = dispatch.git_targets(worktree)
    assert admin / 'commondir' in targets and common / 'hooks' in targets and worktree / '.git' in targets
    before = dispatch.git_state(targets)
    (common / 'hooks' / 'sub').mkdir()
    (common / 'hooks' / 'sub' / 'x').write_text('y', encoding='utf-8')
    assert dispatch.git_state(targets) != before


# --- claude/employee-safety-2: changes between runs, and processes a run leaves behind -------------------

def _two_runs(tmp_path, monkeypatch, between, contained=True):
    """Run opencode twice on one layout; `between` changes it after the first run. Returns what the second
    run did: its result, the git steps it took, whether it spawned, the pause and the user's messages."""
    from jarvis.agents.dispatch_service import read_pause
    common, admin, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    git_calls, spawned = [], []
    _tamper_worker(tmp_path, monkeypatch, worktree, lambda: spawned.append(1), git_calls, contained=contained)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    assert dispatch.run_employee('opencode', ledger.try_start('opencode', daily_runs=10), config(), guard, ledger,
                                 board) == 'ok'
    between(common, admin, worktree)
    git_calls.clear()
    spawned.clear()
    result = dispatch.run_employee('opencode', ledger.try_start('opencode', daily_runs=10), config(), guard, ledger,
                                   board)
    messages = [m['body'] for m in board.inbox('user:heejun')['messages']]
    return result, [call[0] for call in git_calls], bool(spawned), read_pause(guard)['paused'], messages, guard


@pytest.mark.parametrize('kind', ['hook planted', 'config edited', 'commondir moved', 'worktree link moved'])
def test_a_change_made_between_runs_stops_the_next_run_before_any_git(tmp_path, monkeypatch, kind):
    result, steps, spawned, paused, messages, _ = _two_runs(
        tmp_path, monkeypatch, lambda common, admin, worktree: _changes(common, admin, worktree)[kind]())
    assert result == 'error' and steps == [] and not spawned  # not even ensure_worktree's merge
    assert paused is True
    [message] = messages
    assert '[경고]' in message and '지난 실행 뒤' in message


def test_resuming_after_a_between_runs_alarm_accepts_the_new_state(tmp_path, monkeypatch):
    def edit(common, admin, worktree):
        (common / 'config').write_text('[core]\n\tsshCommand = evil\n', encoding='utf-8')
    result, *_, guard = _two_runs(tmp_path, monkeypatch, edit)
    assert result == 'error'
    dispatch.dispatch_control.resume(guard, 'checked')
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    assert dispatch.run_employee('opencode', ledger.try_start('opencode', daily_runs=10), config(), guard, ledger,
                                 board) == 'ok'


def test_upstream_tracking_set_by_other_sessions_is_not_a_change(tmp_path, monkeypatch):
    def track(common, admin, worktree):
        with (common / 'config').open('a', encoding='utf-8') as stream:
            stream.write('[branch "claude/x"]\n\tremote = origin\n\tmerge = refs/heads/claude/x\n')
    result, steps, spawned, paused, messages, _ = _two_runs(tmp_path, monkeypatch, track)
    assert result == 'ok' and steps == ['ensure_worktree', 'rev-list', 'push'] and spawned
    assert paused is False and messages == []


def test_branch_merge_options_still_count_as_a_change(tmp_path, monkeypatch):
    def options(common, admin, worktree):
        with (common / 'config').open('a', encoding='utf-8') as stream:
            stream.write('[branch "auto/opencode"]\n\tmergeOptions = -s evil\n')
    result, steps, *_ = _two_runs(tmp_path, monkeypatch, options)
    assert result == 'error' and steps == []


def test_an_unreadable_baseline_counts_as_a_change(tmp_path, monkeypatch):
    def corrupt(common, admin, worktree):
        (tmp_path / '.jarvis-dispatch' / 'git-baseline.json').write_text('{not json', encoding='utf-8')
    result, steps, spawned, paused, *_ = _two_runs(tmp_path, monkeypatch, corrupt)
    assert result == 'error' and steps == [] and not spawned and paused is True


def test_a_run_whose_processes_cannot_all_be_ended_is_not_pushed(tmp_path, monkeypatch):
    from jarvis.agents.dispatch_service import read_pause
    common, _, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    git_calls = []
    _tamper_worker(tmp_path, monkeypatch, worktree, lambda: None, git_calls, contained=False)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    run_id = ledger.try_start('opencode', daily_runs=10)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'ok'
    assert git_calls == [('ensure_worktree',)]
    assert read_pause(guard)['paused'] is True
    [message] = board.inbox('user:heejun')['messages']
    assert '모두 끝내지 못했어요' in message['body']


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Objects require Windows')
def test_terminating_the_run_job_ends_children_that_outlive_the_worker(tmp_path):
    import subprocess, sys, time as clock
    from jarvis.agents.dispatch_service import KillOnCloseJob
    marker = tmp_path / 'late-write'
    # The worker starts a child that writes after a delay, then exits at once (after the job binds it).
    child = f'import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text("x")'
    worker = subprocess.Popen([sys.executable, '-c', 'import subprocess, sys, time; time.sleep(0.5); '
                               f'subprocess.Popen([sys.executable, "-c", {child!r}])'])
    job = KillOnCloseJob()
    assert job.assign(worker)
    worker.wait()
    assert job.active_processes() >= 1  # the child still runs (a venv python is a launcher plus python)
    assert job.terminate() is True and job.handle is None
    clock.sleep(4)
    assert not marker.exists()


# --- review of #43: parallel runs in one tick, a deleted record ------------------------------------------

def test_a_run_that_passes_the_check_after_another_runs_alarm_does_not_start(tmp_path, monkeypatch):
    """Employee A (codex) already saw a shared change: it paused and recorded. Employee B then reads a record
    that matches, but must still not reach ensure_worktree's merge."""
    common, _, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    git_calls = []
    _tamper_worker(tmp_path, monkeypatch, worktree, lambda: None, git_calls)
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard), DispatchLedger(guard)
    targets = dispatch.git_targets(worktree)
    dispatch.record_git_after_run(guard, 'opencode', dispatch.git_snapshot(targets))
    (common / 'config').write_text('[core]\n\tsshCommand = evil\n', encoding='utf-8')
    assert dispatch.check_git_before_run(guard, 'codex', dispatch.git_snapshot(targets), history=True,
                                         reason='codex saw it') == ['shared']
    run_id = ledger.try_start('opencode', daily_runs=10)
    assert dispatch.run_employee('opencode', run_id, config(), guard, ledger, board) == 'error'
    assert git_calls == []
    assert ledger.summary()['recent'][0]['note'] == 'dispatcher paused'


def test_an_alarm_pauses_before_it_records_and_keeps_the_old_record_if_the_pause_fails(tmp_path, monkeypatch):
    common, _, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    guard = Guardrails(tmp_path)
    targets = dispatch.git_targets(worktree)
    dispatch.record_git_after_run(guard, 'opencode', dispatch.git_snapshot(targets))
    baseline = tmp_path / '.jarvis-dispatch' / 'git-baseline.json'
    old = baseline.read_text(encoding='utf-8')
    (common / 'config').write_text('[core]\n\tsshCommand = evil\n', encoding='utf-8')
    seen_at_pause = []
    monkeypatch.setattr(dispatch.dispatch_control, 'pause',
                        lambda guard, reason: seen_at_pause.append(baseline.read_text(encoding='utf-8')))
    dispatch.check_git_before_run(guard, 'opencode', dispatch.git_snapshot(targets), history=True, reason='x')
    assert seen_at_pause == [old] and baseline.read_text(encoding='utf-8') != old   # paused, then recorded
    newer = baseline.read_text(encoding='utf-8')
    (common / 'config').write_text('[core]\n\tsshCommand = worse\n', encoding='utf-8')
    monkeypatch.setattr(dispatch, 'log_service_error', lambda exc: None)
    monkeypatch.setattr(dispatch.dispatch_control, 'pause', lambda guard, reason: (_ for _ in ()).throw(OSError('locked')))
    for check in (lambda s: dispatch.check_git_before_run(guard, 'opencode', s, history=True, reason='x'),
                  lambda s: dispatch.record_git_after_run(guard, 'opencode', s, alarm='x')):
        check(dispatch.git_snapshot(targets))
        assert baseline.read_text(encoding='utf-8') == newer   # not paused: the next check still refuses


def test_a_deleted_record_after_finished_runs_counts_as_a_change(tmp_path, monkeypatch):
    def delete(common, admin, worktree):
        (tmp_path / '.jarvis-dispatch' / 'git-baseline.json').unlink()
    result, steps, spawned, paused, messages, _ = _two_runs(tmp_path, monkeypatch, delete)
    assert result == 'error' and steps == [] and not spawned and paused is True
    assert '기준 기록' in messages[0]


def test_the_first_run_ever_trusts_and_records_the_state(tmp_path, monkeypatch):
    common, _, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    guard = Guardrails(tmp_path)
    snapshot = dispatch.git_snapshot(dispatch.git_targets(worktree))
    assert dispatch.check_git_before_run(guard, 'opencode', snapshot, history=False, reason='x') == []
    assert json.loads((tmp_path / '.jarvis-dispatch' / 'git-baseline.json').read_text(encoding='utf-8')) == {
        'shared': snapshot['shared'], 'worktrees': {'opencode': snapshot['worktree']}}


# --- review of #43, round 2: runs overlap, so pushes wait for the whole tick ------------------------------

def _ready_run(tmp_path, monkeypatch):
    common, _, worktree = _git_layout(tmp_path)
    monkeypatch.setattr(dispatch, 'GIT_DIR', common)
    guard = Guardrails(tmp_path)
    targets = dispatch.git_targets(worktree)
    dispatch.record_git_after_run(guard, 'opencode', dispatch.git_snapshot(targets), alarm=None)
    git_calls = []
    monkeypatch.setattr(dispatch, 'git', lambda *a, **kw: git_calls.append(a) or SimpleNamespace(stdout='1'))
    return common, guard, {'employee': 'opencode', 'targets': targets, 'worktree': worktree}, git_calls


def test_pushes_after_the_tick_check_git_once_more_and_push_when_unchanged(tmp_path, monkeypatch):
    from jarvis.agents.dispatch_service import read_pause
    _, guard, run, git_calls = _ready_run(tmp_path, monkeypatch)
    board = BoardStore(guard)
    dispatch.push_after_runs(guard, board, [run])
    assert [call[0] for call in git_calls] == ['rev-list', 'push']
    assert read_pause(guard)['paused'] is False


def test_a_change_by_a_run_still_alive_when_another_finished_blocks_every_push(tmp_path, monkeypatch):
    """B finished clean and queued its push; A, still running then, changed .git/config before it ended."""
    from jarvis.agents.dispatch_service import read_pause
    common, guard, run, git_calls = _ready_run(tmp_path, monkeypatch)
    board = BoardStore(guard)
    (common / 'config').write_text('[core]\n\tsshCommand = evil\n', encoding='utf-8')
    dispatch.push_after_runs(guard, board, [run])
    assert git_calls == [] and read_pause(guard)['paused'] is True
    [message] = board.inbox('user:heejun')['messages']
    assert 'push 직전에' in message['body'] and 'opencode' in message['body']


def test_pushes_wait_while_paused(tmp_path, monkeypatch):
    _, guard, run, git_calls = _ready_run(tmp_path, monkeypatch)
    dispatch.dispatch_control.pause(guard, 'user')
    dispatch.push_after_runs(guard, BoardStore(guard), [run])
    assert git_calls == []


def test_a_clean_run_does_not_overwrite_a_shared_state_another_runs_alarm_recorded(tmp_path, monkeypatch):
    common, guard, run, _ = _ready_run(tmp_path, monkeypatch)
    clean_after = dispatch.git_snapshot(run['targets'])
    (common / 'config').write_text('[core]\n\tsshCommand = evil\n', encoding='utf-8')
    alarmed = dispatch.git_snapshot(run['targets'])
    dispatch.record_git_after_run(guard, 'codex', alarmed, alarm='codex changed it')
    dispatch.record_git_after_run(guard, 'opencode', clean_after)
    record = json.loads((tmp_path / '.jarvis-dispatch' / 'git-baseline.json').read_text(encoding='utf-8'))
    assert record['shared'] == alarmed['shared'] and record['worktrees']['opencode'] == clean_after['worktree']
