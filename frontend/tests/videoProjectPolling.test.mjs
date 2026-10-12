import test from 'node:test'
import assert from 'node:assert/strict'
import {createVideoProjectPoller} from '../src/videoProjectPolling.js'
test('idle failed projects receive external completions, with no overlapping reads',async()=>{
 let release,calls=0,updated
 const tick=createVideoProjectPoller({canRead:()=>true,readIdentity:()=>({id:'a'}),load:()=>{calls++;return new Promise(r=>release=r)},isCurrent:()=>true,apply:r=>updated=r,onError:()=>{}})
 const pending=tick();await tick();assert.equal(calls,1)
 release({status:'video_review',shots:[{video_status:'completed'}]});await pending
 assert.equal(updated.shots[0].video_status,'completed')
})
test('a refresh cannot overwrite edits started during the read or a changed project',async()=>{
 for(const reason of ['editing','navigation']){
  let release,canRead=true,current=true,applied=false
  const tick=createVideoProjectPoller({canRead:()=>canRead,readIdentity:()=>({id:'a'}),load:()=>new Promise(r=>release=r),isCurrent:()=>current,apply:()=>applied=true,onError:()=>{}})
  const pending=tick();if(reason==='editing')canRead=false;else current=false
  release({});await pending;assert.equal(applied,false)
 }
})
test('failed reads release the lock and only current errors are reported',async()=>{
 let calls=0,errors=0,current=true
 const tick=createVideoProjectPoller({canRead:()=>true,readIdentity:()=>({}),load:async()=>{calls++;throw Error('offline')},isCurrent:()=>current,apply:()=>{},onError:()=>errors++})
 await tick();current=false;await tick();assert.equal(calls,2);assert.equal(errors,1)
})
