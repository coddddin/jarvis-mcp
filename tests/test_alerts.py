"""W3 alerts, stage 1: chosen dispatcher events reach the user once a day; the HUD reads them read-only."""
import hashlib
import json

import pytest

from jarvis.agents import alerts
from jarvis.agents import dispatch_control
from jarvis.cli import alerts as cli_alerts
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.core.roster import load_roster
from jarvis.features.board.service import BoardStore

NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def guard(tmp_path):
    return Guardrails(tmp_path)


def test_events_cover_each_kind_with_whitelisted_text():
    found = alerts.events(
        stale=[{'id': 'a' * 32, 'title': 'Long "job"', 'owner': 'codex:chat'}],
        blocked=[{'id': 'b' * 32, 'title': 'x' * 100, 'owner': None}],
        problems={'opencode': ['privacy_open', 'unknown_code'], 'cursor': ['probation_can_edit']},
        not_woken=['cursor', 'cursor'],
        findings=[{'kind': 'service_down', 'ref': 'service', 'detail': {}},
                  {'kind': 'owner_dead', 'ref': 'run:7', 'detail': {'employee': 'codex', 'pid': 99}},
                  {'kind': 'service_record_stale', 'ref': 'service', 'detail': {}}])
    keys = [key for key, _ in found]
    assert keys == ['stale_claim:task:' + 'a' * 32, 'blocked_task:task:' + 'b' * 32,
                    'adapter:cursor:probation_can_edit', 'adapter:opencode:privacy_open',
                    'adapter_check_failed:cursor', 'watch:service_down:service', 'watch:owner_dead:run:7']
    texts = dict(found)
    assert texts['stale_claim:task:' + 'a' * 32] == "멈춘 담당: Long 'job'(codex)"
    assert texts['blocked_task:task:' + 'b' * 32] == '막힌 작업: ' + 'x' * 60 + '(-)'
    assert '99' not in texts['watch:owner_dead:run:7'] and 'codex' in texts['watch:owner_dead:run:7']


def test_not_registered_service_is_its_own_line_and_service_down_stays():
    found = dict(alerts.events(service='not_registered',
                               findings=[{'kind': 'service_down', 'ref': 'service', 'detail': {}}]))
    assert found['service:not_registered'] == '디스패처 설정은 켜졌지만 서비스가 등록되지 않음 (일시정지 아님)'
    assert found['watch:service_down:service'].startswith('디스패처 서비스가 꺼져 있음')
    for status in (None, 'running', 'stopped', 'unknown', 'identity_mismatch'):
        assert not any(key.startswith('service:') for key, _ in alerts.events(service=status))


def _service_config(tmp_path, enabled):
    path = tmp_path / 'dispatcher.json'
    path.write_text(json.dumps({'version': 1, 'enabled': enabled, 'interval_minutes': 10, 'daily_runs': 10,
                                'run_timeout_minutes': 30, 'employees': {'cursor': {}}}), encoding='utf-8')
    return path


@pytest.mark.parametrize('enabled,paused,expected', [(True, False, True), (False, False, False), (True, True, False)])
def test_gather_reports_not_registered_only_when_enabled_and_not_paused(tmp_path, enabled, paused, expected):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    guard, roster = Guardrails(workspace), load_roster()
    if paused:
        dispatch_control.pause(guard, 'test')
    found = dict(cli_alerts.gather(guard, BoardStore(guard, roster=roster), roster, _service_config(tmp_path, enabled)))
    assert ('service:not_registered' in found) is expected


def test_gather_skips_not_registered_while_a_service_record_exists(tmp_path):
    import os, time
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    guard, roster = Guardrails(workspace), load_roster()
    info = workspace / '.jarvis-dispatch/service.json'
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}), encoding='utf-8')
    found = dict(cli_alerts.gather(guard, BoardStore(guard, roster=roster), roster, _service_config(tmp_path, True)))
    assert not any(key.startswith('service:') for key in found)


def test_titles_are_redacted_again(monkeypatch):
    monkeypatch.setattr(alerts, 'redact', lambda text: text.replace('secret', '[REDACTED]'))
    (_, text), = alerts.events(stale=[{'id': 'c' * 32, 'title': 'my secret task', 'owner': 'codex:x'}])
    assert 'secret' not in text and '[REDACTED]' in text


def test_same_event_is_sent_once_per_day_and_again_the_next_day(guard):
    clock, ledger, sent = Clock(), alerts.AlertLedger(guard), []
    found = [('k1', 'one'), ('k2', 'two')]
    assert alerts.notify(ledger, found, sent.append, clock.now) == 2
    assert alerts.notify(ledger, found, sent.append, clock.now + 600) == 0
    assert alerts.notify(ledger, found + [('k3', 'three')], sent.append, clock.now + 1200) == 1
    assert alerts.notify(ledger, found, sent.append, clock.now + 86400) == 2
    assert len(sent) == 3 and sent[0] == '[알림] 자비스 확인 필요\n- one\n- two' and sent[1].endswith('- three')


def test_failed_send_records_nothing_so_the_next_tick_retries(guard):
    ledger = alerts.AlertLedger(guard)
    def fail(text):
        raise RuntimeError('board unavailable')
    with pytest.raises(RuntimeError):
        alerts.notify(ledger, [('k', 'text')], fail, NOW)
    assert alerts.notify(ledger, [('k', 'text')], lambda text: None, NOW) == 1


def test_long_batches_are_bounded_and_the_rest_waits(guard):
    ledger, sent = alerts.AlertLedger(guard), []
    found = [(f'k{i}', f'event {i}') for i in range(13)]
    assert alerts.notify(ledger, found, sent.append, NOW) == 10
    assert sent[0].endswith('- 외 3건은 다음 확인 때 알림') and len(sent[0]) <= 1000
    assert alerts.notify(ledger, found, sent.append, NOW + 60) == 3


def test_old_days_are_pruned(guard):
    ledger = alerts.AlertLedger(guard)
    alerts.notify(ledger, [('old', 'x')], lambda text: None, NOW - 10 * 86400)
    alerts.notify(ledger, [('new', 'y')], lambda text: None, NOW)
    with ledger.connection() as db:
        assert [row['key'] for row in db.execute('SELECT key FROM alerts')] == ['new']


def test_read_today_is_read_only_and_empty_without_a_file(guard, tmp_path):
    assert alerts.read_today(guard, NOW) == []
    assert not (tmp_path / alerts.DIRECTORY).exists()
    alerts.notify(alerts.AlertLedger(guard), [('k', 'hello')], lambda text: None, NOW)
    path = tmp_path / alerts.DATABASE
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert alerts.read_today(guard, NOW) == [{'text': 'hello', 'sent': NOW}]
    assert alerts.read_today(guard, NOW + 86400) == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_read_today_refuses_unusual_storage(guard, tmp_path):
    (tmp_path / alerts.DATABASE).mkdir(parents=True)
    with pytest.raises(GuardrailError):
        alerts.read_today(guard, NOW)


def _dispatcher_config(tmp_path):
    path = tmp_path / 'dispatcher.json'
    path.write_text(json.dumps({'version': 1, 'enabled': False, 'interval_minutes': 10, 'daily_runs': 10,
                                'run_timeout_minutes': 30, 'employees': {'cursor': {}}}), encoding='utf-8')
    return path


def test_send_alerts_gathers_board_and_adapter_events_and_sends_once(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    guard, roster, clock = Guardrails(workspace), load_roster(), Clock()
    board = BoardStore(guard, roster=roster, clock=clock)
    stale = board.create('codex:chat', 'Stuck work', '', ['a.py'])['id']
    board.claim('codex:chat', stale, lease_minutes=5)
    blocked = board.create('codex:chat', 'Waiting on user', '', ['b.py'])['id']
    board.claim('codex:chat', blocked)
    board.release('codex:chat', blocked, 'blocked', 'needs a decision')
    clock.now += 600
    config = _dispatcher_config(tmp_path)
    sent = cli_alerts.send_alerts(guard, board, roster, config, clock.now, sender='local:dispatcher',
                                  not_woken=['cursor'])
    inbox = board.inbox('user:heejun')['messages']
    assert sent >= 3 and len(inbox) == 1 and inbox[0]['from'] == 'local:dispatcher'
    body = inbox[0]['body']
    assert '멈춘 담당: Stuck work(codex)' in body and '막힌 작업: Waiting on user(codex)' in body
    assert '깨우지 않음: cursor' in body and 'needs a decision' not in body
    assert cli_alerts.send_alerts(guard, board, roster, config, clock.now + 60, sender='local:dispatcher',
                                  not_woken=['cursor']) == 0
    assert board.inbox('user:heejun')['messages'] == []


def test_snapshot_lists_todays_alerts_and_survives_a_broken_record(tmp_path, monkeypatch):
    guard, config = Guardrails(tmp_path), _dispatcher_config(tmp_path)
    assert dispatch_control.snapshot(guard, config, clock=lambda: NOW)['alerts'] == {'today': []}
    alerts.notify(alerts.AlertLedger(guard), [('k', 'hello')], lambda text: None, NOW)
    assert dispatch_control.snapshot(guard, config, clock=lambda: NOW)['alerts'] == {
        'today': [{'text': 'hello', 'sent': NOW}]}
    monkeypatch.setattr(alerts, 'read_today', lambda *a, **k: 1 / 0)
    state = dispatch_control.snapshot(guard, config, clock=lambda: NOW)
    assert state['alerts'] == {'today': [], 'error': 'alerts_unavailable'} and 'watch' in state


def _tick_setup(tmp_path, monkeypatch):
    from dataclasses import replace
    from jarvis.agents import worker_adapters as wa
    from jarvis.cli import dispatch as cli_dispatch
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'cursor': {}, 'claude-code': {}}, 'lead': {'enabled': False}}), encoding='utf-8')
    roster = load_roster()
    board = BoardStore(Guardrails(workspace), roster=roster)
    board.send('user:heejun', 'cursor', 'review please')
    board.send('user:heejun', 'claude-code', 'build please')
    launched, logged = [], []
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: Guardrails(workspace))
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda guard: BoardStore(guard, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'run_employee', lambda employee, *a: launched.append(employee) or 'ok')
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'log_service_error', logged.append)
    original = wa.ADAPTERS['cursor'].work_command
    monkeypatch.setitem(wa.ADAPTERS, 'cursor', replace(wa.ADAPTERS['cursor'], work_command=lambda r: original(r) + ['-f']))
    return cli_dispatch, board, launched, logged


def test_tick_alerts_the_user_about_a_kept_asleep_employee_once(tmp_path, monkeypatch):
    cli_dispatch, board, launched, logged = _tick_setup(tmp_path, monkeypatch)
    cli_dispatch.tick()
    assert launched == ['claude-code'] and len(logged) == 1        # only the existing adapter-check log line
    (message,) = board.inbox('user:heejun')['messages']
    assert '깨우지 않음: cursor' in message['body'] and '서비스가 등록되지 않음' in message['body']
    assert '서비스가 꺼져 있음' not in message['body']                 # no record is not a stop
    assert "'-f'" not in message['body'] and 'EXECUTABLE' not in message['body']   # no command fragments
    cli_dispatch.tick()
    assert board.inbox('user:heejun')['messages'] == []          # same events, same day: no repeat
    cli_dispatch.tick(dry_run=True)
    assert board.inbox('user:heejun')['messages'] == []


def test_tick_keeps_working_when_alerts_fail(tmp_path, monkeypatch):
    cli_dispatch, board, launched, logged = _tick_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_dispatch, 'send_alerts', lambda *a, **k: 1 / 0)
    cli_dispatch.tick()
    assert launched == ['claude-code'] and any(isinstance(item, ZeroDivisionError) for item in logged)
