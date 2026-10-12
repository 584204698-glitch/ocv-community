import test from 'node:test'
import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'

const source=await readFile(new URL('../src/components/VideoStudio.vue',import.meta.url),'utf8')
const handler=source.slice(source.indexOf('async function handleRecovery(action){'),source.indexOf('const historyPreviewIndex=ref'))
function fixture(fail=false){
 const calls=[],hydrations=[];let current={id:'project-A',revision:4}
 const deps={requireAdoptedVideoInputs:()=>true,selectedShot:{value:{id:'shot-A'}},
  confirmAction:async()=>true,autoAdvanceEnabled:{value:true},view:{value:'storyboard'},videoLogsOpen:{value:false},
  project:{get value(){return current},set value(v){hydrations.push(v);current=v}},
  run:async f=>f(),call:async(path,method,body)=>{
   calls.push({path,method,body});assert.equal(hydrations.length,0,'child editors must not reload between requests')
   if(path.endsWith('retry-preparation'))return {id:'project-A',revision:5}
   if(fail)throw Error('service unavailable')
   return {id:'project-A',revision:6,status:'video_generating'}
  }}
 const run=new Function(...Object.keys(deps),handler+';return handleRecovery')(...Object.values(deps))
 return {run,calls,hydrations,deps}
}
test('one confirmed recovery proceeds to only the selected shot using fresh revision before editor hydration',async()=>{
 const f=fixture();await f.run('prepare')
 assert.deepEqual(f.calls.map(x=>x.path),['/project-A/shots/shot-A/retry-preparation','/project-A/videos/generate'])
 assert.deepEqual(f.calls[1].body,{revision:5,shot_ids:['shot-A'],retry_failed:true})
 assert.equal(f.hydrations.length,1)
 assert.equal(f.deps.autoAdvanceEnabled.value,false)
})
test('submission failure retains prepared revision for a user retry without a hidden retry loop',async()=>{
 const f=fixture(true);await assert.rejects(f.run('prepare'),/service unavailable/)
 assert.equal(f.calls.length,2)
 assert.equal(f.deps.project.value.revision,5)
})
