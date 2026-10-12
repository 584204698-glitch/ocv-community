import test from 'node:test'
import assert from 'node:assert/strict'
import {voiceLibraryPage} from '../src/voiceLibrary.js'

test('100 voice assets render bounded pages with complete coverage and clamped last page',()=>{
 const voices=Array.from({length:100},(_,i)=>({id:String(i),name:'音色'+i}))
 const ids=[]
 for(let page=1;page<=13;page++){
  const result=voiceLibraryPage(voices,{page})
  assert.ok(result.rows.length<=8);ids.push(...result.rows.map(v=>v.id))
 }
 assert.equal(new Set(ids).size,100)
 assert.equal(voiceLibraryPage(voices,{page:99}).page,13)
})
test('project filter includes explicitly bound legacy voices but excludes other stories',()=>{
 const voices=[{id:'1',voice_id:'qwen3:1'},{id:'2',source:{project_id:'a',character_name:'小熊'}},{id:'3',source:{project_id:'b'}}]
 assert.deepEqual(voiceLibraryPage(voices,{scope:'project',projectId:'a',boundIds:['qwen3:1']}).rows.map(v=>v.id),['1','2'])
 assert.deepEqual(voiceLibraryPage(voices,{query:'小熊'}).rows.map(v=>v.id),['2'])
})
test('archive hides without losing favorites and filtering resets out-of-range pages',()=>{
 const voices=[{id:'1',favorite:true,archived:true},{id:'2',favorite:true}]
 assert.deepEqual(voiceLibraryPage(voices,{scope:'favorites'}).rows.map(v=>v.id),['2'])
 assert.deepEqual(voiceLibraryPage(voices,{scope:'archived',page:9}).rows.map(v=>v.id),['1'])
 assert.equal(voiceLibraryPage(voices,{query:'不存在',page:9}).page,1)
})
