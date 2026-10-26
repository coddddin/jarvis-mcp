'use strict';
// Source cards for answers grounded in approved Vault notes (PA1-KNOWLEDGE). Text nodes only, no HTML strings.
// chat.js calls JarvisSources.render(turn) and appends the result; nothing here keeps state between renders.
(() => {
  const make = (tag, className, text) => {
    const value = document.createElement(tag);
    value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  };
  const valid = source => source && typeof source.title === 'string' && Number.isInteger(source.line_start)
    && Number.isInteger(source.line_end);
  const lines = source => source.line_start === source.line_end ? '줄 ' + source.line_start
    : '줄 ' + source.line_start + '-' + source.line_end;
  function entry(source) {
    const item = make('li', 'chat-source');
    item.append(make('strong', '', source.title), make('span', 'muted small', ' · ' + lines(source)));
    if (typeof source.path === 'string') item.title = source.path;
    return item;
  }
  function render(turn) {
    const sources = Array.isArray(turn?.sources) ? turn.sources.filter(valid) : [];
    if (!sources.length) return null;
    const cited = sources.filter(source => source.cited === true);
    const other = sources.filter(source => source.cited !== true);
    const box = make('details', 'chat-tools chat-sources');
    box.open = cited.length > 0;
    box.append(make('summary', '', cited.length ? '참고한 노트 · ' + cited.length + '개'
      : '검색된 노트 · ' + other.length + '개 (답변 인용 확인 안 됨)'));
    if (cited.length) {const list = make('ul', 'chat-list'); list.append(...cited.map(entry)); box.append(list);}
    if (cited.length && other.length) box.append(make('p', 'muted small', '검색됐지만 답변에 인용되지 않은 노트'));
    if (other.length) {const list = make('ul', 'chat-list'); list.append(...other.map(entry)); box.append(list);}
    box.append(make('p', 'muted small', '승인된 Vault 노트를 이 PC에서만 검색했어요. 줄 위치는 답변 당시 파일 기준이에요.'));
    return box;
  }
  window.JarvisSources = {render};
})();
