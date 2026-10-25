"""HUD routes for the dispatcher panel: status, pause, resume.

Built as a separate route list so the HUD app only needs to append it. Same rules as the other HUD APIs:
bearer token from the connection file, exact local Origin, matching Host, no query string. State changes
(POST) also require an Origin header, which browsers always send on POST.
"""
import asyncio
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from jarvis.agents import dispatch_control


def dispatcher_routes(guard, config_path, *, valid, origins, headers, board=None):
    hosts = {urlsplit(item).netloc for item in origins}

    def refuse(request):
        origin = request.headers.get('origin')
        if request.url.query or request.url.netloc not in hosts or (origin is not None and origin not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=headers)
        if request.method == 'POST' and origin is None:
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=headers)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=headers)
        return None

    async def status(request):
        denied = refuse(request)
        if denied:
            return denied
        try:
            if request.url.path.endswith('/pause'):
                await asyncio.to_thread(dispatch_control.pause, guard)
            elif request.url.path.endswith('/resume'):
                await asyncio.to_thread(dispatch_control.resume, guard)
            result = await asyncio.to_thread(dispatch_control.snapshot, guard, config_path)
            return JSONResponse(result, headers=headers)
        except Exception:
            return JSONResponse({'error': 'dispatcher_unavailable'}, status_code=503, headers=headers)

    async def messages(request):
        """GET only: one-line headers of the messages that woke a run. Nothing is marked read."""
        denied = refuse(request)
        if denied:
            return denied
        run_id = request.path_params['run_id']  # the int convertor admits digits only
        if not 0 < run_id < 2 ** 63:
            return JSONResponse({'error': 'not_found'}, status_code=404, headers=headers)
        if board is None:
            return JSONResponse({'error': 'board_unavailable'}, status_code=503, headers=headers)
        try:
            result = await asyncio.to_thread(dispatch_control.run_messages, guard, board, run_id)
        except Exception:
            return JSONResponse({'error': 'dispatcher_unavailable'}, status_code=503, headers=headers)
        if result is None:
            return JSONResponse({'error': 'not_found'}, status_code=404, headers=headers)
        return JSONResponse(result, headers=headers)

    return [Route('/api/dispatcher', status, methods=['GET']),
            Route('/api/dispatcher/runs/{run_id:int}/messages', messages, methods=['GET']),
            Route('/api/dispatcher/pause', status, methods=['POST']),
            Route('/api/dispatcher/resume', status, methods=['POST'])]
