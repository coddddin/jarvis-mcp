"""Persist-before-execution bridge for verified free provider adapters only."""
import time

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.agents.chat import ChatBusy, ChatError
from jarvis.agents.provider_routing import DispatchBudget, ProviderCandidate, next_provider, reserve_attempt, finish_attempt
from jarvis.core.guardrails import GuardrailError
from jarvis.core.redaction import redact


class DispatchResult(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    reply: str = Field(min_length=1, max_length=5000)
    actual_tokens: int | None = Field(default=None, ge=0)


class ProviderFailure(Exception):
    """Trusted adapters classify failures; raw diagnostics never enter chat history."""
    def __init__(self, reason):
        if reason not in {'rate_limit', 'quota_exhausted', 'context_limit', 'authentication', 'invalid_response', 'provider_error'}:
            raise ValueError('Invalid provider failure category.')
        self.reason = reason
        super().__init__(reason)


class FreeDispatcher:
    def __init__(self, store, *, candidates=(), executors=None):
        self.store, self.guard = store, store.guard
        self.candidates = candidates
        self.executors = dict(executors or {})

    def snapshot(self):
        raw = self.candidates() if callable(self.candidates) else self.candidates
        result = [ProviderCandidate.model_validate(item) for item in raw]
        return [item.model_copy(update={'available': False}) if item.id not in self.executors else item for item in result]

    def metadata(self):
        return {'free_only': True, 'max_calls': 3, 'max_tokens': 12000,
                'configured_candidates': len(self.snapshot()), 'automatic_paid_fallback': False}

    def run(self, identity, request_id, message, *, capability='coding', deadline=None):
        deadline = min(deadline or time.monotonic()+180, time.monotonic()+180)
        turn = next((t for t in self.store.get(identity)['turns'] if t['id'] == request_id), None)
        if turn is None or turn['status'] != 'pending' or turn['mode'] != 'free' or turn['user'] != message:
            raise ChatError('Free dispatch requires an admitted matching request.')
        state = turn['dispatch']
        if state:
            raise ChatBusy('Dispatch already claimed; automatic replay is forbidden.')
        # UTF-8 bytes is a conservative reservation proxy, not actual token usage.
        input_tokens = len(message.encode('utf-8')) + 512
        budget = DispatchBudget()
        traces = []
        while True:
            self.guard.check_running()
            candidates = self.snapshot()
            choice = next_provider(candidates, budget, capability=capability,
                input_tokens=input_tokens, output_tokens=1024)
            previous = state
            state = {'status': 'needs_user_input', 'selection': choice,
                     'budget': budget.model_dump(mode='json'), 'free_only': True}
            if choice['selected'] is None or time.monotonic() >= deadline:
                if time.monotonic() >= deadline:
                    state['selection'] = {'status': 'deadline_exceeded', 'selected': None}
                self.store.update_dispatch(identity, request_id, previous, state)
                messages = {'request_budget_exhausted': '이번 요청의 호출·토큰 예약 한도에 도달했어.',
                    'reconciliation_required': '이전 호출의 완료 여부를 먼저 확인해야 해.',
                    'deadline_exceeded': '요청 시간이 초과됐어.'}
                return messages.get(state['selection']['status'], '현재 자동 실행 가능한 무료 에이전트를 확인하지 못했어.') + ' 유료 모델이나 로컬 모델로 전환하지 않았어.', traces
            selected = choice['selected']
            budget = reserve_attempt(candidates, budget, capability=capability,
                input_tokens=input_tokens, output_tokens=1024)
            # Spend the guard budget first: a refusal here must not leave a reservation that looks in-flight.
            self.guard.enter_call()
            state.update(status='running', budget=budget.model_dump(mode='json'))
            self.store.update_dispatch(identity, request_id, previous, state)
            try:
                result = DispatchResult.model_validate(self.executors[selected](message=message, deadline=deadline))
                if not result.reply.strip():
                    raise ProviderFailure('invalid_response')
                if time.monotonic() >= deadline:
                    raise TimeoutError()
            except GuardrailError:
                raise
            except Exception as exc:
                unknown = not isinstance(exc, (ProviderFailure, ValidationError))
                reason = exc.reason if isinstance(exc, ProviderFailure) else 'invalid_response' if isinstance(exc, ValidationError) else 'timeout' if isinstance(exc, TimeoutError) else 'provider_error'
                budget = finish_attempt(budget, status='unknown' if unknown else 'failed', reason=reason)
                updated = {**state, 'status': 'needs_user_input', 'budget': budget.model_dump(mode='json')}
                self.store.update_dispatch(identity, request_id, state, updated)
                state = updated
                traces.append({'name': 'provider/' + selected, 'status': 'error', 'detail': '에이전트 요청 실패: ' + reason})
                if unknown or reason not in {'rate_limit', 'quota_exhausted', 'context_limit'}:
                    return '에이전트 응답을 완료하지 못했어. 중복 실행이나 유료 전환 없이 멈췄어. 기록을 확인해줘.', traces
                continue
            budget = finish_attempt(budget, status='completed', actual_tokens=result.actual_tokens)
            reply = redact(result.reply) + '\n\n※ 에이전트 제안입니다. 코드 실행·파일 적용·독립 검수는 수행하지 않았습니다.'
            updated = {**state, 'status': 'completed', 'budget': budget.model_dump(mode='json'), 'reply': reply}
            self.store.update_dispatch(identity, request_id, state, updated)
            traces.append({'name': 'provider/' + selected, 'status': 'ok', 'detail': '검증된 무료 후보의 제안을 받았습니다. 실행 검증은 아닙니다.'})
            return reply, traces
