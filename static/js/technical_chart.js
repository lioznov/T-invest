(function(root){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=(v,d=2)=>Number.isFinite(v)?v.toLocaleString('ru-RU',{maximumFractionDigits:d}):'—';
  const when=v=>new Date(v).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow'});
  const valid=p=>p?.version==='technical_context_v1' && p.entry_allowed===false && ['ok','stale'].includes(p.status);
  function state(p,now=Date.now()){
    if(!valid(p))return null;
    let result=p;
    const until=Date.parse(p.valid_until);
    if(p.status==='ok' && (!Number.isFinite(until)||now>until))result={...p,status:'stale',state:null,message:'Исторические коридоры: текущая свеча устарела.'};
    const b=p.book||{};
    if(['ok','demo'].includes(b.status) && (!Number.isFinite(Date.parse(b.valid_until)) || now>Date.parse(b.valid_until)))
      result={...result,book:{...b,status:'stale',matches:[],message:'Сверка стакана устарела. Ждём свежие заявки.'}};
    return result;
  }
  function extents(p,data){
    if(!valid(p))return [];
    const times=new Set(data.map(c=>Date.parse(c.time))),values=[];
    for(const c of p.range_points||[])if(times.has(Date.parse(c.time)))values.push(c.lower,c.upper);
    if(p.regression)for(const key of ['start_lower','start_upper','lower','upper'])values.push(p.regression[key]);
    return values.filter(v=>Number.isFinite(v)&&v>0);
  }
  function markup(data,p,g,layers={},now=Date.now()){
    p=state(p,now);
    if(!p||!Array.isArray(data)||!data.length)return {behind:'',front:''};
    const index=new Map(data.map((c,i)=>[Date.parse(c.time),i])),behind=[],front=[];
    const price=v=>Number.isFinite(v)&&v>0;
    const clip='technical-chart-clip';
    const defs=`<defs><clipPath id="${clip}"><rect x="${g.x(0)-4}" y="${g.top}" width="${g.end-g.x(0)+8}" height="${g.bottom-g.top}"/></clipPath></defs>`;
    if(layers.channels!==false){
      const segments=[];let segment=[],last=-2;
      for(const c of p.range_points||[]){
        const i=index.get(Date.parse(c.time));
        if(i===undefined||![c.lower,c.upper,c.middle].every(price)||c.lower>c.upper)continue;
        if(i!==last+1&&segment.length){segments.push(segment);segment=[];}
        segment.push({i,...c});last=i;
      }
      if(segment.length)segments.push(segment);
      function path(points,key){return points.map((c,i)=>`${i?'H'+g.x(c.i)+' V':'M'+g.x(c.i)+','}${g.y(c[key])}`).join(' ');}
      for(const points of segments){
        if(points.length<2)continue;
        const upper=path(points,'upper'),reverse=[...points].reverse();
        const fill=upper+` L${g.x(reverse[0].i)},${g.y(reverse[0].lower)} `+reverse.slice(1).map(c=>`H${g.x(c.i)} V${g.y(c.lower)}`).join(' ')+' Z';
        behind.push(`<g data-technical="range-channel"><title>Коридор предыдущих 20 свечей. Текущая свеча не задаёт собственные границы.</title><path d="${fill}" fill="#74a4b7" opacity=".07"/><path d="${upper}" fill="none" stroke="#9db8c5" stroke-width="1" opacity=".6"/><path d="${path(points,'lower')}" fill="none" stroke="#9db8c5" stroke-width="1" opacity=".6"/></g>`);
      }
      const r=p.regression;
      if(r&&['lower','upper','middle','slope','start_lower','start_upper'].every(k=>Number.isFinite(r[k]))){
        const end=index.get(Date.parse(r.end_time));
        const first=index.get(Date.parse(r.start_time))??0;
        if(end!==undefined&&end>first){
          const startLow=r.lower-r.slope*(end-first),startHigh=r.upper-r.slope*(end-first);
          behind.push(`<g data-technical="regression-channel"><title>Наклонный коридор 60 свечей. Построен после ${esc(new Date(r.known_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}))} МСК. Пунктирная историческая часть — прямая, подобранная по этому окну; не прежние сигналы.</title><path d="M${g.x(first)},${g.y(startLow)} L${g.x(end)},${g.y(r.lower)} L${g.x(end)},${g.y(r.upper)} L${g.x(first)},${g.y(startHigh)} Z" fill="#8d96c0" opacity=".07"/><path d="M${g.x(first)},${g.y(startLow)} L${g.x(end)},${g.y(r.lower)} M${g.x(first)},${g.y(startHigh)} L${g.x(end)},${g.y(r.upper)} M${g.x(first)},${g.y((startLow+startHigh)/2)} L${g.x(end)},${g.y(r.middle)}" fill="none" stroke="#a29fc5" stroke-dasharray="7 4" stroke-width="1.2" opacity=".75"/></g>`);
        }
      }
    }
    if(layers.book!==false&&['ok','demo'].includes(p.book?.status))for(const m of p.book.matches||[]){
      if(!['full','partial'].includes(m.coverage)||!price(m.price)||!price(m.tolerance)||!Number.isFinite(m.lots)||m.lots<=0)continue;
      const color=m.kind==='support'?'#64b8a1':'#cc9875',top=g.y(m.price+m.tolerance),bottom=g.y(m.price-m.tolerance);
      front.push(`<g data-technical="book-zone"><title>${esc(m.name)} · наблюдалось ${num(m.lots,0)} лотов в ${esc(when(p.book.time))} МСК. ${esc(m.label)}. ${p.book.status==='demo'?'Вымышленные заявки. ':''}Заявки могут исчезнуть.</title><rect x="${g.end-54}" y="${top}" width="54" height="${Math.max(2,bottom-top)}" fill="${color}" opacity=".18"/><line x1="${g.end-54}" x2="${g.end}" y1="${g.y(m.price)}" y2="${g.y(m.price)}" stroke="${color}" stroke-width="2"/><text x="${g.end-4}" y="${g.y(m.price)-5}" text-anchor="end" fill="${color}" font-size="9">${num(m.lots,0)} л.</text></g>`);
    }
    return {behind:defs+`<g clip-path="url(#${clip})">${behind.join('')}</g>`,front:`<g clip-path="url(#${clip})">${front.join('')}</g>`};
  }
  function panelMarkup(value){
    const p=state(value);
    if(!p)return `<p class="muted">${esc(value?.message||'Ждём закрытые свечи и стакан.')}</p>`;
    const band=p.range_points?.at(-1),b=p.book||{},matches=new Map((b.matches||[]).map(m=>[m.key,m]));
    const directions={up:'Восходящий',down:'Нисходящий',flat:'Боковой'};
    const rows=(p.levels||[]).map(l=>{const m=matches.get(l.key),visible=m&&m.coverage!=='outside';return `<tr><td>${esc(l.name)}<small>${l.kind==='support'?'Сравниваем с покупками':'Сравниваем с продажами'}</small></td><td>${num(l.price)} ₽</td><td>${visible?num(m.lots,0)+(m.coverage==='partial'?' · частично':''):'—'}</td><td>${esc(m?.label||b.message||'Ожидаем стакан')}<small>${m?.retention_fraction!=null?'Осталось '+num(m.retention_fraction*100,0)+'% от максимума в '+num(m.observations,0)+' снимках':'Изменение объёма пока не оценено'}</small></td></tr>`;}).join('');
    return `<p class="technical-summary">${esc(p.mode==='demo'?'Учебная разметка вымышленных цен. ':'')}${esc(p.message)}</p><div class="technical-metrics"><div><span>Коридор 20 свечей</span><strong>${num(band?.lower)} — ${num(band?.upper)} ₽</strong></div><div><span>Коридор 60 свечей</span><strong>${esc(directions[p.direction]||'Нет оценки')}</strong></div><div><span>Сверка стакана</span><strong>${b.status==='ok'?'Свежие данные':b.status==='demo'?'Учебные заявки':b.status==='stale'?'Данные устарели':'Ожидаем поток'}</strong></div></div><p class="research-meta">${esc(b.message||'Ждём стакан.')}${b.time?' Снимок: '+esc(when(b.time))+' МСК.':''}</p><div class="table-scroll"><table class="technical-table"><thead><tr><th>Уровень графика</th><th>Цена</th><th>Видимые лоты возле уровня</th><th>Что видно в стакане</th></tr></thead><tbody>${rows}</tbody></table></div><details><summary>Как читать коридоры и заявки</summary><p class="research-meta">${esc(p.note)}</p></details>`;
  }
  function researchMarkup(data){
    if(data?.status!=='ok')return `<p class="research-meta">${esc(data?.message||'Проверка коридоров пока недоступна.')}</p>`;
    const names={test:'Сентябрь — 2 октября',additional:'3–12 октября',later:'13–16 октября'};
    const result=r=>r.unknown_trades?'Есть непроверенные исходы':r.trades?num(r.return_pct,3)+'%':'Нет сделок';
    const rows=Object.entries(data.results).flatMap(([p,variants])=>Object.entries(variants).map(([key,r])=>`<tr><td>${esc(names[p]||p)}</td><td>${esc(data.variants[key])}</td><td>${num(r.base.trades,0)}</td><td>${result(r.base)}</td><td>${result(r.double_cost)}</td></tr>`)).join('');
    const diagnostic=Object.entries(data.diagnostics).map(([p,r])=>`<tr><td>${esc(names[p]||p)}</td><td>${num(r.samples,0)}</td><td>${num(r.probability_max*100,1)} / 100</td><td>${num(r.expected_net_max_pct,3)}%</td><td>${num(r.passes_both,0)}</td></tr>`).join('');
    return `<p class="research-meta">${esc(data.note)}</p><div class="table-scroll"><table class="technical-table"><thead><tr><th>Период 2026</th><th>Правило</th><th>Сделок</th><th>После расходов</th><th>Расходы ×2</th></tr></thead><tbody>${rows}</tbody></table></div><p class="research-meta">Почему нейросеть ждёт: проверяем максимальные оценки на том же наборе. Для её сценария нужны одновременно оценка ≥50/100 и ожидаемый результат ≥+0,05% после обычных расходов. Максимумы в разных столбцах могут относиться к разным свечам.</p><div class="table-scroll"><table class="technical-table"><thead><tr><th>Период 2026</th><th>Примеров</th><th>Максимальная оценка нейросети</th><th>Максимум ожидаемого результата</th><th>Оба условия</th></tr></thead><tbody>${diagnostic}</tbody></table></div>`;
  }
  const api={valid,state,extents,markup,panelMarkup,researchMarkup};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.VorbyTechnical=api;
})(typeof window!=='undefined'?window:globalThis);
