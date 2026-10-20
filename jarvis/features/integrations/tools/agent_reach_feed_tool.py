def build(context):
    def read_registered_feed_with_agent_reach(source_id: str) -> dict:
        """Read one pre-registered public RSS/Atom feed using Agent Reach's offline backend; no login or arbitrary URL."""
        return context.integrations.feed(source_id)
    return read_registered_feed_with_agent_reach
