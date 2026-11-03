const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const api=require('../static/js/temporal_validation.js');
const script=fs.readFileSync(require.resolve('../static/js/temporal_validation.js'),'utf8');

function fixture(){
  const paper={status:'ok',trades:8,selected_candidates:10,unknown_trades:0,net_return_pct:-.38,stress_return_pct:-.5,stress_trades:8,stress_unknown_trades:0};
  const report={samples:200,days:100,eligible_forecasts:203,unknown_targets:3,mae_bps:88.340386,baseline_mae_bps:88.020851,mean_baseline_mae_bps:90,rmse_bps:120,baseline_rmse_bps:119,mean_baseline_rmse_bps:125,mape_pct:.9,baseline_mape_pct:.88,mean_baseline_mape_pct:.91,mean_signed_error_bps:-5,direction_accuracy_pct:49.6,direction_nonzero_samples:199,direction_baseline_pct:51,direction_baseline_label:'Всегда рост',actual_up:100,actual_down:99,actual_zero:1,coverage_pct:80,interval_width_bps:250,abstention_pct:95,paper,always_buy:{...paper}};
  const stock={samples:200,mae_rub:2,baseline_mae_rub:1.9,mean_baseline_mae_rub:2.1,rmse_rub:3,baseline_rmse_rub:2.9,mean_baseline_rmse_rub:3.1,mape_pct:.9,baseline_mape_pct:.88,mean_baseline_mape_pct:.91};
  function example(year){const day={2024:'2024-10-21',2025:'2025-10-20',2026:'2026-10-19'}[year];return {ticker:'SBER',as_of:`${day}T17:45:00+03:00`,target_at:`${day.slice(0,8)}${Number(day.slice(8))+1}T10:00:00+03:00`,anchor_price:300,predicted_price:303,actual_price:301,absolute_error_rub:2,absolute_error_pct:2/301*100,kind:'latest'};}
  const folds=[2024,2025,2026].map(year=>({id:`test_${year}`,label:`Модель ${year}`,trained_rows:2000,calibration_rows:500,periods:{train:{start:'2018-03-07',end_exclusive:`${year-1}-01-01`},calibration:{start:`${year-1}-01-01`,end_exclusive:`${year}-01-01`},test:{start:`${year}-01-01`,end_exclusive:year===2026?'2026-10-23':`${year+1}-01-01`}},report:structuredClone(report),stocks:{SBER:structuredClone(stock)},examples:[example(year)],current_year_report:structuredClone(report),current_year_stocks:{SBER:structuredClone(stock)},current_year_examples:[example(2026)]}));
  return {status:'historical',entry_allowed:false,notice:'Ретроспектива, не заранее записанные прогнозы.',publication_note:'Ни один вариант не выбран победителем.',note:'Реальные заявки и Telegram не подключены.',folds,coverage:{SBER:{bars:200000,first_actual:'2018-03-07T10:00:00+03:00',last_actual:'2026-10-22T23:45:00+03:00',requested_from:'2018-03-07T00:00:00+03:00',to:'2026-10-23T00:00:00Z'}}};
}
function harness(value,options={}){
  const nodes=new Map();
  function node(tag='div'){return {tagName:tag,textContent:'',children:[],attributes:{},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(name,text){this.attributes[name]=text;}};}
  for(const id of ['temporal-validation','temporal-status','temporal-result'])nodes.set(id,node());
  const calls=[];
  const context={document:{getElementById:id=>nodes.get(id),createElement:node},fetch:async(url,request)=>{calls.push({url,request});if(options.failure)throw new Error('private error');return {ok:options.ok!==false,json:async()=>value};},Number,Date,Set,Array,Object,module:undefined};
  vm.runInNewContext(script,context);
  const text=item=>item.textContent+item.children.map(text).join(' ');
  return {nodes,calls,text,tick:()=>new Promise(resolve=>setImmediate(resolve))};
}
function elements(root,cls){return [root,...root.children.flatMap(child=>elements(child,cls))].filter(node=>node.className===cls);}

test('accepts only the three chronological historical fold identities',()=>{
  assert.equal(api.valid(fixture()),true);
  for(const bad of [null,{}, {...fixture(),status:'ok'}, {...fixture(),entry_allowed:true}])assert.equal(api.valid(bad),false);
  const duplicate=fixture();duplicate.folds[1]=duplicate.folds[0];assert.equal(api.valid(duplicate),false);
  const missing=fixture();missing.folds[0].current_year_report=null;assert.equal(api.valid(missing),false);
  const invalid=fixture();invalid.folds[0].periods.test.start='2024-02-30';assert.equal(api.valid(invalid),false);
});
test('one local GET loads the report without timers, fitting or brokerage calls',async()=>{
  const h=harness(fixture());await h.tick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/temporal-validation/');assert.equal(h.calls[0].request.method,undefined);assert.equal(h.calls[0].request.cache,'no-store');assert.match(h.nodes.get('temporal-status').textContent,/Только ретроспективная проверка/);
});
test('three cards show training then calibration then each primary test period',async()=>{
  const h=harness(fixture());await h.tick();const cards=elements(h.nodes.get('temporal-result'),'temporal-card');assert.equal(cards.length,3);
  for(const [i,card] of cards.entries()){const text=h.text(card);assert.match(text,/1. Обучение/);assert.match(text,/2. Калибровка диапазона/);assert.match(text,/3. Основная проверка/);assert.match(text,/07.03.2018/);assert.ok(text.includes(String(2024+i)));}
  assert.match(h.text(cards[2]),/22.10.2026/);
});
test('both comparisons show full diagnostics and fixed training-only directional baseline',async()=>{
  const h=harness(fixture());await h.tick();const text=h.text(h.nodes.get('temporal-result'));
  for(const phrase of ['Основные проверки','Все три модели на одних примерах 2026','0,8834%','0,8802%','RMSE','1,2000%','1,1900%','MAPE','0,8800%','Среднее смещение','n=199','Всегда рост · 51,00%','100 / 99 / 1','203 / 3','8 / 0'])assert.ok(text.includes(phrase),phrase);
  assert.match(text,/Различие результатов не доказывает/);assert.match(text,/повтор той же проверки, не ещё один независимый тест/);
});
test('actual future price is explicitly a scoring answer and examples show ruble errors',async()=>{
  const h=harness(fixture());await h.tick();const text=h.text(h.nodes.get('temporal-result'));
  for(const phrase of ['Фактическая цена — ответ для проверки, модель не видит её при прогнозе','303,00 ₽','301,00 ₽','2,00 ₽','0,6645%','не заранее записанные прогнозы','Последний пример'])assert.ok(text.includes(phrase),phrase);
});
test('bad finite predicted prices are shown rather than clamped or silently discarded',async()=>{
  const value=fixture();value.folds[0].examples[0].predicted_price=-2;
  const h=harness(value);await h.tick();assert.match(h.text(h.nodes.get('temporal-result')),/-2,00 ₽/);
  assert.equal(api.price(0,true),'0,00 ₽');assert.equal(api.price(-2,true),'-2,00 ₽');assert.equal(api.price(0),'—');assert.equal(api.price(Infinity,true),'—');
});
test('per-stock details retain model and both baseline errors',async()=>{
  const h=harness(fixture());await h.tick();const text=h.text(h.nodes.get('temporal-result'));
  for(const phrase of ['MAE без изменения, ₽','MAE среднее train, ₽','RMSE без изменения, ₽','RMSE среднее train, ₽','MAPE без изменения','1,90 ₽','2,90 ₽','3,10 ₽'])assert.ok(text.includes(phrase),phrase);
});
test('unknown outcomes suppress P&L and stress P&L without suppressing denominators',async()=>{
  const value=fixture();for(const f of value.folds)for(const report of [f.report,f.current_year_report]){
    report.paper={status:'incomplete',selected_candidates:70,trades:27,unknown_trades:1,net_return_pct:999,stress_trades:27,stress_unknown_trades:1,stress_return_pct:777};report.always_buy={status:'ok',trades:20,unknown_trades:1,net_return_pct:888};
  }
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('temporal-result'));assert.match(text,/Неполные данные · доходность неизвестна/);assert.match(text,/Выбрано кандидатов по порогу/);assert.match(text,/27 \/ 1/);assert.match(text,/70/);assert.doesNotMatch(text,/999|777|888/);
});
test('zero trades or absent stress counts never imply profitable zero results',()=>{
  assert.match(api.tradeResult({status:'ok',trades:0,unknown_trades:0,net_return_pct:0}),/Нет сделок/);
  assert.match(api.tradeResult({status:'ok',trades:2,unknown_trades:0,stress_return_pct:0},true),/Неполные данные/);
  assert.equal(api.tradeResult({status:'ok',trades:2,unknown_trades:0,net_return_pct:null}),'—');
});
test('model labels and notes are text nodes, never HTML',async()=>{
  const value=fixture();value.folds[0].label='<img src=x onerror=alert(1)>';value.note='<script>bad()</script>';
  const h=harness(value);await h.tick();const result=h.nodes.get('temporal-result');assert.match(h.text(result),/<img src=x onerror=alert\(1\)>/);assert.match(h.text(result),/<script>bad\(\)<\/script>/);assert.equal(result.innerHTML,undefined);
});
test('failed GET renders no fabricated metrics, answers or transport error',async()=>{
  for(const options of [{failure:true},{ok:false}]){const h=harness(fixture(),options);await h.tick();assert.match(h.nodes.get('temporal-status').textContent,/недоступен/);assert.equal(h.nodes.get('temporal-result').children.length,0);assert.doesNotMatch(h.text(h.nodes.get('temporal-result')),/private error/);}
});
test('date ranges reject impossible dates and percentage errors keep meaningful precision',()=>{
  assert.equal(api.range({start:'2026-01-01',end_exclusive:'2026-10-23'}),'01.01.2026 — 22.10.2026');assert.equal(api.range({start:'2026-02-30',end_exclusive:'2026-03-04'}),'—');assert.equal(api.date('2026-10-21T17:45:00'),'—');
  assert.equal(api.bps(88.340386),'0,8834%');assert.equal(api.bps(88.020851),'0,8802%');assert.equal(api.bps(null),'—');assert.equal(api.pct(8.55439),'+8,55%');
});
test('paper assumptions remain explicit and table scrolling hints are visible',async()=>{
  const h=harness(fixture());await h.tick();const root=h.nodes.get('temporal-result'),text=h.text(root);
  for(const phrase of ['Условный счёт 100 000 ₽','выше 0,4%','не более 25%','комиссия 0,05%','проскальзывание 0,05%','кандидат не равен исполненной сделке'])assert.ok(text.includes(phrase),phrase);
  const groups=elements(root,'temporal-group');assert.equal(groups.length,2);for(const group of groups){assert.ok(group.children.indexOf(elements(group,'temporal-hint')[0])<group.children.indexOf(elements(group,'temporal-scroll')[0]));}
});
test('scoped responsive CSS overrides table nowrap and template preserves retrospective caveats',()=>{
  const css=fs.readFileSync(require.resolve('../static/css/temporal_validation.css'),'utf8'),html=fs.readFileSync(require.resolve('../templates/_temporal_validation.html'),'utf8');
  assert.match(css,/table-layout:fixed/);assert.match(css,/\.workspace \.temporal-panel \.temporal-table th,\.workspace \.temporal-panel \.temporal-table td\{white-space:normal\}/);assert.match(html,/не ещё один независимый тест/);assert.match(html,/2026 год показан только до 22 октября/);
});
