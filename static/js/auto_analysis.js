(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('symbol-form');
  if (!form) return;
  const {price, percent, date} = window.VorbyMorningForecast;
  const endpoint = '/api/analysis/morning/';
  const maxPolls = 180;
  const stageLabels = {loading:'Загружаем данные…',analyzing:'Анализируем…',forecasting:'Рассчитываем прогноз…'};
  const validPrice = x => Number.isFinite(x) && x > 0;
  let ticker, quote, forecast, controller, timer, revision = 0, stage = '', marketPending = false;

  function setProgress() {
    $('analysis-progress').textContent = stageLabels[stage] || (marketPending ? 'Загружаем данные…' : '');
  }

  function detail(id, value) {
    const el = $(id);
    el.textContent = value;
    el.hidden = !value;
  }

  function render() {
    const now = Date.now();
    const target = Date.parse(forecast?.target_at);
    const anchor = Date.parse(forecast?.as_of);
    const hasForecast = validPrice(forecast?.predicted_price) && validPrice(forecast.anchor_price) && anchor <= now && target > now;
    $('forecast-overview-price').textContent = hasForecast ? price(forecast.predicted_price) : '—';
    detail('forecast-overview-detail', hasForecast ? `На ${date(forecast.target_at)} · Свечи до ${date(forecast.as_of)}` : '');
    if (!hasForecast) {
      detail('forecast-overview-change', '');
      if (forecast && target <= now) $('forecast-job-status').textContent = 'Время прогноза прошло. Нажми «Анализировать» для нового расчёта.';
      return;
    }
    const quoteAt = Date.parse(quote?.at);
    const latest = quote?.mode === 'live' && quote.ticker === ticker && validPrice(quote.price) && quoteAt <= now;
    const useQuote = latest && quoteAt >= anchor;
    if (!useQuote) {
      $('price').dataset.anchorDisplayed = 'true';
      $('price').textContent = price(forecast.anchor_price);
      $('price-change').textContent = `Закрытие свечи · ${date(forecast.as_of)}`;
    }
    const reference = useQuote ? quote.price : forecast.anchor_price;
    const delta = forecast.predicted_price - reference;
    const formatted = new Intl.NumberFormat('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:Math.abs(delta)<.01?6:2, signDisplay:'exceptZero'}).format(delta);
    detail('forecast-overview-change', `${formatted} ₽ (${percent(delta/reference*100)}) к указанной цене`);
  }

  async function load(post, attempt, symbol, polls = 0) {
    const active = new AbortController();
    controller = active;
    const deadline = setTimeout(() => active.abort(), 15000);
    try {
      const options = {signal:active.signal,cache:'no-store'};
      let url = endpoint + '?ticker=' + encodeURIComponent(symbol);
      if (post) {
        url = endpoint;
        options.method = 'POST';
        options.headers = {'Content-Type':'application/json','X-CSRFToken':form.elements.namedItem('csrfmiddlewaretoken').value};
        options.body = JSON.stringify({ticker:symbol});
      }
      const response = await fetch(url, options);
      const result = await response.json();
      if (revision !== attempt) return;
      if (result.ticker !== symbol) throw new Error('Different ticker');
      if (result.status === 'running') {
        if (polls >= maxPolls) throw new Error('Forecast timeout');
        stage = result.stage;
        timer = setTimeout(() => load(false, attempt, symbol, polls+1), 850);
        return;
      }
      stage = '';
      $('forecast-overview').dataset.state = result.status === 'ok' ? 'ok' : 'error';
      $('forecast-job-status').textContent = result.status === 'ok' ? 'Расчётная оценка по биржевым свечам.' :
        result.message || 'Не удалось завершить расчёт. Нажми «Анализировать» ещё раз.';
      forecast = result.status === 'ok' ? result.forecast : null;
      if (forecast) {
        const inst = forecast.instrument;
        if (inst?.ticker === symbol && inst.name) {
          $('instrument-name').textContent = inst.name;
          $('ticker-label').textContent = `${symbol} · ${inst.lot} шт. в лоте`;
        }
        const q = forecast.quality;
        if (q) $('auto-morning-quality').textContent = `Проверка ${q.from} — ${q.to}: ${q.test} прогнозов. Средняя ошибка ${price(q.mae_rub)} (${percent(q.mape).replace(/^\+/, '')}), у ориентира «цена не изменится» — ${price(q.baseline_mae_rub)} (${percent(q.baseline_mape).replace(/^\+/, '')}). Для обучения использовано ${q.train} более ранних сеансов.`;
      }
      render();
    } catch {
      if (revision !== attempt) return;
      stage = '';
      $('forecast-overview').dataset.state = 'error';
      $('forecast-job-status').textContent = 'Не удалось загрузить данные. Нажми «Анализировать» ещё раз.';
    } finally {
      clearTimeout(deadline);
      if (revision === attempt) setProgress();
    }
  }

  function reset() {
    revision++;
    controller?.abort();
    clearTimeout(timer);
    stage = '';
    marketPending = false;
    ticker = quote = forecast = null;
    delete $('price').dataset.anchorDisplayed;
    $('forecast-overview').dataset.state = 'idle';
    $('forecast-job-status').textContent = 'Выбери акцию и нажми «Анализировать».';
    $('auto-morning-quality').textContent = '';
    render();
    setProgress();
  }

  document.addEventListener('vorby:analysis-selection', event => {
    if (event.detail.mode === 'demo' || ticker && event.detail.ticker !== ticker) reset();
  });
  document.addEventListener('vorby:analysis-start', event => {
    reset();
    ticker = event.detail.ticker;
    stage = 'loading';
    marketPending = true;
    $('forecast-overview').dataset.state = 'running';
    $('forecast-job-status').textContent = 'Результат появится после расчёта.';
    setProgress();
    load(true, revision, ticker);
  });
  document.addEventListener('vorby:analysis-market', event => {
    if (event.detail.ticker !== ticker) return;
    marketPending = event.detail.pending;
    setProgress();
  });
  document.addEventListener('vorby:analysis-quote', event => {
    quote = event.detail;
    if (forecast) render();
  });
  setInterval(() => { if (forecast) render(); }, 15000);
})();
