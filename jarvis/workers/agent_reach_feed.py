"""Offline Agent Reach RSS backend; bounded bytes on stdin, never a URL."""
from io import BytesIO
import json
import sys

import feedparser
from agent_reach.channels.rss import RSSChannel


def main():
    payload = sys.stdin.buffer.read(524289)
    if len(payload) > 524288:
        raise ValueError('Feed input too large.')
    status, _ = RSSChannel().check()
    if status != 'ok':
        raise ValueError('Agent Reach RSS backend unavailable.')
    parsed = feedparser.parse(BytesIO(payload))
    if not parsed.version:
        raise ValueError('No recognized RSS/Atom feed.')
    items = [{'title': str(item.get('title', ''))[:1000], 'summary': str(item.get('summary', ''))[:2000],
              'url': str(item.get('link', ''))[:2048], 'published_at': str(item.get('published', ''))[:100]}
             for item in parsed.entries[:10]]
    print(json.dumps({'backend': 'Agent Reach RSS / feedparser', 'items': items}, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'Offline feed backend failed.'}))
        sys.exit(1)
