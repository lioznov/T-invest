const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../static/js/neural_trader.js');
const now=Date.parse('2026-10-17T10:00:00Z');
const fresh={status:'ok',decision:'WATCH',score:70,expected_net_pct:.2,valid_until:new Date(now+5000).toISOString(),drivers:[],plan:{entry:100,stop:99}};

test('demo and expired readings never display entry or expected earnings',()=>{
  for(const value of [ui.state(fresh,'demo',now),ui.state({...fresh,valid_until:new Date(now-1).toISOString()},'live',now)]) {
    assert.equal(value.decision,'WAIT');assert.equal(value.expected_net_pct,null);assert.equal(value.plan,null);
  }
});
test('fresh valid estimates survive, invalid scores do not',()=>{
  assert.equal(ui.state(fresh,'live',now).decision,'WATCH');
  assert.equal(ui.state({...fresh,expected_net_pct:NaN},'live',now).decision,'WAIT');
});
test('zero trades and missing outcomes never appear as a profitable percentage',()=>{
  assert.equal(ui.outcome({trades:0,unknown_trades:0,return_pct:0}),'Нет сделок');
  assert.equal(ui.outcome({trades:20,unknown_trades:1,return_pct:50}),'Есть непроверенные исходы');
});
test('external messages are escaped',()=>{
  const html=ui.readingMarkup({status:'unavailable',message:'<img src=x onerror=alert(1)>'},'live');
  assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
});
