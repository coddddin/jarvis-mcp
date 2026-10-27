"""Local proposal loop: admitted model steps followed by evidence-based static QA."""
import hashlib
import json
import subprocess
import sys
import time

from pydantic import Field, model_validator

from jarvis.agents.models import Contract, QAEvidence, StepResult, TERMINAL
from jarvis.agents.store import TaskError, TaskStore
from jarvis.agents.memory import MemoryScope, MemoryStore
from jarvis.agents.process_registry import observe_spawned
from jarvis.agents.sandbox import DockerRuntimeConfig, SandboxRunner
from jarvis.core.paths import WORKERS_ROOT
from jarvis.core.guardrails import GuardrailError


class PlannerReply(Contract):
    summary: str = Field(min_length=1, max_length=2000)


class CoderReply(PlannerReply):
    proposal: str = Field(min_length=1, max_length=12000)


class LocalConfig(Contract):
    model: str = Field(default='qwen3.5:9b', pattern=r'^[a-zA-Z0-9_.-]+:[a-zA-Z0-9_.-]+$')
    docker: DockerRuntimeConfig | None = None

    @model_validator(mode='after')
    def offline_model(self):
        if 'cloud' in self.model.lower():
            raise ValueError('Cloud models are disabled.')
        return self


class LocalRunner:
    def __init__(self, store: TaskStore, config: LocalConfig):
        self.store = store
        self.config = LocalConfig.model_validate(config.model_dump())

    def _worker(self, mode, payload, deadline):
        self.store.guard.check_running()
        encoded = json.dumps(payload, ensure_ascii=True).encode('utf-8')
        if len(encoded) > 65536:
            raise TaskError('Worker input exceeds size limit.')
        process = subprocess.Popen([sys.executable, '-I', str(WORKERS_ROOT / 'agent_step.py'), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        stop_at = time.monotonic() + (10 if mode == 'qa' else 120)
        first = True
        try:
            observe_spawned(self.store.guard, process, kind='verification' if mode == 'qa' else 'worker',
                label='local-' + mode, timeout_seconds=min(10 if mode == 'qa' else 120,
                                                          max(1, deadline - time.time())))
            while True:
                self.store.guard.check_running()
                if '_task_id' in payload:
                    self._assert_lease(payload['_task_id'], payload['_step_id'])
                if time.time() >= deadline or time.monotonic() >= stop_at:
                    raise TaskError('Task or role request timed out.')
                try:
                    output, _ = process.communicate(input=encoded if first else None, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if len(output) > 262144:
                raise TaskError('Worker response exceeds size limit.')
            envelope = json.loads(output)
            if process.returncode != 0 or envelope.get('ok') is not True:
                raise TaskError('Local worker failed; check Ollama installation/model or input limits.')
            return envelope['result']
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def _assert_lease(self, task_id, step_id):
        current = self.store.get(task_id)
        if current.status != 'running' or current.active_step != step_id:
            raise TaskError('Task was cancelled, expired, or no longer owns this active step.')

    def step(self, task_id):
        current = self.store.get(task_id)
        if current.status in TERMINAL:
            return current
        step = self.store.begin_step(task_id, current.expected_role,
                                     reserve_output_tokens=1024 if current.expected_role != 'qa' else 0)
        try:
            if step.expected_role == 'qa':
                proposal = step.history[-1].proposal
                cases = [x.model_dump(mode='json') for x in step.request.integer_cases]
                if step.request.qa_mode == 'container':
                    if self.config.docker is None:
                        raise TaskError('Trusted container runtime is not configured; host fallback is forbidden.')
                    evidence = SandboxRunner(self.store.guard, self.config.docker,
                        cancel_check=lambda: self._assert_lease(task_id, step.active_step)).check(proposal, cases, step.deadline)
                else:
                    evidence = QAEvidence.model_validate(self._worker('qa', {'proposal': proposal, 'integer_cases': cases,
                        '_task_id': task_id, '_step_id': step.active_step}, step.deadline))
                if evidence.sha256 != hashlib.sha256(proposal.encode('utf-8')).hexdigest():
                    raise TaskError('QA evidence does not match the admitted proposal.')
                if cases:
                    case_hash = hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    expected_checker = 'python_container_cases_v1' if step.request.qa_mode == 'container' else 'python_integer_cases_v1'
                    if evidence.checker != expected_checker or evidence.cases_sha256 != case_hash or evidence.cases_total != len(cases):
                        raise TaskError('QA evidence does not match the requested test cases.')
                result = StepResult(step_id=step.active_step, role='qa', summary=evidence.detail,
                                    qa_passed=evidence.passed, qa_evidence=evidence)
            else:
                reply_type = PlannerReply if step.expected_role == 'planner' else CoderReply
                # Only explicit task data and bounded prior results are sent; no file discovery.
                context = {'task': step.request.summary,
                           'operator_feedback': step.operator_feedback,
                           'plan': step.history[0].summary if step.history else '',
                           'previous_proposal': next((x.proposal for x in reversed(step.history) if x.proposal), ''),
                           'last_result': step.history[-1].summary if step.history else ''}
                if step.request.integer_cases:
                    context['integer_cases'] = [x.model_dump(mode='json') for x in step.request.integer_cases]
                    context['restriction'] = 'Exactly one pure integer function with a single return expression; no imports/calls/loops/statements/defaults/decorators.'
                    if step.request.qa_mode == 'container':
                        context['restriction'] = 'Implement the named function for the explicit integer cases. QA uses an isolated Linux container. Do not request tools or host access.'
                if step.request.memory_project:
                    memories = MemoryStore(self.store).list(MemoryScope(
                        project=step.request.memory_project, owner=step.request.memory_owner), confirmed_only=True, limit=8)
                    context['untrusted_confirmed_preferences'] = [
                        {'kind': m.kind, 'key': m.key, 'value': m.value, 'source': m.source,
                         'memory_id': m.memory_id, 'not_permissions': True} for m in memories]
                raw = self._worker('generate', {'model': self.config.model, 'role': step.expected_role,
                       '_task_id': task_id, '_step_id': step.active_step,
                       'schema': reply_type.model_json_schema(), 'context': json.dumps(context, ensure_ascii=False)}, step.deadline)
                reply = reply_type.model_validate(raw)
                result = StepResult(step_id=step.active_step, role=step.expected_role,
                                    reserved_tokens=step.pending_generation_tokens,
                                    **reply.model_dump())
            return self.store.finish_step(task_id, result)
        except GuardrailError:
            # Stop marker blocks DB commits too; retain charged running state, never restart silently.
            raise
        except Exception as exc:
            state = self.store.get(task_id)
            if state.status not in TERMINAL:
                self.store.interrupt(task_id, 'Role failed: ' + type(exc).__name__ + '; no code was executed or applied.')
            raise TaskError('Role failed; inspect persisted task status. No automatic transport retry.') from exc

    def run(self, task_id):
        while True:
            task = self.step(task_id)
            if task.status in TERMINAL:
                return task
