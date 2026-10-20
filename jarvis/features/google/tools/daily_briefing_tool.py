"""One MCP tool. Registration and call admission are owned by the loader."""


def build(context):
    def get_daily_briefing(day: str = '', limit: int = 10) -> dict:
        """Read primary Calendar on YYYY-MM-DD (Seoul), plus current unread Gmail headers. No writes."""
        return context.google.briefing(day, limit)
    return get_daily_briefing
