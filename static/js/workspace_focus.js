/* Navigation only: existing analysis and data-quality rules remain authoritative. */
(function () {
  'use strict';
  if (document.body.dataset.page === 'single') {
    document.querySelectorAll('main details > summary').forEach(summary => {
      const label = document.createElement('span');
      label.className = 'disclosure-label';
      label.append(...summary.childNodes);
      const arrow = document.createElement('span');
      arrow.className = 'disclosure-chevron';
      arrow.setAttribute('aria-hidden', 'true');
      summary.append(label, arrow);
      summary.classList.add('aligned-disclosure');
    });
  }
  window.VorbyRiskUI?.bind(document.getElementById('risk-form'),document);
  const tabs = [...document.querySelectorAll('[data-focus-tab]')];
  const panes = [...document.querySelectorAll('[data-focus-pane]')];
  if (!tabs.length || !panes.length) return;
  function activate(name, focus = false) {
    if (!tabs.some(tab => tab.dataset.focusTab === name)) return;
    panes.forEach(pane => { pane.hidden = pane.dataset.focusPane !== name; });
    tabs.forEach(tab => {
      const active = tab.dataset.focusTab === name;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
  }
  function revealHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const target = document.getElementById(id);
    const pane = target?.closest('[data-focus-pane]');
    if (!pane) return;
    activate(pane.dataset.focusPane);
    let parent = target;
    while (parent && parent !== pane) {
      if (parent.tagName === 'DETAILS') parent.open = true;
      parent = parent.parentElement;
    }
    requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => {
      activate(tab.dataset.focusTab);
      // A clean URL opens on the summary next time; explicit deep links still work.
      history.replaceState(null, '', location.pathname + location.search);
    });
    tab.addEventListener('keydown', event => {
      const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length
        : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
      if (next < 0) return;
      event.preventDefault();
      activate(tabs[next].dataset.focusTab, true);
    });
  });
  document.body.classList.add('focus-ready');
  activate('now');
  window.addEventListener('hashchange', revealHash);
  // Also handle an already selected anchor (which does not emit hashchange).
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#"]');
    if (link && link.hash === location.hash) requestAnimationFrame(revealHash);
  });
  revealHash();
})();
