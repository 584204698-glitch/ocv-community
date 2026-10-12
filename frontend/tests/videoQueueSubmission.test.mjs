import test from 'node:test'
import assert from 'node:assert/strict'
import {submitLocalVideoQueue} from '../src/videoQueueSubmission.js'
const record=()=>({id:'p',revision:3,status:'video_generating',logs:[],settings:{style:'绘本'},shots:[{id:'s1',action:'取纸',image_version:'a'},{id:'s2',video_status:'running'}]})
const conflict=()=>Object.assign(new Error('项目刚刚发生变化，请刷新后重试'),{status:409})
test('sibling progress permits one guarded retry with a fresh revision',async()=>{
 const old=record(),latest=record();latest.revision=4;latest.logs.push('progress');latest.shots[1].video_status='completed'
 const calls=[];let refreshed
 const result=await submitLocalVideoQueue({record:old,shotIds:['s1'],call:async(...args)=>{
  calls.push(args);if(calls.length===1)throw conflict();if(calls.length===2)return latest;return {ok:true}
 },onRefresh:value=>refreshed=value})
 assert.deepEqual(result,{ok:true});assert.equal(calls[2][2].revision,4);assert.equal(refreshed,latest)
})
test('adopted image, action, project or settings changes require user review',async()=>{
 for(const mutate of [v=>v.shots[0].image_version='b',v=>v.shots[0].action='放纸',v=>v.settings.style='漫画',v=>v.id='other']){
  const latest=record();mutate(latest);let count=0
  await assert.rejects(submitLocalVideoQueue({record:record(),shotIds:['s1'],call:async()=>{if(++count===1)throw conflict();return latest},onRefresh:()=>{}}))
  assert.equal(count,2)
 }
})
test('a second revision conflict is not retried again',async()=>{
 let count=0;const latest=record();latest.revision=4
 await assert.rejects(submitLocalVideoQueue({record:record(),shotIds:['s1'],call:async()=>{if(++count===2)return latest;throw conflict()},onRefresh:()=>{}}))
 assert.equal(count,3)
})
test('guard refusals and uncertain network failures never trigger a new request',async()=>{
 for(const error of [Object.assign(new Error('原任务仍在运行'),{status:409}),new Error('timeout')]){
  let count=0
  await assert.rejects(submitLocalVideoQueue({record:record(),shotIds:['s1'],call:async()=>{count++;throw error},onRefresh:()=>{}}))
  assert.equal(count,1)
 }
})
