(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object' && module.exports)module.exports=api;
  if(root.document)api.mount(root.document,root);
})(typeof window==='undefined'?globalThis:window,function(){
  'use strict';
  const ids=['control','main_hour','evening_hour'];
  const clocks={control:['17:45','18:00'],main_hour:['18:00','18:15'],evening_hour:['22:45','23:00']};
  const finite=value=>typeof value==='number' && Number.isFinite(value);
  const count=value=>Number.isInteger(value) && value>=0;
  function number(value,digits=2){return finite(value)?value.toLocaleString('ru-RU',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—';}
  function pct(value){return finite(value)?`${value>0?'+':''}${number(value)}%`:'—';}
  function bps(value){return finite(value)?`${number(value/100)}%`:'—';}
  function complete(paper,stress=false){
    if(!paper || paper.status==='incomplete' || !count(paper.unknown_trades) || paper.unknown_trades!==0 || !count(paper.trades) || paper.trades===0)return false;
    return !stress || count(paper.stress_trades) && paper.stress_trades>0 && paper.stress_unknown_trades===0;
  }
  function tradeResult(paper,stress=false){
    if(!paper)return 'Нет расчёта';
    const unknown=stress?paper.stress_unknown_trades:paper.unknown_trades;
    const trades=stress?paper.stress_trades:paper.trades;
    if(paper.status==='incomplete' || !count(unknown) || unknown>0)return 'Неполные данные · доходность неизвестна';
    if(!count(trades) || trades===0)return 'Нет сделок · доходность не определена';
    if(!complete(paper,stress))return 'Доходность не определена';
    return pct(stress?paper.stress_return_pct:paper.net_return_pct);
  }
  function valid(value){
    if(value?.status!=='historical' || value.entry_allowed!==false || !Array.isArray(value.variants) || value.variants.length!==3)return false;
    const names=new Set();
    for(const v of value.variants){
      if(!v || !ids.includes(v.id) || names.has(v.id) || v.cutoff!==clocks[v.id][0] || v.entry!==clocks[v.id][1] || typeof v.label!=='string' || !v.report || !v.paired_report)return false;
      names.add(v.id);
    }
    return true;
  }
  function dateRange(period){
    const parse=value=>typeof value==='string' && /^\d{4}-\d\d-\d\d$/.test(value)?Date.parse(`${value}T00:00:00Z`):NaN;
    const from=parse(period?.start),to=parse(period?.end_exclusive);
    if(!Number.isFinite(from)||!Number.isFinite(to)||to<=from || new Date(from).toISOString().slice(0,10)!==period.start || new Date(to).toISOString().slice(0,10)!==period.end_exclusive)return null;
    const format=x=>new Date(x).toLocaleDateString('ru-RU',{timeZone:'UTC'});
    return `${format(from)} — ${format(to-86400000)}`;
  }
  function mount(document,env){
    const panel=document.getElementById('close-timing-research');
    if(!panel)return null;
    const status=document.getElementById('close-timing-status'),result=document.getElementById('close-timing-result');
    function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
    function paragraph(parent,text,cls='muted small'){parent.append(node('p',text,cls));}
    function reportTable(parent,variants,periodKey,reportKey){
      const wrap=node('div',undefined,'close-timing-scroll');
      wrap.tabIndex=0;wrap.setAttribute('role','region');
      wrap.setAttribute('aria-label',`Сравнение времени: ${periodKey==='test'?'первая проверка':'поздний период'}${reportKey==='paired_report'?', общие даты':''}`);
      const table=node('table',undefined,'close-timing-table'),head=node('thead'),headers=node('tr');
      const corner=node('th','Показатель');corner.scope='col';headers.append(corner);
      for(const v of variants){const th=node('th',`${v.cutoff} МСК`);th.scope='col';th.append(node('small',`${v.label} · вход ${v.entry}`));headers.append(th);}
      head.append(headers);table.append(head);
      const body=node('tbody'),periods=variants.map(v=>v[reportKey]?.periods?.[periodKey]);
      const rows=[
        ['Обучение / калибровка',(_,v)=>`${count(v[reportKey]?.trained_rows)?number(v[reportKey].trained_rows,0):'—'} / ${count(v[reportKey]?.calibration_rows)?number(v[reportKey].calibration_rows,0):'—'}`],
        ['Прогнозов с известной целью / дней',p=>`${count(p?.samples)?number(p.samples,0):'—'} / ${count(p?.days)?number(p.days,0):'—'}`],
        ['Всего прогнозов / неизвестных целей',p=>`${count(p?.eligible_forecasts)?number(p.eligible_forecasts,0):'—'} / ${count(p?.unknown_targets)?number(p.unknown_targets,0):'—'}`],
        ['Средняя ошибка модели',p=>p?.samples>0?bps(p.mae_bps):'Нет оценённых прогнозов'],
        ['Ошибка «цена не изменится»',p=>p?.samples>0?bps(p.baseline_mae_bps):'Нет оценённых прогнозов'],
        ['Ошибка «как в среднем на обучении»',p=>p?.samples>0?bps(p.mean_baseline_mae_bps):'Нет оценённых прогнозов'],
        ['Угадано направление по точке',p=>p?.samples>0?`${finite(p.direction_accuracy_pct)?`${number(p.direction_accuracy_pct)}%`:'—'} · n=${count(p.direction_nonzero_samples)?number(p.direction_nonzero_samples,0):'—'}`:'Нет оценённых прогнозов'],
        ['Ширина диапазона / попадания',p=>p?.samples>0?`${bps(p.interval_width_bps)} / ${finite(p.coverage_pct)?`${number(p.coverage_pct)}%`:'—'}`:'Нет оценённых прогнозов'],
        ['Не выбрано направление по диапазону',p=>p?.samples>0 && finite(p.abstention_pct)?`${number(p.abstention_pct)}%`:'—'],
        ['Условных сделок / неизвестных исходов',p=>`${count(p?.paper?.trades)?number(p.paper.trades,0):'—'} / ${count(p?.paper?.unknown_trades)?number(p.paper.unknown_trades,0):'—'}`],
        ['Результат счёта после расходов',p=>tradeResult(p?.paper)],
        ['Результат при удвоенных расходах',p=>tradeResult(p?.paper,true)],
        ['Стресс-сделок / неизвестных исходов',p=>`${count(p?.paper?.stress_trades)?number(p.paper.stress_trades,0):'—'} / ${count(p?.paper?.stress_unknown_trades)?number(p.paper.stress_unknown_trades,0):'—'}`],
        ['«Покупать каждый вечер» после расходов',p=>tradeResult(p?.always_buy)],
      ];
      for(const [label,read] of rows){const row=node('tr'),th=node('th',label);th.scope='row';row.append(th);periods.forEach((p,i)=>row.append(node('td',read(p,variants[i]))));body.append(row);}
      table.append(body);wrap.append(table);parent.append(wrap);
      const ranges=[...new Set(periods.map(dateRange).filter(Boolean))];
      if(ranges.length)paragraph(parent,`Границы проверки: ${ranges.join('; ')}.`);
      for(let i=0;i<periods.length;i++)if(typeof periods[i]?.note==='string')paragraph(parent,`${variants[i].cutoff}: ${periods[i].note}`);
    }
    function reportGroup(parent,variants,key){
      for(const [periodKey,label] of [['test','Первая проверка после обучения'],['later','Более поздний период']]){
        const section=node('section',undefined,'close-timing-period');section.append(node('h3',label));reportTable(section,variants,periodKey,key);parent.append(section);
      }
    }
    function render(value){
      result.replaceChildren();
      if(!valid(value)){status.textContent='Сравнение недоступно или не прошло проверку. Старые результаты вместо него не подставляются.';return;}
      status.textContent='Только историческое исследование. Ни один из трёх вариантов не подключён к текущим сигналам.';
      if(typeof value.notice==='string')paragraph(result,value.notice,'close-timing-warning');
      paragraph(result,'Сначала — все доступные примеры каждого времени. Разное число примеров может менять результат: сравнивать только проценты без количества наблюдений нельзя. Пропуски утренней цены сохраняются в счётчиках, а не превращаются в нулевую прибыль.');
      const variants=ids.map(id=>value.variants.find(v=>v.id===id));
      reportGroup(result,variants,'report');
      const paired=node('details',undefined,'close-timing-paired');paired.append(node('summary','Проверить на общих датах и акциях'));
      paragraph(paired,typeof value.paired_note==='string'?value.paired_note:'Общие даты позволяют сравнивать время при одинаковом наборе входных примеров. Неизвестные исходы не удаляются.');
      paragraph(paired,'Это условное сравнение на пересечении доступных входных данных, не полный результат всех торговых дней. Неизвестные цены входа и утренние исходы остаются неизвестными.');
      reportGroup(paired,variants,'paired_report');result.append(paired);
      paragraph(result,'Меньшая ошибка лучше. Результат условного счёта не равен движению одной акции. Точность направления не показывает вероятность заработка, а диапазон — не гарантия. При отсутствии сделок или неизвестном исходе доходность не определяется.');
      paragraph(result,'Условные покупки проверялись по отдельному порогу точечного прогноза. Строка «не выбрано направление» относится ко всему диапазону ошибки: это разные правила, поэтому условные сделки могут быть даже при неопределённом направлении по диапазону. Рост от момента расчёта также не равен прибыли от более поздней покупки.');
      if(typeof value.note==='string')paragraph(result,value.note,'close-timing-warning');
    }
    async function load(){
      try{const response=await env.fetch('/api/close-timing/',{cache:'no-store',headers:{Accept:'application/json'}});if(!response.ok)throw new Error('unavailable');render(await response.json());}
      catch(_){render(null);}
    }
    load();
    return {render};
  }
  return {mount,valid,tradeResult,dateRange,bps,pct};
});
