'use strict';
// W5 dispatcher panel. Builds its own DOM inside #workspace (no index.html markup needed) and reuses
// hud.css classes, since the page CSP allows only same-origin stylesheets. The connection token comes
// from hud.js through the 'jarvis-connection' event, like chat.js.
(() => {
  const byId = id => document.getElementById(id);
  const REFRESH_MS = 10000;
  const runLabels = {running:'실행 중', ok:'완료', error:'오류', failed:'실패', limit:'한도 도달', eligibility:'사용 자격 없음',
    timeout:'시간 초과', stopped:'중지됨', lost:'추적 끊김'};
  const runClass = {running:'running', ok:'completed', limit:'needs_user_input', eligibility:'needs_user_input',
    timeout:'timed_out', error:'needs_user_input', failed:'needs_user_input'};
  const errors = {401:'연결이 만료됐어요. 연결 파일을 다시 선택해주세요.', 403:'이 연결로는 디스패처를 바꿀 수 없어요.',
    503:'디스패처 상태를 지금 읽을 수 없어요. 자비스 실행 상태를 확인해주세요.'};
  const stamp = seconds => new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}).format(new Date(seconds * 1000));
  const count = value => typeof value === 'number' ? new Intl.NumberFormat('ko-KR').format(value) : '—';
  const isTime = value => typeof value === 'number' && Number.isFinite(value);

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const workspace = byId('workspace');
  if (!workspace) return;
  const panel = element('section', 'detail-panel panel');
  panel.id = 'dispatcherPanel'; panel.hidden = true; panel.setAttribute('aria-label', '자동 순환 디스패처');
  if (panel.style) panel.style.marginTop = '24px';  // CSSOM assignment is allowed by the CSP
  const heading = element('div', 'detail-heading');
  const titleBox = element('div');
  titleBox.append(element('p', 'eyebrow', 'AUTO DISPATCH'), element('h2', '', '자동 순환 (디스패처)'));
  const state = element('span', 'status', '확인 중');
  heading.append(titleBox, state);
  const summary = element('p', 'muted small', '');
  const notice = element('p', 'notice', ''); notice.hidden = true; notice.setAttribute('role', 'status');
  const actions = element('div', 'heading-actions');
  const toggle = element('button', 'button secondary', '일시정지'); toggle.type = 'button'; toggle.disabled = true;
  const reload = element('button', 'button text-button', '새로고침'); reload.type = 'button';
  actions.append(toggle, reload);
  const employees = element('ul', 'activity'); employees.setAttribute('aria-label', '직원별 누적');
  const recent = element('ol', 'activity'); recent.setAttribute('aria-label', '최근 실행');
  // W2 process watch: report-only findings about orphaned records and overdue runs.
  const watchTitle = element('h3', '', '확인이 필요한 기록'); watchTitle.hidden = true;
  const watch = element('ul', 'activity'); watch.setAttribute('aria-label', '감시 경고'); watch.hidden = true;
  // W3: last observed 5-hour and weekly usage; display only, the dispatcher ignores stale samples.
  const usageTitle = element('h3', '', '마지막 사용량'); usageTitle.hidden = true;
  const usage = element('ul', 'activity'); usage.setAttribute('aria-label', '마지막 사용량'); usage.hidden = true;
  // W3 alerts: what the dispatcher already told the user today (board message to user:heejun).
  const alertTitle = element('h3', '', '오늘 보낸 알림'); alertTitle.hidden = true;
  const alertList = element('ul', 'activity'); alertList.setAttribute('aria-label', '오늘 알림'); alertList.hidden = true;
  panel.append(heading, summary, notice, actions, watchTitle, watch, usageTitle, usage, alertTitle, alertList, element('h3', '', '직원별 누적'), employees,
    element('h3', '', '최근 실행 (최대 10개)'), recent);
  workspace.append(panel);

  let token = null, generation = 0, loading = false, acting = false, snapshot = null;

  function showNotice(message = '') { notice.textContent = message; notice.hidden = !message; }
  const visible = () => Boolean(token) && !document.hidden && !workspace.hidden;

  function valid(data) {
    return data && typeof data === 'object' && typeof data.effective_enabled === 'boolean'
      && typeof data.paused === 'boolean' && typeof data.config_enabled === 'boolean'
      && data.employees && typeof data.employees === 'object' && Array.isArray(data.recent)
      && data.service && typeof data.service === 'object';
  }

  async function request(method, path) {
    const selected = token, version = generation;
    const options = {method, headers:{Authorization:'Bearer ' + selected}, cache:'no-store', credentials:'omit'};
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) options.signal = AbortSignal.timeout(10000);
    const response = await fetch(path, options);
    if (token !== selected || generation !== version) return null;  // connection changed meanwhile
    if (!response.ok) throw new Error(errors[response.status] || '디스패처 요청이 실패했어요.');
    const data = await response.json();
    if (token !== selected || generation !== version) return null;
    if (!valid(data)) throw new Error('디스패처 응답 형식을 확인할 수 없어요.');
    return data;
  }

  function watchText(item) {
    const detail = item.detail && typeof item.detail === 'object' ? item.detail : {};
    const who = String(item.ref || '') + (detail.employee ? ' (' + String(detail.employee) + ')' : '');
    switch (item.kind) {
      case 'service_down': return '서비스가 꺼져 있어요. 설정은 켜져 있고 일시정지도 아니에요.';
      case 'service_record_stale': return '서비스 기록은 남아 있지만 그 프로세스는 없어요.';
      case 'owner_dead': return who + ': 실행 중으로 남아 있지만 프로세스가 없어요.';
      case 'owner_unknown': return who + ': 프로세스 번호 없이 실행 중으로 남아 있어요. 이 직원은 다시 실행되지 않아요.';
      case 'overdue': return who + ': 제한 시간 ' + count(Math.round((detail.limit_seconds || 0) / 60)) + '분을 넘겼어요.';
      default: return null;
    }
  }

  // Older servers send only running; newer ones add status. A missing record is not a stopped service.
  const SERVICE_TEXT = {stopped:'서비스 멈춤', identity_mismatch:'서비스 신원 미확인 (프로세스 번호 재사용)',
    unknown:'서비스 상태 미확인'};
  function serviceText(data) {
    const status = data.service.status;
    if (status === undefined) return '서비스 멈춤';
    if (status === 'not_registered') return data.effective_enabled ? '설정은 켜졌지만 서비스 미등록' : '서비스 미등록';
    return SERVICE_TEXT[status] || '서비스 상태 미확인';
  }

  function warn(text) {
    const row = element('li');
    row.append(element('span', 'status needs_user_input', '확인 필요'), element('span', '', ' ' + text));
    watch.append(row);
  }

  function renderWatch(report, data) {
    watch.replaceChildren();
    if (data && data.effective_enabled && !data.service.running && data.service.status === 'not_registered')
      warn('자동 배정 설정은 켜져 있지만 서비스가 등록되지 않았어요. 일이 와도 직원을 자동으로 깨우지 못할 수 있어요.');
    const findings = report && Array.isArray(report.findings) ? report.findings : [];
    for (const item of findings.slice(0, 20)) {
      const text = item && typeof item === 'object' ? watchText(item) : null;
      if (!text) continue;
      warn(text);
    }
    if (report && report.error) watch.append(element('li', 'empty-feed', '감시 결과를 읽지 못했어요.'));
    const show = watch.children.length > 0;
    watch.hidden = !show; watchTitle.hidden = !show;
  }

  function renderUsage(view) {
    usage.replaceChildren();
    const names = view && typeof view === 'object' ? Object.keys(view).slice(0, 5) : [];
    for (const name of names) {
      const item = view[name];
      if (!item || typeof item.five_hour !== 'number') continue;
      let text = String(name) + ' · 5시간 ' + Math.round(item.five_hour) + '%';
      if (typeof item.weekly === 'number') text += ' · 주간 ' + Math.round(item.weekly) + '%';
      const row = element('li');
      row.append(element('span', '', text),
        element('span', 'muted small', (isTime(item.sampled_at) ? stamp(item.sampled_at) : '시각 모름')
          + (item.fresh ? '' : ' · 오래됨')));
      usage.append(row);
    }
    if (usage.children.length) usage.append(element('li', 'empty-feed', '마지막으로 관찰한 값입니다. 판단에는 쓰지 않음(오래된 값).'));
    const show = usage.children.length > 0;
    usage.hidden = !show; usageTitle.hidden = !show;
  }

  function renderAlerts(report) {
    alertList.replaceChildren();
    const today = report && Array.isArray(report.today) ? report.today : [];
    for (const item of today.slice(0, 20)) {
      if (!item || typeof item.text !== 'string') continue;
      const row = element('li');
      row.append(element('span', 'status needs_user_input', isTime(item.sent) ? stamp(item.sent) : '알림'),
        element('span', '', ' ' + item.text.slice(0, 300)));
      alertList.append(row);
    }
    if (report && report.error) alertList.append(element('li', 'empty-feed', '알림 기록을 읽지 못했어요.'));
    const show = alertList.children.length > 0;
    alertList.hidden = !show; alertTitle.hidden = !show;
  }

  function render(data) {
    snapshot = data;
    const [label, kind] = data.effective_enabled ? ['켜짐', 'completed']
      : data.paused ? ['일시정지', 'needs_user_input'] : ['설정에서 꺼짐', 'cancelled'];
    state.textContent = label; state.className = 'status ' + kind;
    const service = data.service.running
      ? '서비스 실행 중' + (data.service.pid ? ' (PID ' + data.service.pid + ')' : '')
        + (isTime(data.service.started) ? ' · 시작 ' + stamp(data.service.started) : '')
      : serviceText(data);
    const parts = [service, count(data.interval_minutes) + '분마다', '직원당 하루 ' + count(data.daily_runs) + '회'];
    if (data.paused && isTime(data.paused_at)) parts.push('일시정지 ' + stamp(data.paused_at));
    if (!data.config_enabled) parts.push('설정 파일(config/dispatcher.json)에서 꺼져 있음');
    if (isTime(data.read_at)) parts.push('확인 ' + stamp(data.read_at));
    summary.textContent = parts.join(' · ');
    toggle.textContent = data.paused ? '재개' : '일시정지';
    toggle.disabled = acting;

    renderWatch(data.watch, data);
    renderUsage(data.usage);
    renderAlerts(data.alerts);

    const now = Date.now() / 1000;
    employees.replaceChildren();
    const names = Object.keys(data.employees);
    if (!names.length) employees.append(element('li', 'empty-feed', '아직 실행 기록이 없습니다.'));
    for (const name of names) {
      const item = data.employees[name] || {};
      const row = element('li');
      const waiting = isTime(item.exhausted_until) && item.exhausted_until > now
        ? ' · 한도 대기 ' + stamp(item.exhausted_until) + '까지' : '';
      row.append(element('span', '', name + ' · 실행 ' + count(item.runs) + '회' + waiting),
        element('span', 'muted small', '입력 ' + count(item.total_input_tokens) + ' · 출력 '
          + count(item.total_output_tokens) + ' 토큰'));
      employees.append(row);
    }

    recent.replaceChildren();
    const runs = data.recent.slice(0, 10);
    if (!runs.length) recent.append(element('li', 'empty-feed', '최근 실행이 없습니다.'));
    for (const run of runs) {
      const row = element('li'), left = element('span');
      const details = [];
      if (run.input_tokens != null || run.output_tokens != null)
        details.push('입력 ' + count(run.input_tokens) + ' · 출력 ' + count(run.output_tokens));
      if (run.exit_code != null) details.push('종료 코드 ' + run.exit_code);
      if (isTime(run.ended) && isTime(run.started)) details.push(Math.max(0, Math.round((run.ended - run.started) / 60)) + '분');
      left.append(element('span', 'status ' + (runClass[run.status] || 'cancelled'), runLabels[run.status] || '상태 확인'),
        element('span', '', ' ' + String(run.employee || '?') + (details.length ? ' · ' + details.join(' · ') : '')));
      const time = element('time', '', isTime(run.started) ? stamp(run.started) : '—');
      if (isTime(run.started)) time.dateTime = new Date(run.started * 1000).toISOString();
      row.append(left, time);
      recent.append(row);
    }
  }

  async function load(force = false) {
    if (loading || acting || (!force && !visible()) || !token) return;
    const version = generation;
    loading = true;
    try {
      const data = await request('GET', '/api/dispatcher');
      if (data && version === generation) { render(data); showNotice(); }
    } catch (error) {
      if (version === generation) showNotice(error.message);
    } finally {
      loading = false;
    }
  }

  async function act() {
    if (acting || !token || !snapshot) return;
    const resume = snapshot.paused;
    if (resume && !window.confirm('디스패처를 재개할까요? 받은 일이 있는 직원이 자동으로 실행됩니다.')) return;
    const version = generation;
    acting = true; toggle.disabled = true;
    try {
      const data = await request('POST', resume ? '/api/dispatcher/resume' : '/api/dispatcher/pause');
      if (data && version === generation) { acting = false; render(data); showNotice(resume ? '재개했어요.' : '일시정지했어요. 진행 중인 실행은 끝까지 갑니다.'); }
    } catch (error) {
      if (version === generation) showNotice(error.message);
    } finally {
      acting = false;
      if (version === generation) toggle.disabled = !snapshot;
    }
  }

  toggle.addEventListener('click', act);
  reload.addEventListener('click', () => load(true));
  window.addEventListener('jarvis-connection', event => {
    const next = event.detail && event.detail.token || null;
    if (next === token) { if (token) load(); return; }
    token = next; generation++; snapshot = null; acting = false; loading = false;
    panel.hidden = !token; toggle.disabled = true; showNotice();
    state.textContent = '확인 중'; state.className = 'status'; summary.textContent = '';
    employees.replaceChildren(); recent.replaceChildren();
    if (token) load(true);
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  const tasksTab = byId('tasksTab');
  if (tasksTab) tasksTab.addEventListener('click', () => load());
  setInterval(load, REFRESH_MS);
})();
