"""Isolated MCP client session for one JARVIS task. No project imports.

Reads one JSON command per stdin line and writes one JSON reply per stdout line:
  {"op": "start", "servers": [{"id", "command", "args", "env", "cwd", "timeout"}]}
      -> {"ok": true, "servers": {id: {"ok": true, "server_name": "...", "tools": [{"name", "description", "input_schema"}]}
                                    | {"ok": false, "error": "..."}}}
  {"op": "call", "server", "tool", "arguments", "timeout", "max_chars"}
      -> {"ok": true, "is_error": bool, "text": "...", "truncated": bool, "omitted": [kinds]}
  {"op": "close"} -> {"ok": true}, then exit
Servers run over stdio through the MCP SDK, which puts each one in a kill-on-close job object on Windows. Server
stderr is discarded. Every reply is bounded; the parent also kills this process tree on timeout or stop.
"""
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MAX_REPLY = 1_000_000           # bytes per reply line
MAX_TOOLS = 128                 # tools listed per server
MAX_DESCRIPTION = 2000


def reply(value):
    data = json.dumps(value, ensure_ascii=False).encode('utf-8')     # UTF-8 bytes, whatever the console code page
    if len(data) > MAX_REPLY:
        data = json.dumps({'ok': False, 'error': 'Reply exceeded the worker size limit.'}).encode('utf-8')
    sys.stdout.buffer.write(data + b'\n')
    sys.stdout.buffer.flush()


def tool_text(result, max_chars):
    """Text parts of a CallToolResult; images, audio and embedded binaries are named, not passed on."""
    parts, omitted = [], []
    for item in result.content or []:
        kind = getattr(item, 'type', 'unknown')
        if kind == 'text':
            parts.append(item.text)
        elif kind == 'resource' and getattr(item.resource, 'text', None) is not None:
            parts.append(item.resource.text)
        else:
            omitted.append(kind)
    structured = getattr(result, 'structuredContent', None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False))
    text = '\n'.join(parts)
    return {'ok': True, 'is_error': bool(result.isError), 'text': text[:max_chars],
            'truncated': len(text) > max_chars, 'omitted': sorted(set(omitted))}


async def start_server(stack, spec, devnull):
    params = StdioServerParameters(command=spec['command'], args=list(spec.get('args') or []),
                                   env={name: os.environ[name] for name in spec.get('env') or [] if name in os.environ},
                                   cwd=spec.get('cwd'))
    read, write = await stack.enter_async_context(stdio_client(params, errlog=devnull))
    session = await stack.enter_async_context(ClientSession(read, write))
    initialized = await asyncio.wait_for(session.initialize(), spec.get('timeout', 30))
    info = getattr(initialized, 'serverInfo', None)
    name = str(getattr(info, 'name', '') or '')[:100]
    listed = await asyncio.wait_for(session.list_tools(), spec.get('timeout', 30))
    tools = [{'name': tool.name, 'description': (tool.description or '')[:MAX_DESCRIPTION],
              'input_schema': tool.inputSchema} for tool in listed.tools[:MAX_TOOLS]]
    return session, tools, name


async def main_async():
    sessions = {}
    loop = asyncio.get_running_loop()
    with open(os.devnull, 'w', encoding='utf-8') as devnull:
        async with AsyncExitStack() as stack:
            while True:
                line = (await loop.run_in_executor(None, sys.stdin.buffer.readline)).decode('utf-8', 'replace')
                if not line:
                    return
                try:
                    command = json.loads(line)
                    op = command.get('op')
                except (ValueError, AttributeError):
                    reply({'ok': False, 'error': 'Invalid command.'})
                    continue
                if op == 'close':
                    reply({'ok': True})
                    return
                if op == 'start':
                    report = {}
                    for spec in command.get('servers') or []:
                        server_id = spec.get('id')
                        own = AsyncExitStack()          # a server that fails to start is shut down at once
                        try:
                            sessions[server_id], tools, name = await start_server(own, spec, devnull)
                            stack.push_async_callback(own.aclose)
                            report[server_id] = {'ok': True, 'tools': tools, 'server_name': name}
                        except Exception as exc:  # one failing server does not stop the others
                            try:
                                await own.aclose()
                            except Exception:
                                pass
                            report[server_id] = {'ok': False, 'error': type(exc).__name__}
                    reply({'ok': True, 'servers': report})
                elif op == 'call':
                    session = sessions.get(command.get('server'))
                    if session is None:
                        reply({'ok': False, 'error': 'Server is not running.'})
                        continue
                    timeout = float(command.get('timeout', 30))
                    try:
                        result = await asyncio.wait_for(session.call_tool(
                            command['tool'], command.get('arguments') or {},
                            read_timeout_seconds=timedelta(seconds=timeout)), timeout)
                        reply(tool_text(result, int(command.get('max_chars', 8000))))
                    except asyncio.TimeoutError:
                        reply({'ok': False, 'timeout': True, 'error': 'Tool call timed out.'})
                    except Exception as exc:
                        reply({'ok': False, 'error': type(exc).__name__})
                else:
                    reply({'ok': False, 'error': 'Unknown command.'})


def main():
    asyncio.run(main_async())
    return 0


if __name__ == '__main__':
    sys.exit(main())
