(() => {
  'use strict';
  const labels = {share: 'Акция', etf: 'Фонд', currency: 'Валюта'};

  async function search(query, sharesOnly = false) {
    const suffix = sharesOnly ? '&kind=share' : '';
    const response = await fetch(`/api/search/?q=${encodeURIComponent(query)}${suffix}`, {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok || data.status !== 'ok') throw new Error(data.message || 'Поиск временно недоступен');
    return data.results || [];
  }

  async function exact(ticker, sharesOnly = true) {
    const value = ticker.trim().toUpperCase();
    const results = await search(value, sharesOnly);
    return results.find(item => item.ticker.toUpperCase() === value) || null;
  }

  function attach({input, dropdown, onSelect, sharesOnly = false, getQuery = null, isAdded = null}) {
    if (!input || !dropdown) return;
    let timer, revision = 0, active = -1, options = [];
    dropdown.setAttribute('role', 'listbox');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');

    function close() {
      dropdown.style.display = 'none';
      input.setAttribute('aria-expanded', 'false');
      active = -1;
    }
    function open() {
      dropdown.style.display = 'block';
      input.setAttribute('aria-expanded', 'true');
    }
    function message(value) {
      dropdown.replaceChildren();
      const row = document.createElement('div');
      row.className = 'search-message';
      row.textContent = value;
      dropdown.append(row);
      options = [];
      open();
    }
    function emphasize(container, ticker, query) {
      const start = ticker.toUpperCase().indexOf(query.toUpperCase());
      if (start < 0) { container.textContent = ticker; return; }
      container.append(document.createTextNode(ticker.slice(0, start)));
      const mark = document.createElement('mark');
      mark.textContent = ticker.slice(start, start + query.length);
      container.append(mark, document.createTextNode(ticker.slice(start + query.length)));
    }
    function render(items, query) {
      dropdown.replaceChildren();
      options = [];
      active = -1;
      if (!items.length) { message('Ничего не найдено. Проверь тикер.'); return; }
      items.forEach(item => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'search-item';
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', 'false');
        const tickerLine = document.createElement('span');
        tickerLine.className = 'search-item-ticker';
        emphasize(tickerLine, item.ticker, query);
        const type = document.createElement('span');
        type.className = 'instrument-type';
        type.textContent = labels[item.type] || item.type;
        tickerLine.append(type);
        const name = document.createElement('span');
        name.className = 'search-item-name';
        name.textContent = item.name;
        button.append(tickerLine, name);
        if (isAdded && isAdded(item)) {
          button.classList.add('already-added');
          const badge = document.createElement('span');
          badge.className = 'instrument-added';
          badge.textContent = 'Уже добавлена';
          tickerLine.append(badge);
        }
        button.addEventListener('click', () => { onSelect(item); close(); });
        dropdown.append(button);
        options.push(button);
      });
      open();
    }
    function currentQuery() { return (getQuery ? getQuery(input.value) : input.value).trim(); }
    async function update() {
      const requestId = ++revision;
      const query = currentQuery();
      if (query.length < 2) { close(); return; }
      message('Ищем акции…');
      try {
        const items = await search(query, sharesOnly);
        if (requestId !== revision || query !== currentQuery()) return;
        render(items, query);
      } catch {
        if (requestId === revision) message('Поиск недоступен. Попробуй ещё раз.');
      }
    }
    input.addEventListener('input', () => {
      clearTimeout(timer);
      ++revision;
      if (currentQuery().length < 2) { close(); return; }
      timer = setTimeout(update, 180);
    });
    input.addEventListener('focus', () => { if (currentQuery().length >= 2) update(); });
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { close(); return; }
      if (!options.length || dropdown.style.display === 'none') return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        active = (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
        options.forEach((button, index) => button.setAttribute('aria-selected', String(index === active)));
        options[active].scrollIntoView({block: 'nearest'});
      } else if (event.key === 'Enter' && active >= 0) {
        event.preventDefault();
        options[active].click();
      }
    });
    document.addEventListener('click', event => {
      if (!input.contains(event.target) && !dropdown.contains(event.target)) close();
    });
    return {close, update};
  }
  window.InstrumentSearch = {attach, exact, search};
})();
