const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../static/js/risk_ui.js');
const result={status:'ok',lots:3,shares:30,position_cost:8487.86,planned_loss:59.1,risk_budget:500,
  allocation_budget:10000,expected_target_profit:68.7,effective_risk_pct:.0591,reward_risk:1.16,
  message:'Размер округлён до целых лотов.',assumptions:'Расходы учтены с обеих сторон.'};

test('budget hint translates valid percentages into rubles and never invents missing values',()=>{
  assert.equal(ui.budget('100000','.5'),500);
  assert.equal(ui.budget(80000,.2),160);
  for(const pair of [['',.5],[null,.5],[true,.5],['NaN',.5],[100000,0],[100000,5.01],[Infinity,.5]])
    assert.equal(ui.budget(...pair),null);
});
test('result presents server-provided whole lots, cost, risk and conditional target with a demo label',()=>{
  const html=ui.markup(result,'SBER','demo');
  for(const text of ['30 <small>акций','3 лот.','На покупку','Убыток при выходе по стопу','Результат при достижении цели','Учебные цены','не команда покупать','Лимиты и допущения'])assert.ok(html.includes(text),text);
  assert.ok(!ui.markup(result,'SBER','live').includes('Учебные цены'));
});
test('missing numbers, inconsistent zero lots and nonfinite fields suppress financial results',()=>{
  for(const bad of [{...result,position_cost:null},{...result,planned_loss:Infinity},{...result,lots:0},
                    {...result,shares:.5},{...result,shares:0}]){
    const html=ui.markup(bad,'SBER','live');
    assert.ok(html.includes('Расчёт пока недоступен'));
    assert.ok(!html.includes('На покупку'));
    assert.ok(!html.includes('NaN'));
  }
});
test('zero lots explain an insufficient budget without showing hypothetical profits',()=>{
  const html=ui.markup({...result,lots:0,shares:0,position_cost:0,planned_loss:0,expected_target_profit:0,
    effective_risk_pct:0,reward_risk:null},'SBER','live');
  assert.ok(html.includes('Один лот не помещается'));
  assert.ok(!html.includes('Результат при достижении цели'));
});
test('a target that loses money after costs is shown as negative and not as a positive outcome',()=>{
  const html=ui.markup({...result,expected_target_profit:-12.34,reward_risk:-.21},'SBER','live');
  assert.match(html,/class="negative">-12,34 ₽/);
});
test('server text and ticker names are escaped including errors and assumptions',()=>{
  const html=ui.markup({...result,message:'<script>bad</script>',assumptions:['<img src=x>']},'<svg onload=bad>','live');
  assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));assert.ok(!html.includes('<svg'));
  assert.ok(ui.markup({status:'error',message:'<b>Проверь цену</b>'},'SBER','live').includes('&lt;b&gt;'));
});
