(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.VorbyResearchHelper=api;})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const unavailable=message=>({headline:'Помощник ожидает данные',action:'WAIT',entry_allowed:false,confidence:null,
    data_review:{state:'unavailable',message},signal_review:{message:'Условия пока не проверены.'},
    neural_review:{message:'Свежая оценка нейросети недоступна.'},risk_review:{message:'Сценарий входа пока не подтверждён.'},
    research_review:{forward_note:'Проверка следующих котировок пока недоступна.',paper:{message:'Журнал исходов пока недоступен.'}},next_steps:[]});
  function state(value,mode,now=Date.now()){
    if(!value||value.version!=='trading_research_helper_v1'||value.entry_allowed!==false||value.confidence!==null)
      return unavailable('Получаем проверенный разбор с сервера.');
    if(mode!=='live')return {...unavailable('Учебный график использует вымышленные цены.'),headline:'Учебный разбор'};
    const fresh=value.data_review?.state==='fresh_live',until=Date.parse(value.data_review?.valid_until),created=Date.parse(value.created_at);
    if(fresh&&(!Number.isFinite(until)||!Number.isFinite(created)||created>now||now>until))
      return {...unavailable('Оценка устарела. Нужен новый снимок рынка.'),headline:'Ожидаем свежие данные'};
    if(!fresh)return {...value,action:'WAIT',risk_review:{message:'Для сценария нужны свежие подтверждённые данные.'},
      neural_review:{message:'Свежая проверенная оценка нейросети пока недоступна.'},
      signal_review:{message:'Прошлая оценка условий не подтверждает текущий вход.'}};
    return value;
  }
  function markup(value,mode,now=Date.now()){
    const h=state(value,mode,now),e=h.research_review||{},r=h.risk_review||{};
    const cards=[['Данные',h.data_review?.message],['Условия',h.signal_review?.message],['Нейросеть',h.neural_review?.message]];
    return `<div class="helper-headline">${esc(h.headline)}</div><div class="helper-grid">${cards.map(([title,text])=>`<div><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`).join('')}</div>`+
      `<p class="helper-risk">${esc(r.message)}</p><details class="helper-evidence"><summary>Что подтверждено наблюдениями</summary><p>${esc(e.forward_note)}</p><p>${esc(e.paper?.message)}</p><p>${esc(e.historical_model_note)}</p></details>`+
      `${Array.isArray(h.next_steps)&&h.next_steps.length?`<ul class="helper-steps">${h.next_steps.map(t=>`<li>${esc(t)}</li>`).join('')}</ul>`:''}`;
  }
  return {state,markup};
});
