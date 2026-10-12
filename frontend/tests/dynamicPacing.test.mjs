import test from 'node:test'
import assert from 'node:assert/strict'
import {selectDynamicPacing, restoreDynamicPacing, dynamicPacingSummary, normalizeDynamicCustom} from '../src/dynamicPacing.js'

test('compact selection updates submitted fields without touching story or audio',()=>{
 const form={script:'故事',tts_voice_id:'upload:voice',visual_pacing_preset:'standard'}
 selectDynamicPacing(form,'fast')
 assert.deepEqual(form,{script:'故事',tts_voice_id:'upload:voice',visual_pacing_preset:'fast',visual_min_duration:4,visual_target_duration:6,visual_max_duration:8,visual_max_slides:6})
 assert.equal(dynamicPacingSummary(form),'紧凑：目标 6 秒，最长 8 秒')
})
test('custom bounds remain valid for existing generation API',()=>{
 const form={visual_min_duration:0,visual_target_duration:0,visual_max_duration:0,visual_max_slides:100}
 normalizeDynamicCustom(form)
 assert.deepEqual(form,{visual_min_duration:4,visual_target_duration:5,visual_max_duration:6,visual_max_slides:12})
 form.visual_min_duration=15;form.visual_target_duration=NaN;form.visual_max_duration=NaN
 normalizeDynamicCustom(form)
 assert.equal(form.visual_target_duration,15);assert.equal(form.visual_max_duration,15)
})
test('auto and unknown choices do not rewrite existing settings',()=>{
 const form={visual_pacing_preset:'custom',visual_target_duration:9}
 selectDynamicPacing(form,'auto');assert.equal(form.visual_target_duration,9)
 selectDynamicPacing(form,'bad');assert.equal(form.visual_pacing_preset,'auto')
 assert.equal(dynamicPacingSummary(form),'自动：按故事内容切镜')
})

test('auto can be undone after reload and another preset remains selectable',()=>{
 const form={visual_pacing_preset:'custom',visual_min_duration:5,visual_target_duration:9,visual_max_duration:11,visual_max_slides:3}
 selectDynamicPacing(form,'auto')
 const restored=JSON.parse(JSON.stringify(form))
 selectDynamicPacing(restored,'auto');restoreDynamicPacing(restored)
 assert.equal(restored.visual_pacing_preset,'custom')
 assert.equal(restored.visual_target_duration,9)
 assert.equal(restored.visual_max_slides,3)
 selectDynamicPacing(restored,'auto');selectDynamicPacing(restored,'fast')
 assert.equal(restored.visual_pacing_preset,'fast')
 assert.equal(restored.visual_target_duration,6)
})
