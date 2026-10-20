"""One MCP tool. Registration and call admission are owned by the loader."""


def build(context):
    def get_google_connection_status() -> dict:
        """Report server credential presence without reading mail or verifying Google connectivity."""
        return context.google.status()
    return get_google_connection_status
