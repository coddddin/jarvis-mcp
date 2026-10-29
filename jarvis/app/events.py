"""Authenticated loopback event bridge with optional bounded local chat."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from jarvis.agents.events import AsyncEventBus, EventError
from jarvis.agents.store import TaskNotFound
from jarvis.agents.chat import ChatError, ChatBusy
from jarvis.app.briefing_routes import briefing_routes
from jarvis.app.wiki_routes import wiki_routes
from jarvis.features.wiki.service import WikiStore
from jarvis.app.dispatcher_routes import dispatcher_routes
from jarvis.app.model_status import model_status_routes, observer_for
from jarvis.core.paths import AUTH_ROOT, PROJECT_ROOT


def load_event_token(root=AUTH_ROOT):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.is_junction():
        raise ValueError('Unsafe event authentication directory.')
    root.mkdir(exist_ok=True)
    path = root / 'agent-events-token.txt'
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                stream.write(secrets.token_urlsafe(32))
                stream.flush()
                os.fsync(stream.fileno())
    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('Linked event authentication files are forbidden.')
    with path.open(encoding='ascii') as stream:
        token = stream.read(129)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('Invalid event authentication file.')
    return token


CONNECT_SECONDS = 300
CONNECT_FAILURES = 5


def create_event_app(tasks, *, token, task_ids, origins, chat=None, processes=None, board=None, activity=None,
                     sessions=None, dispatcher=None, briefing=None, connect_code=None, voice=None):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('A strong local event token is required.')
    if connect_code is not None and (not isinstance(connect_code, str)
                                     or not re.fullmatch(r'[A-Za-z0-9_-]{43}', connect_code)):
        raise ValueError('A strong one-time connect code is required.')
    # The browser that `hud --open` starts gets this code in the URL fragment, never the token itself, so a saved
    # or synced history entry holds only a spent code. One use, CONNECT_SECONDS, CONNECT_FAILURES wrong guesses.
    pairing = {'code': connect_code, 'expires': time.monotonic() + CONNECT_SECONDS, 'failures': 0}
    task_ids, origins = frozenset(task_ids), frozenset(origins)
    if (not task_ids and chat is None) or len(task_ids) > 32 or not origins:
        raise ValueError('Explicit task and Origin allowlists are required.')
    for task_id in task_ids:
        tasks.get(task_id)
    for origin in origins:
        value = urlsplit(origin)
        if (value.scheme != 'http' or value.hostname not in {'127.0.0.1', 'localhost'}
                or not value.port or value.username or value.password or value.path or value.query or value.fragment):
            raise ValueError('Only exact local HTTP Origins are allowed.')
    bus = AsyncEventBus(tasks)
    active = 0

    def valid(candidate):
        return isinstance(candidate, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{43}', candidate)) and secrets.compare_digest(candidate, token)

    response_headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}

    async def asset(request):
        filename = {'/wiki': 'wiki/index.html', '/assets/wiki/wiki.css': 'wiki/wiki.css', '/assets/wiki/wiki.js': 'wiki/wiki.js', '/': 'index.html', '/assets/hud.css': 'hud.css', '/assets/hud.js': 'hud.js',
                    '/assets/task-stream.js': 'task-stream.js', '/assets/chat.js': 'chat.js', '/assets/sources.js': 'sources.js',
                    '/assets/collaboration.js': 'collaboration.js', '/assets/collaboration.css': 'collaboration.css',
                    '/assets/dispatcher.js': 'dispatcher.js', '/assets/briefing.js': 'briefing.js',
                    # Assistant view: the default page opened by `hud --open`; the HUD above is the developer screen.
                    '/assistant': 'assistant/index.html', '/assets/assistant/assistant.css': 'assistant/assistant.css',
                    '/assets/assistant/assistant.js': 'assistant/assistant.js', '/assets/assistant/orb.js': 'assistant/orb.js',
                    '/assets/assistant/connection.js': 'assistant/connection.js',
                    '/assets/assistant/knowledge.js': 'assistant/knowledge.js',
                    '/assets/assistant/presence.js': 'assistant/presence.js',
                    '/assets/assistant/voice.js': 'assistant/voice.js'}[request.url.path]
        return FileResponse(PROJECT_ROOT / 'frontend' / filename, headers=response_headers)

    def summaries():
        result = []
        for task_id in sorted(task_ids):
            try:
                task = tasks.get(task_id)
            except TaskNotFound:
                # Explicitly forgotten/expired tasks are not exposed as fabricated state.
                continue
            evidence = next((item.qa_evidence for item in reversed(task.history) if item.qa_evidence), None)
            try:
                latest_sequence = tasks.events(task_id, limit=1).latest_sequence
                events_available = True
            except EventError:
                latest_sequence, events_available = 0, False
            result.append({'task_id': task.task_id, 'title': task.request.summary[:100], 'status': task.status,
                'role': task.expected_role, 'steps_started': task.steps_started, 'corrections': task.correction_retries,
                'resumes': task.transport_retries, 'updated_at': task.updated_at, 'deadline': task.deadline,
                'output_reserved': task.generation_tokens_reserved, 'output_limit': task.request.max_generation_tokens,
                'model_reserved': task.model_tokens_reserved, 'model_limit': task.request.max_model_tokens,
                'qa': {'checker': evidence.checker, 'passed': evidence.passed, 'cases_passed': evidence.cases_passed,
                       'cases_total': evidence.cases_total} if evidence else None,
                'latest_sequence': latest_sequence, 'events_available': events_available})
        return sorted(result, key=lambda job: job['updated_at'], reverse=True)

    async def listing(request):
        origin = request.headers.get('origin')
        header = request.headers.get('authorization', '')
        if origin is not None and origin not in origins:
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=response_headers)
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=response_headers)
        try:
            if request.url.path == '/api/processes':
                if processes is None:
                    return JSONResponse({'error': 'monitor_unavailable'}, status_code=503, headers=response_headers)
                observed = await asyncio.to_thread(processes.inspect, limit=100)
                # Expose observations, never command lines, paths or parent identities.
                items = [{key: item[key] for key in ('id', 'pid', 'kind', 'label', 'process_state', 'warnings', 'observed_at')}
                         for item in observed['processes']]
                return JSONResponse({'processes': items, 'has_more': observed['has_more'],
                    'termination_supported': False}, headers=response_headers)
            jobs = await asyncio.to_thread(summaries)
            return JSONResponse({'tasks': jobs, 'read_only': True, 'local_only': True}, headers=response_headers)
        except Exception:
            return JSONResponse({'error': 'status_unavailable'}, status_code=503, headers=response_headers)

    async def collaboration(request):
        if (request.url.query or request.url.netloc not in {urlsplit(item).netloc for item in origins}
                or request.headers.get('origin') is not None and request.headers['origin'] not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=response_headers)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=response_headers)
        if board is None:
            return JSONResponse({'error': 'collaboration_unavailable'}, status_code=503, headers=response_headers)
        try:
            snapshot = await asyncio.to_thread(board.activity_snapshot)
            return JSONResponse(snapshot, headers=response_headers)
        except Exception:
            return JSONResponse({'error': 'collaboration_unavailable'}, status_code=503, headers=response_headers)

    async def session_status(request):
        if (request.url.query or request.url.netloc not in {urlsplit(item).netloc for item in origins}
                or request.headers.get('origin') is not None and request.headers['origin'] not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=response_headers)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=response_headers)
        if sessions is None:
            return JSONResponse({'error': 'sessions_unavailable'}, status_code=503, headers=response_headers)
        try:
            return JSONResponse(await asyncio.to_thread(sessions.inspect), headers=response_headers)
        except Exception:
            return JSONResponse({'error': 'sessions_unavailable'}, status_code=503, headers=response_headers)

    async def activity_api(request):
        if (request.url.query or request.url.netloc not in {urlsplit(item).netloc for item in origins}
                or request.headers.get('origin') is not None and request.headers['origin'] not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=response_headers)
        header = request.headers.get('authorization', '')
        # The trace route is registered GET-only and never reaches the ingest branch, whatever its method.
        trace = 'task_id' in request.path_params
        if request.method == 'GET' or trace:
            if not header.startswith('Bearer ') or not valid(header[7:]):
                return JSONResponse({'error': 'connection_required'}, status_code=401, headers=response_headers)
            # Activity task IDs, not Board or selected TaskStore IDs. The HUD token already reads every run via
            # /api/activity, so the trace adds no wider scope.
            if trace and not re.fullmatch(r'[a-f0-9]{32}', request.path_params['task_id']):
                return JSONResponse({'error': 'invalid_activity_task'}, status_code=400, headers=response_headers)
            if activity is None:
                return JSONResponse({'error': 'activity_unavailable'}, status_code=503, headers=response_headers)
            try:
                if trace:
                    return JSONResponse(await asyncio.to_thread(activity.trace, request.path_params['task_id']),
                                        headers=response_headers)
                return JSONResponse(await asyncio.to_thread(activity.inspect, limit=100), headers=response_headers)
            except Exception:
                return JSONResponse({'error': 'activity_unavailable'}, status_code=503, headers=response_headers)
        # Native run reporters have their own credential, never the HUD connection token.
        if not header.startswith('Bearer ') or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', header[7:]):
            return JSONResponse({'error': 'run_token_required'}, status_code=401, headers=response_headers)
        if activity is None:
            return JSONResponse({'error': 'activity_unavailable'}, status_code=503, headers=response_headers)
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            return JSONResponse({'error': 'json_required'}, status_code=415, headers=response_headers)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 1024:
                    return JSONResponse({'error': 'event_too_large'}, status_code=413, headers=response_headers)
            event = json.loads(raw)
            run_id = request.path_params['run_id']
            if not re.fullmatch(r'[a-f0-9]{32}', run_id) or not isinstance(event, dict) or event.get('run_id') != run_id:
                raise ValueError('Invalid event identity.')
            result = await asyncio.to_thread(activity.ingest, header[7:], event)
            return JSONResponse({key: result[key] for key in ('status', 'run_id', 'seq')}, headers=response_headers)
        except ValueError:
            return JSONResponse({'error': 'event_rejected'}, status_code=400, headers=response_headers)
        except Exception:
            return JSONResponse({'error': 'activity_unavailable'}, status_code=503, headers=response_headers)

    def gate(request, *, post=False):
        origin = request.headers.get('origin')
        if (request.url.query or request.url.netloc not in {urlsplit(item).netloc for item in origins}
                or origin is not None and origin not in origins or post and origin not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=response_headers)
        return None

    async def connect_api(request):
        denied = gate(request, post=True)
        if denied:
            return denied
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            return JSONResponse({'error': 'json_required'}, status_code=415, headers=response_headers)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256:
                return JSONResponse({'error': 'request_too_large'}, status_code=413, headers=response_headers)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        candidate = data.get('code') if isinstance(data, dict) and set(data) == {'code'} else None
        if (pairing['code'] is None or time.monotonic() > pairing['expires']
                or pairing['failures'] >= CONNECT_FAILURES):
            pairing['code'] = None
            return JSONResponse({'error': 'connect_code_expired'}, status_code=410, headers=response_headers)
        if (not isinstance(candidate, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', candidate)
                or not secrets.compare_digest(candidate, pairing['code'])):
            pairing['failures'] += 1
            return JSONResponse({'error': 'connect_code_invalid'}, status_code=401, headers=response_headers)
        pairing['code'] = None
        return JSONResponse({'token': token}, headers=response_headers)

    async def chat_api(request):
        header = request.headers.get('authorization', '')
        origin = request.headers.get('origin')
        if (request.url.query or request.url.netloc not in {urlsplit(item).netloc for item in origins}
                or origin is not None and origin not in origins
                or request.method == 'POST' and origin not in origins):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=response_headers)
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=response_headers)
        if chat is None:
            return JSONResponse({'error': 'chat_unavailable'}, status_code=503, headers=response_headers)
        try:
            if request.method == 'GET':
                identity = request.path_params.get('conversation_id')
                result = await asyncio.to_thread(chat.store.get, identity) if identity else await asyncio.to_thread(chat.metadata)
                return JSONResponse(result, headers=response_headers)
            if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                return JSONResponse({'error': 'json_required'}, status_code=415, headers=response_headers)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 16384:
                    return JSONResponse({'error': 'request_too_large'}, status_code=413, headers=response_headers)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ChatError('Expected a JSON object.')
            if request.url.path == '/api/chat/warm':
                # The user started typing or opened the browser mic: load the model now. Nothing is generated.
                if data:
                    raise ChatError('No warm-up parameters accepted.')
                await asyncio.to_thread(chat.warm_up)
                return JSONResponse({'warming': True}, status_code=202, headers=response_headers)
            if request.url.path == '/api/chat/conversations':
                if data:
                    raise ChatError('No creation parameters accepted.')
                result = await asyncio.to_thread(chat.store.create)
                return JSONResponse(result, status_code=201, headers=response_headers)
            if set(data) not in ({'request_id', 'message'}, {'request_id', 'message', 'mode'}):
                raise ChatError('Expected request_id, message, and optional mode only.')
            result = await asyncio.to_thread(chat.send, request.path_params['conversation_id'], data['request_id'], data['message'], data.get('mode', 'chat'))
            return JSONResponse(result, headers=response_headers)
        except ChatBusy as exc:
            return JSONResponse({'error': str(exc)}, status_code=409, headers=response_headers)
        except (ChatError, ValueError) as exc:
            # Validation errors contain only controlled messages, never raw credentials/model payload.
            return JSONResponse({'error': str(exc) if isinstance(exc, ChatError) else 'Invalid JSON request.'},
                                status_code=400, headers=response_headers)
        except Exception:
            return JSONResponse({'error': '자비스 응답을 완료하지 못했습니다. 연결과 로컬 모델을 확인하세요.'},
                                status_code=503, headers=response_headers)

    async def watch(websocket):
        nonlocal active
        task_id = websocket.path_params['task_id']
        if websocket.headers.get('origin') not in origins or task_id not in task_ids or active >= 16:
            await websocket.close(code=1008)
            return
        header = websocket.headers.get('authorization')
        if header is not None and (not header.startswith('Bearer ') or not valid(header[7:])):
            await websocket.close(code=1008)
            return
        active += 1
        try:
            await websocket.accept()
            if header is None:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=3)
                if len(raw) > 128:
                    await websocket.close(code=1008)
                    return
                credentials = json.loads(raw)
                if not isinstance(credentials, dict) or set(credentials) != {'token'} or not valid(credentials['token']):
                    await websocket.close(code=1008)
                    return
            after_text = websocket.query_params.get('after', '0')
            if not re.fullmatch(r'[0-9]{1,9}', after_text) or set(websocket.query_params) - {'after'}:
                await websocket.close(code=1008)
                return
            async for event in bus.stream(task_id, after=int(after_text)):
                await asyncio.wait_for(websocket.send_json(event.model_dump(mode='json')), timeout=3)
            await websocket.send_json({'kind': 'stream.end', 'reconnect_with_last_sequence': True})
            await websocket.close(code=1000)
        except WebSocketDisconnect:
            return
        except EventError:
            try:
                await websocket.send_json({'kind': 'stream.error', 'code': 'cursor_recovery_required', 'snapshot_required': True})
                await websocket.close(code=1008)
            except (WebSocketDisconnect, RuntimeError):
                return
        except Exception:
            # No raw exception/source/credential data crosses the bridge.
            try:
                await websocket.close(code=1008)
            except (WebSocketDisconnect, RuntimeError):
                return
        finally:
            active -= 1

    # W5 integration-4: dispatcher status, pause and resume (token, Origin and Host checks live in the routes).
    extra = (dispatcher_routes(*dispatcher, valid=valid, origins=origins, headers=response_headers, board=board)
             if dispatcher is not None else [])
    # Today's Google briefing card (read only; same gate, in-memory cache only).
    if briefing is not None:
        extra += briefing_routes(briefing, valid=valid, origins=origins, headers=response_headers)
    # Ollama card: last /api/tags observation for the configured chat model (same gate; no chat => 503).
    extra += model_status_routes(observer_for(chat), valid=valid, origins=origins, headers=response_headers)
    # Local voice (hud --voice): push-to-talk and spoken replies over one WebSocket (same token, Origin and Host).
    if voice is not None:
        from jarvis.app.voice_routes import voice_routes
        extra += voice_routes(voice, valid=valid, origins=origins)
    if chat is not None:
        extra += wiki_routes(WikiStore(tasks.guard), guard=tasks.guard, infer=getattr(chat, '_infer', None), valid=valid, origins=origins, headers=response_headers)
    return Starlette(routes=extra + [Route('/wiki', asset), Route('/assets/wiki/wiki.css', asset), Route('/assets/wiki/wiki.js', asset), Route('/', asset), Route('/assets/dispatcher.js', asset), Route('/assets/briefing.js', asset), Route('/assets/hud.css', asset), Route('/assets/hud.js', asset),
        Route('/assets/task-stream.js', asset), Route('/assets/chat.js', asset), Route('/assets/sources.js', asset),
        Route('/assistant', asset), Route('/assets/assistant/assistant.css', asset), Route('/assets/assistant/assistant.js', asset),
        Route('/assets/assistant/orb.js', asset), Route('/assets/assistant/connection.js', asset),
        Route('/assets/assistant/knowledge.js', asset),
        Route('/assets/assistant/presence.js', asset), Route('/assets/assistant/voice.js', asset),
        Route('/api/chat/conversations', chat_api, methods=['GET', 'POST']),
        Route('/api/chat/warm', chat_api, methods=['POST']),
        Route('/api/chat/conversations/{conversation_id}', chat_api, methods=['GET']),
        Route('/api/chat/conversations/{conversation_id}/messages', chat_api, methods=['POST']),
        Route('/api/connect', connect_api, methods=['POST']),
        Route('/api/sessions', session_status), Route('/api/activity', activity_api), Route('/api/activity/runs/{run_id}/events', activity_api, methods=['POST']),
        Route('/api/activity/tasks/{task_id}/trace', activity_api, methods=['GET']),
        Route('/assets/collaboration.js', asset), Route('/assets/collaboration.css', asset), Route('/api/collaboration', collaboration),
        Route('/api/tasks', listing), Route('/api/processes', listing), WebSocketRoute('/tasks/{task_id}/events', watch)])
