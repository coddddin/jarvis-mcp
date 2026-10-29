"""Web benchmark scoring rules (scripts/web_eval): pure checks, no network or model."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'scripts' / 'web_eval'
spec = importlib.util.spec_from_file_location('web_eval_score', ROOT / 'score.py')
score_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score_module)
score = score_module.score
TASKS = {task['id']: task for task in json.loads((ROOT / 'tasks.json').read_text(encoding='utf-8'))['tasks']}


def test_task_file_has_ten_tasks_with_eight_korean_one_english_one_download():
    assert sorted(TASKS) == [f't{index:02d}' for index in range(1, 11)]
    korean = [task for task in TASKS.values() if any('가' <= char <= '힣' for char in task['prompt'])]
    assert len(korean) == 9 and sum('download' in task for task in TASKS.values()) == 1   # 8 info + 1 download
    assert all(task['source_hosts'] for task in TASKS.values())


def test_an_answer_passes_with_the_value_and_an_official_source_cited_or_visited():
    task = TASKS['t01']
    assert score(task, '시간급 10,320원입니다.\nSources: https://www.minimumwage.go.kr/main.do', [], [])['pass']
    assert score(task, '시간급 10320원', ['https://www.moel.go.kr/info'], [])['pass']


def test_wrong_value_empty_answer_or_unofficial_source_fails():
    task = TASKS['t06']
    assert not score(task, '52,300원 https://www.korail.com/', [], [])['pass']
    assert not score(task, '', ['https://www.korail.com/'], [])['checks']['answered']
    assert not score(task, '59,800원 https://blog.example.com/ktx', [], [])['checks']['sourced']


def test_search_pages_are_neither_sources_nor_followed_pages():
    task = TASKS['t05']
    search = ['https://www.bing.com/search?q=x&site=ko.wikipedia.org', 'https://ko.wikipedia.org/wiki/세종']
    result = score(task, '국보 제70호', search, [])
    assert result['checks']['sourced'] and not result['checks']['followed_links']
    assert score(task, '국보 제70호', search + ['https://ko.wikipedia.org/wiki/훈민정음'], [])['pass']


def test_host_matching_is_by_domain_suffix_not_substring():
    assert score_module.host_matches('www.weather.go.kr', ['weather.go.kr'])
    assert not score_module.host_matches('weather.go.kr.evil.example', ['weather.go.kr'])
    assert not score_module.host_matches('notpython.org', ['python.org'])


def test_download_needs_an_allowed_small_file_that_scanned_clean():
    task = TASKS['t10']
    clean = [{'name': 'data.csv', 'bytes': 1000, 'clean': True}]
    assert score(task, '받았습니다', ['https://www.data.go.kr/data/1/fileData.do'], clean)['pass']
    for files in ([], [{'name': 'data.csv', 'bytes': 9_000_000, 'clean': True}],
                  [{'name': 'data.csv', 'bytes': 1000, 'clean': None}], [{'name': 'tool.exe', 'bytes': 10, 'clean': True}]):
        assert not score(task, '받았습니다', ['https://www.data.go.kr/'], files)['pass']
