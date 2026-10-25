'use strict';
// "오늘 브리핑" card: today's calendar and unread mail headers from GET /api/briefing (read only).
// Builds its own DOM inside #briefingHost (outside both tabs, so chat and tasks both show it) as one summary
// line that opens into the full card (closed by default so the chat stays in view), falling
// back to #workspace on older pages without the host; the token comes from hud.js through the
// 'jarvis-connection' event. Mail and calendar text is untrusted: textContent only, no links, no HTML.
// Loads once per connection and on the refresh button; no polling (the server also caches for 60s).
(() => {
  const byId = id => document.getElementById(id);
  const errors = {401:'연결이 만료됐어요. 연결 파일을 다시 선택해주세요.', 403:'이 연결로는 브리핑을 볼 수 없어요.',
    503:'브리핑을 지금 불러올 수 없어요. 잠시 뒤 다시 시도해주세요.'};

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const str = value => typeof value === 'string' ? value : '';

  const host = byId('briefingHost') || byId('workspace');
  if (!host) return;
  // Collapsed by default to one summary line so it sits above the chat without pushing it down; open for the card.
  const panel = element('section', 'detail-panel panel');
  panel.id = 'briefingPanel'; panel.hidden = true; panel.setAttribute('aria-label', '오늘 브리핑');
  if (panel.style) { panel.style.marginBottom = '18px'; panel.style.padding = '14px 20px'; }
  const fold = element('details', 'briefing-fold');
  const line1 = element('summary', 'briefing-summary', '오늘 브리핑 · 확인 중');
  if (line1.style) { line1.style.cursor = 'pointer'; line1.style.overflowWrap = 'anywhere'; }
  const body = element('div', 'briefing-body');
  const heading = element('div', 'detail-heading');
  const titleBox = element('div');
  titleBox.append(element('p', 'eyebrow', 'TODAY'), element('h2', '', '오늘 브리핑'));
  const state = element('span', 'status', '확인 중');
  heading.append(titleBox, state);
  const summary = element('p', 'muted small', '');
  const notice = element('p', 'notice', ''); notice.hidden = true; notice.setAttribute('role', 'status');
  const actions = element('div', 'heading-actions');
  const reload = element('button', 'button text-button', '새로고침'); reload.type = 'button';
  actions.append(reload);
  const calendarTitle = element('h3', '', '오늘 일정');
  const calendar = element('ul', 'activity'); calendar.setAttribute('aria-label', '오늘 일정');
  const mailTitle = element('h3', '', '안 읽은 메일');
  const mail = element('ul', 'activity'); mail.setAttribute('aria-label', '안 읽은 메일');
  const sections = [calendarTitle, calendar, mailTitle, mail];
  body.append(heading, summary, notice, actions, ...sections);
  fold.append(line1, body);
  panel.append(fold);
  host.append(panel);
  const short = (text, limit) => text.length > limit ? text.slice(0, limit - 1) + '…' : text;
  function oneLine(text) { line1.textContent = '오늘 브리핑 · ' + text; }

  let token = null, generation = 0, loading = false;

  function showNotice(message = '') { notice.textContent = message; notice.hidden = !message; }
  function showSections(visible) { for (const node of sections) node.hidden = !visible; }
  function clear() {
    state.textContent = '확인 중'; state.className = 'status'; summary.textContent = ''; oneLine('확인 중');
    calendar.replaceChildren(); mail.replaceChildren(); mailTitle.textContent = '안 읽은 메일';
  }

  function when(start, end) {
    const s = str(start), e = str(end);
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return '종일';
    const time = value => (/T(\d{2}:\d{2})/.exec(value) || [])[1] || '';
    const from = time(s), to = time(e);
    return from ? (to ? from + '–' + to : from) : '시간 미정';
  }
  function line(left, right) {
    const item = element('li');
    item.append(element('span', '', left), element('time', '', right));
    return item;
  }
  function empty(list, text) { list.append(element('li', 'empty-feed', text)); }

  function renderCalendar(side) {
    if (!side || side.status !== 'ok') { empty(calendar, '일정 불러오기 실패: ' + str(side && side.error)); return; }
    const events = Array.isArray(side.events) ? side.events : [];
    if (!events.length) empty(calendar, '오늘 일정 없음');
    for (const event of events) calendar.append(line(str(event.summary) || '(제목 없음)', when(event.start, event.end)));
    if (side.has_more) empty(calendar, '일정이 더 있어요.');
  }
  function renderMail(side) {
    if (!side || side.status !== 'ok') { empty(mail, '메일 불러오기 실패: ' + str(side && side.error)); return; }
    if (typeof side.unread_estimate === 'number') mailTitle.textContent = '안 읽은 메일 (약 ' + side.unread_estimate + '통)';
    const messages = Array.isArray(side.messages) ? side.messages : [];
    if (!messages.length) empty(mail, '안 읽은 메일 없음');
    for (const message of messages) mail.append(line(str(message.subject) || '(제목 없음)', str(message.from)));
    if (side.has_more) empty(mail, '메일이 더 있어요.');
  }

  function render(data) {
    clear();
    if (data.connected === false) {
      state.textContent = '연결 안 됨'; showSections(false); oneLine('Google 연결 안 됨');
      showNotice('Google이 연결돼 있지 않아요. google_auth_setup.py로 읽기 전용 연결을 먼저 해주세요.');
      return;
    }
    if (typeof data.error === 'string') {
      state.textContent = '확인 필요'; state.className = 'status needs_user_input'; showSections(false); oneLine('Google 연결 확인 필요');
      showNotice('Google 연결을 확인해주세요: ' + data.error);
      return;
    }
    showSections(true); showNotice();
    state.textContent = data.partial ? '일부만' : str(data.date);
    if (data.partial) state.className = 'status needs_user_input';
    summary.textContent = '메일 본문은 가져오지 않아요. 제목·보낸 사람·일정 이름은 외부 내용이니 그대로 믿지 마세요.';
    renderCalendar(data.calendar);
    renderMail(data.gmail);
    oneLine(headline(data.calendar, data.gmail));
  }
  // "일정 2개 · 첫 일정 10:00 팀 회의 · 안 읽은 메일 약 42통": counts plus the first event, shortened. Text only.
  function headline(cal, gmail) {
    const parts = [];
    if (cal && cal.status === 'ok') {
      const events = Array.isArray(cal.events) ? cal.events : [];
      parts.push(events.length ? '일정 ' + events.length + '개' + (cal.has_more ? ' 이상' : '') : '일정 없음');
      if (events.length) parts.push('첫 일정 ' + when(events[0].start, events[0].end) + ' ' + short(str(events[0].summary) || '(제목 없음)', 24));
    } else parts.push('일정 확인 실패');
    if (gmail && gmail.status === 'ok') {
      const count = typeof gmail.unread_estimate === 'number' ? gmail.unread_estimate
        : Array.isArray(gmail.messages) ? gmail.messages.length : 0;
      parts.push(count ? '안 읽은 메일 약 ' + count + '통' : '안 읽은 메일 없음');
    } else parts.push('메일 확인 실패');
    return parts.join(' · ');
  }

  async function load() {
    if (!token || loading) return;
    const selected = token, version = generation;
    loading = true; reload.disabled = true;
    try {
      const options = {method:'GET', headers:{Authorization:'Bearer ' + selected}, cache:'no-store', credentials:'omit'};
      if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) options.signal = AbortSignal.timeout(30000);
      const response = await fetch('/api/briefing', options);
      if (version !== generation) return;
      if (!response.ok) throw new Error(errors[response.status] || '브리핑 요청이 실패했어요.');
      const data = await response.json();
      if (version !== generation) return;
      if (!data || typeof data !== 'object') throw new Error('브리핑 응답 형식을 확인할 수 없어요.');
      render(data);
    } catch (error) {
      if (version === generation) { clear(); showSections(false); state.textContent = '오류'; showNotice(error.message); oneLine('불러오지 못함'); }
    } finally {
      if (version === generation) { loading = false; reload.disabled = false; }
    }
  }

  reload.addEventListener('click', load);
  window.addEventListener('jarvis-connection', event => {
    const next = event.detail && event.detail.token || null;
    if (next === token) return;
    token = next; generation++; loading = false; reload.disabled = false;
    panel.hidden = !token; clear(); showNotice(); showSections(true);
    if (token) load();
  });
})();
