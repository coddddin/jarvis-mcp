"""HUD route for the Ollama card: the last observation of the fixed local model server, read only.

Same gate as the other HUD APIs: bearer token from the connection file, exact local Origin, matching Host,
no query string. The gate runs before anything touches the network. The only probe is one HTTP GET /api/tags
on 127.0.0.1:11434 (the endpoint the existing worker already uses) for the configured chat model: no
redirects, proxies, credentials, downloads, generation, warm-up or processes. A listed tag means the model
is installed, never that it is loaded, ready for inference or that chat will succeed. The socket timeout
bounds each blocking socket operation, not the whole probe. Raw bodies, error text and the rest of the
installed inventory never leave this module. Results are kept in this process's memory for CACHE_SECONDS.
"""
import asyncio
import http.client
import json
import threading
import time
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from jarvis.agents.runner import LocalConfig

HOST, PORT, PATH = '127.0.0.1', 11434, '/api/tags'
SOCKET_TIMEOUT = 2
MAX_BYTES = 262144
CACHE_SECONDS = 10


def probe_tags(model, *, connect=None):
    """Return (status, model_installed, reason) with fixed codes only."""
    connection = (connect or http.client.HTTPConnection)(HOST, PORT, timeout=SOCKET_TIMEOUT)
    try:
        connection.request('GET', PATH, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200:
            return 'unknown', None, 'http_error'
        if (response.getheader('Content-Encoding') or 'identity').strip().lower() != 'identity':
            return 'unknown', None, 'unsupported_encoding'
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return 'unknown', None, 'response_too_large'
        try:
            tags = json.loads(data)
        except ValueError:
            return 'unknown', None, 'malformed_response'
        models = tags.get('models') if isinstance(tags, dict) else None
        if not isinstance(models, list) or not all(isinstance(item, dict) and isinstance(item.get('name'), str)
                                                   for item in models):
            return 'unknown', None, 'malformed_response'
        if any(item['name'] == model for item in models):
            return 'responding', True, 'model_listed'
        return 'responding', False, 'model_not_listed'
    except ConnectionRefusedError:
        return 'connection_refused', None, 'connection_refused'
    except TimeoutError:            # socket.timeout is an alias since Python 3.10
        return 'unknown', None, 'timeout'
    except (OSError, http.client.HTTPException):
        return 'unknown', None, 'connection_error'
    finally:
        connection.close()


class ModelStatusObserver:
    """One shared, briefly cached observation; concurrent polls wait for the same probe."""

    def __init__(self, model, *, probe=None, clock=time.monotonic, wall=time.time):
        self.model = LocalConfig(model=model).model   # invalid or cloud names raise before any probe
        self.probe, self.clock, self.wall = probe, clock, wall
        self.lock = threading.Lock()
        self.cache: dict = {'at': None, 'body': None}   # this process only; never persisted

    def observe(self):
        with self.lock:
            if self.cache['at'] is None or self.clock() - self.cache['at'] >= CACHE_SECONDS:
                status, installed, reason = (self.probe or probe_tags)(self.model)
                self.cache['body'] = {'source': 'observed', 'observed_at': self.wall(), 'model': self.model,
                                      'status': status, 'model_installed': installed, 'reason': reason}
                self.cache['at'] = self.clock()   # a raise leaves the old entry expired, so a 503 is never cached
            return dict(self.cache['body'])


def observer_for(chat):
    """Observer for the existing validated chat model, or None (no chat, or a name that fails validation)."""
    if chat is None:
        return None
    try:
        return ModelStatusObserver(getattr(chat, 'model', None))
    except ValueError:
        return None


def model_status_routes(observer, *, valid, origins, headers):
    hosts = {urlsplit(item).netloc for item in origins}

    async def model_status(request):
        origin = request.headers.get('origin')
        if request.url.query or request.url.netloc not in hosts or (origin is not None and origin not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=headers)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=headers)
        if observer is None:
            return JSONResponse({'error': 'model_status_unavailable'}, status_code=503, headers=headers)
        try:
            return JSONResponse(await asyncio.to_thread(observer.observe), headers=headers)
        except Exception:
            return JSONResponse({'error': 'model_status_unavailable'}, status_code=503, headers=headers)

    return [Route('/api/model-status', model_status, methods=['GET'])]
