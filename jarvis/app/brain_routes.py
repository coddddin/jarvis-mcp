"""HUD routes for the model-name button: list the selectable models and change the chat/voice model.

Same gate as the chat API: bearer token from the connection file, exact local Origin (required for POST),
matching Host, no query string. The selection is validated by its provider before it is saved; any failure keeps
the old one and answers why. The first switch from local to an external provider answers 409 with what would be
sent and what never is (jarvis.agents.brain.EXTERNAL_SENDS / NEVER_SENT) until the request repeats with
confirm_external. Responses never carry secrets: a selection names a connection_ref, never a key.

API keys (brain P3): POST /api/brain/key {provider_id, key} checks the key with that company's models endpoint and
stores it in Windows Credential Manager; the answer is only {provider_id, connected}. POST /api/brain/key/delete
{provider_id} removes it. The key is never logged, echoed or written to brain.json.
"""
import asyncio
import json
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from jarvis.agents.brain import BrainError, ConfirmRequired, consent_payload
from jarvis.core.guardrails import GuardrailError

MAX_BODY = 1024
MAX_KEY_BODY = 2048
FIELDS = {'provider_id', 'model_id'}
OPTIONAL = {'connection_ref', 'confirm_external'}


def brain_routes(chat, *, valid, origins, headers):
    hosts = {urlsplit(item).netloc for item in origins}

    def refuse(request):
        origin = request.headers.get('origin')
        if (request.url.query or request.url.netloc not in hosts or (origin is not None and origin not in origins)
                or (request.method == 'POST' and origin not in origins)):
            return JSONResponse({'error': 'origin_denied'}, status_code=403, headers=headers)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not valid(header[7:]):
            return JSONResponse({'error': 'connection_required'}, status_code=401, headers=headers)
        if chat is None:
            return JSONResponse({'error': 'chat_unavailable'}, status_code=503, headers=headers)
        return None

    async def listing(request):
        refused = refuse(request)
        if refused is not None:
            return refused
        try:
            return JSONResponse(await asyncio.to_thread(chat.brain.listing), headers=headers)
        except Exception:
            return JSONResponse({'error': 'brain_unavailable'}, status_code=503, headers=headers)

    async def select(request):
        refused = refuse(request)
        if refused is not None:
            return refused
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            return JSONResponse({'error': 'json_required'}, status_code=415, headers=headers)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                return JSONResponse({'error': 'request_too_large'}, status_code=413, headers=headers)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if (not isinstance(data, dict) or not FIELDS <= set(data) <= FIELDS | OPTIONAL
                or not all(isinstance(data[key], str) and 0 < len(data[key]) <= 120 for key in FIELDS)
                or data.get('connection_ref') is not None and not isinstance(data['connection_ref'], str)
                or not isinstance(data.get('confirm_external', False), bool)):
            return JSONResponse({'error': 'invalid_selection'}, status_code=400, headers=headers)
        try:
            choice = await asyncio.to_thread(chat.brain.select, data['provider_id'], data['model_id'],
                                             data.get('connection_ref'),
                                             confirm_external=data.get('confirm_external', False))
        except ConfirmRequired as exc:
            return JSONResponse({'error': 'confirm_required', **consent_payload(exc.info)}, status_code=409,
                                headers=headers)
        except BrainError as exc:
            return JSONResponse({'error': 'model_unavailable', 'reason': str(exc)}, status_code=400, headers=headers)
        except GuardrailError:
            return JSONResponse({'error': 'stopped'}, status_code=503, headers=headers)
        except Exception:
            return JSONResponse({'error': 'brain_unavailable'}, status_code=503, headers=headers)
        return JSONResponse({'selection': {**choice.selection.public(), 'label': choice.label,
                                           'external': choice.external}}, headers=headers)

    async def body_of(request, limit):
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            return JSONResponse({'error': 'json_required'}, status_code=415, headers=headers)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                return JSONResponse({'error': 'request_too_large'}, status_code=413, headers=headers)
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def key_action(request, store):
        refused = refuse(request)
        if refused is not None:
            return refused
        data = await body_of(request, MAX_KEY_BODY)
        if isinstance(data, JSONResponse):
            return data
        fields = {'provider_id', 'key'} if store else {'provider_id'}
        if (not isinstance(data, dict) or set(data) != fields
                or not all(isinstance(data[name], str) and 0 < len(data[name]) <= 1024 for name in fields)):
            return JSONResponse({'error': 'invalid_key_request'}, status_code=400, headers=headers)
        provider_id = data['provider_id']
        try:
            if store:
                await asyncio.to_thread(chat.brain.set_key, provider_id, data['key'])
            else:
                await asyncio.to_thread(chat.brain.delete_key, provider_id)
        except BrainError as exc:
            # The reason is ours (Korean, redacted provider message); it never contains the submitted key.
            reason = str(exc).replace(data['key'], '[키]') if store else str(exc)
            return JSONResponse({'error': 'key_rejected', 'reason': reason}, status_code=400, headers=headers)
        except GuardrailError:
            return JSONResponse({'error': 'stopped'}, status_code=503, headers=headers)
        except Exception:
            return JSONResponse({'error': 'brain_unavailable'}, status_code=503, headers=headers)
        finally:
            data = None
        return JSONResponse({'provider_id': provider_id, 'connected': store}, headers=headers)

    async def store_key(request):
        return await key_action(request, True)

    async def delete_key(request):
        return await key_action(request, False)

    return [Route('/api/brain', listing, methods=['GET']),
            Route('/api/brain/selection', select, methods=['POST']),
            Route('/api/brain/key', store_key, methods=['POST']),
            Route('/api/brain/key/delete', delete_key, methods=['POST'])]
