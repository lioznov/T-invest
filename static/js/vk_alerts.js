(() => {
  'use strict';
  const form=document.getElementById('vk-form');
  if(!form)return;
  const byId=id=>document.getElementById(id);
  const badge=byId('vk-badge'), status=byId('vk-status'), items=byId('vk-items');
  const tickers=byId('vk-tickers'), enabled=byId('vk-enabled'), save=byId('vk-save');
  let initialized=false,busy=false,visibleCount=50,lastValue=null;
  const testButton=byId('vk-test'),testStatus=byId('vk-test-status');
  const date=value=>value?new Date(value*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК':'—';
  async function request(options={}) {
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
    try {
      const response=await fetch('/api/vk-alerts/',{...options,cache:'no-store',signal:controller.signal});
      const value=await response.json();
      if(!response.ok)throw new Error(value.message || 'Настройки недоступны.');
      return value;
    } finally {clearTimeout(timer);}
  }
  function render(value,replaceForm=false) {
    lastValue=value;
    if(!initialized || replaceForm){tickers.value=value.tickers.join(', ');enabled.checked=value.enabled;initialized=true;}
    badge.textContent=!value.configured?'Бот не настроен':!value.enabled?'Выключено':value.worker_running?'Наблюдение включено':'Ждём запуска';
    byId('vk-collector-command').textContent='python manage.py watch_vk --seconds 28800';
    status.textContent=!value.configured
      ? 'Нужны VK_COMMUNITY_TOKEN и VK_USER_ID в серверном .env. После изменения перезапусти сервер. Секреты на сайте не показываются.'
      : !value.enabled?'Настройки сохранены. Уведомления выключены.'
      : `${value.worker_running?'Фоновая проверка работает':'Свежая работа фоновой проверки пока не подтверждена'}. ${value.message || ''} Это не подтверждение доставки или наличия сигнала.`;
    items.replaceChildren();
    const collector=value.collector || {}, active=new Set(collector.active_tickers || []), unsupported=new Set(collector.unsupported_tickers || []);
    const selectedActive=value.tickers.filter(t=>active.has(t)).length;
    byId('vk-coverage').textContent=`Сохранено тикеров: ${value.tickers.length}. ${collector.alive?`Сейчас в сборе: ${selectedActive}${collector.batch_count?` · группа ${collector.batch}/${collector.batch_count}`:''}`:'Живой сборщик не подтверждён'}. Анализ очередью, до 3 акций одновременно. ${value.tickers.length>50?'Группы до 50 меняются раз в 15 минут; вне группы сигналов нет.':''}`;
    const indexed=new Map(value.items.map(row=>[row.ticker,row]));
    const states={LONG:'Кандидат покупки',WAIT:'Ожидание',EXIT:'Условия покупки не выполнены',UNAVAILABLE:'Вход заблокирован'};
    for(const ticker of value.tickers.slice(0,visibleCount)){
      const item=indexed.get(ticker);
      const age=item?Date.now()/1000-item.checked:Infinity;
      const card=document.createElement('div');card.className='telegram-item';
      const title=document.createElement('strong');title.textContent=ticker;
      const text=document.createElement('p');text.textContent=unsupported.has(ticker)?'Акция TQBR не найдена сборщиком. Проверь тикер.':!active.has(ticker)?'Сейчас вне активного сбора. Сигнал покупки не проверяется.':item && age>=0 && age<=30?`${states[item.decision] || 'Ожидание'} · ${item.detail}`:'В активной группе. Ждём свежей проверки очередью; старая оценка не является текущим сигналом.';
      const time=document.createElement('p');time.className='muted';time.textContent=item?`Проверено: ${date(item.checked)}${item.last_sent?` · Отправка/попытка: ${date(item.last_sent)}`:''}`:'';
      const delivery=document.createElement('p');delivery.className='muted';delivery.textContent=item?.delivery_detail || '';
      card.append(title,text,time,delivery);items.append(card);
    }
    byId('vk-more').hidden=value.tickers.length<=visibleCount;
    save.disabled=false;testButton.disabled=!value.configured;
  }
  async function load(){
    if(busy)return;
    try{render(await request());}catch{badge.textContent='Связь не подтверждена';status.textContent='Не удалось прочитать настройки. Работа уведомлений не подтверждена. Повторим проверку.';}
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;
    const list=[...new Set(tickers.value.toUpperCase().split(/[,;\s]+/).filter(Boolean))];
    if(!list.length || list.some(t=>!/^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(t))){status.textContent='Введи тикеры через запятую или с новой строки, например SBER, GAZP.';return;}
    busy=true;save.disabled=true;
    try{
      render(await request({method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value},
        body:JSON.stringify({enabled:enabled.checked,tickers:list})}),true);
    }catch(error){status.textContent=error.message || 'Не удалось сохранить. Проверь состояние перед повтором.';}
    finally{busy=false;save.disabled=!initialized;}
  });
  byId('vk-more').addEventListener('click',()=>{visibleCount+=50;if(lastValue)render(lastValue);});
  testButton.addEventListener('click',async()=>{
    if(busy||!lastValue?.configured)return;
    busy=true;testButton.disabled=true;save.disabled=true;testStatus.textContent='Отправляем тестовое сообщение на получателя из .env…';
    try{
      const value=await request({method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value},body:JSON.stringify({action:'test'})});
      testStatus.textContent=value.test?.message || 'Ответ не подтверждён. Проверь ВК перед повтором.';
    }catch(error){testStatus.textContent=error.message || 'Результат отправки неизвестен. Проверь ВК перед повтором.';}
    finally{busy=false;testButton.disabled=!lastValue?.configured;save.disabled=!initialized;}
  });
  load();setInterval(load,10000);
})();
