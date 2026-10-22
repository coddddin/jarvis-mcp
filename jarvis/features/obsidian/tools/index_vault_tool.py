"""Index approved Vault notes locally."""
from jarvis.features.obsidian.service import ObsidianStore


def build(context):
    store = ObsidianStore(context.guard)

    def index_obsidian_vault(directory: str = 'JARVIS-Vault') -> dict:
        """Index approved non-secret Markdown in a workspace Vault; never write or upload notes."""
        return store.index(directory)
    return index_obsidian_vault
