import test from 'node:test'
import assert from 'node:assert/strict'
import {applyNewVideoDefaults} from '../src/videoCreationDefaults.js'
const defaults={video_generation_backend:'comfyui',comfyui_profile_id:'configured-fast',comfyui_h3_prompt_agent:true,image_profile_id:'configured-images'}
test('a fresh draft receives account defaults without copying unrelated project inputs',async()=>{
 const form={script:'my new story',reference_image_ids:[],video_generation_backend:'api',comfyui_profile_id:''}
 await applyNewVideoDefaults(form,{isCurrent:()=>true,request:async()=>({defaults})})
 assert.equal(form.comfyui_profile_id,'configured-fast');assert.equal(form.video_generation_backend,'comfyui')
 assert.equal(form.script,'my new story');assert.deepEqual(form.reference_image_ids,[]);assert.equal(form._video_defaults_loading,false)
})
test('late defaults do not replace a manual choice or a changed project/account',async()=>{
 for(const change of ['manual','account','profile']){
  const form={video_generation_backend:'api',comfyui_profile_id:''};let current=true,release
  const result=applyNewVideoDefaults(form,{isCurrent:()=>current,request:()=>new Promise(resolve=>{release=resolve})})
  if(change==='manual')form._video_service_edited=true
  if(change==='account')current=false
  if(change==='profile')form.comfyui_profile_id='my-choice'
  release({defaults});await result
  assert.equal(form.video_generation_backend,'api')
  assert.equal(form.comfyui_profile_id,change==='profile'?'my-choice':'')
 }
})
test('accounts without defaults retain official behavior and read errors unlock the form',async()=>{
 const form={video_generation_backend:'api'};let error=''
 await applyNewVideoDefaults(form,{isCurrent:()=>true,request:async()=>({defaults:null})})
 assert.equal(form.video_generation_backend,'api')
 await applyNewVideoDefaults(form,{isCurrent:()=>true,request:async()=>{throw Error('offline')},onError:message=>{error=message}})
 assert.equal(error,'offline');assert.equal(form._video_defaults_loading,false)
})

test('an image profile loading first does not discard the saved video workflow',async()=>{
 const form={video_generation_backend:'api',comfyui_profile_id:'',image_profile_id:'',image_resolution:'2k'}
 let release,saved=0
 const pending=applyNewVideoDefaults(form,{isCurrent:()=>true,request:()=>new Promise(resolve=>{release=resolve}),onSaved:()=>saved++})
 // ImageProfileSelector mounts and selects a configured model before the
 // account creation defaults arrive. This ordering occurs in the real UI.
 form.image_profile_id='loaded-images';form.image_resolution='1k'
 release({defaults});await pending
 assert.equal(form.video_generation_backend,'comfyui');assert.equal(form.comfyui_profile_id,'configured-fast')
 assert.equal(form.comfyui_h3_prompt_agent,true)
 assert.equal(form.image_profile_id,'loaded-images');assert.equal(form.image_resolution,'1k')
 assert.equal(saved,1);assert.equal(form._video_defaults_loading,false)
})

test('changing a video option preserves the whole video choice but still applies image defaults',async()=>{
 const form={video_generation_backend:'comfyui',comfyui_profile_id:'chosen',comfyui_h3_prompt_agent:false,image_profile_id:''}
 let release
 const pending=applyNewVideoDefaults(form,{isCurrent:()=>true,request:()=>new Promise(resolve=>{release=resolve})})
 form.comfyui_h3_prompt_agent=true
 release({defaults});await pending
 assert.equal(form.comfyui_profile_id,'chosen');assert.equal(form.comfyui_h3_prompt_agent,true)
 assert.equal(form.image_profile_id,'configured-images')
})
