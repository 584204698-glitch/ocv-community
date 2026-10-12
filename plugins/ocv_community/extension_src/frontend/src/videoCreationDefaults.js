import {requestJSON} from './api'

export const videoDefaultFields=['video_generation_backend','comfyui_profile_id','comfyui_steps','comfyui_width','comfyui_height','comfyui_h3_prompt_agent','comfyui_reference_audio','image_profile_id','image_resolution']
export function snapshotVideoDefaults(form){return Object.fromEntries(videoDefaultFields.map(key=>[key,form[key]]))}
const videoFields=videoDefaultFields.filter(key=>!key.startsWith('image_'))
const imageFields=videoDefaultFields.filter(key=>key.startsWith('image_'))

// Apply only to a genuinely new draft, never to saved projects or to another
// account/draft reached while this request was in flight.
export async function applyNewVideoDefaults(form,{isCurrent,request=requestJSON,onError=()=>{},onSaved=()=>{}}){
 form._video_defaults_loading=true;form._video_service_edited=false
 const baseline=snapshotVideoDefaults(form)
 try{
  const {defaults}=await request('/api/comfyui/creation-defaults')
  if(!isCurrent())return
  if(defaults&&['api','comfyui'].includes(defaults.video_generation_backend)){
   // Image profiles load independently and may auto-select while this request
   // is pending. Keep each service's choices together, without allowing an
   // image change to discard the account's video backend and workflow.
   let applied=false
   for(const fields of [videoFields,imageFields]){
    if(fields===videoFields&&form._video_service_edited)continue
    if(fields.some(key=>form[key]!==baseline[key]))continue
    for(const key of fields)if(Object.hasOwn(defaults,key)){form[key]=defaults[key];applied=true}
   }
   if(applied)onSaved()
  }
 }catch(e){if(isCurrent())onError(e.message||'默认制作配置读取失败，请在画面编排中检查')}
 finally{if(isCurrent())form._video_defaults_loading=false}
}
