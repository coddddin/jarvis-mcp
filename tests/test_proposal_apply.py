import json
import os
import subprocess

import pytest

from jarvis.agents.models import QAEvidence, StepResult, TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.core.guardrails import Guardrails, GuardrailError
from jarvis.features.development.service import ProposalApplier, ProposalError
from jarvis.features.snapshots.service import SnapshotStore
from jarvis.workers.agent_step import check_python


@pytest.fixture
def env(tmp_path):
    (tmp_path / 'projects/demo').mkdir(parents=True)
    guard = Guardrails(tmp_path)
    snapshots, tasks = SnapshotStore(guard), TaskStore(guard)
    source = "def add(a, b):\n    return a + b\n"
    task = tasks.create(TaskRequest(summary='Prepare a Python proposal'))
    for role in ['planner', 'coder', 'qa']:
        step = tasks.begin_step(task.task_id, role)
        tasks.finish_step(task.task_id, StepResult(step_id=step.active_step, role=role, summary='Verified step',
            proposal=source if role == 'coder' else '', qa_passed=True if role == 'qa' else None,
            qa_evidence=QAEvidence.model_validate(check_python(source)) if role == 'qa' else None))
    target = tmp_path / 'projects/demo/app.py'
    target.write_bytes(b'def add(a, b):\n    return 0\n')
    return ProposalApplier(snapshots, tasks), task.task_id, target, source


def preview(env, relative='projects/demo/app.py'):
    applier, task_id, _, _ = env
    return applier.preview(task_id, relative)


def test_preview_has_diff_without_writes(env):
    applier, _, target, _ = env
    before = target.read_bytes()
    report = preview(env)
    assert report['action'] == 'replace' and '-    return 0' in report['diff']
    assert report['functional_correctness_verified'] is False
    assert target.read_bytes() == before
    assert not (applier.guard.workspace / '.jarvis-snapshots').exists()


@pytest.mark.parametrize('new', [False, True])
def test_apply_backs_up_and_does_not_execute(env, new):
    applier, task_id, target, source = env
    if new:
        target.unlink()
    old = target.read_bytes() if target.exists() else None
    report = preview(env)
    result = applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])
    assert target.read_bytes() == source.encode()
    assert result['code_executed'] is False
    backup, files = applier.snapshots._load(result['backup_id'])
    assert files.get('app.py') == old
    journal = applier.guard.workspace / '.jarvis-agent-changes' / result['recovery_id'] / 'journal.json'
    saved = json.loads(journal.read_text())
    assert saved['status'] == 'applied' and saved['project'] == 'projects/demo'
    with pytest.raises(ProposalError):
        applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])


def test_stale_original_and_wrong_target_refused(env):
    applier, task_id, target, _ = env
    report = preview(env)
    target.write_bytes(b'# edited externally\n')
    with pytest.raises(ProposalError, match='stale'):
        applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])
    with pytest.raises(ProposalError, match='stale'):
        applier.apply(task_id, 'projects/demo/other.py', report['review_digest'])
    assert target.read_bytes() == b'# edited externally\n'
    assert not (applier.guard.workspace / '.jarvis-agent-changes').exists()


@pytest.mark.parametrize('target', ['server.py', '../escape.py', 'documents/a.py',
                                   'projects/demo/.hidden.py', 'projects/demo/client_secret.py',
                                   'projects/demo/file.txt'])
def test_scope_and_protected_paths(env, target):
    applier, task_id, _, _ = env
    with pytest.raises(ValueError):
        applier.preview(task_id, target)


def test_missing_qa_evidence_or_pending_task_cannot_apply(env):
    applier, _, _, _ = env
    task = applier.tasks.create(TaskRequest(summary='No completed QA'))
    with pytest.raises(ProposalError, match='completed'):
        applier.preview(task.task_id, 'projects/demo/app.py')
    for role in ['planner', 'coder', 'qa']:
        step = applier.tasks.begin_step(task.task_id, role)
        applier.tasks.finish_step(task.task_id, StepResult(step_id=step.active_step, role=role,
            summary='Manual result without evidence', proposal='print(1)' if role == 'coder' else '',
            qa_passed=True if role == 'qa' else None))
    with pytest.raises(ProposalError, match='evidence'):
        applier.preview(task.task_id, 'projects/demo/app.py')


def test_backup_failure_leaves_target_unchanged_and_claim_consumed(env, monkeypatch):
    applier, task_id, target, _ = env
    original = target.read_bytes()
    report = preview(env)
    def fail(_):
        raise OSError('Disk full')
    monkeypatch.setattr(applier.snapshots, 'create', fail)
    with pytest.raises(ProposalError, match='rollback=not_needed'):
        applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])
    assert target.read_bytes() == original
    reopened = ProposalApplier(SnapshotStore(applier.guard), TaskStore(applier.guard))
    with pytest.raises(ProposalError, match='consumed'):
        reopened.apply(task_id, 'projects/demo/app.py', report['review_digest'])


@pytest.mark.parametrize('new', [False, True])
def test_post_apply_journal_error_rolls_back(env, monkeypatch, new):
    applier, task_id, target, _ = env
    if new:
        target.unlink()
    original = target.read_bytes() if target.exists() else None
    report = preview(env)
    journal = applier.snapshots._journal
    def fail_once(recovery, data):
        if data['status'] == 'applied':
            raise OSError('Cannot save final journal')
        return journal(recovery, data)
    monkeypatch.setattr(applier.snapshots, '_journal', fail_once)
    with pytest.raises(ProposalError, match='rollback=completed'):
        applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])
    assert (target.read_bytes() if target.exists() else None) == original


def test_concurrent_file_after_apply_is_preserved_not_blindly_rolled_back(env, monkeypatch):
    applier, task_id, target, _ = env
    report = preview(env)
    journal = applier.snapshots._journal
    def fail_after_other_writer(recovery, data):
        if data['status'] == 'applied':
            target.write_bytes(b'# other writer\n')
            raise OSError('Concurrent change')
        return journal(recovery, data)
    monkeypatch.setattr(applier.snapshots, '_journal', fail_after_other_writer)
    with pytest.raises(ProposalError, match='rollback=incomplete'):
        applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])
    assert target.read_bytes() == b'# other writer\n'


def test_project_lock_blocks_competing_writer(env):
    applier, task_id, _, _ = env
    import hashlib
    lock = applier.guard.workspace / '.jarvis-agent-changes/locks' / (
        hashlib.sha256(b'projects/demo').hexdigest() + '.lock')
    lock.mkdir(parents=True)
    with pytest.raises(ProposalError, match='lock'):
        applier.apply(task_id, 'projects/demo/app.py', preview(env)['review_digest'])


@pytest.mark.parametrize('incomplete', [True, False])
def test_unfinished_or_missing_recovery_journal_blocks_new_apply(env, incomplete):
    applier, task_id, target, _ = env
    report = preview(env)
    recovery = applier.guard.workspace / '.jarvis-agent-changes' / ('a' * 64)
    recovery.mkdir(parents=True)
    if incomplete:
        (recovery / 'journal.json').write_text(json.dumps({'project': 'projects/demo', 'status': 'applying'}))
    before = target.read_bytes()
    with pytest.raises(ProposalError, match='recovery|unfinished'):
        applier.apply(task_id, 'projects/demo/app.py', report['review_digest'])
    assert target.read_bytes() == before


def test_stop_marker_blocks_apply(env):
    applier, task_id, target, _ = env
    before = target.read_bytes()
    digest = preview(env)['review_digest']
    (applier.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        applier.apply(task_id, 'projects/demo/app.py', digest)
    assert target.read_bytes() == before


def test_secret_or_hardlinked_original_is_not_previewed(env):
    applier, _, target, _ = env
    target.write_text('password="not-for-model"', encoding='utf-8')
    with pytest.raises(ProposalError, match='sensitive'):
        preview(env)
    target.write_bytes(b'# ordinary\n')
    os.link(target, target.parent / 'copy.py')
    with pytest.raises(ValueError):
        preview(env)


def test_linked_target_parent_rejected_without_touching_external_data(env, tmp_path):
    applier, task_id, _, _ = env
    outside = tmp_path / 'external'
    outside.mkdir()
    marker = outside / 'keep.txt'
    marker.write_bytes(b'keep')
    link = applier.guard.workspace / 'projects/demo/linked'
    assert link.absolute().is_relative_to(applier.guard.workspace)
    if os.name == 'nt':
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                              capture_output=True, text=True, timeout=5)
        assert made.returncode == 0, made.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(GuardrailError):
            applier.preview(task_id, 'projects/demo/linked/new.py')
        assert list(outside.iterdir()) == [marker]
        assert marker.read_bytes() == b'keep'
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()
