import test from 'node:test'
import assert from 'node:assert/strict'
import {audioSettingsDraft} from '../src/projectLifecycle.js'
const record={id:'a'.repeat(32),revision:7,settings:{name:'已有项目',style:'绘本',ratio:'16:9'},creation_parameters:{script:'原文',tts_engine:'indextts25',tts_voice_id:'upload:fixed'}}
test('audio return edits the existing project and synchronizes latest audio instead of creating a job',async()=>{
 const calls=[]
 const draft=await audioSettingsDraft({id:'job'},async(url,options)=>{calls.push([url,options]);return url.includes('by-audio-task')?{project:record}:{...record,revision:8}})
 assert.deepEqual(calls.map(c=>c[0]),['/api/video-studio/by-audio-task/job','/api/video-studio/'+record.id+'/sync-audio'])
 assert.equal(draft.source_project_id,record.id);assert.equal(draft.source_project_revision,8)
 assert.equal(draft.form.tts_voice_id,'upload:fixed');assert.equal(draft.rerun,true)
 assert.equal(draft.form.script,'原文')
})
test('audio without storyboard imports once and does not submit media or planning',async()=>{
 const calls=[]
 await audioSettingsDraft({id:'job'},async(url,options)=>{calls.push([url,options]);return url.includes('by-audio-task')?{project:null}:record})
 assert.equal(calls.length,2);assert.equal(calls[1][0],'/api/video-studio/from-project')
 assert.deepEqual(JSON.parse(calls[1][1].body),{job_id:'job',from_audio_task:true})
})
test('failed or aborted return cannot apply a stale draft',async()=>{
 await assert.rejects(audioSettingsDraft({id:'job'},async()=>{throw Error('read failed')}),/read failed/)
 const controller=new AbortController()
 await assert.rejects(audioSettingsDraft({id:'job'},async()=>{controller.abort();return {project:record}},controller.signal),{name:'AbortError'})
})
