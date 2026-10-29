"""Web task agent (M2): numbered coarse actions, the URL gate around the browser, loop guard, answer forcing,
quarantined downloads."""
import base64
import socket

import pytest

from jarvis.core.guardrails import GuardrailError, Guardrails
import jarvis.features.web_agent.service as agent
from jarvis.features.web_agent.service import (Browser, WebAgent, WebTaskError, bing_target, check_public,
                                               parse_bing, parse_snapshot, store_download)
from jarvis.features.web.service import WebError
from jarvis.workers.web_https import FetchError

PUBLIC = {'www.bing.com', 'www.weather.go.kr', 'example.org', 'files.example.org'}


def resolve(host):
    if host in PUBLIC:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))]
    raise FetchError('DNS destination is not a public Internet address.')


def ck(url):
    return 'https://www.bing.com/ck/a?!&&p=x&u=a1' + base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


def snapshot(url, title, yaml):
    return f'### Page\n- Page URL: {url}\n- Page Title: {title}\n### Snapshot\n```yaml\n{yaml}\n```'


BING = snapshot('https://www.bing.com/search?q=x', 'x - Search', f'''- main [ref=e1]:
  - list [ref=e2]:
    - listitem [ref=e3]:
      - heading "기상청 날씨누리" [level=2] [ref=e4]:
        - link "기상청 날씨누리" [ref=e5] [cursor=pointer]:
          - /url: {ck("https://www.weather.go.kr/w/index.do")}
      - paragraph [ref=e6]: 오늘 제주 날씨 예보
    - listitem [ref=e7]:
      - heading "Bing 관련 검색" [level=2] [ref=e8]:
        - link "관련" [ref=e9]:
          - /url: https://www.bing.com/search?q=y''')

PAGE = snapshot('https://example.org/a', 'Example', '''- banner [ref=e1]:
  - link "Home" [ref=e2]:
    - /url: https://example.org/
  - button "로그인" [ref=e3]
- main [ref=e4]:
  - heading "Report 2026" [level=1] [ref=e5]
  - paragraph [ref=e6]: The value is 42.
  - link "report.csv" [ref=e7]:
    - /url: https://files.example.org/report.csv
  - button "더 보기" [ref=e8]''')


# --- parsing and URLs ---

def test_bing_results_decode_the_click_link_and_drop_bing_links():
    assert parse_bing(BING) == [{'title': '기상청 날씨누리', 'url': 'https://www.weather.go.kr/w/index.do',
                                 'snippet': '오늘 제주 날씨 예보'}]
    assert bing_target('https://example.org/x') == 'https://example.org/x'


def test_a_snapshot_gives_text_ranked_links_and_buttons():
    page = parse_snapshot(PAGE)
    assert page['url'] == 'https://example.org/a' and page['title'] == 'Example'
    assert 'The value is 42.' in page['text'] and 'Home' not in page['text']     # page chrome is not body text
    ranks = {link['text']: link['rank'] for link in page['links']}
    assert ranks == {'Home': 2, 'report.csv': 0}
    assert [(b['text'], b['ref']) for b in page['buttons']] == [('로그인', 'e3'), ('더 보기', 'e8')]


@pytest.mark.parametrize('url', ['http://example.org/', 'https://localhost/', 'https://192.168.0.1/',
                                 'https://example.org:8443/', 'https://user:pw@example.org/',
                                 'https://example.org/?token=abc', 'https://intranet.local/'])
def test_only_public_https_addresses_pass(url):
    with pytest.raises(WebTaskError):
        check_public(url, resolve)


def test_non_ascii_is_encoded_and_the_fragment_dropped():
    assert check_public('https://example.org/검색#top', resolve) == 'https://example.org/%EA%B2%80%EC%83%89'


# --- browser gate ---

class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls, self.current = pages, [], 'about:blank'

    def call(self, name, arguments):
        tool = name.split('/', 1)[1]
        self.calls.append((tool, arguments))
        if tool == 'browser_navigate':
            self.current = arguments['url']
            return {'text': 'navigated', 'is_error': False}
        if tool == 'browser_click':
            self.current = self.pages.get(('click', arguments['target']), self.current)
            return {'text': 'clicked', 'is_error': False}
        if tool == 'browser_snapshot':
            return {'text': self.pages.get(self.current) or snapshot(self.current, '', '- main:\n  - paragraph: x'),
                    'is_error': False}
        raise AssertionError(tool)


def browser(pages, tmp_path):
    return Browser(FakeSession(pages), 'pw', tmp_path / 'out', resolve=resolve, sleep=lambda s: None)


def test_the_browser_never_navigates_to_a_private_address(tmp_path):
    b = browser({}, tmp_path)
    with pytest.raises(WebTaskError):
        b.open('https://localhost/admin')
    assert b.session.calls == []


def test_a_redirect_to_a_private_address_is_left_unread(tmp_path):
    b = browser({'https://example.org/go': snapshot('https://10.0.0.1/', 'router', '- main:\n  - paragraph: x')},
                tmp_path)
    with pytest.raises(WebTaskError, match='not public'):
        b.open('https://example.org/go')
    assert b.session.calls[-1] == ('browser_navigate', {'url': 'about:blank'})


# --- the loop ---

class Collector:
    def __init__(self, pages=None):
        self.pages, self.read = pages or {}, []

    def read_page(self, url):
        self.read.append(url)
        if url not in self.pages:
            raise WebError('No visible page text was returned.')
        return {'url': url, 'title': 'T', 'excerpt': self.pages[url], 'links': []}


def scripted(*decisions):
    seen = []

    def decide(messages, schema, deadline):
        seen.append((messages, schema))
        step = decisions[min(len(seen) - 1, len(decisions) - 1)]
        return {'tool': 'answer', 'query': '', 'number': 0, 'answer': '', 'sources': [], **step}
    decide.seen = seen
    return decide


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'ws').mkdir()
    return Guardrails(tmp_path / 'ws')


def make(guard, tmp_path, decide, pages=None, collector=None, **kwargs):
    return WebAgent(guard, collector or Collector(), browser(pages or {}, tmp_path), decide,
                    downloads_dir=tmp_path / 'downloads', **kwargs)


def test_search_open_answer_with_the_sources_jarvis_appends(guard, tmp_path):
    text = '제주 오늘 최고기온 21도. ' * 40
    decide = scripted({'tool': 'search', 'query': '제주 날씨'}, {'tool': 'open', 'number': 1},
                      {'tool': 'answer', 'answer': '최고 21도', 'sources': [2]})
    pages = {agent.bing_url('제주 날씨'): BING}
    result = make(guard, tmp_path, decide, pages, Collector({'https://www.weather.go.kr/w/index.do': text})).run(
        '제주 날씨 알려줘')
    assert result['status'] == 'completed' and result['calls'] == 2
    assert result['answer'].startswith('최고 21도') and 'https://www.weather.go.kr/w/index.do' in result['answer']
    assert [t['tool'] for t in result['traces']] == ['search', 'open']
    first = decide.seen[0][1]['properties']
    assert first['tool']['enum'] == ['search', 'answer'] or first['tool']['enum'] == ['search']
    assert 'download' not in decide.seen[1][1]['properties']['tool']['enum']      # not a download request


def test_short_https_text_falls_back_to_the_browser(guard, tmp_path):
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'answer', 'answer': '42', 'sources': [2]})
    collector = Collector({'https://example.org/a': 'too short'})
    result = make(guard, tmp_path, decide, {'https://example.org/a': PAGE}, collector).run(
        'https://example.org/a 의 값은?')
    assert result['traces'][-1]['source'] == 'browser'
    assert 'The value is 42.' in decide.seen[1][0][-2]['content']


def test_an_unoffered_number_or_tool_is_refused_and_the_answer_forced(guard, tmp_path):
    decide = scripted({'tool': 'open', 'number': 99}, {'tool': 'delete'}, {'tool': 'open', 'number': 7},
                      {'tool': 'answer', 'answer': '모름'})
    result = make(guard, tmp_path, decide).run('아무거나')
    assert result['status'] == 'completed' and result['calls'] == 0
    assert decide.seen[-1][1]['properties']['tool']['enum'] == ['answer']


def test_a_model_that_never_answers_ends_as_failed(guard, tmp_path):
    result = make(guard, tmp_path, scripted({'tool': 'answer', 'answer': ''})).run('질문')
    assert result['status'] == 'failed' and '답을 쓰지 않았' in result['reason']


def test_an_opened_item_is_not_offered_again(guard, tmp_path):
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'answer', 'answer': 'x'})
    make(guard, tmp_path, decide, {'https://example.org/a': PAGE}).run('https://example.org/a')
    assert 1 not in decide.seen[1][1]['properties']['number']['enum']


def test_the_call_limit_forces_the_answer(guard, tmp_path):
    decide = scripted(*[{'tool': 'search', 'query': f'q{i}'} for i in range(5)], {'tool': 'answer', 'answer': 'a'})
    result = make(guard, tmp_path, decide, limits={'calls': 2}).run('q')
    assert result['calls'] == 2 and result['status'] == 'completed'


def test_the_stop_marker_ends_the_task(guard, tmp_path):
    (guard.workspace / '.jarvis-stop').write_text('', encoding='utf-8')
    with pytest.raises(GuardrailError):
        make(guard, tmp_path, scripted({'tool': 'answer', 'answer': 'x'})).run('q')


def test_login_and_payment_buttons_are_not_pressed(guard, tmp_path):
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'press', 'number': 6}, {'tool': 'press', 'number': 5},
                      {'tool': 'answer', 'answer': 'x'})
    agent_ = make(guard, tmp_path, decide, {'https://example.org/a': PAGE})
    agent_.run('https://example.org/a')
    page = decide.seen[1][0][-2]['content']
    assert '[5] 더 보기' in page and '로그인' not in page                     # never offered
    assert decide.seen[1][1]['properties']['number']['enum'].count(6) == 0
    targets = [args.get('target') for tool, args in agent_.browser.session.calls if tool == 'browser_click']
    assert targets == ['e8']
    with pytest.raises(WebTaskError, match='누르지 않는다'):                 # second layer at press time
        agent_._act(agent.TaskState('q', items={9: {'kind': 'button', 'text': '결제하기', 'ref': 'e3'}}), 'press',
                    {'number': 9})


# --- downloads ---

def test_download_is_offered_only_when_asked(guard, tmp_path):
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'answer', 'answer': 'x'})
    make(guard, tmp_path, decide, {'https://example.org/a': PAGE}).run('https://example.org/a 파일 다운로드 해줘')
    assert 'download' in decide.seen[1][1]['properties']['tool']['enum']


def test_a_clean_download_moves_out_of_quarantine(tmp_path):
    quarantine = tmp_path / 'q'
    quarantine.mkdir()
    (quarantine / 'report.csv').write_text('a,b\n1,2\n', encoding='utf-8')
    record = store_download(quarantine / 'report.csv', tmp_path / 'downloads', scan=lambda path: True)
    assert record['status'] == 'saved' and (tmp_path / 'downloads' / 'report.csv').read_text() == 'a,b\n1,2\n'


@pytest.mark.parametrize('name, data, verdict, status', [
    ('tool.exe', b'MZ', True, 'refused'),
    ('fake.pdf', b'MZ\x90\x00', True, 'refused'),
    ('doc.pdf', b'%PDF-1.7', False, 'threat'),
    ('doc.pdf', b'%PDF-1.7', None, 'unscanned'),
])
def test_unsafe_downloads_never_leave_quarantine(tmp_path, name, data, verdict, status):
    path = tmp_path / name
    path.write_bytes(data)
    record = store_download(path, tmp_path / 'downloads', scan=lambda p: verdict)
    assert record['status'] == status and not (tmp_path / 'downloads').exists()
    assert path.exists() == (status == 'unscanned')


def test_the_download_action_scans_and_reports_the_file(guard, tmp_path):
    out = tmp_path / 'out'
    out.mkdir()

    class Saving(FakeSession):
        def call(self, name, arguments):
            if arguments.get('url', '').endswith('.csv'):
                (out / 'report.csv').write_text('a,b\n', encoding='utf-8')
            return super().call(name, arguments)

    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'download', 'number': 3},
                      {'tool': 'answer', 'answer': '저장했다'})
    b = Browser(Saving({'https://example.org/a': PAGE}), 'pw', out, resolve=resolve, sleep=lambda s: None)
    result = WebAgent(guard, Collector(), b, decide, downloads_dir=tmp_path / 'downloads',
                      scan=lambda p: True).run('https://example.org/a 에서 csv 내려받아줘')
    assert [f['status'] for f in result['files']] == ['saved']
    assert (tmp_path / 'downloads' / 'report.csv').exists() and '내려받은 파일' in result['answer']


def test_one_address_keeps_one_number_and_opened_pages_are_not_linked_again(guard, tmp_path):
    state = agent.TaskState('q')
    assert state.add('result', 'a', url='https://example.org/a') == state.add('link', 'a', url='https://example.org/a')
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'answer', 'answer': 'x'})
    make(guard, tmp_path, decide, {'https://example.org/a': PAGE}).run('https://example.org/a')
    assert '[1]' not in decide.seen[1][0][-2]['content'].split('링크:', 1)[1]


def test_a_failed_navigation_does_not_reread_the_old_page(tmp_path):
    class Stuck(FakeSession):
        def call(self, name, arguments):
            if arguments.get('url') == 'https://example.org/broken':
                return {'text': 'navigated', 'is_error': False}          # but the browser stayed where it was
            return super().call(name, arguments)
    b = Browser(Stuck({'https://example.org/a': PAGE}), 'pw', tmp_path, resolve=resolve, sleep=lambda s: None)
    b.open('https://example.org/a')
    with pytest.raises(WebTaskError, match='did not open'):
        b.open('https://example.org/broken')


def test_a_search_page_is_never_read_as_a_source(tmp_path):
    b = browser({'https://example.org/x': snapshot('https://www.bing.com/search?q=x', 'x', '- main:\n  - paragraph: x')},
                tmp_path)
    with pytest.raises(WebTaskError, match='search page'):
        b.open('https://example.org/x')


def test_download_tasks_list_file_links_and_buttons_first(guard, tmp_path):
    many = '\n'.join(f'  - button "메뉴 {i}" [ref=m{i}]' for i in range(12))
    page = snapshot('https://example.org/a', 'Data', f'- main:\n  - paragraph: data\n{many}\n  - button "다운로드" [ref=d1]')
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'answer', 'answer': 'x'})
    make(guard, tmp_path, decide, {'https://example.org/a': page}).run('https://example.org/a 파일 내려받아줘')
    buttons = decide.seen[1][0][-2]['content'].split('버튼:', 1)[1]
    assert buttons.split('|')[0].strip().endswith('다운로드')


def test_public_body_hosts_are_marked_in_results(guard, tmp_path):
    decide = scripted({'tool': 'search', 'query': '제주 날씨'}, {'tool': 'answer', 'answer': 'x'})
    make(guard, tmp_path, decide, {agent.bing_url('제주 날씨'): BING}).run('제주 날씨')
    assert 'www.weather.go.kr (공공기관)' in decide.seen[1][0][-2]['content']

def test_a_repeated_search_opens_the_best_result_instead(guard, tmp_path):
    decide = scripted({'tool': 'search', 'query': '제주 날씨'}, {'tool': 'search', 'query': '제주  날씨'},
                      {'tool': 'answer', 'answer': 'x'})
    text = '제주 오늘 최고기온 21도. ' * 40
    result = make(guard, tmp_path, decide, {agent.bing_url('제주 날씨'): BING},
                  Collector({'https://www.weather.go.kr/w/index.do': text})).run('제주 날씨')
    assert [t['tool'] for t in result['traces']] == ['search', 'open']          # JARVIS opened the best result
    assert result['calls'] == 2 and 'https://www.weather.go.kr/w/index.do' in result['sources']

def test_a_saved_file_carries_the_mark_of_the_web(tmp_path):
    (tmp_path / 'r.csv').write_text('a\n', encoding='utf-8')
    record = store_download(tmp_path / 'r.csv', tmp_path / 'downloads', scan=lambda p: True,
                            source_url='https://files.example.org/r.csv')
    assert record['marked'] is True
    mark = open(record['path'] + ':Zone.Identifier', encoding='ascii').read()
    assert 'ZoneId=3' in mark and 'HostUrl=https://files.example.org/r.csv' in mark


def test_extra_new_files_are_deleted_not_left_unscanned(guard, tmp_path):
    out = tmp_path / 'out'
    out.mkdir()

    class Many(FakeSession):
        def call(self, name, arguments):
            if arguments.get('url', '').endswith('.csv'):
                for i in range(5):
                    (out / f'f{i}.csv').write_text('a\n', encoding='utf-8')
            return super().call(name, arguments)

    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'download', 'number': 3}, {'tool': 'answer', 'answer': 'x'})
    b = Browser(Many({'https://example.org/a': PAGE}), 'pw', out, resolve=resolve, sleep=lambda s: None)
    result = WebAgent(guard, Collector(), b, decide, downloads_dir=tmp_path / 'downloads',
                      scan=lambda p: True).run('https://example.org/a csv 내려받아줘')
    assert [f['status'] for f in result['files']] == ['saved'] * 3 + ['refused'] * 2
    assert not [p for p in out.iterdir() if p.suffix == '.csv']


def test_an_answer_without_an_official_page_is_sent_back_once(guard, tmp_path):
    blog = snapshot('https://example.org/a', 'Blog', '- main:\n  - paragraph: 요금 1,550원')
    named = agent.TaskState('요금은? example.org', pages={1: {'url': 'https://example.org/a'}})
    assert WebAgent._official(named)                        # a host the request names counts as official
    decide = scripted({'tool': 'search', 'query': '요금'}, {'tool': 'open', 'number': 1},
                      {'tool': 'answer', 'answer': '1,550원'}, {'tool': 'answer', 'answer': '1,550원'})
    bing = BING.replace(ck('https://www.weather.go.kr/w/index.do'), ck('https://example.org/a'))
    result = make(guard, tmp_path, decide, {agent.bing_url('요금'): bing, 'https://example.org/a': blog}).run('요금은?')
    assert result['status'] == 'completed' and len(decide.seen) == 4
    assert '공식 출처' in decide.seen[3][0][-2]['content']


def test_jarvis_picks_public_bodies_before_blogs(guard, tmp_path):
    state = agent.TaskState('지하철 요금')
    blog = state.add('result', 'b', url='https://blog.naver.com/x')
    city = state.add('result', 'c', url='https://www.seoul.go.kr/fare')
    assert WebAgent._best_result(state, [blog, city]) == city

def test_an_earlier_page_is_kept_as_the_models_notes(guard, tmp_path):
    other = snapshot('https://example.org/', 'Home', '- main:\n  - paragraph: ' + '홈 본문 ' * 300)
    decide = scripted({'tool': 'open', 'number': 1}, {'tool': 'open', 'number': 4, 'notes': '값은 42'},
                      {'tool': 'answer', 'answer': '42'})
    make(guard, tmp_path, decide, {'https://example.org/a': PAGE, 'https://example.org/': other}).run(
        'https://example.org/a 값은?')
    final = [m['content'] for m in decide.seen[2][0] if m['content'].startswith('UNTRUSTED')]
    assert '메모: 값은 42' in final[0] and 'The value is 42.' not in final[0]
    assert '홈 본문' in final[1]                                   # the newest page is still whole


def test_at_most_ten_pages_and_two_link_hops(guard, tmp_path):
    state = agent.TaskState('q')
    for number in range(1, 11):
        state.pages[100 + number] = {'url': f'https://example.org/{number}', 'title': '', 'status': 'ok'}
    state.add('result', 'r', url='https://example.org/next')
    tools, _ = WebAgent(guard, Collector(), browser({}, tmp_path), None, downloads_dir=tmp_path)._options(state, False)
    assert 'open' not in tools
    state = agent.TaskState('q')
    deep = state.add('link', 'l', url='https://example.org/deep', depth=3)
    near = state.add('link', 'l', url='https://example.org/near', depth=2)
    tools, _ = WebAgent(guard, Collector(), browser({}, tmp_path), None, downloads_dir=tmp_path)._options(state, False)
    assert tools['open'] == [near] and deep not in tools['open']


def test_a_page_at_the_depth_limit_offers_no_links(guard, tmp_path):
    decide = scripted({'tool': 'answer', 'answer': 'x'})
    agent_ = make(guard, tmp_path, decide, {'https://example.org/a': PAGE})
    state = agent.TaskState('q')
    content, _ = agent_._page_result(state, parse_snapshot(PAGE), 'browser', depth=agent.MAX_DEPTH)
    assert '링크:' not in content


def test_the_stop_callback_ends_the_task(guard, tmp_path):
    result = make(guard, tmp_path, scripted({'tool': 'search', 'query': 'q'}), should_stop=lambda: True).run('q')
    assert result['status'] == 'stopped' and result['calls'] == 0


def test_run_task_without_an_approved_playwright_server_asks_for_setup(guard):
    with pytest.raises(agent.WebSetupRequired, match='setup_web_agent'):
        agent.run_task(guard, 'q', None)


def test_run_task_reports_a_missing_node(guard, tmp_path):
    from jarvis.features.mcp_client.service import McpSettings
    settings = McpSettings(guard)
    node = tmp_path / 'node.exe'
    settings.add_server('pw', 'PW', str(node), [r'C:\x\@playwright\mcp\cli.js', '--headless', '--isolated',
                                                '--no-webmcp', '--output-dir', str(tmp_path / 'q')])
    config = settings.load()
    settings.save(config.model_copy(update={'servers': (config.servers[0].model_copy(update={'state': 'approved'}),)}))
    with pytest.raises(agent.WebSetupRequired, match='Node.js'):
        agent.run_task(guard, 'q', None)

def test_without_model_notes_jarvis_keeps_the_sentences_matching_the_request():
    text = '사이트 안내입니다. 메뉴 보기. 2026년 최저임금은 시간급 10,320원입니다. 문의는 고객센터로.'
    excerpt = agent.relevant_excerpt(text, '2026년 최저임금 시간급은?')
    assert '10,320원' in excerpt and '메뉴' not in excerpt


def test_an_empty_operator_search_is_retried_in_plain_words(guard, tmp_path):
    plain = agent.simpler_query('site:data.go.kr "csv" 공공데이터 OR 파일')
    assert plain == 'csv 공공데이터 파일'
    decide = scripted({'tool': 'search', 'query': 'site:www.weather.go.kr 제주 날씨'}, {'tool': 'answer', 'answer': 'x'})
    result = make(guard, tmp_path, decide, {agent.bing_url('제주 날씨'): BING}).run('제주 날씨')
    assert result['searches'] == ['site:www.weather.go.kr 제주 날씨', '제주 날씨']
    assert '[1] 기상청 날씨누리' in decide.seen[1][0][-2]['content']