const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../static/js/notification_settings.js'),'utf8');

function harness(mode='analysis-single',initial={},storage={}){
  const make=()=>({textContent:'',checked:false,disabled:true,dataset:{},handlers:{},addEventListener(k,fn){this.handlers[k]=fn;}});
  const channels={};for(const c of ['vk','telegram'])channels[c]={enabled:false,configured:true,tickers:['SBER'],revision:0,result:'',message:'Сигналов анализа пока не отправляли.',setup:'Нужны ключи в .env.',collector:{alive:false},...initial[c]};
  const state={mode,channels},nodes=new Map(),rows={};
  for(const c of ['vk','telegram']){rows[c]=new Map();for(const s of ['input','[data-channel-state]','[data-channel-connection]','[data-channel-delivery]','[data-channel-test]','[data-channel-test-status]'])rows[c].set(s,make());}
  for(const s of ['[data-channel-count]','[data-settings-status]','[data-selection]','[data-monitor-status]'])nodes.set(s,make());
  const panel={dataset:{notificationMode:mode},querySelector(s){if(s.includes('data-channel="')){const c=s.includes('"telegram"')?'telegram':'vk';return {querySelector:s=>rows[c].get(s)};}if(s==='[name=csrfmiddlewaretoken]')return {value:'test-csrf'};return nodes.get(s);}};
  const ticker={textContent:'SBER · 10 шт. в лоте'},saved={'vorby.watchlist':JSON.stringify(['SBER','GAZP']),...storage},calls=[];
  let poll;
  const context={document:{querySelector:()=>panel,querySelectorAll:()=>[],getElementById:id=>id==='ticker-label'?ticker:null,body:{classList:{contains:()=>true}}},
    localStorage:{getItem:key=>saved[key]??null,setItem:(key,value)=>{saved[key]=value;}},
    fetch:async(url,options)=>{calls.push({url,options});const body=options.body&&JSON.parse(options.body);
      if(body?.action==='test')return {ok:true,json:async()=>({...state,test:{status:'ok',message:'Сервис принял тест.'}})};
      if(body){const c=channels[body.channel];c.enabled=body.enabled;c.revision++;if(body.tickers)c.tickers=body.tickers;}
      return {ok:true,json:async()=>JSON.parse(JSON.stringify(state))};},
    AbortController,setTimeout,clearTimeout,setInterval(fn){poll=fn;},Date,JSON,Set,Error,encodeURIComponent};
  vm.runInNewContext(script,context);
  return {calls,channels,rows,nodes,ticker,saved,context,state,poll:()=>poll(),tick:()=>new Promise(r=>setImmediate(r))};
}
const posts=h=>h.calls.filter(c=>c.options.method==='POST').map(c=>JSON.parse(c.options.body));

test('the same two switches load independently on all four modes without sending',async()=>{
  for(const mode of ['analysis-single','analysis-multi','radar-single','radar-multi']){
    const h=harness(mode);await h.tick();assert.equal(posts(h).length,0);
    assert.equal(h.calls[0].url,'/api/notifications/?mode='+mode);
    assert.equal(h.nodes.get('[data-channel-count]').textContent,'0 из 2 каналов');
  }
});

test('switches independently save one channel, source tickers and CSRF',async()=>{
  const h=harness();await h.tick();
  for(const channel of ['vk','telegram']){h.rows[channel].get('input').checked=true;await h.rows[channel].get('input').handlers.change();}
  h.rows.vk.get('input').checked=false;await h.rows.vk.get('input').handlers.change();
  assert.deepEqual(posts(h),[{channel:'vk',enabled:true,tickers:['SBER']},{channel:'telegram',enabled:true,tickers:['SBER']},{channel:'vk',enabled:false,tickers:['SBER']}]);
  assert.equal(h.rows.telegram.get('input').checked,true);assert.equal(h.rows.vk.get('input').checked,false);
  assert.equal(h.calls.find(c=>c.options.method==='POST').options.headers['X-CSRFToken'],'test-csrf');
});

test('multi-analysis uses the validated persisted watchlist, not text being typed',async()=>{
  const h=harness('analysis-multi');await h.tick();h.rows.vk.get('input').checked=true;
  await h.rows.vk.get('input').handlers.change();assert.deepEqual(posts(h)[0].tickers,['SBER','GAZP']);
  assert.match(h.nodes.get('[data-selection]').textContent,/SBER, GAZP/);
});

test('changing the single selected instrument updates enabled channels once',async()=>{
  const h=harness('analysis-single',{vk:{enabled:true}});await h.tick();h.ticker.textContent='GAZP · 10 шт. в лоте';
  await h.poll();assert.deepEqual(posts(h),[{channel:'vk',enabled:true,tickers:['GAZP']}]);
  await h.poll();assert.equal(posts(h).length,1);assert.equal(h.channels.telegram.enabled,false);
});

test('removing every share suspends the analysis list without silently watching old stocks',async()=>{
  const h=harness('analysis-multi',{vk:{enabled:true,tickers:['SBER','GAZP']}});await h.tick();
  h.saved['vorby.watchlist']='[]';await h.poll();
  assert.deepEqual(posts(h),[{channel:'vk',enabled:true,tickers:[]}]);assert.match(h.nodes.get('[data-selection]').textContent,/Список пуст/);
});

test('page reload and polling do not repeat unchanged settings or send tests',async()=>{
  const h=harness('analysis-single',{vk:{enabled:true}});await h.tick();await h.poll();await h.poll();
  assert.equal(posts(h).length,0);assert.equal(h.rows.vk.get('input').checked,true);
});

test('provider refusal and unknown result are visible text, not a delivery claim',async()=>{
  const h=harness('radar-multi',{vk:{enabled:true,result:'rejected',message:'ВК (901): Нет разрешения.'},telegram:{enabled:true,result:'unknown',message:'<script>bad</script>'}});await h.tick();
  assert.match(h.rows.vk.get('[data-channel-delivery]').textContent,/Ошибка отправки/);
  assert.match(h.rows.telegram.get('[data-channel-delivery]').textContent,/Доставка не подтверждена/);
  assert.equal(h.rows.telegram.get('[data-channel-delivery]').innerHTML,undefined);
  assert.equal(posts(h).length,0);
});

test('unconfigured channel cannot enable but can disable previously saved subscription',async()=>{
  const h=harness('analysis-single',{vk:{configured:false},telegram:{configured:false,enabled:true}});await h.tick();
  assert.equal(h.rows.vk.get('input').disabled,true);assert.equal(h.rows.telegram.get('input').disabled,false);
  assert.match(h.rows.vk.get('[data-channel-connection]').textContent,/\.env/);
});

test('test action requires a button click, never includes receivers and never enables a channel',async()=>{
  const h=harness();await h.tick();await h.rows.telegram.get('[data-channel-test]').handlers.click();
  assert.deepEqual(posts(h),[{channel:'telegram',action:'test'}]);assert.equal(h.channels.telegram.enabled,false);
  assert.match(h.rows.telegram.get('[data-channel-test-status]').textContent,/принял тест/);
});

test('radar Telegram preference migrates once from the existing browser key',async()=>{
  const h=harness('radar-single',{}, {'investpro_bot_enabled':'true'});await h.tick();
  assert.deepEqual(posts(h),[{channel:'telegram',enabled:true}]);await h.poll();assert.equal(posts(h).length,1);
  const a=harness('analysis-single',{}, {'investpro_bot_enabled':'true'});await a.tick();assert.equal(posts(a).length,0);
});

test('network errors preserve the saved state and never trigger a test-message retry',async()=>{
  const h=harness();await h.tick();h.context.fetch=async()=>{throw Error('offline');};
  h.rows.vk.get('input').checked=true;await h.rows.vk.get('input').handlers.change();
  assert.match(h.nodes.get('[data-settings-status]').textContent,/не подтверждено/);
  assert.equal(posts(h).length,0);
});
