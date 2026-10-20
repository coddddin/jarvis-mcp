"""Bounded local diagnostics. Never imports, executes, or changes supplied code."""
import ast
import hashlib
import json
import os
from jarvis.core.redaction import redact
import difflib
from pathlib import PurePosixPath
import re
import tokenize
from io import StringIO
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DebugError(ValueError):
    """Safe, actionable diagnostic failure."""


class ProposalError(ValueError):
    """Rejected proposal application; messages contain no source text."""


class ProposalRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    target: str = Field(min_length=1, max_length=800)
    review_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class ProposalApplier:
    """Trusted local operator API. Applying source never executes it."""
    MAX_BYTES = 65536

    def __init__(self, snapshots, tasks):
        if snapshots.guard.workspace != tasks.guard.workspace:
            raise ProposalError('Task and snapshot workspaces must match.')
        self.snapshots = snapshots
        self.tasks = tasks
        self.guard = snapshots.guard

    def _inspect(self, request):
        self.guard.check_running()
        s = self.snapshots
        s._member(request.target)
        parts = PurePosixPath(request.target).parts
        if len(parts) < 3 or parts[0] != 'projects' or not request.target.endswith('.py'):
            raise ProposalError('Target must be a Python file in projects/<name>.')
        project = '/'.join(parts[:2])
        s._project(project, exists=True)
        target = s._path(request.target)
        if not target.parent.is_dir():
            raise ProposalError('Target parent must already exist.')
        s._path(str(target.parent), exists=True)
        original = s._read(target, self.MAX_BYTES) if target.exists() else None
        try:
            before = original.decode('utf-8-sig') if original is not None else ''
        except UnicodeError:
            raise ProposalError('Original file must be bounded UTF-8 text.') from None
        if redact(before) != before:
            raise ProposalError('Recognized sensitive source cannot be previewed or replaced.')
        task = self.tasks.get(request.task_id)
        if task.status != 'completed' or len(task.history) < 2:
            raise ProposalError('Task must have completed static QA.')
        coder, qa = task.history[-2:]
        source = coder.proposal
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        if (coder.role != 'coder' or not source.strip() or qa.role != 'qa' or not qa.qa_passed
                or qa.qa_evidence is None or not qa.qa_evidence.passed or qa.qa_evidence.sha256 != digest):
            raise ProposalError('Matching coder proposal and static-QA evidence required.')
        if redact(source) != source:
            raise ProposalError('Recognized sensitive proposal cannot be applied.')
        # Recheck the exact stored source; never evaluate/import it.
        try:
            compile(ast.parse(source, filename='<proposal>'), '<proposal>', 'exec', dont_inherit=True)
        except (SyntaxError, ValueError, RecursionError):
            raise ProposalError('Proposal no longer passes static QA.') from None
        raw = source.encode('utf-8')
        manifest = {'version': 1, 'task_id': task.task_id, 'target': request.target,
                    'original_sha256': hashlib.sha256(original).hexdigest() if original is not None else None,
                    'proposal_sha256': digest}
        review = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return target, original, raw, before, project, manifest, review

    def preview(self, task_id, target):
        request = ProposalRequest(task_id=task_id, target=target)
        _, original, raw, before, project, manifest, review = self._inspect(request)
        diff = ''.join(difflib.unified_diff(before.splitlines(True), raw.decode().splitlines(True),
                       fromfile='a/' + target, tofile='b/' + target))
        return {**manifest, 'project': project, 'review_digest': review,
                'action': 'unchanged' if original == raw else 'create' if original is None else 'replace',
                'diff': diff, 'code_executed': False, 'functional_correctness_verified': False}

    def apply(self, task_id, target, review_digest):
        request = ProposalRequest(task_id=task_id, target=target, review_digest=review_digest)
        if request.review_digest is None:
            raise ProposalError('Explicit preview digest is required.')
        s = self.snapshots
        # Serialize with other operations using this snapshot service instance.
        with s.lock:
            path, original, raw, _, project, manifest, review = self._inspect(request)
            if review != request.review_digest:
                raise ProposalError('Preview is stale or belongs to a different target/task.')
            if original == raw:
                raise ProposalError('Proposal is already present; no write or backup needed.')
            base = s._path('.jarvis-agent-changes')
            base.mkdir(exist_ok=True)
            s._path(str(base), exists=True)
            locks = s._path(str(base / 'locks'))
            locks.mkdir(exist_ok=True)
            lock = s._path(str(locks / (hashlib.sha256(project.encode()).hexdigest() + '.lock')))
            try:
                lock.mkdir()  # Across processes; stale lock requires operator review, not automatic removal.
            except FileExistsError:
                raise ProposalError('Another application or interrupted operation holds the project lock.') from None
            try:
                self._check_recovery(base, project)
                return self._apply_locked(request, path, original, raw, project, manifest, review, base)
            finally:
                # Only our empty lock directory is removed; never recursive removal.
                lock.rmdir()

    def _check_recovery(self, base, project):
        entries = list(base.iterdir())
        if len(entries) > 101:
            raise ProposalError('Proposal recovery retention limit reached; operator review required.')
        for entry in entries:
            if entry.name == 'locks':
                continue
            self.snapshots._path(str(entry), exists=True)
            if not re.fullmatch(r'[a-f0-9]{64}', entry.name) or not entry.is_dir():
                raise ProposalError('Unexpected recovery entry; operator review required.')
            try:
                record = json.loads(self.snapshots._read(entry / 'journal.json', self.MAX_BYTES))
                if record['status'] not in {'preparing', 'applying', 'applied', 'failed', 'recovery_required'}:
                    raise ValueError()
                if not isinstance(record['project'], str):
                    raise ValueError()
            except (ValueError, KeyError, OSError):
                raise ProposalError('Unreadable recovery journal; operator review required.') from None
            if record['project'] == project and record['status'] in {'preparing', 'applying', 'recovery_required'}:
                raise ProposalError('Project has an unfinished application; inspect recovery before proceeding.')

    def _apply_locked(self, request, path, original, raw, project, manifest, review, base):
        s = self.snapshots
        self._same_target(request.target, original)
        recovery = s._path(str(base / review))
        try:
            recovery.mkdir()  # One-shot claim survives restart and interrupted applies.
        except FileExistsError:
            raise ProposalError('Preview was already consumed; inspect its recovery journal.') from None
        backup_id = None
        journal = {**manifest, 'project': project, 'review_digest': review, 'status': 'preparing',
                   'backup_id': None, 'code_executed': False}
        changed = False
        try:
            s._journal(recovery, journal)
            backup_id = s.create(project)['id']
            journal['backup_id'] = backup_id
            s._journal(recovery, journal)
            s._write(recovery / 'proposal.py', raw)
            if original is not None:
                s._write(recovery / 'original.py', original)
            self._same_target(request.target, original)
            journal['status'] = 'applying'
            s._journal(recovery, journal)
            self.guard.check_running()
            self._same_target(request.target, original)
            destination = s._path(request.target)
            if original is None:
                # Atomic no-overwrite create; a concurrently created file is preserved.
                os.link(s._path(str(recovery / 'proposal.py'), exists=True), destination)
                changed = True
                (recovery / 'proposal.py').unlink()
            else:
                s._replace(recovery / 'proposal.py', destination)
                changed = True
            self._same_target(request.target, raw)
            journal['status'] = 'applied'
            s._journal(recovery, journal)
            return {'status': 'applied', 'target': request.target, 'backup_id': backup_id,
                    'recovery_id': review, 'code_executed': False, 'functional_correctness_verified': False}
        except Exception as exc:
            rollback = 'not_needed'
            if changed:
                try:
                    self.guard.check_running()
                    self._same_target(request.target, raw)
                    if original is None:
                        s._path(request.target, exists=True).unlink()
                    else:
                        s._replace(recovery / 'original.py', s._path(request.target, exists=True))
                    rollback = 'completed'
                except Exception:
                    rollback = 'incomplete'
            journal.update(status='recovery_required' if rollback == 'incomplete' else 'failed', rollback=rollback)
            journal_saved = True
            try:
                s._journal(recovery, journal)
            except Exception:
                # The reported recovery ID remains available if stop/disk error also blocks the journal.
                journal_saved = False
            raise ProposalError(f'Apply failed; rollback={rollback}; journal_saved={journal_saved}; '
                                f'backup_id={backup_id}; recovery_id={review}') from exc

    def _same_target(self, target, expected):
        path = self.snapshots._path(target)
        actual = self.snapshots._read(path, self.MAX_BYTES) if path.exists() else None
        if actual != expected:
            raise ProposalError('Target changed since preview or during application.')


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class DebugRequest(StrictModel):
    error_log: str = Field(default='', max_length=65536)
    log_path: str = Field(default='', max_length=800)
    code_path: str = Field(default='', max_length=800)

    @model_validator(mode='after')
    def inputs(self):
        if self.error_log and self.log_path:
            raise ValueError('Supply log text OR a log path, not both.')
        if not any((self.error_log.strip(), self.log_path, self.code_path)):
            raise ValueError('Supply an error log or a Python source path.')
        return self


class Finding(StrictModel):
    kind: str
    evidence: str
    basis: Literal['parser', 'log_pattern']
    advice: str
    line: int | None = None


class DebugReport(StrictModel):
    mode: Literal['local_read_only'] = 'local_read_only'
    untrusted_content: bool = True
    findings: list[Finding] = Field(default_factory=list, max_length=12)
    log_excerpt: str = ''
    proposed_diff: str = ''
    syntax_after_proposal: bool | None = None
    warnings: list[str] = Field(default_factory=list)
    limitations: str = 'Rule-based hypotheses only; no runtime tests, network, execution or file changes. Redaction is best-effort.'




RULES = {
    'ModuleNotFoundError': 'Check the package name and the Python environment used by the launcher. Do not run install commands copied from logs.',
    'ImportError': 'Check installed versions, import names and circular imports in the selected environment.',
    'NameError': 'Check spelling, scope and whether the name is defined before use.',
    'FileNotFoundError': 'Check the configured path and working directory. Never expand workspace permissions to bypass this error.',
    'PermissionError': 'Check permitted paths and file ownership; do not disable guardrails or request administrator access automatically.',
    'TypeError': 'Compare argument types and the called function signature.',
    'ValueError': 'Check the input format and accepted range.',
    'KeyError': 'Check the available mapping keys and handle missing values explicitly.',
    'AttributeError': 'Check the object type and whether the attribute exists in this library version.',
    'JSONDecodeError': 'Check JSON quotes, commas and encoding. Do not paste credentials into reports.',
    'TimeoutError': 'Check service availability and bounded timeout settings; do not retry indefinitely.',
    'ConnectionError': 'Check the registered service endpoint and whether its process is running.',
    'SyntaxError': 'Inspect the indicated source line; source parsing can confirm syntax only.',
    'IndentationError': 'Check block indentation in the source file.',
    'TabError': 'Use consistent indentation; inspect tabs and spaces before changing code.',
}


class DebugAssistant:
    MAX_BYTES = 65536

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.guard = snapshots.guard

    def _read(self, value: str, suffixes: set[str]) -> str:
        # Explicit projects/<name> scope; no traceback-derived paths are followed.
        self.snapshots._member(value)
        parts = PurePosixPath(value).parts
        if len(parts) < 3 or parts[0] != 'projects':
            raise DebugError('Diagnostic files must be inside projects/<name> in JarvisWorkspace.')
        self.snapshots._project('/'.join(parts[:2]), exists=True)
        if PurePosixPath(value).suffix.lower() not in suffixes:
            raise DebugError('Unsupported diagnostic file extension.')
        raw = self.snapshots._read(self.guard.validate_path(value), self.MAX_BYTES)
        try:
            return raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise DebugError('Diagnostic files must be UTF-8 text.') from None

    def analyze(self, error_log: str = '', log_path: str = '', code_path: str = '') -> dict:
        request = DebugRequest(error_log=error_log, log_path=log_path, code_path=code_path)
        self.guard.check_running()
        log = self._read(request.log_path, {'.log', '.txt'}) if request.log_path else request.error_log
        report = DebugReport()
        cleaned = redact(log)
        report.log_excerpt = '\n'.join(cleaned.splitlines()[-40:])[-4000:]
        if cleaned != log:
            report.warnings.append('Recognized sensitive content/control characters were redacted.')
        seen = set()
        for line in reversed(cleaned.splitlines()):
            match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\s*:', line)
            kind = match[1].rsplit('.', 1)[-1] if match else None
            if kind in RULES and kind not in seen and len(report.findings) < 10:
                seen.add(kind)
                report.findings.append(Finding(kind=kind, evidence=line[:500], basis='log_pattern', advice=RULES[kind]))
        if request.code_path:
            source = self._read(request.code_path, {'.py'})
            self.guard.check_running()
            if len(source.splitlines()) > 2000 or any(len(line) > 4000 for line in source.splitlines()):
                raise DebugError('Python source exceeds diagnostic line limits.')
            try:
                ast.parse(source, filename='<selected-source>')
                compile(source, '<selected-source>', 'exec', dont_inherit=True)
            except SyntaxError as exc:
                report.findings.insert(0, Finding(kind=type(exc).__name__, evidence=redact(exc.msg)[:500],
                    basis='parser', line=exc.lineno, advice='Review this source line; code was parsed but never executed.'))
                self._propose_colon(source, request.code_path, exc, report)
            except (RecursionError, MemoryError, ValueError):
                raise DebugError('Source cannot be safely parsed within diagnostic limits.') from None
            else:
                report.warnings.append('Selected source has valid syntax; this does not verify runtime behavior or fix a log error.')
        if not report.findings:
            report.warnings.append('No supported error pattern found; provide the exception line and an explicitly selected source file.')
        self.guard.check_running()
        return report.model_dump()

    @staticmethod
    def _propose_colon(source, path, exc, report):
        if redact(source) != source:
            report.warnings.append('Diff suppressed because source contains recognized sensitive content.')
            return
        if exc.msg != "expected ':'" or not exc.lineno:
            return
        lines = source.splitlines(keepends=True)
        index = exc.lineno - 1
        try:
            # Locate comments lexically, so a # inside a string is not a comment.
            line = lines[index]
            comment = next((t.start[1] for t in tokenize.generate_tokens(StringIO(line.lstrip()).readline)
                            if t.type == tokenize.COMMENT), None)
            indent = len(line) - len(line.lstrip())
            offset = indent + comment if comment is not None else len(line.rstrip('\r\n'))
            prefix, suffix = line[:offset].rstrip(), line[offset:]
            gap = line[len(prefix):offset]
            lines[index] = prefix + ':' + gap + suffix
            candidate = ''.join(lines)
            ast.parse(candidate, filename='<proposal>')
            compile(candidate, '<proposal>', 'exec', dont_inherit=True)
        except (SyntaxError, tokenize.TokenError, IndentationError, RecursionError, ValueError, IndexError):
            return
        report.proposed_diff = ''.join(difflib.unified_diff(source.splitlines(True), candidate.splitlines(True),
            fromfile='a/' + path, tofile='b/' + path, n=2))
        report.syntax_after_proposal = True
        report.warnings.append('Missing-colon proposal parses successfully; runtime behavior is unverified. No change was applied.')
