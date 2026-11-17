"""Fixed local embedding worker. No tool execution, credential access or project imports.

Reads one JSON object on stdin: {"model": "<installed local tag>", "input": ["text", ...], "keep_alive": "2m"} and
prints one line: {"ok": true, "embeddings": [[float, ...], ...], "dim": n} or {"ok": false, "error": <exception
name>, "kind"?: http_<status> | timeout}. Only POST 127.0.0.1:11434/api/embed is called, after the same local-model
checks as the chat worker (named tag, installed, gguf, no remote host) plus an 'embedding' capability. Inputs are
bounded (MAX_INPUTS texts of MAX_CHARS each) and the reply is size-capped. Ollama's error text never leaves.
"""
import http.client
import json
import math
import re
import sys

MAX_INPUTS = 32
MAX_CHARS = 2000
MAX_REPLY = 8 * 1024 * 1024              # 32 vectors of 4096 floats as JSON stay far below this
MAX_DIM = 4096
KEEP_ALIVE = re.compile(r'^[1-9][0-9]?[sm]$')
MODEL_TAG = re.compile(r'^[a-zA-Z0-9_.-]+:[a-zA-Z0-9_.-]+$')


class OllamaError(ValueError):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def request(path, body=None, timeout=120):
    if path not in {'/api/tags', '/api/show', '/api/embed'}:
        raise ValueError('Endpoint not allowed.')
    connection = http.client.HTTPConnection('127.0.0.1', 11434, timeout=timeout)
    try:
        connection.request('GET' if body is None else 'POST', path,
                           None if body is None else json.dumps(body).encode('utf-8'),
                           {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Local Ollama returned an encoded response.')
        data = response.read(MAX_REPLY + 1)
        if response.status != 200:
            raise OllamaError(f'http_{response.status}')
        if len(data) > MAX_REPLY:
            raise ValueError('Local response exceeds size limit.')
        return json.loads(data)
    finally:
        connection.close()


def local_embedding_model(model):
    if not isinstance(model, str) or not MODEL_TAG.fullmatch(model) or 'cloud' in model.lower():
        raise ValueError('Only a named local model tag is allowed.')
    tags = request('/api/tags')
    if not any(item.get('name') == model for item in tags.get('models', [])):
        raise ValueError('Configured model is not installed; automatic downloads are disabled.')
    info = request('/api/show', {'model': model})
    if info.get('remote_host') or info.get('remote_model') or info.get('details', {}).get('format') != 'gguf':
        raise ValueError('Cloud or unknown model metadata is refused.')
    if 'embedding' not in (info.get('capabilities') or []):
        raise ValueError('The model is not an embedding model.')


def embed(payload):
    texts = payload.get('input')
    if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_INPUTS
            or not all(isinstance(text, str) and text.strip() for text in texts)):
        raise ValueError('input must be 1 to 32 non-empty strings.')
    keep_alive = payload.get('keep_alive', '2m')
    if not isinstance(keep_alive, str) or not KEEP_ALIVE.fullmatch(keep_alive):
        raise ValueError('keep_alive must look like 30s or 2m.')
    model = payload.get('model')
    local_embedding_model(model)
    data = request('/api/embed', {'model': model, 'input': [text[:MAX_CHARS] for text in texts],
                                  'truncate': True, 'keep_alive': keep_alive})
    vectors = data.get('embeddings')
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise ValueError('Unexpected embedding response.')
    dim = len(vectors[0]) if vectors and isinstance(vectors[0], list) else 0
    if not 1 <= dim <= MAX_DIM:
        raise ValueError('Unexpected embedding size.')
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != dim
                or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector)):
            raise ValueError('Unexpected embedding values.')
    return {'ok': True, 'embeddings': vectors, 'dim': dim}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUTS * MAX_CHARS * 4 + 4097)
        if len(raw) > MAX_INPUTS * MAX_CHARS * 4 + 4096:
            raise ValueError('Embedding request too large.')
        result = embed(json.loads(raw))
    except Exception as exc:
        kind = exc.kind if isinstance(exc, OllamaError) else 'timeout' if isinstance(exc, TimeoutError) else None
        sys.stdout.write(json.dumps({'ok': False, 'error': type(exc).__name__, **({'kind': kind} if kind else {})})
                         + '\n')
        return 1
    sys.stdout.write(json.dumps(result) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
