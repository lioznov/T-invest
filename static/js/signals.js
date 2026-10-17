(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (v, digits=2) => Number.isFinite(v) ? v.toLocaleString('ru-RU',{maximumFractionDigits:digits,minimumFractionDigits:digits}) : '—';
  const money = v => Number.isFinite(v) ? `${num(v)} ₽` : '—';
  const time = value => value ? new Date(value).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Europe/Moscow'})+' МСК' : '—';
  const day = value => new Date(value).toLocaleDateString('ru-RU',{day:'2-digit',month:'short',timeZone:'Europe/Moscow'});
  const snapshots = count => `${count} ${count%10===1 && count%100!==11?'снимок':count%10>=2 && count%10<=4 && (count%100<12 || count%100>14)?'снимка':'снимков'} за 2 минуты`;
  const read = (key,fallback) => {try {return JSON.parse(localStorage.getItem(key)) ?? fallback;} catch {return fallback;}};
  const save = (key,value) => {try {localStorage.setItem(key,JSON.stringify(value));} catch { /* Private browser: page still works. */ }};
  const validTicker = value => /^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(value);
  const watch = document.body.dataset.page==='watchlist';
  const query = new URLSearchParams(location.search);
  let mode = query.get('mode')==='demo' ? 'demo' : 'live';
  let ticker = (query.get('ticker') || read('vorby.symbol','SBER')).toUpperCase();
  if (!validTicker(ticker)) ticker='SBER';
  let symbols=read('vorby.watchlist',['SBER','GAZP','LKOH','YDEX']);
  if (!Array.isArray(symbols)) symbols=[];
  symbols=[...new Set(symbols.filter(s=>typeof s==='string' && validTicker(s)))].slice(0,20);
  let generation=0, timer, researchGeneration=0, displayedUntil=0, paperRevision=0, riskRevision=0, patternResearchRevision=0;
  let patternUntil=0;
  let riskContext='', riskTouched=false, currentPlan=null;
  let researchHistory=read('vorby.researchHistory','archive')==='current'?'current':'archive';
  const results=new Map();
  let currentAi=null;
  let chartSnapshot=null,structureWasFresh=false;

  async function request(url, timeoutMs=15000) {
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),timeoutMs);
    try {
      const response=await fetch(url,{signal:controller.signal,cache:'no-store'});
      const payload=await response.json();
      if (!response.ok) throw new Error(payload.message || 'Не удалось получить ответ сервера.');
      return payload;
    } finally {clearTimeout(timeout);}
  }
  async function monitorHealth() {
    const node=$('monitor-health');
    if(!node)return;
    try {
      const h=await request('/api/monitor-health/',5000);
      const labels={not_started:'Не запущен',starting:'Запуск',connecting:'Подключение',collecting:'Сбор данных',reconnecting:'Переподключение',stale:'Нет свежего подтверждения работы',deadline:'Завершён по времени',interrupted:'Остановлен',disk_limit:'Остановлен: лимит архива',retries_exhausted:'Остановлен: связь не восстановилась',subscription_rejected:'Подписка отклонена',archive_error:'Ошибка записи',unavailable:'Статус недоступен'};
      const pulse=Date.parse(h.heartbeat_at),fresh=Number.isFinite(pulse) && Date.now()-pulse>=0 && Date.now()-pulse<=20000;
      const active=h.alive===true && fresh;
      node.textContent=`${active?'Работает':'Сейчас не работает'} · ${labels[h.status] || 'Статус неизвестен'}${Array.isArray(h.tickers)?` · ${h.tickers.join(', ')}`:''}. Стаканов: ${num(h.books,0)}, сделок: ${num(h.trades,0)}, закрытых свечей: ${num(h.candles,0)}. ${h.last_message_at?`Последнее сообщение: ${time(h.last_message_at)}.`:''} Анализ без браузера: ${active && h.signal_worker_running===true?'процесс запущен':'не подтверждён'}. Границы подключений могут содержать пропуски; наличие процесса не подтверждает свежий сигнал.`;
    } catch {
      node.textContent='Статус фонового сбора недоступен. Работа сборщика не подтверждена; сигналы сайта оцениваются отдельно.';
    }
  }
  monitorHealth();
  setInterval(monitorHealth,10000);
  function banner() {
    document.querySelectorAll('[data-mode]').forEach(button=>button.classList.toggle('selected',button.dataset.mode===mode));
    $('mode-banner').classList.toggle('demo',mode==='demo');
    $('mode-banner').textContent=mode==='demo'
      ? 'УЧЕБНЫЙ РЕЖИМ · Цены и результаты вымышлены. Это демонстрация интерфейса и правил, не торговые сигналы.'
      : 'Т-Банк · Реальные данные, экспериментальные сигналы. Кандидат покупки — совпадение шести условий; «Проверь продажу» — повод оценить выход из уже купленной акции. Доходность этих сигналов не доказана.';
  }
  function aiState(value) {
    if(value?.status==='ok' && (!value.valid_until || !Number.isFinite(Date.parse(value.valid_until)) || Date.now()>Date.parse(value.valid_until)))return {...value,status:'waiting',decision:'WAIT',score:null,plan:null,drivers:[],message:'Оценка ИИ устарела. Ждём новую свечу и котировки.'};
    return value;
  }
  function renderStructureResearch(data) {
    const node=$('structure-research');
    if(!node)return;
    if(data?.status!=='ok'){node.textContent=data?.message || 'Проверка уровней на истории недоступна.';return;}
    const names={train:'Март–июль',calibration:'Август',test:'Сентябрь — 2 октября',additional:'3–12 октября'};
    const result=(r)=>r.unknown_trades?'Исходы неизвестны':!r.trades?'Нет сделок':num(r.return_pct,3)+'%';
    const rows=data.variants.flatMap(v=>Object.entries(v.periods).map(([p,r])=>`<tr><td>${esc(v.name)}</td><td>${esc(names[p]||p)}</td><td>${num(r.base.trades,0)}</td><td>${result(r.base)}</td><td>${result(r.double_cost)}</td><td>${num(r.base.unknown_trades,0)}</td></tr>`)).join('');
    node.innerHTML=`<p>${esc(data.message)} ${mode==='demo'?'Это результаты реальной сохранённой истории, не вымышленные цены учебного графика.':''}</p><div class="table-scroll"><table><thead><tr><th>Правило</th><th>Период 2026</th><th>Сделок</th><th>После расходов</th><th>Расходы ×2</th><th>Неизвестных</th></tr></thead><tbody>${rows}</tbody></table></div><p>${esc(data.note)}</p>`;
  }
  function structureState(p) {
    if(!window.VorbyStructure?.validPanel(p))return null;
    const until=Date.parse(p.valid_until);
    return {...p,status:p.status==='ok' && Number.isFinite(until) && Date.now()<=until?'ok':'stale'};
  }
  function renderStructure(p) {
    const node=$('structure-status');
    if(!node)return;
    const current=structureState(p);
    structureWasFresh=current?.status==='ok';
    if(!current){node.textContent=p?.message || 'Нет проверенной истории для разметки.';$('structure-levels').innerHTML='';return;}
    node.textContent=`${mode==='demo'?'Учебная разметка вымышленных цен. ':current.source_confirmed?'Биржевые свечи. ':'Источник свечей не подтверждён. '}${current.status==='ok'?current.message:'Историческая разметка: данные устарели, текущий пробой не подтверждается.'} Закрытие: ${day(current.evaluated_at)}, ${time(current.evaluated_at)}. Не сигнал покупки.`;
    $('structure-levels').innerHTML=current.levels.map(l=>`<div class="structure-level ${l.kind==='resistance'?'resistance':''} ${l.broken?'broken':''}"><span>${l.kind==='support'?'Поддержка':'Сопротивление'}</span><strong>${money(l.price)}</strong><small>Известна с ${day(l.known_at)}, ${time(l.known_at)}<br>${l.broken?'Закрытие уже вышло за уровень':'Цена пока не вышла за уровень'} · точек рядом: ${num(l.touches,0)}</small></div>`).join('') || '<p class="research-meta">Пока нет подтверждённых вершин и впадин в выбранном окне.</p>';
  }
  function renderAiComparison(c) {
    const node=$('ai-comparison');
    if(!node)return;
    if(c?.status!=='ok'){node.textContent=c?.message || 'Результатов новой модели нет.';return;}
    const names={test:'Сентябрь — 2 октября',additional:'3–12 октября'};
    const rows=Object.entries(c.tests).map(([p,e])=>`<tr><td>${esc(names[p] || p)}</td><td>${num(e.v2.classification.samples,0)}</td><td>${num(e.v1.classification.auc,3)} → ${num(e.v2.classification.auc,3)}</td><td>${num(e.v1.classification.brier,4)} → ${num(e.v2.classification.brier,4)}</td><td>${num(e.v1.base.trades,0)} → ${num(e.v2.base.trades,0)}</td><td>${e.v2.base.trades?num(e.v2.base.return_pct,3)+'%':'Нет сделок'}</td><td>${e.v2.double_cost.trades?num(e.v2.double_cost.return_pct,3)+'%':'Нет сделок'}</td></tr>`).join('');
    node.innerHTML=`<p>${esc(c.message)}</p><p>${num(c.features_count,0)} признаков · обучение: ${num(c.trained_rows,0)} примеров · настройка оценки: ${num(c.calibration_rows,0)}.</p><div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Период 2026</th><th>Примеров</th><th>Различение исходов: v1 → v2</th><th>Ошибка оценки: v1 → v2</th><th>Сделки: v1 → v2</th><th>Итог v2 после расходов</th><th>Расходы ×2</th></tr></thead><tbody>${rows}</tbody></table></div><p>AUC («различение»): около 0,5 — почти случайное ранжирование; больше — лучше. Brier («ошибка»): меньше — лучше. Это не шанс заработка. Итоги — среднее семи отдельных условных счетов по 100 000 ₽; отсутствие сделок не доказывает доходность.</p><p>${esc(c.note)}</p>`;
  }
  function renderAi(value) {
    currentAi=value;
    const a=aiState(value);
    const node=$('ai-result');
    if(!a || a.status!=='ok') {node.innerHTML=`<p class="muted">${esc(mode==='demo'?'ИИ оценивает реальные биржевые данные; учебные цены не используются.':a?.message || 'Ожидаем свежую свечу и подтверждённый стакан для этой акции.')}</p>`;return;}
    node.innerHTML=`<div class="ai-reading"><div><span class="muted">Оценка модели</span><strong>${num(a.score,0)}<small>/100</small></strong></div><div><h3>${a.decision==='WATCH'?'Наблюдать за акцией':'Подождать со входом'}</h3><p class="muted">${esc(a.message)}</p><p class="research-meta">Свеча закрылась: ${time(a.candle_closed_at)} · оценка условной покупки на час</p></div></div><div class="ai-drivers">${(a.drivers || []).map(d=>`<span>${d.direction==='up'?'↑':'↓'} ${esc(d.name)} <small>${d.direction==='up'?'повышает оценку':'снижает оценку'}</small></span>`).join('')}</div>${a.plan?`<p class="research-meta">Исследовательский сценарий: вход ${money(a.plan.entry)} · стоп ${money(a.plan.stop)} · выход до ${time(a.plan.exit_by)}. Покупка ещё не подтверждена проверкой будущих наблюдений.</p>`:''}<p class="research-meta">Баллы модели не являются обещанием заработка.</p>`;
  }
  async function loadAiModel(myGeneration) {
    try {
      const data=await request('/api/ai-model/');
      if(myGeneration!==generation)return;
      renderAiComparison(data.comparison);
      renderStructureResearch(data.structure_research);
      if(data.status!=='ok') {$('ai-model-badge').textContent='Модель недоступна';$('ai-model-description').textContent=data.message;$('ai-model-summary').textContent='';$('ai-model-evidence').textContent='Результатов обученной модели нет.';return;}
      $('ai-model-badge').textContent=`${data.version==='market_ai_boost_v2'?'v2':'v1'} · ${data.paper_ready?'Бумажная проверка':'Режим исследования'}`;
      $('ai-model-description').textContent=`${data.message} ${data.readiness_message}`;
      $('ai-model-summary').innerHTML=`<div><span>Примеров для обучения</span><strong>${num(data.trained_rows,0)}</strong></div><div><span>Примеров для настройки оценки</span><strong>${num(data.calibration_rows,0)}</strong></div><div><span>Обучение завершено</span><strong>${day(data.trained_at)}</strong></div>`;
      const names={test:'Сентябрь — 2 октября',additional:'3–12 октября'};
      const tests=Object.entries(data.tests).map(([key,e])=>`<tr><td>${esc(names[key] || key)}</td><td>${num(e.classification.samples,0)}</td><td>${num(e.classification.accuracy_pct,1)}% / ${num(e.classification.baseline_accuracy_pct,1)}%</td><td>${num(e.base.trades,0)}</td><td>${e.base.trades?num(e.base.return_pct,3)+'%':'Нет сделок'}</td><td>${e.double_cost.trades?num(e.double_cost.return_pct,3)+'%':'Нет сделок'}</td></tr>`).join('');
      $('ai-model-evidence').innerHTML=`<p>${esc(data.note)}</p><p>Акции обучения: ${esc(data.tickers.join(', '))}. Порог наблюдения: ${num(data.threshold_score,0)} баллов.</p><div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Проверка 2026</th><th>Примеров</th><th>Совпадения: ИИ / простой прогноз</th><th>Сделок</th><th>После расходов</th><th>Расходы ×2</th></tr></thead><tbody>${tests}</tbody></table></div><p>Простой прогноз всегда выбирает наиболее частый исход обучения. Процент совпадений — качество классификации, а не вероятность прибыли. Денежные результаты — среднее семи отдельных условных счетов по 100 000 ₽.</p>`;
    } catch {
      if(myGeneration===generation){$('ai-model-badge').textContent='Нет связи с сервером';$('ai-model-description').textContent='Не удалось проверить обученную модель.';if($('ai-comparison'))$('ai-comparison').textContent='Сравнение моделей недоступно: нет ответа сервера.';}
    }
  }
  function aiWatchMarkup(a) {
    const state=aiState(a?.ai);
    const structure=structureState(a?.market_structure);
    const note=structure?`<div class="watch-structure">Разметка: ${esc(structure.status==='ok'?structure.message:'Исторические уровни, данные устарели')}. Без команды покупки.</div>`:'';
    return `<div class="watch-ai"><strong>ИИ: ${state?.status==='ok'?num(state.score,0)+' / 100 · '+(state.decision==='WATCH'?'наблюдать':'ждать'):'ожидание'}</strong><span>${esc(state?.message || (mode==='demo'?'Реальная модель не оценивает учебные цены.':'Ждём данные для модели.'))}</span></div>${note}`;
  }
  function clearSingle(message='Получаем данные…') {
    displayedUntil=0;
    $('load-status').textContent=message;
    $('instrument-name').textContent=ticker;
    $('ticker-label').textContent=`${ticker} · TQBR`;
    $('ticker-avatar').textContent=ticker[0];
    ['price','buy-share','rvol','vwap','spread','data-time'].forEach(id=>$(id).textContent='—');
    chartSnapshot=null;structureWasFresh=false;renderStructure(null);
    $('price-change').textContent='Нет актуальной оценки';
    $('decision').textContent='Ожидание'; $('decision').classList.remove('positive','negative');
    $('score').innerHTML='—<small>/100</small>'; $('score-fill').style.width='0';
    $('passed').textContent='Условия не оценены';
    $('decision-text').textContent='До получения свежих данных сигнал заблокирован.';
    $('blockers').textContent=''; $('rules').innerHTML=''; $('price-chart').innerHTML='';
    $('data-quality').textContent='';
    $('range-levels').textContent='Уровни появятся после загрузки закрытых свечей.';
    $('chart-empty').classList.remove('hidden'); $('chart-period').textContent='—';
    $('orderbook').innerHTML='<p class="muted">Ожидаем заявки биржи</p>';
    $('book-samples').textContent='0 снимков';
    $('trade-plan').innerHTML='<p class="muted">План покупки появится, когда все шесть условий будут выполнены на свежих данных.</p>';
    currentPlan=null;$('use-plan').disabled=true;
    $('risk-plan-note').textContent='Свежего сценария покупки сейчас нет. Введённые цены — твой самостоятельный расчёт.';
    clearPatterns(message);
    renderAi(null);
  }
  const patternDefinitions=[
    {key:'reclaim',name:'Возврат после ложного пробоя',shape:'Цена → ниже поддержки → снова выше',explanation:'Цена ненадолго упала ниже прежней поддержки, но закрылась обратно выше неё.'},
    {key:'orb_retest',name:'Пробой и проверка уровня',shape:'Утренний диапазон → пробой → ретест',explanation:'Цена вышла выше утреннего диапазона, вернулась к его верхней границе и удержала её сверху.'},
    {key:'flag',name:'Продолжение после отката',shape:'Рост → спокойный откат → продолжение',explanation:'После роста цена спокойно откатилась и затем снова вышла вверх из отката.'},
    {key:'engulf_pullback',name:'Поглощение после отката',shape:'Красная свеча → зелёная перекрывает её тело',explanation:'Покупатели вернули цену после отката возле средней цены. Польза этой формы не подтверждена.'},
    {key:'hammer_confirm',name:'Молот у поддержки',shape:'Длинная нижняя тень → отдельное подтверждение',explanation:'Цена отскочила от поддержки; только следующая закрытая свеча подтверждает восстановление.'},
    {key:'inside_break',name:'Внутренняя свеча и пробой',shape:'Большой диапазон → маленький → выход вверх',explanation:'Цена сжалась внутри предыдущего диапазона, а следующая свеча закрылась выше него.'}
  ];
  function patternState(a) {
    const p=a?.patterns;
    if(!p || p.version!=='price_patterns_v1')return {status:'insufficient',message:'Разбор паттернов пока недоступен. Ждём совместимые данные сервера.',payload:null,until:0};
    if(p.status!=='ok')return {status:p.status,message:p.message || 'Недостаточно свежих свечей для оценки.',payload:p,until:0};
    if(p.entry_allowed!==false || !Array.isArray(p.items))return {status:'invalid',message:'Формат оценки не подтверждён. Паттерны не используются для входа.',payload:null,until:0};
    const evaluated=Date.parse(p.evaluated_at);
    if(!Number.isFinite(evaluated))return {status:'invalid',message:'Не удалось проверить время закрытой свечи.',payload:null,until:0};
    if(mode==='demo')return {status:'ok',message:p.message || 'Учебные формы на вымышленных ценах.',payload:p,until:0};
    const quoted=Date.parse(a.data_time),updated=Date.parse(a.updated_at),now=Date.now();
    const until=Math.min(evaluated+17*60000,quoted+45000,updated+35000);
    if(!Number.isFinite(until) || evaluated>now+60000 || until<=now)return {status:'stale',message:'Свечи или рыночный снимок устарели. Отметки скрыты до свежей оценки.',payload:p,until:0};
    return {status:'ok',message:p.message || 'Проверены закрытые свечи. Это наблюдение, не команда покупки.',payload:p,until};
  }
  function patternCards(items=[],available=false) {
    return patternDefinitions.map(def=>{
      const item=items.find(x=>x?.key===def.key),evaluated=available && typeof item?.detected==='boolean',detected=evaluated && item.detected===true;
      const label=detected?'Форма обнаружена':evaluated?'Сейчас не обнаружена':'Не оценено';
      const detail=available && item?.explanation?item.explanation:def.explanation;
      const signalAt=Date.parse(item?.signal_time),closedAt=Number.isFinite(signalAt)?new Date(signalAt+15*60000).toISOString():null;
      return `<article class="pattern-card ${detected?'detected':''}"><div class="pattern-card-heading"><h3>${esc(def.name)}</h3><span class="pattern-detection">${label}</span></div><div class="pattern-shape">${esc(def.shape)}</div><p>${esc(detail)}</p>${detected?`<div class="pattern-observation">Закрытие сигнальной свечи: ${esc(time(closedAt))}${Number.isFinite(item.level)?` · уровень ${money(item.level)}`:''}</div>`:''}</article>`;
    }).join('');
  }
  function clearPatterns(message='Получаем свечи для разбора…') {
    if(watch)return;
    patternUntil=0;
    $('pattern-status').textContent=message;
    $('pattern-items').innerHTML=patternCards();
    $('pattern-context').innerHTML='';
    $('pattern-time').textContent='Свеча не оценена';
    $('pattern-market').textContent='Стакан и лента пока не оценены';
    $('pattern-evidence').textContent='Прибыльность паттернов не подтверждена. Совпадение формы не разрешает покупку.';
    $('price-chart').querySelectorAll('[data-pattern-marker]').forEach(node=>node.remove());
  }
  function renderPatterns(a) {
    const state=patternState(a),p=state.payload,available=state.status==='ok';
    const extra=a.candle_patterns,extraOk=available && extra?.status==='ok' && extra.version==='candle_patterns_v3' && extra.entry_allowed===false && extra.evaluated_at===p.evaluated_at;
    patternUntil=state.until;
    $('pattern-status').textContent=mode==='demo'?`Учебный режим: цены и отметки вымышлены. ${state.message}`:state.message;
    $('pattern-items').innerHTML=patternCards([...(p?.items || []),...(extraOk && Array.isArray(extra.items)?extra.items:[])],available);
    $('pattern-context').innerHTML=available && Array.isArray(p.context)?p.context.map(c=>`<div class="pattern-check ${c.pass===true?'checked':''}"><span aria-hidden="true">${c.pass===true?'✓':c.pass===false?'−':'…'}</span><div><strong>${esc(c.name)}</strong><p>${esc(c.detail)}</p></div></div>`).join(''):'';
    $('pattern-time').textContent=available?`Закрытая свеча: ${day(p.evaluated_at)}, ${time(p.evaluated_at)}`:'Свежей оценки нет';
    $('pattern-market').textContent=mode==='demo'?'Стакан и лента вымышлены':available && p.market_confirmed===true?'Контекст стакана и ленты совпал · не разрешение входа':'Контекст стакана и ленты не подтверждён';
    $('pattern-evidence').textContent=mode==='demo'?'Учебная демонстрация не доказывает доходность. ◇ на графике — только наблюдение формы.':`${p?.evidence?.message || 'Прибыльность паттернов не подтверждена.'} ◇ на графике — наблюдение, не рекомендация покупки.`;
    return [...(available && Array.isArray(p.markers)?p.markers:[]),...(extraOk && Array.isArray(extra.markers)?extra.markers:[])];
  }
  function patternWatchMarkup(a) {
    if(!a)return '<div class="watch-pattern muted">Паттерны: ожидаем оценку</div>';
    const state=patternState(a),extra=a.candle_patterns,extraOk=state.status==='ok' && extra?.status==='ok' && extra.version==='candle_patterns_v3' && extra.entry_allowed===false && extra.evaluated_at===state.payload.evaluated_at;
    const found=state.status==='ok'?[...state.payload.items,...(extraOk && Array.isArray(extra.items)?extra.items:[])].filter(x=>x?.detected===true).map(x=>patternDefinitions.find(def=>def.key===x.key)?.name).filter(Boolean):[];
    const text=state.status==='ok'?(found.length?found.join(' · '):'Сейчас не обнаружены'):state.status==='stale'?'Оценка устарела':'Пока не оценены';
    return `<div class="watch-pattern"><span>Паттерны · ${mode==='demo'?'учебные':'эксперимент'}</span><strong>${esc(text)}</strong><small>Без команды покупки</small></div>`;
  }
  function ruleValue(rule,a) {
    const values={trend:`EMA 50: ${money(a.ema)}`, flow:`Покупки: ${num(a.buy_share===null?null:a.buy_share*100,0)}% · ${a.trade_count} сделок`,
      volume:`Объём: ${num(a.rvol)}×`,vwap:`VWAP: ${money(a.vwap)}`,spread:`Спред: ${num(a.spread_bps===null?null:a.spread_bps/100,3)}%`,
      book:a.retention===null?'Накапливаем снимки стакана':`Сохранилось ${num(a.retention*100,0)}% объёма`};
    return values[rule.key] || '';
  }
  function drawChart(data,markers=[],ranges=null,structure=null) {
    const svg=$('price-chart');
    chartSnapshot={data,markers,ranges,structure};
    const layers=Object.fromEntries([...document.querySelectorAll('[data-structure-layer]')].map(n=>[n.dataset.structureLayer,n.checked]));
    if (!data.length) {svg.innerHTML=''; $('chart-empty').classList.remove('hidden'); return;}
    $('chart-empty').classList.add('hidden');
    const width=800,height=310,left=12,right=75,top=16,bottom=37;
    const all=data.flatMap(c=>[c.low,c.high,c.ema,c.vwap]).filter(Number.isFinite);
    const low=Math.min(...all),high=Math.max(...all),padding=Math.max((high-low)*.12,high*.001);
    const min=low-padding,max=high+padding;
    const x=i=>left+i*(width-left-right)/Math.max(1,data.length-1);
    const y=v=>top+(max-v)/(max-min)*(height-top-bottom);
    let out='';
    for(let i=0;i<5;i++) {
      const price=min+(max-min)*i/4,py=y(price);
      out+=`<line x1="${left}" y1="${py}" x2="${width-right+8}" y2="${py}" stroke="#25303d" stroke-dasharray="3 5"/><text x="${width-right+18}" y="${py+4}" fill="#71839b" font-size="10">${num(price)}</text>`;
    }
    const barWidth=Math.max(2,Math.min(6,(width-left-right)/data.length*.58));
    data.forEach((c,i)=>{
      const color=c.close>=c.open?'#55bfa0':'#b3757e';
      out+=`<line x1="${x(i)}" y1="${y(c.high)}" x2="${x(i)}" y2="${y(c.low)}" stroke="${color}"/><rect x="${x(i)-barWidth/2}" y="${Math.min(y(c.open),y(c.close))}" width="${barWidth}" height="${Math.max(1,Math.abs(y(c.open)-y(c.close)))}" fill="${color}" rx=".5"/>`;
    });
    for(const [key,color] of [['ema','#889ef9'],['vwap','#d4ab69']]) {
      let d='',lastDay=null;
      data.forEach((c,i)=>{
        if (!Number.isFinite(c[key])) {lastDay=null;return;}
        const currentDay=day(c.time);
        const start=key==='vwap' && currentDay!==lastDay;
        d+=`${i===0||start||lastDay===null?'M':'L'}${x(i).toFixed(2)},${y(c[key]).toFixed(2)} `;
        lastDay=currentDay;
      });
      out+=`<path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" opacity=".9"/>`;
    }
    for(const i of [0,Math.floor(data.length/3),Math.floor(data.length*2/3),data.length-1]) {
      const label=new Date(data[i].time).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'});
      out+=`<text x="${x(i)}" y="${height-10}" fill="#71839b" text-anchor="${i===0?'start':i===data.length-1?'end':'middle'}" font-size="10">${label}</text>`;
    }
    const indices=new Map(data.map((c,i)=>[Date.parse(c.time),i]));
    const seen=new Set();
    const twenty=ranges?.status==='ok' && ranges.entry_allowed===false ? ranges.levels?.find(r=>r.bars===20) : null;
    if(twenty && layers.levels)for(const [key,label,color] of [['support','Минимум 20','#55bfa0'],['resistance','Максимум 20','#d4ab69']]) {
      const level=twenty[key];
      if(!Number.isFinite(level) || level<min || level>max)continue;
      out+=`<g data-range-level="${key}"><title>${label} предыдущих 20 закрытых свечей по 15 минут: ${esc(money(level))}. Не сигнал покупки.</title><line x1="${left}" x2="${width-right}" y1="${y(level)}" y2="${y(level)}" stroke="${color}" stroke-dasharray="7 5" opacity=".65"/><text x="${left+5}" y="${y(level)-5}" fill="${color}" font-size="10">${label}</text></g>`;
    }
    for(const marker of markers) {
      const timestamp=Date.parse(marker?.time),i=indices.get(timestamp),def=patternDefinitions.find(item=>item.key===marker?.key);
      if(i===undefined || !def || !Number.isFinite(marker.price) || marker.price<min || marker.price>max || seen.has(`${timestamp}:${marker.key}`))continue;
      seen.add(`${timestamp}:${marker.key}`);
      const px=x(i),py=y(marker.price),label=marker.key==='reclaim'?'В':marker.key==='orb_retest'?'Р':'П';
      out+=`<g data-pattern-marker="${esc(marker.key)}"><title>${esc(def.name)} · ${esc(day(marker.time))}, ${esc(time(marker.time))} · ${esc(money(marker.price))}. ${mode==='demo'?'Вымышленные учебные данные. ':''}Наблюдение, не команда покупки.</title><path d="M ${px} ${py-6} L ${px+6} ${py} L ${px} ${py+6} L ${px-6} ${py} Z" fill="#131a23" stroke="#d4ab69" stroke-width="1.6"/><text x="${px}" y="${Math.max(top+9,py-10)}" text-anchor="middle" fill="#dfb76f" font-size="9">${label}</text></g>`;
    }
    if(window.VorbyStructure)out+=VorbyStructure.markup(data,structure,{x,y,min,max,top,bottom:height-bottom,end:width-right},layers);
    svg.innerHTML=out;
    $('chart-period').textContent=`${day(data[0].time)} — ${day(data[data.length-1].time)} · МСК`;
  }
  function renderSingle(payload) {
    if(payload.status!=='ok') {clearSingle(payload.message || 'Получаем историю и рыночные данные…'); return;}
    const a=payload.analysis,inst=payload.instrument;
    renderAi(a.ai);
    displayedUntil=['LONG','EXIT'].includes(a.decision)?Math.min(Date.parse(a.data_time)+45000,Date.parse(a.updated_at)+35000):0;
    $('load-status').textContent=payload.refreshing?'Обновляем котировки; предыдущая оценка была WAIT.':mode==='demo'?'Демонстрация на вымышленных данных.':a.blockers.length?'Данные получены; проверь ограничения ниже.':'';
    $('instrument-name').textContent=inst.name; $('ticker-label').textContent=`${inst.ticker} · ${inst.lot} шт. в лоте`;
    $('price').textContent=money(a.price);
    $('price-change').textContent=mode==='demo'?'Учебная котировка':'Середина лучшей покупки и продажи';
    $('decision').textContent=a.decision==='LONG'?'Кандидат покупки':a.decision==='EXIT'?'Проверь продажу':'Ждать';
    $('decision').classList.toggle('positive',a.decision==='LONG');
    $('decision').classList.toggle('negative',a.decision==='EXIT');
    $('score').innerHTML=`${a.score}<small>/100</small>`;
    $('score-fill').style.width=a.score+'%'; $('passed').textContent=`${a.passed} из 6 условий покупки`;
    $('decision-text').textContent=a.decision==='LONG'?'Все условия покупки совпали. Изучи сценарий и расходы перед собственным решением.':a.decision==='EXIT'?'Если акция уже есть: три признака ухудшения указывают, что стоит проверить выход из позиции.':'Для покупки должны совпасть все шесть условий на свежих данных.';
    $('blockers').innerHTML=[...a.blockers,...(a.decision==='EXIT'?a.exit_reasons:[])].map(x=>`<p>${esc(x)}</p>`).join('');
    $('data-time').textContent=time(a.data_time);
    const quality=a.data_quality;
    $('data-quality').textContent=mode==='live' && quality
      ? `${quality.book_consistency_confirmed===true?'Биржевой поток · согласованность последнего стакана подтверждена.':'Источник стакана не подтверждён.'} ${quality.action_blocked===true?'Действия заблокированы из-за качества данных.':''} ${quality.warning || ''}` : '';
    $('buy-share').textContent=a.buy_share===null?'—':num(a.buy_share*100,0)+'%';
    $('rvol').textContent=num(a.rvol)+'×'; $('vwap').textContent=money(a.vwap);
    $('spread').textContent=a.spread_bps===null?'—':num(a.spread_bps/100,3)+'%';
    $('rules').innerHTML=a.rules.map(r=>`<article class="panel rule ${r.pass===true?'pass':r.pass===null?'missing':'fail'}"><div class="rule-top"><span class="rule-icon">${r.pass===true?'✓':r.pass===null?'…':'−'}</span><h3>${esc(r.name)}</h3><span class="points">${r.pass===true?r.weight:0} / ${r.weight}</span></div><p>${esc(r.detail)}</p><div class="rule-value">${esc(ruleValue(r,a))}</div></article>`).join('');
    drawChart(a.chart,renderPatterns(a),a.range_levels,a.market_structure);
    renderStructure(a.market_structure);
    const ranges=a.range_levels;
    $('range-levels').textContent=ranges?.status==='ok' && ranges.entry_allowed===false
      ? `${mode==='demo'?'Учебные уровни. ':ranges.source_confirmed?'Биржевые свечи. ':'Источник свечей не подтверждён. '}${ranges.levels.map(r=>`${r.bars} свечей: минимум ${money(r.support)}, максимум ${money(r.resistance)} · ${r.above?'закрытие выше максимума':r.below?'закрытие ниже минимума':'цена внутри диапазона'}`).join(' | ')}. ${ranges.note}`
      : 'Нет свежих полных данных для уровней. Пробой не является командой покупки.';
    const max=Math.max(1,...a.book.bids.map(x=>x[1]),...a.book.asks.map(x=>x[1]));
    $('orderbook').innerHTML=Array.from({length:Math.max(a.book.bids.length,a.book.asks.length)},(_,i)=>{
      const bid=a.book.bids[i],ask=a.book.asks[i];
      return `<div class="book-row"><div class="book-side bid" style="--depth:${bid?bid[1]/max*100:0}%"><span>${bid?num(bid[1],0):'—'}</span><span>${bid?num(bid[0]):'—'}</span></div><div class="book-side ask" style="--depth:${ask?ask[1]/max*100:0}%"><span>${ask?num(ask[0]):'—'}</span><span>${ask?num(ask[1],0):'—'}</span></div></div>`;
    }).join('') || '<p class="muted">Нет доступного стакана</p>';
    $('book-samples').textContent=snapshots(a.book_samples);
    $('trade-plan').innerHTML=a.plan?`<div class="plan-row"><span>Ориентир входа · лучшая продажа</span><strong>${money(a.plan.entry)}</strong></div><div class="plan-row"><span>Стоп · ограничение убытка</span><strong class="negative">${money(a.plan.stop)}</strong></div><div class="plan-row"><span>Цель · 2 размера риска</span><strong class="positive">${money(a.plan.target)}</strong></div><p class="small muted">Расстояние до стопа: ${money(a.plan.risk_per_share)} на акцию, ${money(a.plan.risk_per_share*inst.lot)} на лот. Расходы и проскальзывание увеличат возможный убыток.</p>`:a.decision==='EXIT'?'<p class="muted">Это предупреждение для уже открытой позиции, не план короткой продажи. Проверь причину сигнала и принимай решение самостоятельно.</p>':'<p class="muted">План покупки появится, когда все шесть условий будут выполнены на свежих данных.</p>';
    currentPlan=a.decision==='LONG' && a.plan && (mode==='demo' || displayedUntil>Date.now())?a.plan:null;
    $('use-plan').disabled=!currentPlan;
    if(!$('risk-lot').value && Number.isInteger(inst.lot) && inst.lot>0)$('risk-lot').value=inst.lot;
    $('risk-plan-note').textContent=mode==='demo'?'Учебные цены вымышлены. Калькулятор показывает только устройство расчёта.':currentPlan?'Есть свежий кандидат покупки. Можно подставить его цены или ввести свои.':'Свежего сценария покупки сейчас нет. Введённые цены — твой самостоятельный расчёт.';
    if(currentPlan && !riskTouched && ['risk-entry','risk-stop','risk-target'].every(id=>!$(id).value))fillRiskPlan();
  }
  function resetRiskContext() {
    const context=`${mode}:${ticker}`;
    if(context===riskContext)return;
    riskContext=context;riskRevision++;riskTouched=false;currentPlan=null;
    ['risk-entry','risk-stop','risk-target','risk-lot'].forEach(id=>$(id).value='');
    $('risk-symbol').textContent=mode==='demo'?`${ticker} · учебный режим`:ticker;
    $('risk-result').innerHTML='<p class="research-meta">Введи цены сценария для этой акции и рассчитай допустимое количество лотов.</p>';
    $('risk-form').querySelector('button[type="submit"]').disabled=false;
  }
  function fillRiskPlan() {
    if(!currentPlan || (mode==='live' && displayedUntil<=Date.now()))return;
    for(const key of ['entry','stop','target'])$('risk-'+key).value=Number(currentPlan[key]).toFixed(9);
    riskRevision++;riskTouched=true;
    $('risk-form').querySelector('button[type="submit"]').disabled=false;
    $('risk-result').innerHTML='<p class="research-meta">Подставлены цены текущего сценария. Проверь их и нажми «Рассчитать размер позиции».</p>';
  }
  function renderRisk(data) {
    if(data.status!=='ok'){$('risk-result').textContent=data.message || 'Проверь введённые значения.';return;}
    const assumptions=Array.isArray(data.assumptions)?data.assumptions.join(' '):data.assumptions;
    $('risk-result').innerHTML=`<div class="research-verdict ${data.lots?'':'evidence-caution'}">${esc(data.message || (data.lots?'Количество рассчитано по двум ограничениям: риск и доля капитала.':'Даже один лот не помещается в заданные ограничения.'))}</div><div class="forward-stats risk-stats"><div><span>Количество · ${esc(ticker)}</span><strong>${num(data.lots,0)} лот. / ${num(data.shares,0)} шт.</strong></div><div><span>Стоимость позиции</span><strong>${money(data.position_cost)}</strong></div><div><span>Расчётный убыток при стопе</span><strong class="negative">${money(data.planned_loss)}</strong></div><div><span>Лимит убытка</span><strong>${money(data.risk_budget)}</strong></div><div><span>Прибыль при достижении цели</span><strong>${money(data.expected_target_profit)}</strong></div><div><span>Прибыль / риск после расходов</span><strong>${num(data.reward_risk)}×</strong></div></div><p class="research-meta">Расчётный риск: ${num(data.effective_risk_pct)}% капитала. ${esc(assumptions || '')} ${mode==='demo'?'Учебный режим: цены вымышлены.':''}</p>`;
  }
  function renderWatch() {
    $('total-count').textContent=symbols.length;
    const longs=symbols.filter(s=>results.get(s)?.analysis?.decision==='LONG' && results.get(s)?.status==='ok').length;
    const exits=symbols.filter(s=>results.get(s)?.analysis?.decision==='EXIT' && results.get(s)?.status==='ok').length;
    const waits=symbols.filter(s=>results.get(s)?.analysis?.decision==='WAIT' && results.get(s)?.status==='ok').length;
    $('long-count').textContent=longs; $('exit-count').textContent=exits; $('wait-count').textContent=waits;
    $('pending-count').textContent=symbols.length-longs-exits-waits;
    if(!symbols.length) {$('watch-grid').innerHTML='<div class="empty-state">Добавь первую акцию, чтобы начать наблюдение.<br><small>Например: SBER, GAZP, LKOH</small></div>';return;}
    $('watch-grid').innerHTML=symbols.map(s=>{
      const r=results.get(s),ok=r?.status==='ok',a=ok?r.analysis:null;
      const label=a?.decision==='LONG'?'Кандидат покупки':a?.decision==='EXIT'?'Проверь продажу':ok?'Ждать':r?.status==='error'?'Данные недоступны':'Получаем данные';
      const note=ok?(a.blockers[0] || (a.decision==='LONG'?'Все шесть условий покупки совпали. Открой подробный разбор.':a.decision==='EXIT'?'Если акция есть: '+a.exit_reasons.slice(0,2).join('; '):'Ожидаем: '+a.rules.filter(x=>x.pass!==true).map(x=>x.name.toLowerCase()).slice(0,2).join(', '))):(r?.message||'Загружаем данные Т-Банка…');
      return `<article class="panel watch-card"><button class="remove" data-remove="${esc(s)}" aria-label="Удалить ${esc(s)} из обзора">×</button><a href="/?ticker=${encodeURIComponent(s)}&mode=${mode}"><h3>${esc(s)}</h3><div class="company">${esc(ok?r.instrument.name:'Загрузка инструмента')}</div><div class="watch-price">${ok?money(a.price):'—'}</div><div class="watch-decision"><span class="${a?.decision==='LONG'?'positive':a?.decision==='EXIT'?'negative':'muted'}">${label}</span><span>${ok?a.score+' / 100 покупки':'—'}</span></div><div class="watch-checks">${Array.from({length:6},(_,i)=>`<span class="${a?.rules[i].pass===true?'yes':''}" title="${esc(a?.rules[i].name||'Ожидание')}"></span>`).join('')}</div><div class="watch-note">${esc(note)}</div>${aiWatchMarkup(a)}${patternWatchMarkup(a)}<div class="card-bottom"><span>${ok?time(a.data_time):'Ожидание ответа'}</span><span>Разобрать →</span></div></a></article>`;
    }).join('');
  }
  function renderForward(data) {
    if(data.status!=='ok') {$('forward-result').textContent=data.message || 'Не удалось прочитать наблюдения.';return;}
    const buy=data.buy_candidates,exit=data.exit_warnings;
    const excludedLegacy=Number.isInteger(data.excluded_legacy_observations)?data.excluded_legacy_observations:0;
    const excludedBook=Number.isInteger(data.excluded_invalid_book_observations)?data.excluded_invalid_book_observations:0;
    const ruleScope=`Проверяются текущие правила${data.strategy_version?' · '+data.strategy_version:''}${data.provider_version?' · биржевой поток':''}. Исключено: ${num(excludedLegacy,0)} наблюдений старых правил/источника и ${num(excludedBook,0)} с непроверенным стаканом.${data.completed?'':' Проверенных часовых результатов по текущим правилам пока нет; старые записи не считаются торговыми результатами.'}`;
    const note=buy.samples
      ? `После ${buy.samples} кандидатов покупки средний условный результат за час — ${num(buy.average_pct)}% после расходов; плюсовых случаев: ${buy.positive_or_correct}.`
      : `Зафиксировано ${data.candidate_events} появлений кандидата покупки; с проверенным часовым исходом пока нет. Баллы ниже показывают только условные покупки для исследования.`;
    const exitNote=exit.samples
      ? `После ${exit.samples} предупреждений о продаже цена за час снизилась в ${exit.positive_or_correct} случаях; среднее изменение: ${num(exit.average_pct)}%.`
      : `Зафиксировано ${data.exit_events} появлений предупреждения о продаже; с проверенным исходом пока нет.`;
    const table=data.completed?`<div class="table-scroll"><table><thead><tr><th>Баллы покупки</th><th>Сравнений</th><th>Средний результат через час</th><th>Плюсовых после расходов</th></tr></thead><tbody>${data.bands.map(b=>`<tr><td>${esc(b.name)}</td><td>${b.samples}</td><td class="${b.average_pct===null?'':b.average_pct>=0?'positive':'negative'}">${num(b.average_pct)}${b.average_pct===null?'':'%'}</td><td>${b.samples?`${b.positive_or_correct} из ${b.samples}`:'—'}</td></tr>`).join('')}</tbody></table></div>`:'';
    $('forward-result').innerHTML=`<p class="research-meta">${esc(ruleScope)}</p><div class="forward-stats"><div><span>Свежих наблюдений</span><strong>${data.observations}</strong></div><div><span>Часовых сравнений</span><strong>${data.completed}</strong></div><div><span>Пока без пары</span><strong>${data.without_pair}</strong></div></div><p class="research-meta">${esc(note)}</p><p class="research-meta">${esc(exitNote)}</p>${table}<p class="research-meta">Для сравнения баллов берём первое свежее наблюдение каждого часа; начало каждого нового сигнала считаем отдельно. Сравниваем с первой доступной котировкой через 60–70 минут. Условная покупка — по лучшей цене продажи, выход — по лучшей цене покупки. В расчёте по ${num(data.fee_bps/100)}% комиссии и ${num(data.slippage_bps/100)}% проскальзывания на каждую сторону. Пропущенные часы не достраиваем. Менее 30 сравнений — слишком мало для вывода, а соседние часы могут зависеть друг от друга.</p>`;
  }
  async function loadForward(myGeneration) {
    if(watch || mode!=='live')return;
    try {
      const data=await request(`/api/forward/?ticker=${encodeURIComponent(ticker)}&mode=live`);
      if(myGeneration===generation)renderForward(data);
    } catch {
      if(myGeneration===generation)$('forward-result').textContent='Не удалось загрузить локальную историю наблюдений.';
    }
  }
  const percent=value=>Number.isFinite(value)?`${num(value)}%`:'—';
  const signedClass=value=>Number.isFinite(value)?value>0?'positive':value<0?'negative':'':'';
  const dated=value=>value && Number.isFinite(Date.parse(value))?`${day(value)}, ${time(value)}`:'—';
  const patternVerdicts={unvalidated:'Польза не подтверждена',failed:'Проверка не пройдена',exploratory:'Плюс на этом участке · не подтверждено',candidate:'Кандидат для дальнейшей проверки',insufficient:'Мало данных'};
  function historicalFlowMarkup(flow) {
    if(mode!=='live' || !flow || typeof flow!=='object')return '';
    const heading='<h3>Дополнительная проверка: объём покупок по API</h3>';
    const message=esc(flow.message || 'Добавочная польза фильтра покупок пока не подтверждена.');
    if(flow.status!=='ok')return `<section class="pattern-flow-research">${heading}<p class="research-meta">${message}</p></section>`;
    const rows=Array.isArray(flow.patterns)?flow.patterns:[];
    if(!rows.length)return `<section class="pattern-flow-research">${heading}<p class="research-meta">Недостаточно результатов дополнительной проверки. Это не нулевая доходность.</p></section>`;
    const table=`<div class="table-scroll"><table class="pattern-research-table"><caption class="sr-only">Паттерны с фильтром объёма покупок. База рассчитана на том же источнике данных; разница в процентных пунктах.</caption><thead><tr><th>Паттерн</th><th>Сделок</th><th>После расходов</th><th>Расходы × 2</th><th>База на тех же данных</th><th>Разница, п.п.</th><th>Плюсовых</th><th>Просадка</th><th>Вывод</th></tr></thead><tbody>${rows.map(row=>{
      const active=row.trades>0;
      const returns=active?`<td class="${signedClass(row.return_pct)}">${percent(row.return_pct)}</td><td class="${signedClass(row.stress_return_pct)}">${percent(row.stress_return_pct)}</td>`:'<td>Нет сделок</td><td>—</td>';
      const deltaValue=Number.isFinite(row.difference_pp) && Math.abs(row.difference_pp)<.005?0:row.difference_pp;
      const delta=active && Number.isFinite(deltaValue)?`${deltaValue>0?'+':''}${num(deltaValue)} п.п.`:'—';
      const verdict=row.trades===0?patternVerdicts.insufficient:patternVerdicts[row.verdict] || 'Требует проверки';
      return `<tr><td>${esc(row.name || row.key)}</td><td>${num(row.trades,0)}</td>${returns}<td>${percent(row.baseline_return_pct)}</td><td>${delta}</td><td>${active?percent(row.win_rate_pct):'—'}</td><td>${active?percent(row.max_drawdown_pct):'—'}</td><td class="pattern-verdict">${esc(verdict)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
    return `<section class="pattern-flow-research">${heading}<p class="research-meta">${message}</p><p class="research-meta">База — тот же паттерн с объёмом ≥ 1,3, без фильтра доли покупок. Сравнение проводится на одном источнике свечей. Разница указана в процентных пунктах, не как обещание дополнительного заработка. Числа предыдущей таблицы получены из другого набора и напрямую с этой базой не сравниваются.</p>${table}<p class="research-meta">${esc(flow.note || 'Фильтр объёма покупок не восстанавливает прошлый стакан и не доказывает прибыльность следующей сделки.')}</p>${Number.isFinite(flow.coverage_pct)?`<p class="research-meta">Покрытие данных: ${percent(flow.coverage_pct)}.</p>`:''}${flow.at?`<p class="research-meta">Дополнительный отчёт: ${esc(dated(flow.at))}</p>`:''}</section>`;
  }
  function candleResearchMarkup(data) {
    if(mode!=='live' || !data)return '';
    const intro=`<h3>Дополнительные свечные паттерны</h3><p class="research-meta">${esc(data.message)}</p>`;
    if(data.status!=='ok' || data.entry_allowed!==false)return intro;
    const rows=Array.isArray(data.patterns)?data.patterns:[];
    return `${intro}<div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Форма</th><th>Сделок</th><th>После расходов</th><th>Расходы × 2</th><th>Вывод</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.name)}</td><td>${num(row.trades,0)}</td><td class="${signedClass(row.return_pct)}">${row.trades>0?percent(row.return_pct):'Нет сделок'}</td><td>${row.trades>0?percent(row.stress_return_pct):'—'}</td><td>${esc(patternVerdicts[row.verdict] || 'Не подтверждено')}</td></tr>`).join('')}</tbody></table></div><p class="research-meta">${esc(data.note)}</p>`;
  }
  function pressureResearchMarkup(data) {
    if(mode!=='live' || !data)return '';
    const intro=`<h3>Проверка записанного стакана и сделок</h3><p class="research-meta">${esc(data.message)}</p>`;
    if(data.status!=='ok' || data.entry_allowed!==false)return intro;
    const rows=Array.isArray(data.variants)?data.variants:[];
    return `${intro}<div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Идея</th><th>Проверено</th><th>Неизвестный исход</th><th>Средняя сделка</th><th>Расходы × 2</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.name)}</td><td>${num(row.verified,0)}</td><td>${num(row.unverified,0)}</td><td class="${signedClass(row.mean_trade_return_pct)}">${percent(row.mean_trade_return_pct)}</td><td>${percent(row.mean_stress_trade_return_pct)}</td></tr>`).join('')}</tbody></table></div><p class="research-meta">${esc(data.note)}</p>`;
  }
  function rangeResearchMarkup(data) {
    if(mode!=='live' || !data)return '';
    const intro=`<h3>${esc(data.title || 'Максимумы, минимумы и защитный выход')}</h3><p class="research-meta">${esc(data.message)}</p>`;
    if(data.status!=='ok' || data.entry_allowed!==false)return intro;
    const rangePercent=v=>Number.isFinite(v)?`${v>0?'+':''}${num(v,3)}%`:'—';
    const cell=m=>m?.trades>0?`${rangePercent(m.return_pct)} · сделок: ${num(m.trades,0)}<br><small>Расходы ×2: ${rangePercent(m.stress_return_pct)} · непроверенных: ${num(m.unverified_trades,0)}</small>`:'Нет сделок';
    const table=(group,label)=>`<h4>${esc(label)}</h4><div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Идея</th><th>2023 — июнь 2026</th><th>Июль — август</th><th>Сентябрь — 2 октября</th></tr></thead><tbody>${data.variants.map(r=>`<tr><td>${esc(r.name)}</td>${['development','selection','recent'].map(p=>`<td>${cell(r[group][p])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    return `${intro}${table('periods',data.ticker)}${table('group_periods','Среднее семи отдельных счетов; число сделок суммарное')}<p class="research-meta">${esc(data.note)}</p>`;
  }
  function jointResearchMarkup(data) {
    if(mode!=='live' || !data)return '';
    const intro=`<h3>${esc(data.title)}</h3><p class="research-meta">${esc(data.message)}</p>`;
    if(data.status!=='ok' || data.entry_allowed!==false)return intro;
    const rows=Array.isArray(data.variants)?data.variants:[];
    const cell=m=>m?.verified>0?percent(m.mean_trade_return_pct):'Недостаточно данных';
    const table=(group,label)=>`<h4>${esc(label)}</h4><div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Условия</th><th>Сценариев</th><th>Проверено</th><th>Неизвестный исход</th><th>Средняя сделка</th><th>Расходы ×2</th></tr></thead><tbody>${rows.map(r=>{const m=r[group];return `<tr><td>${esc(r.name)}</td><td>${num(m.signals,0)}</td><td>${num(m.verified,0)}</td><td>${num(m.unverified,0)}</td><td>${cell(m)}</td><td>${m.verified>0?percent(m.mean_stress_trade_return_pct):'—'}</td></tr>`;}).join('')}</tbody></table></div>`;
    const c=data.coverage;
    const coverage=c?`<p class="research-meta">Архив ${esc(data.ticker)}: стаканов ${num(c.books,0)}, сделок ${num(c.trades,0)}. Непригодных стаканов: ${num(c.diagnostics?.bad_books,0)}; задержанных или непроверенных сделок: ${num(c.diagnostics?.invalid_trades,0)}. Это диагностика данных, не число проигрышных сделок.</p>`:'';
    return `${intro}${table('stock',data.ticker)}${table('group','Все три акции: средняя условная сделка, не счёт')}${coverage}<p class="research-meta">${esc(data.note)}</p>${data.forward?`<p class="research-meta">Будущая проверка: ${esc(dated(data.forward.start))} — ${esc(dated(data.forward.end))}. ${esc(data.forward.message)}</p>`:''}${data.at?`<p class="research-meta">Отчёт: ${esc(dated(data.at))}</p>`:''}${data.new_observations?jointResearchMarkup(data.new_observations):''}`;
  }
  function monthlyResearchMarkup(data) {
    if(mode!=='live' || !data)return '';
    const intro=`<h3>${esc(data.title)}</h3><p class="research-meta">${esc(data.message)}</p>`;
    if(data.status!=='ok' || data.entry_allowed!==false)return intro;
    const value=m=>m?.trades>0?`${num(m.return_pct,3)}%${m.verified?'':' · данные требуют проверки'}`:'Нет сделок';
    const rows=data.variants.map(r=>`<tr><td>${esc(r.name)}</td><td>${value(r.full)}</td><td>${value(r.double_cost)}</td><td>${value(r.selection)}</td><td>${value(r.recent)}</td><td>${num(r.full.trades,0)}</td></tr>`).join('');
    const rolling=data.rolling?.base;
    return `${intro}<p class="research-meta">Расчётов: ${num(data.simulations,0)} · Прошли отбор: ${num(data.passed,0)} из 12</p><div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Выбор акций</th><th>Весь период</th><th>Расходы ×2</th><th>Январь — июнь 2026</th><th>Июль — октябрь</th><th>Сделок за весь период</th></tr></thead><tbody>${rows}</tbody></table></div>${rolling?`<p class="research-meta">Выбор стратегии по предшествующим данным: ${num(rolling.return_pct,3)}% · сделок: ${num(rolling.trades,0)}. Это последовательная проверка на уже изученной истории.</p>`:''}<p class="research-meta">${esc(data.note)}</p><p class="research-meta">Отчёт: ${esc(dated(data.at))}</p>`;
  }
  function openingActivityMarkup(data) {
    if(mode!=='live' || !data)return '';
    const intro=`<h3>${esc(data.title)}</h3><p class="research-meta">${esc(data.message)}</p>`;
    if(data.status!=='ok' || data.entry_allowed!==false)return intro;
    const value=m=>m?.trades>0?`${num(m.return_pct,3)}% · сделок: ${num(m.trades,0)}${m.unverified_trades?` · непроверенных: ${num(m.unverified_trades,0)}`:''}`:'Нет сделок';
    const cell=scenarios=>`${value(scenarios.base)}<br><small>Расходы ×2: ${value(scenarios.double_cost)}<br>Вход позже: ${value(scenarios.delayed)}</small>`;
    const table=(scope,label)=>`<h4>${esc(label)}</h4><div class="table-scroll"><table class="pattern-research-table"><thead><tr><th>Алгоритм</th><th>Март — июль</th><th>Август</th><th>Сентябрь — 2 октября</th><th>3–12 октября</th></tr></thead><tbody>${data.variants.map(r=>`<tr><td>${esc(r.name)}</td>${['development','selection','recent','additional'].map(p=>`<td>${cell(r[scope][p])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    return `${intro}<p class="research-meta">Расчётов: ${num(data.simulations,0)} · Прошли отбор: ${num(data.passed,0)} из 3 · Дополнительная история: ${num(data.additional_accounts,0)} из 7 акций</p>${table('stock',data.ticker)}${table('group','Среднее семи отдельных счетов')}<p class="research-meta">${esc(data.note)}</p><p class="research-meta">Отчёт: ${esc(dated(data.at))}</p>`;
  }
  async function loadPatternResearch() {
    const revision=++patternResearchRevision,myGeneration=generation,button=$('pattern-research-load');
    button.disabled=true;
    $('pattern-research-result').textContent='Читаем сохранённую проверку паттернов…';
    try {
      const data=await request(`/api/pattern-research/?ticker=${encodeURIComponent(ticker)}&mode=${mode}`,10000);
      if(revision!==patternResearchRevision || myGeneration!==generation)return;
      if(data.status!=='ok'){$('pattern-research-result').innerHTML=`<p class="research-meta">${esc(data.message || 'Для этой акции результатов проверки пока нет.')}</p>${historicalFlowMarkup(data.historical_flow)}${candleResearchMarkup(data.candlestick_v3)}${pressureResearchMarkup(data.stream_pressure)}${rangeResearchMarkup(data.range_swing)}${rangeResearchMarkup(data.trader_tactics)}${jointResearchMarkup(data.joint_factors)}${monthlyResearchMarkup(data.monthly_momentum)}${openingActivityMarkup(data.opening_activity)}`;return;}
      const verdicts=patternVerdicts;
      const rows=Array.isArray(data.patterns)?data.patterns:[];
      const table=rows.length?`<div class="table-scroll"><table class="pattern-research-table"><caption class="sr-only">Историческая проверка паттернов. Результаты расчётные, не обещание дохода.</caption><thead><tr><th>Паттерн</th><th>Сделок</th><th>После расходов</th><th>Расходы × 2</th><th>Плюсовых</th><th>Просадка</th><th>Вывод</th></tr></thead><tbody>${rows.map(row=>{
        const metrics=row.trades>0?`<td class="${signedClass(row.return_pct)}">${percent(row.return_pct)}</td><td class="${signedClass(row.stress_return_pct)}">${percent(row.stress_return_pct)}</td><td>${percent(row.win_rate_pct)}</td><td>${percent(row.max_drawdown_pct)}</td>`:'<td>Нет сделок</td><td>—</td><td>—</td><td>—</td>';
        const verdict=row.trades===0?verdicts.insufficient:verdicts[row.verdict] || row.verdict || 'Требует проверки';
        return `<tr><td>${esc(row.name || patternDefinitions.find(def=>def.key===row.key)?.name || row.key)}</td><td>${num(row.trades,0)}</td>${metrics}<td class="pattern-verdict">${esc(verdict)}</td></tr>`;
      }).join('')}</tbody></table></div>`:'';
      $('pattern-research-result').innerHTML=`<p class="research-meta">${esc(data.message || 'Это проверка прошлых свечей, не разрешение на покупку.')}</p>${table}<p class="research-meta">${esc(data.note || 'Историческая прибыль не гарантирует результат следующей сделки. Стакан и лента не восстановлены по свечам.')}</p>${data.at?`<p class="research-meta">Отчёт: ${esc(dated(data.at))}</p>`:''}${historicalFlowMarkup(data.historical_flow)}${candleResearchMarkup(data.candlestick_v3)}${pressureResearchMarkup(data.stream_pressure)}${rangeResearchMarkup(data.range_swing)}${rangeResearchMarkup(data.trader_tactics)}${jointResearchMarkup(data.joint_factors)}${monthlyResearchMarkup(data.monthly_momentum)}${openingActivityMarkup(data.opening_activity)}`;
    } catch {
      if(revision===patternResearchRevision && myGeneration===generation)$('pattern-research-result').textContent='Проверка паттернов сейчас недоступна. Текущий график и оценка акции продолжают обновляться.';
    } finally {
      if(revision===patternResearchRevision && myGeneration===generation)button.disabled=false;
    }
  }
  function renderPaper(data) {
    if(data.status!=='ok'){$('paper-result').textContent=data.message || 'Журнал пока недоступен.';return;}
    const s=data.summary,c=data.coverage || {},trades=data.trades || [];
    const statusNames={closed:'Закрыта условно',open:'Открыта условно',pending:'Ждём котировку входа',unverified:'Исход не подтверждён'};
    const reasonNames={stop:'Стоп',target:'Цель',exit:'Предупреждение о продаже',exit_signal:'Предупреждение о продаже',data_gap:'Пробел в наблюдениях',stale:'Котировка устарела',gap:'Пробел в наблюдениях',no_entry:'Нет подходящей котировки входа'};
    const evidence=s.unverified?`${s.unverified} сценариев с неподтверждённым исходом не включены в доходность. Пропуски наблюдений могут заметно изменить результат.`:s.closed<30?'Закрытых условных сделок пока мало для вывода о пользе сигналов.':'Это результат наблюдаемых котировок. Исполнение реальных сделок может отличаться.';
    const table=trades.length?`<div class="table-scroll"><table><thead><tr><th>Сигнал · МСК</th><th>Состояние</th><th>Вход / выход, ₽</th><th>Стоп / цель, ₽</th><th>Результат после расходов</th><th>Причина</th></tr></thead><tbody>${trades.map(t=>`<tr><td>${esc(dated(t.signal_at))}<small class="cell-detail">Вход: ${esc(dated(t.entry_at))}<br>Выход: ${esc(dated(t.exit_at))}</small></td><td><span class="trade-state ${t.status==='unverified'?'unverified':''}">${esc(statusNames[t.status] || t.status)}</span></td><td>${money(t.entry)} / ${money(t.exit)}</td><td>${money(t.stop)} / ${money(t.target)}</td><td class="${t.status==='closed'?signedClass(t.net_pct):''}">${t.status==='closed'?percent(t.net_pct):'—'}</td><td>${esc(t.reason_text || reasonNames[t.reason] || t.reason || 'Ожидаем продолжение')}</td></tr>`).join('')}</tbody></table></div>`:'<div class="journal-empty">В журнале пока нет новых кандидатов покупки с зафиксированным планом и последующей свежей котировкой. Старые сигналы без сохранённого плана не превращаются задним числом в сделки.</div>';
    $('paper-result').innerHTML=`<div class="forward-stats paper-stats"><div><span>Закрытых сценариев</span><strong>${num(s.closed,0)}</strong></div><div><span>Открытых / ждут входа</span><strong>${num(s.open,0)} / ${num(s.pending || 0,0)}</strong></div><div><span>Исход не подтверждён</span><strong class="${s.unverified?'evidence-text':''}">${num(s.unverified,0)}</strong></div></div><div class="research-verdict evidence-caution">${esc(evidence)}</div>${s.closed?`<div class="forward-stats paper-stats"><div><span>Накопленный условный результат</span><strong class="${signedClass(s.net_return_pct)}">${percent(s.net_return_pct)}</strong></div><div><span>Плюсовых сделок</span><strong>${percent(s.win_rate)}</strong></div><div><span>Максимальная просадка</span><strong>${percent(s.max_drawdown_pct)}</strong></div></div>`:''}${table}<p class="research-meta">Наблюдений: ${num(c.observations,0)} · ${c.first_at?`${esc(dated(c.first_at))} — ${esc(dated(c.last_at))}`:'свежих записей ещё нет'}. Расходы на каждую сторону: комиссия ${num(Number($('fee').value))}%, проскальзывание ${num(Number($('slippage').value))}%.</p><details class="research-meta"><summary>Как устроен журнал</summary><p>${esc(data.method || '')}</p><p>${esc(data.limitation || '')}</p><p>Накопленный результат последовательно перемножает доходности закрытых условных сделок; это не доходность твоего портфеля. Открытые и неподтверждённые сценарии в него не входят. Калькулятор размера позиции выше не меняет расчёт журнала.</p></details>`;
  }
  async function loadPaper(myGeneration) {
    if(watch || mode!=='live')return;
    const revision=++paperRevision,fee=Number($('fee').value),slip=Number($('slippage').value);
    if(!$('fee').validity.valid || !$('slippage').validity.valid){$('paper-result').textContent='Укажи корректные расходы в настройках проверки ниже.';return;}
    try {
      const data=await request(`/api/paper/?ticker=${encodeURIComponent(ticker)}&mode=live&fee=${fee*100}&slip=${slip*100}`);
      if(myGeneration===generation && revision===paperRevision)renderPaper(data);
    } catch {
      if(myGeneration===generation && revision===paperRevision)$('paper-result').textContent='Не удалось прочитать журнал. Повторим при обновлении данных.';
    }
  }
  function validationMarkup(data) {
    const v=data.validation;
    if(!v || v.status!=='ok')return `<div class="research-verdict evidence-caution">${esc(v?.message || 'Для проверки в четырёх периодах пока недостаточно истории. Требуется не менее 500 закрытых свечей.')}</div>`;
    const labels={needs_evidence:'Мало сделок',negative:'Убыточно на проверке',mixed:'Результат неустойчив',positive_unproven:'Есть плюс · требует наблюдения'};
    const windows=v.windows || [],strategies=v.strategies || [];
    const description=mode==='demo'?'Учебные данные вымышлены. Таблица объясняет расчёт и не оценивает доходность на бирже.':'Плюс в одном периоде может быть случайностью. Сравни все периоды, количество сделок и результат при удвоенных расходах.';
    return `<div class="research-verdict">${description}</div><div class="validation-periods">${windows.map((w,i)=>`<div><span>Период ${i+1}</span><strong>${esc(day(w.start))} — ${esc(day(w.end))}</strong><small>Купить и держать: ${percent(w.buy_hold_pct)}</small></div>`).join('')}</div><div class="table-scroll"><table class="validation-table"><thead><tr><th>Правило</th>${windows.map((w,i)=>`<th>Период ${i+1}<small class="cell-detail">Результат / сделок</small></th>`).join('')}<th>Всего</th><th>Расходы × 2</th><th>Просадка</th><th>Вывод</th></tr></thead><tbody>${strategies.map(s=>`<tr><td>${esc(s.name)}</td>${(s.windows || []).map(w=>`<td class="${signedClass(w.return_pct)}">${percent(w.return_pct)}<small class="cell-detail">${num(w.trades,0)} сделок</small></td>`).join('')}<td class="${signedClass(s.return_pct)}">${percent(s.return_pct)}<small class="cell-detail">${num(s.total_trades,0)} сделок</small></td><td class="${signedClass(s.stress_return_pct)}">${percent(s.stress_return_pct)}</td><td>${percent(s.max_drawdown_pct)}</td><td><span class="validation-label">${esc(labels[s.verdict] || 'Нужна дальнейшая проверка')}</span><small class="cell-detail">В плюсе ${num(s.positive_windows,0)} из ${num(s.total_windows,0)} периодов</small></td></tr>`).join('')}</tbody></table></div><p class="research-meta">${esc(v.method || '')} «Расходы × 2» — те же правила с удвоенной комиссией и проскальзыванием. Без сделок результат — 0%; это не подтверждение качества стратегии.</p>`;
  }
  function renderResearch(data) {
    const source=mode==='demo'?'Вымышленные учебные свечи':data.history_source==='archive'?'Сохранённая длинная история (ранее загруженные данные)':'Текущая загруженная история';
    $('research-result').innerHTML=`<p class="research-meta">${source} · ${data.count} свечей · ${esc(day(data.start))} — ${esc(day(data.end))}${data.dataset_saved_at?` · сохранено ${esc(dated(data.dataset_saved_at))}`:''}. Комиссия ${num(data.fee_bps/100)}%, проскальзывание ${num(data.slippage_bps/100)}% на каждую сторону.</p>${validationMarkup(data)}<details class="research-meta"><summary>Дополнительное сравнение: первые 70% и последние 30% истории</summary><p>Эта проверка использует ту же историю и не является независимым подтверждением таблицы выше. «Купить и держать» на последних 30%: ${percent(data.buy_hold_pct)}; без сделок: 0%.</p><div class="table-scroll"><table><thead><tr><th>Правило</th><th>Первые 70%</th><th>Последние 30%</th><th>Сделок на проверке</th><th>Плюсовых</th><th>Просадка</th></tr></thead><tbody>${data.strategies.map(s=>`<tr><td>${esc(s.name)}</td><td>${percent(s.development.return_pct)}</td><td class="${signedClass(s.holdout.return_pct)}">${percent(s.holdout.return_pct)}</td><td>${num(s.holdout.trades,0)}${s.holdout.trades<30?' · мало данных':''}</td><td>${percent(s.holdout.win_rate)}</td><td>${percent(s.holdout.max_drawdown_pct)}</td></tr>`).join('')}</tbody></table></div></details><details class="research-meta"><summary>Как считаем и что означают результаты</summary><p>${esc(data.method)}</p><p>${esc(data.limitation)}</p><p>Просадка — самое сильное падение расчётного капитала от предыдущего максимума. «Плюсовых» — доля сделок с положительным результатом после расходов. Правила не обучаются на следующих периодах, но уже просмотренная история не становится новым независимым испытанием.</p></details>`;
  }
  async function poll(myGeneration) {
    if(myGeneration!==generation) return;
    if(document.hidden) {timer=setTimeout(()=>poll(myGeneration),5000);return;}
    let loading=false;
    const queue=watch?[...symbols]:[ticker];
    async function worker() {
      while(queue.length && myGeneration===generation) {
        const symbol=queue.shift();
        let payload;
        try {payload=await request(`/api/signals/?ticker=${encodeURIComponent(symbol)}&mode=${mode}`);} catch(err) {payload={status:'error',message:err.name==='AbortError'?'Сервер отвечает слишком долго. Повторим запрос.':'Нет связи с сервером. Живой сигнал заблокирован.'};}
        if(myGeneration!==generation) return;
        loading ||= payload.status==='loading' || payload.refreshing===true;
        if(watch) {results.set(symbol,payload);renderWatch();} else renderSingle(payload);
      }
    }
    await Promise.all(Array.from({length:Math.min(3,Math.max(queue.length,1))},worker));
    if(myGeneration===generation) {
      if(!watch && !loading)loadPaper(myGeneration);
      timer=setTimeout(()=>poll(myGeneration),loading?4000:30000);
    }
  }
  function restart() {
    clearTimeout(timer);generation++;researchGeneration++;paperRevision++;patternResearchRevision++;
    banner();results.clear();
    currentAi=null;
    loadAiModel(generation);
    if(watch)$('ai-result').innerHTML='<p class="muted">Оценки ИИ появятся в карточках акций ниже. Открой акцию для объяснения признаков.</p>';
    if(watch) renderWatch(); else {
      $('symbol').value=ticker;resetRiskContext();clearSingle();
      $('forward-result').innerHTML=mode==='demo'?'<p class="muted">В учебном режиме исходы вымышлены. Здесь появятся только наблюдения реального рынка.</p>':'<p class="muted">Читаем сохранённые наблюдения…</p>';
      $('research-result').innerHTML='<p class="muted">Нажми «Проверить на истории» после загрузки акции.</p>';
      $('research-history').value=mode==='demo'?'current':researchHistory;
      $('research-history').disabled=mode==='demo';
      $('paper-result').innerHTML=mode==='demo'?'<p class="muted">В учебном режиме журнал недоступен. Здесь учитываются только сохранённые живые котировки.</p>':'<p class="muted">Читаем журнал условных сделок…</p>';
      $('pattern-research-result').innerHTML=mode==='demo'?'<p class="research-meta">Цены вымышлены. Учебные результаты не доказывают прибыльность на бирже.</p>':'<p class="research-meta">Нажми «Посмотреть результаты проверки», чтобы прочитать сохранённый отчёт для этой акции.</p>';
      $('pattern-research-load').disabled=false;
      document.querySelector('#research-form button').disabled=false;
      loadForward(generation);
      loadPaper(generation);
    }
    poll(generation);
  }
  document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{
    mode=button.dataset.mode;
    history.replaceState(null,'',`${location.pathname}?mode=${mode}${watch?'':'&ticker='+encodeURIComponent(ticker)}`);
    restart();
  }));
  $('refresh').addEventListener('click',restart);
  if(watch) {
    InstrumentSearch.attach({
      input:$('tickers'),dropdown:$('tickers-suggestions'),sharesOnly:true,
      getQuery:value=>value.split(/[\s,;]+/).pop(),
      isAdded:item=>symbols.includes(item.ticker),
      onSelect:item=>{
        const value=$('tickers').value;
        const previous=value.replace(/[^\s,;]*$/, '').trim();
        $('tickers').value=`${previous ? previous.replace(/[\s,;]+$/, '')+', ' : ''}${item.ticker}, `;
        $('watch-message').textContent=`Выбрана ${item.ticker} — ${item.name}. Нажми «Добавить в обзор».`;
        $('tickers').focus();
      }
    });
    $('watch-form').addEventListener('submit',async event=>{
      event.preventDefault();const incoming=$('tickers').value.toUpperCase().split(/[\s,;]+/).filter(Boolean);
      if(!incoming.length) {$('watch-message').textContent='Введи хотя бы один тикер.';return;}
      const bad=incoming.filter(s=>!validTicker(s));
      if(bad.length) {$('watch-message').textContent='Не удалось прочитать: '+bad.join(', ');return;}
      const next=[...new Set([...symbols,...incoming])];
      if(next.length>20) {$('watch-message').textContent='В первой версии можно наблюдать до 20 акций. Удали ненужные из обзора.';return;}
      const button=event.submitter || $('watch-form').querySelector('button[type="submit"]');
      button.disabled=true;$('watch-message').textContent='Проверяем тикеры в каталоге акций…';
      try {
        const checked=await Promise.all(incoming.map(symbol=>InstrumentSearch.exact(symbol)));
        const unknown=incoming.filter((symbol,index)=>!checked[index]);
        if(unknown.length) {$('watch-message').textContent=`Не найдены акции: ${unknown.join(', ')}. Выбери тикер из подсказок.`;return;}
      } catch {$('watch-message').textContent='Не удалось проверить тикеры. Попробуй ещё раз, когда поиск заработает.';return;}
      finally {button.disabled=false;}
      symbols=next;save('vorby.watchlist',symbols);$('tickers').value='';$('watch-message').textContent='Список сохранён в этом браузере.';restart();
    });
    $('watch-grid').addEventListener('click',event=>{
      const button=event.target.closest('[data-remove]');if(!button)return;
      symbols=symbols.filter(s=>s!==button.dataset.remove);save('vorby.watchlist',symbols);restart();
    });
  } else {
    $('pattern-research-load').addEventListener('click',loadPatternResearch);
    $('risk-percent').max='5';
    $('risk-allocation').min='1';
    $('use-plan').addEventListener('click',fillRiskPlan);
    $('risk-form').addEventListener('input',()=>{
      riskRevision++;riskTouched=true;
      $('risk-form').querySelector('button[type="submit"]').disabled=false;
      $('risk-result').innerHTML='<p class="research-meta">Параметры изменены. Нажми «Рассчитать размер позиции», чтобы обновить результат.</p>';
    });
    $('risk-form').addEventListener('submit',async event=>{
      event.preventDefault();const revision=++riskRevision,context=riskContext,button=event.submitter || $('risk-form').querySelector('button[type="submit"]');
      const entry=Number($('risk-entry').value),stop=Number($('risk-stop').value),target=Number($('risk-target').value);
      if(!(stop<entry && entry<target)){$('risk-result').textContent='Для сценария покупки стоп должен быть ниже цены покупки, а цель — выше.';return;}
      const params=new URLSearchParams({capital:$('risk-capital').value,risk_pct:$('risk-percent').value,allocation_pct:$('risk-allocation').value,entry,stop,target,lot:$('risk-lot').value,fee:Number($('risk-fee').value)*100,slip:Number($('risk-slippage').value)*100});
      button.disabled=true;$('risk-result').textContent='Считаем количество лотов, расходы и риск…';
      try {
        const data=await request(`/api/risk/?${params}`);
        if(revision===riskRevision && context===riskContext)renderRisk(data);
      } catch(error) {
        if(revision===riskRevision && context===riskContext)$('risk-result').textContent=error.name==='AbortError'?'Расчёт занял слишком много времени. Попробуй ещё раз.':error.message;
      } finally {if(revision===riskRevision && context===riskContext)button.disabled=false;}
    });
    for(const id of ['fee','slippage'])$(id).addEventListener('input',()=>{
      researchGeneration++;paperRevision++;
      $('research-form').querySelector('button').disabled=false;
      $('research-result').textContent='Расходы изменены. Запусти историческую проверку повторно.';
      $('paper-result').textContent=mode==='demo'?'В учебном режиме журнал недоступен.':'Обновляем журнал с новыми расходами…';
      loadPaper(generation);
    });
    $('research-history').addEventListener('change',()=>{
      researchHistory=$('research-history').value;save('vorby.researchHistory',researchHistory);researchGeneration++;
      $('research-form').querySelector('button').disabled=false;
      $('research-result').textContent='Источник данных изменён. Запусти историческую проверку.';
    });
    InstrumentSearch.attach({
      input:$('symbol'),dropdown:$('symbol-suggestions'),sharesOnly:true,
      onSelect:item=>{$('symbol').value=item.ticker;$('load-status').textContent=`Выбрана ${item.ticker} — ${item.name}. Нажми «Анализировать».`;}
    });
    $('symbol-form').addEventListener('submit',async event=>{
      event.preventDefault();const value=$('symbol').value.trim().toUpperCase();
      if(!validTicker(value)) {$('load-status').textContent='Введи биржевой тикер латиницей, например SBER.';return;}
      try {
        if(!await InstrumentSearch.exact(value)) {$('load-status').textContent=`Акция ${value} не найдена. Выбери её из подсказок.`;return;}
      } catch {$('load-status').textContent='Не удалось проверить тикер. Попробуй ещё раз, когда поиск заработает.';return;}
      ticker=value;save('vorby.symbol',ticker);history.replaceState(null,'',`/?ticker=${ticker}&mode=${mode}`);restart();
    });
    $('research-form').addEventListener('submit',async event=>{
      event.preventDefault();const revision=++researchGeneration,button=event.submitter || document.querySelector('#research-form button');
      button.disabled=true;$('research-result').textContent='Считаем сделки и расходы…';
      try {
        const data=await request(`/api/research/?ticker=${encodeURIComponent(ticker)}&mode=${mode}&history=${mode==='demo'?'current':researchHistory}&fee=${Number($('fee').value)*100}&slip=${Number($('slippage').value)*100}`,60000);
        if(revision!==researchGeneration)return;
        if(data.status!=='ok') {$('research-result').textContent=data.message;return;}
        renderResearch(data);
      } catch {if(revision===researchGeneration)$('research-result').textContent='Не удалось выполнить проверку. Убедись, что сервер доступен.';}
      finally {if(revision===researchGeneration)button.disabled=false;}
    });
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)restart();});
  document.querySelectorAll('[data-structure-layer]').forEach(input=>input.addEventListener('change',()=>{
    if(chartSnapshot){const c=chartSnapshot;drawChart(c.data,c.markers,c.ranges,c.structure);}
  }));
  setInterval(()=>{if(!watch && mode==='live' && !document.hidden)loadForward(generation);},60000);
  // Clear actionable prices even if a slow request or a disconnected server delays polling.
  setInterval(()=>{
    if(mode!=='live')return;
    if(!watch && currentAi?.status==='ok' && aiState(currentAi)?.status!=='ok')renderAi(aiState(currentAi));
    if(!watch) {
      if(structureWasFresh && chartSnapshot?.structure && structureState(chartSnapshot.structure)?.status==='stale')renderStructure(chartSnapshot.structure);
      if(displayedUntil && Date.now()>displayedUntil)clearSingle('Котировки устарели. Ожидаем свежие данные.');
      if(patternUntil && Date.now()>patternUntil)clearPatterns('Оценка паттернов устарела. Отметки скрыты до свежих данных.');
    } else {
      let changed=false;
      for(const [symbol,payload] of results) {
        const a=payload.analysis;
        if(payload.status==='ok' && a?.ai?.status==='ok' && aiState(a.ai)?.status!=='ok'){
          results.set(symbol,{...payload,analysis:{...a,ai:aiState(a.ai)}});changed=true;
        }
        if(payload.status==='ok' && ['LONG','EXIT'].includes(a?.decision) && Date.now()>Math.min(Date.parse(a.data_time)+45000,Date.parse(a.updated_at)+35000)) {
          results.set(symbol,{status:'loading',message:'Котировки устарели. Ожидаем обновление.'});changed=true;
        } else if(payload.status==='ok' && a?.patterns?.status==='ok' && patternState(a).status==='stale') {
          results.set(symbol,{...payload,analysis:{...a,patterns:{...a.patterns,status:'stale',markers:[],message:'Оценка устарела. Ждём обновления.'}}});changed=true;
        }
      }
      if(changed)renderWatch();
    }
  },1000);
  function clock() {$('clock').textContent=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'})+' МСК';}
  clock();setInterval(clock,30000);restart();
})();
