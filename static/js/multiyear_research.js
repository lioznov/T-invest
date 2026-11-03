(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object' && module.exports)module.exports=api;
  if(root.document)api.mount(root.document,root);
})(typeof window==='undefined'?globalThis:window,function(){
  'use strict';
  const order=['daily_full','control','main_hour','evening_hour'];
  const clocks={control:['17:45','18:00'],main_hour:['18:00','18:15'],evening_hour:['22:45','23:00']};
  const finite=value=>typeof value==='number' && Number.isFinite(value);
  const count=value=>Number.isSafeInteger(value) && value>=0;
  function number(value,digits=2){return finite(value)?value.toLocaleString('ru-RU',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—';}
  function pct(value){return finite(value)?`${value>0?'+':''}${number(value)}%`:'—';}
  function bps(value){return finite(value)?`${number(value/100,4)}%`:'—';}
  function dated(value){
    if(typeof value!=='string' || !/^\d{4}-\d\d-\d\dT.+(?:Z|[+-]\d\d:\d\d)$/.test(value))return null;
    const parsed=Date.parse(value);return Number.isFinite(parsed)?parsed:null;
  }
  function day(value){const time=dated(value);return time===null?'—':new Date(time).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow'});}
  function dateRange(period){
    const parse=value=>typeof value==='string' && /^\d{4}-\d\d-\d\d$/.test(value)?Date.parse(`${value}T00:00:00Z`):NaN;
    const from=parse(period?.start),to=parse(period?.end_exclusive);
    if(!Number.isFinite(from)||!Number.isFinite(to)||to<=from || new Date(from).toISOString().slice(0,10)!==period.start || new Date(to).toISOString().slice(0,10)!==period.end_exclusive)return null;
    return `${new Date(from).toLocaleDateString('ru-RU',{timeZone:'UTC'})} — ${new Date(to-86400000).toLocaleDateString('ru-RU',{timeZone:'UTC'})}`;
  }
  function tradeResult(paper,stress=false){
    if(!paper)return 'Нет расчёта';
    const unknown=stress?paper.stress_unknown_trades:paper.unknown_trades;
    const trades=stress?paper.stress_trades:paper.trades;
    if(paper.status==='incomplete' || !count(paper.unknown_trades) || paper.unknown_trades>0 || !count(unknown) || unknown>0)return 'Неполные данные · доходность неизвестна';
    if(!count(trades) || trades===0)return 'Нет сделок · доходность не определена';
    return pct(stress?paper.stress_return_pct:paper.net_return_pct);
  }
  function valid(value){
    if(value?.status!=='historical' || value.entry_allowed!==false || !Array.isArray(value.variants) || value.variants.length!==4)return false;
    const seen=new Set();
    for(const v of value.variants){
      if(!v || !order.includes(v.id) || seen.has(v.id) || typeof v.label!=='string' || !v.report)return false;
      if(v.id==='daily_full'){if(v.kind!=='daily_observational')return false;}
      else if(v.kind!=='intraday' || v.cutoff!==clocks[v.id][0] || v.entry!==clocks[v.id][1])return false;
      seen.add(v.id);
    }
    if(!value.coverage || typeof value.coverage!=='object' || Array.isArray(value.coverage) || Object.keys(value.coverage).length===0)return false;
    return Object.entries(value.coverage).every(([ticker,windows])=>/^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(ticker)
      && ['day','intraday'].every(key=>{
        const w=windows?.[key],from=dated(w?.requested_from),to=dated(w?.to);
        if(!w || !count(w.bars) || from===null || to===null || from>=to)return false;
        if(w.bars===0)return w.first_actual===null && w.last_actual===null;
        const first=dated(w.first_actual),last=dated(w.last_actual);
        return first!==null && last!==null && from<=first && first<=last && last<to;
      }));
  }
  function mount(document,env){
    if(!document.getElementById('multiyear-research'))return null;
    const status=document.getElementById('multiyear-status'),result=document.getElementById('multiyear-result');
    function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
    function paragraph(parent,text,cls='muted small'){parent.append(node('p',text,cls));}
    function overview(parent,control){
      const section=node('section',undefined,'multiyear-overview');section.append(node('h3','Коротко: контрольный расчёт в 17:45'));
      paragraph(section,'Это заранее определённый контроль 17:45, не автоматический выбор лучшего варианта. Все четыре модели и неполные проверки других часов остаются в таблицах ниже.');
      paragraph(section,'Условный счёт 100 000 ₽, не реальные заработанные деньги.','multiyear-warning');
      paragraph(section,'Правило проверки: точечный прогноз роста более +0,4% от цены 17:45 → условная покупка в 18:00 → выход следующим днём в 10:00. Только пн–чт, без шорта и плеча, не более 25% счёта на одну акцию.');
      paragraph(section,'Расходы: комиссия 0,05% и проскальзывание 0,05% на каждую сторону. Стресс удваивает оба значения. Цены свечей — лишь приближение исполнения, не реальные заявки.');
      const grid=node('div',undefined,'multiyear-summary-grid');
      for(const [key,year] of [['test','2025'],['later','2026']]){
        const period=control?.report?.periods?.[key],paper=period?.paper,card=node('article',undefined,'multiyear-summary-card');
        card.append(node('h4',year));const range=dateRange(period);if(range)paragraph(card,range);
        const list=node('dl');
        for(const [label,value] of [
          ['После обычных расходов',tradeResult(paper)],
          ['При удвоенных расходах',tradeResult(paper,true)],
          ['Условных сделок',count(paper?.trades)?number(paper.trades,0):'—'],
          ['Неизвестных исходов',count(paper?.unknown_trades)?number(paper.unknown_trades,0):'—'],
        ]){const line=node('div');line.append(node('dt',label),node('dd',value));list.append(line);}
        card.append(list);grid.append(card);
      }
      section.append(grid);
      paragraph(section,'Ошибка прогноза цены сравнивается ниже с простым предположением «цена без изменения». Положительный результат условного счёта сам по себе не доказывает качество прогноза и будущую прибыль.');
      parent.append(section);
    }
    function coverage(parent,value){
      const details=node('details',undefined,'multiyear-coverage-details');
      const total=Object.values(value).reduce((sum,windows)=>sum+windows.day.bars+windows.intraday.bars,0);
      details.append(node('summary',`Сколько истории получено по каждой акции?${count(total)?` · ${number(total,0)} свечей`:''}`));
      paragraph(details,'Сумма включает дневные и внутридневные свечи разных интервалов, а не отдельные сделки.');
      const grid=node('div',undefined,'multiyear-coverage');
      for(const [ticker,windows] of Object.entries(value)){
        if(!/^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(ticker))continue;
        const card=node('section',undefined,'multiyear-stock');card.append(node('h4',ticker));
        for(const [key,label] of [['day','Дневные свечи'],['intraday','Внутридневные свечи']]){
          const window=windows?.[key];
          if(!window || !count(window.bars)){paragraph(card,`${label}: нет проверенных данных.`);continue;}
          paragraph(card,`${label}: ${number(window.bars,0)}.`);
          if(window.bars===0)paragraph(card,'Фактическая история отсутствует.');
          else paragraph(card,`Фактически: ${day(window.first_actual)} — ${day(window.last_actual)}.`);
          paragraph(card,`Запрошено с ${day(window.requested_from)} до ${day(window.to)} (правая граница не включается).`);
        }
        grid.append(card);
      }
      details.append(grid);
      paragraph(details,'Количество свечей не равно количеству обучающих примеров: часть данных используется для подготовки признаков, а неполные окна исключаются. Фактическое начало истории может быть позже запрошенного.');
      parent.append(details);
    }
    function reportTable(parent,variants,periodKey){
      const section=node('section',undefined,'multiyear-period');section.append(node('h3',periodKey==='test'?'Проверка на 2025 году':'Более поздняя проверка на 2026 году'));
      const periods=variants.map(v=>v.report?.periods?.[periodKey]);
      const ranges=[...new Set(periods.map(dateRange).filter(Boolean))];
      if(ranges.length)paragraph(section,`Границы проверки: ${ranges.join('; ')}.`);
      paragraph(section,'В таблице 4 модели. Если не все столбцы видны, прокрути таблицу вправо →.','multiyear-table-hint');
      const wrap=node('div',undefined,'multiyear-scroll');wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label',`Многолетние модели: ${periodKey==='test'?'2025':'2026'}`);
      const table=node('table',undefined,'multiyear-table'),head=node('thead'),headers=node('tr');
      const corner=node('th','Показатель');corner.scope='col';headers.append(corner);
      for(const v of variants){
        const daily=v.kind==='daily_observational',th=node('th',daily?'Полная дневная свеча':`${v.cutoff} МСК`);th.scope='col';
        th.append(node('small',v.label),node('small',daily?'Цель: следующее дневное открытие':`Вход ${v.entry} · цель следующий день 10:00`));headers.append(th);
      }
      head.append(headers);table.append(head);
      const body=node('tbody');
      const rows=[
        ['Обучение / калибровка',(_,v)=>`${count(v.report?.trained_rows)?number(v.report.trained_rows,0):'—'} / ${count(v.report?.calibration_rows)?number(v.report.calibration_rows,0):'—'}`],
        ['Оценённых прогнозов / дней',p=>`${count(p?.samples)?number(p.samples,0):'—'} / ${count(p?.days)?number(p.days,0):'—'}`],
        ['Всего прогнозов / неизвестных целей',p=>`${count(p?.eligible_forecasts)?number(p.eligible_forecasts,0):'—'} / ${count(p?.unknown_targets)?number(p.unknown_targets,0):'—'}`],
        ['Средняя ошибка модели',p=>p?.samples>0?bps(p.mae_bps):'Нет оценённых прогнозов'],
        ['Ошибка «цена не изменится»',p=>p?.samples>0?bps(p.baseline_mae_bps):'Нет оценённых прогнозов'],
        ['Ошибка «как в среднем на обучении»',p=>p?.samples>0?bps(p.mean_baseline_mae_bps):'Нет оценённых прогнозов'],
        ['Угадано направление по точке',p=>p?.samples>0?`${finite(p.direction_accuracy_pct)?`${number(p.direction_accuracy_pct)}%`:'—'} · n=${count(p.direction_nonzero_samples)?number(p.direction_nonzero_samples,0):'—'}`:'Нет оценённых прогнозов'],
        ['Ширина диапазона / попадания',p=>p?.samples>0?`${bps(p.interval_width_bps)} / ${finite(p.coverage_pct)?`${number(p.coverage_pct)}%`:'—'}`:'Нет оценённых прогнозов'],
        ['Не выбрано направление по диапазону',p=>p?.samples>0 && finite(p.abstention_pct)?`${number(p.abstention_pct)}%`:'—'],
      ];
      const tradingRows=[
        ['Всего отобрано кандидатов',p=>count(p?.paper?.selected_candidates)?number(p.paper.selected_candidates,0):'—'],
        ['Условных сделок / неизвестных исходов',p=>`${count(p?.paper?.trades)?number(p.paper.trades,0):'—'} / ${count(p?.paper?.unknown_trades)?number(p.paper.unknown_trades,0):'—'}`],
        ['Результат счёта после расходов',p=>tradeResult(p?.paper)],
        ['Результат при удвоенных расходах',p=>tradeResult(p?.paper,true)],
        ['Стресс-сделок / неизвестных исходов',p=>`${count(p?.paper?.stress_trades)?number(p.paper.stress_trades,0):'—'} / ${count(p?.paper?.stress_unknown_trades)?number(p.paper.stress_unknown_trades,0):'—'}`],
        ['«Покупать каждый вечер» после расходов',p=>tradeResult(p?.always_buy)],
      ];
      function appendRow(label,read,trading){const row=node('tr'),th=node('th',label);th.scope='row';row.append(th);periods.forEach((p,i)=>row.append(node('td',trading && variants[i].kind==='daily_observational'?'Не моделировалось':read(p,variants[i]))));body.append(row);}
      for(const [label,read] of rows)appendRow(label,read,false);
      for(const [label,read] of tradingRows)appendRow(label,read,true);
      table.append(body);wrap.append(table);section.append(wrap);
      paragraph(section,'Дневная модель: только оценка цены, покупка по закрытию не моделировалась. Её дневное открытие и цель внутридневных моделей в 10:00 — разные значения. Сравнивать их ошибки как единую торговую стратегию нельзя.');
      paragraph(section,'При неизвестном выбранном исходе точный расчёт всего счёта останавливается: показанные сделки — только рассчитанные до остановки, не все отобранные кандидаты. Частичный результат не выдаётся за доходность периода.');
      for(let i=0;i<periods.length;i++)if(typeof periods[i]?.note==='string')paragraph(section,`${variants[i].kind==='daily_observational'?'Дневная модель':variants[i].cutoff}: ${periods[i].note}`);
      parent.append(section);
    }
    function render(value){
      result.replaceChildren();
      if(!valid(value)){status.textContent='Многолетний отчёт недоступен или не прошёл проверку. Даты, точность и доходность не выдумываются.';return;}
      status.textContent='Только историческая проверка. Новые прогнозы и торговые сигналы здесь не создаются.';
      if(typeof value.notice==='string')paragraph(result,value.notice,'multiyear-warning');
      if(typeof value.publication_note==='string')paragraph(result,value.publication_note,'multiyear-warning');
      overview(result,value.variants.find(v=>v.id==='control'));
      coverage(result,value.coverage);
      const variants=order.map(id=>value.variants.find(v=>v.id===id));
      reportTable(result,variants,'test');reportTable(result,variants,'later');
      paragraph(result,'Для внутридневных моделей условные покупки проверяются отдельным порогом точечного прогноза, а неопределённое направление относится ко всему диапазону ошибки. Это разные правила. Угаданное направление не равно прибыли после расходов. Неизвестный исход не считается нулевым убытком или прибылью.');
      if(typeof value.note==='string')paragraph(result,value.note,'multiyear-warning');
    }
    async function load(){
      try{const response=await env.fetch('/api/multiyear-research/',{cache:'no-store',headers:{Accept:'application/json'}});if(!response.ok)throw new Error('unavailable');render(await response.json());}
      catch(_){render(null);}
    }
    load();return {render};
  }
  return {mount,valid,tradeResult,day,dateRange,bps,pct};
});
