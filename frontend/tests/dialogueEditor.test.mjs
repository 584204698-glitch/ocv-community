import assert from 'node:assert/strict'
import test from 'node:test'
import {ref} from 'vue'
import {useDialogueEditor} from '../src/useDialogueEditor.js'
import {deferred,mountComposable} from './support/composableHarness.mjs'
const dialogue=()=>({id:'shot-1',native_dialogue:{turns:[{character_id:'pc_'+'1'.repeat(32),text:'一起走吧。',start:0.5,end:3.5,delivery:'naturally'}]}})
async function mount(t,request){
  const project=ref({id:'a',revision:3}),shot=ref(dialogue()),account=ref('1'),disabled=ref(false),updates=[]
  const mounted=mountComposable(()=>useDialogueEditor({project,shot,account,disabled,request,onUpdated:r=>updates.push(r)}))
  t.after(()=>mounted.unmount());await mounted.flush()
  return {...mounted,editor:mounted.instance,project,shot,account,disabled,updates}
}
test('local edit submits only exact turns and current project revision',async t=>{
  const calls=[];const c=await mount(t,async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {id:'a',revision:4}})
  c.editor.turns.value[0].text='雨停了吗？'
  assert.equal(await c.editor.save(),true)
  assert.equal(calls[0].url,'/api/video-studio/a/shots/shot-1/dialogue')
  assert.deepEqual(Object.keys(calls[0].body),['revision','turns'])
  assert.equal(calls[0].body.turns[0].text,'雨停了吗？');assert.equal(c.editor.dirty.value,false)
})
test('polling same saved dialogue preserves unsaved edits; conflicting remote edit is visible',async t=>{
  const c=await mount(t,async()=>{throw new Error('must not save')})
  c.editor.turns.value[0].text='等等我。';c.shot.value=dialogue();await c.flush()
  assert.equal(c.editor.turns.value[0].text,'等等我。');assert.equal(c.editor.conflict.value,false)
  c.shot.value={...dialogue(),native_dialogue:{turns:[{...dialogue().native_dialogue.turns[0],text:'我来撑伞。'}]}};await c.flush()
  assert.equal(c.editor.conflict.value,true);assert.equal(await c.editor.save(),false)
  c.editor.reset();assert.equal(c.editor.turns.value[0].text,'我来撑伞。');assert.equal(c.editor.dirty.value,false)
})
test('late write cannot change a new project or a different account',async t=>{
  const pending=deferred(),c=await mount(t,()=>pending.promise)
  c.editor.turns.value[0].text='等等我。';const work=c.editor.save()
  c.account.value='2';c.project.value={id:'b',revision:1};c.shot.value=dialogue();await c.flush()
  pending.resolve({id:'a',revision:4})
  assert.equal(await work,false);assert.equal(c.updates.length,0);assert.equal(c.editor.busy.value,false)
})
