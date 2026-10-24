"""HUD route for the "오늘 브리핑" card: today's Google briefing, read only.

Same gate as the other HUD APIs: bearer token from the connection file, exact local Origin, matching Host,
no query string. The response is a whitelist of what GoogleBriefing already returns, without ids, links or
bodies. The last result is kept in this process's memory for CACHE_SECONDS so refreshes cannot spam Google
(the HUD does not go through the MCP call budget); it is never written to disk. Mail and calendar text is
personal: nothing here logs it, and an unexpected failure answers a fixed code without its message.
"""
import asyncio
import threading
import time
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from jarvis.features.google.service import GoogleIntegrationError

CACHE_SECONDS = 60
MAX_ITEMS = 10
MAX_TEXT = 300


def _text(value, limit=MAX_TEXT):
    return value[:limit] if isinstance(value, str) else ''


def _when(value):
    """Calendar start/end: {'dateTime': ...} or {'date': ...} becomes that string; anything else ''."""
    if not isinstance(value, dict):
        return ''
    return _text(value.get('dateTime')) or _text(value.get('date'))


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _side(raw, items, shape, extra=()):
    if not isinstance(raw, dict):
        return {'status': 'error', 'error': 'No data.'}
    if raw.get('status') != 'ok':
        return {'status': 'error', 'error': _text(raw.get('error'), 200) or 'Unavailable.'}
    found = raw.get(items) if isinstance(raw.get(items), list) else []
    result = {'status': 'ok', 'has_more': raw.get('has_more') is True,
              items: [shape(item) for item in [i for i in found if isinstance(i, dict)][:MAX_ITEMS]]}
    for key in extra:
        result[key] = _count(raw.get(key))
    return result


def shape_briefing(raw):
    """Only the fields the card shows. Unknown keys never pass, odd values become '' or None."""
    return {
        'connected': True, 'date': _text(raw.get('date'), 10), 'generated_at': _text(raw.get('generated_at'), 40),
        'partial': raw.get('partial') is True,
        'calendar': _side(raw.get('calendar'), 'events', lambda item: {
            'summary': _text(item.get('summary')), 'start': _when(item.get('start')), 'end': _when(item.get('end'))}),
        'gmail': _side(raw.get('gmail'), 'messages', lambda item: {
            'from': _text(item.get('from')), 'subject': _text(item.get('subject')), 'date': _text(item.get('date'), 100)},
            extra=('unread_estimate',)),
    }


def briefing_routes(service, *, valid, origins, headers, clock=time.monotonic):
    hosts = {urlsplit(item).netloc for item in origins}
    cache: dict = {'at': None, 'body': None}  # this process only; never persisted
    lock = threading.Lock()  # concurrent refreshes share one Google call

    def refuse(request):
        origin = request.headers.get('origin')
        if request.url.query or request.url.netloc not in hosts or (origin is not None and origin not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=headers)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=headers)
        return None

    def fetch():
        if not service.status().get('credentials_present'):
            return {'connected': False}
        try:
            return shape_briefing(service.briefing())
        except GoogleIntegrationError as exc:   # its messages carry no credentials or API responses
            return {'connected': True, 'error': _text(str(exc), 200)}

    def cached():
        with lock:
            if cache['at'] is None or clock() - cache['at'] >= CACHE_SECONDS:
                cache['body'] = fetch()      # a raise leaves the old entry expired, so a 503 is never cached
                cache['at'] = clock()
            return cache['body']

    async def briefing(request):
        denied = refuse(request)
        if denied:
            return denied
        try:
            body = await asyncio.to_thread(cached)
        except Exception:
            return JSONResponse({'error': 'briefing_unavailable'}, status_code=503, headers=headers)
        return JSONResponse(body, headers=headers)

    return [Route('/api/briefing', briefing, methods=['GET'])]
