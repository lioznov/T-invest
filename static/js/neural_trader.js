(function(root) {
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=(v,d=2)=>Number.isFinite(v)?v.toLocaleString('ru-RU',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  function state(value,mode='live',now=Date.now()) {
    const waiting={status:'waiting',decision:'WAIT',score:null,expected_net_pct:null,plan:null,drivers:[]};
    if(mode==='demo')return {...waiting,message:'Нейросеть ждёт реальные рыночные данные. Учебный график показывает вымышленные цены.'};
    if(value?.status!=='ok')return {...waiting,message:value?.message||'Ожидаем свежие свечи и стакан.'};
    if(!Number.isFinite(Date.parse(value.valid_until)) || Date.parse(value.valid_until)<now)
      return {...waiting,message:'Оценка нейросети устарела. Ждём новую свечу и стакан.'};
    if(!Number.isFinite(value.score) || !Number.isFinite(value.expected_net_pct))
      return {...waiting,message:'Оценка нейросети не прошла проверку.'};
    return value;
  }
  function outcome(r) {
    if(r.unknown_trades)return 'Есть непроверенные исходы';
    return r.trades?`${num(r.return_pct,3)}%`:'Нет сделок';
  }
  function reportMarkup(data) {
    if(data?.status!=='ok')return `<p class="muted">${esc(data?.message||'Обученная нейросеть недоступна.')}</p>`;
    const names={test:'Сентябрь — 2 октября',additional:'3–12 октября'};
    let rows=Object.entries(data.tests).map(([p,e])=>`<tr><td>${esc(names[p]||p)}</td><td>${num(e.base.trades,0)}</td><td>${outcome(e.base)}</td><td>${outcome(e.double_cost)}</td><td>${num(e.base.max_realised_drawdown_pct)}%</td><td>${outcome(e.comparison.linear.base)} / ${outcome(e.comparison.trees.base)}</td></tr>`).join('');
    if(data.later_check?.status==='ok') {
      const e=data.later_check.result;
      rows+=`<tr><td>${esc(data.later_check.label)} · новые даты</td><td>${num(e.base.trades,0)}</td><td>${outcome(e.base)}</td><td>${outcome(e.double_cost)}</td><td>${num(e.base.max_realised_drawdown_pct)}%</td><td>—</td></tr>`;
    }
    return `<p>${esc(data.message)}</p><div class="neural-summary"><span>Обучение: <strong>${num(data.trained_rows,0)} примеров</strong></span><span>Общий капитал: <strong>${num(data.capital,0)} ₽</strong></span><span>Направление: <strong>только покупки</strong></span></div><div class="table-scroll"><table><thead><tr><th>Проверка 2026</th><th>Сделок</th><th>Итог после расходов</th><th>Расходы ×2</th><th>Просадка закрытых сделок</th><th>Прежние модели: линейная / деревья</th></tr></thead><tbody>${rows}</tbody></table></div><p class="research-meta">${esc(data.later_check?.status==='ok'?data.later_check.note:data.later_check?.message||'Проверка новых дат ещё недоступна.')}</p><p class="research-meta">Если не покупать, денежный результат — 0%. Отсутствие сделок не считается доказательством прибыльности. Эти исторические результаты относятся к сохранённым биржевым данным, включая просмотр в учебном режиме.</p><details><summary>Как принимает решение и что проверено</summary><p class="research-meta">20 показателей графика и объёма → два слоя нейронов → оценка вероятности и среднего результата условной покупки на час. Для наблюдения нужны оценка от ${num(data.probability_threshold*100,0)}/100 и ожидаемый результат от ${num(data.minimum_expected_net_pct)}% после обычных расходов. Это расчёты модели, а не обещание роста.</p><p class="research-meta">${esc(data.note)}</p></details>`;
  }
  function readingMarkup(value,mode) {
    const a=state(value,mode);
    if(a.status!=='ok')return `<p class="muted">${esc(a.message)}</p>`;
    return `<div class="ai-reading"><div><span class="muted">Ожидаемый итог</span><strong>${num(a.expected_net_pct)}<small>%</small></strong></div><div><h3>${a.decision==='WATCH'?'Ситуация для бумажного наблюдения':'Подождать с покупкой'}</h3><p class="muted">${esc(a.message)}</p><p class="research-meta">Оценка положительного исхода: ${num(a.score,0)}/100 · срок сценария: один час</p></div></div><div class="ai-drivers">${a.drivers.map(d=>`<span>${d.direction==='up'?'↑':'↓'} ${esc(d.name)}</span>`).join('')}</div>${a.plan?`<p class="research-meta">Условный вход: ${num(a.plan.entry)} ₽ · стоп: ${num(a.plan.stop)} ₽. Размер позиции рассчитай по своему капиталу в калькуляторе риска.</p>`:''}`;
  }
  function watchMarkup(value,mode) {
    const a=state(value,mode);
    return `<div class="watch-ai"><strong>Нейросеть: ${a.status==='ok'?(a.decision==='WATCH'?'наблюдать':'ждать')+' · '+num(a.expected_net_pct)+'%':'ожидание'}</strong><span>${esc(a.message)}</span></div>`;
  }
  const api={state,reportMarkup,readingMarkup,watchMarkup,outcome};
  if(typeof module!=='undefined' && module.exports)module.exports=api;
  else root.VorbyNeural=api;
})(typeof window!=='undefined'?window:globalThis);
