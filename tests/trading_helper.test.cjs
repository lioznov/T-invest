const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../static/js/trading_helper.js');
const now=Date.parse('2026-11-03T12:00:00Z');
const helper={version:'trading_research_helper_v1',entry_allowed:false,confidence:null,action:'REVIEW',headline:'Есть сценарий',
  created_at:new Date(now-1000).toISOString(),data_review:{state:'fresh_live',message:'Свежие данные',valid_until:new Date(now+5000).toISOString()},
  signal_review:{message:'6 условий'},neural_review:{message:'Оценка модели'},risk_review:{message:'Проверь риск'},research_review:{},next_steps:[]};
test('expired, future, unknown and demo states suppress current entry wording',()=>{
  for(const h of [null,{...helper,created_at:new Date(now+1).toISOString()}, {...helper,data_review:{...helper.data_review,valid_until:new Date(now-1).toISOString()}}]){
    const result=ui.state(h,'live',now);assert.equal(result.action,'WAIT');assert.ok(!ui.markup(h,'live',now).includes('6 условий'));
  }
  assert.equal(ui.state(helper,'demo',now).action,'WAIT');
  assert.equal(ui.state({...helper,entry_allowed:true},'live',now).action,'WAIT');
});
test('blocked history cannot retain a fresh neural or risk scenario',()=>{
  const h=ui.state({...helper,data_review:{state:'stale_live'}},'live',now);
  assert.equal(h.action,'WAIT');assert.ok(!h.signal_review.message.includes('6 условий'));
  assert.equal(h.risk_review.entry,undefined);
});
test('every server explanation is escaped',()=>{
  const html=ui.markup({...helper,headline:'<img src=x onerror=alert(1)>',next_steps:['<script>boom</script>']},'live',now);
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;img'));
});
