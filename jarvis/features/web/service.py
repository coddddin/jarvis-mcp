"""Registered public sources, extractive briefs and exclusive local report saves."""
from jarvis.core.paths import CONFIG_ROOT, WORKERS_ROOT
import base64
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlencode
from uuid import uuid4
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.core.redaction import redact
from jarvis.core.guardrails import GuardrailError
from jarvis.workers.web_https import MAX_BYTES, FetchError, validate_url


class WebError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class Source(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    kind: Literal['feed', 'html', 'weather']
    url: str = Field(max_length=2048)
    link_hosts: list[str] = Field(min_length=1, max_length=10)


class Registry(StrictModel):
    version: Literal[1]
    allowed_hosts: list[str] = Field(min_length=1, max_length=20)
    sources: dict[str, Source] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def origins(self):
        for host in self.allowed_hosts + [host for source in self.sources.values() for host in source.link_hosts]:
            if not re.fullmatch(r'(?:[a-z0-9][a-z0-9-]*\.)+[a-z]{2,63}', host) or len(host) > 253:
                raise ValueError('Invalid registered public host.')
        for name, source in self.sources.items():
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,59}', name):
                raise ValueError('Invalid source name.')
            validate_url(source.url, self.allowed_hosts)
        return self


class CollectionRequest(StrictModel):
    source_ids: list[str] = Field(min_length=1, max_length=3)
    max_items: int = Field(default=5, ge=1, le=5)
    save_report: bool = True

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.source_ids)) != len(self.source_ids) or any(len(x) > 60 for x in self.source_ids):
            raise ValueError('Use unique registered source IDs.')
        return self


class SearchRequest(StrictModel):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=3, ge=1, le=3)


class PageRequest(StrictModel):
    url: str = Field(min_length=10, max_length=2048)


class Item(StrictModel):
    title: str = Field(max_length=300)
    summary: str = Field(max_length=600)
    url: str = Field(max_length=2048)
    published_at: str = Field(default='', max_length=100)


class SourceResult(StrictModel):
    source_id: str
    label: str
    url: str
    fetched_at: str
    status: Literal['ok', 'error']
    items: list[Item] = Field(default_factory=list, max_length=5)
    error: str = ''
    warnings: list[str] = Field(default_factory=list)


class CollectionReport(StrictModel):
    status: Literal['ok', 'partial', 'error']
    generated_at: str
    untrusted_content: bool = True
    summary_method: str = 'Publisher excerpt / leading text, not model-generated or independently verified.'
    sources: list[SourceResult]
    report_path: str | None = None


class VisibleText(HTMLParser):
    SKIP = {'script', 'style', 'nav', 'footer', 'noscript', 'template', 'svg', 'form'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.in_title = False
        self.parts, self.title = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.hidden.append(tag)
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:self.hidden.index(tag)]
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            if self.in_title:
                self.title.append(data)
            else:
                self.parts.append(data)


def normalize(parts, limit):
    """Join already-parsed text; never feed it back to the HTML parser."""
    return re.sub(r'\s+', ' ', redact(' '.join(parts))).strip()[:limit]


def plain(text, limit):
    parser = VisibleText()
    parser.feed(text)
    return normalize(parser.parts, limit)


def child_text(element, names):
    for child in element:
        if child.tag.rsplit('}', 1)[-1] in names:
            return ''.join(child.itertext())
    return ''


class WebCollector:
    CONFIG = CONFIG_ROOT / 'web_sources.json'
    WORKER = WORKERS_ROOT / 'web_https.py'
    WORKER_TIMEOUT = 12

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.guard = snapshots.guard

    def _registry(self):
        path = self.CONFIG.absolute()
        if path.resolve().is_relative_to(self.guard.workspace):
            raise WebError('Source registry must be outside the data workspace.')
        if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise WebError('Linked source registry is forbidden.')
        with path.open('rb') as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise WebError('Hardlinked source registry is forbidden.')
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise WebError('Source registry exceeds size limit.')
        return Registry.model_validate_json(raw)

    def list_sources(self):
        self.guard.check_running()
        registry = self._registry()
        return {'sources': [{'source_id': name, **source.model_dump()} for name, source in registry.sources.items()],
                'max_sources_per_call': 3, 'max_items_per_source': 5,
                'mode': 'Registered public HTTPS sources only; no general web search or account access.'}

    def search(self, query, limit=3):
        """Fixed Bing RSS search, through the existing pinned HTTPS worker only."""
        request = SearchRequest(query=query, limit=limit)
        self.guard.check_running()
        if (not request.query.strip() or redact(request.query) != request.query
                or any(ord(char) < 32 or ord(char) == 127 for char in request.query)):
            raise WebError('Search requires a public query without credentials or control characters.')
        source = Source(label='Bing search excerpts', kind='feed',
            url='https://www.bing.com/search?' + urlencode({'format': 'rss', 'q': request.query}),
            link_hosts=['www.bing.com'])
        registry = Registry(version=1, allowed_hosts=['www.bing.com'], sources={'search': source})
        content_type, text = self._fetch(source, registry)
        items = self._items(source, content_type, text, request.limit, search_links=True)
        ignored = {
            'the', 'and', 'for', 'with', 'official', 'site', 'website', 'search',
            '공식', '사이트', '검색', '알려줘', '찾아줘'}
        terms = [term for term in re.findall(r'[a-z0-9가-힣]{2,}', request.query.casefold()) if term not in ignored]
        if not terms:
            raise WebError('Use a specific public search topic.')
        items = [item for item in items if terms[0] in
            (item.title + ' ' + item.summary + ' ' + item.url).casefold()]
        if not items:
            raise WebError('Search returned no excerpts matching query terms; reformulate the query.')
        return {'status': 'ok', 'backend': 'Bing RSS', 'query': request.query,
                'retrieved_at': datetime.now(timezone.utc).isoformat(),
                'untrusted_content': True, 'items': [item.model_dump() for item in items],
                'scope': 'Search excerpts only; result pages not fetched, ranking/currentness not independently verified.'}

    def read_page(self, url):
        request = PageRequest(url=url)
        self.guard.check_running()
        host = urlsplit(request.url).hostname or ''
        source = Source(label='Public page excerpt', kind='html', url=request.url, link_hosts=[host])
        registry = Registry(version=1, allowed_hosts=[host], sources={'page': source})
        mime, text = self._fetch(source, registry)
        if mime.split(';', 1)[0].strip() not in {'text/html', 'text/plain'}:
            raise WebError('Only public HTML/plain text pages are supported.')
        if re.search(r'<title[^>]*>\s*(?:Just a moment|Access denied)|verify you are human|cf-chl-|g-recaptcha', text, re.I):
            raise WebError('Page requires verification; no page content was confirmed.')
        parser = VisibleText()
        parser.feed(text)
        excerpt = normalize(parser.parts, 3001)
        if not excerpt.strip():
            raise WebError('No visible page text was returned.')
        return {'status': 'ok', 'url': source.url, 'title': normalize(parser.title, 300) or host,
                'excerpt': excerpt[:3000], 'truncated': len(excerpt) > 3000,
                'retrieved_at': datetime.now(timezone.utc).isoformat(), 'untrusted_content': True,
                'scope': 'Visible page excerpt only; no script execution, whole-document review or independent fact checking.'}

    def feed_input(self, source_id):
        """Reuse registered HTTPS admission for an offline alternative RSS parser."""
        self.guard.check_running()
        if not isinstance(source_id, str) or not 1 <= len(source_id) <= 60:
            raise WebError('Use a registered feed ID.')
        registry = self._registry()
        source = registry.sources.get(source_id)
        if source is None or source.kind != 'feed':
            raise WebError('Select a registered RSS/Atom feed source.')
        content_type, text = self._fetch(source, registry)
        if content_type.split(';', 1)[0].strip() not in {'application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml', 'application/octet-stream'}:
            raise WebError('Expected an RSS/Atom XML response.')
        return source, text

    def _fetch(self, source, registry):
        validate_url(source.url, registry.allowed_hosts)
        script = self.WORKER
        if script.is_symlink() or script.is_junction() or script.stat().st_nlink != 1:
            raise WebError('Invalid trusted HTTPS worker.')
        env = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
        process = subprocess.Popen([sys.executable, '-I', str(script)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        payload = json.dumps({'url': source.url, 'allowed_hosts': registry.allowed_hosts}).encode()
        deadline, sent = time.monotonic() + self.WORKER_TIMEOUT, False
        try:
            while True:
                self.guard.check_running()
                if time.monotonic() >= deadline:
                    raise WebError('HTTPS worker deadline exceeded.')
                try:
                    output, _ = process.communicate(input=None if sent else payload, timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    sent = True
            if process.returncode != 0 or len(output) > 750000:
                raise WebError('HTTPS worker failed or exceeded output limit.')
            data = json.loads(output)
            if data.get('ok') is not True:
                raise WebError(data.get('error', 'Public HTTPS request failed.'))
            raw = base64.b64decode(data['body_base64'], validate=True)
            if len(raw) > MAX_BYTES:
                raise WebError('Response exceeds size limit.')
            return data['content_type'].lower(), raw.decode('utf-8-sig')
        except GuardrailError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            if isinstance(exc, WebError):
                raise
            raise WebError('Malformed or non-UTF-8 public response.') from None
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    @staticmethod
    def _link(link, source):
        url = urljoin(source.url, link)
        try:
            validate_url(url, source.link_hosts)
        except FetchError:
            return ''
        return url

    def _items(self, source, content_type, text, limit, *, search_links=False):
        mime = content_type.split(';', 1)[0].strip()
        if source.kind == 'feed':
            generic_xml = mime == 'application/octet-stream' and text.lstrip().startswith('<?xml')
            if mime not in {'application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml'} and not generic_xml:
                raise WebError('Expected an XML feed, not this content type.')
            if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
                raise WebError('XML declarations/entities are not accepted.')
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                raise WebError('Invalid XML feed.') from None
            if root.tag.rsplit('}', 1)[-1] not in {'rss', 'feed'}:
                raise WebError('Expected an RSS or Atom feed root.')
            if sum(1 for _ in root.iter()) > 10000:
                raise WebError('Feed exceeds node limit.')
            stack = [(root, 0)]
            while stack:
                node, depth = stack.pop()
                if depth > 40:
                    raise WebError('Feed exceeds nesting limit.')
                stack.extend((child, depth + 1) for child in node)
            entries = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] in {'item', 'entry'}]
            items = []
            for node in entries:
                self.guard.check_running()
                link = child_text(node, {'link'})
                if not link:
                    link = next((child.attrib.get('href', '') for child in node
                        if child.tag.rsplit('}', 1)[-1] == 'link' and child.attrib.get('rel', 'alternate') == 'alternate'), '')
                if search_links:
                    # Result URLs are data only: never fetch or follow them. Reject
                    # local/IP destinations and keep the ordinary HTTPS URL checks.
                    host = urlsplit(link).hostname or ''
                    if (not re.fullmatch(r'(?:[a-z0-9][a-z0-9-]*\.)+[a-z]{2,63}', host)
                            or host.rsplit('.', 1)[-1] in {'local', 'localhost', 'internal', 'test', 'invalid', 'arpa'}):
                        continue
                    try:
                        validate_url(link, [host])
                    except FetchError:
                        continue
                    url = link
                else:
                    url = self._link(link, source) if link else ''
                if not url:
                    continue
                items.append(Item(title=plain(child_text(node, {'title'}), 300),
                    summary=plain(child_text(node, {'description', 'summary', 'content'}), 500), url=url,
                    published_at=plain(child_text(node, {'pubDate', 'published', 'updated'}), 100)))
                if len(items) == limit:
                    break
            if not items:
                raise WebError('Feed contains no usable registered article links.')
            return items
        if source.kind == 'html':
            if mime not in {'text/html', 'text/plain'}:
                raise WebError('Expected public HTML or plain text.')
            parser = VisibleText()
            parser.feed(text)
            summary = plain(' '.join(parser.parts), 500)
            if not summary:
                raise WebError('No visible text was extracted.')
            return [Item(title=plain(' '.join(parser.title), 300) or source.label, summary=summary, url=source.url)]
        if mime != 'application/json':
            raise WebError('Expected weather JSON.')
        try:
            data = json.loads(text)
            current = data['current']
            units = data['current_units']
            values = [current['temperature_2m'], current['relative_humidity_2m'], current['weather_code']]
            if (data['timezone'] != 'Asia/Seoul' or units['temperature_2m'] != '°C'
                    or units['relative_humidity_2m'] != '%' or any(type(v) not in (int, float) for v in values)
                    or not -100 <= values[0] <= 100 or not 0 <= values[1] <= 100
                    or type(values[2]) is not int or not 0 <= values[2] <= 999):
                raise ValueError()
            observed = str(current['time'])
            datetime.fromisoformat(observed)
        except (ValueError, KeyError, TypeError):
            raise WebError('Unexpected weather data schema or units.') from None
        return [Item(title=source.label, summary=f'서울 {observed} (Asia/Seoul): 기온 {values[0]} °C, 습도 {values[1]}%, WMO 날씨 코드 {values[2]}. 출처: Open-Meteo.',
                     url=source.url, published_at=observed)]

    def collect(self, source_ids, max_items=5, save_report=True):
        request = CollectionRequest(source_ids=source_ids, max_items=max_items, save_report=save_report)
        registry = self._registry()
        if any(name not in registry.sources for name in request.source_ids):
            raise WebError('Unknown source ID; list registered web sources first.')
        sources = []
        for name in request.source_ids:
            self.guard.check_running()
            source = registry.sources[name]
            stamp = datetime.now(timezone.utc).isoformat()
            try:
                content_type, text = self._fetch(source, registry)
                items = self._items(source, content_type, text, request.max_items)
                sources.append(SourceResult(source_id=name, label=source.label, url=source.url,
                    fetched_at=stamp, status='ok', items=items,
                    warnings=['Generic content type; verified registered RSS/Atom XML structure.']
                        if source.kind == 'feed' and content_type.split(';', 1)[0].strip() == 'application/octet-stream' else []))
            except GuardrailError:
                raise
            except (WebError, FetchError) as exc:
                sources.append(SourceResult(source_id=name, label=source.label, url=source.url,
                    fetched_at=stamp, status='error', error=redact(str(exc))[:300]))
        successes = sum(item.status == 'ok' for item in sources)
        status = 'ok' if successes == len(sources) else 'partial' if successes else 'error'
        report = CollectionReport(status=status, generated_at=datetime.now(timezone.utc).isoformat(), sources=sources)
        self.guard.check_running()
        if request.save_report:
            folder = self.guard.validate_path('reports/web', must_exist=False)
            folder.mkdir(parents=True, exist_ok=True)
            self.guard.validate_path(str(folder))
            target = self.guard.validate_path(str(folder / (uuid4().hex + '.txt')), must_exist=False)
            report.report_path = str(target)
            self.snapshots._write(target, report.model_dump_json(indent=2).encode('utf-8'))
        return report.model_dump()
