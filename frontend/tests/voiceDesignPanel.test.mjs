import assert from 'node:assert/strict'
import test from 'node:test'
import {readFile} from 'node:fs/promises'
import {compileScript,parse} from '@vue/compiler-sfc'
import {installBrowserGlobals,mountComposable} from './support/composableHarness.mjs'
const url=new URL('../src/components/VoiceDesignPanel.vue',import.meta.url)
const code=compileScript(parse(await readFile(url,'utf8')).descriptor,{id:'voice-agent-test'}).content.replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,names,specifier)=>`import ${names} from '${specifier==='vue'?import.meta.resolve('vue'):new URL(specifier+'.js',url).href}'`)
const {default:component}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const props={projectId:'a'.repeat(32),characterId:'pc_'+'b'.repeat(32),characterName:'小熊',voiceId:'',boundVoiceIds:[],nativeH3:true}

test('automatic design requests character-grounded text then one audition without adopting',async t=>{
 const calls=[],events=[]
 const browser=installBrowserGlobals({fetch:async(url,options={})=>{
  calls.push([url,options.body?JSON.parse(options.body):null])
  if(url.endsWith('/suggest'))return Response.json({name:'温厚小熊',description:'温暖清晰的普通话幼年声音，厚实自然。',reference_text:'今天阳光很好，出去走走吧。',reason:'适合认真害羞的小熊'})
  if(options.method==='POST')return Response.json({id:'c'.repeat(32),status:'generating',name:'温厚小熊',source:{project_id:props.projectId}})
  return Response.json({health:{ready:true},voices:[]})
 }})
 const mounted=mountComposable(()=>component.setup(props,{expose(){},emit:(...args)=>events.push(args)}))
 t.after(()=>{mounted.unmount();browser.restore()})
 await mounted.flush();await mounted.instance.create(true)
 assert.equal(calls[1][0],'/api/voice-design/suggest')
 assert.equal(calls[1][1].character_id,props.characterId)
 assert.equal(calls[2][1].description,'温暖清晰的普通话幼年声音，厚实自然。')
 assert.equal(calls[2][1].project_id,props.projectId)
 assert.equal(calls.length,3)
 assert.ok(!events.some(e=>e[0]==='select'))
 assert.equal(mounted.instance.busy.value,false)
})

test('leaving while voice direction is pending does not start Qwen in another project',async t=>{
 let resolve,calls=[]
 const browser=installBrowserGlobals({fetch:async(url)=>{
  calls.push(url)
  if(url.endsWith('/suggest'))return new Promise(r=>{resolve=r})
  return Response.json({health:{ready:true},voices:[]})
 }})
 const mounted=mountComposable(()=>component.setup(props,{expose(){},emit(){}}))
 t.after(()=>browser.restore())
 await mounted.flush();const pending=mounted.instance.create(true)
 mounted.unmount();resolve(Response.json({name:'小熊',description:'温暖清晰',reference_text:'今天阳光很好，出去走走吧。'}));await pending
 assert.equal(calls.length,2)
})

test('adoption stays busy until project binding finishes and exposes unsuccessful binding',async t=>{
 let finish
 const saved={id:'c'.repeat(32),voice_id:'qwen3:'+'c'.repeat(32),status:'saved'}
 const browser=installBrowserGlobals({fetch:async(url)=>Response.json(url.endsWith('/adopt')?saved:{health:{ready:true},voices:[saved]})})
 const events=[]
 const mounted=mountComposable(()=>component.setup({...props,characterName:'旁白',adoptVoice:()=>new Promise(r=>{finish=r})},{expose(){},emit:(...e)=>events.push(e)}))
 t.after(()=>{mounted.unmount();browser.restore()})
 await mounted.flush()
 const adopting=mounted.instance.select(saved);await mounted.flush()
 assert.equal(mounted.instance.busy.value,true)
 finish(false);await adopting
 assert.equal(mounted.instance.busy.value,false)
 assert.match(mounted.instance.error.value,/未绑定当前说话者/)
 assert.ok(!events.some(e=>e[0]==='select'))
})
