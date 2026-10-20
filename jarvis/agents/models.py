"""Persisted task contracts. Summaries are untrusted data, never permissions."""
from typing import Literal
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.core.redaction import redact

Role = Literal['planner', 'coder', 'qa']
Status = Literal['ready', 'running', 'completed', 'needs_user_input', 'cancelled', 'timed_out']
TERMINAL = {'completed', 'needs_user_input', 'cancelled', 'timed_out'}


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True, allow_inf_nan=False)


class IntegerCase(Contract):
    function: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    args: tuple[int, ...] = Field(min_length=1, max_length=8)
    expected: int = Field(ge=-1_000_000_000, le=1_000_000_000)

    @field_validator('args')
    @classmethod
    def bounded_args(cls, values):
        if any(abs(value) > 1_000_000_000 for value in values):
            raise ValueError('Integer QA argument limit exceeded.')
        return values


class TaskRequest(Contract):
    summary: str = Field(min_length=1, max_length=2000)
    max_steps: int = Field(default=12, ge=3, le=20)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_generation_tokens: int = Field(default=8192, ge=1024, le=32768)
    max_model_tokens: int = Field(default=65536, ge=9216, le=262144)
    retention_days: int = Field(default=7, ge=1, le=90)
    integer_cases: tuple[IntegerCase, ...] = Field(default=(), max_length=32)
    memory_project: str | None = Field(default=None, pattern=r'^projects/[A-Za-z0-9_-]{1,80}$')
    memory_owner: str = Field(default='local', pattern=r'^[A-Za-z0-9_-]{1,64}$')
    qa_mode: Literal['restricted', 'container'] = 'restricted'

    @model_validator(mode='after')
    def container_requires_cases(self):
        if self.qa_mode == 'container' and not self.integer_cases:
            raise ValueError('Container QA requires explicit test cases.')
        return self

    @field_validator('summary')
    @classmethod
    def safe_summary(cls, value: str) -> str:
        value = redact(value).strip()
        if not value:
            raise ValueError('A nonempty summary is required.')
        return value


class QAEvidence(Contract):
    checker: Literal['python_syntax_v1', 'python_integer_cases_v1', 'python_container_cases_v1'] = 'python_syntax_v1'
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    passed: bool
    detail: str = Field(min_length=1, max_length=500)
    cases_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    cases_passed: int = Field(default=0, ge=0, le=32)
    cases_total: int = Field(default=0, ge=0, le=32)
    runtime_image_id: str | None = Field(default=None, pattern=r'^sha256:[a-f0-9]{64}$')

    @model_validator(mode='after')
    def case_evidence(self):
        if (self.checker == 'python_container_cases_v1') != (self.runtime_image_id is not None):
            raise ValueError('Only container evidence requires a pinned runtime image identity.')
        if self.cases_passed > self.cases_total:
            raise ValueError('Invalid case counts.')
        if self.checker in {'python_integer_cases_v1', 'python_container_cases_v1'}:
            if not self.cases_sha256 or self.cases_total < 1 or self.passed != (self.cases_passed == self.cases_total):
                raise ValueError('Incomplete integer-case evidence.')
        elif self.cases_sha256 is not None or self.cases_total or self.cases_passed:
            raise ValueError('Syntax checker cannot claim functional cases.')
        return self


class StepResult(Contract):
    step_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    role: Role
    summary: str = Field(min_length=1, max_length=2000)
    qa_passed: bool | None = None
    proposal: str = Field(default='', max_length=12000)
    qa_evidence: QAEvidence | None = None
    reserved_tokens: int = Field(default=0, ge=0, le=3072)

    _safe_summary = field_validator('summary')(TaskRequest.safe_summary.__func__)

    @model_validator(mode='after')
    def qa_contract(self):
        if (self.role == 'qa') != (self.qa_passed is not None):
            raise ValueError('Only QA must supply a boolean verdict.')
        if self.proposal and (self.role != 'coder' or not self.proposal.strip() or redact(self.proposal) != self.proposal):
            raise ValueError('Only coder may supply a nonempty proposal without recognized secrets.')
        if self.qa_evidence is not None and (self.role != 'qa' or self.qa_evidence.passed != self.qa_passed):
            raise ValueError('QA evidence must match the verdict.')
        if self.role == 'qa' and self.reserved_tokens:
            raise ValueError('Static/restricted QA does not reserve model tokens.')
        return self


class Task(Contract):
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    request: TaskRequest
    status: Status = 'ready'
    expected_role: Role = 'planner'
    active_step: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    steps_started: int = Field(default=0, ge=0, le=20)
    correction_retries: int = Field(default=0, ge=0, le=3)
    created_at: float = Field(ge=0)
    deadline: float = Field(ge=0)
    updated_at: float = Field(ge=0)
    history: tuple[StepResult, ...] = Field(default=(), max_length=20)
    reason: str = Field(default='', max_length=2000)
    generation_tokens_reserved: int = Field(default=0, ge=0, le=32768)
    pending_generation_tokens: int = Field(default=0, ge=0, le=3072)
    transport_retries: int = Field(default=0, ge=0, le=2)
    recovery_ready: bool = False
    operator_feedback: str = Field(default='', max_length=2000)
    failure_notes: tuple[str, ...] = Field(default=(), max_length=2)

    @property
    def model_tokens_reserved(self):
        # Every 1024-output reservation also reserves the fixed 8192 context.
        return self.generation_tokens_reserved * 9

    @model_validator(mode='after')
    def state_contract(self):
        if (self.status == 'running') != (self.active_step is not None):
            raise ValueError('Only a running task has an active step.')
        if self.steps_started > self.request.max_steps:
            raise ValueError('Persisted step budget exceeded.')
        if self.generation_tokens_reserved > self.request.max_generation_tokens:
            raise ValueError('Persisted generation budget exceeded.')
        if self.model_tokens_reserved > self.request.max_model_tokens:
            raise ValueError('Persisted combined context/output reservation budget exceeded.')
        if self.generation_tokens_reserved != sum(x.reserved_tokens for x in self.history) + self.pending_generation_tokens:
            raise ValueError('Generation reservation differs from persisted history.')
        if (self.generation_tokens_reserved % 1024 or self.pending_generation_tokens % 1024
                or any(x.reserved_tokens % 1024 for x in self.history)):
            raise ValueError('Generation reservations must use fixed request units.')
        if len(self.failure_notes) != self.transport_retries:
            raise ValueError('Explicit execution retry accounting differs from retained failures.')
        if len(self.history) > self.steps_started:
            raise ValueError('More results than admitted steps.')
        if self.deadline != self.created_at + self.request.timeout_seconds:
            raise ValueError('Persisted deadline differs from original budget.')
        expected, retries, previous, finished = 'planner', 0, None, False
        ids = set()
        for result in self.history:
            if finished or result.role != expected or result.step_id in ids:
                raise ValueError('Invalid persisted role history.')
            ids.add(result.step_id)
            if result.role == 'qa' and self.request.integer_cases:
                cases = [case.model_dump(mode='json') for case in self.request.integer_cases]
                digest = hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                checker = 'python_container_cases_v1' if self.request.qa_mode == 'container' else 'python_integer_cases_v1'
                if (result.qa_evidence is None or result.qa_evidence.checker != checker
                        or result.qa_evidence.cases_sha256 != digest or result.qa_evidence.cases_total != len(cases)):
                    raise ValueError('Persisted QA evidence differs from the requested cases or execution mode.')
            if result.role == 'planner':
                expected = 'coder'
            elif result.role == 'coder':
                retries += int(previous == 'qa')
                expected = 'qa'
            elif result.qa_passed or retries == 3:
                finished = True
            else:
                expected = 'coder'
            previous = result.role
        outstanding = self.steps_started - len(self.history)
        if outstanding not in (0, 1) or self.active_step in ids:
            raise ValueError('Invalid admitted-step accounting.')
        retries += int(outstanding == 1 and expected == 'coder' and previous == 'qa')
        if self.expected_role != expected or self.correction_retries != retries:
            raise ValueError('Role or retry counter disagrees with history.')
        if self.recovery_ready and (self.status != 'ready' or outstanding != 1):
            raise ValueError('Recovery requires one previously charged unfinished step.')
        if self.status == 'running' and outstanding != 1 or self.status == 'ready' and outstanding != int(self.recovery_ready):
            raise ValueError('Task status disagrees with admitted steps.')
        success = bool(self.history) and self.history[-1].qa_passed is True
        if (self.status == 'completed') != success or finished and self.status not in TERMINAL:
            raise ValueError('Terminal status disagrees with QA result.')
        return self
