"""AI employee roster: one config entry admits a new agent everywhere."""
import json

import pytest

from jarvis.core import roster
from jarvis.core.roster import RosterError, load_roster


def write(tmp_path, agents, version=1):
    path = tmp_path / 'agents.json'
    path.write_text(json.dumps({'version': version, 'agents': agents}), encoding='utf-8')
    return path


ENTRY = {'label': 'Cursor', 'kind': 'cli', 'command': 'agent', 'capabilities': ['coding', 'review'],
         'cost': 'subscription', 'status': 'probation', 'max_tier': 'standard',
         'instructions': ['AGENTS.md', 'AGENTS_CONTEXT.md']}


def test_shipped_roster_lists_current_and_new_employees():
    loaded = load_roster()
    assert {'codex', 'claude-code', 'cursor'} <= set(loaded.agents)
    assert loaded.agents['cursor'].status == 'probation'
    assert loaded.agents['cursor'].max_tier == 'standard'
    assert loaded.agents['codex'].status == 'active'


def test_board_prefixes_include_builtins_and_exclude_retired(tmp_path):
    path = write(tmp_path, {'cursor': ENTRY, 'old-bot': {**ENTRY, 'label': 'Old', 'status': 'retired'}})
    prefixes = load_roster(path).board_prefixes()
    assert {'cursor', 'user', 'local'} <= prefixes and 'old-bot' not in prefixes


def test_runtimes_are_non_retired_employee_ids(tmp_path):
    path = write(tmp_path, {'cursor': ENTRY, 'gone': {**ENTRY, 'status': 'retired'}})
    assert load_roster(path).runtimes() == {'cursor'}


@pytest.mark.parametrize('change', [
    {'status': 'boss'}, {'max_tier': 'root'}, {'capabilities': ['hacking']}, {'cost': 'cheap'},
    {'kind': 'robot'}, {'unexpected': 1}, {'command': 'rm -rf /'}, {'instructions': ['../secret.md']},
])
def test_invalid_entries_are_rejected(tmp_path, change):
    with pytest.raises(RosterError):
        load_roster(write(tmp_path, {'cursor': {**ENTRY, **change}}))


@pytest.mark.parametrize('name', ['Cursor', 'user', 'local', 'a', 'x' * 40, 'bad_name'])
def test_invalid_or_reserved_ids_are_rejected(tmp_path, name):
    with pytest.raises(RosterError):
        load_roster(write(tmp_path, {name: ENTRY}))


def test_wrong_version_and_missing_file_are_rejected(tmp_path):
    with pytest.raises(RosterError):
        load_roster(write(tmp_path, {'cursor': ENTRY}, version=2))
    with pytest.raises(RosterError):
        load_roster(tmp_path / 'missing.json')


def test_roster_inside_workspace_is_refused(tmp_path, monkeypatch):
    path = write(tmp_path, {'cursor': ENTRY})
    with pytest.raises(RosterError, match='workspace'):
        load_roster(path, workspace=tmp_path)


def test_instructions_point_to_existing_project_files():
    for agent in load_roster().agents.values():
        for name in agent.instructions:
            assert (roster.PROJECT_ROOT / name).is_file(), name
