(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object' && module.exports)module.exports=api;
  if(root.document)api.mount(root.document,root);
})(typeof window==='undefined'?globalThis:window,function(){
  'use strict';
  const order=['test_2024','test_2025','test_2026'];
  const finite=value=>typeof value==='number' && Number.isFinite(value);
  const count=value=>Number.isSafeInteger(value) && value>=0;
  function number(value,digits=2){return finite(value)?value.toLocaleString('ru-RU',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—';}
  function pct(value){return finite(value)?`${value>0?'+':''}${number(value)}%`:'—';}
  function bps(value){return finite(value)?`${number(value/100,4)}%`:'—';}
  function price(value,allowNonpositive=false){return finite(value) && (value>0 || allowNonpositive)?`${number(value)} ₽`:'—';}
  function date(value){
    if(typeof value!=='string' || !/^\d{4}-\d\d-\d\dT.+(?:Z|[+-]\d\d:\d\d)$/.test(value))return '—';
    const time=Date.parse(value);return Number.isFinite(time)?`${new Date(time).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})} МСК`:'—';
  }
  function range(value){
    const parse=v=>typeof v==='string' && /^\d{4}-\d\d-\d\d$/.test(v)?Date.parse(`${v}T00:00:00Z`):NaN;
    const start=parse(value?.start),end=parse(value?.end_exclusive);
    if(!Number.isFinite(start)||!Number.isFinite(end)||start>=end || new Date(start).toISOString().slice(0,10)!==value.start || new Date(end).toISOString().slice(0,10)!==value.end_exclusive)return '—';
    const format=x=>new Date(x).toLocaleDateString('ru-RU',{timeZone:'UTC'});
    return `${format(start)} — ${format(end-86400000)}`;
  }
  function tradeResult(paper,stress=false){
    if(!paper)return 'Нет расчёта';
    const unknown=stress?paper.stress_unknown_trades:paper.unknown_trades,trades=stress?paper.stress_trades:paper.trades;
    if(paper.status==='incomplete' || !count(paper.unknown_trades) || paper.unknown_trades>0 || !count(unknown) || unknown>0)return 'Неполные данные · доходность неизвестна';
    if(!count(trades) || trades===0)return 'Нет сделок · доходность не определена';
    return pct(stress?paper.stress_return_pct:paper.net_return_pct);
  }
  function valid(value){
    if(value?.status!=='historical' || value.entry_allowed!==false || !Array.isArray(value.folds) || value.folds.length!==3)return false;
    const names=new Set();
    for(const f of value.folds){
      if(!f || !order.includes(f.id) || names.has(f.id) || typeof f.label!=='string'
          || !f.report || !f.current_year_report || !f.stocks || !f.current_year_stocks
          || !Array.isArray(f.examples) || !Array.isArray(f.current_year_examples)
          || ['train','calibration','test'].some(k=>range(f.periods?.[k])==='—'))return false;
      names.add(f.id);
    }
    return true;
  }
  function mount(document,env){
    if(!document.getElementById('temporal-validation'))return null;
    const status=document.getElementById('temporal-status'),result=document.getElementById('temporal-result');
    function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
    function paragraph(parent,text,cls='muted small'){parent.append(node('p',text,cls));}
    function hint(parent){paragraph(parent,'Если столбцы не помещаются, прокрути таблицу вправо →.','temporal-hint');}
    function table(parent,headers,rows,cls=''){
      hint(parent);const scroll=node('div',undefined,'temporal-scroll');scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label',headers.join(' · '));
      const table=node('table',undefined,`temporal-table${cls?` ${cls}`:''}`),head=node('thead'),top=node('tr');
      for(const header of headers){const th=node('th',header);th.scope='col';top.append(th);}head.append(top);table.append(head);
      const body=node('tbody');for(const values of rows){const row=node('tr');values.forEach((text,i)=>{const cell=node(i===0?'th':'td',text);if(i===0)cell.scope='row';row.append(cell);});body.append(row);}
      table.append(body);scroll.append(table);parent.append(scroll);
    }
    function cards(parent,folds){
      const grid=node('div',undefined,'temporal-cards');
      for(const fold of folds){const card=node('article',undefined,'temporal-card');card.append(node('h3',fold.label));const list=node('dl');
        for(const [key,label] of [['train','1. Обучение'],['calibration','2. Калибровка диапазона'],['test','3. Основная проверка']]){const line=node('div');line.append(node('dt',label),node('dd',range(fold.periods[key])));list.append(line);}
        card.append(list);paragraph(card,`Обучающих примеров: ${count(fold.trained_rows)?number(fold.trained_rows,0):'—'}; калибровочных: ${count(fold.calibration_rows)?number(fold.calibration_rows,0):'—'}.`);grid.append(card);}
      parent.append(grid);
    }
    function reports(parent,folds,current){
      const group=node('section',undefined,'temporal-group');group.append(node('h3',current?'Все три модели на одних примерах 2026 года':'Основные проверки на последующих годах'));
      paragraph(group,current?'Одинаковые даты и акции: 01.01.2026 — 22.10.2026, без переобучения и повторной калибровки. Для модели с основной проверкой 2026 это повтор той же проверки, не ещё один независимый тест.':'Здесь разные годы: 2024, 2025 и часть 2026. Различие результатов не доказывает, что третья модель лучше: менялись и рынок, и история обучения.');
      const values=folds.map(f=>current?f.current_year_report:f.report);
      const rows=[
        ['Проверяемые даты',(_,f)=>current?'01.01.2026 — 22.10.2026':range(f.periods.test)],
        ['Оценённых прогнозов / дней',p=>`${count(p.samples)?number(p.samples,0):'—'} / ${count(p.days)?number(p.days,0):'—'}`],
        ['Всего прогнозов / неизвестных целей',p=>`${count(p.eligible_forecasts)?number(p.eligible_forecasts,0):'—'} / ${count(p.unknown_targets)?number(p.unknown_targets,0):'—'}`],
        ['MAE · средняя абсолютная ошибка',p=>bps(p.mae_bps)],
        ['MAE · «цена не изменится»',p=>bps(p.baseline_mae_bps)],
        ['MAE · среднее движение на обучении',p=>bps(p.mean_baseline_mae_bps)],
        ['RMSE · сильнее штрафует большие ошибки',p=>bps(p.rmse_bps)],
        ['RMSE · «цена не изменится»',p=>bps(p.baseline_rmse_bps)],
        ['RMSE · среднее движение на обучении',p=>bps(p.mean_baseline_rmse_bps)],
        ['MAPE · ошибка относительно фактической цены',p=>finite(p.mape_pct)?`${number(p.mape_pct,4)}%`:'—'],
        ['MAPE · «цена не изменится»',p=>finite(p.baseline_mape_pct)?`${number(p.baseline_mape_pct,4)}%`:'—'],
        ['MAPE · среднее движение на обучении',p=>finite(p.mean_baseline_mape_pct)?`${number(p.mean_baseline_mape_pct,4)}%`:'—'],
        ['Среднее смещение ошибки со знаком',p=>bps(p.mean_signed_error_bps)],
        ['Угадано направление моделью по точке',p=>`${finite(p.direction_accuracy_pct)?`${number(p.direction_accuracy_pct)}%`:'—'} · n=${count(p.direction_nonzero_samples)?number(p.direction_nonzero_samples,0):'—'}`],
        ['Фиксированный прогноз направления',p=>`${typeof p.direction_baseline_label==='string'?p.direction_baseline_label:'—'} · ${finite(p.direction_baseline_pct)?`${number(p.direction_baseline_pct)}%`:'—'}`],
        ['Фактически рост / падение / без изменения',p=>['actual_up','actual_down','actual_zero'].map(k=>count(p[k])?number(p[k],0):'—').join(' / ')],
        ['Ширина диапазона / фактические попадания',p=>`${bps(p.interval_width_bps)} / ${finite(p.coverage_pct)?`${number(p.coverage_pct)}%`:'—'}`],
        ['Не выбрано направление по диапазону',p=>finite(p.abstention_pct)?`${number(p.abstention_pct)}%`:'—'],
        ['Выбрано кандидатов по порогу',p=>count(p.paper?.selected_candidates)?number(p.paper.selected_candidates,0):'—'],
        ['Условных сделок / неизвестных исходов',p=>`${count(p.paper?.trades)?number(p.paper.trades,0):'—'} / ${count(p.paper?.unknown_trades)?number(p.paper.unknown_trades,0):'—'}`],
        ['Счёт после обычных расходов',p=>tradeResult(p.paper)],
        ['Счёт при удвоенных расходах',p=>tradeResult(p.paper,true)],
        ['Стресс-сделок / неизвестных исходов',p=>`${count(p.paper?.stress_trades)?number(p.paper.stress_trades,0):'—'} / ${count(p.paper?.stress_unknown_trades)?number(p.paper.stress_unknown_trades,0):'—'}`],
        ['«Покупать каждый вечер» после расходов',p=>tradeResult(p.always_buy)],
      ].map(([label,read])=>[label,...values.map((p,i)=>read(p,folds[i]))]);
      const headers=folds.map((f,i)=>current?`Модель ${['A','B','C'][i]} · обучение до ${f.periods.train.end_exclusive.slice(0,4)}-го`:f.label);
      table(group,['Показатель',...headers],rows);
      paragraph(group,'MAE и RMSE здесь выражены в процентах движения от вечерней цены, MAPE — от фактической утренней цены. Меньше ошибка — лучше. Простые сравнения: цена останется прежней либо повторит среднее движение обучающего периода. Направление базового прогноза выбирается один раз по знаку среднего движения на обучении, а не по ответам теста.');
      paragraph(group,'Условный счёт 100 000 ₽, не реальные заработанные деньги. Неизвестный исход не превращается в нулевой убыток: при неполной проверке доходность не определяется. Условные покупки по порогу точечной оценки и выбор направления по всему диапазону — разные правила; угаданное направление не гарантирует прибыль.');
      paragraph(group,'Допущения условных покупок: точечный прогноз роста от 17:45 выше 0,4%; покупка в 18:00, выход следующего дня в 10:00; не более 25% счёта в одной акции и только целые лоты. С каждой стороны комиссия 0,05% и проскальзывание 0,05%; стресс-проверка удваивает оба расхода. Выбранный кандидат не равен исполненной сделке.');
      for(let i=0;i<values.length;i++)if(typeof values[i].note==='string')paragraph(group,`${folds[i].label}: ${values[i].note}`);
      parent.append(group);
    }
    function details(parent,folds,current){
      const detail=node('details',undefined,'temporal-detail');detail.append(node('summary',current?'Примеры и ошибки каждой модели на общих датах 2026':'Примеры прогнозов и ошибки по акциям в основных проверках'));
      paragraph(detail,'Фактическая цена — ответ для проверки, модель не видит её при прогнозе. Примеры заранее определены правилом: последние SBER и максимальная относительная ошибка, не лучшие сделки. Это ретроспектива, не заранее записанные прогнозы.');
      for(const fold of folds){
        detail.append(node('h4',fold.label));const stocks=current?fold.current_year_stocks:fold.stocks,examples=current?fold.current_year_examples:fold.examples;
        const stockRows=Object.entries(stocks).map(([ticker,s])=>[ticker,count(s.samples)?number(s.samples,0):'—',...['mae_rub','baseline_mae_rub','mean_baseline_mae_rub','rmse_rub','baseline_rmse_rub','mean_baseline_rmse_rub'].map(k=>price(s[k],true)),...['mape_pct','baseline_mape_pct','mean_baseline_mape_pct'].map(k=>finite(s[k])?`${number(s[k],4)}%`:'—')]);
        table(detail,['Акция','Прогнозов','MAE, ₽','MAE без изменения, ₽','MAE среднее train, ₽','RMSE, ₽','RMSE без изменения, ₽','RMSE среднее train, ₽','MAPE','MAPE без изменения','MAPE среднее train'],stockRows,'temporal-stocks');
        if(!examples.length){paragraph(detail,'Примеров с известным исходом нет. Цены не подставляются.');continue;}
        const rows=examples.map(e=>[e.ticker,date(e.as_of),date(e.target_at),price(e.anchor_price),price(e.predicted_price,true),price(e.actual_price),price(e.absolute_error_rub,true),finite(e.absolute_error_pct)?`${number(e.absolute_error_pct,4)}%`:'—',e.kind==='largest_error'?'Максимальная ошибка':'Последний пример']);
        table(detail,['Акция','Данные на','Утро','Вечерняя цена','Прогноз','Фактическая цена','Ошибка, ₽','Ошибка, %','Почему показан'],rows,'temporal-examples');
      }
      parent.append(detail);
    }
    function coverage(parent,value){
      const detail=node('details',undefined,'temporal-detail');detail.append(node('summary','Фактическая история по акциям'));
      if(!value || typeof value!=='object'){paragraph(detail,'Проверенного покрытия нет.');parent.append(detail);return;}
      for(const [ticker,row] of Object.entries(value))paragraph(detail,`${ticker}: ${count(row.bars)?number(row.bars,0):'—'} свечей. Фактически ${date(row.first_actual)} — ${date(row.last_actual)}; запрошено ${date(row.requested_from)} — ${date(row.to)} (правая граница не включается).`);
      parent.append(detail);
    }
    function render(value){
      result.replaceChildren();
      if(!valid(value)){status.textContent='Отчёт недоступен или не прошёл проверку. Метрики и примеры цен не подставляются.';return;}
      status.textContent='Только ретроспективная проверка, не живые сигналы. Лучший вариант автоматически не выбирается.';
      if(typeof value.notice==='string')paragraph(result,value.notice,'temporal-warning');
      if(typeof value.publication_note==='string')paragraph(result,value.publication_note,'temporal-warning');
      const folds=order.map(id=>value.folds.find(f=>f.id===id));cards(result,folds);reports(result,folds,false);reports(result,folds,true);details(result,folds,false);details(result,folds,true);coverage(result,value.coverage);
      if(typeof value.note==='string')paragraph(result,value.note,'temporal-warning');
    }
    async function load(){try{const response=await env.fetch('/api/temporal-validation/',{cache:'no-store',headers:{Accept:'application/json'}});if(!response.ok)throw new Error('unavailable');render(await response.json());}catch(_){render(null);}}
    load();return {render};
  }
  return {mount,valid,range,date,price,pct,bps,tradeResult};
});
