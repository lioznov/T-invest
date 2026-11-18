(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object' && module.exports)module.exports=api;
  if(root.document)api.mount(root.document,root);
})(typeof window==='undefined'?globalThis:window,function(){
  'use strict';
  const finite=value=>typeof value==='number' && Number.isFinite(value);
  const positive=value=>finite(value) && value>0;
  const ticker=value=>typeof value==='string' && /^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(value);
  function number(value,digits=2){return finite(value)?value.toLocaleString('ru-RU',{maximumFractionDigits:digits,minimumFractionDigits:digits}):'—';}
  function price(value){return positive(value)?`${value<0.000001?value.toPrecision(4):number(value,value<1?6:2)} ₽`:'—';}
  function percent(value){return finite(value)?`${value>0?'+':''}${number(value)}%`:'—';}
  function date(value){
    if(typeof value!=='string' || !/^\d{4}-\d\d-\d\dT.+(?:Z|[+-]\d\d:\d\d)$/.test(value))return '—';
    const stamp=Date.parse(value);
    return Number.isFinite(stamp)?`${new Date(stamp).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})} МСК`:'—';
  }
  function periodDateRange(start,endExclusive){
    const parse=value=>typeof value==='string' && /^\d{4}-\d\d-\d\d$/.test(value)?Date.parse(`${value}T00:00:00Z`):NaN;
    const from=parse(start),until=parse(endExclusive);
    if(!Number.isFinite(from) || !Number.isFinite(until) || until<=from
      || new Date(from).toISOString().slice(0,10)!==start || new Date(until).toISOString().slice(0,10)!==endExclusive)return null;
    const format=stamp=>{const iso=new Date(stamp).toISOString();return `${iso.slice(8,10)}.${iso.slice(5,7)}.${iso.slice(0,4)}`;};
    return `${format(from)} — ${format(until-86400000)}`;
  }
  function forecastState(value,now=Date.now()){
    if(!value || typeof value!=='object')return {state:'unavailable',forecast:null};
    if(value.status==='demo')return {state:'demo',forecast:null};
    if(value.status==='loading')return {state:'loading',forecast:null};
    if(!['ok','historical'].includes(value.status))return {state:'unavailable',forecast:null};
    const f=value.forecast;
    if(!f || !positive(f.anchor_price) || !positive(f.predicted_price) || date(f.as_of)==='—' || date(f.target_at)==='—'
      || Date.parse(f.target_at)<=Date.parse(f.as_of))return {state:'unavailable',forecast:null};
    const historical=value.status==='historical' || f.historical===true || Date.parse(f.target_at)<=now
      || now>=Date.parse(f.as_of)+15*60*1000;
    return {state:historical?'historical':'ok',forecast:f};
  }
  function overviewState(value,selection,quote,now=Date.now()){
    if(!selection || selection.mode==='demo')return {state:'demo'};
    if(value?.status==='loading' && value.ticker===selection.ticker)return {state:'loading'};
    if(value?.ticker!==selection.ticker || value.entry_allowed!==false)return {state:'unavailable'};
    const {state,forecast}=forecastState(value,now);
    if(state!=='ok')return {state};
    const result={state,forecast};
    if(quote?.ticker===selection.ticker && quote.mode===selection.mode && positive(quote.price)){
      result.delta=forecast.predicted_price-quote.price;
      const percentChange=result.delta/quote.price*100;
      if(finite(percentChange))result.percentChange=percentChange;
    }
    return result;
  }
  function mount(document,env){
    const form=document.getElementById('morning-form');
    if(!form)return null;
    const byId=id=>document.getElementById(id);
    const field=byId('morning-ticker'),button=byId('morning-refresh'),badge=byId('morning-badge');
    const status=byId('morning-status'),result=byId('morning-result'),report=byId('morning-report');
    let sequence=0,controller=null,expiryTimer=null;
    const overview=byId('forecast-overview');
    let selection=null,quote=null,overviewValue=null,overviewExpiry=null;
    function renderOverview(){
      if(overviewExpiry!==null)env.clearTimeout(overviewExpiry);
      overviewExpiry=null;
      const {state,forecast,delta,percentChange}=overviewState(overviewValue,selection,quote);
      const ready=state==='ok';
      overview.dataset.state=state;
      overview.setAttribute('aria-busy',String(state==='loading'));
      byId('forecast-overview-price').textContent=ready?price(forecast.predicted_price):
        state==='loading'?'Загружаем прогноз…':'Прогноз на завтра пока недоступен';
      const explanations={
        demo:'В учебном режиме доступны только примеры котировок.',
        historical:'Сохранённый расчёт доступен в архиве. Для нового прогноза нужен свежий вечерний расчёт.',
        unavailable:overviewValue?.message || 'Для выбранной акции пока нет свежего сохранённого расчёта.',
      };
      byId('forecast-overview-detail').textContent=ready
        ? `Расчётная оценка на ${date(forecast.target_at)}. Данные модели на ${date(forecast.as_of)}.`
        : explanations[state] || 'Проверяем сохранённый расчёт для выбранной акции.';
      const difference=byId('forecast-overview-change');
      difference.hidden=!ready || !finite(delta);
      difference.textContent=difference.hidden?'':`К последней доступной цене ${delta>0?'+':''}${number(delta,Math.abs(delta)<.01 && delta!==0?6:2)} ₽${finite(percentChange)?' ('+percent(percentChange)+')':''}`;
      const range=byId('forecast-overview-range');
      range.hidden=!ready || !positive(forecast.lower_price) || !positive(forecast.upper_price) ||
        forecast.lower_price>forecast.predicted_price || forecast.upper_price<forecast.predicted_price;
      range.textContent=range.hidden?'':`Диапазон оценки ${price(forecast.lower_price)} – ${price(forecast.upper_price)}. Его ширина основана на прошлых ошибках модели.`;
      const direction=byId('forecast-overview-direction');
      direction.hidden=!ready;
      const directions={up:'Возможен рост',down:'Возможно снижение',uncertain:'Направление неопределённо'};
      direction.textContent=ready?`${directions[forecast.direction] || directions.uncertain} относительно цены на ${date(forecast.as_of)} (${price(forecast.anchor_price)}).`:'';
      if(ready){
        const expires=Math.min(Date.parse(forecast.target_at),Date.parse(forecast.as_of)+900000);
        overviewExpiry=env.setTimeout(renderOverview,Math.max(1,expires-Date.now()));
      }
    }
    function clearExpiry(){if(expiryTimer!==null){env.clearTimeout(expiryTimer);expiryTimer=null;}}
    function node(tag,text,className){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;}
    function paragraph(parent,text,className='muted small'){parent.append(node('p',text,className));}
    function metric(parent,label,value,note){const card=node('div',undefined,'morning-metric');card.append(node('span',label),node('strong',value));if(note)card.append(node('small',note));parent.append(card);}
    function item(parent,label,value){const line=node('div');line.append(node('dt',label),node('dd',value));parent.append(line);}
    function renderReport(value,rules){
      report.replaceChildren();
      if(!value || typeof value!=='object'){paragraph(report,'Сохранённого отчёта нет. Нельзя оценить качество или доходность модели.');return;}
      paragraph(report,`Обучение: ${finite(value.trained_rows)?number(value.trained_rows,0):'—'} примеров. Калибровка диапазона: ${finite(value.calibration_rows)?number(value.calibration_rows,0):'—'} примеров.`);
      const periods=node('div',undefined,'morning-periods');
      for(const [key,label] of [['test','Первая проверка после обучения'],['later','Более поздний период']]){
        const period=value.periods?.[key],card=node('section',undefined,'morning-period');card.append(node('h3',label));
        const periodDates=periodDateRange(period?.start,period?.end_exclusive);
        if(periodDates)paragraph(card,`Границы проверки: ${periodDates}.`);
        if(!period || !positive(period.samples)){paragraph(card,'Нет оценённых прогнозов. Точность и доходность не определены.');periods.append(card);continue;}
        const list=node('dl');
        item(list,'Прогнозов / торговых дней',`${number(period.samples,0)} / ${finite(period.days)?number(period.days,0):'—'}`);
        if(finite(period.unknown_targets))item(list,'Неизвестных утренних цен',number(period.unknown_targets,0));
        item(list,'Средняя ошибка модели',finite(period.mae_bps)?`${number(period.mae_bps/100)}%`:'—');
        item(list,'Ошибка «цена не изменится»',finite(period.baseline_mae_bps)?`${number(period.baseline_mae_bps/100)}%`:'—');
        if(finite(period.mean_baseline_mae_bps))item(list,'Ошибка «как в среднем на обучении»',`${number(period.mean_baseline_mae_bps/100)}%`);
        const directionAccuracy=finite(period.direction_accuracy_pct)?`${number(period.direction_accuracy_pct)}%`:'—';
        item(list,'Угадано направление по точке',`${directionAccuracy}${finite(period.direction_nonzero_samples)?` · n=${number(period.direction_nonzero_samples,0)} без нулевых движений`:''}`);
        item(list,'Цена попала в диапазон',finite(period.coverage_pct)?`${number(period.coverage_pct)}%`:'—');
        if(finite(period.interval_width_bps))item(list,'Ширина диапазона',`${number(period.interval_width_bps/100)}%`);
        if(finite(period.abstention_pct))item(list,'Модель не выбрала направление',`${number(period.abstention_pct)}%`);
        card.append(list);
        const paper=period.paper;
        if(finite(paper?.selected_candidates))paragraph(card,`Выбрано кандидатов по условному правилу: ${number(paper.selected_candidates,0)}. Это не число исполненных сделок.`, 'morning-paper muted');
        const incomplete=period.status==='incomplete' || paper?.status==='incomplete' || positive(paper?.unknown_trades);
        if(incomplete)paragraph(card,'Проверка условных сделок неполная. Есть неизвестные исходы или незавершённый расчёт: доходность и просадка не оцениваются.', 'morning-paper');
        else if(!paper || !positive(paper.trades))paragraph(card,'Условных сделок нет. Доходность не оценивается.', 'morning-paper');
        else paragraph(card,`Условных сделок: ${number(paper.trades,0)}. Результат после расходов: ${percent(paper.net_return_pct)}. Максимальная просадка: ${finite(paper.max_drawdown_pct)?`${number(paper.max_drawdown_pct)}%`:'—'}.`, 'morning-paper');
        paragraph(card,`Сделок с неизвестным исходом: ${finite(paper?.unknown_trades)?number(paper.unknown_trades,0):'не указано'}. Пропуски не считаются прибылью.`, 'morning-paper muted');
        if(paper && Object.hasOwn(paper,'stress_return_pct')){
          const stressUnknown=paper.stress_unknown_trades;
          if(incomplete || positive(stressUnknown) || !finite(stressUnknown))paragraph(card,'При удвоенных расходах проверка неполная; результат не оценивается.', 'morning-paper muted');
          else if(!positive(paper.trades) || paper.stress_trades===0)paragraph(card,'При удвоенных расходах нет оценённых сделок; доходность не определяется.', 'morning-paper muted');
          else paragraph(card,`При удвоенных расходах: ${percent(paper.stress_return_pct)}.`, 'morning-paper muted');
          paragraph(card,`Неизвестных исходов при удвоенных расходах: ${finite(stressUnknown)?number(stressUnknown,0):'не указано'}.`, 'morning-paper muted');
        }
        const baseline=period.always_buy;
        if(baseline){
          const baselineIncomplete=period.status==='incomplete' || baseline.status==='incomplete' || positive(baseline.unknown_trades);
          if(baselineIncomplete)paragraph(card,`Сравнение «покупать каждый вечер»: проверка неполная, неизвестных исходов ${finite(baseline.unknown_trades)?number(baseline.unknown_trades,0):'не указано'}. Доходность не оценивается.`, 'morning-paper muted');
          else if(!positive(baseline.trades))paragraph(card,'Сравнение «покупать каждый вечер»: нет условных сделок; доходность не определяется.', 'morning-paper muted');
          else paragraph(card,`Сравнение «покупать каждый вечер»: ${number(baseline.trades,0)} условных сделок, после расходов ${percent(baseline.net_return_pct)}. Это базовое сравнение на том же периоде, не рекомендация.`, 'morning-paper muted');
        }
        if(typeof period.note==='string')paragraph(card,period.note);
        periods.append(card);
      }
      report.append(periods);
      paragraph(report,'Ошибка сравнивается с простым прогнозом «утром цена останется прежней». Точность направления считается по точечной оценке; сама модель выбирает направление только если весь диапазон выше или ниже исходной цены. Угаданное направление не означает прибыль после комиссии и проскальзывания. Диапазон с ориентиром 80% — общая историческая калибровка, не 80% вероятности заработать на этой акции.');
      if(rules && positive(rules.paper_threshold_pct) && positive(rules.paper_capital) && positive(rules.max_stock_allocation)
        && finite(rules.fee_bps_side) && finite(rules.slippage_bps_side)
        && typeof rules.cutoff==='string' && typeof rules.entry==='string' && typeof rules.target==='string'
        && typeof rules.paper_sizing==='string' && rules.paper_sizing.includes('whole lots')){
        paragraph(report,`Условные сделки проверялись по отдельному правилу: покупка, когда точечный прогноз роста от цены на ${rules.cutoff} выше ${percent(rules.paper_threshold_pct)}. Вход — ${rules.entry}, выход — ${rules.target}. Начальный капитал — ${price(rules.paper_capital)}, до ${number(rules.max_stock_allocation*100,0)}% в одной акции, только целые лоты. С каждой стороны учтены комиссия ${number(rules.fee_bps_side/100)}% и проскальзывание ${number(rules.slippage_bps_side/100)}%; стресс-проверка удваивает оба расхода. Это не выбор направления по всему диапазону: он может оставаться неопределённым, даже когда точечный прогноз создаёт условную сделку.`);
      }
      if(typeof value.note==='string')paragraph(report,value.note);
    }
    function render(value,now=Date.now()){
      clearExpiry();
      const model=forecastState(value,now);result.replaceChildren();renderReport(value?.report,value?.rules);
      const labels={ok:'Эксперимент · не сигнал',historical:'Архивный прогноз',demo:'Учебный режим',loading:'Расчёт ещё не готов',unavailable:'Нет доступного прогноза'};
      badge.textContent=labels[model.state];status.dataset.state=model.state;
      const messages={
        ok:'Исследовательский прогноз. Автоматическая покупка и продажа запрещены: качество модели ещё нужно подтверждать на новых днях.',
        historical:'Это архивный прогноз, не текущий сигнал покупки. Вечернее окно до 18:00 МСК уже закрыто либо сохранённые данные не относятся к текущему вечеру.',
        demo:'Учебный режим: реального прогноза на утро нет. Вымышленные цены и сигналы не показываются. Для чтения реальной сохранённой модели выбери «Рынок».',
        loading:'Прогноз ещё не готов. Нажми «Посмотреть прогноз» позже; старые цены не выдаются за новый расчёт.',
        unavailable:'Сохранённый прогноз недоступен или неполон. Это не означает, что цена не изменится. Проверь историю и обучение отдельной модели.'
      };
      status.textContent=messages[model.state];
      if(typeof value?.message==='string')paragraph(result,value.message);
      const f=model.forecast;
      if(f){
        paragraph(result,`${ticker(value.ticker)?value.ticker:'Акция'} · Данные на ${date(f.as_of)}. Прогноз на ${date(f.target_at)}.`, 'morning-dates');
        const cards=node('div',undefined,'morning-metrics');
        metric(cards,'Исходная цена',price(f.anchor_price),'Из доступных модели данных');
        metric(cards,'Оценка цены утром',price(f.predicted_price),'Не цена исполнения заявки');
        metric(cards,'Изменение к исходной цене',percent(f.predicted_change_pct),'Не ожидаемая прибыль сделки');
        const range=positive(f.lower_price) && positive(f.upper_price) && f.lower_price<=f.predicted_price && f.upper_price>=f.predicted_price;
        metric(cards,'Калиброванный диапазон',range?`${price(f.lower_price)} – ${price(f.upper_price)}`:'—','Ориентир 80% на прошлых примерах, не гарантия');
        result.append(cards);
        const directions={up:'Модель ожидает рост. Это не разрешение покупать.',down:'Модель ожидает снижение. Не открывать новую покупку — не значит продавать в шорт.',uncertain:'Направление слишком неопределённо. Модель не выбирает сторону.'};
        paragraph(result,directions[f.direction] || directions.uncertain,'morning-direction');
        if(f.available_at)paragraph(result,`Расчёт был доступен: ${date(f.available_at)}.`);
      }
      if(typeof value?.note==='string')paragraph(result,value.note);
      if(model.state==='ok'){
        const deadline=Date.parse(f.as_of)+15*60*1000,delay=deadline-now;
        // A local expiry only changes the displayed state; it never fetches or trains.
        // Passing the elapsed deadline prevents an early timer or clock rollback loop.
        if(delay>0 && delay<=15*60*1000){const payloadSequence=sequence;const scheduled=env.setTimeout(()=>{
          if(payloadSequence!==sequence || expiryTimer!==scheduled)return;
          expiryTimer=null;render(value,Math.max(Date.now(),deadline));
        },delay);expiryTimer=scheduled;}
      }
      return model;
    }
    async function load(){
      const symbol=field.value.trim().toUpperCase();
      const current=++sequence;clearExpiry();if(controller)controller.abort();
      if(!ticker(symbol)){button.disabled=false;render({status:'unavailable',message:'Введи один корректный тикер, например SBER. Только латинские буквы, цифры, точка или дефис.'});return;}
      field.value=symbol;
      controller=new env.AbortController();const active=controller;
      const timer=env.setTimeout(()=>active.abort(),15000);button.disabled=true;
      render({status:'loading'});
      const query=new URLSearchParams(env.location.search),mode=query.get('mode')==='demo'?'demo':'live';
      try{
        const response=await env.fetch(`/api/morning-forecast/?ticker=${encodeURIComponent(symbol)}&mode=${mode}`,{cache:'no-store',signal:active.signal});
        const value=await response.json();if(current!==sequence)return;
        if(!response.ok)publish({status:'unavailable',ticker:symbol,message:typeof value?.message==='string'?value.message:undefined},symbol,mode);
        else publish(value,symbol,mode);
      }catch{if(current===sequence)publish({status:'unavailable',ticker:symbol,message:'Не удалось получить ответ сервера. Текущий прогноз не подтверждён.'},symbol,mode);}
      finally{env.clearTimeout(timer);if(current===sequence)button.disabled=false;}
    }
    function publish(value,symbol,mode){
      render(value);
      if(overview && selection?.ticker===symbol && selection.mode===mode){
        overviewValue=value;
        renderOverview();
      }
    }
    const selected=new URLSearchParams(env.location.search).get('ticker')?.toUpperCase();if(ticker(selected))field.value=selected;
    form.addEventListener('submit',event=>{event.preventDefault();load();});
    if(overview){
      document.addEventListener('vorby:analysis-selection',event=>{
        selection=event.detail;
        quote=null;
        field.value=selection.ticker;
        overviewValue={status:'loading',ticker:selection.ticker};
        renderOverview();
        load();
      });
      document.addEventListener('vorby:analysis-quote',event=>{
        quote=event.detail;
        if(overviewValue?.status==='ok')renderOverview();
      });
    }else{
      document.querySelectorAll('[data-mode]').forEach(element=>element.addEventListener('click',load));
      load();
    }
    return {render,load};
  }
  return {forecastState,overviewState,mount,price,percent,date,periodDateRange};
});
