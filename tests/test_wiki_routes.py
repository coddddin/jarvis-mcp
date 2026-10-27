import json

import pytest
from starlette.testclient import TestClient

from jarvis.agents.models import TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails

ORIGIN='http://127.0.0.1:8787'
HEADERS={'Origin':ORIGIN,'Authorization':'Bearer '+'x'*43,'Content-Type':'application/json'}
SOURCE={'request_id':'a'*32,'title':'합성 위키','text':'초록별 기본 모델은 로컬 Ollama다.'}


class Model:
    model='fake-local'
    def __init__(self, bad=False):self.calls=0;self.bad=bad
    def _infer(self, messages, schema, deadline):
        self.calls+=1
        assert 'source_text' in messages[-1]['content']
        if self.bad:raise RuntimeError('private-model-detail')
        return {'summary':'초록별은 로컬 추론을 사용한다.','facts':[{'text':'기본 모델은 로컬이다.','quote_id':1}]}


def client(root,model=None):
    tasks=TaskStore(Guardrails(root));task=tasks.create(TaskRequest(summary='wiki test'))
    app=create_event_app(tasks,token='x'*43,task_ids=[task.task_id],origins=[ORIGIN],chat=model or Model(),connect_code='c'*43)
    return TestClient(app,base_url=ORIGIN)


def test_wiki_prepare_review_search_and_durable_duplicate(tmp_path):
    model=Model();c=client(tmp_path,model)
    before=c.post('/api/wiki/search',json={'query':'초록별'},headers=HEADERS)
    assert before.status_code==200 and before.json()['results']==[]
    first=c.post('/api/wiki/prepare',json=SOURCE,headers=HEADERS)
    assert first.status_code==200,first.text
    draft=first.json();assert draft['status']=='proposed'
    assert c.post('/api/wiki/search',json={'query':'초록별'},headers=HEADERS).json()['results']==[]
    for other in [c,client(tmp_path,model)]:
        repeat=other.post('/api/wiki/prepare',json=SOURCE,headers=HEADERS)
        assert repeat.status_code==200 and repeat.json()['id']==draft['id']
    assert model.calls==1
    assert c.post('/api/wiki/approve',json={'candidate_id':draft['id'],'digest':'0'*64},headers=HEADERS).status_code==400
    saved=c.post('/api/wiki/approve',json={'candidate_id':draft['id'],'digest':draft['digest']},headers=HEADERS)
    assert saved.status_code==200 and saved.json()['indexed'] is True,saved.text
    found=c.post('/api/wiki/search',json={'query':'초록별'},headers=HEADERS).json()['results']
    assert found and found[0]['metadata']['review_status']=='approved'
    assert found[0]['source_sha256'] and '로컬 Ollama' in found[0]['excerpt']


@pytest.mark.parametrize('changes,status',[({'Authorization':''},401),({'Origin':'https://evil.example'},403),({'Content-Type':'text/plain'},415)])
def test_wiki_refuses_before_model(tmp_path,changes,status):
    model=Model();c=client(tmp_path,model)
    result=c.post('/api/wiki/prepare',content=json.dumps(SOURCE),headers={**HEADERS,**changes})
    assert result.status_code==status and model.calls==0


def test_unknown_model_failure_is_not_retried_after_reopen(tmp_path):
    model=Model(bad=True);c=client(tmp_path,model)
    result=c.post('/api/wiki/prepare',json=SOURCE,headers=HEADERS)
    assert result.status_code==503 and 'private-model-detail' not in result.text
    result=client(tmp_path,model).post('/api/wiki/prepare',json=SOURCE,headers=HEADERS)
    assert result.status_code==503 and model.calls==1


def test_wiki_assets_share_csp_and_no_auth_bypass(tmp_path):
    c=client(tmp_path)
    for route in ['/wiki','/assets/wiki/wiki.css','/assets/wiki/wiki.js']:
        response=c.get(route)
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert "default-src 'self'" in response.headers['content-security-policy']
    assert c.get('/assets/wiki/../index.html').status_code==404
    assert c.post('/api/wiki/prepare?extra=1',json=SOURCE,headers=HEADERS).status_code==403
    assert c.post('/api/wiki/prepare',content=b'x'*70001,headers=HEADERS).status_code==400


def test_request_reuse_with_changed_source_does_not_call_model(tmp_path):
    model=Model();c=client(tmp_path,model)
    assert c.post('/api/wiki/prepare',json=SOURCE,headers=HEADERS).status_code==200
    assert c.post('/api/wiki/prepare',json={**SOURCE,'text':'다른 자료'},headers=HEADERS).status_code==400
    assert model.calls==1


import threading
import time
from concurrent.futures import ThreadPoolExecutor

def test_confirmed_failure_requires_explicit_new_request(tmp_path):
    model=Model(bad=True)
    c=client(tmp_path,model)
    failed=c.post('/api/wiki/prepare',json=SOURCE,headers=HEADERS)
    assert failed.status_code==503
    assert failed.json()=={'error':'wiki_preparation_failed','retry':'manual_new_request'}
    assert 'private-model-detail' not in failed.text
    model.bad=False
    assert c.post('/api/wiki/prepare',json=SOURCE,headers=HEADERS).status_code==503
    assert model.calls==1
    assert c.post('/api/wiki/prepare',json={**SOURCE,'request_id':'b'*32},headers=HEADERS).status_code==200
    assert model.calls==2

def test_empty_index_is_explicit(tmp_path):
    result=client(tmp_path).post('/api/wiki/search',json={'query':'hello'},headers=HEADERS)
    assert result.json()=={'results':[],'indexed':False,'error':'index_not_ready'}

def test_cross_app_publications_do_not_overlap_and_preserve_both(tmp_path,monkeypatch):
    from jarvis.features.obsidian.service import ObsidianStore
    a,b=client(tmp_path),client(tmp_path)
    drafts=[c.post('/api/wiki/prepare',json={**SOURCE,'request_id':key*32,'title':title},headers=HEADERS).json()
            for c,key,title in [(a,'a','Alpha'),(b,'b','Beta')]]
    original=ObsidianStore.index
    lock=threading.Lock()
    active=0
    maximum=0
    def delayed(self,*args,**kwargs):
        nonlocal active,maximum
        with lock:
            active+=1
            maximum=max(maximum,active)
        try:
            time.sleep(.1)
            return original(self,*args,**kwargs)
        finally:
            with lock: active-=1
    monkeypatch.setattr(ObsidianStore,'index',delayed)
    def approve(pair):
        c,d=pair
        return c.post('/api/wiki/approve',json={'candidate_id':d['id'],'digest':d['digest']},headers=HEADERS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(approve,zip([a,b],drafts)))
    assert all(r.status_code==200 and r.json()['indexed'] for r in responses)
    assert maximum==1
    found=a.post('/api/wiki/search',json={'query':'초록별'},headers=HEADERS).json()['results']
    assert {r['metadata']['title'] for r in found}=={'Alpha','Beta'}
