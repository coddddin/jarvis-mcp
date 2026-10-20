"""One MCP tool. Registration and call admission are owned by the loader."""


def build(context):
    def index_local_documents(directory: str = 'documents') -> dict:
        """Replace local index with UTF-8 .md/.txt/.rst documents in a workspace directory; no uploads."""
        return context.knowledge.index(directory)
    return index_local_documents
