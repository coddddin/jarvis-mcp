"""W4-3 probation review: promotion suggestions from exact evaluation, never a roster change."""
import json
import typing

import pytest

from jarvis.agents import promotion
from jarvis.agents import worker_adapters as wa
from jarvis.agents.dispatch_policy import build_digest
from jarvis.cli import promotion as cli
from jarvis.core.guardrails import Guardrails
from jarvis.core.roster import ROSTER_PATH, ProblemCode, RosterError, load_roster
from jarvis.features.board.service import BoardStore


def _roster(tmp_path, *, block=None, statuses=None):
    data = json.loads(ROSTER_PATH.read_text(encoding='utf-8'))
    data.pop('promotion', None)
    if block is not None:
        data['promotion'] = block
    for name, status in (statuses or {}).items():
        data['agents'][name]['status'] = status
    path = tmp_path / 'agents.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return load_roster(path)


def _report(**rows):
    return {'employees': {name: {'exact_done': done, 'exact_reworked_done': reworked}
                          for name, (done, reworked) in rows.items()}}


def test_problem_code_lists_stay_equal():
    assert typing.get_args(ProblemCode) == wa.PROBLEM_CODES


def test_roster_without_block_uses_defaults_and_block_is_strict(tmp_path):
    assert _roster(tmp_path).promotion.model_dump() == {
        'min_exact_done': 5, 'max_rework_ratio': 0.2, 'blocking_problems': ['privacy_open', 'probation_can_edit']}
    assert _roster(tmp_path, block={'min_exact_done': 2}).promotion.min_exact_done == 2
    for bad in ({'min_exact_done': 0}, {'max_rework_ratio': 1.5}, {'blocking_problems': ['unknown']}, {'extra': 1}):
        with pytest.raises(RosterError):
            _roster(tmp_path, block=bad)


def test_the_real_roster_carries_the_block():
    assert json.loads(ROSTER_PATH.read_text(encoding='utf-8'))['promotion'] == load_roster().promotion.model_dump()


def test_candidate_needs_enough_exact_done_low_rework_and_no_blocking_problem(tmp_path):
    roster = _roster(tmp_path, block={'min_exact_done': 3, 'max_rework_ratio': 0.34})
    result = promotion.review(roster, _report(cursor=(3, 1), opencode=(2, 0), codex=(50, 0)),
                              {'opencode': ['privacy_open']})
    assert result['candidates'] == [{'employee': 'cursor', 'done': 3, 'reworked': 1, 'ratio': 0.333}]
    (opencode,) = result['not_ready']
    assert opencode['employee'] == 'opencode'
    assert opencode['reasons'] == ['정확 집계 done 2건 < 기준 3건', '어댑터 점검: privacy_open']
    assert 'codex' not in json.dumps(result)            # active employees are not reviewed
    assert '사용자' in result['note']


def test_no_exact_record_and_high_rework_are_reported(tmp_path):
    roster = _roster(tmp_path)
    result = promotion.review(roster, _report(cursor=(10, 5)), {})
    reasons = {item['employee']: item['reasons'] for item in result['not_ready']}
    assert reasons['opencode'] == [promotion.NO_RECORD]
    assert reasons['cursor'] == ['재작업 비율 0.5 > 기준 0.2']
    assert result['candidates'] == []


def test_only_configured_problems_block(tmp_path):
    report, codes = _report(cursor=(5, 0)), {'cursor': ['label_mismatch']}
    assert promotion.review(_roster(tmp_path), report, codes)['candidates'][0]['employee'] == 'cursor'
    strict = _roster(tmp_path, block={'blocking_problems': ['label_mismatch']})
    assert promotion.review(strict, report, codes)['candidates'] == []


def test_candidate_and_retired_statuses_are_not_reviewed(tmp_path):
    roster = _roster(tmp_path, statuses={'cursor': 'retired', 'opencode': 'active'})
    result = promotion.review(roster, _report(cursor=(9, 0), opencode=(9, 0)), {})
    assert result['candidates'] == [] and result['not_ready'] == []


def test_digest_line_only_when_there_are_candidates(tmp_path):
    roster = _roster(tmp_path)
    ready = promotion.review(roster, _report(cursor=(5, 0)), {})
    assert promotion.digest_line(ready) == '- 승격 후보: cursor(done 5, 재작업 0)'
    assert promotion.digest_line(promotion.review(roster, _report(), {})) is None
    base = dict(day='d', done=[], review=[], stale=[], runs={}, exhausted={}, pause={})
    assert '승격' not in build_digest(**base)
    assert build_digest(**base, promotion=promotion.digest_line(ready)).endswith('- 승격 후보: cursor(done 5, 재작업 0)')


def test_review_from_a_real_board_counts_only_exact_work_and_never_edits_the_roster(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    roster = _roster(tmp_path, block={'min_exact_done': 1, 'blocking_problems': []})
    before = (tmp_path / 'agents.json').read_bytes()
    board = BoardStore(Guardrails(workspace), roster=roster)
    task = board.create('cursor:auto', 'Review', '', ['a.py'])['id']
    board.claim('cursor:auto', task)
    board.release('cursor:auto', task, 'done', 'reviewed', verification='read diff')
    result = cli.promotion_review(Guardrails(workspace), now=0, roster=roster)
    assert result['candidates'] == [{'employee': 'cursor', 'done': 1, 'reworked': 0, 'ratio': 0.0}]
    assert (tmp_path / 'agents.json').read_bytes() == before


def test_roster_consumers_work_with_and_without_the_block(tmp_path):
    # Every Python reader goes through load_roster; check the board, the dispatcher and the adapter check.
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.agents.dispatch_policy import DispatcherConfig
    config = DispatcherConfig.model_validate({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'cursor': {}}})
    for index, block in enumerate((None, {'min_exact_done': 7})):
        folder = tmp_path / str(index)
        (folder / 'ws').mkdir(parents=True)
        roster = _roster(folder, block=block)
        board = BoardStore(Guardrails(folder / 'ws'), roster=roster)
        board.create('cursor:auto', 'T', '', ['a.py'])
        assert wa.check_roster(roster) == wa.check_roster(load_roster())
        assert isinstance(cli_dispatch.blocked_now(roster, config, board), list)
        assert roster.board_prefixes() == load_roster().board_prefixes()


def test_cli_prints_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('JARVIS_WORKSPACE', str(tmp_path))
    assert cli.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {'criteria', 'candidates', 'not_ready', 'note'}
