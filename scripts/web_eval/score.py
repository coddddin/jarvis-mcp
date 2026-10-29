"""Scoring rules for the web-task benchmark. Pure functions: no network, no model, no file opening of downloads.

A task passes when every check below holds:
- the answer is not empty;
- every `answer_all` regex matches the answer (case-insensitive);
- a host in `source_hosts` was cited in the answer or visited by a tool call (JARVIS adds sources from its traces,
  so a visited official page counts even when the model did not list it);
- `min_visited_pages` distinct pages were visited, when set (link-following tasks);
- for `download` tasks: at least one new file with an allowed extension, no larger than `max_bytes`, and every
  such file was scanned clean.
"""
import re
from urllib.parse import urlsplit

URL = re.compile(r'https?://[^\s<>()\[\]"\'`|]+')
# Search result pages do not count as sources or visited pages.
SEARCH_HOSTS = ('bing.com', 'duckduckgo.com', 'google.com', 'search.naver.com')


def host_matches(host, allowed):
    host = (host or '').lower().rstrip('.')
    return any(host == item or host.endswith('.' + item) for item in allowed)


def page_hosts(urls):
    return [urlsplit(url).hostname or '' for url in urls if not host_matches(urlsplit(url).hostname, SEARCH_HOSTS)]


def score(task, answer, visited_urls, files):
    """files: [{'name', 'bytes', 'clean'}] of downloads. Returns {'pass', 'checks': {name: bool}}."""
    answer = answer or ''
    cited = page_hosts(URL.findall(answer))
    visited = page_hosts(visited_urls)
    checks = {'answered': bool(answer.strip()),
              'answer_matches': all(re.search(rule, answer, re.I) for rule in task.get('answer_all', [])),
              'sourced': any(host_matches(host, task['source_hosts']) for host in cited + visited)}
    if task.get('min_visited_pages'):
        pages = {url.split('#', 1)[0] for url in visited_urls if not host_matches(urlsplit(url).hostname, SEARCH_HOSTS)}
        checks['followed_links'] = len(pages) >= task['min_visited_pages']
    rule = task.get('download')
    if rule:
        wanted = [item for item in files if item['name'].lower().endswith(tuple(rule['extensions']))]
        checks['downloaded'] = any(item['bytes'] <= rule['max_bytes'] for item in wanted)
        checks['scanned_clean'] = bool(wanted) and all(item.get('clean') is True for item in wanted)
    return {'pass': all(checks.values()), 'checks': checks}
