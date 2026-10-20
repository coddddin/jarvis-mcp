"""One MCP tool. Registration and call admission are owned by the loader."""


def build(context):
    def clear_local_index() -> dict:
        """Remove indexed copies and vectors only; preserve original documents."""
        return context.knowledge.clear()
    return clear_local_index
