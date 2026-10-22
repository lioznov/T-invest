/* Pure SVG renderer: chart annotations, never an order or probability. */
(function(root){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const price=v=>v.toLocaleString('ru-RU',{maximumFractionDigits:4});
  const when=v=>new Date(v).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'});
  function validPanel(p){return p?.version==='market_structure_v1' && p.entry_allowed===false && ['ok','stale'].includes(p.status);}
  function markup(data,p,g,layers={levels:true,trends:true,pivots:true,events:true}){
    if(!validPanel(p) || !Array.isArray(data) || !data.length)return '';
    const stamps=data.map(c=>Date.parse(c.time));
    const index=new Map(stamps.map((t,i)=>[t,i]));
    const inPrice=v=>Number.isFinite(v) && v>=g.min && v<=g.max;
    const firstAfter=t=>{const i=stamps.findIndex(s=>s>=t);return i<0?data.length-1:i;};
    const svg=[];
    if(layers.levels)for(const level of p.levels||[]){
      const known=Date.parse(level.known_at),v=level.price;
      if(!inPrice(v) || !Number.isFinite(known) || !['support','resistance'].includes(level.kind))continue;
      const color=level.kind==='support'?'#64b8a1':'#cc9875';
      const name=level.kind==='support'?'Поддержка':'Сопротивление';
      const y=g.y(v),start=g.x(firstAfter(known));
      svg.push(`<g data-structure="level"><title>${name} ${price(v)} ₽ · известна с ${esc(when(level.known_at))} МСК. ${level.broken?'Закрытие вышло за уровень. ':''}Ориентир, не сигнал покупки.</title><line x1="${start}" x2="${g.end}" y1="${y}" y2="${y}" stroke="${color}" stroke-width="1.25" stroke-dasharray="${level.broken?'2 5':'8 4'}" opacity="${level.broken ? .45 : .8}"/><text x="${g.end-5}" y="${Math.max(g.top+10,y-5)}" text-anchor="end" fill="${color}" font-size="9">${name} ${price(v)}</text></g>`);
    }
    if(layers.trends)for(const line of p.trend_lines||[]){
      const at=Date.parse(line.start_time),endAt=Date.parse(line.end_time);
      if(!inPrice(line.start_price)||!inPrice(line.end_price)||!Number.isFinite(at)||!Number.isFinite(endAt)||endAt<stamps[0])continue;
      const first=firstAfter(at),last=index.get(endAt)??data.length-1;
      if(last<=first)continue;
      // Do not stretch an off-screen start price to the left edge: interpolate by bar count.
      let startPrice=line.start_price;
      if(at<stamps[0]){
        const anchors=line.anchors||[];
        if(anchors.length!==2 || !Number.isInteger(anchors[1].index)||!Number.isInteger(anchors[1].confirmed_index))continue;
        const span=anchors[1].index-anchors[0].index;
        if(span<=0)continue;
        const slope=(anchors[1].price-anchors[0].price)/span;
        startPrice=line.end_price-slope*last;
        if(!inPrice(startPrice))continue;
      }
      const color=line.kind==='support'?'#7ba7d4':'#bd99c5';
      svg.push(`<g data-structure="trend"><title>Наклонная ${line.kind==='support'?'поддержка':'линия сопротивления'} · построена после ${esc(when(line.known_at))} МСК. Продление прямой, не прогноз цены.</title><line x1="${g.x(first)}" y1="${g.y(startPrice)}" x2="${g.x(last)}" y2="${g.y(line.end_price)}" stroke="${color}" stroke-width="1.6" stroke-dasharray="${line.broken?'3 5':'12 4'}" opacity=".8"/></g>`);
    }
    if(layers.pivots)for(const pivot of (p.pivots||[]).slice(-14)){
      const i=index.get(Date.parse(pivot.time));
      if(i===undefined || !inPrice(pivot.price) || !['high','low'].includes(pivot.kind))continue;
      const high=pivot.kind==='high',color=high?'#cc9875':'#64b8a1',px=g.x(i),py=g.y(pivot.price);
      svg.push(`<g data-structure="pivot"><title>${high?'Максимум':'Минимум'} ${price(pivot.price)} ₽ · свеча ${esc(when(pivot.time))} МСК; подтверждён только ${esc(when(pivot.confirmed_at))} МСК.</title><circle cx="${px}" cy="${py}" r="3" fill="#131a23" stroke="${color}" stroke-width="1.5"/><text x="${px}" y="${Math.max(g.top+10,Math.min(g.bottom-2,py+(high?-9:14)))}" text-anchor="middle" fill="${color}" font-size="8">${high?'max':'min'}</text></g>`);
    }
    if(layers.events)for(const e of p.events||[]){
      const i=index.get(Date.parse(e.time));
      const names={breakout:'Пробой сопротивления',breakdown:'Уход ниже поддержки',bounce:'Отскок от поддержки'};
      if(i===undefined||!names[e.key]||!inPrice(e.price)||!Number.isFinite(e.level))continue;
      const px=g.x(i),py=g.y(e.price),color=e.key==='breakdown'?'#c68791':'#93b9dc';
      svg.push(`<g data-structure="event"><title>${names[e.key]} · уровень ${price(e.level)} ₽ · обнаружен ${esc(when(e.detected_at))} МСК. Историческое наблюдение, не приказ покупать.</title><path d="M ${px-5} ${py+6} L ${px} ${py-5} L ${px+5} ${py+6} Z" fill="#131a23" stroke="${color}" stroke-width="1.4"/><text x="${px}" y="${Math.max(g.top+10,py-10)}" text-anchor="middle" fill="${color}" font-size="8">${e.key==='breakout'?'↑':e.key==='breakdown'?'↓':'↗'}</text></g>`);
    }
    return svg.join('');
  }
  const api={markup,validPanel};
  if(typeof module!=='undefined' && module.exports)module.exports=api;
  else root.VorbyStructure=api;
})(typeof window!=='undefined'?window:globalThis);
