"""Streamed chat drafts: reply decoding in the worker, guarded line reading, throttled unverified drafts."""
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from jarvis.agents.chat import ChatError, ChatStore
from jarvis.app.chat import ChatDecision, ChatService, DraftWriter
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import WORKERS_ROOT


def worker_module():
    spec = importlib.util.spec_from_file_location('chat_step_under_test', WORKERS_ROOT / 'chat_step.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def step():
    return worker_module()


# --- reply_prefix: the decoded, complete part of the leading "reply" string ---

def test_reply_prefix_decodes_escapes_and_stops_at_the_closing_quote(step):
    assert step.reply_prefix('{"reply": "a\\nb \\"q\\" \\\\ \\u00e9", "tool": "none"}') == 'a\nb "q" \\ é'
    assert step.reply_prefix('{ "reply" : "partial') == 'partial'


def test_reply_prefix_waits_for_unfinished_escapes_and_surrogate_pairs(step):
    assert step.reply_prefix('{"reply":"x\\') == 'x'
    assert step.reply_prefix('{"reply":"x\\u00') == 'x'
    assert step.reply_prefix('{"reply":"x\\ud83d') == 'x'          # high surrogate alone: wait
    assert step.reply_prefix('{"reply":"x\\ud83d\\ude00y') == 'x\U0001F600y'


def test_reply_prefix_is_empty_unless_reply_is_the_first_key(step):
    assert step.reply_prefix('{"tool":"none","reply":"hi"}') == ''
    assert step.reply_prefix('not json') == ''


# --- the worker's streaming path (fake Ollama over the same HTTP shape) ---

class FakeResponse:
    status = 200

    def __init__(self, lines):
        self.lines = list(lines)

    def getheader(self, name, default=None):
        return default

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


def run_worker(step, monkeypatch, chunks, *, stream=True):
    lines = [json.dumps(chunk).encode() + b'\n' for chunk in chunks]

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body, headers):
            assert (method, path) == ('POST', '/api/chat') and json.loads(body)['stream'] is True

        def getresponse(self):
            return FakeResponse(lines)

        def close(self):
            pass

    checked = []
    monkeypatch.setattr(step.support, 'local_model', checked.append)
    monkeypatch.setattr(step.http.client, 'HTTPConnection', Connection)
    payload = {'model': 'qwen2.5-coder:7b', 'messages': [], 'schema': {}, 'stream': stream}
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    stdout = io.StringIO()
    monkeypatch.setattr(step.sys, 'stdin', stdin)
    monkeypatch.setattr(step.sys, 'stdout', stdout)
    code = step.main()
    assert checked == ['qwen2.5-coder:7b']                       # the local-model check still runs first
    return code, [json.loads(line) for line in stdout.getvalue().splitlines()]


def piece(text, done=False, **extra):
    return {'message': {'role': 'assistant', 'content': text}, 'done': done, **extra}


def test_worker_streams_reply_deltas_then_the_same_final_line(step, monkeypatch):
    content = ['{"reply":"안', '녕\\n하', '세요","tool":"none","argument":"",', '"basis":"general","evidence_ids":[]}']
    code, out = run_worker(step, monkeypatch, [piece(content[0]), piece(content[1]), piece(content[2]),
                                               piece(content[3], done=True, done_reason='stop')])
    assert code == 0
    assert [line['delta'] for line in out[:-1]] == ['안', '녕\n하', '세요']
    assert out[-1] == {'ok': True, 'result': {'reply': '안녕\n하세요', 'tool': 'none', 'argument': '',
                                              'basis': 'general', 'evidence_ids': []}}


def test_worker_refuses_a_cut_off_stream(step, monkeypatch):
    code, out = run_worker(step, monkeypatch, [piece('{"reply":"abc'), piece('"}', done=True, done_reason='length')])
    assert code == 1 and out[-1] == {'ok': False, 'error': 'ValueError'}
    code, out = run_worker(step, monkeypatch, [piece('{"reply":"abc')])           # stream ended without done
    assert code == 1 and out[-1]['ok'] is False


def test_worker_stream_keeps_the_256_kb_cap(step, monkeypatch):
    big = [piece('x' * 4000) for _ in range(80)]
    code, out = run_worker(step, monkeypatch, big + [piece('', done=True)])
    assert code == 1 and out[-1] == {'ok': False, 'error': 'ValueError'}


# --- the service reads the worker's lines with the same guards ---

def spawn(script):
    return subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


@pytest.fixture
def service(tmp_path):
    return ChatService(Guardrails(tmp_path))


def test_streaming_infer_passes_deltas_and_returns_the_final_result(service):
    script = ('import sys,json,time\nsys.stdin.buffer.read()\n'
              'for d in ["a","b"]:\n print(json.dumps({"delta":d}),flush=True); time.sleep(0.05)\n'
              'print(json.dumps({"ok":True,"result":{"reply":"ab"}}),flush=True)')
    seen = []
    result = service._infer_streaming(spawn(script), b'{}', time.monotonic() + 20, seen.append)
    assert seen == ['a', 'b'] and result == {'reply': 'ab'}


def test_streaming_infer_ignores_a_failing_draft_callback(service):
    script = ('import sys,json\nsys.stdin.buffer.read()\nprint(json.dumps({"delta":"a"}))\n'
              'print(json.dumps({"ok":True,"result":{"reply":"a"}}))')

    def broken(_):
        raise ChatError('storage busy')
    assert service._infer_streaming(spawn(script), b'{}', time.monotonic() + 20, broken) == {'reply': 'a'}


@pytest.mark.parametrize('script, code', [
    ('import sys,time\nsys.stdin.buffer.read()\ntime.sleep(30)', 'timeout'),
    ('import sys,json\nsys.stdin.buffer.read()\nsys.stdout.write((json.dumps({"delta":"x"*1000})+"\\n")*300)',
     'invalid_response'),
    ('import sys\nsys.stdin.buffer.read()\nprint("not json")', 'invalid_response'),
    ('import sys,json\nsys.stdin.buffer.read()\nprint(json.dumps({"ok":False,"error":"ValueError"}))\nsys.exit(1)',
     'invalid_response'),
])
def test_streaming_infer_keeps_deadline_size_and_exit_checks(service, script, code):
    process = spawn(script)
    with pytest.raises(ChatError) as caught:
        service._infer_streaming(process, b'{}', time.monotonic() + 1.5, lambda _: None)
    assert caught.value.code == code and not str(caught.value).isascii()    # a Korean reason for the user
    assert process.poll() is not None                              # never left running


class HangingProcess:
    """Closed its stdout but does not exit within the 5 s grace period."""
    returncode = None

    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b'{"ok": true, "result": {}}\n')
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def wait(self, timeout=None):
        if timeout is not None and not self.killed:
            raise subprocess.TimeoutExpired('worker', timeout)

    def kill(self):
        self.killed = True


def test_streaming_infer_reports_a_worker_that_hangs_after_closing_stdout(service):
    process = HangingProcess()
    with pytest.raises(ChatError) as caught:
        service._infer_streaming(process, b'{}', time.monotonic() + 20, lambda _: None)
    assert caught.value.code == 'invalid_response'
    assert process.killed and process.stdout.closed and process.stdin.closed


def test_streaming_infer_closes_the_worker_pipes(service):
    process = spawn('import sys,json\nsys.stdin.buffer.read()\nprint(json.dumps({"ok":True,"result":{}}))')
    assert service._infer_streaming(process, b'{}', time.monotonic() + 20, lambda _: None) == {}
    assert process.stdout.closed and process.stdin.closed


def test_streaming_infer_honours_the_stop_marker(service, tmp_path):
    (tmp_path / '.jarvis-stop').write_text('stop')
    process = spawn('import sys,time\nsys.stdin.buffer.read()\ntime.sleep(30)')
    with pytest.raises(Exception, match='stop'):
        service._infer_streaming(process, b'{}', time.monotonic() + 20, lambda _: None)
    assert process.poll() is not None


# --- drafts: throttled, unverified, gone once the turn ends ---

class FakeStore:
    def __init__(self):
        self.saved = []

    def draft(self, identity, request_id, text):
        self.saved.append(text)


def test_draft_writer_saves_at_most_every_quarter_second():
    now = [100.0]
    store = FakeStore()
    writer = DraftWriter(store, 'c', 'r', clock=lambda: now[0])
    for delta in 'abcd':
        writer.add(delta)
        now[0] += 0.1
    assert store.saved == ['a', 'abcd'] and writer.text == 'abcd'
    writer.clear()
    assert store.saved[-1] == '' and writer.text == ''


def test_store_shows_a_draft_only_while_pending_and_finish_clears_it(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    conversation, request = store.create()['id'], uuid4().hex
    store.begin(conversation, request, 'hi', time.time() + 60)
    store.draft(conversation, request, '작성 중')
    assert store.get(conversation)['turns'][0]['draft'] == '작성 중'
    store.finish(conversation, request, reply='final')
    turn = store.get(conversation)['turns'][0]
    assert turn['draft'] == '' and turn['assistant'] == 'final'
    store.draft(conversation, request, 'late')                       # a finished turn takes no draft
    assert store.get(conversation)['turns'][0]['draft'] == ''


def test_an_expired_pending_turn_hides_its_draft(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    conversation, request = store.create()['id'], uuid4().hex
    store.begin(conversation, request, 'hi', time.time() + 0.2)
    store.draft(conversation, request, 'half')
    time.sleep(0.3)
    turn = store.get(conversation)['turns'][0]
    assert turn['status'] == 'interrupted' and turn['draft'] == ''


def test_an_older_chat_database_gains_the_draft_column(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    path = tmp_path / ChatStore.DATABASE
    db = sqlite3.connect(path)
    columns = [row[1] for row in db.execute('PRAGMA table_info(turns)')]
    db.execute('ALTER TABLE turns DROP COLUMN draft')
    db.commit()
    db.close()
    assert 'draft' in columns
    ChatStore(Guardrails(tmp_path))
    db = sqlite3.connect(path)
    assert 'draft' in [row[1] for row in db.execute('PRAGMA table_info(turns)')]
    db.close()


def test_send_streams_the_final_step_and_clears_a_tool_step_draft(service, monkeypatch):
    saved = []
    original = service.store.draft
    monkeypatch.setattr(service.store, 'draft', lambda *args: (saved.append(args[2]), original(*args)))
    steps = iter([ChatDecision(reply='검색해볼게', tool='get_system_status', argument=''),
                  ChatDecision(reply='CPU는 여유', tool='none', argument='', basis='tool', evidence_ids=['e1'])])

    def worker(messages, schema, deadline, on_delta=None):
        decision = next(steps)
        on_delta(decision.reply)
        return decision
    monkeypatch.setattr(service, '_worker', worker)
    service.tools['get_system_status'] = lambda: {'cpu': 1}
    conversation = service.store.create()['id']
    turn = service.send(conversation, uuid4().hex, 'PC 상태 알려줘')['turns'][0]
    assert saved == ['검색해볼게', '', 'CPU는 여유']                   # tool step draft cleared before the answer
    assert turn['status'] == 'completed' and turn['draft'] == ''


def test_a_round_that_must_call_a_tool_streams_no_draft(service, monkeypatch):
    saved, callbacks = [], []
    original = service.store.draft
    monkeypatch.setattr(service.store, 'draft', lambda *args: (saved.append(args[2]), original(*args)))
    monkeypatch.setattr(service, '_infer', lambda *args: {'mode': 'chat', 'evidence': 'local',
                                                         'web_action': 'none', 'unsupported': False})
    steps = iter([ChatDecision(reply='P', tool='get_system_status', argument=''),
                  ChatDecision(reply='CPU는 여유', tool='none', argument='', basis='tool', evidence_ids=['e1'])])

    def worker(messages, schema, deadline, on_delta=None):
        decision = next(steps)
        callbacks.append(on_delta is not None)
        if on_delta is not None:
            on_delta(decision.reply)
        return decision
    monkeypatch.setattr(service, '_worker', worker)
    service.tools['get_system_status'] = lambda: {'cpu': 1}
    turn = service.send(service.store.create()['id'], uuid4().hex, '오늘 PC 상태 한 줄로', mode='auto')['turns'][0]
    assert callbacks == [False, True]                               # the forced status round streams nothing
    assert 'P' not in saved and turn['status'] == 'completed'


# --- warm-up: load the model once when the HUD starts, generate nothing ---

def test_worker_warm_up_only_loads_the_model_with_the_chat_options(step, monkeypatch):
    checked, sent = [], []
    monkeypatch.setattr(step.support, 'local_model', checked.append)
    monkeypatch.setattr(step.support, 'request', lambda path, body: sent.append((path, body)) or
                        {'message': {'role': 'assistant', 'content': ''}, 'done': True, 'done_reason': 'load'})
    stdout = io.StringIO()
    monkeypatch.setattr(step.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'{"model": "qwen2.5-coder:7b", "warm": true}')))
    monkeypatch.setattr(step.sys, 'stdout', stdout)
    assert step.main() == 0 and json.loads(stdout.getvalue()) == {'ok': True, 'result': {}}
    assert checked == ['qwen2.5-coder:7b']
    assert sent == [('/api/chat', {'model': 'qwen2.5-coder:7b', 'messages': [], 'stream': False,
                                   'options': step.OPTIONS, 'keep_alive': step.KEEP_ALIVE})]


def test_worker_warm_up_reports_a_model_that_did_not_load(step, monkeypatch):
    monkeypatch.setattr(step.support, 'local_model', lambda model: None)
    monkeypatch.setattr(step.support, 'request', lambda path, body: {'done': False})
    stdout = io.StringIO()
    monkeypatch.setattr(step.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'{"model": "qwen2.5-coder:7b", "warm": true}')))
    monkeypatch.setattr(step.sys, 'stdout', stdout)
    assert step.main() == 1 and json.loads(stdout.getvalue())['ok'] is False


class WarmProcess:
    def __init__(self, error=None):
        self.sent, self.error, self.done = [], error, False

    def communicate(self, data=None, timeout=None):
        if data is not None:
            self.sent.append(json.loads(data))
        if self.error and not self.done:
            raise self.error
        return b'', b''

    def poll(self):
        return 0 if self.done else None

    def kill(self):
        self.done = True


def test_service_warm_up_runs_in_the_background_and_sends_only_the_model(service, monkeypatch):
    process = WarmProcess()
    monkeypatch.setattr(service, '_spawn_worker', lambda: process)
    service.warm_up().join(5)
    assert process.sent == [{'model': service.model, 'warm': True}]


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('worker', 120), OSError('no python')])
def test_service_warm_up_swallows_failures_and_never_leaves_the_worker_running(service, monkeypatch, error):
    process = WarmProcess(error)
    monkeypatch.setattr(service, '_spawn_worker', lambda: process if isinstance(error, subprocess.TimeoutExpired)
                        else (_ for _ in ()).throw(error))
    thread = service.warm_up()
    thread.join(5)
    assert not thread.is_alive()
    if isinstance(error, subprocess.TimeoutExpired):
        assert process.done


def test_worker_keeps_the_chat_model_loaded_for_five_minutes(step):
    assert step.KEEP_ALIVE == '5m'                          # ADR 0001: unload after 5 idle minutes


def test_service_warm_up_runs_once_while_one_is_in_flight(service, monkeypatch):
    import threading
    release, spawned = threading.Event(), []

    class Slow(WarmProcess):
        def communicate(self, data=None, timeout=None):
            release.wait(5)
            return super().communicate(data, timeout)

    monkeypatch.setattr(service, '_spawn_worker', lambda: spawned.append(1) or Slow())
    first = service.warm_up()
    assert service.warm_up() is first                       # a second mic open reuses the running warm-up
    release.set()
    first.join(5)
    service.warm_up().join(5)                               # after it ends a new one may start
    assert spawned == [1, 1]


def test_service_warm_up_after_a_failed_one_tries_again(service, monkeypatch):
    spawned = []

    def spawn():
        spawned.append(1)
        raise OSError('no python')

    monkeypatch.setattr(service, '_spawn_worker', spawn)
    first = service.warm_up()
    first.join(5)
    second = service.warm_up()                              # the next mic open retries
    second.join(5)
    assert second is not first and spawned == [1, 1]


def test_hud_prefetches_the_chat_model_when_the_mic_opens():
    from jarvis.cli.hud import prefetch_on_listen

    class Voice:
        def subscribe(self, listener):
            self.listener = listener
            return lambda: None

    class Chat:
        calls = 0

        def warm_up(self):
            Chat.calls += 1
            raise RuntimeError('never reaches the voice service')

    voice = Voice()
    prefetch_on_listen(voice, Chat())
    for event in [{'type': 'state', 'state': 'ready'}, {'type': 'transcript', 'text': 'listening'},
                  {'type': 'state', 'state': 'transcribing'}]:
        voice.listener(event)
    assert Chat.calls == 0
    voice.listener({'type': 'state', 'state': 'listening'})   # errors are swallowed by the listener
    assert Chat.calls == 1


def test_service_warm_up_respects_the_stop_marker(service, tmp_path, monkeypatch):
    (tmp_path / '.jarvis-stop').write_text('stop')
    spawned = []
    monkeypatch.setattr(service, '_spawn_worker', lambda: spawned.append(1))
    service.warm_up().join(5)
    assert spawned == []


def test_hud_server_warms_the_model_only_after_a_successful_bind(monkeypatch):
    import asyncio
    import uvicorn
    from jarvis.cli.hud import HUDServer
    calls = []

    async def startup(server, sockets=None):
        assert calls == []
        server.started = sockets != 'fail'
    monkeypatch.setattr(uvicorn.Server, 'startup', startup)
    asyncio.run(HUDServer(uvicorn.Config('unused'), on_start=lambda: calls.append(1)).startup(sockets='fail'))
    assert calls == []
    asyncio.run(HUDServer(uvicorn.Config('unused'), on_start=lambda: calls.append(1)).startup())
    assert calls == [1]


def test_store_keeps_structured_progress_only_while_pending_and_finish_clears_it(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    conversation, request = store.create()['id'], uuid4().hex
    store.begin(conversation, request, 'hi', time.time() + 60)
    assert store.get(conversation)['turns'][0]['progress'] == {}
    store.task_progress(conversation, request, {'kind': 'web', 'line': '검색 중: 제주', 'pages_read': 0})
    turn = store.get(conversation)['turns'][0]
    assert turn['progress'] == {'kind': 'web', 'line': '검색 중: 제주', 'pages_read': 0} and turn['draft'] == ''
    store.finish(conversation, request, reply='final')
    assert store.get(conversation)['turns'][0]['progress'] == {}
    store.task_progress(conversation, request, {'kind': 'web', 'line': 'late'})     # a finished turn takes none
    assert store.get(conversation)['turns'][0]['progress'] == {}


def test_store_progress_is_a_small_redacted_object(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    conversation, request = store.create()['id'], uuid4().hex
    store.begin(conversation, request, 'hi', time.time() + 60)
    for bad in ('text', ['list'], {'line': 'x' * 5000}):
        with pytest.raises(ChatError):
            store.task_progress(conversation, request, bad)
    store.task_progress(conversation, request, {'line': 'key sk-abcdefghijklmnopqrstuvwxyz0123456789'})
    assert 'sk-abcdefghijklmnopqrstuvwxyz0123456789' not in store.get(conversation)['turns'][0]['progress']['line']


def test_an_older_chat_database_gains_the_progress_column(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    path = tmp_path / ChatStore.DATABASE
    db = sqlite3.connect(path)
    db.execute('ALTER TABLE turns DROP COLUMN progress')
    db.commit()
    db.close()
    store = ChatStore(Guardrails(tmp_path))
    conversation, request = store.create()['id'], uuid4().hex
    store.begin(conversation, request, 'hi', time.time() + 60)
    assert store.get(conversation)['turns'][0]['progress'] == {}


def test_store_progress_redacts_each_string_so_a_bearer_word_never_breaks_the_json(tmp_path):
    store = ChatStore(Guardrails(tmp_path))
    conversation, request = store.create()['id'], uuid4().hex
    store.begin(conversation, request, 'hi', time.time() + 60)
    for line in ('검색 중: jwt Bearer token', '검색 중: jwt Bearer [REDACTED]', 'Bearer'):
        store.task_progress(conversation, request, {'line': line, 'recent': [line]})
        saved = store.get(conversation)['turns'][0]['progress']
        assert set(saved) == {'line', 'recent'} and 'token' not in saved['line']
