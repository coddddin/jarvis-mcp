"""One MCP tool. Registration and call admission are owned by the loader."""
import logging
import psutil
from mcp.server.fastmcp.exceptions import ToolError
from jarvis.core.guardrails import GuardrailError
LOGGER = logging.getLogger("jarvis")


def build(context):
    guard = context.guard
    def get_system_status() -> dict:
        """Return host CPU, memory and workspace volume usage; CPU sampling takes 0.1s."""
        try:
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage(str(guard.workspace))
            return {
                'cpu_percent': psutil.cpu_percent(interval=0.1),
                'memory': {'total_bytes': memory.total, 'available_bytes': memory.available,
                           'percent': memory.percent},
                'disk': {'total_bytes': disk.total, 'free_bytes': disk.free, 'percent': disk.percent},
            }
        except GuardrailError as exc:
            raise ToolError(str(exc)) from exc
        except (OSError, psutil.Error) as exc:
            LOGGER.error('System metrics failed (%s)', type(exc).__name__)
            raise ToolError('System metrics unavailable; check local permissions.') from exc
    return get_system_status
