const test = require('node:test');
const assert = require('node:assert/strict');
const {mount, outcome} = require('../static/js/forecast_workflow.js');

const selection = {ticker:'SBER',mode:'live'};
const time = Date.parse('2026-11-19T06:30:00Z');
const forecast = {
  as_of:'2026-11-18T14:45:00Z',
  target_at:'2026-11-19T07:00:00Z',
  created_at:'2026-11-18T14:46:00Z',
  anchor_price:100,
  predicted_price:105,
  lower_price:97,
  upper_price:110,
  direction:'uncertain',
};
const ready = {status:'ok',ticker:'SBER',forecast};

function harness(responses) {
  const nodes = new Map();
  const listeners = new Map();
  const timers = new Map();
  const calls = [];
  let timer = 0;
  let liveModeClicks = 0;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      dataset:{}, hidden:false, disabled:false, textContent:'',
      events:{}, addEventListener(event, handler){ this.events[event] = handler; },
      elements:{namedItem:() => ({value:'example-csrf'})},
    });
    return nodes.get(id);
  }
  const document = {
    getElementById:node, addEventListener:(type, callback) => listeners.set(type,callback),
    querySelector:() => ({click(){liveModeClicks++;}}),
  };
  const env = {
    AbortController,
    setTimeout(callback, delay) { const id=++timer; timers.set(id, {callback,delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      calls.push({url,options});
      const value = await responses.shift();
      if (value instanceof Error) throw value;
      return {ok:true,json:async()=>value};
    },
  };
  const app = mount(document,env);
  return {
    app, node, calls, timers, liveModeClicks:() => liveModeClicks,
    tick:() => new Promise(resolve => setImmediate(resolve)),
    submit:() => node('forecast-job-form').events.submit({preventDefault(){}}),
  };
}

test('forecast belongs to the selected live ticker and compares to the latest price until the target', () => {
  const shown = outcome(ready, selection, {...selection,price:102}, time);
  assert.equal(shown.delta,3);
  assert.equal(shown.displayedQuote,102);
  assert.equal(outcome(ready, selection, {ticker:'GAZP',mode:'live',price:102}, time).delta,5);
  assert.equal(outcome(ready, selection, {...selection,price:102,at:'2026-11-18T14:30:00Z'}, time).delta,5);
  assert.equal(outcome(ready, {...selection,mode:'demo'}, null, time),null);
  assert.equal(outcome(ready, {...selection,ticker:'GAZP'},null,time),null);
  assert.equal(outcome(ready, selection,null,time-24*60*60*1000),null);
});

test('actual price appears after target and lookup is offered only after the candle ends', () => {
  const target = Date.parse(forecast.target_at);
  const pending = outcome(ready, selection, {...selection,price:110}, target);
  assert.equal(pending.historical,true);
  assert.equal(pending.delta,5);
  assert.equal(pending.canCompare,false);
  assert.equal(outcome(ready, selection,null,target+15*60000).canCompare,true);
  const compared = {...ready,forecast:{...forecast,actual_price:102,historical:true}};
  assert.equal(outcome(compared,selection,null,target+15*60000).actual,102);
  assert.equal(outcome(compared,selection,null,target+15*60000).canCompare,false);
});

test('selection is read only; one click sends a CSRF-protected request and follows server progress', async () => {
  const h = harness([
    {status:'idle',ticker:'SBER'},
    {status:'running',ticker:'SBER',stage:'training'},
    {status:'running',ticker:'SBER',stage:'forecasting'},
    ready,
  ]);
  h.app.select(selection);
  await h.tick();
  assert.equal(h.calls.length,1);
  assert.match(h.calls[0].url, /\?ticker=SBER$/);
  assert.equal(h.calls[0].options.method,undefined);
  h.submit();
  h.submit();
  await h.tick();
  assert.equal(h.calls.length,2);
  assert.equal(h.calls[1].options.method,'POST');
  assert.equal(h.calls[1].options.body,'{"ticker":"SBER"}');
  assert.equal(h.calls[1].options.headers['X-CSRFToken'],'example-csrf');
  assert.match(h.node('forecast-job-status').textContent,/Готовим модель/);
  assert.equal(h.node('forecast-run').disabled,true);
  for (const phrase of [/Рассчитываем/, /Цена открытия|^$/]) {
    const timer = [...h.timers].find(([,entry])=>entry.delay===1800);
    h.timers.delete(timer[0]);
    timer[1].callback();
    await h.tick();
    // Historic fixture outcome has a stable displayed price, independent of today's date.
    if (h.calls.length===3) assert.match(h.node('forecast-job-status').textContent,phrase);
  }
  assert.equal(h.calls.length,4);
  assert.equal(h.node('forecast-run').disabled,false);
});

test('demo offers the market mode without starting calculation; a failed request retries as a read', async () => {
  const h = harness([new Error('transport private details'), {status:'idle',ticker:'SBER'}]);
  h.app.select({...selection,mode:'demo'});
  h.submit();
  assert.equal(h.calls.length,0);
  assert.equal(h.liveModeClicks(),1);
  h.app.select(selection);
  await h.tick();
  assert.match(h.node('forecast-job-status').textContent,/Не удалось связаться/);
  assert.equal(h.node('forecast-run').textContent,'Проверить состояние');
  h.submit();
  await h.tick();
  assert.equal(h.calls[1].options.method,undefined);
  assert.equal(h.node('forecast-run').textContent,'Рассчитать прогноз');
});

test('after the target a separate comparison request keeps the forecast visible and then shows the measured error', async () => {
  const pastForecast = {...forecast, as_of:'2020-11-18T14:45:00Z',
    target_at:'2020-11-19T07:00:00Z',historical:true};
  const h = harness([
    {...ready,forecast:pastForecast},
    {status:'running',ticker:'SBER',stage:'checking_actual'},
    {...ready,forecast:{...pastForecast,actual_price:102,
      absolute_error_rub:3,absolute_error_pct:2.94}},
  ]);
  h.app.select(selection);
  await h.tick();
  const compare = h.node('forecast-compare');
  assert.equal(compare.hidden,false);
  compare.events.click();
  await h.tick();
  assert.equal(h.calls[1].options.body,'{"ticker":"SBER","action":"compare"}');
  assert.match(h.node('forecast-overview-price').textContent,/105,00 ₽/);
  const timer = [...h.timers].find(([,entry])=>entry.delay===1800);
  timer[1].callback();
  await h.tick();
  assert.equal(compare.hidden,true);
  assert.match(h.node('forecast-overview-actual').textContent,/102,00 ₽ · Ошибка 3 ₽ \(2,94%\)/);
});

test('a delayed response for the previous ticker is ignored when the stock changes', async () => {
  let release;
  const delayed = new Promise(resolve => { release=resolve; });
  const h = harness([delayed,{status:'idle',ticker:'GAZP',message:'Готов к расчёту GAZP.'}]);
  h.app.select(selection);
  h.app.select({...selection,ticker:'GAZP'});
  await h.tick();
  assert.equal(h.calls[0].options.signal.aborted,true);
  release({status:'error',ticker:'SBER',message:'Старый запрос'});
  await h.tick();
  assert.equal(h.node('forecast-job-status').textContent,'Готов к расчёту GAZP.');
});
