"""OpenRig-derived routing, durable ownership and local-lead hybrid execution."""
import itertools
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest

from jarvis.agents.chat import ChatError, ChatStore
from jarvis.agents.team import HybridTeam, WorkResult, _discover_claude
from jarvis.agents.team_routing import select_role_seat
from jarvis.core.guardrails import GuardrailError, Guardrails


def candidate(name, *, role='coder', runtime='local', pending=0, state='ready'):
    return {'address': name, 'session': name, 'role': role, 'runtime': runtime,
            'pending': pending, 'state': state, 'node_kind': 'agent'}


def test_routing_deterministic_by_backlog_then_address_for_every_input_order():
    seats = [candidate('driver2@rig'), candidate('driver10@rig'), candidate('idle@rig', pending=1)]
    for order in itertools.permutations(seats):
        result = select_role_seat('coder', order)
        assert result['seat'] == 'driver10@rig'
        assert result['qualified'] == ['driver10@rig', 'driver2@rig', 'idle@rig']


def test_routing_requires_role_liveness_runtime_and_stable_identity():
    seats = [candidate('wrong', role='researcher'), candidate('offline', state='unavailable'),
             candidate('external', runtime='codex'), {**candidate('changed'), 'session': 'another'},
             {**candidate('infra'), 'node_kind': 'infrastructure'}]
    result = select_role_seat('coder', seats, runtime='local')
    assert result['seat'] is None
    assert {item['reason'] for item in result['rejected']} == {
        'role_not_declared', 'not_live', 'runtime_mismatch', 'session_identity_mismatch'}


class FixtureInference:
    def __init__(self, plan=None, review_passed=True):
        self.calls = []
        self.plan = plan or {'summary': 'One local research packet, then Codex implementation.', 'assignments': [
            {'id': 'p1', 'role': 'researcher', 'runtime': 'local', 'objective': 'Define the edge cases.', 'depends_on': []},
            {'id': 'p2', 'role': 'coder', 'runtime': 'codex', 'objective': 'Write a complete solution.', 'depends_on': ['p1']}]}
        self.review_passed = review_passed

    def __call__(self, messages, schema, deadline):
        self.calls.append((messages, schema))
        title = schema['title']
        if title == 'TeamPlan':
            return self.plan
        if title == 'WorkResult':
            return {'summary': 'Edge cases identified.', 'output': 'Keep empty input and negative integers in scope.'}
        if title == 'ReviewResult':
            return {'passed': self.review_passed, 'summary': 'Model review only.',
                    'issues': [] if self.review_passed else ['Negative input still needs a defined contract.']}
        if title == 'RevisionPlan':
            return {'action': 'stop', 'assignment_ids': [], 'summary': 'Operator should clarify the contract.'}
        return {'reply': 'The implementation proposal handles the requested edge cases; execution remains unverified.'}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = ChatStore(Guardrails(tmp_path))
    infer = FixtureInference()
    team = HybridTeam(store, infer, codex=str(Path(__file__).resolve()), claude='missing-claude.exe')
    session = store.create()['id']
    request = uuid4().hex
    store.begin(session, request, 'Prepare a sum function proposal.', time.time() + 660, mode='team')
    return store, team, infer, session, request


def test_local_lead_dispatches_context_handoff_to_external_and_reviews(setup, monkeypatch):
    store, team, infer, session, request = setup
    external = []
    def specialist(runtime, context, folder, name, deadline):
        external.append((runtime, context))
        assert context['dependencies'][0]['id'] == 'p1'
        assert 'negative integers' in context['dependencies'][0]['output']
        return WorkResult(summary='Complete proposal.', output='def add(a, b):\n    return a + b')
    monkeypatch.setattr(team, '_external', specialist)
    answer, traces = team.run(session, request, 'Prepare a sum function proposal.')
    saved = ChatStore(store.guard).get(session)['turns'][0]['team']
    assert saved['lead'] == 'local' and saved['status'] == 'completed'
    assert saved['assignments'][1]['owner'] == 'codex-coder@jarvis'
    assert saved['review']['kind'] == 'model_review_only' and not saved['review']['tests_executed']
    assert external[0][0] == 'codex' and len(traces) == 3
    assert infer.calls[0][1]['title'] == 'TeamPlan' and infer.calls[-1][1]['title'] == 'LeadResult'
    assert 'lead planner' in infer.calls[0][0][0]['content']
    assert 'independent reviewer' in infer.calls[-2][0][0]['content']
    assert '코드 실행·파일 적용은 수행하지 않았습니다' in answer
    assert store.get(session)['reserved'] == 4 * 9216
    with pytest.raises(ChatError, match='replay'):
        team.run(session, request, 'Prepare a sum function proposal.')


@pytest.mark.parametrize('assignments', [
    [{'id': 'p1', 'role': 'coder', 'runtime': 'claude-code', 'objective': 'Work', 'depends_on': []}],
    [{'id': 'p1', 'role': 'coder', 'runtime': 'local', 'objective': 'Work', 'depends_on': ['p2']}],
    [{'id': 'p2', 'role': 'coder', 'runtime': 'local', 'objective': 'Work', 'depends_on': []}],
])
def test_unavailable_runtime_or_invalid_graph_fails_without_worker_execution(setup, assignments):
    store, team, infer, session, request = setup
    infer.plan = {'summary': 'Invalid dispatch.', 'assignments': assignments}
    with pytest.raises(ChatError):
        team.run(session, request, 'Prepare a sum function proposal.')
    assert len(infer.calls) == 1
    assert store.get(session)['turns'][0]['team']['status'] == 'failed'


def test_failed_review_is_never_reported_as_clean_completion(setup, monkeypatch):
    store, team, infer, session, request = setup
    infer.review_passed = False
    infer.plan['assignments'] = infer.plan['assignments'][:1]
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    assert answer.startswith('협업 중단: 로컬 리드의 사용자 확인 요청')
    assert '검수에서 미해결' in answer
    assert store.get(session)['turns'][0]['team']['status'] == 'needs_user_input'


def test_failed_external_packet_retains_completed_local_result(setup, monkeypatch):
    store, team, _, session, request = setup
    def fail(*args):
        raise ChatError('Provider unavailable')
    monkeypatch.setattr(team, '_external', fail)
    with pytest.raises(ChatError, match='worker failed'):
        team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert state['status'] == 'failed'
    assert [item['status'] for item in state['assignments']] == ['completed', 'failed']
    assert state['failure']['stage'] == 'working'
    assert state['failure']['completed_assignments'] == ['p1']
    assert 'Provider unavailable' not in json.dumps(state['failure'])


def test_independent_local_and_external_packets_can_run_concurrently(setup, monkeypatch):
    store, team, infer, session, request = setup
    infer.plan['assignments'][1]['depends_on'] = []
    rendezvous = threading.Barrier(2)
    original = infer.__call__
    def local(messages, schema, deadline):
        if schema['title'] == 'WorkResult':
            rendezvous.wait(timeout=3)
        return original(messages, schema, deadline)
    team.infer = local
    def external(*args):
        rendezvous.wait(timeout=3)
        return WorkResult(summary='Parallel specialist.', output='A complete independent specialist proposal.')
    monkeypatch.setattr(team, '_external', external)
    team.run(session, request, 'Prepare a sum function proposal.')
    assert store.get(session)['turns'][0]['team']['status'] == 'completed'


def test_local_only_cannot_route_to_external_even_when_installed(setup):
    store, team, _, session, _ = setup
    request = uuid4().hex
    with store.connection() as db:
        db.execute('UPDATE turns SET status="failed"')
    store.begin(session, request, 'Prepare a sum function proposal.', time.time() + 660, mode='team-local')
    with pytest.raises(ChatError, match='No eligible'):
        team.run(session, request, 'Prepare a sum function proposal.', external=False)


def test_unadmitted_mismatched_and_stopped_requests_never_infer(setup):
    store, team, infer, session, request = setup
    with pytest.raises(ChatError):
        team.run(session, uuid4().hex, 'Unadmitted')
    with pytest.raises(ChatError):
        team.run(session, request, 'Different user message')
    (store.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        team.run(session, request, 'Prepare a sum function proposal.')
    assert infer.calls == []


def test_codex_command_pins_readonly_and_disables_unsafe_channels(setup, tmp_path):
    _, team, _, _, _ = setup
    executable = tmp_path.parent / (uuid4().hex + '.exe')
    executable.write_bytes(b'fixture; never executed')
    team.executables['codex'] = str(executable)
    argv = team._external_command('codex', tmp_path / 'schema.json', tmp_path / 'answer.json')
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    for feature in ('shell_tool', 'unified_exec', 'multi_agent', 'apps', 'plugins', 'hooks', 'browser_use'):
        assert any(argv[index:index + 2] == ['--disable', feature] for index in range(len(argv)))
    assert '--ignore-user-config' in argv and '--ephemeral' in argv
    assert not any('bypass' in part or 'danger-full' in part for part in argv)


def test_external_executable_inside_data_workspace_is_rejected(setup):
    store, team, _, _, _ = setup
    fake = store.guard.workspace / 'danger.exe'
    fake.write_bytes(b'fixture; never executed')
    team.executables['codex'] = str(fake)
    with pytest.raises(ChatError, match='trusted'):
        team._external_command('codex', fake, fake)


def test_existing_chat_records_migrate_without_replacing_history(tmp_path):
    import sqlite3
    directory = tmp_path / '.jarvis-chat'
    directory.mkdir()
    with sqlite3.connect(directory / 'conversations.sqlite3') as db:
        db.execute('CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,created REAL,updated REAL,reserved INTEGER)')
        db.execute('CREATE TABLE turns(conversation TEXT,id TEXT,created REAL,deadline REAL,status TEXT,user TEXT,assistant TEXT,tools TEXT,PRIMARY KEY(conversation,id))')
        identity, request = uuid4().hex, uuid4().hex
        db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)', (identity, 'Original', 1, 1, 0))
        db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?,?,?)', (identity, request, 1, 2, 'completed', 'Original message', 'Original answer', '[]'))
    record = ChatStore(Guardrails(tmp_path)).get(identity)['turns'][0]
    assert record['user'] == 'Original message' and record['assistant'] == 'Original answer'
    assert record['mode'] == 'chat' and record['team'] == {}


class CorrectionInference(FixtureInference):
    def __init__(self, plan, *, never_pass=False, selection=None):
        super().__init__(plan=plan)
        self.reviews = 0
        self.never_pass = never_pass
        self.selection = selection or ['p1']

    def __call__(self, messages, schema, deadline):
        title = schema['title']
        if title == 'ReviewResult':
            self.calls.append((messages, schema))
            self.reviews += 1
            passed = self.reviews > 1 and not self.never_pass
            return {'passed': passed, 'summary': 'Correct one boundary case.' if not passed else 'Issue resolved.',
                    'issues': [] if passed else ['Handle zero as an explicit example.']}
        if title == 'RevisionPlan':
            self.calls.append((messages, schema))
            return {'action': 'revise', 'assignment_ids': self.selection, 'summary': 'Correct the assigned proposal.'}
        if title == 'WorkResult':
            self.calls.append((messages, schema))
            context = json.loads(messages[-1]['content'])
            if 'correction' in context:
                assert context['previous_output'] and context['review_feedback']['issues']
            return {'summary': 'Revision-aware result.', 'output': 'Proposal version ' + str(context.get('correction', 0))}
        return super().__call__(messages, schema, deadline)


def single_local_plan():
    return {'summary': 'One local proposal.', 'assignments': [
        {'id': 'p1', 'role': 'coder', 'runtime': 'local', 'objective': 'Write an example.', 'depends_on': []}]}


def test_review_feedback_returns_to_same_owner_and_preserves_every_version(setup):
    store, team, _, session, request = setup
    infer = CorrectionInference(single_local_plan())
    team.infer = infer
    team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    item = state['assignments'][0]
    assert state['status'] == 'completed' and state['corrections'] == 1
    assert item['owner'] == 'local-coder@jarvis' and item['output'] == 'Proposal version 1'
    assert [review['passed'] for review in state['review_history']] == [False, True]
    import hashlib
    for version in item['versions']:
        data = (store.guard.workspace / version['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == version['sha256']
    old = json.loads((store.guard.workspace / item['versions'][0]['path']).read_text(encoding='utf-8'))
    assert old['output'] == 'Proposal version 0'


def test_correction_circuit_breaker_stops_after_three_and_keeps_counters(setup):
    store, team, _, session, request = setup
    team.infer = CorrectionInference(single_local_plan(), never_pass=True)
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert state['corrections'] == 3 and len(state['review_history']) == 4
    assert state['stop_reason'] == 'correction_limit' and state['status'] == 'needs_user_input'
    assert len(state['assignments'][0]['versions']) == 4
    assert state['stage_calls'] <= 20 and answer.startswith('협업 중단: 수정 횟수 상한')
    assert '검수에서 미해결' in answer
    with pytest.raises(ChatError, match='replay'):
        team.run(session, request, 'Prepare a sum function proposal.')


def test_revising_upstream_invalidates_dependent_packets(setup, monkeypatch):
    store, team, infer, session, request = setup
    corrective = CorrectionInference(infer.plan)
    team.infer = corrective
    dependencies = []
    def external(runtime, context, folder, name, deadline):
        dependencies.append(context['dependencies'][0]['output'])
        return WorkResult(summary='Dependent proposal.', output='Uses ' + dependencies[-1])
    monkeypatch.setattr(team, '_external', external)
    team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert dependencies == ['Proposal version 0', 'Proposal version 1']
    assert state['external_calls'] == 2
    assert all(len(item['versions']) == 2 for item in state['assignments'])


def test_external_call_limit_is_shared_across_corrections(setup, monkeypatch):
    store, team, _, session, request = setup
    plan = single_local_plan()
    plan['assignments'][0]['runtime'] = 'codex'
    team.infer = CorrectionInference(plan, never_pass=True)
    called = []
    def external(*args):
        called.append(args[3])
        return WorkResult(summary='External revision.', output='Proposal revision.')
    monkeypatch.setattr(team, '_external', external)
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert len(called) == 3 and len(set(called)) == 3
    assert state['external_calls'] == 3 and state['corrections'] == 2
    assert state['stop_reason'] == 'external_call_limit' and state['status'] == 'needs_user_input'
    assert answer.startswith('협업 중단: 외부 호출 상한 (수정 2회, 외부 호출 3회).')


def test_revision_coordinator_cannot_invent_new_assignment_or_owner(setup):
    store, team, _, session, request = setup
    team.infer = CorrectionInference(single_local_plan(), selection=['p3'])
    with pytest.raises(ChatError, match='invalid'):
        team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert state['status'] == 'failed' and state['corrections'] == 0


def test_all_local_corrections_cannot_exceed_twenty_total_stages(setup):
    store, team, _, session, request = setup
    plan = single_local_plan()
    for number in (2, 3):
        plan['assignments'].append({**plan['assignments'][0], 'id': f'p{number}'})
    team.infer = CorrectionInference(plan, never_pass=True, selection=['p1', 'p2', 'p3'])
    team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert state['stage_calls'] == 20 and state['corrections'] == 3
    assert state['status'] == 'needs_user_input' and state['stop_reason'] == 'stage_call_limit'


def test_history_verification_detects_changes_without_inference_or_reexecution(setup):
    store, team, _, session, request = setup
    team.infer = CorrectionInference(single_local_plan())
    team.run(session, request, 'Prepare a sum function proposal.')
    def forbidden(*args):
        raise AssertionError('History checks must not infer or dispatch.')
    team.infer = forbidden
    before = store.get(session)
    checked = team.verify_history(session, request)
    assert checked['status'] == 'ok' and len(checked['files']) == 4
    reference = before['turns'][0]['team']['assignments'][0]['versions'][0]
    path = store.guard.workspace / reference['path']
    path.write_text('{"output":"modified synthetic record"}', encoding='utf-8')
    changed = team.verify_history(session, request)
    assert changed['status'] == 'not_verified'
    assert changed['files'][0]['status'] == 'changed'
    assert store.get(session) == before


def test_history_verification_rejects_redirected_paths_and_reports_missing_records(setup):
    store, team, _, session, request = setup
    team.infer = CorrectionInference(single_local_plan())
    team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    versions = state['assignments'][0]['versions']
    versions[0]['path'] = '../private.json'
    versions[1].update(correction=3, path=f'.jarvis-chat/team-{session}-{request}/p1-version-3.json')
    store.progress(session, request, state)
    result = team.verify_history(session, request)
    assert result['status'] == 'not_verified'
    assert [item['status'] for item in result['files'][:2]] == ['invalid_reference', 'missing']


@pytest.mark.parametrize('issues,expected', [(['Missing zero case.'], 'needs_user_input'), ([], 'completed')])
def test_unresolved_review_issues_override_model_pass_without_losing_original_flag(setup, issues, expected):
    store, team, _, session, request = setup
    original = FixtureInference(plan=single_local_plan())
    def infer(messages, schema, deadline):
        if schema['title'] == 'ReviewResult':
            return {'passed': True, 'summary': 'Model says pass.', 'issues': issues}
        return original(messages, schema, deadline)
    team.infer = infer
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert state['status'] == expected
    assert state['review']['model_reported_passed'] is True
    assert state['review']['passed'] is (not bool(issues))
    archived = json.loads((store.guard.workspace / state['review_history'][0]['path']).read_text(encoding='utf-8'))
    assert archived['model_reported_passed'] is True
    if issues:
        assert state['review']['policy_adjustment'] == 'unresolved_issues'
        assert archived['issues'] == issues and archived['passed'] is False
        assert '검수에서 미해결' in answer


@pytest.mark.parametrize('size,expected', [(1500, 'completed'), (1501, 'needs_user_input')])
def test_partial_review_cannot_approve_unseen_output_or_trigger_speculative_rewrites(setup, size, expected):
    store, team, _, session, request = setup
    original = FixtureInference(plan=single_local_plan())
    calls = []
    def infer(messages, schema, deadline):
        calls.append(schema['title'])
        if schema['title'] == 'WorkResult':
            return {'summary': 'Long proposal.', 'output': 'x' * size}
        return original(messages, schema, deadline)
    team.infer = infer
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert state['status'] == expected
    assert 'RevisionPlan' not in calls
    if size > 1500:
        assert state['review']['model_reported_passed'] is True
        assert state['review']['passed'] is False
        assert state['review']['policy_adjustment'] == 'review_context_truncated'
        assert state['stop_reason'] == 'review_context_limit'
        assert '검수 문맥에 원문 일부만 포함됨' in answer


@pytest.mark.parametrize('summary_failure', [False, True])
def test_worker_code_survives_omitted_or_failed_synthesis(setup, summary_failure):
    store, team, _, session, request = setup
    original = FixtureInference(plan=single_local_plan())
    code = 'def add(a, b):\n    return a + b'
    def infer(messages, schema, deadline):
        if schema['title'] == 'WorkResult':
            return {'summary': 'Function proposal.', 'output': code}
        if schema['title'] == 'LeadResult':
            if summary_failure:
                raise ChatError('PRIVATE provider response must not leak')
            return {'reply': '요청한 함수 제안을 준비했습니다. 사용 방법은 별도 기록을 확인하세요.'}
        return original(messages, schema, deadline)
    team.infer = infer
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    state = store.get(session)['turns'][0]['team']
    assert code in answer and '작업 원문 p1' in answer
    assert 'PRIVATE' not in answer + json.dumps(state)
    assert state['status'] == ('needs_user_input' if summary_failure else 'completed')
    if summary_failure:
        assert state['stop_reason'] == 'synthesis_failed'
        assert state['assignments'][0]['status'] == 'completed'
        assert team.verify_history(session, request)['status'] == 'ok'


def test_long_outputs_share_answer_space_and_preserve_limits_and_notice(setup):
    store, team, _, session, request = setup
    plan = single_local_plan()
    for number in (2, 3):
        plan['assignments'].append({**plan['assignments'][0], 'id': f'p{number}'})
    original = FixtureInference(plan=plan)
    def infer(messages, schema, deadline):
        if schema['title'] == 'WorkResult':
            return {'summary': 'Long proposal.', 'output': 'z' * 6000}
        if schema['title'] == 'LeadResult':
            return {'reply': 's' * 6000}
        return original(messages, schema, deadline)
    team.infer = infer
    answer, _ = team.run(session, request, 'Prepare a sum function proposal.')
    assert len(answer) <= 6000
    assert all(f'작업 원문 p{number}' in answer for number in (1, 2, 3))
    assert answer.count('[원문 일부만 표시합니다.') == 3
    assert answer.endswith('코드 실행·파일 적용은 수행하지 않았습니다.')
    assert all(len(item['output']) == 6000 for item in store.get(session)['turns'][0]['team']['assignments'])


def test_stop_during_synthesis_never_becomes_partial_success(setup):
    store, team, _, session, request = setup
    original = FixtureInference(plan=single_local_plan())
    def infer(messages, schema, deadline):
        if schema['title'] == 'LeadResult':
            (store.guard.workspace / '.jarvis-stop').write_text('stop', encoding='utf-8')
            raise ChatError('Summary interrupted.')
        return original(messages, schema, deadline)
    team.infer = infer
    with pytest.raises(GuardrailError):
        team.run(session, request, 'Prepare a sum function proposal.')


def test_independent_instances_claim_same_request_only_once(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store, team, _, session, request = setup
    second_store = ChatStore(Guardrails(store.guard.workspace))
    original = FixtureInference(plan=single_local_plan())
    team.infer = original
    other = HybridTeam(second_store, original)
    # Both instances read the admitted turn before either performs the SQLite claim.
    rendezvous = threading.Barrier(2)
    for candidate_store in (store, second_store):
        original_get = candidate_store.get
        def first_read(identity, get=original_get, entered=[False]):
            result = get(identity)
            if not entered[0]:
                entered[0] = True
                rendezvous.wait(timeout=5)
            return result
        monkeypatch.setattr(candidate_store, 'get', first_read)
    def run(candidate_team):
        try:
            return candidate_team.run(session, request, 'Prepare a sum function proposal.')
        except ChatError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, candidate_team) for candidate_team in (team, other)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, tuple) for result in results) == 1
    failures = [result for result in results if isinstance(result, ChatError)]
    assert len(failures) == 1 and 'claimed' in str(failures[0])
    assert sum(schema['title'] == 'TeamPlan' for _, schema in original.calls) == 1
    state = store.get(session)['turns'][0]['team']
    assert state['status'] == 'completed' and state['stage_calls'] == 4
    assert len(state['assignments'][0]['versions']) == 1
    assert team.verify_history(session, request)['status'] == 'ok'


def test_claude_native_winget_discovery_does_not_require_refreshed_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr('jarvis.agents.team.shutil.which', lambda name: None)
    assert _discover_claude() is None
    installed = tmp_path / 'AppData/Local/Microsoft/WinGet/Packages/Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe/claude.exe'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b'fixture; never executed')
    assert _discover_claude() == str(installed)


def test_claude_command_disables_tools_mcp_settings_and_session_persistence(setup, tmp_path):
    _, team, _, _, _ = setup
    installed = tmp_path.parent / (uuid4().hex + '.exe')
    installed.write_bytes(b'fixture; never executed')
    schema = tmp_path / 'schema.json'
    schema.write_text('{}', encoding='utf-8')
    team.executables['claude-code'] = str(installed)
    argv = team._external_command('claude-code', schema, tmp_path / 'answer.json')
    assert argv[argv.index('--tools') + 1] == ''
    assert argv[argv.index('--mcp-config') + 1] == '{"mcpServers":{}}'
    assert argv[argv.index('--setting-sources') + 1] == ''
    assert '--strict-mcp-config' in argv and '--no-session-persistence' in argv
    assert argv[argv.index('--max-turns') + 1] == '1'
    assert not any('skip-permissions' in part or 'bypass' in part for part in argv)


def test_native_worker_registers_spawn_before_collecting_output(setup, monkeypatch):
    from types import SimpleNamespace
    store, team, _, _, _ = setup
    observed = []
    def communicate(*args, **kwargs):
        assert observed
        return json.dumps({'structured_output': {'summary':'proposal','output':'bounded proposal'}}).encode(), None
    process = SimpleNamespace(returncode=0,communicate=communicate,poll=lambda:0)
    monkeypatch.setattr(team, '_external_command', lambda *args:['native-fixture'])
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', lambda *args,**kwargs:process)
    monkeypatch.setattr('jarvis.agents.team.observe_spawned', lambda guard,child,**kwargs:observed.append(kwargs))
    folder = store.guard.workspace / 'native-observation'
    folder.mkdir()
    result = team._external('claude-code', {'request':'synthetic'},folder,'once',time.monotonic()+30)
    assert result.output == 'bounded proposal'
    assert observed[0]['kind'] == 'worker' and observed[0]['label'] == 'native-claude-code'
    assert 0 < observed[0]['timeout_seconds'] <= 30
