import assert from 'node:assert/strict'
import test from 'node:test'
import {nativePromptReview} from '../src/nativePromptReview.js'
const plan = () => ({duration:6,speakers:[
  {character_id:'a',name:'小鸭',kind:'character',on_screen:true,voice_id:'saved-a'},
  {character_id:'b',name:'小熊',kind:'character',on_screen:true,presence:'later'},
  {character_id:'n',name:'旁白',kind:'narrator',on_screen:false,voice_id:'saved-n'},
],turns:[{character_id:'a',text:'我够到了！',start:.5,end:2},{character_id:'n',text:'它笑了。',start:3,end:5}]})
test('submitted Chinese review uses frozen words and action even after local edits',()=>{
  const submitted=plan(),edited=plan();edited.turns[0].text='后来改的台词'
  const r=nativePromptReview({action:'后来改的动作',native_dialogue:edited,
    video_request:{source_prompt:'说完沿墙跑开。',dialogue:{plan:submitted}}})
  assert.equal(r.frozen,true);assert.equal(r.action,'说完沿墙跑开。')
  assert.equal(r.turns[0].text,'我够到了！');assert.deepEqual(r.turns[0].listeners,['小熊（入画后）'])
  assert.equal(r.turns[1].source,'画外旁白');assert.equal(r.afterSpeech,5)
})
test('historical missing Chinese action is not replaced by a current draft',()=>{
  const r=nativePromptReview({action:'新动作',video_request:{source_prompt:'English staging',dialogue:{plan:plan()}}})
  assert.equal(r.action,'');assert.equal(nativePromptReview({video_request:{},native_dialogue:plan()}),null)
})
test('new unsaved project and action-only shot have their own current preview',()=>{
  const draft=plan();draft.turns=[]
  const r=nativePromptReview({action:'一起把门推开。',native_dialogue:draft,native_staging_stale:true})
  assert.equal(r.frozen,false);assert.equal(r.silent,true);assert.equal(r.stale,true)
  assert.equal(r.action,'一起把门推开。');assert.equal(r.turns.length,0)
})
test('Chinese action timeline is read from the same frozen request as the words',()=>{
  const frozen=plan();frozen.performance_beats=[{start:0,end:.5,action:'The duck faces the bear.',action_zh:'小鸭面向小熊。'}]
  const edited=plan();edited.performance_beats=[{start:0,end:1,action_zh:'后来修改的动作。'}]
  const r=nativePromptReview({native_dialogue:edited,video_request:{dialogue:{plan:frozen}}})
  assert.deepEqual(r.beats,[{start:0,end:.5,action:'小鸭面向小熊。'}])
})
test('opening observations belong to the submitted image, never a later edit',()=>{
  const shot={native_visual_grounding:{observations:'后来修改成站在门外。'},
    video_request:{dialogue:{plan:plan()},native_visual_grounding:{observations:'开场只有手从门后露出。'}}}
  assert.equal(nativePromptReview(shot).openingObservation,'开场只有手从门后露出。')
  delete shot.video_request.native_visual_grounding
  assert.equal(nativePromptReview(shot).openingObservation,'')
})


test('unrechecked retained instructions never claim a new image observation',()=>{
  const shot={native_dialogue:plan(),native_visual_grounding:{status:'not_rechecked',observations:'????'}}
  assert.equal(nativePromptReview(shot).openingObservation,'')
  shot.video_request={dialogue:{plan:plan()},native_visual_grounding:{status:'not_rechecked',observations:'?????'}}
  assert.equal(nativePromptReview(shot).openingObservation,'')
})

test('post-dub review includes frozen narration and measured dialogue positions',()=>{
  const original=plan(),visual=plan()
  visual.speakers=visual.speakers.filter(s=>s.kind!=='narrator')
  visual.turns=[{character_id:'a',start:1,end:2.3,mouth_visible:false,text:'视觉稿'}]
  const shot={native_dialogue:{speakers:[{character_id:'n',name:'后来的旁白'}]},video_request:{
    dialogue:{plan:visual,basis:{plan:original}},external_dialogue:{clips:[
      {character_id:'n',kind:'narrator',text:'旁白原文',start:.2,end:.9,voice_id:'n',voice_sha256:'sha-n'},
      {character_id:'a',kind:'character',text:'角色原文',start:1,end:2.15,voice_id:'a',voice_sha256:'sha-a'},
    ]}}}
  const r=nativePromptReview(shot)
  assert.equal(r.externalTts,true);assert.equal(r.silent,false)
  assert.equal(r.turns[0].name,'旁白');assert.equal(r.turns[0].source,'画外旁白')
  assert.equal(r.turns[0].text,'旁白原文');assert.equal(r.turns[1].end,2.15)
  assert.equal(r.turns[1].mouthHidden,true);assert.equal(r.turns[1].voiceBound,true)
  assert.equal(r.afterSpeech,2.15)
})
