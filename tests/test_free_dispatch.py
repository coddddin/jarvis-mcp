from datetime import datetime, timezone
from uuid import uuid4

import pytest

from jarvis.agents.chat import ChatBusy
from jarvis.agents.provider_routing import ProviderCandidate
from jarvis.app.chat import ChatService
from jarvis.app.dispatch import FreeDispatcher, ProviderFailure
from jarvis.core.guardrails import GuardrailError, Guardrails


@pytest.fixture
def setup(tmp_path):
    service = ChatService(Guardrails(tmp_path))
    identity, request = service.store.create()['id'], uuid4().hex
    service.store.begin(identity, request, 'Write an add function proposal', 9999999999, mode='free')
    return service, identity, request


def candidate(name='free-a', **changes):
    return ProviderCandidate(id=name, runtime='opencode', model='public-free', capabilities=['coding'],
        cost='free', available=True, access_verified=True, verified_at=datetime.now(timezone.utc),
        authentication_required=False, **changes)


def test_no_ready_provider_persists_reason_without_any_inference(setup):
    service, identity, request = setup
    service._infer = lambda *args: pytest.fail('Free dispatch invoked local inference')
    answer, traces = service.dispatcher.run(identity, request, 'Write an add function proposal')
    state = service.store.get(identity)['turns'][0]['dispatch']
    assert state['status'] == 'needs_user_input'
    assert state['selection']['status'] == 'no_eligible_provider'
    assert state['budget']['attempts'] == [] and traces == []
    assert '전환하지 않았어' in answer


def test_reservation_is_durable_before_executor_and_result_is_preserved(setup):
    service, identity, request = setup
    calls = []
    def execute(**kwargs):
        state = service.store.get(identity)['turns'][0]['dispatch']
        assert state['status'] == 'running' and state['budget']['attempts'][0]['status'] == 'reserved'
        calls.append(kwargs)
        return {'reply': 'def add(a,b): return a+b', 'actual_tokens': 100}
    dispatcher = FreeDispatcher(service.store, candidates=[candidate()], executors={'free-a': execute})
    answer, traces = dispatcher.run(identity, request, 'Write an add function proposal')
    assert len(calls) == 1 and traces[0]['status'] == 'ok'
    assert service.store.get(identity)['turns'][0]['dispatch']['reply'] == answer
    with pytest.raises(ChatBusy):
        FreeDispatcher(service.store, candidates=[candidate()], executors={'free-a': execute}).run(identity, request, 'Write an add function proposal')
    assert len(calls) == 1


def test_exhausted_call_budget_records_no_running_attempt(setup):
    # The guard refuses before any provider call, so no reservation may be left looking in-flight.
    service, identity, request = setup
    calls = []
    dispatcher = FreeDispatcher(service.store, candidates=[candidate()],
        executors={'free-a': lambda **kwargs: calls.append(kwargs) or {'reply': 'x'}})
    service.store.guard._calls = service.store.guard.MAX_CALLS
    with pytest.raises(GuardrailError):
        dispatcher.run(identity, request, 'Write an add function proposal')
    assert calls == []
    assert service.store.get(identity)['turns'][0]['dispatch'] == {}


def test_only_confirmed_quota_failure_switches_to_another_free_provider(setup):
    service, identity, request = setup
    calls = []
    def limited(**kwargs):
        calls.append('a')
        raise ProviderFailure('rate_limit')
    def success(**kwargs):
        calls.append('b')
        return {'reply': 'proposal'}
    dispatcher = FreeDispatcher(service.store, candidates=[candidate(), candidate('free-b')],
        executors={'free-a': limited, 'free-b': success})
    dispatcher.run(identity, request, 'Write an add function proposal')
    state = service.store.get(identity)['turns'][0]['dispatch']
    assert calls == ['a', 'b'] and state['status'] == 'completed'
    assert [a['status'] for a in state['budget']['attempts']] == ['failed', 'completed']
    assert sum(a['reserved_tokens'] for a in state['budget']['attempts']) > 3000


def test_timeout_is_unknown_and_never_switches_or_exposes_raw_error(setup):
    service, identity, request = setup
    def timeout(**kwargs):
        raise TimeoutError('private diagnostic')
    dispatcher = FreeDispatcher(service.store, candidates=[candidate(), candidate('free-b')],
        executors={'free-a': timeout, 'free-b': lambda **kwargs: pytest.fail('Timeout retried')})
    reply, traces = dispatcher.run(identity, request, 'Write an add function proposal')
    state = service.store.get(identity)['turns'][0]['dispatch']
    assert state['budget']['attempts'][0]['status'] == 'unknown'
    assert 'private diagnostic' not in str(state) + reply + str(traces)


def test_free_mode_returns_delivered_but_not_successful_without_candidate(tmp_path, monkeypatch):
    service = ChatService(Guardrails(tmp_path))
    monkeypatch.setattr(service, '_infer', lambda *args: pytest.fail('Local model must not run in free mode'))
    identity, request = service.store.create()['id'], uuid4().hex
    first = service.send(identity, request, 'Prepare a proposal', mode='free')
    turn = first['turns'][0]
    assert turn['status'] == 'completed' and turn['dispatch']['status'] == 'needs_user_input'
    assert service.send(identity, request, 'Prepare a proposal', mode='free') == first


def test_authenticated_http_free_request_does_not_invoke_local_or_unverified_models(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from jarvis.agents.store import TaskStore
    from jarvis.app.events import create_event_app
    service = ChatService(Guardrails(tmp_path))
    monkeypatch.setattr(service, '_infer', lambda *args: pytest.fail('Unexpected local inference'))
    app = create_event_app(TaskStore(service.guard), token='x'*43, task_ids=[], origins=['http://127.0.0.1:8788'], chat=service)
    identity = service.store.create()['id']
    path = '/api/chat/conversations/' + identity + '/messages'
    with TestClient(app, base_url='http://127.0.0.1:8788') as client:
        assert client.post(path, headers={'Origin': 'http://127.0.0.1:8788'},
            json={'request_id': uuid4().hex, 'message': 'proposal', 'mode': 'free'}).status_code == 401
        response = client.post(path, headers={'Authorization': 'Bearer '+'x'*43, 'Origin': 'http://127.0.0.1:8788'},
            json={'request_id': uuid4().hex, 'message': 'proposal', 'mode': 'free'})
        turn = response.json()['turns'][0]
        assert response.status_code == 200 and turn['dispatch']['status'] == 'needs_user_input'
        assert turn['dispatch']['budget']['attempts'] == []


def test_atomic_dispatch_claim_has_single_winner(setup):
    service, identity, request = setup
    service.store.update_dispatch(identity, request, {}, {'status': 'running'})
    with pytest.raises(ChatBusy):
        service.store.update_dispatch(identity, request, {}, {'status': 'running'})


def test_natural_free_agent_request_routes_without_selecting_a_model(tmp_path, monkeypatch):
    service = ChatService(Guardrails(tmp_path))
    monkeypatch.setattr(service, '_infer', lambda *args: pytest.fail('Explicit free request invoked local inference'))
    monkeypatch.setattr(service.team, 'run', lambda *args, **kwargs: pytest.fail('Free route fell back to local team'))
    turn = service.send(service.store.create()['id'], uuid4().hex, '무료 API 에이전트로 함수 제안을 만들어줘', mode='auto')['turns'][0]
    assert turn['mode'] == 'free' and turn['dispatch']['status'] == 'needs_user_input'


def test_paid_candidate_is_rejected_even_when_adapter_is_registered(setup):
    service, identity, request = setup
    paid = candidate().model_copy(update={'cost': 'subscription'})
    dispatcher = FreeDispatcher(service.store, candidates=[paid],
        executors={'free-a': lambda **kwargs: pytest.fail('Paid fallback')})
    dispatcher.run(identity, request, 'Write an add function proposal')
    state = service.store.get(identity)['turns'][0]['dispatch']
    assert state['selection']['rejected'] == [{'id': 'free-a', 'reason': 'cost_not_allowed'}]
