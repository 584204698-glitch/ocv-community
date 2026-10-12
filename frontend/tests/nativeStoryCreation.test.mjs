import test from 'node:test'
import assert from 'node:assert/strict'
import { nativeStoryPayload, isNativeStory } from '../src/nativeStoryCreation.js'
import { snapshotDraftForm, restoreDraftForm, clearProjectContent } from '../src/studioDrafts.js'

const form = () => ({dynamic_video:true,speech_mode:'dialogue',project_name:'雨伞',script:'小兔、小熊和小猫在门前商量如何共用一把伞。',visual_style_prompt:'水彩绘本',
  video_generation_backend:'comfyui',comfyui_profile_id:'ref2va',dynamic_auto_advance:true,tts_voice_id:'old-narrator',api_key:'private-value'})

test('new defaults and explicitly cleared render overrides retain distinct request identities',()=>{
  const draft=form()
  const initial=nativeStoryPayload(draft,()=> 'a'.repeat(32))
  assert.equal(initial.comfyui_steps,8)
  draft.comfyui_steps=null
  const cleared=nativeStoryPayload(draft,()=> 'b'.repeat(32))
  assert.equal(cleared.comfyui_steps,null)
  assert.notEqual(cleared.request_id,initial.request_id)
  assert.equal(nativeStoryPayload(draft,()=> 'c'.repeat(32)).request_id,cleared.request_id)
})

test('native request includes exact story and settings, without legacy TTS or credentials',()=>{
  const draft=form(),payload=nativeStoryPayload(draft,()=> 'a'.repeat(32))
  assert.equal(payload.story,draft.script);assert.equal(payload.speech_mode,'dialogue')
  assert.equal(payload.one_click,true);assert.equal(payload.minimum_shots,null)
  assert.equal(payload.duration_seconds,null)
  assert.equal(payload.request_id,'a'.repeat(32))
  assert.equal(Object.hasOwn(payload,'tts_voice_id'),false)
  assert.equal(Object.hasOwn(payload,'api_key'),false)
})
test('automatic planning clears fixed counts and carries the selected pacing',()=>{
  const draft=form()
  Object.assign(draft,{native_duration_seconds:90,native_minimum_shots:12,visual_pacing_preset:'fast'})
  const manual=nativeStoryPayload(draft,()=> 'a'.repeat(32))
  assert.equal(manual.duration_seconds,90)
  assert.equal(manual.minimum_shots,12)
  assert.equal(manual.visual_pacing_preset,'fast')
  Object.assign(draft,{native_duration_seconds:'',native_minimum_shots:null,visual_pacing_preset:'auto'})
  const automatic=nativeStoryPayload(draft,()=> 'b'.repeat(32))
  assert.equal(automatic.duration_seconds,null)
  assert.equal(automatic.minimum_shots,null)
  assert.equal(automatic.visual_pacing_preset,'auto')
  assert.notEqual(automatic.request_id,manual.request_id)
})

test('reload and retry retain request identity; actual input edit creates a new identity',()=>{
  const draft=form();nativeStoryPayload(draft,()=> 'a'.repeat(32))
  const reopened={};restoreDraftForm(reopened,form(),snapshotDraftForm(draft))
  assert.equal(nativeStoryPayload(reopened,()=> 'b'.repeat(32)).request_id,'a'.repeat(32))
  reopened.script+='他们决定轮流撑伞。'
  assert.equal(nativeStoryPayload(reopened,()=> 'b'.repeat(32)).request_id,'b'.repeat(32))
  reopened.speech_mode='mixed'
  assert.equal(nativeStoryPayload(reopened,()=> 'c'.repeat(32)).request_id,'c'.repeat(32))
})
test('scene reference choice reaches native creation and changes request identity when toggled',()=>{
  const draft=form()
  const enabled=nativeStoryPayload(draft,()=> 'a'.repeat(32))
  assert.equal(enabled.scene_references_enabled,true)
  draft.scene_references_enabled=false
  const disabled=nativeStoryPayload(draft,()=> 'b'.repeat(32))
  assert.equal(disabled.scene_references_enabled,false)
  assert.notEqual(disabled.request_id,enabled.request_id)
  assert.equal(nativeStoryPayload(draft,()=> 'c'.repeat(32)).request_id,disabled.request_id)
})
test('new story does not carry adopted source, references or old request identity into submission',()=>{
  const draft={...form(),story_source:{story_id:'private-old'}}
  nativeStoryPayload(draft,()=> 'a'.repeat(32));clearProjectContent(draft)
  draft.script='小松鼠和小熊重新开始一个完全不同的故事。'
  const payload=nativeStoryPayload(draft,()=> 'b'.repeat(32))
  assert.equal(payload.story_source,null);assert.equal(payload.request_id,'b'.repeat(32))
})
test('narration is unchanged and native route cannot silently ignore an unsupported reference choice',()=>{
  assert.equal(isNativeStory({...form(),speech_mode:'narration'}),false)
  assert.throws(()=>nativeStoryPayload({...form(),video_generation_backend:'api'}),/H3/)
  assert.throws(()=>nativeStoryPayload({...form(),reference_image_ids:['old-character']}),/参考图/)
})


test('dialogue route defaults to external TTS and switching route renews request identity',()=>{
  const draft=form()
  const external=nativeStoryPayload(draft,()=> 'a'.repeat(32))
  assert.equal(external.dialogue_route,'external_tts')
  draft.dialogue_route='h3_native'
  const native=nativeStoryPayload(draft,()=> 'b'.repeat(32))
  assert.equal(native.dialogue_route,'h3_native')
  assert.notEqual(native.request_id,external.request_id)
  assert.equal(nativeStoryPayload(draft,()=> 'c'.repeat(32)).request_id,native.request_id)
  draft.speech_mode='mixed';draft.dialogue_route='external_tts'
  assert.equal(nativeStoryPayload(draft,()=> 'd'.repeat(32)).dialogue_route,'external_tts')
})

test('TTS engine changes request identity without changing story or adopted source',()=>{
  const draft=form()
  const index=nativeStoryPayload(draft,()=> 'a'.repeat(32))
  assert.equal(index.dialogue_tts_engine,'indextts25')
  draft.dialogue_tts_engine='qwen3'
  const qwen=nativeStoryPayload(draft,()=> 'b'.repeat(32))
  assert.equal(qwen.dialogue_tts_engine,'qwen3')
  assert.equal(qwen.story,index.story)
  assert.equal(qwen.dialogue_route,'external_tts')
  assert.notEqual(qwen.request_id,index.request_id)
})
