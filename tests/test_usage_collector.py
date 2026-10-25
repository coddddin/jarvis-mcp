"""W3 usage collection (fab78e52): Codex rollout observation and Claude statusline input, no model calls."""
import json
import os
from pathlib import Path
import time

import pytest

from jarvis.agents import usage_collector as uc
from jarvis.agents.dispatch_service import read_usage_sample
from jarvis.core.guardrails import Guardrails

NOW = 1_800_000_000.0
SECRET = 'PRIVATE PROMPT sk-secret-value'


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(epoch))


def token_line(at, primary=81.0, secondary=44.0):
    return json.dumps({'timestamp': iso(at), 'type': 'event_msg', 'payload': {
        'type': 'token_count', 'info': {'last': SECRET},
        'rate_limits': {'limit_id': 'codex', 'primary': {'used_percent': primary, 'window_minutes': 300,
                                                         'resets_at': int(NOW + 3600)},
                        'secondary': {'used_percent': secondary, 'window_minutes': 10080,
                                      'resets_at': int(NOW + 86400)},
                        'credits': {'balance': SECRET}, 'plan_type': 'plus'}}})


def private_lines(n=5):
    return [json.dumps({'timestamp': iso(NOW), 'type': 'response_item',
                        'payload': {'type': 'message', 'content': SECRET + ' token_count rate_limits'}})
            for _ in range(n)]


def rollout(root, name, lines, mtime):
    folder = root / '2026' / '10' / '22'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'rollout-{name}.jsonl'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def sessions(tmp_path):
    root = tmp_path / 'home' / '.codex' / 'sessions'
    root.mkdir(parents=True)
    return root


def test_latest_token_count_becomes_a_workload_sample(sessions):
    rollout(sessions, 'a', private_lines() + [token_line(NOW - 120, 10.0, 20.0)] + private_lines(), NOW - 100)
    rollout(sessions, 'b', private_lines() + [token_line(NOW - 60)] + private_lines(), NOW - 50)
    sample = uc.read_codex_usage(sessions)
    assert sample == {'usage': {'primary': {'usedPercent': 81.0, 'resetsAt': int(NOW + 3600), 'windowMinutes': 300},
                                'secondary': {'usedPercent': 44.0, 'resetsAt': int(NOW + 86400),
                                              'windowMinutes': 10080}},
                      'sampled_at': pytest.approx(NOW - 60), 'source': 'codex_rollout_observed'}
    assert SECRET not in json.dumps(sample)


def test_no_token_count_or_no_files_gives_none(sessions):
    assert uc.read_codex_usage(sessions) is None
    rollout(sessions, 'a', private_lines(), NOW)
    assert uc.read_codex_usage(sessions) is None


def test_only_the_three_newest_files_and_the_tail_are_read(sessions, monkeypatch):
    rollout(sessions, 'old', [token_line(NOW - 999)], NOW - 1000)
    for i in range(3):
        rollout(sessions, f'new{i}', private_lines(), NOW - i)
    assert uc.read_codex_usage(sessions) is None          # the fourth-newest file is never opened
    big = rollout(sessions, 'big', [token_line(NOW - 5)] + ['x' * 1000] * 400, NOW + 10)
    assert big.stat().st_size > uc.TAIL_BYTES
    assert uc.read_codex_usage(sessions) is None          # the token line is beyond the last 256 KB


def test_lines_without_both_markers_are_never_parsed(sessions, monkeypatch):
    rollout(sessions, 'a', private_lines() + [token_line(NOW)], NOW)
    parsed = []
    real = json.loads
    monkeypatch.setattr(uc.json, 'loads', lambda raw, *a, **k: parsed.append(raw) or real(raw, *a, **k))
    uc.read_codex_usage(sessions)
    assert len(parsed) == 1   # the private lines mention the words but never as the token_count type marker


@pytest.mark.parametrize('bad', [
    {'primary': {'used_percent': 120, 'window_minutes': 300, 'resets_at': 1}},
    {'primary': {'used_percent': 'x', 'window_minutes': 300, 'resets_at': 1}},
    {'primary': None}, 'nope'])
def test_invalid_windows_are_ignored(sessions, bad):
    line = json.loads(token_line(NOW))
    line['payload']['rate_limits'] = bad
    rollout(sessions, 'a', [json.dumps(line)], NOW)
    assert uc.read_codex_usage(sessions) is None


def test_errors_never_carry_file_content(sessions, monkeypatch):
    path = rollout(sessions, 'a', [token_line(NOW)], NOW)
    path.write_bytes(b'{"payload": {"type": "token_count", "rate_limits": ' + SECRET.encode() + b'\n')
    assert uc.read_codex_usage(sessions) is None


def test_links_and_paths_outside_the_root_are_refused(sessions, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    rollout(outside, 'x', [token_line(NOW)], NOW)
    link = sessions / 'linked'
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks need privileges on this machine')
    assert uc.read_codex_usage(sessions) is None
    with pytest.raises(ValueError):
        uc.check_sessions_root(link)


def test_root_must_be_an_absolute_existing_plain_directory(tmp_path):
    for bad in (Path('relative/sessions'), tmp_path / 'missing'):
        with pytest.raises(ValueError):
            uc.check_sessions_root(bad)
    (tmp_path / 'file').write_text('x')
    with pytest.raises(ValueError):
        uc.check_sessions_root(tmp_path / 'file')


def test_default_root_is_the_user_codex_sessions_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, 'SESSIONS_ROOT', None)
    monkeypatch.setattr(uc.Path, 'home', lambda: tmp_path)
    assert uc.default_codex_sessions() == tmp_path / '.codex' / 'sessions'


def test_write_and_read_back_through_the_dispatcher_reader(tmp_path, sessions):
    rollout(sessions, 'a', [token_line(NOW - 60)], NOW)
    guard = Guardrails(tmp_path)
    assert uc.collect_codex(guard, sessions) is True
    stored = read_usage_sample(guard, 'codex')
    assert stored['source'] == 'codex_rollout_observed' and stored['usage']['primary']['usedPercent'] == 81.0
    assert not list((tmp_path / '.jarvis-dispatch' / 'usage').glob('*.tmp'))


def test_collect_leaves_the_old_sample_when_nothing_is_found(tmp_path, sessions):
    guard = Guardrails(tmp_path)
    uc.write_usage_sample(guard, 'codex', {'usage': {'primary': {'usedPercent': 5.0, 'resetsAt': 1, 'windowMinutes': 300}},
                                           'sampled_at': 1.0, 'source': 'codex_rollout_observed'})
    assert uc.collect_codex(guard, sessions) is False
    assert read_usage_sample(guard, 'codex')['usage']['primary']['usedPercent'] == 5.0


def test_writer_rejects_unknown_employees_and_shapes(tmp_path):
    guard = Guardrails(tmp_path)
    with pytest.raises(ValueError):
        uc.write_usage_sample(guard, '../x', {'usage': {}, 'sampled_at': 1.0, 'source': 's'})
    with pytest.raises(ValueError):
        uc.write_usage_sample(guard, 'codex', {'usage': 'nope', 'sampled_at': 1.0, 'source': 's'})


def test_claude_statusline_input_becomes_a_sample():
    data = {'session_id': 'abc', 'transcript_path': SECRET, 'cwd': SECRET,
            'rate_limits': {'five_hour': {'used_percentage': 23.5, 'resets_at': 1738425600},
                            'seven_day': {'used_percentage': 41.2, 'resets_at': 1738857600}}}
    sample = uc.claude_statusline_sample(data, now=NOW)
    assert sample == {'usage': {'primary': {'usedPercent': 23.5, 'resetsAt': 1738425600, 'windowMinutes': 300},
                                'secondary': {'usedPercent': 41.2, 'resetsAt': 1738857600, 'windowMinutes': 10080}},
                      'sampled_at': NOW, 'source': 'claude_statusline'}
    assert SECRET not in json.dumps(sample)


@pytest.mark.parametrize('data', [{}, {'rate_limits': {}}, {'rate_limits': {'seven_day': {'used_percentage': 1,
                                  'resets_at': 2}}}, {'rate_limits': {'five_hour': {'used_percentage': 'x'}}}, 'nope'])
def test_claude_statusline_without_a_usable_five_hour_window_gives_none(data):
    assert uc.claude_statusline_sample(data, now=NOW) is None


def test_junctions_are_refused_like_links(sessions, tmp_path):
    _winapi = pytest.importorskip('_winapi')
    outside = tmp_path / 'outside'
    outside.mkdir()
    rollout(outside, 'x', [token_line(NOW)], NOW)
    junction = sessions / 'joined'
    _winapi.CreateJunction(str(outside), str(junction))
    assert junction.is_junction()
    assert uc.read_codex_usage(sessions) is None          # the file behind the junction is never read
    with pytest.raises(ValueError):
        uc.check_sessions_root(junction)
    root_junction = tmp_path / 'sessions-junction'
    _winapi.CreateJunction(str(sessions), str(root_junction))
    with pytest.raises(ValueError):
        uc.check_sessions_root(root_junction / '2026')    # a linked ancestor is refused too


# --- step 2: dispatcher tick collects Codex usage; Claude statusline script ------------------------------

def _tick_harness(tmp_path, monkeypatch):
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import load_roster
    from jarvis.features.board.service import BoardStore
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'codex': {}}, 'lead': {'enabled': False}}), encoding='utf-8')
    roster = load_roster()
    logged = []
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: Guardrails(workspace))
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda guard: BoardStore(guard, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'prepare_run', lambda *a: 'ok')
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'log_service_error', logged.append)
    return cli_dispatch, workspace, logged


def test_tick_collects_codex_usage_but_dry_run_does_not(tmp_path, monkeypatch, sessions):
    cli_dispatch, workspace, logged = _tick_harness(tmp_path, monkeypatch)
    rollout(sessions, 'a', [token_line(time.time() - 30)], time.time())
    monkeypatch.setattr(uc, 'SESSIONS_ROOT', sessions)
    usage_file = workspace / '.jarvis-dispatch' / 'usage' / 'codex.json'
    cli_dispatch.tick(dry_run=True)
    assert not usage_file.exists()
    report = cli_dispatch.tick()
    assert json.loads(usage_file.read_text(encoding='utf-8'))['source'] == 'codex_rollout_observed'
    assert report['codex_workload'] != 'no_usage_sample' and logged == []


def test_tick_survives_a_failing_collector(tmp_path, monkeypatch):
    cli_dispatch, _, logged = _tick_harness(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_dispatch, 'collect_codex', lambda guard: 1 / 0)
    report = cli_dispatch.tick()
    assert report['codex_workload'] == 'no_usage_sample' and len(logged) == 1


STATUS_INPUT = {'session_id': 'abc', 'transcript_path': SECRET, 'cwd': SECRET, 'model': {'display_name': 'Opus'},
                'rate_limits': {'five_hour': {'used_percentage': 23.5, 'resets_at': int(NOW + 3600)},
                                'seven_day': {'used_percentage': 41.2, 'resets_at': int(NOW + 86400)}}}


def test_statusline_script_stores_numbers_and_prints_one_line(tmp_path, capsys):
    import io
    from jarvis.cli import claude_statusline_usage as cli
    assert cli.main(['--workspace', str(tmp_path)], stream=io.BytesIO(json.dumps(STATUS_INPUT).encode())) == 0
    out = capsys.readouterr().out
    assert out.strip() == 'Claude 5h 24% · 주간 41%' and SECRET not in out
    stored = read_usage_sample(Guardrails(tmp_path), 'claude-code')
    assert stored['source'] == 'claude_statusline' and stored['usage']['primary']['usedPercent'] == 23.5
    assert SECRET not in (tmp_path / '.jarvis-dispatch' / 'usage' / 'claude-code.json').read_text(encoding='utf-8')


@pytest.mark.parametrize('raw', [b'', b'not json', b'{}', b'x' * 200000], ids=['empty', 'text', 'no-limits', 'big'])
def test_statusline_script_without_usage_prints_a_fallback_and_stores_nothing(tmp_path, capsys, raw):
    import io
    from jarvis.cli import claude_statusline_usage as cli
    assert cli.main(['--workspace', str(tmp_path)], stream=io.BytesIO(raw)) == 0
    assert capsys.readouterr().out.strip() == 'Claude 사용량 정보 없음'
    assert not (tmp_path / '.jarvis-dispatch' / 'usage').exists()


def test_statusline_script_never_fails(tmp_path, capsys, monkeypatch):
    import io
    from jarvis.cli import claude_statusline_usage as cli
    monkeypatch.setattr(cli, 'write_usage_sample', lambda *a: 1 / 0)
    assert cli.main(['--workspace', str(tmp_path)], stream=io.BytesIO(json.dumps(STATUS_INPUT).encode())) == 0
    assert cli.main(['--bogus'], stream=io.BytesIO(b'{}')) == 0
    assert 'Traceback' not in capsys.readouterr().err


def test_render_statusline_config_quotes_validated_paths(tmp_path):
    import sys
    from jarvis.cli import claude_statusline_usage as cli
    script = tmp_path / 'status line.py'
    script.write_text('# x')
    config = cli.render_statusline(python=Path(sys.executable), script=script, workspace=tmp_path)
    assert config['statusLine']['type'] == 'command'
    assert config['statusLine']['command'] == f'"{Path(sys.executable)}" "{script}" --workspace "{tmp_path}"'
    bad = tmp_path / 'bad&name.py'
    bad.write_text('# x')
    with pytest.raises(ValueError):
        cli.render_statusline(python=Path(sys.executable), script=bad, workspace=tmp_path)


def test_statusline_example_file_is_not_installed_anywhere():
    example = json.loads(Path('config/statusline.claude.example.json').read_text(encoding='utf-8'))
    assert example['statusLine']['type'] == 'command'
    assert 'claude_statusline_usage.py' in example['statusLine']['command']
    assert 'approval' in example['description']


# --- step 3: show the last observed values (display only; the gate still ignores stale samples) ----------

def _store(guard, employee, used5, used7, sampled_at, source):
    uc.write_usage_sample(guard, employee, {'usage': {
        'primary': {'usedPercent': used5, 'resetsAt': int(NOW + 3600), 'windowMinutes': 300},
        'secondary': {'usedPercent': used7, 'resetsAt': int(NOW + 86400), 'windowMinutes': 10080}},
        'sampled_at': sampled_at, 'source': source})


def test_overview_lists_last_values_with_freshness(tmp_path):
    guard = Guardrails(tmp_path)
    assert uc.usage_overview(guard, now=NOW) == {}
    _store(guard, 'codex', 81.0, 44.0, NOW - 60, 'codex_rollout_observed')
    _store(guard, 'claude-code', 23.5, 41.2, NOW - 3600, 'claude_statusline')
    view = uc.usage_overview(guard, now=NOW)
    assert view['codex'] == {'five_hour': 81.0, 'weekly': 44.0, 'sampled_at': NOW - 60,
                             'source': 'codex_rollout_observed', 'fresh': True}
    assert view['claude-code']['fresh'] is False and view['claude-code']['five_hour'] == 23.5


def test_overview_skips_unreadable_files(tmp_path):
    folder = tmp_path / '.jarvis-dispatch' / 'usage'
    folder.mkdir(parents=True)
    (folder / 'codex.json').write_text('{oops', encoding='utf-8')
    assert uc.usage_overview(Guardrails(tmp_path), now=NOW) == {}


def test_snapshot_and_digest_show_usage(tmp_path):
    from jarvis.agents import dispatch_control
    from jarvis.agents.dispatch_policy import build_digest
    guard = Guardrails(tmp_path)
    config = tmp_path / 'dispatcher.json'
    config.write_text(json.dumps({'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10,
                                  'run_timeout_minutes': 30, 'employees': {'codex': {}}}), encoding='utf-8')
    _store(guard, 'codex', 81.0, 44.0, time.time() - 60, 'codex_rollout_observed')
    assert dispatch_control.snapshot(guard, config)['usage']['codex']['fresh'] is True
    view = {'codex': {'five_hour': 81.0, 'weekly': 44.0, 'sampled_at': NOW - 60, 'source': 'codex_rollout_observed',
                      'fresh': True},
            'claude-code': {'five_hour': 23.5, 'weekly': None, 'sampled_at': NOW - 7200, 'source': 'claude_statusline',
                            'fresh': False}}
    text = build_digest(day='d', done=[], review=[], stale=[], runs={}, exhausted={}, pause={}, usage=view)
    line = [l for l in text.splitlines() if l.startswith('- 사용량')][0]
    stamp = time.strftime('%m-%d %H:%M', time.localtime(NOW - 60))
    assert line.startswith('- 사용량(마지막 관찰): ') and f'codex 5h 81%·주간 44% ({stamp})' in line
    assert 'claude-code 5h 24% (' in line and '오래됨' in line
    assert '사용량' not in build_digest(day='d', done=[], review=[], stale=[], runs={}, exhausted={}, pause={})


def test_snapshot_survives_a_failing_overview(tmp_path, monkeypatch):
    from jarvis.agents import dispatch_control
    guard = Guardrails(tmp_path)
    config = tmp_path / 'dispatcher.json'
    config.write_text(json.dumps({'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10,
                                  'run_timeout_minutes': 30, 'employees': {'codex': {}}}), encoding='utf-8')
    monkeypatch.setattr(uc, 'usage_overview', lambda *a, **k: 1 / 0)
    assert dispatch_control.snapshot(guard, config)['usage'] == {}
