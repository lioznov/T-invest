const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../static/js/technical_chart.js');
const now=Date.parse('2026-10-17T10:00:00Z');
const data=Array.from({length:5},(_,i)=>({time:new Date(now-(5-i)*900000).toISOString()}));
const g={x:i=>10+i*100,y:v=>300-v,min:90,max:120,top:10,bottom:280,end:410};
function panel(){return {version:'technical_context_v1',entry_allowed:false,status:'ok',mode:'live',valid_until:new Date(now+100000).toISOString(),
  direction:'up',message:'Цена в коридоре',levels:[{key:'s',name:'Поддержка',kind:'support',price:100}],
  range_points:data.map(c=>({time:c.time,lower:98,upper:104,middle:101})),
  regression:{lower:99,upper:105,middle:102,start_lower:97,start_upper:103,slope:.5,start_time:data[0].time,end_time:data[4].time,known_at:data[3].time},
  book:{status:'ok',time:new Date(now).toISOString(),valid_until:new Date(now+1000).toISOString(),matches:[{key:'s',kind:'support',name:'Поддержка',price:100,tolerance:.1,lots:1000,coverage:'full',label:'Объём виден'}]}};}

test('both corridors draw behind candles and order zones in front',()=>{
  const out=ui.markup(data,panel(),g,{},now);
  assert.ok(out.behind.includes('data-technical="range-channel"'));
  assert.ok(out.behind.includes('data-technical="regression-channel"'));
  assert.ok(out.front.includes('data-technical="book-zone"'));
  assert.ok(out.behind.includes('не прежние сигналы'));
});
test('layer switches independently hide channels and orders',()=>{
  const a=ui.markup(data,panel(),g,{channels:false,book:true},now);
  assert.ok(!a.behind.includes('data-technical'));assert.ok(a.front.includes('book-zone'));
  const b=ui.markup(data,panel(),g,{channels:true,book:false},now);
  assert.ok(b.behind.includes('range-channel'));assert.ok(!b.front.includes('book-zone'));
});
test('expired orders disappear while historical corridors remain',()=>{
  const out=ui.markup(data,panel(),g,{},now+2000);
  assert.ok(out.behind.includes('regression-channel'));assert.ok(!out.front.includes('book-zone'));
});
test('approval and unsupported formats draw nothing',()=>{
  for(const p of [{...panel(),entry_allowed:true},{...panel(),version:'unknown'}])
    assert.deepEqual(ui.markup(data,p,g,{},now),{behind:'',front:''});
});
test('invalid prices or uncovered depth cannot add order markers',()=>{
  const p=panel();p.book.matches[0].coverage='outside';
  assert.ok(!ui.markup(data,p,g,{},now).front.includes('book-zone'));
  p.book.matches[0].coverage='full';p.book.matches[0].price=NaN;
  assert.ok(!ui.markup(data,p,g,{},now).front.includes('book-zone'));
});
test('rendered messages are escaped and missing depth is not presented as zero',()=>{
  const html=ui.panelMarkup({status:'invalid',message:'<script>bad</script>'});
  assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));
});
