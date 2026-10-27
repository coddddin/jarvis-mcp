'use strict';
(() => {
  const expand = document.getElementById('knowledgeExpand');
  const space = document.getElementById('knowledgeSpace');
  const close = document.getElementById('knowledgeClose');
  if (!expand || !space || !close) return;
  let previousFocus = null;
  expand.addEventListener('click', () => {
    if (space.open || typeof space.showModal !== 'function') return;
    previousFocus = document.activeElement;
    space.showModal();
    close.focus();
  });
  close.addEventListener('click', () => { if (space.open) space.close(); });
  // Escape uses the native dialog cancel behavior, including focus containment.
  space.addEventListener('close', () => {
    const target = previousFocus?.isConnected ? previousFocus : expand;
    previousFocus = null;
    target.focus();
  });
})();
