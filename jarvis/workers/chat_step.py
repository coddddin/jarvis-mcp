"""Fixed local conversation inference worker. No tool execution or credential access.

With "stream": true in the payload it reads Ollama's NDJSON stream and prints {"delta": text} lines with the
newly decoded part of the JSON "reply" string as it arrives, then the same final {"ok", "result"} line as the
non-streaming path. The guards are the same: named local model only, 256 KB response cap, done/done_reason check.

With "warm": true it only asks Ollama to load the model (empty messages, same options so the later chat does not
reload it) and prints {"ok": true, "result": {}}. Nothing is generated.

A failed call prints {"ok": false, "error": <exception name>} and, when known, "kind": repeat_limit (Ollama aborted a
generation that repeated one token), http_<status>, stream_error or timeout. Ollama's own error text never leaves.
"""
import http.client
import json
import importlib.util
from pathlib import Path
import re
import sys

# -I deliberately removes the script directory from import search. Load only this
# fixed, trusted sibling to share the existing offline-model checks.
spec = importlib.util.spec_from_file_location('local_agent_step', Path(__file__).with_name('agent_step.py'))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)

# Same options for chat and warm-up: a different num_ctx would make Ollama reload the model.
OPTIONS = {'temperature': 0.2, 'num_predict': 1024, 'num_ctx': 8192}
KEEP_ALIVE = '5m'                                       # ADR 0001: free the GPU after 5 idle minutes
REPLY_START = re.compile(r'\s*\{\s*"reply"\s*:\s*"')
REPEAT_LIMIT = re.compile(r'repeat limit', re.I)
ESCAPES = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}


def reply_prefix(content):
    """The decoded part of the leading "reply" string that is complete so far, or '' when the JSON does not start
    with it. Stops before an unfinished escape (including half of a surrogate pair) and at the closing quote."""
    start = REPLY_START.match(content)
    if not start:
        return ''
    out, i = [], start.end()
    while i < len(content):
        char = content[i]
        if char == '"':
            break
        if char != '\\':
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(content):
            break
        kind = content[i + 1]
        if kind in ESCAPES:
            out.append(ESCAPES[kind])
            i += 2
            continue
        if kind != 'u' or i + 6 > len(content):
            break
        try:
            code = int(content[i + 2:i + 6], 16)
        except ValueError:
            break
        if 0xD800 <= code < 0xDC00:  # high surrogate: wait for its pair
            if i + 12 > len(content) or content[i + 6:i + 8] != '\\u':
                break
            try:
                low = int(content[i + 8:i + 12], 16)
            except ValueError:
                break
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)) if 0xDC00 <= low < 0xE000 else '�')
            i += 12
            continue
        out.append(chr(code))
        i += 6
    return ''.join(out)


class OllamaError(ValueError):
    """Ollama answered with an error; kind is repeat_limit, http_<status> or stream_error."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def error_kind(status, data):
    """repeat_limit when Ollama's error says so, otherwise http_<status>. Only the kind is kept."""
    try:
        message = json.loads(data).get('error')
    except (ValueError, AttributeError):
        message = None
    return 'repeat_limit' if isinstance(message, str) and REPEAT_LIMIT.search(message) else f'http_{status}'


def chat(body):
    """POST /api/chat without streaming: the same endpoint, host and size cap as support.request, but a non-success
    answer raises OllamaError with its kind."""
    connection = http.client.HTTPConnection('127.0.0.1', 11434, timeout=120)
    try:
        connection.request('POST', '/api/chat', json.dumps(body).encode('utf-8'),
                           {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Local Ollama returned an encoded response.')
        data = response.read(support.MAX_BYTES + 1)
        if response.status != 200:
            raise OllamaError(error_kind(response.status, data[:4096]))
        if len(data) > support.MAX_BYTES:
            raise ValueError('Local response exceeds size limit.')
        return json.loads(data)
    finally:
        connection.close()


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True) + '\n')
    sys.stdout.flush()


def stream_chat(body):
    """POST /api/chat with stream:true; yields each parsed NDJSON chunk. Same endpoint, host and size cap."""
    connection = http.client.HTTPConnection('127.0.0.1', 11434, timeout=120)
    try:
        connection.request('POST', '/api/chat', json.dumps(body).encode('utf-8'),
                           {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Local Ollama returned an encoded response.')
        if response.status != 200:
            raise OllamaError(error_kind(response.status, response.read(4096)))
        total = 0
        while True:
            line = response.readline(support.MAX_BYTES + 1)
            if not line:
                return
            total += len(line)
            if total > support.MAX_BYTES:
                raise ValueError('Local response exceeds size limit.')
            if line.strip():
                chunk = json.loads(line)
                if isinstance(chunk, dict) and 'error' in chunk:      # Ollama reports a mid-stream failure in-band
                    raise OllamaError('repeat_limit' if REPEAT_LIMIT.search(str(chunk['error'])) else 'stream_error')
                yield chunk
    finally:
        connection.close()


def finish(data, content):
    message = data.get('message') or {}
    if (data.get('done') is not True or data.get('done_reason') == 'length'
            or message.get('role', 'assistant') != 'assistant' or message.get('tool_calls')):
        raise ValueError('Incomplete or unexpected model response.')
    return json.loads(content)


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Chat context too large.')
        payload = json.loads(raw)
        support.local_model(payload['model'])
        if payload.get('warm') is True:
            data = support.request('/api/chat', {'model': payload['model'], 'messages': [], 'stream': False,
                                                 'options': OPTIONS, 'keep_alive': KEEP_ALIVE})
            if data.get('done') is not True:
                raise ValueError('Model did not load.')
            emit({'ok': True, 'result': {}})
            return 0
        body = {'model': payload['model'], 'stream': payload.get('stream') is True,
                'think': False, 'format': payload['schema'], 'messages': payload['messages'],
                'options': OPTIONS, 'keep_alive': KEEP_ALIVE}
        if not body['stream']:
            data = chat(body)
            message = data['message']
            if message.get('role') != 'assistant':
                raise ValueError('Incomplete or unexpected model response.')
            result = finish(data, message['content'])
        else:
            content, shown, last = '', '', None
            for chunk in stream_chat(body):
                message = chunk.get('message') or {}
                if message.get('role', 'assistant') != 'assistant' or message.get('tool_calls'):
                    raise ValueError('Incomplete or unexpected model response.')
                content += message.get('content') or ''
                text = reply_prefix(content)
                if len(text) > len(shown):
                    emit({'delta': text[len(shown):]})
                    shown = text
                last = chunk
                if chunk.get('done') is True:
                    break
            if last is None:
                raise ValueError('Incomplete or unexpected model response.')
            result = finish(last, content)
        emit({'ok': True, 'result': result})
        return 0
    except Exception as exc:
        kind = exc.kind if isinstance(exc, OllamaError) else 'timeout' if isinstance(exc, TimeoutError) else None
        emit({'ok': False, 'error': type(exc).__name__, **({'kind': kind} if kind else {})})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
