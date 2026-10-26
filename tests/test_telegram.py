"""PA4-T1 Telegram status poller: fake transports only, never a real Telegram call."""
import ctypes
from http.client import BadStatusLine, IncompleteRead
import io
import json
import os
from urllib.error import HTTPError, URLError

import pytest

from jarvis.agents import alerts, telegram_view
from jarvis.agents.telegram_state import TelegramState
from jarvis.cli import telegram_poll as tp
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.core.roster import load_roster
from jarvis.features.board.service import BoardError, BoardStore

NOW = 1_800_000_000.0
USER_ID, CHAT_ID = 111222333, 111222333
FAKE_TOKEN = '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11'
CONFIG = tp.TelegramConfig(enabled=True, allowed_user_id=USER_ID, allowed_chat_id=CHAT_ID)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeTransport:
    """Scripted Bot API: each method pops its next result; an exception instance is raised."""

    def __init__(self, **script):
        self.script = {name: list(values) for name, values in script.items()}
        self.calls = []

    def call(self, method, params, *, timeout=15):
        self.calls.append((method, params))
        queue = self.script.get(method)
        value = queue.pop(0) if queue else ({} if method == 'getWebhookInfo' else [] if method == 'getUpdates' else True)
        if isinstance(value, Exception):
            raise value
        return value

    def sent(self):
        return [params['text'] for method, params in self.calls if method == 'sendMessage']


def message(update_id, text='/status', *, date=NOW, user=USER_ID, chat=CHAT_ID, chat_type='private', is_bot=False):
    return {'update_id': update_id, 'message': {'message_id': update_id, 'date': int(date), 'text': text,
                                                'chat': {'id': chat, 'type': chat_type},
                                                'from': {'id': user, 'is_bot': is_bot}}}


@pytest.fixture
def env(tmp_path):
    clock, guard = Clock(), Guardrails(tmp_path)
    board = BoardStore(guard, roster=load_roster(), clock=clock)
    return guard, board, clock


def make(env, transport, *, config=CONFIG, read_status=None, read_task=None):
    guard, board, clock = env
    found = []
    status, task = tp.board_readers(board)
    poller = tp.Poller(guard, transport, TelegramState(guard, clock=clock), config=config,
                       read_status=read_status or status, read_task=read_task or task,
                       alert=found.append, clock=clock, sleep=lambda seconds: found.append(('sleep', seconds)))
    return poller, found


def outcomes(guard):
    with TelegramState(guard).connection() as db:
        return {row['update_id']: row['outcome'] for row in db.execute('SELECT * FROM updates')}


def outbox(guard):
    with TelegramState(guard).connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY id')]


# ---- view -----------------------------------------------------------------------------------------------------

def test_parse_accepts_only_fixed_commands():
    assert telegram_view.parse('/status') == ('status', None)
    assert telegram_view.parse(' /status@jarvis_bot ') == ('status', None)
    assert telegram_view.parse('/task ABCDEF12') == ('task', 'abcdef12')
    assert telegram_view.parse('/task abc') == ('bad_task', None)
    assert telegram_view.parse('/task') == ('bad_task', None)
    assert telegram_view.parse('아까 맡긴 일 어떻게 됐어?') == ('help', None)
    assert telegram_view.parse('/status ' + 'x' * 80) == ('help', None)
    assert telegram_view.parse(None) == ('help', None)


def test_line_shows_id_title_status_owner_update_and_stale():
    task = {'id': 'a' * 32, 'title': 'Fix "build"\nnow', 'status': 'claimed', 'owner': 'codex:auto',
            'stale': True, 'updated': int(NOW - 7200)}
    text = telegram_view.line(task, NOW)
    assert text.startswith('[aaaaaaaa] Fix "build" now | 진행 중 | 담당 codex | 갱신 ')
    assert '(2시간 전)' in text and '멈춤 의심' in text
    reply = telegram_view.status_reply({'active': [task], 'active_total': 7, 'closed': []}, NOW,
                                       {'status': 'running', 'paused': False})
    assert '"' not in reply and '디스패처: 실행 중' in reply and '진행 중인 맡긴 일 7건' in reply and '외 6건' in reply


def test_unreadable_board_is_unknown_not_empty():
    reply = telegram_view.status_reply(None, NOW, None)
    assert '상태 확인 불가' in reply and '디스패처: 알 수 없음' in reply
    empty = telegram_view.status_reply({'active': [], 'active_total': 0, 'closed': []}, NOW,
                                       {'status': 'stopped', 'paused': True})
    assert '0건' in empty and '일시정지' in empty and '확인 불가' not in empty


def test_titles_are_redacted_and_replies_bounded():
    task = {'id': 'b' * 32, 'title': 'api_key = sk-secretvalue123', 'status': 'open', 'owner': None,
            'stale': False, 'updated': int(NOW)}
    assert 'secretvalue' not in telegram_view.line(task, NOW)
    assert len(telegram_view.finish('x' * 5000)) == telegram_view.MAX_REPLY


# ---- board read ------------------------------------------------------------------------------------------------

def test_created_by_reads_only_the_creators_tasks(env):
    guard, board, clock = env
    mine = [board.create('user:heejun', f'mine {n}', '', [])['id'] for n in range(7)]
    board.create('codex:auto', 'not mine', '', [])
    board.claim('claude-code:t', mine[1])
    board.release('claude-code:t', mine[1], 'done', 'finished', verification='pytest ok')
    board.claim('claude-code:t', mine[0], lease_minutes=240)
    clock.now += 2 * 86400
    board.release('claude-code:t', mine[0], 'done', 'finished', verification='pytest ok')
    view = board.created_by('user:heejun')
    assert view['active_total'] == 5 and len(view['active']) == 5
    assert [task['id'] for task in view['closed']] == [mine[0]]   # mine[1] closed more than 24 hours ago
    assert set(view['active'][0]) == {'id', 'title', 'status', 'owner', 'stale', 'updated'}
    assert board.created_by('user:heejun', prefix=mine[2][:32])['matches'][0]['id'] == mine[2]
    assert board.created_by('codex:auto', prefix=mine[2][:12])['matches'] == []
    with pytest.raises(BoardError):
        board.created_by('user:heejun', prefix="a%'--")


# ---- polling ---------------------------------------------------------------------------------------------------

def test_only_the_paired_private_chat_gets_a_reply(env):
    guard, board, clock = env
    poller, _ = make(env, FakeTransport())
    poller.handle(*tp._updates([
        message(1, user=999), message(2, chat=999), message(3, chat_type='group'), message(4, is_bot=True),
        {'update_id': 5, 'edited_message': {}}, {'update_id': 'x'}, message(6)]))
    assert outcomes(guard) == {1: 'rejected', 2: 'rejected', 3: 'rejected', 4: 'rejected', 5: 'rejected',
                               6: 'answered'}
    assert [row['update_id'] for row in outbox(guard)] == [6]


def test_unpaired_config_answers_no_one(env):
    guard, _, _ = env
    poller, _ = make(env, FakeTransport(), config=tp.TelegramConfig())
    poller.handle(*tp._updates([message(1)]))
    assert outcomes(guard) == {1: 'rejected'} and outbox(guard) == []


def test_redelivered_update_is_answered_once_even_after_restart(env):
    guard, board, clock = env
    board.create('user:heejun', 'report', '', [])
    transport = FakeTransport(getUpdates=[[message(10)], [message(10)]])
    poller, _ = make(env, transport)
    poller.handle(*poller.fetch(1))
    restarted, _ = make(env, transport)    # new process, same ledger
    clock.now += 60
    restarted.handle(*restarted.fetch(1))
    assert len(outbox(guard)) == 1
    assert 'offset' not in transport.calls[0][1] and transport.calls[1][1]['offset'] == 11
    restarted.deliver()
    assert len(transport.sent()) == 1 and '[' in transport.sent()[0]


def test_backlog_from_while_pc_was_off_gets_one_combined_reply(env):
    guard, _, clock = env
    poller, _ = make(env, FakeTransport())
    old = NOW - 3 * 3600
    poller.handle(*tp._updates([message(1, date=old), message(2, '/task abcdef', date=old + 60),
                                message(3, date=NOW - 900)]))
    assert outcomes(guard) == {1: 'backlog_merged', 2: 'backlog_merged', 3: 'backlog'}
    [reply] = outbox(guard)
    assert reply['update_id'] == 3 and '질문 3건' in reply['text'] and '3시간 전' in reply['text']
    assert '진행 중인 맡긴 일' in reply['text']


def test_rate_limit_one_answer_per_batch_five_second_gap_and_hourly_cap(env, monkeypatch):
    guard, _, clock = env
    poller, _ = make(env, FakeTransport())
    poller.handle(*tp._updates([message(1), message(2)]))
    clock.now += 2
    poller.handle(*tp._updates([message(3, date=clock.now)]))
    clock.now += 6
    poller.handle(*tp._updates([message(4, date=clock.now)]))
    assert outcomes(guard) == {1: 'answered', 2: 'rate_limited', 3: 'rate_limited', 4: 'answered'}
    replies = [row['text'] for row in outbox(guard)]
    assert '빈도 제한' not in replies[0] and '빈도 제한으로 답하지 않은 질문 2건' in replies[1]
    monkeypatch.setattr(tp, 'MAX_PER_HOUR', 2)
    clock.now += 10
    poller.handle(*tp._updates([message(5, date=clock.now)]))
    assert outcomes(guard)[5] == 'rate_limited'


def test_backlog_and_new_question_in_one_batch_get_one_reply(env):
    guard, _, _ = env
    poller, _ = make(env, FakeTransport())
    poller.handle(*tp._updates([message(1, date=NOW - 3600), message(2, '/task abcdef')]))
    assert outcomes(guard) == {1: 'backlog', 2: 'backlog_merged'}
    [reply] = outbox(guard)
    assert reply['update_id'] == 1 and '질문 1건' in reply['text']


def test_board_failure_replies_unknown(env):
    guard, _, _ = env
    poller, _ = make(env, FakeTransport(), read_status=lambda now: None, read_task=lambda prefix: None)
    poller.handle(*tp._updates([message(1)]))
    assert '상태 확인 불가' in outbox(guard)[0]['text']


def test_task_command_finds_a_unique_prefix(env):
    guard, board, clock = env
    task_id = board.create('user:heejun', 'deploy notes', '', [])['id']
    poller, _ = make(env, FakeTransport())
    poller.handle(*tp._updates([message(1, f'/task {task_id[:8]}')]))
    text = outbox(guard)[0]['text']
    assert f'ID {task_id}' in text and 'deploy notes' in text and '대기' in text


# ---- delivery failures -----------------------------------------------------------------------------------------

def queued(env, poller):
    poller.handle(*tp._updates([message(1)]))
    return outbox(env[0])[0]


def test_network_errors_retry_with_backoff_then_send(env):
    guard, _, clock = env
    transport = FakeTransport(sendMessage=[tp.TelegramError('network'), tp.TelegramError('server'), True])
    poller, _ = make(env, transport)
    queued(env, poller)
    poller.deliver()
    assert outbox(guard)[0]['attempts'] == 1 and outbox(guard)[0]['next_at'] == clock.now + 5
    poller.deliver()            # not due yet
    clock.now += 5
    poller.deliver()
    assert outbox(guard)[0]['next_at'] == clock.now + 10
    clock.now += 10
    poller.deliver()
    row = outbox(guard)[0]
    assert row['state'] == 'sent' and row['text'] == '' and len(transport.calls) == 3


def test_rate_429_waits_retry_after_and_old_replies_expire(env):
    guard, _, clock = env
    poller, _ = make(env, FakeTransport(sendMessage=[tp.TelegramError('rate', 42)]))
    queued(env, poller)
    poller.deliver()
    assert outbox(guard)[0]['next_at'] == clock.now + 42
    clock.now += tp.SEND_WINDOW_SECONDS + 1
    poller.deliver()
    assert outbox(guard)[0]['state'] == 'expired'


def test_bad_request_expires_and_blocked_stops_that_chat(env):
    guard, _, clock = env
    poller, found = make(env, FakeTransport(sendMessage=[tp.TelegramError('bad_request')]))
    queued(env, poller)
    poller.deliver()
    assert outbox(guard)[0]['state'] == 'expired'
    poller2, found2 = make(env, FakeTransport(sendMessage=[tp.TelegramError('blocked')]))
    clock.now += 10
    poller2.handle(*tp._updates([message(2, date=clock.now)]))
    poller2.deliver()
    assert outbox(guard)[1]['state'] == 'expired' and found2 == ['blocked']


def test_invalid_token_on_send_stops_with_alert(env):
    poller, found = make(env, FakeTransport(sendMessage=[tp.TelegramError('token_invalid')]))
    queued(env, poller)
    with pytest.raises(tp.Stop, match='token_invalid'):
        poller.deliver()
    assert found == ['token_invalid']


# ---- run loop ------------------------------------------------------------------------------------------------

def test_webhook_set_refuses_to_start_and_never_deletes_it(env):
    transport = FakeTransport(getWebhookInfo=[{'url': 'https://example.invalid/hook'}])
    poller, found = make(env, transport)
    with pytest.raises(tp.Stop, match='webhook_set'):
        poller.run(rounds=1)
    assert found == ['webhook_set'] and [method for method, _ in transport.calls] == ['getWebhookInfo']


@pytest.mark.parametrize('kind', ['conflict', 'token_invalid'])
def test_conflict_or_invalid_token_while_polling_stops(env, kind):
    poller, found = make(env, FakeTransport(getUpdates=[tp.TelegramError(kind)]))
    with pytest.raises(tp.Stop, match=kind):
        poller.run(rounds=3)
    assert found == [kind]


def test_network_errors_back_off_and_disabled_config_stops(env):
    errors = [tp.TelegramError('network')] * 3 + [tp.TelegramError('rate', 7)]
    poller, found = make(env, FakeTransport(getUpdates=errors))
    assert poller.run(rounds=4) == 'rounds_done'
    assert found == [('sleep', 5), ('sleep', 10), ('sleep', 20), ('sleep', 7)]
    assert poller.run(rounds=4, enabled=lambda: False) == 'disabled'


def test_stop_marker_halts_the_loop(env):
    guard, _, _ = env
    poller, _ = make(env, FakeTransport())
    (guard.workspace / '.jarvis-stop').write_text('stop')
    with pytest.raises(GuardrailError):
        poller.run(rounds=1)


def test_pending_reply_shortens_the_long_poll(env):
    transport = FakeTransport(getUpdates=[[message(1)], []], sendMessage=[tp.TelegramError('network')])
    poller, _ = make(env, transport)
    poller.run(rounds=2)
    polls = [params['timeout'] for method, params in transport.calls if method == 'getUpdates']
    assert polls == [tp.POLL_SECONDS, 1]


# ---- pairing -------------------------------------------------------------------------------------------------

def test_pairing_accepts_the_code_after_local_confirmation(env):
    guard, _, clock = env
    transport = FakeTransport(getUpdates=[[message(1, 'hello', user=5, chat=5), message(2, ' 123456 ')]])
    seen = []
    config = tp.pair(guard, transport, TelegramState(guard, clock=clock), code='123456',
                     confirm=lambda ids: seen.append(ids) or True, clock=clock)
    assert config == CONFIG and seen == [(USER_ID, CHAT_ID)]
    assert outcomes(guard) == {1: 'rejected', 2: 'pairing'}
    poller, _ = make(env, FakeTransport(getUpdates=[[message(2)]]))
    poller.handle(*poller.fetch(1))
    assert outbox(guard) == []   # the pairing message is never answered as a command later


@pytest.mark.parametrize('first', ['안녕', '😀', '１２３４５６'])
def test_pairing_survives_non_ascii_messages(env, first):
    guard, _, clock = env
    transport = FakeTransport(getUpdates=[[message(1, first)], [message(2, '123456')]])
    config = tp.pair(guard, transport, TelegramState(guard, clock=clock), code='123456',
                     confirm=lambda ids: True, clock=clock)
    assert config == CONFIG and outcomes(guard) == {1: 'rejected', 2: 'pairing'}
    assert transport.calls[1][1]['offset'] == 2    # the cursor moved past the non-ASCII message


def test_pairing_declined_or_too_many_wrong_codes(env):
    guard, _, clock = env
    state = TelegramState(guard, clock=clock)
    declined = tp.pair(guard, FakeTransport(getUpdates=[[message(1, '123456')]]), state, code='123456',
                       confirm=lambda ids: False, clock=clock)
    assert declined is None
    wrong = [[message(10 + n, '000000')] for n in range(tp.PAIR_MAX_WRONG)]
    assert tp.pair(guard, FakeTransport(getUpdates=wrong), state, code='123456', confirm=lambda ids: True,
                   clock=clock) is None


def test_config_round_trip_and_strict_validation(env):
    guard, _, _ = env
    assert tp.load_config(guard) == tp.TelegramConfig()
    tp.save_config(guard, CONFIG)
    assert tp.load_config(guard) == CONFIG
    guard.validate_path(tp.CONFIG).write_text(json.dumps({'enabled': True, 'allowed_user_id': '1'}))
    with pytest.raises(GuardrailError):
        tp.load_config(guard)


# ---- HTTP transport --------------------------------------------------------------------------------------------

class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return Response(self.outcome)


def http_error(code, body):
    url = f'https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates'
    return HTTPError(url, code, 'error', {}, io.BytesIO(json.dumps(body).encode()))


def test_http_transport_posts_json_and_returns_result():
    opener = Opener(json.dumps({'ok': True, 'result': [1]}).encode())
    assert tp.HttpTransport(FAKE_TOKEN, opener=opener).call('getUpdates', {'timeout': 1}) == [1]
    request = opener.requests[0]
    assert request.get_method() == 'POST' and json.loads(request.data) == {'timeout': 1}
    assert request.full_url == f'https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates'


@pytest.mark.parametrize('outcome, kind, retry', [
    (http_error(429, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 12}}), 'rate', 12),
    (http_error(429, {'ok': False}), 'rate', 30),
    (http_error(401, {'ok': False}), 'token_invalid', None),
    (http_error(404, {'ok': False}), 'token_invalid', None),
    (http_error(403, {'ok': False}), 'blocked', None),
    (http_error(409, {'ok': False}), 'conflict', None),
    (http_error(400, {'ok': False}), 'bad_request', None),
    (http_error(502, {'ok': False}), 'server', None),
    (URLError('down'), 'network', None),
    (IncompleteRead(b'partial'), 'network', None),
    (BadStatusLine('garbage'), 'network', None),
    (TimeoutError(), 'network', None),
    (b'not json', 'invalid_response', None),
    (json.dumps({'ok': False, 'description': 'x'}).encode(), 'invalid_response', None),
])
def test_http_errors_map_to_fixed_kinds_without_the_token(outcome, kind, retry):
    transport = tp.HttpTransport(FAKE_TOKEN, opener=Opener(outcome))
    with pytest.raises(tp.TelegramError) as caught:
        transport.call('getUpdates', {})
    assert caught.value.kind == kind and caught.value.retry_after == retry
    # No chained exception at all: the HTTPError's URL contains the token.
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert FAKE_TOKEN not in str(caught.value) + repr(caught.value) + repr(transport)


def test_redirects_are_refused():
    handler = tp.NoRedirect()
    with pytest.raises(URLError):
        handler.redirect_request(None, None, 302, 'Found', {}, 'https://elsewhere.invalid/')


def test_bad_token_format_is_refused():
    for bad in ('', 'abc', '123:short', FAKE_TOKEN + '\n'):
        with pytest.raises(tp.TokenError):
            tp.HttpTransport(bad)


def test_token_never_reaches_ledger_or_board(env):
    guard, board, clock = env
    board.create('user:heejun', 'status check', '', [])
    updates = json.dumps({'ok': True, 'result': [message(1)]}).encode()

    class Sequence(Opener):
        def open(self, request, timeout):
            self.requests.append(request)
            method = request.full_url.rsplit('/', 1)[1]
            if method == 'getWebhookInfo':
                return Response(json.dumps({'ok': True, 'result': {'url': ''}}).encode())
            if method == 'getUpdates' and len(self.requests) == 2:
                return Response(updates)
            if method == 'sendMessage':
                raise http_error(401, {'ok': False})
            return Response(json.dumps({'ok': True, 'result': []}).encode())

    transport = tp.HttpTransport(FAKE_TOKEN, opener=Sequence(None))
    poller = tp.Poller(guard, transport, TelegramState(guard, clock=clock), config=CONFIG,
                       read_status=tp.board_readers(board)[0], read_task=tp.board_readers(board)[1],
                       alert=tp.make_alert(guard, board, clock), clock=clock, sleep=lambda s: None)
    with pytest.raises(tp.Stop, match='token_invalid'):
        poller.run(rounds=2)
    assert board.inbox('user:heejun')['messages'][0]['body'].endswith(alerts.TELEGRAM_TEXT['token_invalid'])
    secret = FAKE_TOKEN.encode()
    for path in guard.workspace.rglob('*'):
        if path.is_file():
            assert secret not in path.read_bytes(), path.name


# ---- token storage ----------------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name != 'nt', reason='DPAPI is Windows only')
def test_dpapi_token_round_trip(tmp_path):
    def protect(data, entropy):
        source, keep1 = tp._blob(data)
        extra, keep2 = tp._blob(entropy)
        result = tp._Blob()
        assert ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, ctypes.byref(extra), None, None,
                                                      0x1, ctypes.byref(result))
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(result.pbData)

    path = tmp_path / 'bot-token.bin'
    path.write_bytes(protect(FAKE_TOKEN.encode(), tp.ENTROPY))
    assert FAKE_TOKEN.encode() not in path.read_bytes()
    assert tp.load_token(path) == FAKE_TOKEN
    path.write_bytes(protect(FAKE_TOKEN.encode(), b'other-entropy'))
    with pytest.raises(tp.TokenError):
        tp.load_token(path)
    path.write_bytes(protect(b'not a token', tp.ENTROPY))
    with pytest.raises(tp.TokenError, match='format'):
        tp.load_token(path)
    with pytest.raises(tp.TokenError, match='missing'):
        tp.load_token(tmp_path / 'absent.bin')


def test_check_reports_setup_without_network_or_token(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    monkeypatch.setenv('JARVIS_WORKSPACE', str(workspace))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    assert tp.main(['check']) == 0


def test_alert_events_accept_only_fixed_telegram_codes():
    found = alerts.events(telegram=['token_invalid', 'bogus', 'token_invalid', 'blocked'])
    assert [key for key, _ in found] == ['telegram:blocked', 'telegram:token_invalid']
