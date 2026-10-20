"""One MCP tool. Registration and call admission are owned by the loader."""
import json
import logging
import os
import subprocess
import sys
from mcp.server.fastmcp.exceptions import ToolError
from jarvis.core.guardrails import GuardrailError
LOGGER = logging.getLogger("jarvis")
WORKER = r'''
import getpass, itertools, json, os, socket, sys
data = json.load(sys.stdin)
op, arg = data['operation'], data['argument']
if op == 'pwd':
    result = os.getcwd()
elif op == 'echo':
    result = arg
elif op == 'whoami':
    result = getpass.getuser()
elif op == 'hostname':
    result = socket.gethostname()
elif op == 'ls':
    with os.scandir(arg) as entries:
        names = [entry.name for entry in itertools.islice(entries, 201)]
    result = {'entries': names[:200], 'truncated': len(names) > 200}
else:
    raise ValueError('Unsupported operation')
print(json.dumps(result, ensure_ascii=True))
'''


def build(context):
    guard = context.guard
    def run_safe_shell_command(command: str) -> dict:
        """Run pwd/whoami/hostname/echo/ls only; no arbitrary shell or scripts."""
        try:
            approved = guard.validate_command(command)
            completed = subprocess.run(
                [sys.executable, '-I', '-S', '-c', WORKER],
                input=json.dumps({'operation': approved.operation, 'argument': approved.argument}),
                text=True, encoding='utf-8', capture_output=True, timeout=5,
                cwd=guard.workspace, shell=False,
                env={k: v for k, v in os.environ.items() if k.upper() in {
                    'SYSTEMROOT', 'WINDIR', 'USERNAME', 'USER', 'LOGNAME'
                }},
            )
            if completed.returncode != 0:
                raise ToolError('Command failed in restricted worker; exit code '
                                + str(completed.returncode))
            return {'exit_code': 0, 'output': json.loads(completed.stdout)}
        except GuardrailError as exc:
            raise ToolError(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError('Command exceeded the 5 second timeout and was terminated.') from exc
        except (OSError, ValueError) as exc:
            LOGGER.error('Restricted worker failed (%s)', type(exc).__name__)
            raise ToolError('Restricted worker failed; check local configuration and permissions.') from exc
    return run_safe_shell_command
