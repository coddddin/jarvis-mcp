'use strict';
// Model-name button (brain P1): shows the model that answers chat and voice, lists the selectable models and switches
// with POST /api/brain/selection. The server keeps a request in flight on its starting model, keeps the old selection
// when a switch fails, and answers 409 before the first switch to an external provider; this file only shows what
// the server says. Text nodes only, no HTML strings. Used by the HUD chat (chat.js) and the assistant view.
(() => {
  const make = (tag, className, text) => {
    const value = document.createElement(tag);
    value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  };
  const where = selection => selection?.external ? '외부' : '로컬';
  const label = selection => typeof selection?.label === 'string' && selection.label
    ? selection.label + ' · ' + where(selection) : '모델 확인 필요';

  // One line under an answer: which model answered and, when the local model took over, why.
  function answeredBy(turn) {
    const model = turn?.model;
    if (!model || typeof model.label !== 'string' || !model.label) return '';
    const reason = typeof model.local_reason === 'string' && model.local_reason ? ' · ' + model.local_reason : '';
    return '답한 모델: ' + model.label + (model.external ? ' (외부)' : ' (로컬)') + reason;
  }

  function create({button, panel, token, onChange = () => {}}) {
    let state = null, open = false, busy = false, version = 0;

    async function request(path, body) {
      const selected = token();
      if (!selected) throw new Error('연결이 필요해요.');
      const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
        headers: {Authorization: 'Bearer ' + selected, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit'});
      let data = {};
      try {data = await response.json();} catch (_) { /* a non-JSON error page keeps the generic text */ }
      return {status: response.status, data};
    }

    function show(...children) {panel.replaceChildren(...children);}
    function notice(text) {return make('p', 'model-notice', text);}

    function setOpen(value) {
      open = value;
      panel.hidden = !value;
      button.setAttribute('aria-expanded', String(value));
    }

    function renderList(message) {
      const items = [];
      if (message) items.push(notice(message));
      items.push(make('p', 'model-scope', '고른 모델은 채팅과 음성 답변에만 쓰여요. 요청 분류·작업 판단·위키 검토는 로컬 기본 모델이 해요.'));
      for (const provider of state?.providers || []) {
        const group = make('div', 'model-group');
        group.append(make('p', 'model-provider', provider.label + (provider.external ? ' · 외부로 보냄' : ' · 이 PC')));
        if (!provider.models?.length) group.append(make('p', 'model-reason', '선택할 수 있는 모델이 없어요.'));
        for (const model of provider.models || []) {
          const current = state.selection?.provider_id === provider.provider_id && state.selection?.model_id === model.model_id;
          const choice = make('button', 'model-choice' + (current ? ' selected' : ''), model.label);
          choice.type = 'button';
          choice.disabled = busy || current || !model.available;
          choice.setAttribute('aria-pressed', String(current));
          choice.addEventListener('click', () => choose(provider.provider_id, model.model_id, false));
          group.append(choice);
          if (!model.available && model.reason) group.append(make('p', 'model-reason', model.reason));
        }
        items.push(group);
      }
      show(...items);
    }

    function renderConfirm(body, payload) {
      const box = make('div', 'model-confirm');
      box.setAttribute('role', 'alertdialog');
      box.append(make('p', 'model-confirm-title', '처음 쓰는 외부 모델: ' + (payload.provider || '외부 모델')),
        make('p', '', '이 모델은 이 PC 밖에서 답을 만들어요. 보내는 것과 보내지 않는 것을 확인해 주세요.'));
      const sends = make('ul', 'model-list'), never = make('ul', 'model-list');
      for (const item of payload.sends || []) sends.append(make('li', '', item));
      for (const item of payload.never_sends || []) never.append(make('li', '', item));
      box.append(make('p', 'model-list-title', '보내는 것'), sends, make('p', 'model-list-title', '보내지 않는 것'), never,
        make('p', 'model-reason', '보내면 안 되는 내용이 필요한 질문은 로컬 모델이 답하고 이유를 보여 줘요. 외부 모델이 실패해도 다른 모델로 자동 전환하지 않아요.'));
      const yes = make('button', 'model-confirm-yes', '확인하고 사용'), no = make('button', 'model-confirm-no', '취소');
      yes.type = no.type = 'button';
      yes.addEventListener('click', () => choose(body.provider_id, body.model_id, true));
      no.addEventListener('click', () => renderList());
      const actions = make('div', 'model-confirm-actions');   // stays at the panel's bottom edge while the list scrolls
      actions.append(yes, no);
      box.append(actions);
      show(box);
    }

    async function choose(providerId, modelId, confirmExternal) {
      if (busy) return;
      busy = true;
      const mine = version;
      const body = {provider_id: providerId, model_id: modelId, ...(confirmExternal ? {confirm_external: true} : {})};
      try {
        const {status, data} = await request('/api/brain/selection', body);
        if (mine !== version) return;
        busy = false;
        if (status === 409 && data.error === 'confirm_required') return renderConfirm(body, data);
        if (status !== 200) return renderList('바꾸지 못했어요. 지금 모델을 그대로 써요. ' + (data.reason || ''));
        state = {...state, selection: data.selection};
        button.textContent = label(data.selection);
        onChange(data.selection);
        renderList('다음 질문부터 답하는 모델: ' + data.selection.label);
      } catch (error) {
        if (mine !== version) return;
        busy = false;
        renderList('바꾸지 못했어요. 지금 모델을 그대로 써요. ' + error.message);
      }
    }

    async function refresh() {
      const mine = ++version;
      busy = false;
      try {
        const {status, data} = await request('/api/brain');
        if (mine !== version) return;
        if (status !== 200) {button.textContent = '모델 확인 필요'; return;}
        state = data;
        button.textContent = label(data.selection);
        onChange(data.selection);
        if (open) renderList();
      } catch (_) {
        if (mine === version) button.textContent = '모델 확인 필요';
      }
    }

    function reset() {version++; state = null; busy = false; setOpen(false); button.textContent = '모델 확인 필요';}

    button.addEventListener('click', async () => {
      if (open) return setOpen(false);
      setOpen(true);
      show(notice('모델 목록을 불러오고 있어요.'));
      await refresh();
      if (open) renderList();
    });
    // Escape or a click outside the button and the panel closes it, like a menu.
    document.addEventListener('keydown', event => {
      if (open && event.key === 'Escape') {setOpen(false); button.focus();}
    });
    document.addEventListener('pointerdown', event => {
      if (open && !button.contains(event.target) && !panel.contains(event.target)) setOpen(false);
    });
    setOpen(false);
    return {refresh, reset, get state() {return state;}};
  }

  window.JarvisBrain = {create, answeredBy, label};
})();
