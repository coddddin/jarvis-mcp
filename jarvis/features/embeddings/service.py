"""Local text embeddings for semantic search (board 2e3093a2): the worker client, Markdown chunks and RRF.

Embedder sends batches to jarvis/workers/embed_step.py, which only calls 127.0.0.1:11434/api/embed with an
installed local embedding model. Vectors are L2-normalised float32 (cosine = dot product). A model may need a
query instruction (qwen3-embedding); documents are embedded as they are. Failures raise EmbedError with a kind
(unavailable, timeout, http_<status>, invalid) and a Korean reason, never Ollama's own text.
"""
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
import time

import numpy as np

from jarvis.core.paths import WORKERS_ROOT

# Installed with the user's OK (2026-11-17). query_prefix follows each model card's retrieval instruction.
MODELS = {
    'qwen3-embedding:0.6b': {'query_prefix': 'Instruct: Given a question, retrieve passages from personal notes '
                                             'and documents that answer it\nQuery: '},
    'bge-m3:latest': {'query_prefix': ''},
}
BATCH = 16                      # texts per worker call
MAX_CHARS = 2000                # characters of one text sent to the model (the worker cuts there too)
KEEP_ALIVE = '2m'               # short: the 16 GB GPU is shared with the chat model, TTS and STT (ADR 0001)
CALL_SECONDS = 120              # one batch, a model load included
RRF_K = 60


class EmbedError(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def spawn_embed_worker():
    return subprocess.Popen([sys.executable, '-I', str(WORKERS_ROOT / 'embed_step.py')],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={key: value for key, value in os.environ.items()
                                 if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}},
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


class Embedder:
    """embed(texts, query=False) -> float32 array (len(texts), dim), rows L2-normalised."""

    def __init__(self, guard, model, *, spawn=spawn_embed_worker, batch=BATCH):
        if model not in MODELS:
            raise EmbedError('unavailable', f'지원하지 않는 임베딩 모델입니다: {model}')
        self.guard, self.model, self.spawn, self.batch = guard, model, spawn, batch

    def embed(self, texts, *, query=False):
        prefix = MODELS[self.model]['query_prefix'] if query else ''
        rows = []
        for start in range(0, len(texts), self.batch):
            self.guard.check_running()
            rows.extend(self._call([(prefix + text)[:MAX_CHARS] for text in texts[start:start + self.batch]]))
        if not rows:
            return np.zeros((0, 0), dtype=np.float32)
        matrix = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if not np.all(norms > 0):
            raise EmbedError('invalid', '임베딩 모델이 빈 벡터를 돌려주었습니다.')
        return matrix / norms

    def _call(self, texts):
        payload = json.dumps({'model': self.model, 'input': texts, 'keep_alive': KEEP_ALIVE}).encode('utf-8')
        process = self.spawn()
        try:
            deadline, pending = time.monotonic() + CALL_SECONDS, payload
            while True:
                self.guard.check_running()
                if time.monotonic() > deadline:
                    raise EmbedError('timeout', '임베딩 모델이 제한 시간 안에 답하지 못했습니다.')
                try:
                    output, _ = process.communicate(pending, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pending = None
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        try:
            value = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise EmbedError('invalid', '임베딩 작업자가 올바르지 않은 응답을 보냈습니다.') from None
        if not isinstance(value, dict) or value.get('ok') is not True or process.returncode:
            kind = value.get('kind') if isinstance(value, dict) else None
            kind = kind if isinstance(kind, str) and re.fullmatch(r'timeout|http_[1-5][0-9][0-9]', kind) else 'unavailable'
            raise EmbedError(kind, '로컬 임베딩 모델을 쓸 수 없습니다. Ollama와 모델 설치 상태를 확인해 주십시오.')
        vectors = value.get('embeddings')
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbedError('invalid', '임베딩 작업자가 올바르지 않은 응답을 보냈습니다.')
        return vectors


def to_blob(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blobs(blobs, dim):
    """Stacked float32 rows from stored blobs; a blob of the wrong size raises ValueError."""
    if any(len(blob) != dim * 4 for blob in blobs):
        raise ValueError('Stored vector has the wrong size.')
    return np.frombuffer(b''.join(blobs), dtype=np.float32).reshape(len(blobs), dim) if blobs else \
        np.zeros((0, dim), dtype=np.float32)


def rrf(rankings, k=RRF_K):
    """Reciprocal rank fusion: {key: sum 1/(k + rank)} over rankings (lists of keys, best first)."""
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


# --- Markdown heading chunks -------------------------------------------------------------------------------------
HEADING = re.compile(r'^(#{1,6})[ \t]+(.+?)[ \t#]*$')
FENCE = re.compile(r'^(```|~~~)')
TARGET_CHARS, MAX_CHUNK_CHARS = 1000, 1200


@dataclass(frozen=True)
class Chunk:
    line: int           # 1-based first line of the chunk in the file
    heading: str        # heading path, e.g. 'Guide > Setup' ('' before the first heading)
    text: str


def chunk_markdown(text, *, target=TARGET_CHARS, limit=MAX_CHUNK_CHARS):
    """Split Markdown at headings (outside code fences) and keep the heading path. Every chunk is a verbatim slice
    of text (callers check excerpts against the file). A section longer than limit is grouped by blank-line
    paragraphs into pieces near target; a paragraph longer than limit is cut by characters. Frontmatter stays in the
    first chunk, so titles and tags remain searchable and line 1 is cited."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    front = 0                       # '# ...' inside frontmatter is a YAML comment, not a heading
    if lines and lines[0].rstrip('\r\n') == '---':
        front = next((i + 1 for i in range(1, min(len(lines), 65)) if lines[i].rstrip('\r\n') == '---'), 0)
    sections, path, fenced, begin, heading = [], [], False, 0, ''
    for number in range(front, len(lines)):
        line = lines[number].rstrip('\r\n')
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            # Frontmatter with nothing after it joins the first heading's section (one source per short note).
            if not (begin == 0 and not ''.join(lines[front:number]).strip()):
                sections.append((begin, number, heading))
                begin = number
            level = len(match[1])
            path = path[:level - 1] + [''] * max(0, level - 1 - len(path)) + [match[2].strip()]
            heading = ' > '.join(part for part in path if part)
    sections.append((begin, len(lines), heading))
    chunks = []
    for begin, end, heading in sections:
        if offsets[end] - offsets[begin] <= limit:
            chunks.extend(_slice(text, offsets[begin], offsets[end], heading, limit))
            continue
        # Close a piece at a blank line once it reaches target, or before the next paragraph would pass limit.
        breaks = [number for number in range(begin + 1, end) if not lines[number].strip()] + [end]
        piece = begin
        for index, number in enumerate(breaks):
            following = breaks[index + 1] if index + 1 < len(breaks) else None
            if (number == end or offsets[number] - offsets[piece] >= target
                    or (following is not None and offsets[following] - offsets[piece] > limit)):
                if offsets[number] > offsets[piece]:
                    chunks.extend(_slice(text, offsets[piece], offsets[number], heading, limit))
                piece = number
    return chunks


def _slice(text, start, end, heading, limit):
    """Chunks of text[start:end] without leading/trailing blank space, each at most limit characters."""
    body = text[start:end]
    start += len(body) - len(body.lstrip())
    end -= len(body) - len(body.rstrip())
    out = []
    while start < end:
        stop = min(end, start + limit)
        out.append(Chunk(text.count('\n', 0, start) + 1, heading, text[start:stop]))
        start = stop
    return out


def chunk_plain(text, *, size=MAX_CHUNK_CHARS, step=1000):
    """Today's fixed window for .txt/.rst: 1200 characters every 1000."""
    chunks, offset = [], 0
    while offset < len(text):
        part = text[offset:offset + size]
        if part.strip():
            chunks.append(Chunk(text.count('\n', 0, offset) + 1, '', part))
        offset += step
    return chunks
