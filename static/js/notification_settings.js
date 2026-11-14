(() => {
  'use strict';
  const panel=document.querySelector('[data-notification-mode]');if(!panel)return;
  const mode=panel.dataset.notificationMode,analysis=mode.startsWith('analysis-'),multi=mode.endsWith('-multi');
  const url='/api/notifications/?mode='+encodeURIComponent(mode),find=s=>panel.querySelector(s);
  let last=null,busy=false,syncFailedUntil=0,reachable=false,loadFailed=false;
  const rows=['vk','telegram'].map(channel=>({channel,node:find(`[data-channel="${channel}"]`)}));
  const by=(row,selector)=>row.node.querySelector(selector);
  function selected(){
    if(!analysis)return null;
    if(!multi){const text=document.getElementById('ticker-label')?.textContent || '';const ticker=text.split(' ·')[0].trim().toUpperCase();return /^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(ticker)?[ticker]:null;}
    try{const value=JSON.parse(localStorage.getItem('vorby.watchlist'));if(Array.isArray(value))return [...new Set(value.filter(t=>typeof t==='string'&&/^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(t)))].slice(0,20); }catch{}
    const cards=[...document.querySelectorAll('#watch-grid [data-remove]')];return cards.length?cards.map(e=>e.dataset.remove):null;
  }
  async function request(options={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
    try{const r=await fetch(url,{...options,cache:'no-store',signal:controller.signal});const v=await r.json();if(!r.ok)throw Error(v.message || 'Настройки недоступны.');return v;}
    finally{clearTimeout(timer);}
  }
  function post(body){return request({method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':find('[name=csrfmiddlewaretoken]').value},body:JSON.stringify(body)});}
  function lock(){for(const row of rows){const v=last?.channels[row.channel];by(row,'input').disabled=busy || !reachable || !v || !v.configured&&!v.enabled;by(row,'[data-channel-test]').disabled=busy || !reachable || !v?.configured;}}
  function render(value){
    last=value;let count=0;
    for(const row of rows){const v=value.channels[row.channel];count+=Number(v.enabled);by(row,'input').checked=v.enabled;
      by(row,'[data-channel-state]').textContent=v.enabled?'Включён для этого режима':'Выключен для этого режима';
      by(row,'[data-channel-connection]').textContent=v.configured?'Ключи указаны. Доступ проверяется отправкой.':v.setup;
      const delivery=by(row,'[data-channel-delivery]');delivery.dataset.result=v.result || '';
      delivery.textContent=(v.result==='rejected'?'Ошибка отправки. ':v.result==='unknown'?'Доставка не подтверждена. ':v.result==='ok'?'Последний известный ответ сервиса: ':'')+(v.message || 'Сообщений пока не было.');
    }
    find('[data-channel-count]').textContent=`${count} из 2 каналов`;
    if(analysis){const tickers=selected(),values=Object.values(value.channels),collector=values[0].collector||{};
      find('[data-selection]').textContent=tickers===null?'Ждём выбранную акцию или список.':tickers.length?'Выбрано: '+tickers.join(', ')+'. Включённые каналы следуют за выбором на этой странице.':'Список пуст. Для уведомлений выбери акции.';
      const chosen=new Set(values.filter(v=>v.enabled).flatMap(v=>v.tickers)),active=(collector.active_tickers||[]).filter(t=>chosen.has(t));
      find('[data-monitor-status]').textContent=`${collector.alive?'Сборщик активен: '+active.length+' из '+chosen.size+' выбранных акций сейчас в сборе.':'Живой сборщик не подтверждён. Без стакана и ленты сигналы заблокированы.'} ${values.some(v=>v.enabled&&v.worker_running)?'Фоновая проверка анализа работает.':'Фоновая проверка пока не подтверждена.'} Это не подтверждение доставки. Сервер и отдельный сборщик должны работать.`;
    }
    lock();
  }
  async function syncSelection(){
    if(!analysis||!document.body.classList.contains('focus-ready')||Date.now()<syncFailedUntil)return;
    const tickers=selected();if(tickers===null)return;
    for(const row of rows){const v=last.channels[row.channel];if(v.enabled && JSON.stringify(v.tickers)!==JSON.stringify(tickers))render(await post({channel:row.channel,enabled:true,tickers}));}
  }
  async function load(){
    if(busy)return;busy=true;lock();
    try{
      const value=await request();reachable=true;render(value);
      if(!analysis){const v=last.channels.telegram,key=multi?'investpro_multibot_enabled':'investpro_bot_enabled';
        let legacy=false;try{legacy=localStorage.getItem(key)==='true';}catch{}
        if(v.revision===0 && !v.enabled && v.configured && legacy)render(await post({channel:'telegram',enabled:true}));
      }
      await syncSelection();
      if(loadFailed){find('[data-settings-status]').textContent='Связь восстановлена. Состояние каналов обновлено.';loadFailed=false;}
    }catch(error){loadFailed=true;reachable=false;syncFailedUntil=Date.now()+30000;find('[data-settings-status]').textContent=error.message+' Состояние отправки не подтверждено.';}
    finally{busy=false;lock();}
  }
  for(const row of rows){
    by(row,'input').addEventListener('change',async()=>{
      if(busy)return;const enabled=by(row,'input').checked;busy=true;lock();
      try{const tickers=selected();if(analysis&&tickers===null)throw Error('Дождись загрузки выбранной акции или списка.');
        render(await post({channel:row.channel,enabled,...(analysis?{tickers}:{})}));
        if(!analysis&&row.channel==='telegram')try{localStorage.setItem(multi?'investpro_multibot_enabled':'investpro_bot_enabled',String(enabled));}catch{}
        find('[data-settings-status]').textContent='Настройка сохранена. Смотри ответ отправки у каждого канала.';
      }catch(error){if(last)render(last);find('[data-settings-status]').textContent=error.message;}
      finally{busy=false;await load();}
    });
    by(row,'[data-channel-test]').addEventListener('click',async()=>{
      if(busy)return;busy=true;lock();const result=by(row,'[data-channel-test-status]');result.textContent='Отправляем тест…';
      try{const value=await post({channel:row.channel,action:'test'});render(value);result.textContent=value.test?.message || 'Результат теста не подтверждён.';}
      catch(error){result.textContent=error.message+' Автоматического повтора нет.';}
      finally{busy=false;lock();}
    });
  }
  load();setInterval(load,5000);
})();
