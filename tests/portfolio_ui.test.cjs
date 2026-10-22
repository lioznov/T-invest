const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../templates/portfolio.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];

function harness(Chart,fetch){
  const makeNode=()=>({hidden:false,textContent:'',innerHTML:'',className:'',dataset:{},
    classList:{values:new Set(),toggle(key,on){if(on)this.values.add(key);else this.values.delete(key);}}});
  const nodes=Object.fromEntries(['portfolio-chart-card','portfolio-overview','portfolioChartStatus','portfolioChart','portfolioStatus','portfolioBody'].map(id=>[id,makeNode()]));
  const wrapper=makeNode();nodes['portfolio-chart-card'].querySelector=()=>wrapper;
  nodes.portfolioChart.getContext=()=>({});
  const context=vm.createContext({document:{getElementById:id=>nodes[id]},Chart,
    fetch:fetch||(()=>new Promise(()=>{})),AbortSignal:{timeout:()=>({})},Intl});
  vm.runInContext(script,context);
  return {nodes,wrapper,run:code=>vm.runInContext(code,context)};
}

test('empty holdings produce a compact state without a blank canvas',()=>{
  const h=harness();h.run('renderChart([])');
  assert.equal(h.nodes['portfolio-chart-card'].dataset.state,'empty');
  assert.ok(h.wrapper.hidden);assert.ok(h.nodes['portfolio-chart-card'].classList.values.has('is-empty'));
  assert.match(h.nodes.portfolioChartStatus.textContent,/нет позиций/);
});
test('missing chart library does not claim the account is disconnected',()=>{
  const h=harness();h.run("renderChart([{instrument_type:'share',total_sum:100}])");
  assert.equal(h.nodes['portfolio-chart-card'].dataset.state,'unavailable');
  assert.match(h.nodes.portfolioChartStatus.textContent,/Библиотека/);
  assert.ok(h.wrapper.hidden);
});
test('a valid chart expands only when there is data and a renderer',()=>{
  let config;const h=harness(function(ctx,options){config=options;});
  h.run("renderChart([{instrument_type:'share',total_sum:100}])");
  assert.equal(h.nodes['portfolio-chart-card'].dataset.state,'ready');
  assert.equal(h.wrapper.hidden,false);assert.ok(h.nodes.portfolioChartStatus.hidden);
  assert.ok(h.nodes['portfolio-overview'].classList.values.has('has-chart'));
  assert.equal(config.data.datasets[0].data[0],100);
});
test('a renderer error is isolated to the diagram',()=>{
  const h=harness(function(){throw Error('canvas');});
  assert.doesNotThrow(()=>h.run("renderChart([{instrument_type:'share',total_sum:100}])"));
  assert.equal(h.nodes['portfolio-chart-card'].dataset.state,'unavailable');
  assert.match(h.nodes.portfolioChartStatus.textContent,/таблице/);
});
test('an account API failure collapses the diagram and makes its reason visible',async()=>{
  const h=harness(undefined,async()=>({json:async()=>({status:'error'})}));
  await new Promise(setImmediate);
  assert.equal(h.nodes['portfolio-chart-card'].dataset.state,'unavailable');
  assert.ok(h.wrapper.hidden);
  assert.equal(h.nodes.portfolioStatus.className,'notice');
  assert.match(h.nodes.portfolioStatus.textContent,/Не удалось получить портфель/);
});
