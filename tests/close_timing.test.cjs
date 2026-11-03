const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const api=require('../static/js/close_timing.js');
const script=fs.readFileSync(require.resolve('../static/js/close_timing.js'),'utf8');

function fixture(){
  const paper={status:'ok',trades:8,unknown_trades:0,net_return_pct:-.38,stress_return_pct:-.5,stress_trades:8,stress_unknown_trades:0};
  const period={samples:20,days:10,eligible_forecasts:23,unknown_targets:3,mae_bps:55,baseline_mae_bps:50,direction_accuracy_pct:49.6,direction_nonzero_samples:19,coverage_pct:80,interval_width_bps:250,abstention_pct:95,paper,always_buy:{...paper},start:'2026-09-01',end_exclusive:'2026-10-03'};
  const report={trained_rows:200,calibration_rows:50,periods:{test:structuredClone(period),later:structuredClone(period)}};
  return {status:'historical',entry_allowed:false,notice:'Это исследование, не сигнал.',note:'Прибыль не доказана.',paired_note:'Пересечение входных данных, не удаляем неизвестные исходы.',variants:[['control','17:45','18:00'],['main_hour','18:00','18:15'],['evening_hour','22:45','23:00']].map(([id,cutoff,entry])=>({id,cutoff,entry,label:id,report:structuredClone(report),paired_report:structuredClone(report)}))};
}
function harness(value,options={}){
  const nodes=new Map();
  function node(tag='div'){return {tagName:tag,textContent:'',children:[],attributes:{},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(name,text){this.attributes[name]=text;}};}
  for(const id of ['close-timing-research','close-timing-status','close-timing-result'])nodes.set(id,node());
  const calls=[];
  const context={document:{getElementById:id=>nodes.get(id),createElement:node},
    fetch:async(url,request)=>{calls.push({url,request});if(options.failure)throw new Error('private error');return {ok:options.ok!==false,json:async()=>value};},Number,Date,Set,Array,module:undefined};
  vm.runInNewContext(script,context);
  const text=item=>item.textContent+item.children.map(text).join(' ');
  return {nodes,calls,text,tick:()=>new Promise(resolve=>setImmediate(resolve))};
}

test('only the three fixed historical clock variants can be shown',()=>{
  assert.equal(api.valid(fixture()),true);
  for(const bad of [null,{}, {...fixture(),status:'ok'}, {...fixture(),entry_allowed:true}])assert.equal(api.valid(bad),false);
  const duplicate=fixture();duplicate.variants[1]=duplicate.variants[0];assert.equal(api.valid(duplicate),false);
  const changed=fixture();changed.variants[0].cutoff='17:44';assert.equal(api.valid(changed),false);
});
test('loads only once using local read-only GET and never starts timers or training',async()=>{
  const h=harness(fixture());await h.tick();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/close-timing/');assert.equal(h.calls[0].request.method,undefined);assert.equal(h.calls[0].request.cache,'no-store');
  assert.match(h.nodes.get('close-timing-status').textContent,/Только историческое исследование/);
});
test('shows all three clocks, both periods, baselines and visible denominator counts',async()=>{
  const h=harness(fixture());await h.tick();const text=h.text(h.nodes.get('close-timing-result'));
  for(const label of ['17:45','18:00','22:45','Первая проверка','Более поздний период','Средняя ошибка модели','Ошибка «цена не изменится»','n=19','0,55%','0,50%','-0,38%','-0,50%','23 / 3','8 / 0','2,50%','Проверить на общих датах'])assert.ok(text.includes(label),label);
});
test('incomplete or unknown outcomes hide all profit claims including stress and baseline',async()=>{
  const value=fixture();
  for(const v of value.variants)for(const report of [v.report,v.paired_report])for(const p of Object.values(report.periods)){
    p.paper={status:'incomplete',trades:8,unknown_trades:1,net_return_pct:999,stress_return_pct:777,stress_trades:8,stress_unknown_trades:1};
    p.always_buy={status:'ok',trades:8,unknown_trades:2,net_return_pct:888};
  }
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('close-timing-result'));
  assert.match(text,/Неполные данные · доходность неизвестна/);assert.match(text,/8 \/ 1/);assert.doesNotMatch(text,/999|777|888/);
});
test('zero trades and absent stress counts do not imply zero profit',()=>{
  assert.match(api.tradeResult({status:'ok',trades:0,unknown_trades:0,net_return_pct:0}),/Нет сделок/);
  assert.match(api.tradeResult({status:'ok',trades:2,unknown_trades:0,stress_return_pct:0},true),/Неполные данные/);
  assert.match(api.tradeResult({status:'ok',trades:2,unknown_trades:0,stress_trades:0,stress_unknown_trades:0,stress_return_pct:0},true),/Нет сделок/);
  assert.equal(api.tradeResult({status:'ok',trades:2,unknown_trades:0,net_return_pct:null}),'—');
});
test('safe text nodes preserve a malicious-looking label and never use innerHTML',async()=>{
  const value=fixture();value.variants[0].label='<img src=x onerror=alert(1)>';value.note='<script>bad()</script>';
  const h=harness(value);await h.tick();const result=h.nodes.get('close-timing-result');
  assert.match(h.text(result),/<img src=x onerror=alert\(1\)>/);assert.match(h.text(result),/<script>bad\(\)<\/script>/);assert.equal(result.innerHTML,undefined);
});
test('failure produces no fabricated comparison or exposed transport error',async()=>{
  for(const options of [{failure:true},{ok:false}]){const h=harness(fixture(),options);await h.tick();assert.match(h.nodes.get('close-timing-status').textContent,/недоступно/);assert.equal(h.nodes.get('close-timing-result').children.length,0);assert.doesNotMatch(h.text(h.nodes.get('close-timing-result')),/private error/);}
});
test('date ranges reject impossible dates and use inclusive end',()=>{
  assert.equal(api.dateRange({start:'2026-09-01',end_exclusive:'2026-10-03'}),'01.09.2026 — 02.10.2026');
  for(const p of [{},{start:'2026-02-30',end_exclusive:'2026-03-04'},{start:'2026-10-03',end_exclusive:'2026-09-01'}])assert.equal(api.dateRange(p),null);
  assert.equal(api.bps(null),'—');assert.equal(api.pct(Infinity),'—');
});
test('comparison table overrides global nowrap and distinguishes the new control from frozen v1',()=>{
  const css=fs.readFileSync(require.resolve('../static/css/close_timing.css'),'utf8');
  const html=fs.readFileSync(require.resolve('../templates/_close_timing.html'),'utf8');
  assert.match(css,/table-layout:fixed/);
  assert.match(css,/\.workspace \.close-timing-panel \.close-timing-table th,\.workspace \.close-timing-panel \.close-timing-table td\{white-space:normal\}/);
  assert.match(html,/Контроль 17:45 здесь обучен заново/);
  assert.match(html,/без выходных/);
  assert.match(html,/не опубликованная ранее модель 17:45 v1/);
});
