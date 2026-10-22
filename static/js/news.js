(function(root){
  'use strict';
  const TOPICS={dividends:'Дивиденды',results:'Отчётность',rates:'Ставка и инфляция',sanctions:'Санкции',trading:'Торги и листинг',commodities:'Сырьё',macro:'Экономика',other:'Другие новости'};
  const TICKERS=['SBER','GAZP','LKOH','NVTK','GMKN','ROSN','YDEX'];
  function safeLink(value){
    try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&(!u.port||u.port==='443')?u.href:null;}catch{return null;}
  }
  function percent(value){return typeof value==='number'&&Number.isFinite(value)?(value>0?'+':'')+value.toFixed(2)+'%':'—';}
  if(typeof module==='object'&&module.exports)module.exports={safeLink,TOPICS,percent,TICKERS};
  if(!root.document)return;
  const $=id=>document.getElementById(id),list=$('news-items')||$('stock-news-items');
  if(!list)return;
  const mini=!$('news-filters'),status=$('news-status')||$('stock-news-status');
  const fields=[['ticker','news-ticker'],['source','news-source'],['topic','news-topic'],['q','news-query'],['tone','news-tone'],['kind','news-kind']];
  const date=value=>{if(!value)return 'не указано';const d=new Date(value);return Number.isNaN(d.getTime())?'не указано':d.toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' МСК';};
  const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  function link(text,url,cls){const n=node('a',text,cls),safe=safeLink(url);if(safe){n.href=safe;n.target='_blank';n.rel='noopener noreferrer';}return n;}
  function credit(item,parent){
    if(item.author)parent.append(node('span','Автор: '+item.author+'. '));
    if(item.license_url)parent.append(link('Лицензия CC BY 4.0',item.license_url),node('span',' · Группировка и метки добавлены Vorby.'));
  }
  async function json(url,signal){
    const response=await fetch(url,{signal,cache:'no-store'});
    if(!response.ok)throw new Error('HTTP '+response.status);
    return response.json();
  }
  function marketReaction(item,parent){
    const actions=node('div',undefined,'news-actions'),select=node('select');
    select.setAttribute('aria-label','Акция для сравнения реакции цены');
    const placeholder=node('option','Выбери акцию');placeholder.value='';select.append(placeholder);
    for(const ticker of TICKERS){const option=node('option',ticker);option.value=ticker;select.append(option);}
    select.value=(item.tickers||[]).find(t=>TICKERS.includes(t))||(TICKERS.includes($('news-ticker').value)?$('news-ticker').value:'');
    const button=node('button','Посмотреть реакцию цены','button ghost');button.type='button';button.disabled=!select.value;select.addEventListener('change',()=>{button.disabled=!select.value;});
    actions.append(select,button);parent.append(actions);
    const result=node('div',undefined,'news-reaction');result.hidden=true;parent.append(result);
    button.addEventListener('click',async()=>{
      result.hidden=false;result.textContent='Читаем наблюдения. Если свечей ещё нет, загрузка начнётся в фоне…';button.disabled=true;
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
      try{
        const data=await json('/api/news/reaction/?'+new URLSearchParams({id:item.id,ticker:select.value}),controller.signal);
        if(data.status!=='ok'||!data.reaction)throw new Error('Reaction unavailable');
        result.replaceChildren(node('strong',data.ticker+' · изменение после первого получения'));
        result.append(node('p','Исходная версия: '+data.original_title),node('p','Впервые увидели: '+date(data.observed_at)));
        const grid=node('div',undefined,'news-reaction-grid');
        for(const row of data.reaction.horizons){
          const cell=node('div');cell.append(node('span',row.label));
          cell.append(node('strong',row.status==='ok'?percent(row.change_pct):row.status==='pending'?'Ожидаем':'Нет данных',row.change_pct<0?'negative':row.change_pct>0?'positive':''));
          cell.append(node('small',row.status==='ok'?'С условными расходами: '+percent(row.after_assumed_cost_pct):row.status==='pending'?'Срок ещё не прошёл':'Нет полных свечей; пробелы не заполняем'));
          grid.append(cell);
        }
        result.append(grid,node('p',data.reaction.note),node('p',data.current_market.message));
        if(data.current_market.status==='ok')result.append(node('p','Текущий спред: '+(typeof data.current_market.spread_bps==='number'?data.current_market.spread_bps.toFixed(2):'—')+' б.п. · доля агрессивных покупок: '+percent(typeof data.current_market.buy_share==='number'?data.current_market.buy_share*100:null)));
        result.append(node('p','Загрузка цен выполняется с ограничением частоты. Если данных нет, повтори проверку позже. Это не рекомендация купить.'));
        button.textContent='Перепроверить реакцию';
      }catch{result.textContent='Данные реакции сейчас недоступны. Ни цены, ни результат сделки не подставляются.';}
      finally{clearTimeout(timer);button.disabled=!select.value;}
    });
  }
  function story(item){
    const n=node('article',undefined,'news-story'),meta=node('div',undefined,'news-story-meta'),sem=item.semantics||{};
    meta.append(link(item.source_name,item.source_home,'news-source-link'),node('span',item.official?'Официальный источник':'СМИ / заголовок'));
    if(sem.tone){const badge=node('span',sem.tone_label,'tone');badge.dataset.tone=sem.tone;meta.append(badge);}
    const h=node('h3');h.append(link(item.title,item.url));
    const tags=node('div',undefined,'news-tags');
    (item.tickers||[]).forEach(t=>tags.append(node('span',t,'news-tag company')));
    (item.topics||[]).forEach(t=>tags.append(node('span',TOPICS[t]||'Событие','news-tag')));
    if(sem.kind_label)tags.append(node('span',sem.kind_label,'news-kind'));
    n.append(meta,h,tags);
    if(!mini&&sem.reasons){const why=node('div',undefined,'news-explanation');why.append(node('strong','Почему такая метка: '),node('span',sem.reasons.join('. ')));n.append(why);}
    const timing=node('div',undefined,'news-story-time');
    timing.append(node('span','Опубликовано: '+date(item.published_at)+' · Впервые увидели: '+date(item.first_seen_at)));
    if(item.observed_at!==item.first_seen_at)timing.append(node('span',' · Версия заголовка: '+date(item.observed_at)));
    if(item.stale)timing.append(node('span',' · Старше суток','news-old'));
    if(item.date_unknown)timing.append(node('span',' · Время издателя не подтверждено','news-old'));
    n.append(timing);const legal=node('div',undefined,'news-inline-note');credit(item,legal);if(legal.childNodes.length)n.append(legal);
    if(!mini&&item.publications>1){
      const details=node('details',undefined,'news-members'),ul=node('ul');
      details.append(node('summary',item.publications+' публикации · '+item.publisher_count+' источника'));
      for(const member of item.members||[]){
        const li=node('li');li.append(link(member.source_name+' — '+member.title,member.url),node('span',' · '+date(member.first_seen_at)+' '));credit(member,li);ul.append(li);
      }
      details.append(ul,node('p',item.confirmation_note));n.append(details);
    }
    if(!mini)marketReaction(item,n);
    return n;
  }
  function sourceRow(s){
    const n=node('div',undefined,'news-source-state');n.dataset.status=s.status||'pending';
    const labels={ok:'Лента доступна',policy_blocked:'Сбор отключён',requires_access:'Нужен разрешённый доступ',robots_blocked:'Запрет robots.txt',access_denied:'Доступ ограничен',rate_limited:'Лимит запросов',waiting:'Ожидание разрешённого интервала',tls_error:'Не подтверждён сертификат',policy_review_due:'Нужна перепроверка условий'};
    n.append(link(s.name,s.home),node('p',labels[s.status]||(s.last_attempt?'Временно недоступен':'Ещё не проверен'),'news-source-label'),node('p',s.message||s.policy_note));
    if(s.last_success)n.append(node('p','Успешная проверка: '+date(s.last_success)));
    n.append(link('Условия источника',s.terms_url,'news-policy'),node('p','Правила проверены: '+s.reviewed_on+'. Перепроверить до '+s.review_due+'.'));
    if(s.license_url)n.append(link('Лицензия',s.license_url,'news-policy'));
    return n;
  }
  function stock(){const label=$('ticker-label');return label?label.textContent.split('·')[0].trim():new URLSearchParams(location.search).get('ticker')||'SBER';}
  function params(){
    if(mini)return new URLSearchParams({ticker:stock(),macro:'1'});
    const p=new URLSearchParams();for(const [key,id] of fields)if($(id)&&$(id).value.trim())p.set(key,$(id).value.trim());
    if($('news-macro').checked)p.set('macro','1');return p;
  }
  let calendarSeq=0;
  async function loadCalendar(){
    if(mini||document.hidden)return;
    const current=++calendarSeq,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
    try{
      const data=await json('/api/news/calendar/?'+new URLSearchParams({ticker:$('news-ticker').value}),controller.signal);
      if(current!==calendarSeq)return;
      const target=$('news-calendar'),label=$('calendar-status');target.replaceChildren();
      const coverage=data.coverage||[],good=coverage.filter(c=>c.status==='ok'&&!c.stale).length;
      label.textContent=data.message||('Проверено компаний: '+coverage.length+' из '+( $('news-ticker').value?1:TICKERS.length)+'. Без ошибок и не старше суток: '+good+'.');
      if(!data.events||!data.events.length)target.append(node('p','Подтверждённых предстоящих дат пока нет в локальном архиве. Запросы выполняются в фоне. Пустая выборка не означает, что событий не будет.','news-inline-note'));
      for(const event of (data.events||[]).slice(0,12)){
        const row=node('div',undefined,'news-calendar-item'),day=node('div',event.date.slice(8),'news-calendar-date');
        day.append(node('small',event.date.slice(5,7)+'.'+event.date.slice(0,4)));
        const text=node('div');text.append(node('strong',event.ticker),node('p',event.title));
        if(typeof event.amount==='number')text.append(node('p',event.amount.toFixed(2)+' '+(event.currency||'')+' на акцию'));
        text.append(node('span','Получено '+date(event.observed_at)+(event.stale?' · Данные устарели':'')));row.append(day,text);target.append(row);
      }
      if(coverage.some(c=>c.status!=='ok'||c.stale))target.append(node('p','Часть дат не удалось обновить. Ошибка или пустой ответ API не заменяются вымышленными событиями.','news-inline-note'));
    }catch{if(current===calendarSeq)$('calendar-status').textContent='Не удалось прочитать календарь. Даты не подставляются.';}
    finally{clearTimeout(timer);}
  }
  let seq=0,controller=null,lastStock='';
  async function load(){
    if(document.hidden)return;
    const request=++seq,p=params();
    if(controller)controller.abort();controller=new AbortController();
    const active=controller,timer=setTimeout(()=>active.abort(),15000);
    if(mini){
      if(lastStock!==p.get('ticker')){list.replaceChildren();status.textContent='Загружаем новостной контекст '+p.get('ticker')+'…';}
      lastStock=p.get('ticker');$('stock-news-link').href='/news/?'+p.toString();
    }
    try{
      const data=await json('/api/news/?'+p.toString(),active.signal);if(request!==seq)return;
      list.replaceChildren();const items=(data.items||[]).slice(0,mini?4:80);
      if(items.length)items.forEach(item=>list.append(story(item)));
      else list.append(node('div',data.status==='loading'?'Первый сбор выполняется в фоне. Здесь появятся только реально полученные заголовки.':'По этим фильтрам сохранённых событий нет. Попробуй другую компанию или тональность.','news-empty'));
      const summary=data.summary||{},good=(data.sources||[]).filter(s=>s.enabled&&s.status==='ok').length,total=(data.sources||[]).filter(s=>s.enabled).length;
      status.textContent=(mini?'Контекст '+lastStock+'. ':'')+'Показано событий: '+items.length+'. Доступных лент: '+good+'/'+total+'. '+(data.status==='loading'?'Сбор выполняется в фоне.':'Состояние: '+date(data.now)+'.')+' Источники запрашиваются не чаще 10 минут.';
      if(data.message)status.textContent=data.message;
      if($('news-sources'))$('news-sources').replaceChildren(...(data.sources||[]).map(sourceRow));
      for(const [id,value] of [['news-count',summary.events??items.length],['news-positive',(summary.tone_counts||{}).positive??0],['news-negative',(summary.tone_counts||{}).negative??0],['news-source-count',good+'/'+total]])if($(id))$(id).textContent=value;
    }catch(error){if(request===seq&&!(error.name==='AbortError'&&active!==controller))status.textContent='Не удалось обновить ленту. Проверь, запущен ли сайт; предыдущие заголовки оставлены на экране.';}
    finally{clearTimeout(timer);}
  }
  if(!mini){
    const p=new URLSearchParams(location.search);for(const [key,id] of fields)if(p.has(key)&&$(id))$(id).value=p.get(key);
    $('news-macro').checked=p.get('macro')==='1';
    const apply=()=>{history.replaceState(null,'','/news/?'+params().toString());load();loadCalendar();};
    $('news-filters').addEventListener('submit',event=>{event.preventDefault();apply();});
    for(const id of ['news-tone','news-kind'])$(id).addEventListener('change',apply);
    $('news-refresh').addEventListener('click',()=>{load();loadCalendar();});
  }else if($('ticker-label')){
    new MutationObserver(()=>{if(stock()!==lastStock)load();}).observe($('ticker-label'),{childList:true,characterData:true,subtree:true});
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){load();loadCalendar();}});
  setInterval(()=>{load();loadCalendar();},60000);load();loadCalendar();
})(typeof window==='object'?window:globalThis);
