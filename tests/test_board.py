"""Shared task board: claims, path conflicts, leases, handoffs and concurrency."""
import asyncio
import json
from pathlib import Path
import sys
import threading

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jarvis.core.guardrails import Guardrails
from jarvis.features.board.service import BoardError, BoardStore

CODEX, CLAUDE = 'codex:main', 'claude-code:s1'


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def board(tmp_path, clock):
    return BoardStore(Guardrails(tmp_path), clock=clock)


def make(board, agent=CODEX, paths=('jarvis/app/chat.py',), **extra):
    return board.create(agent, 'Fix chat routing', 'details', list(paths), **extra)['id']


def test_full_flow_create_claim_note_release(board):
    task = make(board)
    claimed = board.claim(CLAUDE, task)
    assert claimed['status'] == 'claimed' and claimed['owner'] == CLAUDE
    board.note(CLAUDE, task, 'finding', 'Found the bug in resolve_auto.', ['jarvis/app/chat.py'])
    done = board.release(CLAUDE, task, 'done', 'Fixed and tested.', verification='pytest -q: 511 passed',
                         commit='308f755')
    assert done['status'] == 'done' and done['lease_until'] is None
    detail = board.get(task)
    kinds = [note['kind'] for note in detail['notes']]
    assert kinds == ['finding', 'handoff']
    assert detail['notes'][-1]['refs'] == ['commit:308f755']
    assert 'pytest -q: 511 passed' in detail['notes'][-1]['body']


def test_overlapping_paths_are_refused_for_another_agent(board):
    first = make(board, paths=['jarvis/app/'])
    board.claim(CODEX, first)
    overlap = make(board, paths=['jarvis/app/chat.py'])
    with pytest.raises(BoardError, match='overlap'):
        board.claim(CLAUDE, overlap)
    separate = make(board, paths=['jarvis/features/web/service.py'])
    assert board.claim(CLAUDE, separate)['status'] == 'claimed'
    # Prefix match is per path segment: jarvis/application is not inside jarvis/app.
    sibling = make(board, paths=['jarvis/application.py'])
    assert board.claim(CLAUDE, sibling)['status'] == 'claimed'


def test_same_agent_may_renew_and_overlap_itself(board):
    first = make(board, paths=['docs/'])
    board.claim(CODEX, first)
    again = board.claim(CODEX, first, lease_minutes=60)
    assert again['owner'] == CODEX
    second = make(board, paths=['docs/PLAN.md'])
    assert board.claim(CODEX, second)['status'] == 'claimed'


def test_expired_lease_is_stale_and_can_be_taken_over_with_a_note(board, clock):
    task = make(board)
    board.claim(CODEX, task, lease_minutes=30)
    clock.now += 31 * 60
    listed = {item['id']: item for item in board.list()['tasks']}
    assert listed[task]['stale'] is True
    taken = board.claim(CLAUDE, task)
    assert taken['owner'] == CLAUDE
    notes = board.get(task)['notes']
    assert notes[-1]['author'] == CLAUDE and 'stale' in notes[-1]['body']
    assert CODEX in notes[-1]['body']


def test_expired_lease_no_longer_blocks_paths(board, clock):
    first = make(board, paths=['jarvis/'])
    board.claim(CODEX, first, lease_minutes=5)
    clock.now += 6 * 60
    other = make(board, paths=['jarvis/app/chat.py'])
    assert board.claim(CLAUDE, other)['status'] == 'claimed'


def test_only_owner_can_heartbeat_or_release(board, clock):
    task = make(board)
    board.claim(CODEX, task, lease_minutes=10)
    with pytest.raises(BoardError, match='owner'):
        board.heartbeat(CLAUDE, task)
    with pytest.raises(BoardError, match='owner'):
        board.release(CLAUDE, task, 'open', 'not mine')
    clock.now += 5 * 60
    renewed = board.heartbeat(CODEX, task, lease_minutes=10)
    assert renewed['lease_until'] == pytest.approx(clock.now + 600)


def test_done_requires_verification_and_valid_commit(board):
    task = make(board)
    board.claim(CODEX, task)
    with pytest.raises(BoardError, match='verification'):
        board.release(CODEX, task, 'done', 'finished')
    with pytest.raises(BoardError, match='commit'):
        board.release(CODEX, task, 'done', 'finished', verification='pytest ok', commit='not-a-sha')
    assert board.release(CODEX, task, 'review', 'please review')['status'] == 'review'


def test_unfinished_dependency_blocks_claim(board):
    base = make(board, paths=['a.py'])
    child = make(board, paths=['b.py'], depends_on=[base])
    with pytest.raises(BoardError, match='depend'):
        board.claim(CLAUDE, child)
    board.claim(CODEX, base)
    board.release(CODEX, base, 'done', 'ok', verification='tests passed')
    assert board.claim(CLAUDE, child)['status'] == 'claimed'


@pytest.mark.parametrize('path', ['../outside.py', '/etc/passwd', 'C:/Windows', 'a/../../b', '', 'a\x00b',
                                  'JarvisWorkspace/x', '.jarvis-auth/token.json'])
def test_invalid_or_sensitive_paths_are_rejected(board, path):
    with pytest.raises(BoardError):
        board.create(CODEX, 'Bad path', '', [path])


def test_backslash_paths_are_normalized(board):
    task = board.create(CODEX, 'Win path', '', ['jarvis\\app\\chat.py'])
    assert task['paths'] == ['jarvis/app/chat.py']


@pytest.mark.parametrize('agent', ['codex', 'root:x', 'claude-code:', 'codex:a b', 'x' * 80])
def test_invalid_agent_ids_are_rejected(board, agent):
    with pytest.raises(BoardError, match='agent'):
        board.create(agent, 'Title', '', ['a.py'])


def test_roster_employees_can_use_the_board(board):
    # Cursor joined through config/agents.json alone; no board code lists it.
    task = make(board, agent='cursor:main')
    assert board.claim('cursor:main', task)['owner'] == 'cursor:main'


def test_retired_or_unknown_employees_are_refused(tmp_path, clock):
    import json
    from jarvis.core.roster import load_roster
    path = tmp_path / 'agents.json'
    path.write_text(json.dumps({'version': 1, 'agents': {'old-bot': {
        'label': 'Old', 'kind': 'cli', 'capabilities': ['coding'], 'cost': 'free',
        'status': 'retired', 'max_tier': 'light'}}}), encoding='utf-8')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    store = BoardStore(Guardrails(workspace), clock=clock, roster=load_roster(path))
    for agent in ('old-bot:main', 'cursor:main'):
        with pytest.raises(BoardError, match='agent'):
            store.create(agent, 'Title', '', ['a.py'])
    assert store.create('user:heejun', 'Title', '', ['a.py'])['creator'] == 'user:heejun'


def test_credentials_in_text_are_rejected(board):
    with pytest.raises(BoardError, match='credential'):
        board.create(CODEX, 'Leak', 'password=hunter2-secret', ['a.py'])
    task = make(board)
    with pytest.raises(BoardError, match='credential'):
        board.note(CODEX, task, 'finding', 'token: ghp_abcdefghijklmnopqrstuvwxyz0123456789')


def test_list_filters_and_reports_counts(board):
    a, b = make(board, paths=['a.py']), make(board, paths=['b.py'])
    board.claim(CODEX, a)
    claimed = board.list(status='claimed')['tasks']
    assert [item['id'] for item in claimed] == [a]
    mine = board.list(agent=CODEX)['tasks']
    assert {item['id'] for item in mine} == {a, b}  # creator or owner
    with pytest.raises(BoardError):
        board.list(status='unknown')


def test_concurrent_claims_have_a_single_winner(tmp_path, clock):
    guard = Guardrails(tmp_path)
    task = BoardStore(guard, clock=clock).create(CODEX, 'Race', '', ['race.py'])['id']
    results, barrier = [], threading.Barrier(4)

    def attempt(index):
        store = BoardStore(guard, clock=clock)
        barrier.wait()
        try:
            store.claim(f'claude-code:w{index}', task)
            results.append('won')
        except BoardError:
            results.append('lost')

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count('won') == 1 and results.count('lost') == 3


def test_two_mcp_clients_share_the_board_over_stdio(tmp_path):
    # Two real server processes on one workspace, as Codex and Claude Code would run them.
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    server = str(Path(__file__).resolve().parents[1] / 'server.py')
    params = StdioServerParameters(command=sys.executable, args=[server], env={'JARVIS_WORKSPACE': str(workspace)})

    async def call(session, name, args):
        result = await session.call_tool(name, args)
        return result.isError, (result.content[0].text if result.content else '')

    async def exercise():
        async with stdio_client(params) as (r1, w1), stdio_client(params) as (r2, w2):
            async with ClientSession(r1, w1) as codex, ClientSession(r2, w2) as claude:
                await codex.initialize()
                await claude.initialize()
                error, text = await call(codex, 'board_create', {'agent': CODEX, 'title': 'Chat fix', 'paths': ['jarvis/app/']})
                assert not error
                first = json.loads(text)['id']
                assert not (await call(codex, 'board_claim', {'agent': CODEX, 'task_id': first}))[0]
                error, text = await call(claude, 'board_create', {'agent': CLAUDE, 'title': 'Overlap', 'paths': ['jarvis/app/chat.py']})
                second = json.loads(text)['id']
                error, text = await call(claude, 'board_claim', {'agent': CLAUDE, 'task_id': second})
                assert error and 'overlap' in text
                error, _ = await call(codex, 'board_release', {'agent': CODEX, 'task_id': first, 'status': 'done',
                                                              'summary': 'Done', 'verification': 'pytest ok'})
                assert not error
                assert not (await call(claude, 'board_claim', {'agent': CLAUDE, 'task_id': second}))[0]
                error, text = await call(claude, 'board_get', {'task_id': first})
                assert json.loads(text)['notes'][-1]['body'].startswith('Done')
    asyncio.run(exercise())


def test_board_storage_stays_inside_workspace(board, tmp_path):
    make(board)
    assert (tmp_path / '.jarvis-board' / 'board.sqlite3').is_file()
