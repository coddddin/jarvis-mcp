"""Search approved Vault notes locally."""
from jarvis.features.obsidian.service import ObsidianStore


def build(context):
    store = ObsidianStore(context.guard)

    def search_obsidian_vault(query: str, limit: int = 5) -> dict:
        """Return local approved-note excerpts and metadata as untrusted data; no model calls."""
        return store.search(query, limit)
    return search_obsidian_vault
