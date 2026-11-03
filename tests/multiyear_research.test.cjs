const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const api=require('../static/js/multiyear_research.js');
const script=fs.readFileSync(require.resolve('../static/js/multiyear_research.js'),'utf8');

function fixture(){
  const paper={status:'ok',trades:8,unknown_trades:0,net_return_pct:-.38,stress_return_pct:-.5,stress_trades:8,stress_unknown_trades:0};
  const period={samples:200,days:100,eligible_forecasts:203,unknown_targets:3,mae_bps:55,baseline_mae_bps:50,mean_baseline_mae_bps:52,direction_accuracy_pct:49.6,direction_nonzero_samples:199,coverage_pct:80,interval_width_bps:250,abstention_pct:95,paper,always_buy:{...paper},start:'2025-01-01',end_exclusive:'2026-01-01'};
  const report={trained_rows:2000,calibration_rows:500,periods:{test:structuredClone(period),later:structuredClone({...period,start:'2026-01-01',end_exclusive:'2026-10-23'})}};
  const coverage={bars:7500,first_actual:'2000-01-05T00:00:00Z',last_actual:'2026-10-22T00:00:00Z',requested_from:'2000-01-01T00:00:00Z',to:'2026-10-23T00:00:00Z'};
  const variants=[['daily_full','daily_observational'],['control','intraday','17:45','18:00'],['main_hour','intraday','18:00','18:15'],['evening_hour','intraday','22:45','23:00']].map(([id,kind,cutoff,entry])=>({id,kind,cutoff,entry,label:id,report:structuredClone(report)}));
  return {status:'historical',entry_allowed:false,notice:'Это исследование, не сигнал.',note:'Прибыль не доказана.',publication_note:'Опубликованы все четыре проверки.',variants,coverage:{SBER:{day:structuredClone(coverage),intraday:{...coverage,bars:200000,requested_from:'2018-01-01T00:00:00Z',first_actual:'2018-03-01T00:00:00Z'}}}};
}
function harness(value,options={}){
  const nodes=new Map();
  function node(tag='div'){return {tagName:tag,textContent:'',children:[],attributes:{},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(name,text){this.attributes[name]=text;}};}
  for(const id of ['multiyear-research','multiyear-status','multiyear-result'])nodes.set(id,node());
  const calls=[];
  const context={document:{getElementById:id=>nodes.get(id),createElement:node},
    fetch:async(url,request)=>{calls.push({url,request});if(options.failure)throw new Error('private error');return {ok:options.ok!==false,json:async()=>value};},Number,Date,Set,Array,Object,module:undefined};
  vm.runInNewContext(script,context);
  const text=item=>item.textContent+item.children.map(text).join(' ');
  return {nodes,calls,text,tick:()=>new Promise(resolve=>setImmediate(resolve))};
}

test('only the four fixed historical model variants are accepted',()=>{
  assert.equal(api.valid(fixture()),true);
  for(const bad of [null,{}, {...fixture(),status:'ok'}, {...fixture(),entry_allowed:true}])assert.equal(api.valid(bad),false);
  const duplicate=fixture();duplicate.variants[1]=duplicate.variants[0];assert.equal(api.valid(duplicate),false);
  const changed=fixture();changed.variants[1].cutoff='17:44';assert.equal(api.valid(changed),false);
  const daily=fixture();daily.variants[0].kind='intraday';assert.equal(api.valid(daily),false);
});
test('loads exactly once via local GET with no timers, training or broker request',async()=>{
  const h=harness(fixture());await h.tick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/multiyear-research/');assert.equal(h.calls[0].request.method,undefined);assert.equal(h.calls[0].request.cache,'no-store');assert.match(h.nodes.get('multiyear-status').textContent,/Только историческая проверка/);
});
test('shows actual versus requested coverage without inventing earliest available year',async()=>{
  const h=harness(fixture());await h.tick();const text=h.text(h.nodes.get('multiyear-result'));
  for(const phrase of ['SBER','7 500','200 000','05.01.2000','01.03.2018','01.01.2018','22.10.2026','Запрошено','Фактически'])assert.ok(text.includes(phrase),phrase);
});
test('both periods, errors, direction denominators and all three intraday clocks are shown',async()=>{
  const h=harness(fixture());await h.tick();const text=h.text(h.nodes.get('multiyear-result'));
  for(const phrase of ['Проверка на 2025','на 2026','17:45','18:00','22:45','31.12.2025','22.10.2026','0,5500%','0,5000%','0,5200%','n=199','203 / 3','8 / 0','-0,38%','-0,50%'])assert.ok(text.includes(phrase),phrase);
});
test('daily full-bar model never publishes a fabricated close-fill return even in forged payload',async()=>{
  const value=fixture();for(const p of Object.values(value.variants[0].report.periods)){p.paper.net_return_pct=999;p.paper.stress_return_pct=777;p.always_buy.net_return_pct=888;}
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('multiyear-result'));
  assert.match(text,/покупка по закрытию не моделировалась/);assert.match(text,/Цель: следующее дневное открытие/);assert.match(text,/Не моделировалось/);assert.doesNotMatch(text,/999|777|888/);
});
test('incomplete outcomes hide intraday profits including stress and baseline',async()=>{
  const value=fixture();for(const v of value.variants)for(const p of Object.values(v.report.periods)){
    p.paper={status:'incomplete',trades:8,unknown_trades:1,net_return_pct:999,stress_return_pct:777,stress_trades:8,stress_unknown_trades:1};
    p.always_buy={status:'ok',trades:8,unknown_trades:2,net_return_pct:888};
  }
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('multiyear-result'));
  assert.match(text,/Неполные данные · доходность неизвестна/);assert.match(text,/8 \/ 1/);assert.doesNotMatch(text,/999|777|888/);
});
test('zero trades and missing stress counts never become zero-percent results',()=>{
  assert.match(api.tradeResult({status:'ok',trades:0,unknown_trades:0,net_return_pct:0}),/Нет сделок/);
  assert.match(api.tradeResult({status:'ok',trades:2,unknown_trades:0,stress_return_pct:0},true),/Неполные данные/);
  assert.match(api.tradeResult({status:'ok',trades:2,unknown_trades:0,stress_trades:0,stress_unknown_trades:0,stress_return_pct:0},true),/Нет сделок/);
  assert.equal(api.tradeResult({status:'ok',trades:2,unknown_trades:0,net_return_pct:null}),'—');
});
test('absent history explicitly says missing and malformed coverage fails closed',async()=>{
  const value=fixture();value.coverage.SBER.intraday={...value.coverage.SBER.intraday,bars:0,first_actual:null,last_actual:null};
  const h=harness(value);await h.tick();assert.match(h.text(h.nodes.get('multiyear-result')),/Фактическая история отсутствует/);
  for(const coverage of [{},{'<script>':value.coverage.SBER},{SBER:{day:{}}}])assert.equal(api.valid({...fixture(),coverage}),false);
  const bad=fixture();bad.coverage.SBER.day.first_actual='1990-01-01T00:00:00Z';assert.equal(api.valid(bad),false);
});
test('safe text nodes show labels and notes without interpreting HTML',async()=>{
  const value=fixture();value.variants[0].label='<img src=x onerror=alert(1)>';value.note='<script>bad()</script>';
  const h=harness(value);await h.tick();const result=h.nodes.get('multiyear-result');
  assert.match(h.text(result),/<img src=x onerror=alert\(1\)>/);assert.match(h.text(result),/<script>bad\(\)<\/script>/);assert.equal(result.innerHTML,undefined);
});
test('failure produces no fabricated dates, model scores or transport details',async()=>{
  for(const options of [{failure:true},{ok:false}]){const h=harness(fixture(),options);await h.tick();assert.match(h.nodes.get('multiyear-status').textContent,/недоступен/);assert.equal(h.nodes.get('multiyear-result').children.length,0);assert.doesNotMatch(h.text(h.nodes.get('multiyear-result')),/private error/);}
});
test('date formatting rejects missing timezone, impossible period and unknown numbers',()=>{
  assert.equal(api.dateRange({start:'2025-01-01',end_exclusive:'2026-01-01'}),'01.01.2025 — 31.12.2025');
  for(const p of [{},{start:'2026-02-30',end_exclusive:'2026-03-04'},{start:'2026-10-03',end_exclusive:'2026-09-01'}])assert.equal(api.dateRange(p),null);
  assert.equal(api.day('2026-10-23T10:00:00'),'—');assert.equal(api.day(null),'—');assert.equal(api.bps(null),'—');assert.equal(api.pct(Infinity),'—');
});
test('scoped CSS wraps table text and template distinguishes nontradable daily estimates',()=>{
  const css=fs.readFileSync(require.resolve('../static/css/multiyear_research.css'),'utf8');
  const html=fs.readFileSync(require.resolve('../templates/_multiyear_research.html'),'utf8');
  assert.match(css,/table-layout:fixed/);assert.match(css,/\.workspace \.multiyear-panel \.multiyear-table th,\.workspace \.multiyear-panel \.multiyear-table td\{white-space:normal\}/);
  assert.match(html,/покупка по закрытию не моделировалась/);assert.match(html,/Старые модели и их результаты не заменяются/);
});
function elements(root,cls){return [root,...root.children.flatMap(child=>elements(child,cls))].filter(node=>node.className===cls);}
test('paper summary discloses the fixed rule and costs and distinguishes selected from evaluated positions',async()=>{
  const value=fixture();
  value.variants[2].report.periods.test.paper={status:'incomplete',selected_candidates:70,trades:27,unknown_trades:1,net_return_pct:null,max_drawdown_pct:null,stress_trades:27,stress_unknown_trades:1,stress_return_pct:null};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('multiyear-result'));
  for(const phrase of ['более +0,4%','покупка в 18:00','в 10:00','не более 25%','комиссия 0,05%','проскальзывание 0,05%','Всего отобрано кандидатов','70','27 / 1','только рассчитанные до остановки'])assert.ok(text.includes(phrase),phrase);
});

test('compact results always describe the predefined 17:45 control, not a picked winning clock',async()=>{
  const value=fixture(),control=value.variants.find(v=>v.id==='control');
  Object.assign(control.report.periods.test.paper,{net_return_pct:8.55,stress_return_pct:4.39,trades:82,stress_trades:82});
  Object.assign(control.report.periods.later.paper,{net_return_pct:3.95,stress_return_pct:1.26,trades:63,stress_trades:63});
  for(const p of Object.values(value.variants[2].report.periods))Object.assign(p.paper,{net_return_pct:999,stress_return_pct:777});
  const h=harness(value);await h.tick();const summary=elements(h.nodes.get('multiyear-result'),'multiyear-overview')[0],text=h.text(summary);
  for(const phrase of ['17:45','заранее определённый контроль','не автоматический выбор лучшего','Условный счёт 100 000 ₽, не реальные заработанные деньги','2025','2026','+8,55%','+4,39%','+3,95%','+1,26%','82','63','цена без изменения'])assert.ok(text.includes(phrase),phrase);
  assert.doesNotMatch(text,/999|777/);
});
test('compact cards suppress unknown-outcome and zero-trade returns using the same safe rules',async()=>{
  const value=fixture(),control=value.variants.find(v=>v.id==='control');
  Object.assign(control.report.periods.test.paper,{status:'incomplete',unknown_trades:1,net_return_pct:999,stress_return_pct:777});
  Object.assign(control.report.periods.later.paper,{trades:0,stress_trades:0,net_return_pct:0,stress_return_pct:0});
  const h=harness(value);await h.tick();const text=h.text(elements(h.nodes.get('multiyear-result'),'multiyear-overview')[0]);
  assert.match(text,/Неполные данные · доходность неизвестна/);assert.match(text,/Нет сделок · доходность не определена/);assert.doesNotMatch(text,/999|777|0,00%/);
});
test('coverage is collapsed separately with dynamically calculated count and is below compact results',async()=>{
  const h=harness(fixture());await h.tick();const root=h.nodes.get('multiyear-result'),details=elements(root,'multiyear-coverage-details')[0];
  assert.equal(details.tagName,'details');assert.notEqual(details.open,true);assert.match(h.text(details.children[0]),/Сколько истории получено по каждой акции\? · 207 500 свечей/);
  assert.ok(root.children.indexOf(elements(root,'multiyear-overview')[0])<root.children.indexOf(details));assert.match(h.text(details),/разных интервалов/);
});
test('horizontal table hint is visible before both tables at every viewport width',async()=>{
  const h=harness(fixture());await h.tick();const root=h.nodes.get('multiyear-result'),sections=elements(root,'multiyear-period');
  assert.equal(sections.length,2);
  for(const section of sections){const hint=elements(section,'multiyear-table-hint')[0],scroll=elements(section,'multiyear-scroll')[0];assert.ok(section.children.indexOf(hint)<section.children.indexOf(scroll));assert.match(h.text(hint),/прокрути таблицу вправо/);}
});
test('price errors retain four decimal places so close baseline differences remain visible',()=>{
  assert.equal(api.bps(88.340386),'0,8834%');
  assert.equal(api.bps(88.020851),'0,8802%');
  assert.notEqual(api.bps(88.340386),api.bps(88.020851));
  assert.equal(api.pct(8.55439),'+8,55%');
});
