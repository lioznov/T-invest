const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../static/js/radar_vk.js'),'utf8');

function harness(scope='single',initial={}){
  const state={scope,enabled:false,configured:true,result:'',message:'Выключено',...initial};
  const toggle={checked:false,disabled:true,addEventListener(name,fn){this[name]=fn;}};
  const status={textContent:''},calls=[];
  const panel={dataset:{vkRadar:scope}};
  const context={document:{querySelector:q=>q==='[data-vk-radar]'?panel:{value:'test-csrf'},getElementById:id=>id==='radarVkToggle'?toggle:status},
    fetch:async(url,options)=>{
      calls.push({url,options});
      if(options.method==='POST'){state.enabled=JSON.parse(options.body).enabled;state.message='ВК принял сообщение.';}
      return {ok:true,json:async()=>state};
    },AbortController,setTimeout,clearTimeout,setInterval(){},JSON,Error,encodeURIComponent};
  vm.runInNewContext(script,context);
  return {state,toggle,status,calls,context,tick:()=>new Promise(resolve=>setImmediate(resolve))};
}

test('each radar loads its own server setting and never sends on initial GET',async()=>{
  for(const scope of ['single','multi']){
    const h=harness(scope);await h.tick();
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,`/api/radar-vk/?scope=${scope}`);
    assert.equal(h.calls[0].options.method,undefined);assert.equal(h.toggle.checked,false);
    assert.equal(h.toggle.disabled,false);
  }
});

test('explicit checkbox change posts only enabled and CSRF then reads saved state',async()=>{
  const h=harness();await h.tick();h.toggle.checked=true;
  await h.toggle.change();
  const post=h.calls.filter(c=>c.options.method==='POST');
  assert.equal(post.length,1);
  assert.deepEqual(JSON.parse(post[0].options.body),{enabled:true});
  assert.equal(post[0].options.headers['X-CSRFToken'],'test-csrf');
  assert.equal(h.calls.length,3);assert.equal(h.toggle.checked,true);
  assert.match(h.status.textContent,/ВК принял сообщение/);
});

test('missing credentials prevents enabling but allows disabling a saved enabled channel',async()=>{
  const h=harness('single',{configured:false});await h.tick();
  assert.equal(h.toggle.disabled,true);assert.match(h.status.textContent,/не настроен/);
  const active=harness('single',{configured:false,enabled:true});await active.tick();
  assert.equal(active.toggle.checked,true);assert.equal(active.toggle.disabled,false);
  active.toggle.checked=false;await active.toggle.change();
  assert.equal(active.toggle.checked,false);assert.equal(active.toggle.disabled,true);
});

test('ambiguous delivery is shown as text and does not cause automatic POST retries',async()=>{
  const h=harness('multi',{enabled:true,message:'<b>Приём не подтверждён</b>',result:'unknown'});await h.tick();
  assert.equal(h.calls.length,1);assert.match(h.status.textContent,/Приём не подтверждён/);
  assert.equal(h.status.innerHTML,undefined);
  assert.doesNotMatch(h.status.textContent,/ВК включён/);
});

test('enabled checkbox with VK rejection displays failure instead of claiming working delivery',async()=>{
  const h=harness('multi',{enabled:true,result:'rejected',message:'ВК (901): Нет разрешения писать получателю.'});await h.tick();
  assert.equal(h.toggle.checked,true);
  assert.match(h.status.textContent,/Сообщение не отправлено/);
  assert.match(h.status.textContent,/901/);
  assert.doesNotMatch(h.status.textContent,/ВК включён/);
  assert.equal(h.calls.length,1);
});

test('a failed connection disables the control and discloses unconfirmed state',async()=>{
  const h=harness();await h.tick();
  h.context.fetch=async()=>{throw Error('offline');};
  h.toggle.checked=true;await h.toggle.change();
  assert.equal(h.toggle.disabled,true);assert.match(h.status.textContent,/не подтверждена/);
});
