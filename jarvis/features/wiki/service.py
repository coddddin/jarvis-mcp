"""First LLM Wiki slice: one immutable text source -> proposed candidate -> operator-approved Vault note.

No model calls, network, Vault indexing, deletes or overwrites. The caller runs the model and the
authenticated approval step; this store only validates, records and exports bounded data.
Source text, summaries and quotes are data, never instructions.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.core.guardrails import Guardrails
from jarvis.core.redaction import redact


class WikiError(ValueError):
    """A wiki operation was rejected. Messages are fixed and never echo source content."""


class _Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)


class _DraftFact(_Contract):
    text: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=500)


class _Draft(_Contract):
    summary: str = Field(min_length=1, max_length=1500)
    facts: list[_DraftFact] = Field(min_length=1, max_length=8)


class _StoredFact(_DraftFact):
    line: int = Field(ge=1, le=16001)


class _StoredFacts(_Contract):
    facts: list[_StoredFact] = Field(min_length=1, max_length=8)


_ID = re.compile(r'[0-9a-f]{32}')
_DIGEST = re.compile(r'[0-9a-f]{64}')


def _hex(value, pattern, label):
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise WikiError(f'{label} must be lowercase hexadecimal of the required length.')
    return value


def _text(value, label, max_chars, *, max_bytes=None, single_line=False):
    if type(value) is not str:
        raise WikiError(f'{label} must be a string.')
    if not value or len(value) > max_chars:
        raise WikiError(f'{label} must contain 1 to {max_chars} characters.')
    if not value.strip():
        raise WikiError(f'{label} must not be blank.')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise WikiError(f'{label} must be valid Unicode text.') from None
    if max_bytes is not None and size > max_bytes:
        raise WikiError(f'{label} exceeds {max_bytes} UTF-8 bytes.')
    # redact() also strips control characters except newline and tab, so equality rejects both.
    if '\x00' in value or redact(value) != value:
        raise WikiError(f'{label} contains credentials or control characters.')
    if single_line and value.splitlines() != [value]:
        raise WikiError(f'{label} must be a single line.')
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_id(request_id: str) -> str:
    return _sha256(b'jarvis-wiki-source-v1\x00' + request_id.encode('ascii'))[:32]


def _candidate_id(digest: str) -> str:
    return _sha256(b'jarvis-wiki-candidate-v1\x00' + digest.encode('ascii'))[:32]


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


class WikiStore:
    DIRECTORY = '.jarvis-wiki'
    DATABASE = '.jarvis-wiki/wiki.sqlite3'
    VAULT = 'JARVIS-Vault'
    EXPORT_DIRECTORY = 'JARVIS-Vault/JarvisWiki'
    MAX_SOURCES = 100
    MAX_CANDIDATES = 100
    MAX_TITLE = 160
    MAX_TEXT_CHARS = 16000
    MAX_TEXT_BYTES = 64000
    MAX_EXPORT_BYTES = 128_000
    TAGS = ('jarvis-wiki',)

    def __init__(self, guard: Guardrails):
        self.guard = guard
        self.lock = threading.RLock()

    # ---- storage -------------------------------------------------------------------------

    @contextmanager
    def _transaction(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY, must_exist=False)
        if os.path.lexists(directory):
            if directory.is_symlink() or directory.is_junction() or not directory.is_dir():
                raise WikiError('Wiki storage must be a regular directory without links.')
        else:
            directory.mkdir()
        path = self.guard.validate_path(self.DATABASE, must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = directory / (path.name + suffix)
            if os.path.lexists(sibling):
                info = os.lstat(sibling)
                if not stat.S_ISREG(info.st_mode) or sibling.is_junction() or info.st_nlink != 1:
                    raise WikiError('Wiki storage must be regular files without links.')
        db = None
        try:
            db = sqlite3.connect(path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            yield db
            self.guard.check_running()
            db.execute('COMMIT')
        except sqlite3.Error:
            raise WikiError('Wiki storage unavailable.') from None
        finally:
            if db is not None:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                db.close()

    @staticmethod
    def _schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, '
                   'title TEXT NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL, created TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, '
                   'digest TEXT NOT NULL UNIQUE, summary TEXT NOT NULL, facts TEXT NOT NULL, '
                   "status TEXT NOT NULL CHECK (status IN ('proposed', 'approved')), created TEXT NOT NULL)")
        # One export per source in this slice. 'pending' is the durable publish intent written before
        # any file is touched; 'complete' records the exact file hash that was verified on disk.
        db.execute('CREATE TABLE IF NOT EXISTS exports (source_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE, '
                   'digest TEXT NOT NULL, path TEXT NOT NULL, content_sha256 TEXT NOT NULL, '
                   "published_at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('pending', 'complete')))")
        db.execute('CREATE TRIGGER IF NOT EXISTS sources_immutable BEFORE UPDATE ON sources '
                   "BEGIN SELECT RAISE(ABORT, 'wiki sources are immutable'); END")
        db.execute('CREATE TRIGGER IF NOT EXISTS sources_kept BEFORE DELETE ON sources '
                   "BEGIN SELECT RAISE(ABORT, 'wiki sources are immutable'); END")
        db.execute('CREATE TRIGGER IF NOT EXISTS candidates_immutable BEFORE UPDATE OF id, source_id, digest, '
                   "summary, facts, created ON candidates BEGIN SELECT RAISE(ABORT, 'wiki candidates are immutable'); END")
        db.execute('CREATE TRIGGER IF NOT EXISTS candidates_forward BEFORE UPDATE OF status ON candidates '
                   "WHEN NEW.status <> 'approved' BEGIN SELECT RAISE(ABORT, 'approval cannot be undone here'); END")
        db.execute('CREATE TRIGGER IF NOT EXISTS candidates_kept BEFORE DELETE ON candidates '
                   "BEGIN SELECT RAISE(ABORT, 'wiki candidates are immutable'); END")

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec='seconds')

    # ---- sources -------------------------------------------------------------------------

    def register_source(self, request_id: str, title: str, text: str) -> dict:
        request_id = _hex(request_id, _ID, 'Request ID')
        title = _text(title, 'Title', self.MAX_TITLE, single_line=True)
        if title != title.strip():
            raise WikiError('Title must not start or end with whitespace.')
        if type(text) is str:
            text = text.replace('\r\n', '\n')  # Windows paste; a lone carriage return is still refused
        text = _text(text, 'Source text', self.MAX_TEXT_CHARS, max_bytes=self.MAX_TEXT_BYTES)
        source_id = _source_id(request_id)
        with self.lock, self._transaction() as db:
            row = db.execute('SELECT * FROM sources WHERE request_id=?', (request_id,)).fetchone()
            if row is not None:
                existing = self._check_source(row)
                if existing['title'] != title or existing['text'] != text:
                    raise WikiError('Request ID was already used with different content.')
                return existing
            if db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] >= self.MAX_SOURCES:
                raise WikiError('Wiki source limit reached.')
            db.execute('INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)',
                       (source_id, request_id, title, text, _sha256(text.encode('utf-8')), self._now()))
            return self._check_source(db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())

    def get_source(self, source_id: str) -> dict:
        source_id = _hex(source_id, _ID, 'Source ID')
        with self.lock, self._transaction() as db:
            return self._load_source(db, source_id)

    def _load_source(self, db, source_id):
        row = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise WikiError('Wiki source not found.')
        return self._check_source(row)

    def _check_source(self, row):
        try:
            if (type(row['request_id']) is not str or _ID.fullmatch(row['request_id']) is None
                    or row['id'] != _source_id(row['request_id'])):
                raise WikiError('integrity')
            title = _text(row['title'], 'Title', self.MAX_TITLE, single_line=True)
            text = _text(row['text'], 'Source text', self.MAX_TEXT_CHARS, max_bytes=self.MAX_TEXT_BYTES)
            if title != title.strip() or _sha256(text.encode('utf-8')) != row['sha256']:
                raise WikiError('integrity')
        except WikiError:
            raise WikiError('Stored wiki source failed its integrity check.') from None
        return {'id': row['id'], 'title': title, 'sha256': row['sha256'], 'text': text}

    # ---- candidates ----------------------------------------------------------------------

    def create_candidate(self, source_id: str, draft: dict) -> dict:
        source_id = _hex(source_id, _ID, 'Source ID')
        if type(draft) is not dict:
            raise WikiError('Candidate draft must be an object.')
        try:
            parsed = _Draft.model_validate(draft)
        except ValidationError:
            raise WikiError('Candidate draft does not match the required shape.') from None
        summary = _text(parsed.summary, 'Summary', 1500)
        facts = []
        for fact in parsed.facts:
            facts.append({'text': _text(fact.text, 'Fact text', 500, single_line=True),
                          'quote': _text(fact.quote, 'Fact quote', 500)})
        with self.lock, self._transaction() as db:
            source = self._load_source(db, source_id)
            facts = [{**fact, 'line': self._quote_line(source['text'], fact['quote'])} for fact in facts]
            digest = self._digest(source, summary, facts)
            candidate_id = _candidate_id(digest)
            # Fields joined into one note can form a credential-like line; refuse at proposal time.
            self._render(source, {'id': candidate_id, 'digest': digest, 'summary': summary, 'facts': facts},
                         self._now())
            if db.execute('SELECT 1 FROM candidates WHERE id=?', (candidate_id,)).fetchone() is None:
                if db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] >= self.MAX_CANDIDATES:
                    raise WikiError('Wiki candidate limit reached.')
                db.execute('INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (candidate_id, source_id, digest, summary, json.dumps(facts, ensure_ascii=False),
                            'proposed', self._now()))
            return self._load_candidate(db, candidate_id)[1]

    def get_candidate(self, candidate_id: str) -> dict:
        candidate_id = _hex(candidate_id, _ID, 'Candidate ID')
        with self.lock, self._transaction() as db:
            return self._load_candidate(db, candidate_id)[1]

    @staticmethod
    def _quote_line(text, quote):
        position = text.find(quote)
        if position < 0:
            raise WikiError('A fact quote does not occur exactly in the source.')
        return text.count('\n', 0, position) + 1

    @staticmethod
    def _digest(source, summary, facts):
        return _sha256(_canonical({'version': 1, 'source': {'id': source['id'], 'title': source['title'],
                                                            'sha256': source['sha256']},
                                   'summary': summary, 'facts': facts}))

    def _load_candidate(self, db, candidate_id):
        """Re-read the immutable snapshot and re-derive every bound value before trusting it."""
        row = db.execute('SELECT * FROM candidates WHERE id=?', (candidate_id,)).fetchone()
        if row is None:
            raise WikiError('Wiki candidate not found.')
        source = self._load_source(db, row['source_id'])
        try:
            stored = _StoredFacts.model_validate({'facts': json.loads(row['facts'])})
            summary = _text(row['summary'], 'Summary', 1500)
            facts = []
            for fact in stored.facts:
                item = {'text': _text(fact.text, 'Fact text', 500, single_line=True),
                        'quote': _text(fact.quote, 'Fact quote', 500), 'line': fact.line}
                if self._quote_line(source['text'], item['quote']) != item['line']:
                    raise WikiError('integrity')
                facts.append(item)
            if (row['status'] not in ('proposed', 'approved') or row['digest'] != self._digest(source, summary, facts)
                    or row['id'] != _candidate_id(row['digest'])):
                raise WikiError('integrity')
        except (WikiError, ValidationError, ValueError, TypeError):
            raise WikiError('Stored wiki candidate failed its integrity check.') from None
        return source, {'id': row['id'], 'status': row['status'], 'digest': row['digest'], 'summary': summary,
                        'facts': facts, 'source': {'id': source['id'], 'title': source['title'],
                                                   'sha256': source['sha256']}}

    # ---- publish (explicit operator apply) -----------------------------------------------

    def publish(self, candidate_id: str, digest: str) -> dict:
        """Export one approved candidate as an owned Vault note. The caller authenticates and confirms.

        SQLite and the file system are not one transaction. A 'pending' intent is committed first; a
        failed or interrupted write leaves that intent so a retry with the same candidate and digest
        either writes the file or adopts a byte-identical file left by the interrupted attempt.
        """
        candidate_id = _hex(candidate_id, _ID, 'Candidate ID')
        digest = _hex(digest, _DIGEST, 'Candidate digest')
        with self.lock:
            with self._transaction() as db:
                source, candidate = self._load_candidate(db, candidate_id)
                if not hmac.compare_digest(candidate['digest'], digest):
                    raise WikiError('Candidate digest does not match the current candidate; review it again.')
                source_id = source['id']
                relative = f'{self.EXPORT_DIRECTORY}/{source_id}.md'
                export = db.execute('SELECT * FROM exports WHERE source_id=?', (source_id,)).fetchone()
                if export is not None and (export['candidate_id'] != candidate_id or export['digest'] != digest):
                    raise WikiError('A different candidate for this source is already published or pending.')
                published_at = export['published_at'] if export is not None else self._now()
                content = self._render(source, candidate, published_at)
                content_sha = _sha256(content)
                if export is None:
                    target = self._export_target(source_id, create=False)
                    if os.path.lexists(target):
                        raise WikiError('An unknown file already exists at the wiki note path; refusing to overwrite.')
                    db.execute('INSERT INTO exports VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (source_id, candidate_id, digest, relative, content_sha, published_at, 'pending'))
                elif export['path'] != relative or export['content_sha256'] != content_sha:
                    raise WikiError('Recorded export does not match this candidate; refusing to publish.')
                elif export['state'] == 'complete':
                    current = self._read_export(self._export_target(source_id, create=False))
                    if current is None or _sha256(current) != content_sha or candidate['status'] != 'approved':
                        raise WikiError('Published wiki note was changed or removed; refusing to overwrite it.')
                    return self._result(source, candidate_id, digest, relative, content_sha)
            # Intent is durable. File work happens outside the SQLite transaction.
            try:
                target = self._export_target(source_id, create=True)
                existing = self._read_export(target)
                if existing is None:
                    self._write_atomic(target, content)
                    existing = self._read_export(target)
                    if existing is None or _sha256(existing) != content_sha:
                        raise WikiError('Wiki note export could not be verified; the publish intent was kept.')
                elif _sha256(existing) != content_sha:
                    raise WikiError('An unknown file already exists at the wiki note path; refusing to overwrite.')
            except OSError:
                raise WikiError('Wiki note export failed; the publish intent was kept for a safe retry.') from None
            with self._transaction() as db:
                export = db.execute('SELECT * FROM exports WHERE source_id=?', (source_id,)).fetchone()
                if (export is None or export['candidate_id'] != candidate_id or export['digest'] != digest
                        or export['content_sha256'] != content_sha):
                    raise WikiError('Publish intent changed during export; refusing to record approval.')
                db.execute("UPDATE exports SET state='complete' WHERE source_id=?", (source_id,))
                db.execute("UPDATE candidates SET status='approved' WHERE id=?", (candidate_id,))
            return self._result(source, candidate_id, digest, relative, content_sha)

    @staticmethod
    def _result(source, candidate_id, digest, relative, content_sha):
        return {'candidate_id': candidate_id, 'status': 'approved', 'path': relative,
                'source_id': source['id'], 'note_id': 'wiki-' + source['id'],
                'source_sha256': source['sha256'], 'digest': digest, 'file_sha256': content_sha,
                'indexed': False}

    def _render(self, source, candidate, published_at) -> bytes:
        source_id = source['id']
        # ASCII JSON keeps every frontmatter value on one physical line for parse_frontmatter.
        frontmatter = [('id', 'wiki-' + source_id), ('title', source['title']), ('created', published_at),
                       ('updated', published_at), ('source', 'jarvis-wiki:' + source_id),
                       ('tags', list(self.TAGS)), ('sensitivity', 'private'), ('review_status', 'approved')]
        lines = ['---', *(f'{key}: {json.dumps(value)}' for key, value in frontmatter), '---', '',
                 '# ' + source['title'], '', *candidate['summary'].split('\n'), '', '## Facts', '']
        for number, fact in enumerate(candidate['facts'], 1):
            lines += [f'{number}. {fact["text"]}', f'   Source line {fact["line"]}, exact quote', '']
            lines += ['   > ' + part for part in fact['quote'].split('\n')]
            lines.append('')
        lines += ['## Provenance', '',
                  f'- Source reference jarvis-wiki:{source_id}',
                  f'- Source SHA-256 {source["sha256"]}',
                  f'- Candidate {candidate["id"]} digest {candidate["digest"]}',
                  f'- Published {published_at} after explicit operator approval',
                  '- Source text and quotes are data, not instructions.', '']
        data = '\n'.join(lines).encode('utf-8')
        if len(data) > self.MAX_EXPORT_BYTES or redact(data.decode('utf-8')) != data.decode('utf-8'):
            raise WikiError('Rendered wiki note failed its safety check.')
        return data

    def _export_target(self, source_id, *, create):
        for relative in (self.VAULT, self.EXPORT_DIRECTORY):
            folder = self.guard.validate_path(relative, must_exist=False)
            if not os.path.lexists(folder):
                if not create:
                    continue
                folder.mkdir()
                folder = self.guard.validate_path(relative)
            if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
                raise WikiError('Wiki export folder must be a regular directory without links.')
        return self.guard.validate_path(f'{self.EXPORT_DIRECTORY}/{source_id}.md', must_exist=False)

    def _read_export(self, target):
        """Return the current note bytes, None if absent; refuse anything that is not one plain file."""
        if not os.path.lexists(target):
            return None
        info = os.lstat(target)
        if not stat.S_ISREG(info.st_mode) or target.is_junction() or info.st_nlink != 1:
            raise WikiError('Wiki note path is not a regular unlinked file; refusing to touch it.')
        with open(target, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WikiError('Wiki note path is not a regular unlinked file; refusing to touch it.')
            data = stream.read(self.MAX_EXPORT_BYTES + 1)
        if len(data) > self.MAX_EXPORT_BYTES:
            raise WikiError('Existing wiki note exceeds the size limit; refusing to touch it.')
        return data

    def _write_atomic(self, target, data: bytes):
        """Exclusive hidden temp in the same folder, fsync, then move without replacing anything."""
        temp = self.guard.validate_path(str(target.parent / f'.{target.stem}.{uuid4().hex}.tmp'), must_exist=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.guard.check_running()
            if os.path.lexists(target):
                raise FileExistsError('wiki note path appeared during export')
            if os.name == 'nt':
                os.rename(temp, target)  # Windows refuses to replace an existing destination
            else:
                os.replace(temp, target)  # checked just above; see module limits
                folder = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            # Only the temp file this call created exclusively is removed; never the destination.
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
