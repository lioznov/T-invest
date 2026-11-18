const test=require('node:test');
const assert=require('node:assert/strict');
const {chartLayout,scenarioPrice,timeTicks}=require('../static/js/chart_ui.js');

test('mobile chart coordinates match its real width instead of scaling an 800px canvas',()=>{
  const layout=chartLayout(313.6,280);
  assert.equal(layout.width,314);assert.equal(layout.height,280);assert.equal(layout.ticks,3);
  const effectiveFont=11*Math.min(313.6/layout.width,280/layout.height);
  assert.ok(effectiveFont>10.9);
  assert.ok(layout.width-layout.left-layout.right>200);
});
test('desktop chart preserves all available drawing space',()=>{
  const layout=chartLayout(940,310);
  assert.equal(layout.width,940);assert.equal(layout.ticks,5);
});
test('axes distinguish calendar dates and show Moscow time on both plots',()=>{
  const view=timeTicks(chartLayout(314,280),[
    {time:'2026-11-17T15:00:00Z'},{time:'2026-11-18T07:00:00Z'}
  ],i=>14+i*180);
  for(const value of ['17.11','18.11','18:00','10:00'])assert.ok(view.includes(value));
});
test('unmeasured or extreme small chart containers have safe layout defaults',()=>{
  assert.equal(chartLayout(NaN,NaN).width,800);
  assert.equal(chartLayout(0,0).height,310);
  assert.equal(chartLayout(100,100).width,240);
});
test('ordinary editable scenario prices use kopecks rather than nine decimals',()=>{
  assert.equal(scenarioPrice(282.628368451),'282.63');
  assert.equal(scenarioPrice(281.203360517),'281.20');
  assert.equal(scenarioPrice(285.478384320),'285.48');
});
test('sub-rouble prices keep precision and never silently become zero',()=>{
  assert.equal(scenarioPrice(.01234567),'0.01234567');
  assert.equal(scenarioPrice(.5),'0.5');
  assert.equal(Number(scenarioPrice(1e-10)),1e-10);
});
test('missing or invalid prices do not appear as zero or NaN',()=>{
  for(const value of [null,undefined,NaN,Infinity,-1,0])assert.equal(scenarioPrice(value),'');
});
