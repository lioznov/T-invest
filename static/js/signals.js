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
  let generation=0, timer, researchGeneration=0, displayedUntil=0, paperRevision=0, riskRevision=0;
  let riskContext='', riskTouched=false, currentPlan=null;
  let researchHistory=read('vorby.researchHistory','archive')==='current'?'current':'archive';
  const results=new Map();

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
  function banner() {
    document.querySelectorAll('[data-mode]').forEach(button=>button.classList.toggle('selected',button.dataset.mode===mode));
    $('mode-banner').classList.toggle('demo',mode==='demo');
    $('mode-banner').textContent=mode==='demo'
      ? 'УЧЕБНЫЙ РЕЖИМ · Цены и результаты вымышлены. Это демонстрация интерфейса и правил, не торговые сигналы.'
      : 'Т-Банк · Реальные данные, экспериментальные сигналы. Кандидат покупки — совпадение шести условий; «Проверь продажу» — повод оценить выход из уже купленной акции. Доходность этих сигналов не доказана.';
  }
  function clearSingle(message='Получаем данные…') {
    displayedUntil=0;
    $('load-status').textContent=message;
    $('instrument-name').textContent=ticker;
    $('ticker-label').textContent=`${ticker} · TQBR`;
    $('ticker-avatar').textContent=ticker[0];
    ['price','buy-share','rvol','vwap','spread','data-time'].forEach(id=>$(id).textContent='—');
    $('price-change').textContent='Нет актуальной оценки';
    $('decision').textContent='Ожидание'; $('decision').classList.remove('positive','negative');
    $('score').innerHTML='—<small>/100</small>'; $('score-fill').style.width='0';
    $('passed').textContent='Условия не оценены';
    $('decision-text').textContent='До получения свежих данных сигнал заблокирован.';
    $('blockers').textContent=''; $('rules').innerHTML=''; $('price-chart').innerHTML='';
    $('chart-empty').classList.remove('hidden'); $('chart-period').textContent='—';
    $('orderbook').innerHTML='<p class="muted">Ожидаем заявки биржи</p>';
    $('book-samples').textContent='0 снимков';
    $('trade-plan').innerHTML='<p class="muted">План покупки появится, когда все шесть условий будут выполнены на свежих данных.</p>';
    currentPlan=null;$('use-plan').disabled=true;
    $('risk-plan-note').textContent='Свежего сценария покупки сейчас нет. Введённые цены — твой самостоятельный расчёт.';
  }
  function ruleValue(rule,a) {
    const values={trend:`EMA 50: ${money(a.ema)}`, flow:`Покупки: ${num(a.buy_share===null?null:a.buy_share*100,0)}% · ${a.trade_count} сделок`,
      volume:`Объём: ${num(a.rvol)}×`,vwap:`VWAP: ${money(a.vwap)}`,spread:`Спред: ${num(a.spread_bps===null?null:a.spread_bps/100,3)}%`,
      book:a.retention===null?'Накапливаем снимки стакана':`Сохранилось ${num(a.retention*100,0)}% объёма`};
    return values[rule.key] || '';
  }
  function drawChart(data) {
    const svg=$('price-chart');
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
    svg.innerHTML=out;
    $('chart-period').textContent=`${day(data[0].time)} — ${day(data[data.length-1].time)} · МСК`;
  }
  function renderSingle(payload) {
    if(payload.status!=='ok') {clearSingle(payload.message || 'Получаем историю и рыночные данные…'); return;}
    const a=payload.analysis,inst=payload.instrument;
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
    $('buy-share').textContent=a.buy_share===null?'—':num(a.buy_share*100,0)+'%';
    $('rvol').textContent=num(a.rvol)+'×'; $('vwap').textContent=money(a.vwap);
    $('spread').textContent=a.spread_bps===null?'—':num(a.spread_bps/100,3)+'%';
    $('rules').innerHTML=a.rules.map(r=>`<article class="panel rule ${r.pass===true?'pass':r.pass===null?'missing':'fail'}"><div class="rule-top"><span class="rule-icon">${r.pass===true?'✓':r.pass===null?'…':'−'}</span><h3>${esc(r.name)}</h3><span class="points">${r.pass===true?r.weight:0} / ${r.weight}</span></div><p>${esc(r.detail)}</p><div class="rule-value">${esc(ruleValue(r,a))}</div></article>`).join('');
    drawChart(a.chart);
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
      return `<article class="panel watch-card"><button class="remove" data-remove="${esc(s)}" aria-label="Удалить ${esc(s)} из обзора">×</button><a href="/?ticker=${encodeURIComponent(s)}&mode=${mode}"><h3>${esc(s)}</h3><div class="company">${esc(ok?r.instrument.name:'Загрузка инструмента')}</div><div class="watch-price">${ok?money(a.price):'—'}</div><div class="watch-decision"><span class="${a?.decision==='LONG'?'positive':a?.decision==='EXIT'?'negative':'muted'}">${label}</span><span>${ok?a.score+' / 100 покупки':'—'}</span></div><div class="watch-checks">${Array.from({length:6},(_,i)=>`<span class="${a?.rules[i].pass===true?'yes':''}" title="${esc(a?.rules[i].name||'Ожидание')}"></span>`).join('')}</div><div class="watch-note">${esc(note)}</div><div class="card-bottom"><span>${ok?time(a.data_time):'Ожидание ответа'}</span><span>Разобрать →</span></div></a></article>`;
    }).join('');
  }
  function renderForward(data) {
    if(data.status!=='ok') {$('forward-result').textContent=data.message || 'Не удалось прочитать наблюдения.';return;}
    const buy=data.buy_candidates,exit=data.exit_warnings;
    const note=buy.samples
      ? `После ${buy.samples} кандидатов покупки средний условный результат за час — ${num(buy.average_pct)}% после расходов; плюсовых случаев: ${buy.positive_or_correct}.`
      : `Зафиксировано ${data.candidate_events} появлений кандидата покупки; с проверенным часовым исходом пока нет. Баллы ниже показывают только условные покупки для исследования.`;
    const exitNote=exit.samples
      ? `После ${exit.samples} предупреждений о продаже цена за час снизилась в ${exit.positive_or_correct} случаях; среднее изменение: ${num(exit.average_pct)}%.`
      : `Зафиксировано ${data.exit_events} появлений предупреждения о продаже; с проверенным исходом пока нет.`;
    const table=data.completed?`<div class="table-scroll"><table><thead><tr><th>Баллы покупки</th><th>Сравнений</th><th>Средний результат через час</th><th>Плюсовых после расходов</th></tr></thead><tbody>${data.bands.map(b=>`<tr><td>${esc(b.name)}</td><td>${b.samples}</td><td class="${b.average_pct===null?'':b.average_pct>=0?'positive':'negative'}">${num(b.average_pct)}${b.average_pct===null?'':'%'}</td><td>${b.samples?`${b.positive_or_correct} из ${b.samples}`:'—'}</td></tr>`).join('')}</tbody></table></div>`:'';
    $('forward-result').innerHTML=`<div class="forward-stats"><div><span>Свежих наблюдений</span><strong>${data.observations}</strong></div><div><span>Часовых сравнений</span><strong>${data.completed}</strong></div><div><span>Пока без пары</span><strong>${data.without_pair}</strong></div></div><p class="research-meta">${esc(note)}</p><p class="research-meta">${esc(exitNote)}</p>${table}<p class="research-meta">Для сравнения баллов берём первое свежее наблюдение каждого часа; начало каждого нового сигнала считаем отдельно. Сравниваем с первой доступной котировкой через 60–70 минут. Условная покупка — по лучшей цене продажи, выход — по лучшей цене покупки. В расчёте по ${num(data.fee_bps/100)}% комиссии и ${num(data.slippage_bps/100)}% проскальзывания на каждую сторону. Пропущенные часы не достраиваем. Менее 30 сравнений — слишком мало для вывода, а соседние часы могут зависеть друг от друга.</p>`;
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
        loading ||= payload.status==='loading';
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
    clearTimeout(timer);generation++;researchGeneration++;paperRevision++;
    banner();results.clear();
    if(watch) renderWatch(); else {
      $('symbol').value=ticker;resetRiskContext();clearSingle();
      $('forward-result').innerHTML=mode==='demo'?'<p class="muted">В учебном режиме исходы вымышлены. Здесь появятся только наблюдения реального рынка.</p>':'<p class="muted">Читаем сохранённые наблюдения…</p>';
      $('research-result').innerHTML='<p class="muted">Нажми «Проверить на истории» после загрузки акции.</p>';
      $('research-history').value=mode==='demo'?'current':researchHistory;
      $('research-history').disabled=mode==='demo';
      $('paper-result').innerHTML=mode==='demo'?'<p class="muted">В учебном режиме журнал недоступен. Здесь учитываются только сохранённые живые котировки.</p>':'<p class="muted">Читаем журнал условных сделок…</p>';
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
  setInterval(()=>{if(!watch && mode==='live' && !document.hidden)loadForward(generation);},60000);
  // Clear actionable prices even if a slow request or a disconnected server delays polling.
  setInterval(()=>{
    if(mode!=='live')return;
    if(!watch) {
      if(displayedUntil && Date.now()>displayedUntil)clearSingle('Котировки устарели. Ожидаем свежие данные.');
    } else {
      let changed=false;
      for(const [symbol,payload] of results) {
        const a=payload.analysis;
        if(payload.status==='ok' && ['LONG','EXIT'].includes(a?.decision) && Date.now()>Math.min(Date.parse(a.data_time)+45000,Date.parse(a.updated_at)+35000)) {
          results.set(symbol,{status:'loading',message:'Котировки устарели. Ожидаем обновление.'});changed=true;
        }
      }
      if(changed)renderWatch();
    }
  },1000);
  function clock() {$('clock').textContent=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'})+' МСК';}
  clock();setInterval(clock,30000);restart();
})();
