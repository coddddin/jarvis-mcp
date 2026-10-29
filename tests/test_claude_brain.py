"""Claude CLI brain (P2): argv limits, stream parsing, error mapping, process caps. No real CLI runs."""
import io
import json
import subprocess
import threading
import time

import pytest

from jarvis.agents import claude_brain as cb
from jarvis.agents.brain import BrainError, BrainRequest
from jarvis.core.guardrails import GuardrailError, Guardrails

SCHEMA = {'type': 'object', 'properties': {'reply': {'type': 'string'}}, 'required': ['reply']}


def stream(*events):
    return b''.join(json.dumps(event, ensure_ascii=False).encode('utf-8') + b'\n' for event in events)


def delta(text):
    return {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                                              'delta': {'type': 'input_json_delta', 'partial_json': text}}}


def block(name):
    return {'type': 'stream_event', 'event': {'type': 'content_block_start',
                                              'content_block': {'type': 'tool_use', 'name': name, 'input': {}}}}


# Shapes recorded from one real call (claude 2.1.302, stream-json, --json-schema).
INIT = {'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': []}
SUCCESS = {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2, 'terminal_reason': 'completed',
           'result': '{"reply":"안녕"}', 'structured_output': {'reply': '안녕'}, 'api_error_status': None}


# --- limits ----------------------------------------------------------------------------------------------------

def test_argv_has_every_lock_no_mcp_and_never_bare():
    argv = cb.build_argv('claude.exe', 'sonnet', SCHEMA)
    value = lambda flag: argv[argv.index(flag) + 1]
    assert argv[:2] == ['claude.exe', '-p'] and '--bare' not in argv
    assert value('--tools') == '' and '--restricted' in argv and '--strict-mcp-config' in argv
    assert json.loads(value('--mcp-config')) == {'mcpServers': {}}
    assert not [part for part in argv if part.lower().startswith(('--allowedtools', '--allowed-tools'))]
    assert not [part for part in argv if 'mcp__' in part]
    assert value('--setting-sources') == '' and '--no-session-persistence' in argv
    assert value('--permission-prompts') == 'none' and value('--max-turns') == str(cb.MAX_TURNS)
    assert value('--output-format') == 'stream-json' and '--verbose' in argv
    assert value('--model') == 'sonnet' and json.loads(value('--json-schema')) == SCHEMA


def test_the_turn_cap_fits_one_structured_answer():
    # The recorded run: StructuredOutput is the only tool and a plain answer ends in num_turns 2.
    assert INIT['tools'] == ['StructuredOutput'] and cb.MAX_TURNS == SUCCESS['num_turns'] == 2


def test_a_schema_too_large_for_the_windows_command_line_is_refused():
    big = {'type': 'object', 'description': 'x' * 40000}
    with pytest.raises(BrainError) as caught:
        cb.build_argv('claude.exe', 'sonnet', big)
    assert caught.value.code == 'too_large'


def test_child_env_drops_api_keys_and_cloud_routing_so_the_subscription_is_used():
    env = cb.child_env({'ANTHROPIC_API_KEY': 'k', 'anthropic_auth_token': 't', 'PATH': 'p', 'USERPROFILE': 'u',
                        'CLAUDE_CODE_USE_BEDROCK': '1', 'CLAUDE_CODE_USE_VERTEX': '1', 'claude_code_use_foundry': '1'})
    assert env == {'PATH': 'p', 'USERPROFILE': 'u'}


# --- prompt and stream ----------------------------------------------------------------------------------------

def test_prompt_puts_system_text_in_instructions_and_ends_with_the_user():
    text = cb.render_prompt([{'role': 'system', 'content': '규칙'}, {'role': 'user', 'content': '안녕'},
                             {'role': 'assistant', 'content': '네'}, {'role': 'user', 'content': '날씨'}])
    assert text.index('규칙') < text.index('<conversation>') < text.index('[user]\n안녕') < text.index('[user]\n날씨')
    assert text.endswith('Answer the last [user] message.')


@pytest.mark.parametrize('partial, expected', [
    ('', None), ('{"tool": "none", "reply": "x"', None), ('{"reply": "', ''), ('{"reply": "안녕', '안녕'),
    ('{"reply": "a\\', 'a'), ('{"reply": "a\\n', 'a\n'), ('{"reply": "a\\u00', 'a'), ('{"reply": "a\\u00e9', 'aé'),
    ('{"reply": "\\ud83d', ''), ('{"reply": "\\ud83d\\ude00', '😀'), ('{"reply": "done", "tool": "no', 'done'),
])
def test_reply_text_decodes_only_complete_characters(partial, expected):
    assert cb.reply_text(partial) == expected


def test_parser_streams_the_reply_of_the_structured_output_only():
    seen = []
    parser = cb.StreamParser(seen.append)
    for event in [INIT, block('mcp__jarvis__search_public_web'), delta('{"reply": "not this"}'),
                  block('StructuredOutput'), delta(''), delta('{"reply": "안'), delta('녕"'), delta(', "tool": "none"}'),
                  {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}}, SUCCESS]:
        parser.feed(json.dumps(event, ensure_ascii=False))
    assert seen == ['안', '녕'] and parser.answer() == {'reply': '안녕'}


def test_parser_ignores_a_failing_delta_callback():
    parser = cb.StreamParser(lambda text: 1 / 0)
    for event in [block('StructuredOutput'), delta('{"reply": "a"}'), SUCCESS]:
        parser.feed(json.dumps(event))
    assert parser.answer() == {'reply': '안녕'}


@pytest.mark.parametrize('result, stderr, code', [
    ({**SUCCESS, 'subtype': 'error_max_turns', 'is_error': True, 'terminal_reason': 'max_turns'}, '', 'max_turns'),
    ({**SUCCESS, 'subtype': 'success', 'is_error': True, 'result': 'Not logged in · Please run /login'}, '', 'auth'),
    ({**SUCCESS, 'is_error': True, 'result': 'Invalid API key', 'api_error_status': 401}, '', 'auth'),
    ({**SUCCESS, 'is_error': True, 'result': "You've hit your usage limit", 'api_error_status': 429}, '', 'rate_limited'),
    ({**SUCCESS, 'is_error': True, 'subtype': 'error_during_execution', 'result': 'boom'}, '', 'unavailable'),
    ({**SUCCESS, 'structured_output': None, 'result': 'plain text'}, '', 'invalid_response'),
    ({**SUCCESS, 'structured_output': ['not', 'an', 'object']}, '', 'invalid_response'),
    (None, 'Error: Please run /login to authenticate', 'auth'),
    (None, 'Claude usage limit reached', 'rate_limited'),
    (None, 'crashed', 'unavailable'),
])
def test_errors_map_to_contract_codes_with_short_korean_text(result, stderr, code):
    parser = cb.StreamParser()
    if result is not None:
        parser.feed(json.dumps(result))
    with pytest.raises(BrainError) as caught:
        parser.answer(stderr)
    assert caught.value.code == code
    assert 'login' not in str(caught.value).lower().replace('claude auth login', '') and 'boom' not in str(caught.value)


def test_rejected_rate_limit_event_explains_a_failed_run():
    parser = cb.StreamParser()
    parser.feed(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}}))
    parser.feed(json.dumps({**SUCCESS, 'is_error': True, 'subtype': 'error_during_execution', 'result': 'x'}))
    with pytest.raises(BrainError) as caught:
        parser.answer()
    assert caught.value.code == 'rate_limited'


@pytest.mark.parametrize('line', ['not json', '[1, 2]'])
def test_non_event_lines_are_invalid(line):
    with pytest.raises(BrainError) as caught:
        cb.StreamParser().feed(line)
    assert caught.value.code == 'invalid_response'


# --- availability ---------------------------------------------------------------------------------------------

class Done:
    def __init__(self, data):
        self.stdout = data if isinstance(data, bytes) else json.dumps(data).encode()


def provider(tmp_path, *, discover=lambda: 'C:/claude.exe', status=None, spawn=None, clock=time.monotonic):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if isinstance(status, Exception):
            raise status
        return Done(status if status is not None else {'loggedIn': True, 'authMethod': 'claude.ai'})
    item = cb.ClaudeCliProvider(Guardrails(tmp_path), discover=discover, run=run, spawn=spawn, clock=clock,
                                kill=lambda process: process.kill())
    return item, calls


def test_check_reports_a_subscription_login_as_available(tmp_path):
    item, calls = provider(tmp_path)
    info = item.check('sonnet')
    assert info.available and info.reason is None and info.external and info.label == 'Claude (구독) · sonnet'
    assert info.capabilities.json_schema and info.capabilities.streaming
    assert calls == [['C:/claude.exe', 'auth', 'status', '--json']]
    assert [model.model_id for model in item.models()] == list(cb.MODELS) and len(calls) == 1   # cached


@pytest.mark.parametrize('kwargs, reason', [
    ({'discover': lambda: None}, cb.NOT_INSTALLED),
    ({'status': {'loggedIn': False}}, cb.NOT_LOGGED_IN),
    ({'status': {'loggedIn': True, 'authMethod': 'api_key'}}, cb.NOT_SUBSCRIPTION),
    ({'status': b'garbage'}, cb.STATUS_FAILED),
    ({'status': subprocess.TimeoutExpired('claude', 10)}, cb.STATUS_FAILED),
])
def test_check_explains_why_claude_cannot_be_used(tmp_path, kwargs, reason):
    item, _ = provider(tmp_path, **kwargs)
    info = item.check('opus')
    assert not info.available and info.reason == reason


def test_unknown_models_are_unavailable(tmp_path):
    item, _ = provider(tmp_path)
    assert not item.check('gpt-9').available
    with pytest.raises(BrainError):
        item.infer('gpt-9', BrainRequest([], SCHEMA, time.monotonic() + 5))


def test_a_failed_status_check_is_kept_briefly_then_retried(tmp_path):
    now = [100.0]
    item, calls = provider(tmp_path, status=b'garbage', clock=lambda: now[0])
    assert [info.reason for info in item.models()] == [cb.STATUS_FAILED] * len(cb.MODELS)
    assert len(calls) == 1                                   # one status check per listing, even when it fails
    now[0] += cb.AUTH_FAILURE_TTL + 0.1
    item.check('opus')
    assert len(calls) == 2


# --- inference ------------------------------------------------------------------------------------------------

class Process:
    def __init__(self, stdout=b'', stderr=b'', hang=False):
        self.stdin, self.pid, self.killed, self.hang = io.BytesIO(), 0, False, hang
        self.stdin.close = lambda: None
        self.stdout = Blocking(stdout, hang, self)
        self.stderr = io.BytesIO(stderr)

    def poll(self):
        return 0 if self.killed or not self.hang else None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self.stdout.release.set()


class Blocking(io.BytesIO):
    """stdout that, when hang is set, blocks after its data until the process is killed."""

    def __init__(self, data, hang, process):
        super().__init__(data)
        self.hang, self.release = hang, threading.Event()

    def readline(self, size=-1):
        line = super().readline(size)
        if not line and self.hang:
            self.release.wait(5)
        return line


def test_infer_streams_the_reply_and_returns_the_structured_answer(tmp_path):
    spawned, seen = [], []
    output = stream(INIT, block('StructuredOutput'), delta('{"reply": "안'), delta('녕"}'), SUCCESS)

    def spawn(argv):
        spawned.append(Process(output))
        spawned[-1].argv = argv
        return spawned[-1]
    item, _ = provider(tmp_path, spawn=spawn)
    request = BrainRequest([{'role': 'user', 'content': '인사해'}], SCHEMA, time.monotonic() + 10, seen.append)
    assert item.infer('sonnet', request) == {'reply': '안녕'}
    assert seen == ['안', '녕'] and spawned[0].killed
    assert '인사해' in spawned[0].stdin.getvalue().decode('utf-8')
    assert '--json-schema' in spawned[0].argv and '인사해' not in ' '.join(spawned[0].argv)   # prompt via stdin only


def test_infer_times_out_and_kills_the_process(tmp_path):
    process = Process(stream(INIT), hang=True)
    item, _ = provider(tmp_path, spawn=lambda argv: process)
    with pytest.raises(BrainError) as caught:
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x'}], SCHEMA, time.monotonic() + 0.3))
    assert caught.value.code == 'timeout' and process.killed


def test_infer_stops_on_the_stop_marker(tmp_path):
    process = Process(stream(INIT), hang=True)
    item, _ = provider(tmp_path, spawn=lambda argv: process)
    threading.Timer(0.2, lambda: (tmp_path / '.jarvis-stop').write_text('stop')).start()
    with pytest.raises(GuardrailError):
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x'}], SCHEMA, time.monotonic() + 5))
    assert process.killed


def test_infer_caps_the_output(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, 'MAX_OUTPUT', 200)
    process = Process(stream(INIT, *[delta('x' * 50)] * 10, SUCCESS))
    item, _ = provider(tmp_path, spawn=lambda argv: process)
    with pytest.raises(BrainError) as caught:
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x'}], SCHEMA, time.monotonic() + 5))
    assert caught.value.code == 'too_large' and process.killed


def test_infer_refuses_a_context_over_64_kb_before_starting(tmp_path):
    item, _ = provider(tmp_path, spawn=lambda argv: pytest.fail('must not start'))
    with pytest.raises(BrainError) as caught:
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x' * 70000}], SCHEMA, time.monotonic() + 5))
    assert caught.value.code == 'too_large'


@pytest.mark.parametrize('kwargs', [{'discover': lambda: None}, {'spawn': lambda argv: (_ for _ in ()).throw(OSError())}])
def test_infer_reports_a_missing_cli(tmp_path, kwargs):
    item, _ = provider(tmp_path, **kwargs)
    with pytest.raises(BrainError) as caught:
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x'}], SCHEMA, time.monotonic() + 5))
    assert caught.value.code == 'unavailable' and str(caught.value) == cb.NOT_INSTALLED


def test_infer_maps_a_login_failure_from_stderr(tmp_path):
    process = Process(b'', b'Invalid API key - Please run /login')
    item, _ = provider(tmp_path, spawn=lambda argv: process)
    with pytest.raises(BrainError) as caught:
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x'}], SCHEMA, time.monotonic() + 5))
    assert caught.value.code == 'auth' and str(caught.value) == cb.NOT_LOGGED_IN


def test_an_unrecognised_non_zero_exit_is_visible_with_a_clean_stderr_line(tmp_path):
    stderr = 'starting\nFatal: crash in C:\\Users\\me\\x.js\x1b[0m\n'
    process = Process(b'', stderr.encode())
    process.wait = lambda timeout=None: 3
    item, _ = provider(tmp_path, spawn=lambda argv: process)
    with pytest.raises(BrainError) as caught:
        item.infer('sonnet', BrainRequest([{'role': 'user', 'content': 'x'}], SCHEMA, time.monotonic() + 5))
    text = str(caught.value)
    assert caught.value.code == 'unavailable' and text.startswith(cb.FAILED) and '종료 코드 3' in text
    assert 'Fatal: crash in <path>' in text and 'Users' not in text
    assert '\x1b' not in text and 'starting' not in text


def test_a_secret_in_stderr_never_reaches_the_message():
    summary = cb.stderr_summary('Error token=sk-ant-abcdefghijklmnopqrstuvwxyz0123')
    assert 'sk-ant' not in summary and summary


def test_stderr_summary_is_short():
    assert cb.stderr_summary('') == '' and cb.stderr_summary('\n  \n') == ''
    assert len(cb.stderr_summary('e' * 500)) == cb.SUMMARY_CHARS


def test_warm_up_does_nothing(tmp_path):
    item, calls = provider(tmp_path, spawn=lambda argv: pytest.fail('must not start'))
    assert item.warm_up('sonnet') is None and calls == []
