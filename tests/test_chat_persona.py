"""JARVIS manner: one shared persona leads every answering system prompt. Synthetic Vault only; no models."""
import json

import pytest

from jarvis.app.chat import PERSONA, ChatDecision, ChatService
from jarvis.core.guardrails import Guardrails


@pytest.fixture
def service(tmp_path):
    folder = tmp_path / 'JARVIS-Vault'
    folder.mkdir()
    meta = {'id': 'meeting', 'title': '회의록', 'created': '2026-10-22T12:00:00+00:00',
            'updated': '2026-10-22T12:01:00+00:00', 'source': 'manual', 'tags': ['테스트'],
            'sensitivity': 'private', 'review_status': 'approved'}
    (folder / 'meeting.md').write_text('---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in meta.items())
                                       + '\n---\n# 회의록\n출시 일정은 11월 첫째 주로 정했다.\n', encoding='utf-8')
    chat = ChatService(Guardrails(tmp_path))
    chat.notes.index()
    return chat


def system_prompts(service, monkeypatch, message):
    calls = []

    def worker(messages, schema, deadline, on_delta=None):
        calls.append(messages)
        if 'reply' not in schema.get('properties', {}):  # request classifier, not an answer
            raise RuntimeError('classifier unavailable in this test')
        return ChatDecision(reply='확인했습니다, 주인님.', tool='none', argument='', basis='general', evidence_ids=[])
    monkeypatch.setattr(service, '_worker', worker)
    service.send(service.store.create()['id'], 'a' * 32, message, 'chat')
    return [m[0]['content'] for m in calls if m and m[0]['role'] == 'system']


def test_persona_sets_the_jarvis_manner():
    for rule in ('합니다체', '주인님', '한 답변에 한 번 이하', '결론부터', '1~3문장', '건조한 위트', '위트를 쓰지 마라', '이모지', '말투 규칙보다 우선'):
        assert rule in PERSONA


@pytest.mark.parametrize('message', ['오늘 기분 어때?', '내 노트에서 출시 일정 찾아줘'])
def test_every_answering_prompt_starts_with_the_persona(service, monkeypatch, message):
    answering = [text for text in system_prompts(service, monkeypatch, message) if 'J.A.R.V.I.S.' in text]
    assert answering and all(text.startswith(PERSONA) for text in answering)
