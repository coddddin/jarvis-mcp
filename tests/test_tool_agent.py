"""Board 204bc125 part 3: the generic tool task loop over pinned MCP tools, and its chat wiring."""
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from jarvis.agents import brain
from jarvis.agents.brain import Capabilities, ModelInfo
from jarvis.app.chat import ChatService, ToolProgress
from jarvis.core.guardrails import Guardrails
from jarvis.features.mcp_client.service import McpConfig, McpError, McpSettings, ServerConfig, ToolPin, discover
from jarvis.features.tool_agent import service as tool_agent
from jarvis.features.tool_agent.service import ToolAgent, argument_problem, named_servers

PATHS = str(Path(__file__).parent / 'fixtures' / 'mcp_path_server.py')
HASH = 'a' * 64
SEARCH = {'name': 'mcp:demo/search', 'description': 'Search strings in the open binary.', 'family': 'local',
          'effect': 'read', 'input_schema': {'type': 'object', 'properties': {
              'query': {'type': 'string'}, 'limit': {'type': 'integer'}, 'exact': {'type': 'boolean'}},
              'required': ['query']}}
BROKEN = {**SEARCH, 'name': 'mcp:demo/broken', 'description': 'Always fails.'}


def step(tool, arguments=None, answer='', notes=''):
    return {'tool': tool, 'arguments': arguments or {}, 'answer': answer, 'notes': notes}


class Script:
    """A brain that answers from a list and records each prompt and schema it was given."""

    def __init__(self, *steps):
        self.steps, self.prompts, self.schemas = list(steps), [], []

    def __call__(self, messages, schema, deadline):
        self.prompts.append(messages)
        self.schemas.append(schema)
        return self.steps.pop(0) if self.steps else step('answer', answer='끝')

    def enums(self):
        return [schema['properties']['tool']['enum'] for schema in self.schemas]


class FakeSession:
    def __init__(self, guard, close_on=None):
        self.guard, self.process, self.calls, self.close_on = guard, object(), [], close_on

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == self.close_on:
            self.process = None
            raise McpError('The MCP server did not answer in time; the session was stopped.')
        if name.endswith('broken'):
            raise McpError('The MCP tool failed: boom')
        return {'text': 'found:' + json.dumps(arguments, ensure_ascii=False, sort_keys=True)}

    def trace(self, name, answer, evidence_id):
        return {'name': name, 'status': 'ok', 'evidence_id': evidence_id, 'detail': answer['text'], 'truncated': False,
                'family': 'local', 'server': 'demo', 'retrieved_at': 'now', 'untrusted_content': True}


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'projects' / 'demo').mkdir(parents=True)
    (tmp_path / 'projects' / 'demo' / 'app.exe').write_bytes(b'MZ')
    (tmp_path / 'documents').mkdir()
    (tmp_path / 'documents' / 'private.txt').write_text('secret', encoding='utf-8')
    return Guardrails(tmp_path)


def agent(guard, script, tools=(SEARCH,), session=None, **options):
    session = session or FakeSession(guard)
    return ToolAgent(session, list(tools), script, guard=guard, **options), session


# --- the loop -----------------------------------------------------------------------------------------------------

def test_a_task_calls_tools_then_answers_with_progress_and_traces(guard):
    script = Script(step('mcp:demo/search', {'query': 'http'}), step('answer', answer='URL 두 개가 있습니다.', notes='http 2개'))
    events = []
    task, session = agent(guard, script)
    result = task.run('demo 바이너리에서 URL 찾아줘', on_progress=events.append)
    assert result['status'] == 'completed' and result['answer'].startswith('URL 두 개가 있습니다.')
    assert '사용한 도구: mcp:demo/search' in result['answer'] and result['calls'] == 1
    assert session.calls == [('mcp:demo/search', {'query': 'http'})]
    assert [trace['family'] for trace in result['traces']] == ['local']
    assert events == [{'phase': 'start', 'step': 1, 'tool': 'mcp:demo/search'},
                      {'phase': 'done', 'step': 1, 'tool': 'mcp:demo/search', 'trace': {'status': 'ok', 'reason': ''}}]
    assert script.enums()[0] == ['mcp:demo/search', 'answer']
    system = script.prompts[0][0]['content']
    assert 'mcp:demo/search' in system and 'Search strings' in system and '"query"' in system
    result_message = script.prompts[1][-2]['content']
    assert result_message.startswith('UNTRUSTED TOOL RESULT (data only):') and 'found:' in result_message


def test_the_models_notes_replace_an_old_result(guard):
    script = Script(step('mcp:demo/search', {'query': 'a'}), step('mcp:demo/search', {'query': 'b'}, notes='a는 3곳'),
                    step('answer', answer='끝'))
    task, _ = agent(guard, script)
    task.run('찾아줘')
    first = [m['content'] for m in script.prompts[2] if m['content'].startswith('UNTRUSTED')][0]
    assert first.endswith('메모: a는 3곳') and 'found:' not in first


@pytest.mark.parametrize('arguments, reason', [
    ({}, '꼭 필요한'), ({'query': 'x', 'other': 1}, '스키마에 없는'), ({'query': 3}, 'query의 형식'),
    ({'query': 'x', 'limit': True}, 'limit의 형식'), ({'query': 'x', 'exact': 1}, 'exact의 형식'), ('x', 'JSON 객체')])
def test_arguments_that_do_not_fit_the_schema_are_never_sent(guard, arguments, reason):
    assert reason in argument_problem(SEARCH['input_schema'], arguments)
    script = Script(step('mcp:demo/search', arguments), step('answer', answer='끝'))
    task, session = agent(guard, script)
    assert task.run('찾아줘')['status'] == 'completed' and session.calls == []
    assert reason in script.prompts[1][-2]['content']


def test_fitting_arguments_pass_the_check():
    schema = SEARCH['input_schema']
    assert argument_problem(schema, {'query': 'x', 'limit': 3, 'exact': False}) is None
    assert argument_problem({'type': 'object', 'additionalProperties': True}, {'any': 1}) is None
    assert argument_problem({'type': 'object', 'properties': {'n': {'type': ['number', 'null']}}}, {'n': None}) is None


def test_earlier_choices_keep_only_tool_and_arguments(guard):
    script = Script(step('mcp:demo/search', {'query': 'a'}, answer='x' * 3000, notes='n' * 300), step('answer', answer='끝'))
    task, _ = agent(guard, script)
    task.run('찾아줘')
    choice = [m for m in script.prompts[1] if m['role'] == 'assistant'][0]['content']
    assert json.loads(choice) == {'tool': 'mcp:demo/search', 'arguments': {'query': 'a'}}


def test_the_same_call_is_not_made_twice(guard):
    script = Script(step('mcp:demo/search', {'query': 'a', 'limit': 2}), step('mcp:demo/search', {'limit': 2, 'query': 'a'}),
                    step('answer', answer='끝'))
    task, session = agent(guard, script)
    task.run('찾아줘')
    assert len(session.calls) == 1 and '이미 같은 인자로' in script.prompts[2][-2]['content']


def test_a_tool_not_offered_is_an_invalid_choice_and_invalid_choices_force_the_answer(guard):
    script = Script(*[step('mcp:demo/save') for _ in range(3)], step('answer', answer='못 했습니다.'))
    task, session = agent(guard, script)
    result = task.run('저장해줘')
    assert session.calls == [] and result['status'] == 'completed'
    assert script.enums()[3] == ['answer']                  # after MAX_INVALID wrong picks only answer is left


def test_the_call_cap_forces_the_answer(guard):
    script = Script(*[step('mcp:demo/search', {'query': str(n)}) for n in range(5)])
    task, session = agent(guard, script, limits={'calls': 2})
    result = task.run('찾아줘')
    assert len(session.calls) == 2 and script.enums()[2] == ['answer'] and result['status'] == 'completed'


def test_an_empty_answer_twice_fails(guard):
    task, _ = agent(guard, Script(step('answer'), step('answer'), step('answer')))
    result = task.run('찾아줘')
    assert result['status'] == 'failed' and '답을 쓰지' in result['answer']


def test_the_stop_flag_ends_the_task_before_the_next_step(guard):
    stop = []
    script = Script(step('mcp:demo/search', {'query': 'a'}), step('mcp:demo/search', {'query': 'b'}))
    task, session = agent(guard, script, should_stop=lambda: bool(stop))
    result = task.run('찾아줘', on_progress=lambda event: stop.append(1) if event['phase'] == 'done' else None)
    assert result['status'] == 'stopped' and '멈췄' in result['answer'] and len(session.calls) == 1


def test_the_time_limit_fails_the_task(guard):
    now = [0.0]
    script = Script(step('mcp:demo/search', {'query': 'a'}))

    def decide(messages, schema, deadline):
        now[0] += 700
        return script(messages, schema, deadline)
    task, _ = agent(guard, decide, clock=lambda: now[0])
    result = task.run('찾아줘')
    assert result['status'] == 'failed' and '시간 한도' in result['answer']


def test_a_failed_tool_is_reported_and_the_loop_goes_on(guard):
    script = Script(step('mcp:demo/broken', {'query': 'a'}), step('answer', answer='실패를 알립니다.'))
    events = []
    task, _ = agent(guard, script, tools=(SEARCH, BROKEN))
    result = task.run('찾아줘', on_progress=events.append)
    assert result['status'] == 'completed' and result['traces'][0]['status'] == 'error'
    assert result['traces'][0]['family'] == 'local' and 'boom' in result['traces'][0]['detail']
    assert events[-1]['trace']['status'] == 'failed' and 'boom' in events[-1]['trace']['reason']
    assert '도구 실패' in script.prompts[1][-2]['content']


def test_a_session_that_stopped_ends_the_task(guard):
    session = FakeSession(guard, close_on='mcp:demo/search')
    task, _ = agent(guard, Script(step('mcp:demo/search', {'query': 'a'})), session=session)
    result = task.run('찾아줘')
    assert result['status'] == 'failed' and '도구 서버가 멈춰' in result['answer']


def test_a_model_failure_fails_the_task(guard):
    def decide(*a):
        raise TimeoutError('model timed out')
    task, session = agent(guard, decide)
    result = task.run('찾아줘')
    assert result['status'] == 'failed' and 'model timed out' in result['answer'] and session.calls == []


# --- with a real MCP server -------------------------------------------------------------------------------------

def approved(guard, choices, roots=('projects',), label='Paths'):
    settings = McpSettings(guard)
    settings.add_server('paths', label, sys.executable, ['-I', PATHS])
    found = discover(guard, settings.load(), 'paths')
    settings.approve('paths', found['tools'], choices)
    if roots:
        settings.set_roots('paths', roots)
    return settings.load()


CHOICES = {'open_binary': {'effect': 'read'}, 'look': {'effect': 'read', 'family': 'public_web'},
           'save_report': {'effect': 'confirm', 'path_args': [{'name': 'output_path', 'mode': 'write'}]}}


def test_a_real_server_task_checks_paths_and_offers_only_read_pins(guard):
    config = approved(guard, CHOICES)
    script = Script(step('mcp:paths/open_binary', {'path': 'documents/private.txt'}),
                    step('mcp:paths/open_binary', {'path': 'projects/demo/app.exe'}),
                    step('answer', answer='열었습니다.'))
    result = tool_agent.run_task(guard, 'Paths로 app.exe 열어줘', script, named_servers(config, 'Paths로'),
                                 external=False)
    assert script.enums()[0] == ['mcp:paths/open_binary', 'mcp:paths/look', 'answer']      # confirm pin not offered
    first, second = result['traces']
    assert first['status'] == 'error' and '승인된 폴더 밖' in first['detail']
    assert second['status'] == 'ok' and second['detail'] == 'open:' + str(guard.workspace / 'projects/demo/app.exe')
    assert result['status'] == 'completed'


def test_an_external_brain_is_offered_only_public_web_tools_of_a_real_server(guard):
    config = approved(guard, CHOICES)
    script = Script(step('mcp:paths/open_binary', {'path': 'projects/demo/app.exe'}), step('answer', answer='끝'))
    result = tool_agent.run_task(guard, 'Paths', script, config.servers, external=True)
    assert script.enums()[0] == ['mcp:paths/look', 'answer'] and result['traces'] == []
    assert 'open_binary' not in script.prompts[0][0]['content']
    assert '로컬 도구를 보여 주지 않아 쓰지 않았습니다: mcp:paths/open_binary' in result['answer']
    local = tool_agent.run_task(guard, 'Paths', Script(step('answer', answer='끝')), config.servers, external=False)
    assert '보여 주지 않아' not in local['answer']


def test_without_an_offerable_tool_the_task_says_why(guard):
    config = approved(guard, {'open_binary': {'effect': 'read'}})
    with pytest.raises(tool_agent.ToolSetupRequired, match='승인된 도구가 없습니다'):
        tool_agent.run_task(guard, 'Paths', Script(), config.servers, external=True)


# --- which servers a request names ------------------------------------------------------------------------------

def server(server_id='rea', label='REA', effect='read', family='local', args=()):
    return ServerConfig(id=server_id, label=label, command=sys.executable, args=args, state='approved',
                        tools=(ToolPin(name='open_binary', family=family, effect=effect, schema_sha256=HASH),))


@pytest.mark.parametrize('message, names', [
    ('REA로 app.exe 분석해줘', ['rea']), ('rea open this', ['rea']), ('리버스 엔지니어링 도구 REA 써서 봐줘', ['rea']),
    ('area 51 찾아줘', []), ('REAL 값', []), ('그냥 분석해줘', []), ('Ghidra Tools로 열어줘', ['ghidra'])])
def test_only_an_explicitly_named_server_starts_a_tool_task(message, names):
    config = McpConfig(servers=(server(), server('ghidra', 'Ghidra Tools')))
    assert [item.id for item in named_servers(config, message)] == names


@pytest.mark.parametrize('server_id, label, message', [
    ('web', 'Web', 'web에서 날씨 찾아줘'), ('time', 'Time', 'what time is it'), ('fetch', 'Fetch', 'fetch 이 페이지'),
    ('git', 'Git', 'git log 보여줘'), ('ab', 'AB', 'ab 테스트 결과')])
def test_generic_or_short_names_never_start_a_tool_task(server_id, label, message):
    assert named_servers(McpConfig(servers=(server(server_id, label),)), message) == []
    distinct = server(server_id, 'Ghidra Tools')            # a distinctive label still works
    assert named_servers(McpConfig(servers=(distinct,)), 'Ghidra Tools로 열어줘') == [distinct]


def test_playwright_confirm_only_and_pending_servers_are_not_tool_task_servers():
    playwright = ServerConfig(id='pw', label='Playwright', command=sys.executable,
                              args=('C:/x/@playwright/mcp/cli.js', '--no-webmcp', '--isolated', '--headless'),
                              state='approved', tools=(ToolPin(name='browser_navigate', effect='read',
                                                               family='public_web', schema_sha256=HASH),))
    pending = server('pend', 'Pend').model_copy(update={'state': 'pending'})
    config = McpConfig(servers=(playwright, server('conf', 'Conf', effect='confirm'), pending))
    assert named_servers(config, 'Playwright Conf Pend 써줘') == []


# --- chat wiring ------------------------------------------------------------------------------------------------

class External:
    provider_id, label, external = 'claude-cli', 'Claude (구독)', True

    def __init__(self):
        self.requests = []

    def _info(self, model_id):
        return ModelInfo(self.provider_id, model_id, 'Claude', True, Capabilities(True, True, False), True, None)

    def models(self):
        return [self._info('sonnet')]

    def check(self, model_id):
        return self._info(model_id)

    def infer(self, model_id, request):
        self.requests.append(request)
        return step('answer', answer='외부 답')

    def warm_up(self, model_id):
        pass


def chat_service(tmp_path, monkeypatch, family='local', external=False):
    McpSettings(Guardrails(tmp_path)).save(McpConfig(servers=(server('lookup', 'Lookup', family=family),)))
    outside = External()
    service = ChatService(Guardrails(tmp_path), brains=[outside])
    monkeypatch.setattr(service, '_infer', lambda *a, **k: step('answer', answer='로컬 답'))
    if external:
        service.brain.select('claude-cli', 'sonnet', confirm_external=True)
    return service, outside


def send(service, message):
    identity = service.store.create()['id']
    return service.send(identity, uuid4().hex, message)['turns'][-1]


def test_chat_runs_a_tool_task_when_the_request_names_a_server(tmp_path, monkeypatch):
    service, _ = chat_service(tmp_path, monkeypatch)
    seen, progress = {}, []
    monkeypatch.setattr(service.store, 'task_progress', lambda identity, request, value: progress.append(value))

    def run_task(guard, request, decide, servers, *, external, on_progress, should_stop):
        seen.update(servers=[item.id for item in servers], external=external, decision=decide([], {}, 0))
        on_progress({'phase': 'start', 'step': 1, 'tool': 'mcp:lookup/open_binary'})
        on_progress({'phase': 'done', 'step': 1, 'tool': 'mcp:lookup/open_binary',
                     'trace': {'status': 'failed', 'reason': '승인된 폴더 밖의 경로입니다.'}})
        assert not should_stop()
        return {'status': 'completed', 'reason': '', 'answer': '분석 결과입니다.', 'calls': 1,
                'traces': [{'name': 'mcp:lookup/open_binary', 'status': 'ok', 'evidence_id': 't1', 'detail': 'MZ',
                            'family': 'local', 'retrieved_at': 'now', 'untrusted_content': True}]}
    monkeypatch.setattr(tool_agent, 'run_task', run_task)
    turn = send(service, 'Lookup으로 app.exe 열어줘')
    assert seen == {'servers': ['lookup'], 'external': False, 'decision': step('answer', answer='로컬 답')}
    assert turn['status'] == 'completed' and turn['assistant'] == '분석 결과입니다.'
    assert progress[0] == {'kind': 'tool', 'phase': 'call', 'line': '도구 실행 중: lookup open_binary', 'step': 1,
                           'step_limit': tool_agent.MAX_CALLS, 'tool': 'mcp:lookup/open_binary',
                           'recent': ['도구 실행 중: lookup open_binary']}
    assert progress[-1]['phase'] == 'failed' and progress[-1]['line'] == '실패: 승인된 폴더 밖의 경로입니다.'
    assert turn['tools'][0]['family'] == 'local' and not ChatService._external_safe(turn)


def test_an_external_brain_with_only_local_tools_hands_the_task_to_the_local_model(tmp_path, monkeypatch):
    service, outside = chat_service(tmp_path, monkeypatch, external=True)
    seen = {}

    def run_task(guard, request, decide, servers, *, external, on_progress, should_stop):
        seen.update(external=external, decision=decide([], {}, 0))
        return {'status': 'completed', 'reason': '', 'answer': '로컬로 답했습니다.', 'calls': 0, 'traces': []}
    monkeypatch.setattr(tool_agent, 'run_task', run_task)
    turn = send(service, 'Lookup으로 app.exe 열어줘')
    assert seen == {'external': False, 'decision': step('answer', answer='로컬 답')} and outside.requests == []
    assert turn['model']['external'] is False and '로컬 도구' in turn['model']['local_reason']
    assert brain.local_only_source('REA로 열어줘') == '프로그램 분석'     # REA by name is local already


def test_an_external_brain_keeps_the_task_when_the_server_has_public_web_tools(tmp_path, monkeypatch):
    service, outside = chat_service(tmp_path, monkeypatch, family='public_web', external=True)
    seen = {}

    def run_task(guard, request, decide, servers, *, external, on_progress, should_stop):
        seen.update(external=external, decision=decide([], {}, 0))
        return {'status': 'completed', 'reason': '', 'answer': '외부로 답했습니다.', 'calls': 0, 'traces': []}
    monkeypatch.setattr(tool_agent, 'run_task', run_task)
    turn = send(service, 'Lookup으로 검색해줘')
    assert seen['external'] is True and len(outside.requests) == 1 and turn['model']['external'] is True


def test_the_chat_stop_button_stops_a_tool_task_and_setup_problems_are_replies(tmp_path, monkeypatch):
    service, _ = chat_service(tmp_path, monkeypatch)

    def run_task(guard, request, decide, servers, *, external, on_progress, should_stop):
        assert service.stop_web() == {'stopping': True} and should_stop()
        return {'status': 'stopped', 'reason': '사용자가 작업을 멈췄습니다.', 'answer': '사용자가 작업을 멈췄습니다.',
                'calls': 0, 'traces': []}
    monkeypatch.setattr(tool_agent, 'run_task', run_task)
    turn = send(service, 'Lookup으로 열어줘')
    assert turn['status'] == 'failed' and '멈췄' in turn['assistant'] and not service._web_stop.is_set()

    def setup(*a, **k):
        raise tool_agent.ToolSetupRequired('Lookup 도구가 바뀌어 HUD에서 다시 승인해야 합니다.')
    monkeypatch.setattr(tool_agent, 'run_task', setup)
    turn = send(service, 'Lookup으로 열어줘')
    assert turn['status'] == 'completed' and '다시 승인' in turn['assistant']


def test_a_request_naming_no_server_keeps_the_normal_chat_path(tmp_path, monkeypatch):
    service, _ = chat_service(tmp_path, monkeypatch)
    monkeypatch.setattr(tool_agent, 'run_task', lambda *a, **k: pytest.fail('no tool task'))
    monkeypatch.setattr(service, '_worker', lambda *a: __import__('jarvis.app.chat', fromlist=['x']).ChatDecision(
        reply='안녕하세요.', tool='none', argument=''))
    assert send(service, '안녕 자비스')['assistant'] == '안녕하세요.'


def test_tool_progress_stays_under_the_store_limit():
    from jarvis.agents.chat import ChatStore
    tracker, value = ToolProgress(), None
    for index in range(30):
        value = tracker.update({'phase': 'start', 'step': index, 'tool': 'mcp:' + 'x' * 200}, '도구 실행 중: ' + '가' * 300)
    assert len(json.dumps(value, ensure_ascii=False).encode()) <= ToolProgress.BYTES < ChatStore.PROGRESS_BYTES
    assert value['step'] == tool_agent.MAX_CALLS and len(value['tool']) == 120


@pytest.fixture(autouse=True)
def no_attached_families():
    yield
    brain.set_attached_families({})
