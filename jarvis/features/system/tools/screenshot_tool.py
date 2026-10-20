"""One MCP tool. Registration and call admission are owned by the loader."""
import io
import logging
import subprocess
import sys
from uuid import uuid4
from PIL import Image
from mcp.server.fastmcp.exceptions import ToolError
from jarvis.core.guardrails import GuardrailError
LOGGER = logging.getLogger("jarvis")
CAPTURE_WORKER = '''
import sys
from PIL import ImageGrab
with ImageGrab.grab() as image:
    if image.width * image.height > 33554432:
        raise RuntimeError('Display exceeds capture size limit')
    image.save(sys.stdout.buffer, format='PNG')
'''

def capture_desktop() -> bytes:
    """Bound desktop API stalls without blocking the MCP server indefinitely."""
    try:
        result = subprocess.run(
            [sys.executable, '-I', '-c', CAPTURE_WORKER],
            capture_output=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError('Screenshot exceeded the 10 second timeout and was terminated.') from exc
    if result.returncode != 0:
        raise ToolError('Desktop capture failed; an accessible interactive desktop is required.')
    return result.stdout


def build(context):
    guard = context.guard
    def take_screenshot() -> dict:
        """Capture the current desktop to a unique PNG inside JarvisWorkspace. May contain sensitive data."""
        try:
            destination = guard.validate_path('screenshot-' + uuid4().hex + '.png', must_exist=False)
            # Capture to memory first, so capture failures leave no fake successful file.
            png = capture_desktop()
            with Image.open(io.BytesIO(png)) as image:
                image.verify()
                dimensions = image.size
            with destination.open('xb') as output:
                output.write(png)
            return {'path': str(destination), 'width': dimensions[0], 'height': dimensions[1]}
        except GuardrailError as exc:
            raise ToolError(str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            LOGGER.error('Screenshot failed (%s)', type(exc).__name__)
            raise ToolError('Screenshot failed; an accessible desktop and write permission are required.') from exc
    return take_screenshot
