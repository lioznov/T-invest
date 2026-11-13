(() => {
  'use strict';
  const panel=document.querySelector('[data-vk-radar]');
  if(!panel)return;
  const toggle=document.getElementById('radarVkToggle'),status=document.getElementById('radarVkStatus');
  const url='/api/radar-vk/?scope='+encodeURIComponent(panel.dataset.vkRadar);
  let busy=false;
  async function request(options={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch(url,{...options,cache:'no-store',signal:controller.signal});
      const value=await response.json();
      if(!response.ok)throw Error(value.message || 'Не удалось сохранить настройки ВК.');
      return value;
    }finally{clearTimeout(timer);}
  }
  function render(value){
    toggle.checked=value.enabled;toggle.disabled=!value.configured && !value.enabled;
    const delivery=value.result==='rejected'?'Сообщение не отправлено.':value.result==='unknown'?'Доставка ВК не подтверждена.':'Уведомления ВК выбраны.';
    status.textContent=!value.configured?'ВК не настроен. Добавь ключ и свой ID в .env и перезапусти сайт. Включённую галочку можно отключить.':
      `${value.enabled?delivery:'Уведомления ВК выключены.'} ${value.message} Только твой личный диалог.`;
  }
  async function load(){
    if(busy)return;
    busy=true;
    try{render(await request());}
    catch{toggle.disabled=true;status.textContent='Статус ВК недоступен. Отправка не подтверждена.';}
    finally{busy=false;}
  }
  toggle.addEventListener('change',async()=>{
    if(busy)return;
    busy=true;toggle.disabled=true;status.textContent='Сохраняем и отправляем подтверждение в личный диалог ВК…';
    try{render(await request({method:'POST',headers:{'Content-Type':'application/json',
      'X-CSRFToken':document.querySelector('#radarVkCsrf [name=csrfmiddlewaretoken]').value},body:JSON.stringify({enabled:toggle.checked})}));}
    catch(error){status.textContent=error.message+' Проверяем сохранённое состояние…';}
    finally{busy=false;await load();}
  });
  load();setInterval(load,10000);
})();
