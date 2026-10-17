const test=require('node:test');
const assert=require('node:assert/strict');
const {markup}=require('../static/js/market_structure.js');
const data=Array.from({length:6},(_,i)=>({time:new Date(Date.UTC(2026,9,13,7,i*15)).toISOString()}));
const geometry={x:i=>10+i*100,y:v=>300-v,min:90,max:120,top:10,bottom:280,end:510};
function panel(){return {version:'market_structure_v1',entry_allowed:false,status:'ok',
  levels:[{kind:'support',price:100,known_at:data[3].time}],
  pivots:[{kind:'low',price:99,time:data[1].time,confirmed_at:data[3].time}],
  trend_lines:[{kind:'support',start_time:data[3].time,end_time:data[5].time,start_price:101,end_price:102,known_at:data[3].time}],
  events:[{key:'breakout',time:data[4].time,detected_at:data[5].time,level:101,price:102}]};}
test('draws all four annotation types with disclosure of confirmation',()=>{
  const svg=markup(data,panel(),geometry);
  for(const layer of ['level','pivot','trend','event'])assert.ok(svg.includes(`data-structure="${layer}"`));
  assert.ok(svg.includes('подтверждён только'));
  assert.ok(svg.includes('не приказ покупать'));
});
test('level starts at availability, not the original pivot',()=>{
  assert.ok(markup(data,panel(),geometry).includes('x1="310"'));
});
test('layers can all be hidden',()=>{
  assert.equal(markup(data,panel(),geometry,{levels:false,trends:false,pivots:false,events:false}),'');
});
test('invalid panel, approval or unsupported version produces no annotations',()=>{
  for(const change of [{status:'invalid'},{entry_allowed:true},{version:'unknown'}])assert.equal(markup(data,{...panel(),...change},geometry),'');
});
test('missing event level and out-of-view pivots are skipped safely',()=>{
  const p=panel();
  p.events[0].level=undefined;
  p.pivots[0].time='2000-01-01T00:00:00Z';
  p.levels[0].price=Infinity;
  const svg=markup(data,p,geometry);
  assert.ok(!svg.includes('data-structure="event"'));
  assert.ok(!svg.includes('data-structure="pivot"'));
  assert.ok(!svg.includes('data-structure="level"'));
});
test('stale panel retains explicitly historical drawings',()=>{
  assert.ok(markup(data,{...panel(),status:'stale'},geometry).includes('Историческое наблюдение'));
});
