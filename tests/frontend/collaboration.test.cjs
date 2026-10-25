const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('frontend/collaboration.js','utf8');
const line=source.split('\n').find(item=>item.trim().startsWith('const preview='));
const preview=vm.runInNewContext(line.trim().replace(/^const preview=/,'(').replace(/;$/,'')+')',{String});

test('collapsed entries show one short line, not the whole message',()=>{
  const long='[second] PR #26 리뷰 OK, P1/P2 없음. 세부 내용:\n- 하나\n- 둘\n'+'긴 설명 '.repeat(200);
  const shown=preview(long);
  assert.equal(shown,'PR #26 리뷰 OK, P1/P2 없음.');
  assert.ok(!shown.includes('\n'));
});

test('long first lines are cut to 90 characters with an ellipsis',()=>{
  const shown=preview('가'.repeat(300));
  assert.equal(shown.length,90);assert.ok(shown.endsWith('…'));
});

test('short first sentences keep the rest of the first line; blank lines and spacing are squeezed',()=>{
  assert.equal(preview('\n\n  OK.   이어서   설명합니다\n둘째 줄'),'OK. 이어서 설명합니다');
  assert.equal(preview('작업 기록\t\t탭 포함'),'작업 기록 탭 포함');
});

test('empty, missing and tag-only text stay safe',()=>{
  assert.equal(preview(''),'');assert.equal(preview(undefined),'');assert.equal(preview('[main]'),'');
  assert.equal(preview('[이 태그는 스물다섯 글자를 넘기는 아주 긴 대괄호 문장입니다] 본문'),'[이 태그는 스물다섯 글자를 넘기는 아주 긴 대괄호 문장입니다] 본문');
});

test('the full text stays available when an entry is opened, as text only',()=>{
  assert.match(source,/node\('p','collaboration-body',item\.text\|\|''\)/);
  assert.match(source,/node\('span','collaboration-preview',preview\(item\.text\)\)/);
  assert.doesNotMatch(source,/innerHTML/);
  const css=fs.readFileSync('frontend/collaboration.css','utf8');
  assert.match(css,/-webkit-line-clamp:2/);assert.match(css,/\.collaboration-body\{max-height:16em;overflow-y:auto\}/);
});
