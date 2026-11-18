/* User-initiated forecast and lookup of the saved outcome. */
(function (root, factory) {
  'use strict';
  const formats = typeof module === 'object' && module.exports
    ? require('./morning_forecast.js') : root.VorbyMorningForecast;
  const api = factory(formats);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root.document) api.mount(root.document, root);
})(typeof window === 'object' ? window : globalThis, function (formats) {
  'use strict';
  const {price, percent, date} = formats;
  const endpoint = '/api/morning-forecast/job/';
  const positive = value => Number.isFinite(value) && value > 0;
  const finite = Number.isFinite;
  const minute = 60 * 1000;
  const pollDelay = 1800;
  const offline = 'Не удалось связаться с сайтом. Попробуй проверить состояние ещё раз.';
  const stages = {
    loading: 'Загружаем рыночные данные…',
    training: 'Готовим модель на сохранённой истории…',
    analyzing: 'Проверяем данные и расписание торгов…',
    forecasting: 'Рассчитываем и сохраняем прогноз…',
    checking_actual: 'Загружаем фактическую цену открытия…',
  };
  const directionText = {
    up: 'Весь диапазон выше вечерней цены. Модель предполагает рост.',
    down: 'Весь диапазон ниже вечерней цены. Модель предполагает снижение.',
    uncertain: 'Диапазон включает вечернюю цену. Направление остаётся неопределённым.',
  };

  function outcome(value, selection, quote, now = Date.now()) {
    if (selection?.mode !== 'live' || value?.ticker !== selection.ticker) return null;
    const forecast = value.forecast;
    if (!forecast || !positive(forecast.predicted_price) || !positive(forecast.anchor_price)) return null;
    const anchor = Date.parse(forecast.as_of);
    const target = Date.parse(forecast.target_at);
    if (date(forecast.as_of) === '—' || date(forecast.target_at) === '—' ||
        !(anchor <= now && target > anchor)) return null;
    const historical = forecast.historical === true || target <= now;
    const quoteTime = Date.parse(quote?.at);
    const displayedQuote = !historical && quote?.mode === selection.mode &&
      quote.ticker === selection.ticker && positive(quote.price) &&
      (!finite(quoteTime) || quoteTime >= anchor) ? quote.price : null;
    const reference = displayedQuote ?? forecast.anchor_price;
    const delta = forecast.predicted_price - reference;
    const comparableAt = date(forecast.comparable_at) !== '—'
      ? Date.parse(forecast.comparable_at) : target + 15 * minute;
    const actual = historical && now >= comparableAt && positive(forecast.actual_price)
      ? forecast.actual_price : null;
    return {
      forecast, historical, target, comparableAt, delta, displayedQuote,
      deltaPercent: delta / reference * 100,
      actual, canCompare: historical && !actual && now >= comparableAt,
    };
  }

  function mount(document, env) {
    const byId = id => document.getElementById(id);
    const panel = byId('forecast-overview');
    const form = byId('forecast-job-form');
    if (!panel || !form) return null;
    const run = byId('forecast-run');
    const compare = byId('forecast-compare');
    const status = byId('forecast-job-status');
    let selection = null;
    let quote = null;
    let value = null;
    let connectionError = false;
    let controller = null;
    let requestActive = false;
    let generation = 0;
    let poll = null;
    let clock = null;

    function setText(id, text) {
      const el = byId(id);
      if (el.textContent !== text) el.textContent = text;
      return el;
    }

    function detail(id, text) {
      const el = setText(id, text);
      el.hidden = !text;
    }

    function clearTimers() {
      env.clearTimeout(poll);
      env.clearTimeout(clock);
      poll = clock = null;
    }

    function render(now = Date.now()) {
      env.clearTimeout(clock);
      clock = null;
      const result = outcome(value, selection, quote, now);
      const ready = Boolean(result);
      const demo = selection?.mode === 'demo';
      const running = value?.status === 'running';
      const busy = requestActive || (running && !connectionError);
      const error = connectionError || value?.status === 'error';
      panel.dataset.state = error ? 'error' : busy ? 'running' : ready ? 'ok' : 'idle';
      let message = '';
      if (!selection) message = 'Выбери акцию.';
      else if (demo) message = 'Для расчёта по биржевым данным включи режим «Рынок».';
      else if (connectionError) message = offline;
      else if (running) message = stages[value.stage] || 'Выполняем расчёт…';
      else if (requestActive) message = 'Проверяем состояние…';
      else if (error) {
        message = value.message?.startsWith('Т-Банк сейчас недоступен')
          ? 'Нет связи с Т-Банком. Проверь подключение и попробуй ещё раз.'
          : value.message || 'Расчёт не завершился. Попробуй ещё раз.';
      }
      else if (!ready) message = value?.message || 'Расчёт доступен с понедельника по четверг после 17:45 МСК.';
      if (!connectionError && !running && value?.next_attempt_at && date(value.next_attempt_at) !== '—') {
        message += ` Ближайшее окно расчёта — ${date(value.next_attempt_at)}.`;
      }
      if (ready && result.historical && !result.actual && !result.canCompare && !busy && !error) {
        message = 'Цена открытия будет доступна для сверки после закрытия 15-минутной свечи.';
      }
      if (status.textContent !== message.trim()) status.textContent = message.trim();
      run.disabled = !selection || busy;
      run.textContent = demo ? 'Перейти к рынку' :
        connectionError ? 'Проверить состояние' :
        running ? 'Идёт расчёт…' : 'Рассчитать прогноз';
      compare.hidden = !result?.canCompare;
      compare.disabled = busy;
      setText('forecast-overview-label', result?.historical
        ? 'Сохранённая расчётная цена' : 'Расчётная цена на следующее торговое утро');
      setText('forecast-overview-price', ready ? price(result.forecast.predicted_price) : '—');
      const showAnchor = ready && !result.historical && !result.displayedQuote;
      byId('price').hidden = showAnchor;
      byId('price-change').hidden = showAnchor;
      detail('forecast-anchor-price', showAnchor ? price(result.forecast.anchor_price) : '');
      detail('forecast-anchor-source', showAnchor ? `Закрытие 15-минутной свечи · ${date(result.forecast.as_of)}` : '');
      if (!ready) {
        for (const key of ['detail', 'change', 'range', 'direction', 'actual']) detail('forecast-overview-' + key, '');
        return;
      }
      const {forecast, historical, target, comparableAt, delta, displayedQuote, deltaPercent, actual, canCompare} = result;
      const created = date(forecast.created_at);
      const timestamp = created === '—' ? '' : ` · Расчёт ${created}`;
      detail('forecast-overview-detail', `На ${date(forecast.target_at)} · Данные на ${date(forecast.as_of)}${timestamp}`);
      const digits = delta !== 0 && Math.abs(delta) < .01 ? 6 : 2;
      const rubles = delta.toLocaleString('ru-RU', {minimumFractionDigits:digits, maximumFractionDigits:digits, signDisplay:'exceptZero'});
      const reference = displayedQuote ? 'от последней цены' : `от вечерней цены ${price(forecast.anchor_price)}`;
      detail('forecast-overview-change', `Изменение ${reference} · ${rubles} ₽ (${percent(deltaPercent)})`);
      const hasRange = positive(forecast.lower_price) && positive(forecast.upper_price) &&
        forecast.lower_price <= forecast.predicted_price && forecast.predicted_price <= forecast.upper_price;
      detail('forecast-overview-range', hasRange ? `Возможный диапазон ${price(forecast.lower_price)} – ${price(forecast.upper_price)}` : '');
      detail('forecast-overview-direction', directionText[forecast.direction] || '');
      const errorRub = forecast.absolute_error_rub;
      const errorPct = forecast.absolute_error_pct;
      const errorText = finite(errorRub) && errorRub >= 0 && finite(errorPct) && errorPct >= 0
        ? ` · Ошибка ${errorRub.toLocaleString('ru-RU', {maximumFractionDigits: 6})} ₽ (${errorPct.toLocaleString('ru-RU', {maximumFractionDigits: 2})}%)` : '';
      detail('forecast-overview-actual', actual ? `Фактическая цена ${price(actual)}${errorText}` : '');
      if (!historical || (!actual && !canCompare)) {
        const changeAt = historical ? comparableAt : target;
        clock = env.setTimeout(() => render(Math.max(Date.now(), changeAt)), Math.max(1, changeAt - now));
      }
    }

    async function request(action) {
      if (selection?.mode !== 'live' || requestActive || (action && value?.status === 'running')) return;
      const marker = generation;
      const symbol = selection.ticker;
      requestActive = true;
      connectionError = false;
      env.clearTimeout(poll);
      controller = new env.AbortController();
      const active = controller;
      const deadline = env.setTimeout(() => active.abort(), 15000);
      const options = {cache: 'no-store', signal: active.signal};
      let url = endpoint + '?ticker=' + encodeURIComponent(symbol);
      if (action) {
        const csrf = form.elements.namedItem('csrfmiddlewaretoken')?.value;
        url = endpoint;
        options.method = 'POST';
        options.headers = {'Content-Type':'application/json', 'X-CSRFToken': csrf || ''};
        options.body = JSON.stringify(action === 'compare' ? {ticker:symbol, action} : {ticker:symbol});
      }
      render();
      try {
        const response = await env.fetch(url, options);
        const result = await response.json();
        if (marker !== generation) return;
        if (!result || result.ticker !== symbol || (!response.ok && result.status !== 'error')) {
          throw new Error('unexpected forecast result');
        }
        const unfinished = result.status === 'running' || result.status === 'error';
        value = unfinished && !result.forecast && value?.forecast
          ? {...result,forecast:value.forecast} : result;
      } catch {
        if (marker === generation) connectionError = true;
      } finally {
        env.clearTimeout(deadline);
        if (marker === generation) {
          requestActive = false;
          controller = null;
          render();
          if (!connectionError && value?.status === 'running') poll = env.setTimeout(request, pollDelay);
        }
      }
    }

    function select(next) {
      if (next?.ticker === selection?.ticker && next?.mode === selection?.mode) return;
      generation++;
      controller?.abort();
      clearTimers();
      selection = next;
      quote = null;
      value = null;
      connectionError = false;
      requestActive = false;
      if (next?.mode === 'live') request();
      else render();
    }
    document.addEventListener('vorby:analysis-selection', event => select(event.detail));
    document.addEventListener('vorby:analysis-quote', event => {
      quote = event.detail;
      if (value?.forecast) render();
    });
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (selection?.mode === 'demo') {
        document.querySelector('[data-mode="live"]')?.click();
        return;
      }
      request(connectionError ? undefined : 'start');
    });
    compare.addEventListener('click', () => request('compare'));
    return {select};
  }

  return {mount, outcome};
});
