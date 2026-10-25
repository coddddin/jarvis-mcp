'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const statusLabels = {planning: '계획 중', working: '작업 중', reviewing: '검토 중',
    revising: '보완 중', summarizing: '답변 정리 중', completed: '완료',
    needs_user_input: '사용자 확인 필요', failed: '실패', queued: '대기', running: '진행 중',
    pending: '응답 준비 중', interrupted: '중단'};
  const statusText = value => statusLabels[value] || '상태 확인 필요';
  let token = null, current = null, pending = false, generation = 0, selection = 0, mode = 'chat';
  let conversations = [];
  const POLL_MS = 400;  // while a reply is pending only: it carries the streamed draft
  const node = (tag, className, text) => {
    const value = document.createElement(tag);
    value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  };
  function say(text = '') {byId('chatNotice').textContent = text; byId('chatNotice').hidden = !text;}
  function view(next) {
    mode = next;
    byId('chatPanel').hidden = !token || mode !== 'chat';
    byId('workspace').hidden = Boolean(token) && mode === 'chat';
    for (const [id, selected] of [['chatTab', mode === 'chat'], ['tasksTab', mode === 'tasks']]) {
      byId(id).classList.toggle('active', selected); byId(id).setAttribute('aria-pressed', String(selected));
    }
  }
  function busy(value) {
    pending = value;
    byId('chatSend').disabled = value || !current;
    byId('newChat').disabled = value;
    byId('chatInput').disabled = value;
    byId('chatSend').textContent = value ? '응답 중…' : '보내기';
    byId('chatMessages').setAttribute('aria-busy', String(value));
    for (const button of byId('chatConversations').querySelectorAll('button')) button.disabled = value;
  }
  async function api(path, body) {
    const selectedToken = token;
    const version = generation;
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: 'Bearer ' + selectedToken, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit'});
    if (token !== selectedToken || generation !== version) throw new Error('연결이 변경되었습니다.');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '자비스 연결을 확인해주세요.');
    return data;
  }
  function renderList() {
    const list = byId('chatConversations'); list.replaceChildren();
    for (const conversation of conversations) {
      const button = node('button', 'conversation-button' + (current?.id === conversation.id ? ' selected' : ''), conversation.title);
      button.type = 'button'; button.disabled = pending;
      button.setAttribute('aria-pressed', String(current?.id === conversation.id));
      button.addEventListener('click', async () => {
        if (pending) return;
        const version = generation, chosen = ++selection;
        try {const result = await api('/api/chat/conversations/' + conversation.id); if (generation !== version || selection !== chosen) return; current = result; render(); say();}
        catch (error) {if (generation === version) say(error.message);}
      });
      list.append(button);
    }
  }
  // Markdown subset for replies, built only from createElement and text nodes (no HTML strings): fenced code, inline code,
  // **bold**, # headings, - / 1. lists and [label](http(s) URL) links. Anything else stays literal text.
  const SAFE_URL = /^https?:\/\/[^\s<>"'`]+$/i;
  const INLINE = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(([^()\s]+)\)/g;
  function inline(text) {
    const parts = [];
    let last = 0;
    for (const match of text.matchAll(INLINE)) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      if (match[1] !== undefined) parts.push(node('code', 'chat-inline-code', match[1]));
      else if (match[2] !== undefined) parts.push(node('strong', '', match[2]));
      else if (SAFE_URL.test(match[4])) {
        const link = node('a', 'chat-link', match[3]);
        link.href = match[4]; link.title = match[4]; link.target = '_blank'; link.rel = 'noopener noreferrer';
        parts.push(link);
      } else parts.push(match[0]);
      last = match.index + match[0].length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }
  function markdown(text) {
    const body = node('div', 'chat-body chat-markdown');
    const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
    let paragraph = [], list = null;
    const endParagraph = () => {
      if (paragraph.length) {const p = node('p', ''); p.append(...inline(paragraph.join('\n'))); body.append(p);}
      paragraph = [];
    };
    const endBlock = () => {endParagraph(); list = null;};
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = /^\s*```/.test(line), heading = /^(#{1,3})\s+(.+)$/.exec(line);
      const item = /^\s*(?:([-*])|(\d{1,9})[.)])\s+(.*)$/.exec(line);
      if (fence) {
        endBlock();
        const code = [];
        while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) code.push(lines[i]);
        const pre = node('pre', 'chat-code');
        pre.append(node('code', '', code.join('\n')));
        body.append(pre);
      } else if (heading) {
        endBlock();
        const title = node('h4', 'chat-md-heading'); title.append(...inline(heading[2])); body.append(title);
      } else if (item) {
        endParagraph();
        const tag = item[1] ? 'ul' : 'ol';
        if (!list || list.tag !== tag) {
          list = {tag, element: node(tag, 'chat-list')};
          if (tag === 'ol' && Number(item[2]) !== 1) list.element.start = Number(item[2]);
          body.append(list.element);
        }
        const entry = node('li', ''); entry.append(...inline(item[3])); list.element.append(entry);
      } else if (!line.trim()) endBlock();
      else {list = null; paragraph.push(line);}
    }
    endBlock();
    return body;
  }
  function bubble(role, text) {
    const article = node('article', 'chat-message ' + role);
    article.append(node('span', 'chat-author', role === 'user' ? '나' : 'J.A.R.V.I.S.'),
      role === 'user' ? node('p', 'chat-body', text) : markdown(text));
    return article;
  }
  // A streamed draft is unverified: grounding may still replace it, so it stays plain, dimmed text.
  function draftBubble(text) {
    const article = node('article', 'chat-message assistant chat-draft');
    article.setAttribute('aria-label', '작성 중인 답변, 아직 확인 전');
    article.append(node('span', 'chat-author', 'J.A.R.V.I.S. · 작성 중 · 확인 전'), node('p', 'chat-body', text));
    return article;
  }
  function render() {
    renderList();
    const list = byId('chatMessages'); list.replaceChildren();
    if (!current?.turns.length) {
      const welcome = node('div', 'chat-greeting');
      welcome.append(node('span', 'brand-mark', 'J'), node('h3', '', '무엇을 도와드릴까요?'), node('p', 'muted', '이야기를 나누거나, PC 상태와 로컬 자료를 물어보세요.'));
      list.append(welcome);
    }
    for (const turn of current?.turns || []) {
      list.append(bubble('user', turn.user));
      if (turn.dispatch?.status) {
        const dispatch = node('details', 'chat-tools');
        dispatch.open = turn.status === 'pending' || turn.dispatch.status === 'needs_user_input';
        dispatch.append(node('summary', '', '무료 자동 배정: ' + statusText(turn.dispatch.status) + ' · 응답 전달: ' + statusText(turn.status)));
        const selectionLabels = {no_eligible_provider: '접근이 확인된 무료 실행 후보가 없어.',
          request_budget_exhausted: '이번 요청의 호출·토큰 예약 한도에 도달했어.',
          reconciliation_required: '이전 호출의 완료 여부를 먼저 확인해야 해.',
          deadline_exceeded: '요청 시간이 초과됐어.', selected: '조건에 맞는 무료 후보를 선택했어.'};
        if (turn.dispatch.selection?.status) dispatch.append(node('p', '', selectionLabels[turn.dispatch.selection.status] || '배정 기록을 확인해줘.'));
        const reasons = {unavailable:'실행 준비 안 됨', access_not_verified:'접근 확인 안 됨',
          verification_expired:'접근 확인 만료', authentication_required:'인증 필요',
          cost_not_allowed:'무료 조건 불일치', quota_exhausted:'한도 소진', capability_mismatch:'작업 종류 불일치', input_budget_exceeded:'입력 한도 초과'};
        for (const rejected of turn.dispatch.selection?.rejected || []) dispatch.append(node('p', '', rejected.id + ' · ' + (reasons[rejected.reason] || '조건 확인 필요')));
        for (const attempt of turn.dispatch.budget?.attempts || []) dispatch.append(node('p', '',
          attempt.provider_id + ' · ' + ({reserved:'호출 예약됨',completed:'응답 수신',failed:'실패 확인',unknown:'완료 여부 알 수 없음'}[attempt.status] || '확인 필요') +
          ' · 예약 ' + attempt.reserved_tokens));
        if (!turn.assistant && turn.dispatch.reply) dispatch.append(node('pre', '', turn.dispatch.reply));
        dispatch.append(node('p', '', '유료 모델·로컬 모델로 자동 전환하지 않아.'));
        list.append(dispatch);
      }
      if (turn.team?.status) {
        const team = node('details', 'chat-tools');
        team.open = turn.status === 'pending';
        team.append(node('summary', '', '협업 결과: ' + statusText(turn.team.status) + ' · 응답 전달: ' + statusText(turn.status)));
        if (turn.team.plan) team.append(node('p', '', turn.team.plan));
        for (const assignment of turn.team.assignments || []) {
          const row = node('div', 'team-assignment');
          row.append(node('p', '', assignment.owner + ' · ' + statusText(assignment.status)),
            node('p', '', assignment.objective));
          if (assignment.summary) row.append(node('p', '', assignment.summary));
          if (assignment.output) {
            const output = node('details', '');
            output.append(node('summary', '', '작업 결과 보기'), node('pre', '', assignment.output));
            row.append(output);
          }
          team.append(row);
        }
        if (turn.team.review) team.append(node('p', '', '모델 검토: ' +
          (turn.team.review.passed ? '통과' : '확인 필요') + ' · 실제 실행 검증을 뜻하지 않아요.'));
        if (Number.isInteger(turn.team.corrections)) team.append(node('p', '', '수정 횟수: ' + turn.team.corrections));
        const stopLabels = {correction_limit: '수정 횟수 상한', external_call_limit: '외부 호출 상한',
          stage_call_limit: '단계 호출 상한', lead_requested_input: '리드가 사용자 확인 요청',
          review_context_limit: '검토 문맥 상한으로 전체 확인 불가', synthesis_failed: '최종 답변 정리 실패'};
        if (turn.team.stop_reason) team.append(node('p', '', '종료 사유: ' + (stopLabels[turn.team.stop_reason] || '기록 확인 필요')));
        list.append(team);
      }
      if (turn.assistant) list.append(bubble('assistant', turn.assistant));
      else if (turn.status === 'pending' && turn.draft) list.append(draftBubble(turn.draft));
      if (turn.tools?.length) {
        const details = node('details', 'chat-tools');
        details.append(node('summary', '', '확인한 도구 · ' + turn.tools.length + '개'));
        for (const tool of turn.tools) details.append(node('pre', '', tool.name + ' · ' + (tool.status === 'ok' ? '완료' : '실패') + '\n' + (tool.evidence_id ? tool.evidence_id + ' · 조회 시각 ' + tool.retrieved_at + '\n' : '') + tool.detail));
        list.append(details);
      }
      if (turn.status !== 'completed') list.append(node('p', 'chat-turn-status', turn.status === 'pending' ? '답변을 준비하고 있어요.' : '이 메시지의 답변은 완료되지 않았습니다. 자동으로 재실행하지 않습니다.'));
    }
    list.scrollTop = list.scrollHeight;
    view(mode);
  }
  async function refreshList() {
    const data = await api('/api/chat/conversations');
    conversations = data.conversations;
    byId('chatModel').textContent = data.model + ' · 로컬';
  }
  async function newConversation() {
    const result = await api('/api/chat/conversations', {});
    current = result; await refreshList(); render(); busy(false); say(); byId('chatInput').focus();
  }
  window.addEventListener('jarvis-connection', async event => {
    const next = event.detail.token;
    if (next === token) {if (token) view(mode); return;}
    token = next; current = null; conversations = []; const version = ++generation;
    byId('workspaceTabs').hidden = !token; byId('chatPanel').hidden = !token;
    busy(false); say();
    if (!token) {byId('chatMessages').replaceChildren(); byId('chatConversations').replaceChildren(); byId('chatInput').value = ''; return;}
    mode = 'chat'; view(mode); busy(true);
    try {
      await refreshList(); if (version !== generation) return;
      current = conversations.length ? await api('/api/chat/conversations/' + conversations[0].id) : await api('/api/chat/conversations', {});
      if (version !== generation) return;
      await refreshList(); render(); busy(false);
    } catch (error) {if (version === generation) {say(error.message); busy(false);}}
  });
  byId('chatTab').addEventListener('click', () => view('chat'));
  byId('tasksTab').addEventListener('click', () => view('tasks'));
  byId('newChat').addEventListener('click', async () => {
    const version = generation; busy(true);
    try {await newConversation();} catch (error) {if (version === generation) {say(error.message); busy(false);}}
  });
  byId('chatInput').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); if (!pending) byId('chatForm').requestSubmit();}
  });
  byId('chatForm').addEventListener('submit', async event => {
    event.preventDefault(); const message = byId('chatInput').value.trim();
    if (!message || pending || !token || !current) return;
    const identity = current.id, version = generation;
    const requestMode = 'auto';
    let polling = true, pollTimer;
    async function poll() {
      if (!polling || generation !== version) return;
      try {
        const snapshot = await api('/api/chat/conversations/' + identity);
        if (polling && generation === version && current?.id === identity) {current = snapshot; render();}
      } catch (error) {if (polling && generation === version) say('진행 조회 실패: ' + error.message);}
      if (polling && generation === version) pollTimer = setTimeout(poll, POLL_MS);
    }
    if (requestMode !== 'chat') pollTimer = setTimeout(poll, POLL_MS);
    busy(true); say('자비스가 답변을 준비하고 있어요.');
    byId('chatMessages').append(bubble('user', message));
    byId('chatMessages').scrollTop = byId('chatMessages').scrollHeight;
    try {
      const result = await api('/api/chat/conversations/' + identity + '/messages', {request_id: crypto.randomUUID().replaceAll('-', ''), message, mode: requestMode});
      polling = false; clearTimeout(pollTimer);
      if (version !== generation) return;
      current = result; byId('chatInput').value = ''; await refreshList(); render();
      const last = current.turns.at(-1);
      say(last?.dispatch?.status === 'needs_user_input' ? '자동 배정이 중단됐어. 실행 후보와 호출 기록을 확인해줘.' :
        last?.team?.status === 'needs_user_input' ? '응답은 전달됐지만 협업 결과는 사용자 확인이 필요해요.' :
        last?.team?.status === 'failed' ? '협업에 실패했어요. 남아 있는 결과와 기록을 확인해줘.' :
        last?.status === 'completed' ? '' : '응답이 완료되지 않았습니다. 기록을 확인해주세요.');
    } catch (error) {
      polling = false; clearTimeout(pollTimer);
      if (version !== generation) return;
      say(error.message);
      try {current = await api('/api/chat/conversations/' + identity); if (version === generation) render();} catch (_) { /* Keep the visible failure and unsent input. */ }
    } finally {polling = false; clearTimeout(pollTimer); if (version === generation) {busy(false); byId('chatInput').focus();}}
  });
  for (const button of document.querySelectorAll('[data-chat-prompt]')) button.addEventListener('click', () => {
    if (!pending) {byId('chatInput').value = button.dataset.chatPrompt; byId('chatInput').focus();}
  });
  window.addEventListener('pagehide', () => {token = null; current = null; generation++;});
})();
