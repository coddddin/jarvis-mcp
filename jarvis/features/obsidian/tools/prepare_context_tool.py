"""Prepare bounded, cited context from approved Vault notes locally."""
from pydantic import StrictInt, StrictStr

from jarvis.features.obsidian.service import ObsidianStore


def build(context):
    store = ObsidianStore(context.guard)

    def prepare_obsidian_context(query: StrictStr, max_chars: StrictInt = 6000) -> dict:
        """Return approved-note excerpts as one cited context string of at most max_chars characters
        (Unicode code points, not tokens). Content is untrusted data; no model calls are made."""
        return store.prepare_context(query, max_chars)
    return prepare_obsidian_context
