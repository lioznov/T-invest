const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../static/js/vk_alerts.js'),'utf8');

function harness(initial={}){
  const state={enabled:false,configured:true,worker_running:false,tickers:['SBER'],items:[],collector:{alive:true,active_tickers:['SBER']},...initial};
  const nodes=new Map();
  function node(){return {textContent:'',value:'',checked:false,disabled:true,children:[],
    append(...children){this.children.push(...children);},replaceChildren(){this.children=[];},
    addEventListener(name,handler){this[name]=handler;},querySelector(){return {value:'test-csrf'};}};}
  for(const id of ['vk-form','vk-badge','vk-status','vk-items','vk-tickers','vk-enabled','vk-save','vk-collector-command','vk-coverage','vk-more','vk-test','vk-test-status'])nodes.set(id,node());
  const calls=[];
  const context={document:{getElementById:id=>nodes.get(id),createElement:node},
    fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>options.method==='POST' && JSON.parse(options.body).action==='test'?{test:state.testResult || {status:'ok',message:'ВК принял сообщение.'}}:state};},
    AbortController,Date,setTimeout,clearTimeout,setInterval(){},JSON,Set,Error};
  vm.runInNewContext(script,context);
  return {nodes,calls,state,tick:()=>new Promise(resolve=>setImmediate(resolve))};
}

test('renders off, same-bot endpoint and collector command without sending a ВК message',async()=>{
  const h=harness();await h.tick();
  assert.equal(h.nodes.get('vk-badge').textContent,'Выключено');
  assert.equal(h.calls[0].url,'/api/vk-alerts/');
  assert.equal(h.nodes.get('vk-collector-command').textContent,'python manage.py watch_vk --seconds 28800');
  assert.equal(h.nodes.get('vk-save').disabled,false);
});

test('POST has CSRF, explicit checkbox and normalized deduplicated ticker list',async()=>{
  const h=harness();await h.tick();
  h.nodes.get('vk-tickers').value='sber, GAZP, sber';
  h.nodes.get('vk-enabled').checked=true;
  await h.nodes.get('vk-form').submit({preventDefault(){}});
  const request=h.calls[1].options;
  assert.equal(request.method,'POST');
  assert.equal(request.headers['X-CSRFToken'],'test-csrf');
  assert.deepEqual(JSON.parse(request.body),{enabled:true,tickers:['SBER','GAZP']});
});

test('malformed or empty tickers never save',async()=>{
  const h=harness();await h.tick();
  for(const value of ['', '<script>']){
    h.nodes.get('vk-tickers').value=value;
    await h.nodes.get('vk-form').submit({preventDefault(){}});
  }
  assert.equal(h.calls.length,1);
});

test('large multiline list is accepted, deduplicated, and sent without a five-share limit',async()=>{
  const h=harness();await h.tick();
  const values=Array.from({length:500},(_,i)=>`STOCK${i}`);
  h.nodes.get('vk-tickers').value=values.join('\n')+'\nSTOCK0';
  await h.nodes.get('vk-form').submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(h.calls[1].options.body).tickers,values);
});

test('large lists render 50 cards at a time and disclose inactive coverage',async()=>{
  const tickers=Array.from({length:123},(_,i)=>`STOCK${i}`);
  const h=harness({tickers,collector:{alive:true,active_tickers:['STOCK0'],batch:1,batch_count:3}});await h.tick();
  assert.equal(h.nodes.get('vk-items').children.length,50);
  assert.equal(h.nodes.get('vk-more').hidden,false);
  assert.match(h.nodes.get('vk-coverage').textContent,/123/);
  assert.match(h.nodes.get('vk-coverage').textContent,/группа 1\/3/);
  assert.match(h.nodes.get('vk-items').children[1].children[1].textContent,/вне активного/);
  h.nodes.get('vk-more').click();
  assert.equal(h.nodes.get('vk-items').children.length,100);
  h.nodes.get('vk-more').click();
  assert.equal(h.nodes.get('vk-items').children.length,123);
  assert.equal(h.nodes.get('vk-more').hidden,true);
});

test('delivery acknowledgment and untrusted text use textContent, not HTML',async()=>{
  const h=harness({enabled:true,worker_running:true,items:[{ticker:'SBER',decision:'WAIT',detail:'<script>bad</script>',
    delivery_detail:'ВК подтвердил отправку.',checked:Date.now()/1000,last_sent:1}]});await h.tick();
  const card=h.nodes.get('vk-items').children[0];
  assert.equal(card.children[3].textContent,'ВК подтвердил отправку.');
  assert.match(card.children[1].textContent,/<script>/);
  assert.equal(card.children[1].innerHTML,undefined);
  assert.equal(h.nodes.get('vk-badge').textContent,'Наблюдение включено');
});

test('a stale LONG card is not displayed as a current candidate',async()=>{
  const h=harness({enabled:true,items:[{ticker:'SBER',decision:'LONG',detail:'Все совпало',checked:1}]});await h.tick();
  assert.match(h.nodes.get('vk-items').children[0].children[1].textContent,/старая оценка не является/);
  assert.doesNotMatch(h.nodes.get('vk-items').children[0].children[1].textContent,/Кандидат покупки/);
});

test('missing credentials is a visible unavailable state, not working notification claim',async()=>{
  const h=harness({configured:false});await h.tick();
  assert.equal(h.nodes.get('vk-badge').textContent,'Бот не настроен');
  assert.match(h.nodes.get('vk-status').textContent,/VK_COMMUNITY_TOKEN/);
});

test('test delivery requires an explicit click, posts only its action and CSRF',async()=>{
  const h=harness();await h.tick();
  assert.equal(h.calls.length,1);
  assert.equal(h.nodes.get('vk-test').disabled,false);
  await h.nodes.get('vk-test').click();
  assert.equal(h.calls.length,2);
  assert.equal(h.calls[1].url,'/api/vk-alerts/');
  assert.equal(h.calls[1].options.headers['X-CSRFToken'],'test-csrf');
  assert.deepEqual(JSON.parse(h.calls[1].options.body),{action:'test'});
  assert.equal(h.nodes.get('vk-enabled').checked,false);
  assert.match(h.nodes.get('vk-test-status').textContent,/ВК принял/);
});

test('unconfigured test cannot send and unknown results do not retry',async()=>{
  const unavailable=harness({configured:false});await unavailable.tick();
  assert.equal(unavailable.nodes.get('vk-test').disabled,true);
  await unavailable.nodes.get('vk-test').click();
  assert.equal(unavailable.calls.length,1);
  const h=harness({testResult:{status:'unknown',message:'Приём не подтверждён. Проверь диалог.'}});await h.tick();
  await h.nodes.get('vk-test').click();await h.tick();
  assert.equal(h.calls.length,2);
  assert.match(h.nodes.get('vk-test-status').textContent,/не подтверждён/);
  assert.equal(h.nodes.get('vk-test').disabled,false);
});

test('VK cards reuse the responsive notification style',async()=>{
  const h=harness();await h.tick();
  assert.equal(h.nodes.get('vk-items').children[0].className,'telegram-item');
});
