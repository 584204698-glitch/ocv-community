export const activeProjectStatuses = ['planning','stopping','image_generating','image_stopping','video_generating','video_stopping','exporting','running','queued']

export function dynamicConfigDraft(record) {
 const saved=record.creation_parameters||{},settings=record.settings||{}
 const form={...saved,dynamic_video:true,step_mode:true,
  project_name:settings.name||saved.project_name||'动态视频',
  visual_style_prompt:settings.style??saved.visual_style_prompt??'',
  global_character_prompt:settings.characters??saved.global_character_prompt??'',
  story_environment_prompt:settings.world??saved.story_environment_prompt??'',
  dynamic_text_mode:settings.dynamic_text_mode||saved.dynamic_text_mode||'visual_first',
  scene_references_enabled:saved.scene_references_enabled!==false,
  video_orientation:settings.ratio==='9:16'?'portrait':saved.video_orientation||'landscape'}
 return {id:'rerun-'+(globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`),name:form.project_name,kind:'dynamic',form,
  subtitle:{project_name:form.project_name,source_audio_id:form.source_audio_id||'',reference_text:form.script||'',use_correction:true},
  engine:form.tts_engine==='indextts2'?'indextts25':form.tts_engine||'indextts25',rerun:true,
  source_project_id:record.id,source_project_revision:record.revision,
  rerun_base:JSON.parse(JSON.stringify(form)),source_view:'storyboard',
  rerun_stages:[{key:'script',label:'设置',state:'current'},{key:'audio',label:'配音与字幕',state:'done'},{key:'storyboard',label:'动态分镜',state:'upcoming'}]}
}

export async function audioSettingsDraft(job, request, signal) {
 const opts={signal}
 const lookup=await request('/api/video-studio/by-audio-task/'+encodeURIComponent(job.id),opts)
 let record=lookup.project
 const post=data=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal})
 if (!record) record=await request('/api/video-studio/from-project',post({job_id:job.id,from_audio_task:true}))
 else record=await request('/api/video-studio/'+record.id+'/sync-audio',post({revision:record.revision}))
 if(signal?.aborted)throw new DOMException('页面已切换','AbortError')
 return dynamicConfigDraft(record)
}
