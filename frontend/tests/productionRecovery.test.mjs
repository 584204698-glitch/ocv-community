import test from 'node:test'
import assert from 'node:assert/strict'
import {productionRecovery} from '../src/productionRecovery.js'

const project={status:'video_review'}
const failure={id:'shot1',native_dialogue:{},video_status:'failed',video_terminal:true}
test('cached preparation failure offers explicit local reanalysis, not whole-project rerun',()=>{
 const r=productionRecovery(project,{...failure,video_error:'H3 提示词转换或本地请求准备失败：开场对接缺少已批准角色的站位'})
 assert.deepEqual(r.actions.map(a=>a.id),['prepare','edit'])
 assert.match(r.help,/其他镜头/)
})
test('hard pixel conflict leads to image/action editing instead of paid blind retries',()=>{
 const r=productionRecovery(project,{...failure,video_error:'开场图与已批准动作不一致，尚未提交H3：爸爸缺失'})
 assert.deepEqual(r.actions.map(a=>a.id),['edit','prepare'])
 assert.match(r.actions[1].label,/图片正确/)
})
test('unknown task overrides errors and never offers a new paid preparation',()=>{
 for(const resumable of [true,false]){
  const r=productionRecovery(project,{...failure,video_status:'unknown',video_resume_available:resumable,video_error:'JSON 错误'})
  assert.ok(!r.actions.some(a=>['prepare','edit'].includes(a.id)))
  assert.equal(r.actions[0].id,resumable?'query':'settings')
 }
})
test('voice and service errors have distinct destinations',()=>{
 assert.equal(productionRecovery(project,{...failure,video_error:'本镜说话角色尚未绑定已采用的 Qwen 音色'}).actions[0].id,'voices')
 assert.equal(productionRecovery(project,{...failure,video_error:'ReadTimeout'}).actions[0].id,'settings')
})
test('planning and export recovery retain their own stage',()=>{
 assert.equal(productionRecovery({status:'draft',error:'原文覆盖不完整'},null).actions[0].id,'plan')
 assert.equal(productionRecovery({status:'export_failed',error:'磁盘空间不足'},null).actions[0].id,'export')
 assert.equal(productionRecovery(project,{}),null)
})
