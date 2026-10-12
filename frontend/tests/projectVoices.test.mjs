import assert from 'node:assert/strict'
import test from 'node:test'
import { ref } from 'vue'
import { useProjectVoices } from '../src/useProjectVoices.js'
import { deferred, mountComposable } from './support/composableHarness.mjs'

const id = 'pc_'+'a'.repeat(32), voice = 'qwen3:'+'1'.repeat(32)
const view = (extra={}) => ({ project_id:'a',revision:1,characters:[{character_id:id,name:'小兔',voice_id:null,status:'unbound'}],...extra })
async function mount(t, request) {
  const project=ref({id:'a',revision:1}), accountKey=ref('owner-a'), disabled=ref(false), updates=[], calls=[]
  const mounted=mountComposable(()=>useProjectVoices({project,accountKey,disabled,
    request:(url,options={})=>{calls.push({url,options});return request(url,options)},
    onUpdated:value=>{updates.push(value);project.value={...project.value,revision:value.revision}},
  }))
  t.after(()=>mounted.unmount());await mounted.flush()
  return {...mounted,panel:mounted.instance,project,accountKey,disabled,updates,calls}
}
test('bind one stable character without sending pictures or synthesizing dialogue',async t=>{
  const c=await mount(t,async(url,options)=>options.method==='PUT'?view({revision:2,characters:[{character_id:id,name:'小兔',voice_id:voice,status:'current'}]}):view())
  assert.equal(await c.panel.bind(id,voice),true)
  assert.deepEqual(JSON.parse(c.calls.at(-1).options.body),{revision:1,character_id:id,voice_id:voice})
  assert.equal(c.project.value.revision,2);assert.equal(c.updates.length,1)
  assert.equal(await c.panel.bind('another-id',voice),false)
})
test('late reads cannot overwrite a different project or a second visit',async t=>{
  const a=deferred(),b=deferred(),again=deferred();const pending=[a,b,again]
  const c=await mount(t,()=>pending.shift().promise)
  c.project.value={id:'b',revision:1};c.project.value={id:'a',revision:1}
  again.resolve(view({revision:3}));await c.flush()
  a.resolve(view());b.resolve(view({project_id:'b'}));await c.flush()
  assert.equal(c.panel.snapshot.value.revision,3)
})
test('switching accounts drops pending write results and preserves the new account state',async t=>{
  const write=deferred();const c=await mount(t,(url,options)=>options.method==='PUT'?write.promise:Promise.resolve(view()))
  const waiting=c.panel.bind(id,voice)
  c.accountKey.value='owner-b';await c.flush()
  write.resolve(view({revision:2}));assert.equal(await waiting,false)
  assert.equal(c.updates.length,0);assert.equal(c.panel.snapshot.value.revision,1)
})
test('conflicts do not retry and server operation disable blocks adoption',async t=>{
  const c=await mount(t,async(url,options)=>{if(options.method==='PUT')throw Object.assign(new Error('conflict'),{status:409});return view()})
  c.disabled.value=true;assert.equal(await c.panel.bind(id,voice),false)
  c.disabled.value=false;assert.equal(await c.panel.bind(id,voice),false)
  assert.match(c.panel.error.value,/本次绑定未保存/)
  assert.equal(c.calls.filter(c=>c.options.method==='PUT').length,1)
})

test('background cast appears without waiting for a project revision change',async t=>{
  let roster=[]
  const c=await mount(t,async()=>view({characters:roster}))
  assert.equal(c.panel.characters.value.length,0)
  roster=[{character_id:id,name:'小兔',voice_id:null,status:'unbound'}]
  c.project.value={...c.project.value,project_cast:{characters:[{id,name:'小兔'}]}}
  await c.flush()
  assert.equal(c.panel.characters.value[0].name,'小兔')
  assert.equal(c.project.value.revision,1)
  assert.equal(c.calls.filter(c=>c.options.method==='PUT').length,0)
})

test('narrator adoption supersedes a pending background read instead of silently dropping it',async t=>{
  const read=deferred();let reads=0
  const c=await mount(t,async(url,options)=>{
    if(options.method==='PUT')return view({revision:2,characters:[{character_id:id,name:'旁白',voice_id:voice,status:'current'}]})
    if(++reads>1)return read.promise
    return view({characters:[{character_id:id,name:'旁白',voice_id:null,status:'unbound'}]})
  })
  const pending=c.panel.load()
  assert.equal(c.panel.loading.value,true)
  assert.equal(await c.panel.bind(id,voice),true)
  read.resolve(view());await pending
  assert.equal(c.panel.characters.value[0].voice_id,voice)
  assert.equal(c.panel.busy.value,false)
  assert.match(c.panel.message.value,/旁白音色已保存/)
})

test('disabled adoption always explains that the library voice is not bound',async t=>{
 const c=await mount(t,async()=>view())
 c.disabled.value=true
 assert.equal(await c.panel.bind(id,voice),false)
 assert.match(c.panel.error.value,/尚未绑定本项目/)
 assert.equal(c.calls.filter(c=>c.options.method==='PUT').length,0)
})

test('explicit H3 generated voice is saved as a choice, not an empty fixed binding',async t=>{
 const c=await mount(t,async(url,options)=>options.method==='PUT'?view({revision:2,characters:[{character_id:id,name:'奶奶',voice_id:null,status:'generated'}]}):view())
 assert.equal(await c.panel.bind(id,null,'generated'),true)
 assert.deepEqual(JSON.parse(c.calls.at(-1).options.body),{revision:1,character_id:id,voice_id:null,voice_mode:'generated'})
 assert.equal(c.panel.characters.value[0].status,'generated')
 assert.match(c.panel.message.value,/H3 自行生成声音/)
})
