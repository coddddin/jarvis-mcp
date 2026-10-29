'use strict';
// Model-name button (brain P1): shows the model that answers chat and voice, lists the selectable models and switches
// with POST /api/brain/selection. The server keeps a request in flight on its starting model, keeps the old selection
// when a switch fails, and answers 409 before the first switch to an external provider; this file only shows what
// the server says. Text nodes only, no HTML strings. Used by the HUD chat (chat.js) and the assistant view.
// API providers (brain P3) carry needs_key/connected/custom_model: a key typed here goes once to POST /api/brain/key,
// the field is cleared at once, and the key is never kept in this page or shown again.
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
        if (provider.needs_key && !provider.connected) {
          group.append(keyForm(provider));
          items.push(group);
          continue;
        }
        if (provider.reason) group.append(make('p', 'model-reason', provider.reason));
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
        if (provider.custom_model) group.append(customModel(provider));
        if (provider.needs_key) {
          const remove = make('button', 'model-key-delete', '키 삭제');
          remove.type = 'button';
          remove.disabled = busy;
          remove.addEventListener('click', () => keyAction('/api/brain/key/delete', {provider_id: provider.provider_id},
            '키를 지웠어요.'));
          group.append(remove);
        }
        items.push(group);
      }
      show(...items);
    }

    // The key field: password type, no autofill, cleared before the request leaves.
    function keyForm(provider) {
      const form = make('form', 'model-key');
      const input = make('input', 'model-input');
      input.type = 'password';
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.placeholder = provider.label + ' API 키';
      input.setAttribute('aria-label', provider.label + ' API 키');
      const save = make('button', 'model-key-save', '키 연결');
      save.type = 'submit';
      save.disabled = busy;
      form.append(make('p', 'model-reason', '키는 이 PC의 Windows 자격 증명 관리자에만 저장되고 다시 보여 주지 않아요.'),
        input, save);
      form.addEventListener('submit', event => {
        event?.preventDefault?.();
        const key = String(input.value || '').trim();
        input.value = '';
        if (!key) return renderList('키를 입력해 주세요.');
        keyAction('/api/brain/key', {provider_id: provider.provider_id, key}, '키를 연결했어요. 모델을 골라 주세요.');
      });
      return form;
    }

    // Any model id the company offers, also ones not in the fetched list; the server checks the format and the model.
    function customModel(provider) {
      const form = make('form', 'model-custom');
      const input = make('input', 'model-input');
      input.type = 'text';
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.maxLength = 120;
      input.placeholder = '모델 ID 직접 입력';
      input.setAttribute('aria-label', provider.label + ' 모델 ID');
      const use = make('button', 'model-custom-use', '이 모델 쓰기');
      use.type = 'submit';
      use.disabled = busy;
      form.append(input, use);
      form.addEventListener('submit', event => {
        event?.preventDefault?.();
        const modelId = String(input.value || '').trim();
        if (modelId) choose(provider.provider_id, modelId, false);
      });
      return form;
    }

    async function keyAction(path, body, done) {
      if (busy) return;
      busy = true;
      const mine = version;
      show(notice(body.key ? '키를 확인하고 있어요.' : '키를 지우고 있어요.'));
      let message;
      try {
        const {status, data} = await request(path, body);
        message = status === 200 ? done : '처리하지 못했어요. ' + (data.reason || '');
      } catch (error) {
        message = '처리하지 못했어요. ' + error.message;
      }
      body.key = undefined;
      if (mine !== version) return;
      busy = false;
      await refresh();
      if (mine + 1 === version) renderList(message);
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
      if (confirmExternal) show(notice('모델을 확인하고 있어요. 처음 한 번은 아주 짧은 확인 요청을 보내요.'));
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
