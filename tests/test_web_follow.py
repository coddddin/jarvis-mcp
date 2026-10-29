"""Web P0: pages return followable links; chat opens only the request's provenance set, within the page limits."""
import json
from uuid import uuid4

import pytest

from jarvis.app.chat import ChatDecision, ChatError, ChatService
from jarvis.core.guardrails import Guardrails
from jarvis.features.snapshots.service import SnapshotStore
from jarvis.features.web.service import (FOLLOW_DEPTH, LINKS_PER_PAGE, PAGES_PER_REQUEST, REQUEST_BYTES,
                                         WebCollector, page_links)


# --- read_page: links ---

@pytest.fixture
def collector(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    return WebCollector(SnapshotStore(Guardrails(workspace)))


def test_page_links_are_absolute_checked_https_links_without_fragments_or_duplicates(collector, monkeypatch):
    html = ('<title>Docs</title><nav><a href="/guide">Guide</a></nav><p>Body text '
            '<a href="install#step">Install</a> <a href="/guide#again">Guide again</a>'
            '<a href="http://example.com/plain">plain http</a> <a href="https://127.0.0.1/admin">loopback</a>'
            '<a href="https://localhost/x">localhost</a> <a href="https://example.com/?token=abc">secret</a>'
            '<a href="file:///C:/x">file</a> <a href="javascript:alert(1)">js</a> <a href="#top">top</a>'
            '<a href="https://example.com/docs/">self</a> <a href="https://other.example.org/a">Other site</a>'
            '<script>var a = \'<a href="https://evil.example/x">x</a>\';</script></p>')
    monkeypatch.setattr(collector, '_fetch', lambda *args: ('text/html', html))
    result = collector.read_page('https://example.com/docs/')
    assert result['links'] == [
        {'url': 'https://example.com/docs/install', 'text': 'Install'},
        {'url': 'https://example.com/guide', 'text': 'Guide again'},       # the body link wins over the nav duplicate
        {'url': 'https://other.example.org/a', 'text': 'Other site'}]
    assert result['bytes'] == len(html.encode())
    assert result['excerpt'].startswith('Body text Install')               # nav text stays out of the excerpt


def test_main_content_links_come_before_menus_and_files_or_empty_links_are_left_out(collector, monkeypatch):
    html = ('<div class="sidebar" role="navigation">' + ''.join(f'<a href="/v{i}">v{i}</a>' for i in range(30)) + '</div>'
            '<div role="main"><p>Start with the <a href="/tutorial/">Tutorial</a>. '
            '<a href="/archive/docs.zip">zip</a> <a href="/manual.pdf">pdf</a> <a href="/logo"><img src="x"></a></p></div>'
            '<header><a href="/home">Home</a></header><p><a href="/faq">FAQ</a></p>')
    monkeypatch.setattr(collector, '_fetch', lambda *args: ('text/html', html))
    links = collector.read_page('https://example.com/')['links']
    assert [link['text'] for link in links[:2]] == ['Tutorial', 'FAQ']
    assert not any(link['url'].endswith(('.zip', '.pdf', '/logo')) for link in links)
    assert len(links) == LINKS_PER_PAGE


def test_page_links_stop_at_the_cap_and_skip_long_urls():
    anchors = [[f'/p{index}', ['x'], 1] for index in range(LINKS_PER_PAGE + 5)] + [['/' + 'a' * 400, ['long'], 0]]
    links = page_links(anchors, 'https://example.com/')
    assert len(links) == LINKS_PER_PAGE and all(len(link['url']) <= 300 for link in links)


# --- chat: provenance set ---

@pytest.fixture
def service(tmp_path):
    return ChatService(Guardrails(tmp_path))


def page(url, links=(), size=1000):
    return {'status': 'ok', 'url': url, 'title': 'T ' + url, 'excerpt': 'Text of ' + url, 'truncated': False,
            'bytes': size, 'links': [{'url': link, 'text': 'link'} for link in links]}


def read_trace(evidence, url, depth=0, links=(), parent=None, status='ok', size=1000):
    trace = {'name': 'read_public_web_page', 'status': status, 'evidence_id': evidence, 'url': url, 'depth': depth,
             'page': page(url, links, size)}
    return {**trace, 'parent_evidence_id': parent} if parent else trace


def test_openable_is_search_results_typed_urls_and_links_of_opened_pages(service):
    search = {'name': 'search_public_web', 'status': 'ok', 'evidence_id': 'e1', 'sources': [{'url': 'https://a.example/'}]}
    opened = read_trace('e2', 'https://a.example/', links=['https://a.example/next', 'https://a.example/'])
    found = service._openable('read https://typed.example/page. please', [search, opened])
    assert found == {'https://typed.example/page': (None, 0),             # trailing punctuation dropped
                     'https://a.example/next': ('e2', 1)}                 # the opened page itself is gone


def test_links_beyond_the_follow_depth_are_not_openable(service):
    deep = read_trace('e3', 'https://a.example/2', depth=FOLLOW_DEPTH, links=['https://a.example/3'])
    assert service._openable('', [deep]) == {}
    failed = read_trace('e4', 'https://a.example/x', links=['https://a.example/y'], status='error')
    assert service._openable('', [failed]) == {}


def test_execute_refuses_invented_urls_and_reads_past_the_limits(service):
    called = []
    service.tools['read_public_web_page'] = lambda **kwargs: called.append(kwargs) or page(kwargs['url'])
    invented = ChatDecision(reply='', tool='read_public_web_page', argument='https://a.example/made-up')
    with pytest.raises(ChatError, match='exactly match'):
        service._execute(invented, 'follow the docs', [read_trace('e1', 'https://a.example/', links=['https://a.example/real'])])
    full = [read_trace(f'e{index}', f'https://a.example/{index}', links=['https://a.example/more'])
            for index in range(PAGES_PER_REQUEST)]
    more = ChatDecision(reply='', tool='read_public_web_page', argument='https://a.example/more')
    with pytest.raises(ChatError, match='limit'):
        service._execute(more, '', full)
    heavy = [read_trace('e1', 'https://a.example/', links=['https://a.example/more'], size=REQUEST_BYTES)]
    with pytest.raises(ChatError, match='limit'):
        service._execute(more, '', heavy)
    assert not called


# --- chat: the loop ---

def run(service, monkeypatch, decisions, message, pages):
    schemas, prompts = [], []

    def worker(messages, schema, deadline, on_delta=None):
        schemas.append(json.loads(json.dumps(schema)))
        prompts.append([dict(item) for item in messages])
        return next(decisions)
    monkeypatch.setattr(service, '_worker', worker)
    service.tools['search_public_web'] = lambda **kwargs: {'items': [{'title': 'Docs', 'url': 'https://a.example/'}]}
    service.tools['read_public_web_page'] = lambda **kwargs: pages[kwargs['url']]
    turn = service.send(service.store.create()['id'], uuid4().hex, message)['turns'][0]
    return turn, schemas, prompts


def test_search_open_follow_answer_records_the_chain(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='search_public_web', argument='example docs'),
        ChatDecision(reply='', tool='read_public_web_page', argument='https://a.example/'),
        ChatDecision(reply='', tool='read_public_web_page', argument='https://a.example/install', evidence_ids=['e2']),
        ChatDecision(reply='claims', tool='none', argument='', basis='external', evidence_ids=['e3'])])
    pages = {'https://a.example/': page('https://a.example/', ['https://a.example/install']),
             'https://a.example/install': page('https://a.example/install')}
    turn, schemas, prompts = run(service, monkeypatch, decisions, '웹에서 example 문서 검색하고 설치 페이지 원문 읽어줘', pages)
    assert [t['name'] for t in turn['tools']] == ['search_public_web', 'read_public_web_page', 'read_public_web_page']
    second = turn['tools'][2]
    assert second['parent_evidence_id'] == 'e2' and second['depth'] == 1 and second['url'] == 'https://a.example/install'
    assert turn['tools'][1]['parent_evidence_id'] == 'e1' and turn['tools'][1]['depth'] == 0
    # After a page is open, only another page from the provenance set or the answer: no search, no local tools.
    assert schemas[2]['properties']['tool']['enum'] == ['none', 'read_public_web_page']
    assert schemas[2]['properties']['argument']['enum'] == ['', 'https://a.example/install']
    assert schemas[3]['properties']['evidence_ids']['items']['enum'] == ['e2', 'e3']
    assert 'Text of https://a.example/install' in turn['assistant'] and 'claims' not in turn['assistant']
    assert '따라간 경로: https://a.example/ → https://a.example/install' in turn['assistant']
    # The earlier page shrinks in the prompt once a newer one is read.
    last_prompt = [item['content'] for item in prompts[3] if item['content'].startswith('UNTRUSTED TOOL RESULT')]
    assert '"earlier_page": true' in last_prompt[1] and '"links"' in last_prompt[2]
    assert all(set(item) == {'role', 'content'} for item in prompts[3])


def test_a_typed_url_is_read_first_without_a_search(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='read_public_web_page', argument='https://typed.example/a'),
        ChatDecision(reply='x', tool='none', argument='', basis='external', evidence_ids=['e1'])])
    pages = {'https://typed.example/a': page('https://typed.example/a')}
    turn, schemas, _ = run(service, monkeypatch, decisions, 'https://typed.example/a 페이지 읽어줘', pages)
    assert schemas[0]['properties']['tool']['enum'] == ['read_public_web_page']
    assert schemas[0]['properties']['argument']['enum'] == ['https://typed.example/a']
    assert [t['name'] for t in turn['tools']] == ['read_public_web_page'] and 'parent_evidence_id' not in turn['tools'][0]


def test_page_reads_stop_at_the_page_limit_and_then_only_the_answer_is_offered(service, monkeypatch):
    urls = [f'https://a.example/{index}' for index in range(PAGES_PER_REQUEST + 1)]
    chain = {urls[index]: page(urls[index], [urls[index + 1]]) for index in range(PAGES_PER_REQUEST)}
    reads = [ChatDecision(reply='', tool='read_public_web_page', argument=url) for url in urls[:PAGES_PER_REQUEST]]
    final = ChatDecision(reply='x', tool='none', argument='', basis='external', evidence_ids=[f'e{PAGES_PER_REQUEST}'])
    decisions = iter(reads + [final])
    message = ' '.join(urls[:PAGES_PER_REQUEST]) + ' 페이지 읽어줘'          # all typed, so depth never runs out
    turn, schemas, _ = run(service, monkeypatch, decisions, message, chain)
    assert len(turn['tools']) == PAGES_PER_REQUEST and turn['status'] == 'completed'
    assert schemas[-1]['properties']['tool']['enum'] == ['none']
    assert len(schemas) == PAGES_PER_REQUEST + 1
