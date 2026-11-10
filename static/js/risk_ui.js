/* Presentation of the existing server calculation; no sizing or trade decisions here. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.VorbyRiskUI=api;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=(value,digits=2)=>value.toLocaleString('ru-RU',{minimumFractionDigits:digits,maximumFractionDigits:digits});
  const money=value=>num(value)+' ₽';
  function budget(capital,risk){
    if(!['number','string'].includes(typeof capital)||!['number','string'].includes(typeof risk))return null;
    if(String(capital).trim()===''||String(risk).trim()==='')return null;
    const c=Number(capital),r=Number(risk);
    return Number.isFinite(c)&&Number.isFinite(r)&&c>=1&&c<=1e10&&r>=.01&&r<=5?c*r/100:null;
  }
  function markup(data,ticker,mode){
    const unavailable='<p class="research-meta">Расчёт пока недоступен. Проверь поля и попробуй ещё раз.</p>';
    if(!data||data.status!=='ok')return data?.message?`<p class="research-meta">${esc(data.message)}</p>`:unavailable;
    const values=['position_cost','planned_loss','risk_budget','allocation_budget','expected_target_profit','effective_risk_pct'];
    if(!values.every(key=>Number.isFinite(data[key]))||!['lots','shares'].every(key=>Number.isSafeInteger(data[key])&&data[key]>=0)
      ||data.position_cost<0||data.planned_loss<0||data.risk_budget<0||data.allocation_budget<0
      ||(data.lots>0&&(data.shares===0||data.position_cost===0))
      ||(data.lots===0&&(data.shares!==0||data.position_cost!==0||data.planned_loss!==0||data.expected_target_profit!==0)))return unavailable;
    const assumptions=Array.isArray(data.assumptions)?data.assumptions.join(' '):data.assumptions;
    const ratio=Number.isFinite(data.reward_risk)?num(data.reward_risk)+'×':'—';
    const example=mode==='demo'?'<span class="badge">Учебные цены</span>':'';
    const heading=data.lots?`<span class="risk-answer-caption">В расчёт помещается · ${esc(ticker)}</span><strong>${num(data.shares,0)} <small>акций</small></strong><span>${num(data.lots,0)} лот. · сумма ограничена твоими настройками</span>`
      :'<span class="risk-answer-caption">По этим условиям</span><strong class="risk-empty-title">Один лот не помещается</strong><span>Бюджета риска или доли капитала недостаточно.</span>';
    const amounts=data.lots?`<div class="risk-answer-amounts"><div><span>На покупку</span><strong>${money(data.position_cost)}</strong></div><div><span>Убыток при выходе по стопу</span><strong class="negative">${money(data.planned_loss)}</strong></div><div><span>Результат при достижении цели</span><strong class="${data.expected_target_profit>0?'positive':data.expected_target_profit<0?'negative':''}">${money(data.expected_target_profit)}</strong></div></div>`:'';
    return `<div class="risk-answer"><div class="risk-answer-main"><div>${heading}</div>${example}</div>${amounts}<p class="risk-answer-note">Это расчёт по указанным ценам, а не команда покупать. Выход хуже стопа может увеличить убыток.</p></div>`+
      `<details class="risk-answer-details"><summary>Лимиты и допущения расчёта</summary><dl><div><dt>Бюджет риска</dt><dd>${money(data.risk_budget)}</dd></div><div><dt>Лимит суммы в акции</dt><dd>${money(data.allocation_budget)}</dd></div><div><dt>Расчётный риск от капитала</dt><dd>${num(data.effective_risk_pct)}%</dd></div><div><dt>Результат к риску после расходов</dt><dd>${ratio}</dd></div></dl><p>${esc(data.message)}</p><p>${esc(assumptions)}</p></details>`;
  }
  function bind(form,doc){
    if(!form)return;
    const amount=doc.getElementById('risk-budget-amount');
    function update(){
      const value=budget(doc.getElementById('risk-capital').value,doc.getElementById('risk-percent').value);
      if(amount)amount.textContent=value===null?'—':money(value);
    }
    // Hidden required fields must become visible before native browser validation focuses them.
    form.addEventListener('invalid',event=>{
      let parent=event.target.parentElement;
      while(parent&&parent!==form){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}
    },true);
    form.addEventListener('input',update);
    update();
  }
  return {budget,markup,bind};
});
