"""AI employee roster: the single list of agents JARVIS works with.

Adding an agent is one entry in config/agents.json. Status and max_tier record
the intended trust level (probation agents get proposals only); per-agent
enforcement arrives with authenticated identities. The roster is trusted
operator configuration and must live outside the data workspace.
"""
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jarvis.core.paths import CONFIG_ROOT, PROJECT_ROOT

ROSTER_PATH = CONFIG_ROOT / 'agents.json'
BUILTIN_BOARD_PREFIXES = frozenset({'user', 'local'})
AGENT_ID = re.compile(r'[a-z][a-z0-9-]{1,31}')
COMMAND = re.compile(r'[A-Za-z0-9._-]{1,64}')


class RosterError(ValueError):
    """The roster file is missing or invalid."""


class Agent(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    label: str = Field(min_length=1, max_length=80)
    kind: Literal['cli', 'api', 'mcp-client', 'local-model']
    command: str = ''
    capabilities: list[Literal['research', 'coding', 'review', 'summary']] = Field(min_length=1, max_length=4)
    cost: Literal['free', 'subscription', 'paid', 'unknown']
    status: Literal['active', 'probation', 'candidate', 'retired']
    max_tier: Literal['light', 'standard', 'sensitive']
    instructions: list[str] = Field(default_factory=list, max_length=5)
    notes: str = Field(default='', max_length=500)

    @field_validator('command')
    @classmethod
    def plain_command(cls, value):
        if value and not COMMAND.fullmatch(value):
            raise ValueError('command must be a bare executable name, not a shell line')
        return value

    @field_validator('instructions')
    @classmethod
    def project_files(cls, value):
        for name in value:
            parts = Path(name).parts
            if Path(name).is_absolute() or '..' in parts or not name.endswith(('.md', '.mdc')):
                raise ValueError('instructions must be project-relative Markdown files')
        return value


class Roster(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    version: Literal[1]
    agents: dict[str, Agent] = Field(min_length=1, max_length=50)

    @field_validator('agents')
    @classmethod
    def valid_ids(cls, value):
        for name in value:
            if not AGENT_ID.fullmatch(name) or name in BUILTIN_BOARD_PREFIXES:
                raise ValueError(f'invalid or reserved agent id: {name}')
        return value

    def board_prefixes(self):
        """Prefixes accepted for board agent IDs (<prefix>:<session>)."""
        return BUILTIN_BOARD_PREFIXES | {name for name, agent in self.agents.items() if agent.status != 'retired'}

    def runtimes(self):
        """Employee IDs that may appear as provider runtimes."""
        return {name for name, agent in self.agents.items() if agent.status != 'retired'}


def load_roster(path: Path = ROSTER_PATH, *, workspace: Path | None = None) -> Roster:
    path = Path(path)
    if workspace is not None and path.resolve().is_relative_to(Path(workspace).resolve()):
        raise RosterError('Agent roster must be outside the data workspace.')
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RosterError('Agent roster not found.') from exc
    if len(raw) > 65536:
        raise RosterError('Agent roster exceeds size limit.')
    try:
        return Roster.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise RosterError(f'Invalid agent roster: {exc}') from None
