'use strict';
// Status while the server is not shown: the app navigates here with #restart-<n> or #stopped (src/main.rs).
(() => {
  const MAX = 5;
  const status = document.getElementById('status');
  function show() {
    const restart = /^#restart-([0-9])$/.exec(location.hash);
    const stopped = location.hash === '#stopped';
    document.body.dataset.state = stopped ? 'stopped' : restart ? 'restarting' : 'starting';
    status.textContent = stopped ? "서버를 다시 시작하지 못했습니다. 트레이 메뉴의 '서버 다시 시작'을 눌러 주세요."
      : restart && restart[1] !== '0' ? `서버가 멈춰서 다시 시작하는 중입니다 (${restart[1]}/${MAX})`
      : restart ? '서버를 다시 시작하는 중입니다' : '서버를 시작하는 중...';
  }
  window.addEventListener('hashchange', show);
  show();
})();
