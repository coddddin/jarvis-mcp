"""Board 204bc125 part 5: the per-call audit log (names and relative paths only, fail-open for reads, fail-closed
otherwise) and the MCP doctor."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

from jarvis.core.guardrails import Guardrails
from jarvis.features.mcp_audit import service as audit_service
from jarvis.features.mcp_audit.service import FOLDER, LINE_BYTES, AuditLog, bounded
from jarvis.features.mcp_client.service import McpError, McpSession, McpSettings, discover
from jarvis.features.mcp_doctor.service import audit_summary, doctor, report_lines

PATH_SERVER = str(Path(__file__).parent / 'fixtures' / 'mcp_path_server.py')
ENV_SERVER = str(Path(__file__).parent / 'fixtures' / 'mcp_env_server.py')
SECRET = 'my private question 7731'


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'projects').mkdir()
    (tmp_path / 'projects' / 'app.bin').write_bytes(b'MZ')
    return Guardrails(tmp_path)


@pytest.fixture(autouse=True)
def fresh_counter(monkeypatch):
    monkeypatch.setattr(audit_service, '_MISSED', {'pending': 0, 'total': 0})


def approved(guard, pins=None):
    settings = McpSettings(guard)
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATH_SERVER])
    found = discover(guard, settings.load(), 'paths')
    settings.approve('paths', found['tools'], pins or {
        'open_binary': {'effect': 'read'}, 'look': {'effect': 'read', 'not_paths': ['url']},
        'save_report': {'effect': 'confirm', 'path_args': [{'name': 'output_path', 'mode': 'write'}],
                        'not_paths': ['text']}})
    settings.set_roots('paths', ['projects'])
    return settings.load()


def lines(guard):
    return [json.loads(line) for path in sorted((guard.workspace / FOLDER).glob('*.jsonl'))
            for line in path.read_text(encoding='utf-8').splitlines()]


def raw_log(guard):
    return ''.join(path.read_text(encoding='utf-8') for path in (guard.workspace / FOLDER).glob('*.jsonl'))


class Broken(AuditLog):
    """An audit log whose writes fail, like a full disk."""
    def write(self, entry):
        audit_service._MISSED['pending'] += 1
        audit_service._MISSED['total'] += 1
        return False


# --- what a call writes -----------------------------------------------------------------------------------------

def test_a_call_writes_its_attempt_and_outcome_with_names_and_relative_paths_only(guard):
    with McpSession(guard, approved(guard)) as session:
        session.call('mcp:paths/open_binary', {'path': 'projects/app.bin'})
        session.call('mcp:paths/look', {'query': SECRET})
    attempt, outcome, second, _ = lines(guard)
    assert attempt['ev'] == 'attempt' and attempt['server'] == 'paths' and attempt['tool'] == 'open_binary'
    assert attempt['paths'] == {'path': 'projects/app.bin'} and attempt['args'] == ['path'] and attempt['effect'] == 'read'
    assert outcome['ev'] == 'outcome' and outcome['id'] == attempt['id'] and outcome['outcome'] == 'ok'
    assert outcome['chars'] == len('open:' + str(guard.workspace / 'projects' / 'app.bin')) and outcome['seconds'] >= 0
    assert second['args'] == ['query'] and second['paths'] == {}
    text = raw_log(guard)
    assert SECRET not in text and str(guard.workspace) not in text and 'open:' not in text


def test_an_argument_name_the_schema_does_not_declare_is_counted_not_written(guard):
    with McpSession(guard, approved(guard)) as session:
        session._ask = lambda command, seconds: {'ok': True, 'is_error': False, 'text': 'x', 'truncated': False}
        session.call('mcp:paths/look', {'query': 'x', SECRET: 'y'})
    attempt = lines(guard)[0]
    assert attempt['args'] == ['query'] and attempt['extra'] == 1
    assert SECRET not in raw_log(guard)


def test_refused_calls_write_one_line_with_a_reason_code(guard):
    with McpSession(guard, approved(guard)) as session:
        with pytest.raises(McpError):
            session.call('mcp:paths/open_binary', {'path': '../outside.bin'})
        with pytest.raises(McpError):
            session.call('mcp:paths/save_report', {'output_path': 'projects/r.txt', 'text': SECRET})
        with pytest.raises(McpError):
            session.call('mcp:paths/compare', {'files': []})
    assert [(row['ev'], row['reason'], row['tool']) for row in lines(guard)] == [
        ('refused', 'arguments_refused', 'open_binary'), ('refused', 'confirm_required', 'save_report'),
        ('refused', 'not_attached', None)]
    assert SECRET not in raw_log(guard) and 'outside' not in raw_log(guard)


def test_the_attempt_is_on_disk_before_the_worker_is_asked_and_a_lost_worker_is_recorded(guard):
    seen = []
    with McpSession(guard, approved(guard)) as session:
        def gone(command, seconds):
            seen.append([row['ev'] for row in lines(guard)])
            raise McpError('The MCP session is not running.')
        session._ask = gone
        with pytest.raises(McpError):
            session.call('mcp:paths/open_binary', {'path': 'projects/app.bin'})
    assert seen == [['attempt']]
    assert [row.get('outcome') for row in lines(guard)] == [None, 'failed']
    assert audit_summary(lines(guard)) ['unfinished'] == 0
    assert audit_summary(lines(guard)[:1])['unfinished'] == 1       # a parent that died after the attempt


# --- fail-open for reads, fail-closed otherwise -----------------------------------------------------------------

def test_a_read_call_runs_when_the_log_cannot_be_written_and_the_miss_is_reported_later(guard):
    config = approved(guard)
    with McpSession(guard, config, audit=Broken(guard)) as session:
        assert session.call('mcp:paths/look', {'query': 'x'})['text'] == 'look:x|'
        assert session.audit_misses == 2
    assert audit_service.missed_writes() == 2
    AuditLog(guard).write({'ev': 'refused', 'server': 'paths', 'tool': 'look', 'reason': 'test'})
    assert lines(guard)[-1]['missed'] == 2
    assert doctor(guard, start=False)['audit']['missed_logged'] == 2


@pytest.mark.parametrize('pins, call', [
    ({'look': {'effect': 'auto', 'not_paths': ['url']}}, ('mcp:paths/look', {'query': 'x'}, False)),
    ({'save_report': {'effect': 'confirm', 'path_args': [{'name': 'output_path', 'mode': 'write'}],
                      'not_paths': ['text']}}, ('mcp:paths/save_report', {'output_path': 'projects/r.txt',
                                                                           'text': 'x'}, True))])
def test_an_auto_confirm_or_writing_call_is_refused_when_the_log_cannot_be_written(guard, pins, call):
    name, arguments, confirmed = call
    with McpSession(guard, approved(guard, pins), audit=Broken(guard)) as session:
        asked = []
        real = session._ask
        session._ask = lambda command, seconds: asked.append(command) or real(command, seconds)
        with pytest.raises(McpError, match='호출 기록을 남길 수 없어'):
            session.call(name, arguments, confirmed=confirmed)
        assert asked == []


# --- bounds -----------------------------------------------------------------------------------------------------

def test_a_line_is_bounded_and_keeps_its_identity():
    record = {'t': 'x', 'ev': 'attempt', 'id': 'a-1', 'server': 's', 'tool': 't', 'args': ['files'] * 10,
              'paths': {'files': ['projects/' + 'd' * 400 + str(n) for n in range(16)]}}
    data = bounded(record)
    row = json.loads(data)
    assert len(data) <= LINE_BYTES and row['shortened'] and row['id'] == 'a-1' and len(row['paths']['files']) <= 3
    assert len(bounded({'t': 'x', 'ev': 'attempt', 'id': 'a', 'tool': 't' * 5000})) <= LINE_BYTES


def test_a_full_day_file_gets_one_cap_line_and_then_nothing(guard, monkeypatch):
    monkeypatch.setattr(audit_service, 'DAY_BYTES', 6 * LINE_BYTES)
    log = AuditLog(guard)
    # Each line is about 1.95 KB; 5 fit below the reserve, the 6th becomes the cap line, the rest are not written.
    results = [log.write({'ev': 'refused', 'server': 's', 'tool': 't', 'reason': 'r' * 1900}) for _ in range(8)]
    rows = lines(guard)
    assert results == [True] * 5 + [False] * 3
    assert [row['ev'] for row in rows].count('cap') == 1 and rows[-1]['ev'] == 'cap' and len(rows) == 6
    day_file = next((guard.workspace / FOLDER).glob('*.jsonl'))
    assert day_file.stat().st_size <= 6 * LINE_BYTES
    assert audit_service.missed_writes() == 3


def test_files_older_than_the_keep_days_are_deleted(guard):
    folder = guard.workspace / FOLDER
    folder.mkdir()
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    old, recent = folder / f'{now - timedelta(days=31):%Y-%m-%d}.jsonl', folder / f'{now - timedelta(days=29):%Y-%m-%d}.jsonl'
    for path in (old, recent, folder / 'notes.jsonl'):
        path.write_text('{}\n', encoding='utf-8')
    assert AuditLog(guard, now=lambda: now).write({'ev': 'refused'})
    assert not old.exists() and recent.exists() and (folder / 'notes.jsonl').exists()


def test_the_log_folder_is_not_a_server_folder_and_cannot_be_an_approved_root(guard):
    assert not FOLDER.startswith('.jarvis-mcp/')
    (guard.workspace / FOLDER).mkdir()
    settings = McpSettings(guard)
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATH_SERVER])
    with pytest.raises(McpError):
        settings.set_roots('paths', [FOLDER])


# --- doctor -----------------------------------------------------------------------------------------------------

def test_doctor_starts_each_approved_server_once_and_summarises_its_log(guard):
    with McpSession(guard, approved(guard)) as session:
        session.call('mcp:paths/look', {'query': 'x'})
        with pytest.raises(McpError):
            session.call('mcp:paths/open_binary', {'path': '../x'})
    result = doctor(guard)
    server = result['servers'][0]
    assert server['session']['state'] == 'ready' and server['session']['offered'] == 3
    assert server['pins'] == {'read': 2, 'confirm': 1} and all(check['ok'] for check in server['checks'])
    assert server['audit']['calls'] == 1 and server['audit']['outcomes'] == {'ok': 1}
    assert server['audit']['refused'] == {'arguments_refused': 1}
    assert result['audit'] == {'missed_logged': 0, 'missed_this_process': 0, 'cap_days': []}
    text = '\n'.join(report_lines(result))
    assert '[Paths]' in text and '시작 ready' in text and '호출 기록 실패' in text
    assert [row['ev'] for row in lines(guard)].count('attempt') == 1          # doctor itself calls no tool


def test_doctor_names_a_missing_program_and_a_bad_fixed_folder_without_its_value(guard, tmp_path_factory):
    outside = tmp_path_factory.mktemp('jdk')
    settings = McpSettings(guard)
    settings.add_server('env', 'Env', sys.executable, ['-I', ENV_SERVER], fixed_env={'JAVA_HOME': str(outside)})
    outside.rmdir()
    result = doctor(guard)
    server = result['servers'][0]
    checks = {check['name']: check for check in server['checks']}
    assert not checks['fixed_settings']['ok'] and server['session'] is None        # pending: not started
    assert str(outside) not in json.dumps(result) and str(outside) not in '\n'.join(report_lines(result))
    config = settings.load()
    gone = config.servers[0].model_copy(update={'command': str(guard.workspace / 'missing.exe')})
    from jarvis.features.mcp_doctor.service import check_server
    assert not {check['name']: check for check in check_server(guard, gone, start=False)['checks']}['program']['ok']
