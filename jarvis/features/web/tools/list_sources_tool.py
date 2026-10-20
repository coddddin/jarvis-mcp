def build(context):
    def list_web_sources() -> dict:
        """List operator-registered public HTTPS sources. No network request."""
        return context.web.list_sources()
    return list_web_sources
