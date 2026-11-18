const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const api=require('../static/js/morning_forecast.js');
const script=fs.readFileSync(path.join(__dirname,'../static/js/morning_forecast.js'),'utf8');
const future='2099-10-22T07:00:00+00:00';
const valid=()=>({status:'ok',ticker:'SBER',entry_allowed:false,forecast:{as_of:'2099-10-21T14:45:00+00:00',target_at:future,anchor_price:300,predicted_price:303,lower_price:295,upper_price:310,predicted_change_pct:1,direction:'up'}});
function harness(value=valid(),options={}){
  const nodes=new Map();
  function node(tag='div'){return {tagName:tag,textContent:'',value:'SBER',disabled:false,children:[],dataset:{},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},addEventListener(name,handler){this[name]=handler;}};}
  for(const id of ['morning-form','morning-ticker','morning-refresh','morning-badge','morning-status','morning-result','morning-report'])nodes.set(id,node());
  const calls=[],timers=new Map();let timerId=0;
  const context={document:{getElementById:id=>nodes.get(id),createElement:node,querySelectorAll:()=>[]},location:{search:options.search||'?mode=live&ticker=SBER'},
    fetch:async(url,request)=>{calls.push({url,request});if(options.failure)throw new Error('secret transport');return {ok:options.ok!==false,json:async()=>value};},
    AbortController,Date,URLSearchParams,encodeURIComponent,
    setTimeout:options.fakeTimers?(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id;}:setTimeout,
    clearTimeout:options.fakeTimers?id=>timers.delete(id):clearTimeout,Number,JSON,Set,Error};
  vm.runInNewContext(script,context);
  const text=item=>item.textContent+item.children.map(text).join(' ');
  return {nodes,calls,timers,text,tick:()=>new Promise(resolve=>setImmediate(resolve))};
}
test('only complete dated positive forecasts can render prices',()=>{
  assert.equal(api.forecastState(valid()).state,'ok');
  for(const broken of [null,{}, {...valid(),forecast:null},{...valid(),forecast:{...valid().forecast,anchor_price:0}},{...valid(),forecast:{...valid().forecast,target_at:'tomorrow'}}])assert.equal(api.forecastState(broken).forecast,null);
});
test('expired or explicitly historical forecasts are prominently archival',async()=>{
  const value=valid();value.forecast.as_of='2020-01-01T14:45:00+00:00';value.forecast.target_at='2020-01-02T07:00:00+00:00';
  assert.equal(api.forecastState(value).state,'historical');
  assert.equal(api.forecastState({...valid(),status:'historical'}).state,'historical');
  const h=harness(value);await h.tick();assert.equal(h.nodes.get('morning-badge').textContent,'Архивный прогноз');assert.match(h.nodes.get('morning-status').textContent,/не текущий сигнал покупки/);
});
test('current evening forecast becomes historical exactly at the 18:00 entry cutoff',()=>{
  const value=valid();value.forecast.as_of='2026-10-22T14:45:00+00:00';value.forecast.target_at='2026-10-23T07:00:00+00:00';
  assert.equal(api.forecastState(value,Date.parse('2026-10-22T14:59:59.999Z')).state,'ok');
  assert.equal(api.forecastState(value,Date.parse('2026-10-22T15:00:00.000Z')).state,'historical');
  assert.equal(api.forecastState(value,Date.parse('2026-10-22T20:00:00.000Z')).state,'historical');
});
test('local deadline timer archives an open current forecast without requesting API or repeating',async()=>{
  const value=valid(),now=Date.now();value.forecast.as_of=new Date(now-2*60*1000).toISOString();value.forecast.target_at=new Date(now+16*60*60*1000).toISOString();
  const h=harness(value,{fakeTimers:true});await h.tick();
  assert.equal(h.nodes.get('morning-badge').textContent,'Эксперимент · не сигнал');assert.equal(h.timers.size,1);
  const [id,scheduled]=[...h.timers][0];assert.ok(scheduled.delay>12*60*1000 && scheduled.delay<=13*60*1000);
  h.timers.delete(id);scheduled.callback();
  assert.equal(h.nodes.get('morning-badge').textContent,'Архивный прогноз');assert.equal(h.timers.size,0);assert.equal(h.calls.length,1);
  assert.match(h.nodes.get('morning-status').textContent,/окно до 18:00 МСК уже закрыто/);
});
test('refresh clears old expiry and stale callback cannot overwrite replacement payload',async()=>{
  const value=valid(),now=Date.now();value.forecast.as_of=new Date(now-2*60*1000).toISOString();value.forecast.target_at=new Date(now+16*60*60*1000).toISOString();
  const h=harness(value,{fakeTimers:true});await h.tick();const old=[...h.timers.values()][0];
  await h.nodes.get('morning-form').submit({preventDefault(){}});await h.tick();
  assert.equal(h.calls.length,2);assert.equal(h.timers.size,1);old.callback();
  assert.equal(h.nodes.get('morning-badge').textContent,'Эксперимент · не сигнал');
});
test('period boundaries use explicit backend dates and inclusive displayed end without timezone drift',async()=>{
  assert.equal(api.periodDateRange('2026-09-01','2026-10-03'),'01.09.2026 — 02.10.2026');
  assert.equal(api.periodDateRange('2026-10-03','2026-10-21'),'03.10.2026 — 20.10.2026');
  for(const pair of [[undefined,undefined],['2026-02-30','2026-03-03'],['2026-10-21','2026-10-03']])assert.equal(api.periodDateRange(...pair),null);
  const value=valid();value.report={periods:{test:{samples:0,start:'2026-09-01',end_exclusive:'2026-10-03'}}};
  const h=harness(value);await h.tick();assert.match(h.text(h.nodes.get('morning-report')),/01.09.2026 — 02.10.2026/);
});
test('demo never shows invented numeric forecasts even if payload contains numbers',async()=>{
  const h=harness({...valid(),status:'demo'},{search:'?mode=demo&ticker=SBER'});await h.tick();
  assert.equal(h.calls[0].url,'/api/morning-forecast/?ticker=SBER&mode=demo');
  assert.equal(h.nodes.get('morning-result').children.length,0);
  assert.match(h.nodes.get('morning-status').textContent,/Вымышленные цены/);
});
test('new forecast uses safe text nodes, is not an instruction and refresh is GET only',async()=>{
  const value=valid();value.message='<img src=x onerror=alert(1)>';const h=harness(value);await h.tick();
  assert.match(h.text(h.nodes.get('morning-result')),/<img src=x onerror=alert\(1\)>/);
  assert.equal(h.nodes.get('morning-result').innerHTML,undefined);
  assert.match(h.text(h.nodes.get('morning-result')),/не разрешение покупать/);
  assert.match(h.text(h.nodes.get('morning-result')),/не гарантия/);
  assert.equal(h.calls[0].request.method,undefined);
  assert.equal(h.nodes.get('morning-refresh').disabled,false);
});
test('no observations or zero trades are not presented as zero-percent profitable results',async()=>{
  const value=valid();value.report={trained_rows:100,calibration_rows:20,periods:{test:{samples:10,days:5,paper:{trades:0,net_return_pct:0,unknown_trades:0}},later:{samples:0,paper:{trades:0}}}};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/Условных сделок нет/);assert.match(text,/Нет оценённых прогнозов/);assert.match(text,/неизвестным исходом: 0/);assert.doesNotMatch(text,/Результат после расходов: 0/);
});
test('archive shows verified replay error in roubles and against the actual opening price',async()=>{
  const value=valid();
  value.replay={periods:{test:{
    samples:2,first_target_at:'2026-09-02T07:00:00Z',last_target_at:'2026-09-03T07:00:00Z',
    mae_rub:2.75,mae_pct:.8,baseline_mae_rub:2,baseline_mae_pct:.65,
    example:{ticker:'SBER',target_at:'2026-09-03T07:00:00Z',predicted_price:304,actual_price:300,error_rub:4,error_pct:1.33},
  }}};
  const h=harness(value);await h.tick();
  const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/2,75 ₽ · 0,80%/);
  assert.match(text,/Расчёт 304,00 ₽, факт 300,00 ₽, ошибка 4,00 ₽/);
  assert.match(text,/относительно фактической цены открытия/);
});
test('report compares errors with unchanged-price baseline and discloses costs and unknowns',async()=>{
  const value=valid();value.report={periods:{test:{samples:20,days:10,mae_bps:45,baseline_mae_bps:60,mean_baseline_mae_bps:58,direction_accuracy_pct:55,direction_nonzero_samples:18,coverage_pct:75,interval_width_bps:200,abstention_pct:92,paper:{status:'ok',trades:5,net_return_pct:-2,max_drawdown_pct:3,unknown_trades:0,stress_return_pct:-3,stress_unknown_trades:0}}}};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/Средняя ошибка модели/);assert.match(text,/0,45%/);assert.match(text,/0,60%/);assert.match(text,/0,58%/);assert.match(text,/-2,00%/);assert.match(text,/неизвестным исходом: 0/);assert.match(text,/не 80% вероятности заработать/);
  assert.match(text,/Ширина диапазона/);assert.match(text,/2,00%/);assert.match(text,/Модель не выбрала направление/);assert.match(text,/92,00%/);assert.match(text,/При удвоенных расходах: -3,00%/);assert.match(text,/n=18 без нулевых движений/);
});
test('paper policy explanation comes only from explicit server rules and is separate from interval direction',async()=>{
  const value=valid();value.report={periods:{}};value.rules={paper_threshold_pct:.4,paper_capital:100000,max_stock_allocation:.25,fee_bps_side:5,slippage_bps_side:5,cutoff:'17:45 МСК',entry:'18:00 МСК',target:'10:00 МСК следующего дня',paper_sizing:'whole lots'};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/выше \+0,40%/);assert.match(text,/до 25% в одной акции, только целые лоты/);assert.match(text,/комиссия 0,05% и проскальзывание 0,05%/);assert.match(text,/не выбор направления по всему диапазону/);
  const without=harness({...value,rules:undefined});await without.tick();assert.doesNotMatch(without.text(without.nodes.get('morning-report')),/выше \+0,40%/);
});
test('incomplete paper status suppresses every P&L claim including stress results',async()=>{
  const value=valid();value.report={periods:{test:{samples:20,days:10,paper:{status:'incomplete',trades:5,net_return_pct:123,max_drawdown_pct:8,unknown_trades:0,stress_return_pct:77,stress_unknown_trades:0}}}};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/Проверка условных сделок неполная/);assert.match(text,/При удвоенных расходах проверка неполная/);assert.doesNotMatch(text,/123,00|77,00|8,00/);
});
test('unknown outcomes suppress profits even if missing explicit incomplete status',async()=>{
  const value=valid();value.report={periods:{test:{samples:20,days:10,paper:{trades:5,net_return_pct:123,max_drawdown_pct:8,unknown_trades:2,stress_return_pct:77,stress_unknown_trades:3}}}};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/неизвестным исходом: 2/);assert.match(text,/Неизвестных исходов при удвоенных расходах: 3/);assert.doesNotMatch(text,/123,00|77,00|8,00/);
});
test('always-buy baseline is shown only with known complete outcomes and exposes counts',async()=>{
  const value=valid();value.report={periods:{test:{samples:20,days:10,unknown_targets:4,paper:{trades:0,selected_candidates:2,unknown_trades:2,status:'incomplete'},always_buy:{status:'ok',trades:20,unknown_trades:0,net_return_pct:1.5}},later:{samples:10,days:5,paper:{trades:0,unknown_trades:0},always_buy:{status:'incomplete',trades:7,unknown_trades:3,net_return_pct:90}}}};
  const h=harness(value);await h.tick();const text=h.text(h.nodes.get('morning-report'));
  assert.match(text,/Неизвестных утренних цен/);assert.match(text,/Выбрано кандидатов по условному правилу: 2/);
  assert.match(text,/20 условных сделок, после расходов \+1,50%/);assert.match(text,/проверка неполная, неизвестных исходов 3/);assert.doesNotMatch(text,/90,00%/);
});
test('malformed ticker never requests and server failures clear old forecasts',async()=>{
  const h=harness(valid());await h.tick();h.nodes.get('morning-ticker').value='<script>';
  h.nodes.get('morning-form').submit({preventDefault(){}});await h.tick();assert.equal(h.calls.length,1);
  const failed=harness(valid(),{failure:true});await failed.tick();assert.equal(failed.nodes.get('morning-badge').textContent,'Нет доступного прогноза');assert.doesNotMatch(failed.text(failed.nodes.get('morning-result')),/secret transport/);
});
test('down prediction never offers unauthorized shorts',async()=>{
  const value=valid();value.forecast.direction='down';const h=harness(value);await h.tick();assert.match(h.text(h.nodes.get('morning-result')),/не значит продавать в шорт/);
});
test('missing finite prices do not become zero, NaN or infinity',()=>{
  for(const value of [null,undefined,NaN,Infinity,-1,0,'303'])assert.equal(api.price(value),'—');
  assert.equal(api.percent(null),'—');assert.equal(api.date('2026-10-21'),'—');assert.equal(api.date('2026-10-21T17:45:00'),'—');
  assert.doesNotMatch(api.price(1e-10),/^0[,.]0+ ₽$/);
});
