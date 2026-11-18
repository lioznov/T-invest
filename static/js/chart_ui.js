(function(root){
  'use strict';
  // Presentation only: indicator values, signals and candle history are unchanged.
  function chartLayout(width,height){
    const w=Number.isFinite(width)&&width>0?Math.max(240,Math.round(width)):800;
    const h=Number.isFinite(height)&&height>0?Math.max(200,Math.round(height)):310;
    return {width:w,height:h,left:14,right:92,top:34,bottom:49,ticks:w<450?3:5};
  }
  function scenarioPrice(value){
    if(!Number.isFinite(value)||value<=0)return '';
    // Kopecks for ordinary share prices; retain small-price precision rather than
    // rendering a sub-rouble instrument as zero. This is not tick-size validation.
    const text=value.toFixed(value>=1?2:8);
    if(Number(text)===0)return String(value);
    return value>=1?text:text.replace(/0+$/,'').replace(/\.$/,'');
  }
  const clock=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'});
  const date=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit'});
  function timeTicks(layout,data,x){
    const {height,ticks}=layout;
    const indices=new Set(Array.from({length:ticks},(_,i)=>Math.round(i*(data.length-1)/(ticks-1))));
    return Array.from(indices,i=>{
      const timestamp=new Date(data[i].time);
      if(!Number.isFinite(timestamp.getTime()))return '';
      const position=x(i),align=i===0?'start':i===data.length-1?'end':'middle';
      return `<text x="${position}" y="${height-28}" text-anchor="${align}" class="plot-axis"><tspan>${date.format(timestamp)}</tspan><tspan x="${position}" dy="16">${clock.format(timestamp)}</tspan></text>`;
    }).join('');
  }
  function inspect(svg,reading,describe){
    const prompt=reading.textContent;
    let plot=null,index=null;
    function select(next){
      if(!plot)return;
      const cursor=svg.querySelector('[data-plot-cursor]');
      if(!cursor)return;
      index=next;
      cursor.style.visibility=index===null?'hidden':'visible';
      const label=index===null?prompt:describe(plot.data[index]);
      if(index!==null){
        const x=plot.x(index);
        cursor.setAttribute('x1',x);cursor.setAttribute('x2',x);
      }
      reading.textContent=label;
      svg.setAttribute('aria-description',label);
    }
    function pick(event){
      if(!plot)return;
      const rect=svg.getBoundingClientRect();
      if(!rect.width || !rect.height)return;
      const px=(event.clientX-rect.left)*plot.layout.width/rect.width;
      const py=(event.clientY-rect.top)*plot.layout.height/rect.height;
      const end=plot.layout.width-plot.layout.right;
      if(px<plot.layout.left-8 || px>end+8 || py<plot.layout.top-8 || py>plot.layout.height-plot.layout.bottom+8)return;
      const step=(px-plot.layout.left)/(end-plot.layout.left);
      select(Math.max(0,Math.min(plot.data.length-1,Math.round(step*(plot.data.length-1)))));
    }
    svg.addEventListener('pointermove',event=>{if(event.pointerType==='mouse')pick(event);});
    svg.addEventListener('pointerleave',event=>{if(event.pointerType==='mouse')select(null);});
    svg.addEventListener('click',pick);
    svg.addEventListener('keydown',event=>{
      if(!plot)return;
      if(event.key==='Escape'){select(null);return;}
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();
      const last=plot.data.length-1,current=index ?? last;
      const next=event.key==='Home'?0:event.key==='End'?last:current+(event.key==='ArrowLeft'?-1:1);
      select(Math.max(0,Math.min(last,next)));
    });
    return {
      set(data,layout,x){
        const previous=index===null?null:plot?.data[index]?.time;
        plot={data,layout,x};
        const next=previous===null?-1:data.findIndex(c=>c.time===previous);
        select(next<0?null:next);
      },
      clear(){plot=null;index=null;reading.textContent=prompt;svg.removeAttribute('aria-description');}
    };
  }
  const api={chartLayout,scenarioPrice,timeTicks,inspect};
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.VorbyChartUI=api;
})(typeof window==='object'?window:globalThis);
