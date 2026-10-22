"""Read-only Vault adapter over the existing local lexical index.

Supports a deliberately small, flat YAML frontmatter subset, never YAML objects.
No plugin execution, wiki-link traversal, note writes, sync or model transmission.
"""
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jarvis.core.redaction import redact
from jarvis.features.knowledge.service import KnowledgeError, KnowledgeStore


class NoteMetadata(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9._-]+$')
    title: str = Field(min_length=1, max_length=200)
    created: str = Field(max_length=40)
    updated: str = Field(max_length=40)
    source: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(max_length=20)
    sensitivity: Literal['private', 'public', 'secret']
    review_status: Literal['proposed', 'approved', 'rejected']

    @model_validator(mode='after')
    def valid_values(self):
        created = datetime.fromisoformat(self.created)
        updated = datetime.fromisoformat(self.updated)
        if created.tzinfo is None or updated.tzinfo is None or updated < created:
            raise ValueError('Use ordered ISO timestamps with timezones')
        if not self.title.strip() or any(not tag.strip() or len(tag) > 80 for tag in self.tags):
            raise ValueError('Invalid title or tag')
        return self


def parse_frontmatter(text: str) -> NoteMetadata:
    """Accept flat scalars and a JSON string array for tags; fail closed otherwise."""
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise KnowledgeError('Vault note requires frontmatter.')
    values = {}
    for line in lines[1:65]:
        if line == '---':
            break
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z_]+):[ \t]*(.+)', line)
        if not match or match[1] in values:
            raise KnowledgeError('Unsupported or duplicate frontmatter field.')
        key, value = match.groups()
        try:
            if key == 'tags' or value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            elif value.startswith(('{', '[', '&', '*', '!', '|', '>', '`', "'")) or ' #' in value:
                raise ValueError('Unsupported YAML syntax')
            values[key] = value
        except ValueError:
            raise KnowledgeError('Unsupported frontmatter value.') from None
    else:
        raise KnowledgeError('Missing or oversized frontmatter boundary.')
    try:
        return NoteMetadata.model_validate(values)
    except (ValidationError, ValueError):
        raise KnowledgeError('Invalid Vault metadata.') from None


class ObsidianStore(KnowledgeStore):
    INDEX_DIRECTORY = '.jarvis-obsidian'

    def _read(self, relative: str) -> bytes:
        if Path(relative).suffix.lower() != '.md':
            raise KnowledgeError('Only Markdown Vault notes are accepted.')
        raw = super()._read(relative)
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeError:
            raise KnowledgeError('Vault note must be UTF-8.') from None
        metadata = parse_frontmatter(text)
        if metadata.review_status != 'approved' or metadata.sensitivity == 'secret':
            raise KnowledgeError('Note is not approved for local search.')
        if '\x00' in text or redact(text) != text:
            raise KnowledgeError('Vault note contains credentials or control characters.')
        return raw

    def index(self, directory: str = 'JARVIS-Vault') -> dict:
        result = super().index(directory)
        return {**result, 'approved_only': True, 'source_notes_unchanged': True,
                'external_transmission': False}

    def search(self, query: str, limit: int = 5) -> dict:
        result = super().search(query, limit)
        accepted = []
        for item in result['results']:
            try:
                # Recheck review status and sensitivity at disclosure time.
                metadata = parse_frontmatter(self._read(item['path']).decode('utf-8-sig'))
            except (OSError, ValueError):
                result['stale_documents'] = sorted(set(result['stale_documents']) | {item['path']})
                continue
            accepted.append({**item, 'metadata': metadata.model_dump()})
        return {**result, 'results': accepted, 'external_transmission': False}
