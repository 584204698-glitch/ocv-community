<script setup>
import {computed,ref,watch,nextTick,onMounted,onUnmounted} from 'vue'
import {requestJSON} from '../api'
import { imageSize } from '../icanImageSizes'
import ShotVideoOptions from './ShotVideoOptions.vue'
import OperationStatus from './OperationStatus.vue'
import LanguageModelPresets from './LanguageModelPresets.vue'
import ResizableShotWorkspace from './ResizableShotWorkspace.vue'
import WorkspacePanels from './WorkspacePanels.vue'
import SubtitleStyleEditor from './SubtitleStyleEditor.vue'
import ShotReferencePreview from './ShotReferencePreview.vue'
import { createSerialTaskQueue } from '../serialTaskQueue'
import {createVideoProjectPoller} from '../videoProjectPolling'
import {submitLocalVideoQueue} from '../videoQueueSubmission'
import {confirmWorkbenchAction,cancelWorkbenchConfirmation,showWorkbenchError} from '../workbenchFeedback'
import ParameterReview from './ParameterReview.vue'
import VideoShotNavigator from './VideoShotNavigator.vue'
import DubTimeline from './DubTimeline.vue'
import ManualH3Prompt from './ManualH3Prompt.vue'
import ProductionRecovery from './ProductionRecovery.vue'
import {productionRecovery} from '../productionRecovery'
import VideoInputPanel from './VideoInputPanel.vue'
import DynamicTextModeSelector from './DynamicTextModeSelector.vue'
import StorySourcePanel from './StorySourcePanel.vue'
import ProjectCastPanel from './ProjectCastPanel.vue'
import ProjectVoicePanel from './ProjectVoicePanel.vue'
import DialogueEditor from './DialogueEditor.vue'
import NativeProjectRevision from './NativeProjectRevision.vue'
import NativePromptReview from './NativePromptReview.vue'
import AutoCastNotice from './AutoCastNotice.vue'
import OneClickStatus from './OneClickStatus.vue'
import ShotCastPlan from './ShotCastPlan.vue'
import { useProjectCast } from '../useProjectCast'
import { normalizeDynamicTextMode, dynamicTextModeLabel } from '../dynamicTextMode'
import { shotPromptNotes, shotHasPromptWarning, promptWarningRows } from '../videoPromptWarnings'
const emit=defineEmits(['back','new-project','duplicate-config','edit-audio','edit-config','open-services'])
const props=defineProps({accountKey:{type:String,default:''},suppressAutoAdvance:{type:Boolean,default:false}})
const videoInputBusy=ref(false),videoInputDirty=ref(false),videoPromptSyncPending=ref(false)
const serverAutoRunning=ref(false)
const projectVoiceBusy=ref(false)
const nativeDialogueBusy=ref(false),nativeDialogueDirty=ref(false)
const nativeRevisionBusy=ref(false)
let navigationEpoch=0,disposed=false
function currentScope(epoch,account){return !disposed&&epoch===navigationEpoch&&account===props.accountKey}
function requireAdoptedVideoInputs(){
 if(nativeRevisionBusy.value){error.value='全片动态方案正在检查或采用，请稍候。';return false}
 if(nativeDialogueBusy.value||nativeDialogueDirty.value){error.value='本镜台词正在保存或尚未保存，请先保存或放弃修改。';return false}
 if(projectVoiceBusy.value){error.value='角色音色正在读取或保存，请稍候。';return false}
 if(projectCastBusy.value){error.value='项目角色正在读取或保存，请稍候。';return false}
 if(projectCastDirty.value){error.value='项目角色资料尚未保存，请先保存，或放弃修改并刷新后再继续。';return false}
 if(videoInputBusy.value){error.value='视频输入正在读取、保存或同步正文，请稍候。';return false}
 if(videoInputDirty.value){error.value='本镜的视频输入尚未采用，请先采用，或放弃修改并刷新后再继续。';return false}
 return true
}
function requireSyncedVideoPrompts(shots,{resume=false}={}){
 if(videoPromptSyncPending.value&&shots.some(shot=>shot.id===selectedShot.value?.id&&!(resume&&shot.video_resume_available&&shot.video_task_id))){error.value='本镜正文尚未按已采用的输入同步，请先完成正文同步，再生成视频。';return false}
 return true
}
function selectVideoShot(index){if(!requireAdoptedVideoInputs())return;selected.value=index;rememberSelectedShot();boundary.value=1;closeBoundaryEditor()}
function newVideoProject(){if(requireAdoptedVideoInputs())emit('new-project')}
function backToProjects(){
 if(!requireAdoptedVideoInputs())return
 if(busy.value){error.value='正在处理当前操作，请稍候再返回项目。';return}
 if(dirty.value){error.value='当前镜头有未保存修改，请先保存后再返回项目。';return}
 emit('back')
}
const items=ref([]), project=ref(null), selected=ref(0), error=ref(''), busy=ref(false), dirty=ref(false)
const pollingError=ref('')
const nativeSpeech=computed(()=>['dialogue','mixed'].includes(project.value?.creation_parameters?.speech_mode))
const externalDialogue=computed(()=>nativeSpeech.value&&project.value?.creation_parameters?.dialogue_route==='external_tts')
const exportReviewNotice=computed(()=>{
 const review=project.value?.export?.review
 if(review?.mode==='observation')return '观察版：未执行导出前自动质量检查，尚未人工验收。'
 if(review?.mode==='automatic')return `自动画面抽样检查 ${review.visual_shots_checked||0} 镜，原声文字检查 ${review.speech_shots_checked||0} 镜。声线、口型和完整观感仍需人工验收；自动检查不代表成片合格。`
 return '历史导出没有质量检查记录，不能视为已验收。'
})
function acceptVoiceUpdate(value){if(project.value?.id===value.project_id)project.value={...project.value,revision:value.revision,status:value.project_status||project.value.status,shots:project.value.shots.map(shot=>({...shot,dialogue_stale:(value.stale_shot_ids||[]).includes(shot.id)}))}}
async function confirmAction(message){
 const epoch=navigationEpoch,account=props.accountKey,id=project.value?.id
 const accepted=await confirmWorkbenchAction(message)
 return accepted&&currentScope(epoch,account)&&project.value?.id===id
}
watch(error,message=>showWorkbenchError(message))
watch([()=>project.value?.id,()=>props.accountKey],()=>{pollingError.value='';cancelWorkbenchConfirmation();showWorkbenchError('')})
onUnmounted(()=>{cancelWorkbenchConfirmation();showWorkbenchError('')})

const view=ref('storyboard'), productionPanel=ref('design')
watch(view,value=>{if(value==='motions'){productionPanel.value='video';view.value='storyboard'}},{flush:'sync'})
watch(selected,value=>{if(value<0)productionPanel.value='design'})
watch(view,value=>{if(value!=='storyboard'&&selected.value<0)selected.value=0})
const exportPreview=ref('raw')
const workspaceTitles=computed(()=>({audio:nativeSpeech.value?'台词与音色':'配音与字幕',storyboard:'分镜与动态制作',motions:'动态镜头',export:'合成与导出',parameters:'参数回顾'}))
const workspaceHints=computed(()=>({audio:nativeSpeech.value?(externalDialogue.value?'核对台词与角色音色；所选配音引擎先合成角色配音，按实测音频安排时间轴，H3 生成画面与嘴部动作。':'核对每句台词与说话角色，选择或设计角色音色；声音将在视频生成时由 H3 演绎。'):'试听配音、核对字幕；需要调整时进入配音精修。',storyboard:'逐镜确认画面与提示词，再将核心图制作成动态片段。',motions:'逐镜试看、重新生成或替换本地视频，满意后合成成片。',export:'预览和下载成片；合成选项可随时调整，不必重新生成镜头。',parameters:'查看本项目保存的配置，也可以用相同配置新建作品。'}))
function moveShot(delta){selectVideoShot(Math.max(0,Math.min((project.value?.shots.length||1)-1,selected.value+delta)))}

const motionDrafts=ref({})
function rememberMotionDraft(shot){if(shotEditingStage.value){motionDrafts.value[shot.id]={intent:shot.intent||'',kind:shot.kind,action:shot.action||'',video_prompt:shot.video_prompt||'',reference_audio_enabled:shot.reference_audio_enabled!==false,reference_audio_lipsync:shot.reference_audio_lipsync!==false,presenter_speaking:shot.visual_design?.presenter_speaking??null};dirty.value=true}else dirty.value=true}
function setPresenterSpeaking(shot,enabled){
 shot.visual_design={...(shot.visual_design||{}),presenter_speaking:enabled,...(enabled?{presenter_visible:true}:{})}
 const id=project.value?.context?.video_direction?.presenter?.reference_id||project.value?.creation_parameters?.presenter_reference_id
 if(enabled&&id&&!shot.reference_ids.includes(id))shot.reference_ids.push(id)
 rememberMotionDraft(shot)
}
async function saveMotionDrafts(){for(const [id,fields] of Object.entries(motionDrafts.value)){const updated=await call('/'+project.value.id+'/shots/'+encodeURIComponent(id)+'/motion','POST',{revision:project.value.revision,...fields});delete motionDrafts.value[id];acceptImageUpdate(updated)}dirty.value=false;sessionStorage.removeItem(draftKey(project.value.id))}
const autoMotionBusy=ref(false)
async function autoFillMotion(shot){
 autoMotionBusy.value=true
 videoLogsOpen.value=true
 const shotNumber=project.value.shots.findIndex(row=>row.id===shot.id)+1
 project.value.logs.push(`第 ${String(shotNumber).padStart(2,'0')} 镜：正在按核心图规划动态表达与视频模型最终提示词…`)
 try{acceptImageUpdate(await call('/'+project.value.id+'/shots/'+encodeURIComponent(shot.id)+'/refresh-prompts','POST',{revision:project.value.revision,basis:'image',action:shot.action||'',image_prompt:shot.image_prompt||''}))}
 catch(e){project.value.logs.push(`第 ${String(shotNumber).padStart(2,'0')} 镜：动态表达与视频提示词更新失败：${e.message}`);throw e}
 finally{autoMotionBusy.value=false}
}
const motionQueueStates=ref({})
const motionQueue=createSerialTaskQueue(async task=>{
 while(busy.value)await new Promise(resolve=>setTimeout(resolve,50))
 if(project.value?.id!==task.projectId)return
 busy.value=true
 try{
  const current=project.value.shots.find(row=>row.id===task.shotId)
  if(!current)throw new Error('排队镜头已不存在')
  if(shotEditingStage.value){
   const fields={...(motionDrafts.value[current.id]||{}),kind:task.kind,
    action:current.action||'',video_prompt:current.video_prompt||'',
    reference_audio_enabled:current.reference_audio_enabled!==false,
    reference_audio_lipsync:current.reference_audio_lipsync!==false}
   const updated=await call('/'+task.projectId+'/shots/'+encodeURIComponent(current.id)+'/motion','POST',{revision:project.value.revision,...fields})
   delete motionDrafts.value[current.id]
   acceptImageUpdate(updated)
  }else{current.kind=task.kind;await save()}
  if(task.kind==='video')await autoFillMotion(project.value.shots.find(row=>row.id===task.shotId))
 }catch(e){error.value='镜头切换/动态规划失败：'+e.message+'；其他排队镜头将继续处理。';throw e}
 finally{busy.value=false;dirty.value=Object.keys(motionDrafts.value).length>0}
},(key,state)=>{motionQueueStates.value={...motionQueueStates.value,[key]:state}})
function motionQueueState(shot){return motionQueueStates.value[project.value?.id+':'+shot.id]}
function changeShotKind(shot,event){
 if(nativeSpeech.value||!requireAdoptedVideoInputs())return
 const kind=event.target.value
 if(kind===shot.kind)return
 shot.kind=kind
 rememberMotionDraft(shot)
 motionQueue.enqueue(project.value.id+':'+shot.id,{projectId:project.value.id,shotId:shot.id,kind})
}
const videoStages=['video_generation_ready','video_generating','video_stopping','video_review','exporting','export_failed','completed']
const videoModel=ref(null),videoModelError=ref(''),videoLogsOpen=ref(false)
const comfyProfiles=ref([]),comfyLoading=ref(false),comfyError=ref(''),shotOptionDrafts=ref({})
try{const saved=JSON.parse(sessionStorage.getItem('ocv.shot-options.drafts')||'{}');if(saved&&typeof saved==='object'&&!Array.isArray(saved))shotOptionDrafts.value=saved}catch{}
watch(shotOptionDrafts,value=>{try{sessionStorage.setItem('ocv.shot-options.drafts',JSON.stringify(value))}catch{}},{deep:true})
const apiVideoReady=computed(()=>videoModel.value?.source==='dedicated'&&videoModel.value?.has_api_key)
async function loadComfyProfiles(){comfyLoading.value=true;comfyError.value='';try{const data=await requestJSON('/api/comfyui');comfyProfiles.value=(data.profiles||[]).filter(item=>item.kind==='video')}catch(e){comfyError.value=e.message}finally{comfyLoading.value=false}}
function optionsFor(shot,includeDraft=true){
 const key=project.value.id+':'+shot.id
 if(includeDraft&&shotOptionDrafts.value[key])return shotOptionDrafts.value[key]
 const params=project.value.creation_parameters||{},saved=shot.video_generation_options||{},request=shot.video_request||{}
 return {backend:saved.backend||request.backend||(['api','comfyui'].includes(shot.video_backend)?shot.video_backend:params.video_generation_backend)||'api',profile_id:saved.profile_id??shot.video_input_selection?.profile_id??request.profile_id??params.comfyui_profile_id??comfyProfiles.value[0]?.id??'',resolution:saved.resolution??request.resolution??'',width:saved.width??request.width,height:saved.height??request.height,h3_prompt_agent:saved.h3_prompt_agent??(request.prompt_format==='h3_ref2va'||Boolean(params.comfyui_h3_prompt_agent))}
}
const normalizedOptions=value=>({backend:value.backend,profile_id:value.backend==='comfyui'?value.profile_id:'',resolution:value.resolution||'',h3_prompt_agent:value.backend==='comfyui'&&Boolean(value.h3_prompt_agent),...(value.resolution==='custom'?{width:value.width,height:value.height}:{})})
const selectedOptionsDirty=computed(()=>Boolean(selectedShot.value)&&JSON.stringify(normalizedOptions(optionsFor(selectedShot.value)))!==JSON.stringify(normalizedOptions(optionsFor(selectedShot.value,false))))
async function saveShotOptions(){if(!requireAdoptedVideoInputs())return;const shot=selectedShot.value;if(!shot)return;await run(async()=>{project.value=await call('/'+project.value.id+'/shots/'+encodeURIComponent(shot.id)+'/generation-options','PUT',{revision:project.value.revision,options:normalizedOptions(optionsFor(shot))});delete shotOptionDrafts.value[project.value.id+':'+shot.id]})}
async function applyAllShotOptions(){if(!requireAdoptedVideoInputs())return;const shot=selectedShot.value;if(!shot)return;const shots=dynamicShots.value;if(!await confirmAction('将当前设置应用到全部 '+shots.length+' 个动态镜头？\n'+generationSummary(shot)+'\n会覆盖各镜头的生成配置；现有素材保留，点击不会开始生成。'))return;await run(async()=>{project.value=await call('/'+project.value.id+'/generation-options','PUT',{revision:project.value.revision,options:normalizedOptions(optionsFor(shot))});for(const item of shots)delete shotOptionDrafts.value[project.value.id+':'+item.id]})}
function generationSummary(shot){const options=optionsFor(shot),profile=comfyProfiles.value.find(item=>item.id===options.profile_id);return (options.backend==='comfyui'?(profile?.engine==='managed'?'OCV 内置 ComfyUI · ':'外部 ComfyUI · ')+(profile?.name||'未选择工作流'):'视频 API')+' · '+(options.resolution==='custom'?options.width+' × '+options.height:options.resolution||profile?.resolution_preset||videoModel.value?.resolution||'跟随设置')}
const shotConfigured=shot=>{const options=optionsFor(shot);return options.backend==='comfyui'?comfyProfiles.value.some(item=>item.id===options.profile_id):Boolean(apiVideoReady.value)}
async function confirmLongApiShots(shots){
 const longShots=shots.filter(shot=>optionsFor(shot).backend==='api'&&Number(shot.generation_duration||Math.max(4,Math.ceil(Number(shot.duration)||0)))>15)
 if(!longShots.length)return true
 const lines=longShots.map(shot=>'第 '+String(project.value.shots.findIndex(item=>item.id===shot.id)+1).padStart(2,'0')+' 镜：请求 '+Number(shot.generation_duration||Math.max(4,Math.ceil(Number(shot.duration)||0)))+' 秒')
 return confirmAction('长镜头 API 兼容性提醒\n\n'+lines.join('\n')+'\n\n以上镜头超过建议的 15 秒。不同服务商／模型的时长上限不同，请确认当前视频 API 支持这些请求时长。\nOCV 将按原时长提交，不会自动裁短；不支持时可能失败，费用以服务商规则为准。\n\n确认支持并继续？取消后可拆分镜头或更换模型。')
}
async function submitVideoBatch(shots,{automated=false,regenerate=false,retryFailed=false}={}){
 if(!requireAdoptedVideoInputs()||!requireSyncedVideoPrompts(shots,{resume:!retryFailed&&!regenerate}))return
 if(!shots.length)return
 const unchecked=shots.filter(shot=>shot.design_needs_review)
 if(unchecked.length){
  const numbers=unchecked.map(shot=>String(project.value.shots.findIndex(item=>item.id===shot.id)+1).padStart(2,'0'))
  error.value='第 '+numbers.join('、')+' 镜字幕范围调整后，设计尚未更新确认。请返回分镜编辑，完整重规划本镜或按核心图更新视频提示词后再生成；本次未提交生成任务。'
  selected.value=project.value.shots.findIndex(item=>item.id===unchecked[0].id)
  return
 }
 if(shots.some(shot=>!shotConfigured(shot))){error.value='部分镜头尚未配置工作流或视频 API，请逐镜检查生成设置。';return}
 if(!await confirmLongApiShots(shots))return
 const lines=shots.map(shot=>'第 '+String(project.value.shots.findIndex(item=>item.id===shot.id)+1).padStart(2,'0')+' 镜：'+generationSummary(shot))
 const paid=shots.filter(shot=>optionsFor(shot).backend==='api').length
 if(!automated&&!await confirmAction((regenerate?'重新生成全部所选镜头，已有片段会保留到历史记录。':'生成所选未完成镜头。')+'\n按各镜头设置执行；页面内尚未保存的配置也会随本次提交保存。\n\n'+lines.join('\n')+'\n\n'+(paid?paid+' 个镜头使用视频 API，可能产生费用。':'全部使用 ComfyUI，内置／外部引擎按各镜头工作流执行。')+'\n是否开始？'))return
 const shot_options=Object.fromEntries(shots.map(shot=>[shot.id,normalizedOptions(optionsFor(shot))]))
 await run(async()=>{const extras={shot_options,regenerate_completed:regenerate};project.value=paid===0
  ?await submitLocalVideoQueue({record:project.value,shotIds:shots.map(shot=>shot.id),retryFailed,call:(path,method,body)=>call(path,method,method==='POST'?{...body,...extras}:body),onRefresh:acceptImageUpdate})
  :await call('/'+project.value.id+'/videos/generate','POST',{revision:project.value.revision,shot_ids:shots.map(shot=>shot.id),retry_failed:retryFailed,...extras});for(const shot of shots)delete shotOptionDrafts.value[project.value.id+':'+shot.id];view.value='motions';videoLogsOpen.value=true})
}
const selectedOptions=computed({get:()=>selectedShot.value?optionsFor(selectedShot.value):{backend:'api',resolution:'',profile_id:'',h3_prompt_agent:false},set:value=>{if(selectedShot.value)shotOptionDrafts.value[project.value.id+':'+selectedShot.value.id]=value}})
const selectedLocalVideo=computed(()=>selectedOptions.value.backend==='comfyui')
const selectedVideoConfigured=computed(()=>selectedLocalVideo.value?comfyProfiles.value.some(item=>item.id===selectedOptions.value.profile_id):apiVideoReady.value)
const shotOptionsLocked=computed(()=>busy.value||videoInputsPreparing.value||nativeRevisionBusy.value||nativeDialogueBusy.value||nativeDialogueDirty.value||shotRenderLocked(selectedShot.value)||selectedShot.value?.video_status==='running'||Boolean(videoRunning.value&&selectedShot.value?.video_queued)||Boolean(selectedShot.value?.video_request&&!selectedShot.value?.video_terminal&&selectedShot.value?.video_status!=='completed'))
const localQueueRunning=computed(()=>videoRunning.value&&['comfyui','mixed'].includes(project.value?.active_video_backend||project.value?.creation_parameters?.video_generation_backend))
const videoStageReady=computed(()=>videoStages.includes(project.value?.status))
const videoRunning=computed(()=>['video_generating','video_stopping'].includes(project.value?.status))
const storyboardStageLabel=computed(()=>({stopping:'正在停止规划…',planning:'正在规划…',generation_ready:'分镜已确认，等待生成核心图',image_generating:'正在生成核心分镜图…',image_stopping:'正在停止核心图生成…',image_review:'请检查核心分镜图',video_generation_ready:'核心图已确认，等待生成动态镜头',video_generating:'动态镜头生成中，可编辑空闲镜头',video_stopping:'动态镜头正在安全停止',video_review:'逐镜编辑、生成与检查'}[project.value?.status]||'分镜确认与编辑'))
const dynamicShots=computed(()=>(project.value?.shots||[]).filter(shot=>shot.kind==='video'))
const completedVideos=computed(()=>dynamicShots.value.filter(shot=>shot.video_status==='completed').length)
const localVideo=computed(()=>project.value?.creation_parameters?.video_generation_backend==='comfyui')
const projectReferenceAudio=computed(()=>Boolean(project.value?.creation_parameters?.presenter_mode)||(localVideo.value&&project.value?.creation_parameters?.comfyui_reference_audio===true&&!project.value?.creation_parameters?.comfyui_h3_prompt_agent))
const videoConfigured=computed(()=>Boolean(dynamicShots.value.length&&dynamicShots.value.every(shotConfigured)))
const canProcessVideo=shot=>shot.kind==='video'&&!shotRenderLocked(shot)&&shot.video_status!=='completed'&&!shot.video_terminal&&shot.video_status!=='failed'&&!(shot.video_status==='unknown'&&!shot.video_resume_available)
const pendingVideos=computed(()=>dynamicShots.value.filter(canProcessVideo))
const localRetryVideos=computed(()=>localVideo.value?dynamicShots.value.filter(shot=>shot.video_status==='failed'&&shot.video_terminal):[])
const localQueueCandidates=computed(()=>localVideo.value&&project.value?.status==='video_generating'?dynamicShots.value.filter(shot=>shot.video_status==='pending'&&!shot.video_terminal):[])
const videoStatusLabel=shot=>shot.kind!=='video'?'静态画面':shot.video_status==='stopped'&&shot.video_resume_available?'后台任务待确认':shot.video_waiting?'等待提交':shot.video_queued&&videoRunning.value?'已加入队列':shot.video_design_changed&&shot.video_status==='pending'?'设计已更新 · 待重生成':({pending:'等待生成',running:'处理中',completed:'已完成',failed:'生成失败',unknown:'状态待核实',stopped:'已停止'}[shot.video_status]||'等待生成')
const videoTask=shot=>({status:shot.video_queued&&videoRunning.value?'queued':shot.video_status,message:shot.video_queued&&videoRunning.value?'已加入 OCV 队列，等待处理':shot.video_progress_message||videoStatusLabel(shot),started_at:shot.video_started_at,finished_at:shot.video_finished_at,error:shot.video_error})
const videoBatchSummary=computed(()=>`已完成 ${completedVideos.value}/${dynamicShots.value.length} 镜 · 处理中 ${dynamicShots.value.filter(s=>s.video_status==='running').length} 镜 · OCV 排队 ${dynamicShots.value.filter(s=>s.video_queued||s.video_waiting).length} 镜`)
const requestedSeconds=shot=>Number(shot.video_request?.duration||shot.generation_duration||Math.max(4,Math.ceil(Number(shot.duration)||0)))
const requestedResolution=shot=>shot.video_request?.width&&shot.video_request?.height?`${shot.video_request.width} × ${shot.video_request.height}`:shot.video_request?.resolution||(shot.video_backend==='comfyui'&&shot.video_request?'旧记录未保存分辨率':shot.video_resolution||(optionsFor(shot).backend==='comfyui'?'按所选工作流':'尚未提交'))
const videoUrl=shot=>base+'/'+project.value.id+'/videos/'+encodeURIComponent(shot.id)+'?v='+encodeURIComponent((shot.video_version||project.value.revision)+'|'+(shot.narration_preview||'original'))

const historyVideoUrl=(shot,index)=>base+'/'+project.value.id+'/videos/'+encodeURIComponent(shot.id)+'/history/'+index+'?v='+encodeURIComponent(project.value.revision)
function defaultProjectView(record){return ['exporting','export_failed','completed'].includes(record?.status)?'export':videoStages.includes(record?.status)?'motions':'storyboard'}
const pendingAudioShots=computed(()=>project.value?.shots?.filter(s=>s.audio_timing_adjustment?.needs_review)||[])
watch(()=>[project.value?.id,project.value?.audio_version],async()=>{await nextTick();const index=project.value?.shots?.findIndex(s=>s.audio_timing_adjustment?.needs_review)??-1;if(index>=0){selected.value=index;view.value=defaultProjectView(project.value)}})
const narrationUrl=computed(()=>project.value?base+'/'+project.value.id+'/audio?v='+encodeURIComponent(project.value.audio_version||project.value.revision):'')
const previewAdaptRate=ref(1)
function nextAudioReview(){const shots=project.value?.shots||[];const indices=shots.map((s,i)=>s.audio_timing_adjustment?.needs_review?i:-1).filter(i=>i>=0);if(indices.length){selected.value=indices.find(i=>i>selected.value)??indices[0];view.value=videoStageReady.value?'motions':'storyboard'}}
async function confirmAudioReview(shot){await run(async()=>{project.value=await call('/'+project.value.id+'/shots/'+encodeURIComponent(shot.id)+'/confirm-audio-timing','POST',{revision:project.value.revision})});if(pendingAudioShots.value.length)nextAudioReview()}
const allVideosReady=computed(()=>Boolean(project.value?.shots.length)&&!project.value.shots.some(s=>s.video_waiting||s.video_queued||s.design_needs_review)&&videoStageReady.value&&project.value.shots.every(shot=>shot.kind==='video'?shot.video_status==='completed':shot.image_status==='completed'))
const exportRunning=computed(()=>project.value?.status==='exporting')
const useVideoAudio=ref(true)
const narrationVolume=ref(100)
const endTitleText=ref('')
const exportLayoutSettings=ref({video_orientation:'landscape',subtitle_layouts:{}})
const exportLayoutVariant=ref('both')
const exportLayoutDirty=computed(()=>JSON.stringify(exportLayoutSettings.value.subtitle_layouts)!==JSON.stringify(project.value?.creation_parameters?.subtitle_layouts||{}))
watch(()=>[project.value?.id,project.value?.creation_parameters?.subtitle_layouts,project.value?.settings?.ratio],(value,old)=>{
 if(!old||value[0]!==old[0]||JSON.stringify(exportLayoutSettings.value.subtitle_layouts)===JSON.stringify(old[1]||{})){
  exportLayoutSettings.value={video_orientation:value[2]==='9:16'?'portrait':'landscape',subtitle_layouts:JSON.parse(JSON.stringify(value[1]||{}))}
 }
 exportLayoutSettings.value.video_orientation=value[2]==='9:16'?'portrait':'landscape'
},{deep:true})

const exportUrl=variant=>base+'/'+project.value.id+'/export/'+variant+'?v='+encodeURIComponent(project.value.revision)
async function syncAudioTimeline(){
 await run(async()=>{project.value=await call('/'+project.value.id+'/sync-audio','POST',{revision:project.value.revision});view.value='audio'})
}
async function exportVideo(automated=false){
 if(!requireAdoptedVideoInputs())return
 if(!allVideosReady.value)return
 if(pendingAudioShots.value.length&&!await confirmAction(`还有 ${pendingAudioShots.value.length} 个镜头的配音变化尚未检查。确定仍然合成？取消可返回检查。`)){nextAudioReview();return}
 const audioNote=nativeSpeech.value?(externalDialogue.value?'角色配音采用所选配音引擎，时间轴使用实测音频位置；H3 画面与嘴部动作仍需观看确认。':'保留每镜完整原生对白与环境声。手动观察版字幕采用计划时间，仍需核对实际说话位置。'):useVideoAudio.value?'动态片段整条原声会降低音量后与旁白混合，其中也可能包含模型生成的配乐。':'动态片段整条原声将被忽略，包括环境声和动作声；成片只使用旁白。'
 const timelineNote=nativeSpeech.value?'将按实际片段长度拼接，生成字幕版与纯净版观察成片。':'将按字幕时间轴裁切动态片段、补齐静态镜头，并生成字幕版与纯净版成片。'
 if(automated!==true&&!await confirmAction(timelineNote+'\n\n'+audioNote+'\n\n是否开始？'))return
 await run(async()=>{project.value=await call('/'+project.value.id+'/export','POST',{revision:project.value.revision,use_video_audio:nativeSpeech.value||useVideoAudio.value,end_title_text:endTitleText.value,...(Object.keys(exportLayoutSettings.value.subtitle_layouts).length||exportLayoutDirty.value?{subtitle_layouts:exportLayoutSettings.value.subtitle_layouts}:{}),...(nativeSpeech.value?{}:{narration_volume:Number(narrationVolume.value)/100})});view.value='export'})

}
async function openExportFolder(){await run(async()=>{await call('/'+project.value.id+'/export/open','POST')})}
async function loadVideoModel(){
 videoModelError.value=''
 try{videoModel.value=await requestJSON('/api/video-model')}catch(e){videoModelError.value=e.message}
}
async function generateVideos(shots,retryFailed=false,automated=false,singleShot=false){
 if(!requireAdoptedVideoInputs())return
 if(!requireSyncedVideoPrompts(shots,{resume:!retryFailed}))return

 if(!shots.length)return
 if(!singleShot&&!(shots.length===1&&shots[0].video_resume_available&&shots[0].video_task_id&&!retryFailed))return submitVideoBatch(shots,{automated,retryFailed})
 const options=optionsFor(shots[0])
 const useLocal=(!retryFailed&&shots[0].video_resume_available&&shots[0].video_task_id?shots[0].video_request?.backend||shots[0].video_backend||options.backend:options.backend)==='comfyui'
 const resuming=!retryFailed&&shots.every(shot=>shot.video_resume_available&&shot.video_task_id)
 const optionsPayload=options&&!resuming?{options}:{}
 if(useLocal){
  const queueNote=videoRunning.value?'当前已有镜头在生成，本次选择将追加到队列，前一镜完成后自动继续。':'将按镜头顺序提交到本地生成队列。'
  const profile=comfyProfiles.value.find(item=>item.id===(options?.profile_id||project.value.creation_parameters.comfyui_profile_id))
  let resourceNote=''
  if(!automated&&singleShot&&profile){
   try{const query=new URLSearchParams({profile_id:profile.id,ratio:project.value.settings.ratio||'16:9',resolution:options?.resolution||'',seconds:String(requestedSeconds(shots[0])),...(options?.resolution==='custom'?{width:options.width,height:options.height}:{})});const estimate=await requestJSON('/api/comfyui/resource-estimate?'+query);resourceNote=`\n\n本镜粗估：显存 ${estimate.vram_estimate_gb.join('–')} GB（当前空闲 ${estimate.vram_free_gb??'未知'} GB），内存 ${estimate.ram_estimate_gb.join('–')} GB（当前可用 ${estimate.ram_available_gb??'未知'} GB）。${estimate.risk==='high'?'⚠ 资源溢出风险较高，请优先降低分辨率或关闭占用程序。':estimate.risk==='caution'?'资源余量接近上限。':''}\n估算不保证运行时不会溢出。`}
   catch(e){resourceNote='\n\n暂时无法读取资源预估：'+e.message}
  }
  if(!automated&&!await confirmAction(`将使用 ComfyUI 工作流「${profile?.name||'项目预设'}」生成 ${shots.length} 个镜头，分辨率：${options?.resolution==='custom'?options.width+' × '+options.height:options?.resolution||profile?.resolution_preset||(profile?profile.default_width+' × '+profile.default_height:'工作流默认')}。\n${queueNote}${resourceNote}\n是否开始？`))return
  await run(async()=>{project.value=await submitLocalVideoQueue({record:project.value,shotIds:shots.map(shot=>shot.id),retryFailed,call:(path,method,body)=>call(path,method,method==='POST'?{...body,...optionsPayload}:body),onRefresh:acceptImageUpdate});view.value='motions';videoLogsOpen.value=true});return

 }
 if(!apiVideoReady.value&&!resuming){error.value='请先到“接口与服务”保存视频 API 配置，再开始生成。';return}
 if(!await confirmLongApiShots(shots))return
 const lines=shots.map(shot=>{const index=project.value.shots.findIndex(item=>item.id===shot.id)+1;return '第 '+String(index).padStart(2,'0')+' 镜：'+requestedSeconds(shot)+' 秒 · '+(options?.resolution||shot.video_request?.resolution||videoModel.value.resolution)+(shot.video_resume_available&&shot.video_task_id&&!retryFailed?' · 查询原任务，不重新提交':shot.video_not_submitted&&!retryFailed?' · 继续首次付费生成（尚未提交）':' · 新的付费生成')})
 const message=(retryFailed?'云端已确认上次任务失败。本操作会提交新的付费请求，服务商可能再次扣费。':'推荐先试生成 1～2 镜，确认效果后再生成其余镜头。')+'\n\n'+lines.join('\n')+'\n\n静音生成，仅上传本镜已选参考图；费用以你的服务商实际计费为准。是否继续？'
 if(!automated&&!await confirmAction(message))return
 await run(async()=>{project.value=await call('/'+project.value.id+'/videos/generate','POST',{revision:project.value.revision,shot_ids:shots.map(shot=>shot.id),retry_failed:retryFailed,...optionsPayload});view.value='motions';videoLogsOpen.value=true})

}
const rerunQueued=shot=>Boolean(shot.video_waiting||shot.video_queued)
async function cancelVideoQueue(shot){await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/videos/'+encodeURIComponent(shot.id)+'/cancel-queue','POST',{revision:project.value.revision}));productionPanel.value='design'})}
async function resumeVideoQueue(){if(!requireAdoptedVideoInputs())return;await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/videos/queue/resume','POST',{revision:project.value.revision}))})}
async function regenerateVideo(shot){
 if(!requireAdoptedVideoInputs())return
 if(!requireSyncedVideoPrompts([shot]))return
 if(rerunQueued(shot)||shot.video_queued)return
 if(!await confirmLongApiShots([shot]))return
 const audioNote=shot.native_dialogue?(externalDialogue.value?'先用已采用的角色音色合成台词配音，按实测音频安排镜头；H3 生成画面与嘴部动作，口型仍需核对。':project.value.creation_parameters?.narration_route==='external_tts'?'旁白由 IndexTTS2.5 单独合成并测量时长；H3只生成角色对白与环境声。实测旁白较长时本镜时长可能增加，超过15秒会提示调整。':'本镜将自动采用各说话角色已绑定的音色参考，由 H3 生成原生对白。'):projectReferenceAudio.value&&shot.reference_audio_enabled!==false
  ?'本镜将继续注入对应 TTS 参考音频'+(shot.reference_audio_lipsync!==false?'并要求人物对口型。':'，但不要求人物对口型。')
  :'本镜不会注入参考音频。'
 const options=optionsFor(shot),profile=comfyProfiles.value.find(item=>item.id===options.profile_id)
 const detail=options.backend==='comfyui'?`ComfyUI · ${profile?.name||'所选工作流'} · ${options.resolution==='custom'?options.width+' × '+options.height:options.resolution||profile?.resolution_preset||'工作流默认'}`:`视频 API · ${options.resolution||videoModel.value?.resolution}（可能再次计费）`
 if(!await confirmAction('将按本镜生成设置重新生成：'+detail+'。\n\n'+audioNote+'\n原片段会保留在历史记录中，但新片段生成后将替代它参与合成。是否继续？'))return
 await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/videos/generate','POST',{revision:project.value.revision,shot_ids:[shot.id],regenerate_completed:true,options}));productionPanel.value='video';videoLogsOpen.value=true})

}
async function retryNarrationPreview(shot){
 await run(async()=>{project.value=await call('/'+project.value.id+'/videos/'+shot.id+'/narration-preview','POST',{revision:project.value.revision})})
}
async function regenerateAllVideos(){
 if(!requireAdoptedVideoInputs())return
 return submitVideoBatch(dynamicShots.value,{regenerate:true})
}
async function stopVideos(){if(!await confirmAction(localVideo.value?'取消本项目已提交的当前任务，并停止后续镜头。排队任务移出队列，运行任务按任务编号终止；原素材和记录保留。确定停止？':'停止后不再提交后续镜头。已经提交的云端任务可能继续执行及计费，停止不会取消扣费；可稍后继续查询原任务。确定停止？'))return;await run(async()=>{project.value=await call('/'+project.value.id+'/videos/stop','POST');videoLogsOpen.value=true})}
async function reopenShot(shot){
 if(!requireAdoptedVideoInputs())return
 await run(async()=>{stopMotionPreviewAudio();project.value=await call('/'+project.value.id+'/shots/'+encodeURIComponent(shot.id)+'/reopen','POST',{revision:project.value.revision});productionPanel.value='design';view.value='storyboard';await nextTick();document.querySelector('.video-detail')?.scrollIntoView({behavior:'smooth',block:'start'})})
}

const input=ref({name:'动态视频草案',srt:'',style:'生动清晰的简笔画风格',characters:'',world:'',ratio:'16:9',dynamic_text_mode:'visual_first',scene_references_enabled:true})
const boundary=ref(1),boundaryEditor=ref({open:false,mode:'split'}),sourceAudio=ref(null),boundaryAudio=ref(null)
const motionAudio=ref(null),previewNarration=ref(true)
let boundaryStopTimer=null
const redrawReferenceIds=ref([]),useCurrentReference=ref(false),useSceneReference=ref(true),redrawResolution=ref('')
const redrawIcan=computed(()=>Boolean(project.value?.creation_parameters?.use_cloud_image_pool)&&project.value?.creation_parameters?.method==='ican')
watch(redrawIcan,()=>{redrawResolution.value=''})
const imagePromptDrafts=ref({}),submittedImagePrompts=ref({})
const sceneAssets=computed(()=>project.value?.scene_assets||[])
const selectedShot=computed(()=>project.value?.shots?.[selected.value]||null)
const storyboardRows=computed(()=>[...(project.value?.scene_assets||[]).map((shot,index)=>({shot,i:-index-1})),...(project.value?.shots||[]).map((shot,i)=>({shot,i}))])
const selectedStoryboardAsset=computed(()=>storyboardRows.value.find(row=>row.i===selected.value)?.shot)
watch(()=>selectedStoryboardAsset.value?.id,()=>hydrateRedrawSelection(selectedStoryboardAsset.value))
const redrawResolutions=computed(()=>project.value?.creation_parameters?.image_profile_snapshot?.protocol==='openai_images'?['1k']:['1k','2k','4k'])
watch(redrawResolutions,values=>{if(!redrawIcan.value&&redrawResolution.value&&!values.includes(redrawResolution.value))redrawResolution.value=''},{flush:'sync'})
const recovery=computed(()=>productionRecovery({...project.value,error:error.value||project.value?.error},selectedShot.value))
async function handleRecovery(action){
 if(!requireAdoptedVideoInputs())return
 const shot=selectedShot.value
 if(action==='settings'){emit('open-services');return}
 if(action==='voices'){view.value='audio';await nextTick();document.querySelector('.project-voice-panel')?.scrollIntoView({behavior:'smooth',block:'start'});return}
 if(action==='export'){view.value='export';return}
 if(action==='plan'){await plan();return}
 if(!shot)return
 if(action==='query'){await generateVideos([shot]);return}
 if(action==='edit'){
  if(videoStageReady.value){await reopenShot(shot)}else{view.value='storyboard'}
  return
 }
 if(action==='prepare'){
  if(!await confirmAction('将重新分析当前镜头并继续生成。\n\n会调用语言模型及视频生成服务，可能产生费用。原分析留存，图片、台词、音色、时长及其他镜头保持不变；校验仍不通过则停止，不自动循环重试。是否继续？'))return
  autoAdvanceEnabled.value=false
  const identity=project.value.id,shotId=shot.id
  await run(async()=>{
   const prepared=await call('/'+identity+'/shots/'+encodeURIComponent(shotId)+'/retry-preparation','POST',{revision:project.value.revision})
   // Do not hydrate child editors between these two requests: their refresh
   // sets a temporary busy flag which would otherwise swallow the continuation.
   try{project.value=await call('/'+identity+'/videos/generate','POST',{revision:prepared.revision,shot_ids:[shotId],retry_failed:true})}
   catch(e){project.value=prepared;throw e}
   view.value='motions';videoLogsOpen.value=true
  })
 }
}

const historyPreviewIndex=ref(null)
const selectedVideoHistory=computed(()=>((selectedShot.value?.video_history)||[])
 .map((entry,index)=>({entry,index}))
 .filter(({entry})=>entry?.video&&(entry.video_status==='completed'||entry.video_version)))
watch(()=>selectedShot.value?.id,()=>{historyPreviewIndex.value=null;hydrateRedrawSelection(selectedShot.value)})
function historyVersionLabel(row,position){
 const attempt=Number(row.entry.video_attempt)||0
 const time=Number(row.entry.invalidated_at)||0
 return `历史版本 ${position+1}${attempt?' · 第 '+attempt+' 次生成':''}${time?' · '+new Date(time*1000).toLocaleString():''}`
}
async function uploadReplacementVideo(shot,event){
 const file=event.target.files?.[0];event.target.value='';if(!file)return
 if(!await confirmAction(`将使用本地文件“${file.name}”替换第 ${String(selected.value+1).padStart(2,'0')} 镜的当前动态片段。\n\n现有片段会进入历史记录，其他镜头、配音和分镜不变；最终成片需要重新合成。是否继续？`))return
 await run(async()=>{const data=new FormData();data.append('revision',String(project.value.revision));data.append('file',file);project.value=await requestJSON(base+'/'+project.value.id+'/videos/'+encodeURIComponent(shot.id)+'/upload',{method:'POST',body:data});historyPreviewIndex.value=null;view.value='motions'})
}
async function adoptHistoryVersion(shot,index){
 if(!await confirmAction('采用这个历史片段作为当前版本？\n\n当前片段会自动进入历史记录，只需重新合成成片，不会重新生成其他镜头。'))return
 await run(async()=>{project.value=await call('/'+project.value.id+'/videos/'+encodeURIComponent(shot.id)+'/history/'+index+'/adopt','POST',{revision:project.value.revision});historyPreviewIndex.value=null})
}
async function deleteHistoryVersion(shot,index){
 if(!await confirmAction('永久删除这个未采用的历史视频及其本地文件？此操作无法撤回。'))return
 await run(async()=>{project.value=await call('/'+project.value.id+'/videos/'+encodeURIComponent(shot.id)+'/history/'+index+'?revision='+encodeURIComponent(project.value.revision),'DELETE');historyPreviewIndex.value=null})
}
const scenesById=computed(()=>Object.fromEntries((project.value?.scenes||[]).map(scene=>[scene.slide_id,scene])))
const boundaryPreview=computed(()=>{
 const shot=selectedShot.value;if(!shot)return null
 let own=(shot.slide_ids||[]).map(id=>scenesById.value[id]).filter(Boolean)
 if(boundaryEditor.value.mode==='boundary')own=own.concat((project.value?.shots?.[selected.value+1]?.slide_ids||[]).map(id=>scenesById.value[id]).filter(Boolean))
 if(boundaryEditor.value.mode==='merge'){
  const following=project.value?.shots?.[selected.value+1]
  if(!following)return null
  const right=(following.slide_ids||[]).map(id=>scenesById.value[id]).filter(Boolean)
  return previewSides(own,right)
 }
 const cut=Math.max(1,Math.min(Number(boundary.value)||1,Math.max(1,own.length-1)))
 return previewSides(own.slice(0,cut),own.slice(cut))
})
const sceneReferencesEnabled=computed(()=>{
 const parameters=project.value?.creation_parameters||{},settings=project.value?.settings||{}
 const dynamic=Object.hasOwn(parameters,'dynamic_text_mode')||Object.hasOwn(settings,'dynamic_text_mode')
 return (dynamic||parameters.director_strategy==='enhanced_beta')&&parameters.scene_references_enabled!==false
})
const sceneReferencesBusy=computed(()=>['image_generating','image_stopping'].includes(project.value?.status)&&['planning','generating'].includes(project.value?.scene_references_status))
const sceneReferenceMessage=computed(()=>project.value?.scene_references_message||project.value?.scene_references_error||project.value?.scene_reference_plan?.message||'')
const sceneReferencesNeedGeneration=computed(()=>project.value?.scene_references_status!=='completed'||sceneAssets.value.some(asset=>asset.image_status!=='completed'))
const staleSceneShots=computed(()=>sceneReferencesEnabled.value?(project.value?.shots||[]).filter(shot=>shot.scene_reference_used_version&&shot.scene_reference_used_version!==sceneAssetFor(shot)?.image_version):[])
const staleCoreShots=computed(()=>(project.value?.shots||[]).filter(shot=>shot.core_reference_error))
const imageRetryNeeded=computed(()=>(project.value?.shots||[]).some(shot=>shot.image_status!=='completed')||staleSceneShots.value.length>0||staleCoreShots.value.length>0)
const sceneReferencesBlockConfirm=computed(()=>sceneReferencesEnabled.value&&(sceneReferencesNeedGeneration.value||staleSceneShots.value.length>0))
const allImageAssets=computed(()=>[...(project.value?.shots||[]),...sceneAssets.value])
const sceneAssetFor=shot=>sceneAssets.value.find(asset=>asset.id===shot.scene_reference_id)
const isSceneAsset=asset=>sceneAssets.value.some(scene=>scene.id===asset.id)
function sceneUsageLabel(shot){const asset=sceneAssetFor(shot);if(!asset)return '尚未匹配场景参考';if(!shot.scene_reference_used_version)return '当前核心图尚未使用此场景';return shot.scene_reference_used_version===asset.image_version?'当前核心图已使用此版场景':'场景已更新；重绘本镜头后才会使用新版'}
function sceneUsedByLabel(asset){return (asset.used_by||[]).map(id=>{const index=project.value.shots.findIndex(shot=>shot.id===id);return index>=0?String(index+1).padStart(2,'0'):null}).filter(Boolean).join('、')||'暂无关联镜头'}
function rememberImagePrompt(asset){imagePromptDrafts.value[asset.id]=asset.image_prompt}
function resetProjectImageState(){imagePromptDrafts.value={};submittedImagePrompts.value={};redrawReferenceIds.value=[];useCurrentReference.value=false;useSceneReference.value=true;redrawResolution.value=''}
function acceptImageUpdate(record,discardIds=[]){
 if(project.value&&(record.id!==project.value.id||Number(record.revision)<Number(project.value.revision)))return
 for(const id of discardIds){delete imagePromptDrafts.value[id];delete submittedImagePrompts.value[id]}
 for(const asset of [...(record.shots||[]),...(record.scene_assets||[])]){
  if(Object.hasOwn(submittedImagePrompts.value,asset.id)){
   if(asset.image_task?.status==='completed'){
    if(imagePromptDrafts.value[asset.id]===submittedImagePrompts.value[asset.id])delete imagePromptDrafts.value[asset.id]
    delete submittedImagePrompts.value[asset.id]
   }else if(asset.image_task?.status==='failed')delete submittedImagePrompts.value[asset.id]
  }
  if(Object.hasOwn(imagePromptDrafts.value,asset.id))asset.image_prompt=imagePromptDrafts.value[asset.id]
 }
 for(const shot of record.shots||[]){if(motionDrafts.value[shot.id])Object.assign(shot,motionDrafts.value[shot.id])}
 project.value=record
}
const promptWarnings=computed(()=>shotPromptNotes(selectedShot.value))
const warnedShotRows=computed(()=>promptWarningRows(project.value?.shots))
const warnedShots=computed(()=>warnedShotRows.value.length)
async function showWarning(index){if(!requireAdoptedVideoInputs())return;selected.value=index;rememberSelectedShot();boundary.value=1;closeBoundaryEditor();await nextTick();const details=document.querySelector('.current-shot-warning');if(details){details.open=true;details.scrollIntoView({behavior:'smooth',block:'center'})}}
const planningCanResume=computed(()=>Boolean(project.value?.planning_resume_available)&&!dirty.value)
const planningButtonLabel=computed(()=>planningCanResume.value?'继续未完成规划':project.value?.shots?.length?'重新规划':'重试规划')
const replanMode=ref('text_assisted'),replanRegroup=ref(false)
watch(()=>[project.value?.id,project.value?.creation_parameters?.dynamic_text_mode,project.value?.settings?.dynamic_text_mode],()=>{
 replanMode.value=normalizeDynamicTextMode(project.value?.creation_parameters?.dynamic_text_mode??project.value?.settings?.dynamic_text_mode)
 replanRegroup.value=false
})
const sources=ref([]),sourceId=ref(''),importName=ref('')
const projectCast=useProjectCast({project,accountKey:()=>props.accountKey,
 disabled:()=>busy.value||videoInputBusy.value||videoRunning.value||imageEditRunning.value||exportRunning.value||['planning','stopping','image_generating','image_stopping'].includes(project.value?.status),
 uploadEnabled:()=>Boolean(project.value?.id),
 onUpdated:record=>{if(project.value)project.value={...project.value,revision:record.revision}}})
const {snapshot:projectCastSnapshot,busy:projectCastBusy,dirty:projectCastDirty}=projectCast
const videoInputsPreparing=computed(()=>videoInputBusy.value||projectCastBusy.value||projectVoiceBusy.value)
const newVisual=ref({style:'生动清晰的简笔画风格',characters:'',world:'',ratio:'16:9',dynamic_text_mode:'visual_first',scene_references_enabled:true})
function chooseSource(){const source=sources.value.find(item=>item.id===sourceId.value);importName.value=source?source.name.slice(0,90)+' · 动态版':''}
async function importProject(){await run(async()=>{if(dirty.value)await save();project.value=await call('/from-project','POST',{job_id:sourceId.value,name:importName.value,...newVisual.value});selected.value=0;view.value='storyboard';dirty.value=false;await list()})}
let timer
const draftKey=id=>'ocv-video-draft:'+encodeURIComponent(props.accountKey)+':'+id
const selectedShotKey=id=>'ocv-video-shot:'+encodeURIComponent(props.accountKey)+':'+id
function rememberSelectedShot(){
 const record=project.value,shot=record?.shots?.[selected.value]
 if(!props.accountKey||!record?.id||!shot?.id)return
 try{sessionStorage.setItem(selectedShotKey(record.id),shot.id)}catch{}
}
function restoredShotIndex(record){
 if(!props.accountKey)return 0
 try{const id=sessionStorage.getItem(selectedShotKey(record.id));return Math.max(0,(record.shots||[]).findIndex(shot=>shot.id===id))}catch{return 0}
}
watch(()=>project.value?.id,(id,previous)=>{if(id!==previous){resetProjectImageState();motionDrafts.value={};productionPanel.value=videoStages.includes(project.value?.status)?'video':'design';useVideoAudio.value=project.value?.export_settings?.use_video_audio!==false;narrationVolume.value=Math.round((project.value?.export_settings?.narration_volume??1)*100);endTitleText.value=project.value?.export_settings?.end_title_text||''}},{flush:'sync'})
watch(project,value=>{if(value&&dirty.value){try{sessionStorage.setItem(draftKey(value.id),JSON.stringify(value))}catch{error.value='浏览器无法暂存修改，请使用保存镜头修改按钮。'}}},{deep:true})
const base='/api/video-studio'
async function call(path,method='GET',body){
 const epoch=navigationEpoch,account=props.accountKey
 const result=await requestJSON(base+path,{method,...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})})
 if(!currentScope(epoch,account))throw Object.assign(new Error('项目已切换'),{stale:true})
 return result
}
async function run(fn){
 if(videoInputBusy.value||projectCastBusy.value){error.value='正在读取或保存镜头输入与角色资料，请稍候再操作。';showWorkbenchError(error.value);return}
 const epoch=navigationEpoch,account=props.accountKey
 busy.value=true;error.value=''
 try{await fn()}catch(e){
  if(currentScope(epoch,account)&&!e.stale){
   error.value=e.message
   if(e.status===409&&project.value?.id){
    autoAdvancePaused.value=true
    const id=project.value.id
    try{
     const latest=await call('/'+id)
     if(currentScope(epoch,account)&&project.value?.id===id){
      acceptImageUpdate(latest)
      error.value=e.message+'。已刷新项目状态，当前编辑保留，请核对后再操作。'
     }
    }catch{/* Keep the original conflict and all local drafts if the refresh fails. */}
   }
  }
 }finally{if(currentScope(epoch,account)){busy.value=false;if(project.value?.creation_parameters?.dynamic_auto_advance)setTimeout(advanceAutoPilot,0)}}
}
async function list(){items.value=(await call('')).items.filter(item=>!item.archived)}
async function open(id){if(!requireAdoptedVideoInputs())return;++navigationEpoch;await run(async()=>{if(dirty.value)await save();const fresh=await call('/'+id);let cached;try{cached=JSON.parse(sessionStorage.getItem(draftKey(id))||'null')}catch{}dirty.value=false;resetProjectImageState();motionDrafts.value={};project.value=fresh;selected.value=restoredShotIndex(fresh);view.value=defaultProjectView(fresh);if(cached&&cached.revision===fresh.revision){if(['draft','storyboard_review'].includes(fresh.status)){project.value=cached;dirty.value=true;error.value='已恢复本页暂存的镜头修改，请确认后保存。'}else if(fresh.status==='image_review'){for(const shot of fresh.shots){const draft=cached.shots?.find(row=>row.id===shot.id);if(draft&&(draft.action!==shot.action||draft.video_prompt!==shot.video_prompt)){shot.action=draft.action||'';shot.video_prompt=draft.video_prompt||'';rememberMotionDraft(shot)}}if(dirty.value)error.value='已恢复暂存的动态表达与视频提示词，请确认后保存。'}}})}

async function create(){await run(async()=>{if(dirty.value)await save();project.value=await call('','POST',input.value);selected.value=0;view.value='storyboard';dirty.value=false;await list()})}
const recoverableSettingsImages=computed(()=>project.value&&['draft','storyboard_review'].includes(project.value.status)&&!project.value.shots.some(s=>s.image_status==='completed')&&(project.value.edit_history||[]).some(h=>h.settings&&h.shots?.some(s=>s.image_status==='completed')))
async function restoreSettingsImages(){
 if(!await confirmAction('核对当前故事与画面设置后，恢复设置修改前已生成的图片关联。不调用生成接口；条件不符时保留现状并说明原因。继续？'))return
 await run(async()=>{project.value=await call('/'+project.value.id+'/settings/restore-assets','POST',{revision:project.value.revision});selected.value=0;dirty.value=false})
}
async function plan(automated=false){
 automated=automated===true
 const message=planningCanResume.value?'将复用已保存的规划，仅继续未完成的步骤；仍需调用语言 API，是否继续？':(dirty.value?'将先保存当前镜头修改，再重新规划。':'')+(project.value.manual_groups?.length?'将保留已确认的手动字幕分组，重新设计各镜头，会消耗 LLM 额度。是否继续？':'使用当前语言 API 规划分镜，会消耗 LLM 额度。已有镜头将在成功后被替换，是否继续？')
 if(!automated&&!await confirmAction(message))return
 await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/plan','POST');dirty.value=false})
}
async function save(){if(shotEditingStage.value){await saveMotionDrafts();return}project.value=await call('/'+project.value.id,'PUT',{revision:project.value.revision,shots:project.value.shots});dirty.value=false;sessionStorage.removeItem(draftKey(project.value.id))}
async function replanWithCurrentModel(){
 const mode=replanMode.value,regroup=replanRegroup.value
 const grouping=regroup?'按全文语义重新划分全部镜头，镜头编号、数量和动静类型可能变化；':'保留当前分镜边界与动静类型；'
 if(!await confirmAction(`使用当前语言服务，以「${dynamicTextModeLabel(mode)}」重新规划？\n保留配音、字幕文字与时间轴。${grouping}重新设计画面与提示词，消耗语言模型额度。\n成功后需确认新方案，再生成图片和视频，不自动调用素材生成。失败则保留原方案。`))return
 await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/plan?fresh=true&revision='+project.value.revision+'&regroup='+regroup+'&expression_mode='+encodeURIComponent(mode),'POST');dirty.value=false;view.value='storyboard'})
}
function previewSides(left,right){
 const side=rows=>({rows,text:rows.map(row=>row.text).join('\n'),start:Number(rows[0]?.start||0),end:Number(rows.at(-1)?.end||0)})
 const a=side(left),b=side(right)
 return {left:a,right:b,boundary:a.end,combinedDuration:Number((b.end-a.start).toFixed(3))}
}
function openBoundaryEditor(mode){if(!canEditStructure.value||!requireAdoptedVideoInputs())return;stopBoundaryPreview();mode=mode||(selected.value<project.value.shots.length-1?'boundary':'split');boundaryEditor.value={open:true,mode};if(mode==='boundary')boundary.value=selectedShot.value.slide_ids.length;else if(mode==='split')boundary.value=Math.min(Math.max(1,Number(boundary.value)||1),Math.max(1,(selectedShot.value?.slide_ids?.length||1)-1))}
async function repairDesigns(){if(!await confirmAction('保留当前字幕分组，仅更新受影响镜头的设计，会调用语言 API。继续？'))return;closeBoundaryEditor();await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/plan?repair_only=true','POST')})}
async function confirmDesign(){if(!await confirmAction('确认本镜的核心图、动态表达与视频提示词已检查完毕并沿用？此操作不生成、不扣费。'))return;await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/design/confirm','POST',{revision:project.value.revision,shot_id:selectedShot.value.id})})}
async function undoStructure(){if(!await confirmAction('撤回最近一次结构调整，恢复调整前的镜头与提示词；当前未保存修改会被放弃。继续？'))return;await run(async()=>{project.value=await call('/'+project.value.id+'/structure/undo','POST',{revision:project.value.revision});dirty.value=false;sessionStorage.removeItem(draftKey(project.value.id));selected.value=Math.min(selected.value,project.value.shots.length-1);closeBoundaryEditor()})}

function closeBoundaryEditor(){stopBoundaryPreview();boundaryEditor.value.open=false}
function stopBoundaryPreview(){
 if(boundaryStopTimer){clearTimeout(boundaryStopTimer);boundaryStopTimer=null}
 const audio=boundaryAudio.value;if(audio){audio.pause();audio.ontimeupdate=null}
}
function stopMotionPreviewAudio(){const audio=motionAudio.value;if(audio)audio.pause()}
async function syncMotionPreview(event,phase){
 const video=event.currentTarget,audio=motionAudio.value,shot=selectedShot.value
 if(nativeSpeech.value){if(audio)audio.pause();return}
 if(!shot)return
 const targetDuration=Number(shot.duration),sourceDuration=Number(video.duration)
 const rate=Number.isFinite(sourceDuration)&&targetDuration>sourceDuration+.03?sourceDuration/targetDuration:1
 previewAdaptRate.value=rate
 if(Math.abs(video.playbackRate-rate)>.001)video.playbackRate=rate
 const offset=Math.max(0,Number(video.currentTime)||0)/rate
 if(offset>=targetDuration-.025){if(audio)audio.pause();if(!video.paused)video.pause();return}
 if(!audio||!previewNarration.value){if(audio)audio.pause();return}

 if(phase==='pause'||phase==='ended'){audio.pause();return}
 const target=Number(shot.start)+offset
 if(phase==='seeking'||Math.abs(audio.currentTime-target)>.22)audio.currentTime=target
 audio.playbackRate=1
 if(phase==='play'&&audio.paused){try{await audio.play()}catch{error.value='镜头可以播放，但浏览器阻止了配音同步。请再次点击播放，或检查浏览器声音权限。'}}
}
async function playBoundaryRange(start,end){
 const audio=boundaryAudio.value;if(!audio||!project.value?.audio){error.value='当前任务没有可试听的配音。';return}
 if(!Number.isFinite(Number(start))||!Number.isFinite(Number(end))||Number(end)<=Number(start)){error.value='当前字幕试听范围无效，请重新选择切口。';return}
 error.value='';stopBoundaryPreview();sourceAudio.value?.pause();stopMotionPreviewAudio();audio.currentTime=Math.max(0,Number(start));const finish=()=>{if(audio.currentTime>=Number(end)-.03)stopBoundaryPreview()};audio.ontimeupdate=finish
 try{await audio.play();if(audio.ontimeupdate===finish)boundaryStopTimer=setTimeout(stopBoundaryPreview,Math.max(500,(Number(end)-Number(start)+.3)*1000))}catch{stopBoundaryPreview();error.value='配音暂时无法播放，请检查音频文件或浏览器播放权限。'}
}
function playBoundaryPart(part){const preview=boundaryPreview.value;if(!preview)return;const side=preview[part];playBoundaryRange(side.start,side.end)}
function playBoundaryEdge(part){const preview=boundaryPreview.value;if(!preview)return;const point=preview.boundary;if(part==='left')playBoundaryRange(Math.max(preview.left.start,point-5),point);else if(part==='right')playBoundaryRange(point,Math.min(preview.right.end,point+5));else playBoundaryRange(Math.max(preview.left.start,point-3),Math.min(preview.right.end,point+3))}
async function applyBoundaryEdit(){const mode=boundaryEditor.value.mode;if(mode==='merge'&&!await confirmAction('合并后，后一镜头的字幕和时长会并入当前镜头。是否继续？'))return;if(await structure(mode))closeBoundaryEditor()}
async function structure(action){if(!canEditStructure.value||!requireAdoptedVideoInputs())return false;if(action==='delete'&&!await confirmAction('删除此画面并将字幕并入上一镜（第一镜并入下一镜）。配音保留，受影响视频需重生成；超过15秒将暂转静态。继续？'))return false;structureFeedback.value='正在提交分镜调整…';const oldCount=project.value.shots.length;let completed=false;await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/structure','POST',{revision:project.value.revision,action,index:selected.value,boundary:Number(boundary.value)});selected.value=Math.min(action==='delete'?Math.max(0,selected.value-1):selected.value,project.value.shots.length-1);view.value='storyboard';completed=true});structureFeedback.value=completed?`分镜调整已保存：${oldCount} 镜 → ${project.value.shots.length} 镜。配音与字幕保留。`:(error.value||'操作未完成');return completed}
async function review(){await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/review','POST',{revision:project.value.revision});project.value=await call('/'+project.value.id+'/images/generate','POST',{revision:project.value.revision})})}
async function generateImages(){await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/images/generate','POST',{revision:project.value.revision})})}
async function stopImages(){await run(async()=>{project.value=await call('/'+project.value.id+'/images/stop','POST')})}
const coreImageReview=ref(null)
const cachedCoreReviewError=ref('')
let cachedCoreReviewRequest=0

const currentCoreReview=computed(()=>{
 const report=coreImageReview.value
 return report&&report.project_id===project.value?.id&&report.revision===project.value?.revision&&report.accountKey===props.accountKey?report:null
})
const coreReviewIssues=computed(()=>currentCoreReview.value?.reports.flatMap(r=>r.review.issues)||[])
const coreReviewUncertainties=computed(()=>currentCoreReview.value?.reports.flatMap(r=>[
 ...r.review.uncertainties.map(v=>typeof v==='string'?v:JSON.stringify(v)),
 ...(r.review.continuity_checks||[]).flatMap(c=>['characters','setting'].filter(k=>c[k]?.status==='uncertain').map(k=>c[k].evidence))
])||[])
async function checkCoreImages(){
 if(busy.value||imageEditRunning.value||serverAutoRunning.value)return
 ++cachedCoreReviewRequest
 cachedCoreReviewError.value=''
 await run(async()=>{
 if(dirty.value)await save()
 const result=await call('/'+project.value.id+'/images/check','POST',{revision:project.value.revision})
 coreImageReview.value={...result,accountKey:props.accountKey}
})}
async function checkDynamicOpenings(){
 if(busy.value||dirty.value||imageEditRunning.value||serverAutoRunning.value)return
 if(!await confirmAction('提前执行视频提交使用的开场对接，汇总本项目尚未完成镜头的问题（返修时只检查本镜）。首次或输入变化时会调用语言模型；不生成图片、配音或 H3 视频，已通过的结果会复用。继续？'))return
 await run(async()=>{const result=await call('/'+project.value.id+'/images/check-dynamic-openings','POST',{revision:project.value.revision});project.value=result.project})
}
async function repairCoreIssue(shotId){
 if(busy.value||dirty.value||imageEditRunning.value||serverAutoRunning.value)return
 const report=currentCoreReview.value
 if(!report)return
 const id=project.value.id,revision=project.value.revision,account=props.accountKey,epoch=navigationEpoch
 if(!await confirmAction('仅按报告修复这个镜头的图片，保留原图、故事、台词和其他镜头；会调用图片接口。完成后需要重新检查。继续？'))return
 if(!currentScope(epoch,account)||project.value?.id!==id||project.value?.revision!==revision||busy.value||dirty.value||imageEditRunning.value||serverAutoRunning.value)return
 await run(async()=>{
  acceptImageUpdate(await call('/'+id+'/images/repair-from-review','POST',{revision,shot_id:shotId,fingerprints:report.fingerprints}))
  coreImageReview.value=null
 })
}
async function confirmImages(regenerateShotId=''){
 const unchecked=project.value.shots.map((shot,index)=>shot.design_needs_review?String(index+1).padStart(2,'0'):null).filter(Boolean)
 if(unchecked.length&&!await confirmAction('第 '+unchecked.join('、')+' 镜的字幕范围曾调整。\n\n如果你已检查现有核心图、动态表达与视频提示词，确认后将统一沿用这些设计，返回动态镜头页。\n不会重新规划、生成或扣费。若尚未检查，请取消并继续编辑。\n\n确认沿用并返回？'))return
 await run(async()=>{if(dirty.value)await save();project.value=await call('/'+project.value.id+'/images/confirm','POST',{revision:project.value.revision,...(unchecked.length?{confirm_adjusted_designs:true}:{}),...(regenerateShotId?{regenerate_shot_id:regenerateShotId}:{})});view.value='motions';videoLogsOpen.value=true})
}

async function finishShotReedit(shot){
 // Merely returning from editing must never invalidate an unchanged video.
 // A deliberate reroll remains available on the dynamic-video page.
 if(shot.id!==project.value?.reedit_shot_id||localReeditBlock.value){error.value=localReeditBlock.value||'当前返修镜头已变化，请重新读取';return}
 await confirmImages()
}
const imageUrl=shot=>base+'/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'?v='+project.value.revision
const redrawReferences=computed(()=>project.value?.redraw_references||[])
const showRetiredReferences=ref(false)
watch(()=>project.value?.id,()=>{showRetiredReferences.value=false},{flush:'sync'})
const projectRedrawReferences=computed(()=>{
 const cast=project.value?.project_cast?.characters||[]
 const bound=new Map(cast.map(character=>[character.reference_id,character]))
 const retired=new Set(project.value?.retired_character_reference_ids||[])
 const rows=[...(project.value?.references||[]),...(project.value?.video_input_assets||[]).filter(asset=>bound.has(asset.id))]
 return rows.map((asset,index)=>({...asset,origin:'project',retired:retired.has(asset.id)&&!bound.has(asset.id),
  displayName:bound.has(asset.id)?bound.get(asset.id).name+' · 当前角色参考':asset.description||asset.label||asset.name||`任务参考图 ${index+1}`}))
})
const uploadedRedrawReferences=computed(()=>redrawReferences.value.map((asset,index)=>({
 ...asset,origin:'uploaded',displayName:asset.name||`新增参考图 ${index+1}`
})))
const sceneRedrawReferences=computed(()=>(project.value?.scene_assets||[]).filter(asset=>asset.image_status==='completed').map(asset=>({...asset,origin:'scene',displayName:asset.name||'场景图'})))
const redrawReferenceGallery=computed(()=>[...projectRedrawReferences.value,...sceneRedrawReferences.value,...uploadedRedrawReferences.value])
const visibleRedrawReferences=computed(()=>redrawReferenceGallery.value.filter(asset=>!asset.retired||showRetiredReferences.value||redrawReferenceIds.value.includes(asset.id)))

const redrawReferenceUrl=asset=>base+'/'+project.value.id+'/redraw-references/'+encodeURIComponent(asset.id)+'?v='+project.value.revision
const currentSceneQuality=asset=>!dirty.value&&asset.quality_review?.image_version===asset.image_version&&asset.review_design_fingerprint&&asset.quality_review.design_fingerprint===asset.review_design_fingerprint?asset.quality_review:null
const sceneBlockingNotes=asset=>{const review=currentSceneQuality(asset);return review?.blocking_uncertainties??review?.uncertainties??[]}
const sceneUncertaintyLabel=(asset,index)=>currentSceneQuality(asset)?.uncertainty_assessments?.[index]?.scope==='incidental'?'观察备注（不阻断）':'待核实'
async function repairSceneFromReview(asset){
 await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/scene-references/'+encodeURIComponent(asset.id)+'/repair','POST',{revision:project.value.revision}));dirty.value=false})
}
const imageEditRunning=computed(()=>sceneReferencesBusy.value||allImageAssets.value.some(asset=>asset.image_task?.status==='running'))
watch([()=>project.value?.id,()=>project.value?.revision,()=>project.value?.status,()=>props.accountKey,serverAutoRunning,()=>imageEditRunning.value],async()=>{
 const request=++cachedCoreReviewRequest
 cachedCoreReviewError.value=''
 coreImageReview.value=null
 const current=project.value
 if(!current?.native_story||!['image_review','video_review','video_generation_ready'].includes(current.status)||serverAutoRunning.value||imageEditRunning.value)return
 const id=current.id,revision=current.revision,account=props.accountKey,epoch=navigationEpoch
 try{
  const result=await call('/'+id+'/images/check','POST',{revision,cached_only:true})
  if(request===cachedCoreReviewRequest&&currentScope(epoch,account)&&project.value?.id===id&&project.value?.revision===revision)
   coreImageReview.value={...result,accountKey:account}
 }catch{
  if(request===cachedCoreReviewRequest&&currentScope(epoch,account))cachedCoreReviewError.value='已有图片检查报告暂时未能读取，请刷新项目后重试。未新增检查或生成任务。'
 }
})
const failedImageRepairIndex=computed(()=>(project.value?.shots||[]).findIndex(shot=>shot.image_task?.action==='auto_repair'&&shot.image_task?.status==='failed'))
const localReeditBlock=computed(()=>{
 const shot=project.value?.shots?.find(row=>row.id===project.value?.reedit_shot_id)
 if(!shot)return '当前返修镜头不存在，请重新读取项目。'
 if(busy.value||imageEditRunning.value)return '请等待当前操作或图片任务结束后返回。'
 if(serverAutoRunning.value)return '请先暂停一键制作后编辑。'
 if(shot.image_status!=='completed')return '请先完成并检查本镜核心图。'
 if(shot.core_reference_error)return shot.core_reference_error+'；请局部更新本镜核心图。'
 if(shot.image_task?.action==='auto_repair'&&shot.image_task?.status==='failed')return '本镜辅助修图失败，请先核对该请求。'
 if(sceneReferencesEnabled.value&&shot.scene_reference_used_version){
  const scene=sceneAssetFor(shot)
  if(!scene||scene.image_status!=='completed'||scene.image_version!==shot.scene_reference_used_version)return '本镜关联场景尚未就绪或版本已变化，请先检查。'
 }
 if(shot.kind==='video'&&!shot.video_prompt?.trim())return '请先补齐本镜的视频提示词。'
 return ''
})
const imageConfirmationBlock=computed(()=>{
 if(project.value?.reedit_shot_id)return localReeditBlock.value
 const shots=project.value?.shots||[]
 const repairing=shots.map((shot,index)=>({shot,index})).filter(({shot})=>shot.image_task?.status==='running')
 if(repairing.length)return '正在修复第 '+repairing.map(({index})=>index+1).join('、')+' 镜核心图，暂不能确认。'+(serverAutoRunning.value?'修复后会自动复查，通过后继续制作，无需重复点击。':'请等待修图结束后再确认。')
 if(imageEditRunning.value)return '场景参考图正在处理，暂不能确认。完成后请核对关联镜头。'
 if(failedImageRepairIndex.value>=0)return '第 '+(failedImageRepairIndex.value+1)+' 镜辅助修图失败。已完成张数包含保留的原图，不表示修复通过。请先查看该镜的失败原因；原图和请求记录已保留。'
 if(serverAutoRunning.value)return '一键制作正在检查核心图；通过后会自动进入视频生成，无需手动确认。需要自行编辑时，请先点击“暂停后编辑”。'
 if(sceneReferencesBlockConfirm.value)return '场景参考尚未就绪，或有关联镜头仍使用旧版场景。请查看下方“场景参考资产”处理提示。'
 if(staleCoreShots.value.length)return '有 '+staleCoreShots.value.length+' 镜的连续性参考已变化。请使用“更新依赖变化的图片”或局部重绘，无关素材会保留。'
 if(shots.some(shot=>shot.image_status!=='completed'))return '还有核心图未生成成功，请处理失败或未完成的镜头后再确认。'
 if(busy.value)return '正在处理当前操作，请稍候再确认。'
 return ''
})
const imageAssetRunning=asset=>shotRenderLocked(asset)||asset?.image_task?.status==='running'||motionQueueState(asset)==='running'
function sceneReferenceIds(asset){return asset.redraw_selection?.reference_ids??asset.reference_ids??[]}
function toggleSceneMaterial(asset,id){
 const ids=sceneReferenceIds(asset)
 if(!ids.includes(id)&&ids.length>=3){error.value='场景图最多使用 3 张参考素材。';return}
 asset.redraw_selection={...(asset.redraw_selection||{}),reference_ids:ids.includes(id)?ids.filter(value=>value!==id):[...ids,id]}
}

function hydrateRedrawSelection(shot){
 if(!shot)return
 const saved=shot.redraw_selection
 const known=new Set(redrawReferenceGallery.value.map(asset=>String(asset.id)))
 const defaults=Array.isArray(saved?.reference_ids)?saved.reference_ids:(shot.reference_ids||shot.reference_image_ids||[])
 redrawReferenceIds.value=defaults.map(String).filter(id=>known.has(id)).slice(0,3)
 useCurrentReference.value=Boolean(saved?.use_current_image)
 useSceneReference.value=saved?.use_scene_reference!==false
 if(useCurrentReference.value&&redrawReferenceIds.value.length>2)redrawReferenceIds.value=redrawReferenceIds.value.slice(0,2)
}
function redrawReferenceNumber(id){return redrawReferenceIds.value.indexOf(id)+(useCurrentReference.value?2:1)}
function toggleRedrawReference(id){const values=redrawReferenceIds.value;if(values.includes(id))redrawReferenceIds.value=values.filter(value=>value!==id);else if(values.length<3-(useCurrentReference.value?1:0))redrawReferenceIds.value=[...values,id];else error.value='每次重绘最多使用 3 张参考图（包含当前画面）。'}
function toggleCurrentReference(){if(!useCurrentReference.value&&redrawReferenceIds.value.length>=3)redrawReferenceIds.value=redrawReferenceIds.value.slice(0,2);useCurrentReference.value=!useCurrentReference.value}
async function uploadRedrawReferences(event){
 const files=[...(event.target.files||[])].slice(0,Math.max(0,3-(useCurrentReference.value?1:0)-redrawReferenceIds.value.length));event.target.value=''
 const id=project.value.id,epoch=navigationEpoch,account=props.accountKey
 // Updating the first asset triggers child-panel reads. Keep one user action
 // around the batch so those reads cannot silently cancel the next upload.
 await run(async()=>{for(const file of files){
  if(!currentScope(epoch,account)||project.value?.id!==id)throw Object.assign(new Error('项目已切换'),{stale:true})
  const data=new FormData();data.append('file',file)
  const updated=await requestJSON(base+'/'+id+'/redraw-references',{method:'POST',body:data})
  if(!currentScope(epoch,account)||project.value?.id!==id)throw Object.assign(new Error('项目已切换'),{stale:true})
  acceptImageUpdate(updated)
  const latest=updated.redraw_references?.at(-1);if(latest)redrawReferenceIds.value=[...redrawReferenceIds.value,latest.id]
 }})
}
async function deleteRedrawReference(asset){
 if(asset.origin!=='uploaded'||!await confirmAction(`删除新增参考图“${asset.displayName}”？图片会从本项目素材库移除。`))return
 await run(async()=>{acceptImageUpdate(await requestJSON(base+'/'+project.value.id+'/redraw-references/'+encodeURIComponent(asset.id)+'?revision='+encodeURIComponent(project.value.revision),{method:'DELETE'}));redrawReferenceIds.value=redrawReferenceIds.value.filter(id=>id!==asset.id)})
}
async function clearUploadedRedrawReferences(){
 if(!uploadedRedrawReferences.value.length||!await confirmAction(`清空本项目后加的 ${uploadedRedrawReferences.value.length} 张参考图？创建任务时上传的原始素材不会删除。`))return
 await run(async()=>{acceptImageUpdate(await requestJSON(base+'/'+project.value.id+'/redraw-references?revision='+encodeURIComponent(project.value.revision),{method:'DELETE'}));const originals=new Set(projectRedrawReferences.value.map(asset=>asset.id));redrawReferenceIds.value=redrawReferenceIds.value.filter(id=>originals.has(id))})
}
async function redrawShot(shot){if(!requireAdoptedVideoInputs())return;if(!shot.image_prompt?.trim()){error.value='请先填写图片提示词。';return}await run(async()=>{rememberImagePrompt(shot);const sceneAsset=isSceneAsset(shot),submittedPrompt=shot.image_prompt;const updated=await call('/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'/redraw','POST',{revision:project.value.revision,prompt:submittedPrompt,reference_ids:redrawReferenceIds.value,use_current_image:useCurrentReference.value,use_scene_reference:sceneAsset?false:useSceneReference.value,image_resolution:redrawIcan.value?null:redrawResolutions.value.includes(redrawResolution.value)?redrawResolution.value:null,size:redrawIcan.value&&redrawResolution.value?imageSize(project.value.settings.ratio||'16:9',redrawResolution.value):null});submittedImagePrompts.value[shot.id]=submittedPrompt;acceptImageUpdate(updated);dirty.value=false;sessionStorage.removeItem(draftKey(project.value.id))})}
function openingRepairFor(shot){
 const report=shot.opening_image_repair
 return report&&report.image===shot.image&&report.image_sha256===shot.image_version&&report.image_prompt===shot.image_prompt&&report.action===shot.action&&report.opening===shot.visual_description?report:null
}
async function repairOpeningImage(shot){
 if(busy.value||dirty.value||imageEditRunning.value||serverAutoRunning.value||!openingRepairFor(shot))return
 const id=project.value.id,revision=project.value.revision,account=props.accountKey,epoch=navigationEpoch
 if(!await confirmAction('按已识别的偏差和已批准开场，只修复本镜核心图。会调用图片接口，保留原图、故事、台词、音色及其他镜头；不再调用语言模型分析。修图后仍需核对新图。继续？'))return
 if(!currentScope(epoch,account)||project.value?.id!==id||project.value?.revision!==revision||busy.value||dirty.value||imageEditRunning.value||serverAutoRunning.value)return
 await run(async()=>{acceptImageUpdate(await call('/'+id+'/images/'+encodeURIComponent(shot.id)+'/repair-opening','POST',{revision}))})
}
async function retryShotImage(shot){
 if(busy.value||imageEditRunning.value||shot.image_status!=='failed')return
 if(!shot.image_prompt?.trim()){error.value='请先填写本镜图片提示词。';return}
 if(!await confirmAction('将只为这一镜提交新的图片请求，保留原请求记录。原请求结果未确认时可能重复计费；请先核对服务端记录。继续生成新版本？'))return
 await run(async()=>{const prompt=shot.image_prompt;rememberImagePrompt(shot);const updated=await call('/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'/retry','POST',{revision:project.value.revision,prompt,confirm_new_attempt:true});submittedImagePrompts.value[shot.id]=prompt;acceptImageUpdate(updated)})
}
async function retryImageRepair(shot){
 if(busy.value||imageEditRunning.value)return
 if(!await confirmAction('按上次修图指令与参考图再提交一次？原请求结果未知时可能已计费。系统会校验参数，保留原图及回执；成功后仍须重新检查画面。'))return
 await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'/retry-repair','POST',{revision:project.value.revision,confirm_new_attempt:true}))})
}

async function replaceShotImage(event,shot){const file=event.target.files?.[0];event.target.value='';if(!file)return;await run(async()=>{const data=new FormData();data.append('revision',String(project.value.revision));data.append('prompt',shot.image_prompt||'');data.append('file',file);acceptImageUpdate(await requestJSON(base+'/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'/upload',{method:'POST',body:data}),[shot.id]);dirty.value=false;sessionStorage.removeItem(draftKey(project.value.id))})}
async function undoShot(shot){await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'/undo','POST',{revision:project.value.revision}),[shot.id]);dirty.value=false})}
async function resetShotPrompt(shot){await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/images/'+encodeURIComponent(shot.id)+'/reset-prompt','POST',{revision:project.value.revision}),[shot.id]);dirty.value=false})}
async function refreshShotPrompts(shot,basis){
 if(!requireAdoptedVideoInputs()||(nativeSpeech.value&&basis==='full'))return
 const label=basis==='full'?'根据本镜字幕和项目设定，重新设计动态表达、核心图提示词和视频提示词，覆盖本镜已有文字编辑；保留字幕与时长，不自动重绘图片或生成视频':basis==='action'?'根据当前动态表达，只更新本镜的核心图提示词和视频提示词':'根据当前核心图，只更新本镜的视频提示词'
 if(!await confirmAction(label+'，会调用语言 API，其他镜头不变。是否继续？'))return

 await run(async()=>{if(dirty.value)await save();const updated=await call('/'+project.value.id+'/shots/'+encodeURIComponent(shot.id)+'/refresh-prompts','POST',{revision:project.value.revision,basis,action:shot.action||'',image_prompt:shot.image_prompt||''});delete imagePromptDrafts.value[shot.id];delete submittedImagePrompts.value[shot.id];project.value=updated;dirty.value=false;sessionStorage.removeItem(draftKey(project.value.id))})
}
async function generateSceneReferences(){await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/scene-references/generate','POST',{revision:project.value.revision}))})}
async function restoreSceneBindings(){
 if(!await confirmAction('将保留现有画面，并恢复它们实际使用的归档场景。不会重绘，也不会把旧图标记成使用了新场景。请先核对这些画面仍符合故事。是否继续？'))return
 await run(async()=>{acceptImageUpdate(await call('/'+project.value.id+'/scene-references/restore-bindings','POST',{revision:project.value.revision}))})
}
async function stopPlanning(){await run(async()=>{project.value=await call('/'+project.value.id+'/stop','POST')})}
async function loadSrt(event){const file=event.target.files[0];if(file)input.value.srt=await file.text()}
function exportPlan(){const blob=new Blob([JSON.stringify(project.value,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=project.value.settings.name+'-分镜草案.json';a.click();URL.revokeObjectURL(url)}
const reviewRequest=computed(()=>{
 const record=project.value||{}, settings=record.settings||{}, saved=record.creation_parameters||{}
 return {...saved,story_handoff:record.story_handoff,dynamic_video:true,step_mode:true,
  ...(record.native_story?{script:record.native_story.text,native_duration_seconds:record.native_story.duration_seconds,native_minimum_shots:record.native_story.minimum_shots}:{}),
  dynamic_text_mode:normalizeDynamicTextMode(saved.dynamic_text_mode??settings.dynamic_text_mode),
  scene_references_enabled:sceneReferencesEnabled.value,
  project_name:settings.name||saved.project_name||'动态视频任务',
  visual_style_prompt:settings.style||saved.visual_style_prompt||'',
  global_character_prompt:settings.characters||saved.global_character_prompt||'',
  story_environment_prompt:settings.world||saved.story_environment_prompt||'',
  video_orientation:settings.ratio==='9:16'?'portrait':(saved.video_orientation||'landscape')}
})
const originalScript=computed(()=>String(project.value?.native_story?.text||reviewRequest.value.script||'').trim())
const finalTranscript=computed(()=>project.value?.scenes?.map(row=>row.text).filter(Boolean).join('\n')||'')
function openClassicEditor(){
 if(!requireAdoptedVideoInputs())return
 const form=JSON.parse(JSON.stringify(reviewRequest.value))
 emit('edit-config',{id:'rerun-'+(globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`),name:form.project_name,kind:'dynamic',form,
  subtitle:{project_name:form.project_name,source_audio_id:form.source_audio_id||'',reference_text:form.script||'',use_correction:true},
  engine:form.tts_engine==='indextts2'?'indextts25':form.tts_engine||'indextts25',rerun:true,
  source_project_id:project.value.id,source_project_revision:project.value.revision,
  rerun_base:JSON.parse(JSON.stringify(form)),rerun_stages:JSON.parse(JSON.stringify(stageSteps.value)),source_view:view.value})
}
function openStage(stage){if(!requireAdoptedVideoInputs())return;if(stage.key==='script'){if(nativeSpeech.value){view.value='audio';return}openClassicEditor();return}view.value=stage.key}
const stageSteps=computed(()=>[
 {key:'script',label:'文案',state:'done'},
 {key:'audio',label:nativeSpeech.value?'台词与音色':'配音与字幕',state:nativeSpeech.value?'review':'done'},
 {key:'storyboard',label:'分镜与动态制作',state:project.value?.status==='completed'?'done':'current'},

 {key:'export',label:'导出',state:project.value?.status==='completed'?'done':(allVideosReady.value||['exporting','export_failed'].includes(project.value?.status))?'current':'upcoming'},
 {key:'parameters',label:'参数回顾',state:'review'},
])
const shotEditingStage=computed(()=>['image_review',...videoStages].includes(project.value?.status)&&project.value?.status!=='exporting')
const shotRenderLocked=shot=>Boolean(shot?.video_queued||shot?.video_waiting||shot?.video_status==='running'||(shot?.video_request&&['unknown','stopped'].includes(shot?.video_status)&&!shot?.video_terminal&&!shot?.video_not_submitted)||(shot?.asset_kind==='scene_reference'&&videoRunning.value))
const storyboardLocked=computed(()=>['generation_ready','image_generating','image_stopping','image_review',...videoStages,'completed'].includes(project.value?.status))
const canEditStructure=computed(()=>project.value&&!nativeSpeech.value&&!videoInputsPreparing.value&&!videoInputDirty.value&&!projectCastDirty.value&&!busy.value&&!imageEditRunning.value&&!shotRenderLocked(selectedShot.value)&&!['planning','stopping','image_generating','image_stopping','video_stopping','exporting'].includes(project.value.status))
const structureFeedback=ref('')
function duplicateFromSnapshot(){
 const form=JSON.parse(JSON.stringify(reviewRequest.value))
 form.project_name=String(form.project_name||'动态视频任务').slice(0,65)+' · 副本'
 form.dynamic_video=true;form.step_mode=true
 const subtitle={project_name:form.project_name,source_audio_id:form.source_audio_id||'',reference_text:form.script||'',use_correction:true}
 emit('duplicate-config',{id:'draft-'+crypto.randomUUID(),form,subtitle,engine:form.tts_engine==='indextts2'?'indextts25':form.tts_engine||'indextts25'})
}
async function resetFromSnapshot(){
 if(!await confirmAction('重置后会保留当前任务参数，创建一份同名、尚未运行的新草稿。原项目及其成片不会删除。是否继续？'))return
 const form=JSON.parse(JSON.stringify(reviewRequest.value))
 form.project_name=String(form.project_name||'动态视频任务').slice(0,65)
 form.dynamic_video=true;form.step_mode=true
 const subtitle={project_name:form.project_name,source_audio_id:form.source_audio_id||'',reference_text:form.script||'',use_correction:true}
 emit('duplicate-config',{id:'draft-'+crypto.randomUUID(),form,subtitle,engine:form.tts_engine==='indextts2'?'indextts25':form.tts_engine||'indextts25',reset:true})
}
const autoAdvanceBusy=ref(false),resumedAutoAdvance=ref(null),autoAdvancePaused=ref(false)
let autoAdvanceKey=''
const autoAdvanceEnabled=computed(()=>project.value?.creation_parameters?.dynamic_execution_owner!=='server'&&!autoAdvancePaused.value&&project.value?.creation_parameters?.dynamic_auto_advance===true&&(!props.suppressAutoAdvance||
 (resumedAutoAdvance.value?.id===project.value?.id&&resumedAutoAdvance.value?.account===props.accountKey)))
const canResumeAutoAdvance=computed(()=>Boolean(project.value?.creation_parameters?.dynamic_execution_owner!=='server'&&project.value?.creation_parameters?.dynamic_auto_advance===true&&!autoAdvanceEnabled.value&&
 project.value.status!=='completed'&&!project.value.reedit_shot_id))
watch([()=>project.value?.id,()=>props.accountKey,()=>props.suppressAutoAdvance],()=>{resumedAutoAdvance.value=null;autoAdvancePaused.value=false;autoAdvanceKey=''},{flush:'sync'})
async function resumeAutoAdvance(){
 if(!canResumeAutoAdvance.value||busy.value||autoAdvanceBusy.value||!requireAdoptedVideoInputs())return
 if(dirty.value){error.value='当前镜头有未保存修改，请先保存后再继续一键制作。';return}
 resumedAutoAdvance.value={id:project.value.id,account:props.accountKey}
 autoAdvancePaused.value=false
 autoAdvanceKey='';error.value=''
 await advanceAutoPilot()
}
async function advanceAutoPilot(){
 const record=project.value
 if(disposed||!record||!autoAdvanceEnabled.value||busy.value||dirty.value||nativeRevisionBusy.value||nativeDialogueBusy.value||nativeDialogueDirty.value||projectVoiceBusy.value||videoInputBusy.value||videoInputDirty.value||videoPromptSyncPending.value||projectCastBusy.value||projectCastDirty.value||autoAdvanceBusy.value||record.error)return
 if(record.reedit_shot_id)return
 const status=String(record.status||'')
 if(['planning','stopping','image_generating','image_stopping','video_generating','video_stopping','exporting','completed'].includes(status))return
 if(status==='image_review'){
  if(record.shots.some(shot=>shot.image_status==='failed')){error.value='一键制作已暂停：存在生成失败的核心分镜图，请重试或替换后继续。';view.value='storyboard';return}
  if(record.shots.some(shot=>shot.image_status!=='completed')||staleCoreShots.value.length||sceneReferencesBlockConfirm.value||imageEditRunning.value)return
 }
 if(['video_generation_ready','video_review'].includes(status)&&(
  record.shots.some(shot=>shot.image_status!=='completed'||(shot.kind==='video'&&!shot.video_prompt?.trim()))||
  failedImageRepairIndex.value>=0||staleCoreShots.value.length||sceneReferencesBlockConfirm.value||imageEditRunning.value)){
  error.value='一键制作已暂停：整片仍有核心图、场景或提示词需要检查；本镜返修已保留。';view.value='storyboard';return
 }
 if(status==='video_generation_ready'&&dynamicShots.value.some(shot=>!shotConfigured(shot))){error.value='一键制作已暂停：请检查各镜头的工作流或视频 API 配置。';view.value='motions';return}
 if(status==='video_review'&&dynamicShots.value.some(shot=>shot.video_status!=='completed')){error.value='一键制作已暂停：部分动态镜头尚未完成，请在动态镜头页检查后继续。';view.value='motions';return}
 const key=`${record.id}:${record.revision}:${status}`
 if(autoAdvanceKey===key)return
 autoAdvanceKey=key;autoAdvanceBusy.value=true
 try{
  if(status==='draft'&&!record.shots.length){view.value='storyboard';await plan(true)}
  else if(status==='storyboard_review'&&record.shots.length){view.value='storyboard';await review()}
  else if(status==='generation_ready'){view.value='storyboard';await generateImages()}
  else if(status==='image_review'){view.value='storyboard';await confirmImages()}
  else if(status==='video_generation_ready'){if(allVideosReady.value){view.value='export';await exportVideo(true)}else{view.value='motions';await generateVideos(dynamicShots.value,false,true)}}
  else if(status==='video_review'&&allVideosReady.value){view.value='export';await exportVideo(true)}
 }finally{autoAdvanceBusy.value=false}
}
watch(()=>[project.value?.status,project.value?.revision,videoConfigured.value,comfyProfiles.value.length],()=>setTimeout(advanceAutoPilot,0))
watch([projectCastBusy,projectCastDirty],()=>{if(!projectCastBusy.value&&!projectCastDirty.value)setTimeout(advanceAutoPilot,0)})

onMounted(()=>{
 run(async()=>{
  const id=sessionStorage.getItem('ocv-video-open')
  if(id){
   project.value=await call('/'+id)
   selected.value=restoredShotIndex(project.value)
   const requestedView=sessionStorage.getItem('ocv-video-view')
   view.value=requestedView&&stageSteps.value.some(stage=>stage.key===requestedView)?requestedView:defaultProjectView(project.value)
   sessionStorage.removeItem('ocv-video-view')
   sessionStorage.removeItem('ocv-video-open')
   const autoPlan=sessionStorage.getItem('ocv-video-autoplan')===id
   if(autoPlan)sessionStorage.removeItem('ocv-video-autoplan')
   // Consume the navigation intent before submission. Reopening or refreshing
   // a project must never trigger another paid planning request.
   if(autoPlan&&!props.suppressAutoAdvance&&project.value.status==='draft'&&!project.value.shots.length){
    project.value=await call('/'+id+'/plan','POST')
   }
  }
  await list()
  if(!project.value)sources.value=(await call('/sources')).items
 })
 loadVideoModel().then(()=>advanceAutoPilot())
 loadComfyProfiles()
 timer=setInterval(createVideoProjectPoller({
  canRead:()=>Boolean(project.value?.id)&&!busy.value&&!dirty.value&&!videoInputDirty.value&&!projectCastDirty.value,
  readIdentity:()=>({id:project.value.id,epoch:navigationEpoch,account:props.accountKey}),
  load:identity=>call('/'+identity.id),
  isCurrent:identity=>currentScope(identity.epoch,identity.account)&&project.value?.id===identity.id,
  apply:value=>{acceptImageUpdate(value);pollingError.value=''},
  onError:e=>{pollingError.value=e.status===401?'登录状态需要更新，请打开登录与配置重新登录。':
   '暂时无法同步项目状态'+(Number.isInteger(e.status)?'（HTTP '+e.status+'）':'（网络连接异常）')+'。恢复连接后自动更新，不会因此重复提交制作请求。'}
 }),2500)

})
watch([selected,view,productionPanel],()=>stopMotionPreviewAudio())
watch(previewNarration,enabled=>{if(!enabled)stopMotionPreviewAudio()})
watch([videoInputBusy,videoInputDirty,videoPromptSyncPending],values=>{if(values.every(value=>!value))setTimeout(advanceAutoPilot,0)})
watch(()=>props.accountKey,()=>{++navigationEpoch;project.value=null;items.value=[];dirty.value=false;busy.value=false;videoInputBusy.value=false;videoInputDirty.value=false;videoPromptSyncPending.value=false})
onUnmounted(()=>{disposed=true;++navigationEpoch;clearInterval(timer);stopBoundaryPreview();stopMotionPreviewAudio()})
</script>
<template>
<section class="content video-workspace">
 <p v-if="pollingError" class="studio-notice project-sync-notice" role="status">{{pollingError}}</p>
 <div v-if="project" class="workspace-project-heading"><div><p class="eyebrow">动态视频工作台</p><h1>{{project.settings.name}}</h1></div><div class="workspace-project-actions"><span v-if="dirty" class="workspace-draft" role="status">有未保存修改</span><button class="ghost-btn" :disabled="busy||videoInputsPreparing" @click="backToProjects">← 返回项目</button><button class="ghost-btn" :disabled="busy||videoInputsPreparing" @click="openClassicEditor">修改画面设置</button><button class="ghost-btn" @click="exportPlan">导出分镜草案</button><button class="ghost-btn" @click="newVideoProject">＋ 新建项目</button></div></div>
 <nav v-if="project" class="video-stage-nav" aria-label="动态视频任务阶段">
  <button v-for="(stage,index) in stageSteps" :key="stage.key" type="button" :aria-current="view===stage.key?'step':undefined" :disabled="videoInputsPreparing" :class="[stage.state,{active:view===stage.key}]" @click="openStage(stage)">
   <span v-if="stage.state==='done'">✓</span><span v-else-if="stage.state==='upcoming'">{{index+1}}</span><span v-else>•</span>
   <b>{{stage.label}}</b><small>{{stage.state==='done'?'已完成':stage.state==='current'?'当前阶段':stage.state==='upcoming'?'尚未开始':'只读查看'}}</small>
  </button>
 </nav>
 <StorySourcePanel v-if="project" :handoff="project.story_handoff" />
 <ProductionRecovery v-if="project&&recovery" :recovery="recovery" :disabled="busy||videoRunning||serverAutoRunning||imageEditRunning" @action="handleRecovery" />
 <details v-if="project?.story_review_advisories?.length" class="panel">
  <summary>故事建议（{{project.story_review_advisories.length}} 条，不阻断制作）</summary>
  <p>这些是原稿或叙事表达建议。保持你采用的故事，不要求反复规划；需要调整时再主动修改故事。</p>
  <article v-for="(note,index) in project.story_review_advisories" :key="index">
   <p>{{note.shot_ids.join('、')}}：{{note.evidence}}</p><p>建议：{{note.repair}}</p>
  </article>
 </details>
 <OneClickStatus v-if="project?.creation_parameters?.dynamic_execution_owner==='server'" :key="String(accountKey)+project.id" :job-id="project.native_story?project.id:project.source_project.id" :disabled="busy||dirty||nativeDialogueBusy||nativeDialogueDirty||projectVoiceBusy||projectCastBusy||projectCastDirty||videoInputBusy||videoInputDirty" @state="serverAutoRunning=$event.state==='running'" />
 <AutoCastNotice v-if="project" :key="project.id" :project="project" @recovered="project=$event;view='storyboard'" />
 <ProjectCastPanel v-if="project" :key="project.id" :editor="projectCast" />
 <ProjectVoicePanel v-if="project&&nativeSpeech" :key="accountKey+':'+project.id" :project="project" :account-key="accountKey" :disabled="busy||dirty||projectCastBusy||projectCastDirty||serverAutoRunning" @busy="projectVoiceBusy=$event" @updated="acceptVoiceUpdate" />
 <div class="page-heading workspace-stage-heading"><div><h2>{{project?workspaceTitles[view]||'动态视频':'动态视频'}}</h2><p class="muted">{{project?workspaceHints[view]:'从文案开始制作动态视频，或继续已有任务。'}}</p></div><span v-if="project?.shots.length" class="readonly-badge">{{project.shots.length}} 镜 · {{project.settings.ratio||'16:9'}}</span></div>
 <div v-if="project&&autoAdvanceEnabled&&project.status!=='completed'" class="auto-pilot-status" role="status"><span class="auto-pilot-dot"></span><div><strong>{{autoAdvanceBusy?'一键制作正在进入下一阶段':'一键制作已开启'}}</strong><p>成功的确认步骤会自动继续；失败或需要人工处理时会停留在对应阶段。</p></div></div>
 <div v-else-if="canResumeAutoAdvance" class="auto-pilot-status"><div><strong>继续这个项目的一键制作</strong><p>按已保存的设置处理剩余阶段，可能调用生成服务；失败或需要确认时暂停。</p></div><button type="button" class="primary-btn" :disabled="busy||autoAdvanceBusy||dirty||videoInputBusy||videoInputDirty||videoPromptSyncPending||projectCastBusy||projectCastDirty" @click="resumeAutoAdvance">继续一键制作</button></div>
 <p v-if="error" class="studio-notice error">{{error}}</p>
 <section v-if="project&&pendingAudioShots.length" class="single-shot-refresh" role="status">
  <div><strong>配音已更新 · {{pendingAudioShots.length}} 镜待检查</strong><p>时间轴已同步，原画面保留。请试看裁切或降速后的效果；不满意时只重新生成对应镜头。</p></div>
  <button @click="nextAudioReview">下一个待检查</button>
 </section>
 <section v-if="selectedShot?.audio_timing_adjustment?.needs_review&&['storyboard','motions'].includes(view)" class="single-shot-refresh">
  <div><strong>第 {{String(selected+1).padStart(2,'0')}} 镜配音发生变化，请检查</strong><p>{{selectedShot.audio_timing_adjustment.previous_duration}} 秒 → {{selectedShot.duration}} 秒。检查动作与叙事是否完整，再决定沿用或重新生成。</p><p v-if="selectedShot.audio_timing_adjustment.text_changed">字幕文字也已修改，请同时核对画面含义。</p></div>
  <button :disabled="!videoStageReady" @click="view='motions'">试看本镜</button>
  <button :disabled="busy" @click="confirmAudioReview(selectedShot)">确认沿用</button>
 </section>
 <LanguageModelPresets v-if="project&&view==='storyboard'" :disabled="busy||!canEditStructure" :cloud-pool="Boolean(project.creation_parameters?.use_cloud_image_pool)">
  <template #actions="{unavailable,pendingSwitch}">
   <div class="replan-description"><strong>重新规划画面</strong><small class="muted">保留配音与字幕时间轴。确认新方案后再生成素材，不自动重做。</small><small v-if="pendingSwitch" class="muted">请先点击“使用此模型”，再重新规划。</small>
    <details class="replan-options"><summary>规划选项 · {{ dynamicTextModeLabel(replanMode) }} · {{ replanRegroup?'重新划分镜头':'保留镜头边界' }}</summary>
     <fieldset :disabled="busy||!canEditStructure||unavailable"><DynamicTextModeSelector v-model="replanMode" />
      <label class="replan-regroup"><input type="checkbox" v-model="replanRegroup">同时重新划分镜头</label>
      <small class="muted">仅重规划时生效。勾选后不保留手动分组，按完整语义重新选择边界和动静类型；配音、字幕不变。未勾选时，如需把动态循环改为静态，可单独修改镜头类型。</small>
     </fieldset>
    </details>
   </div>
   <button type="button" :disabled="busy||!canEditStructure||unavailable" @click="replanWithCurrentModel">重新规划</button>
  </template>
 </LanguageModelPresets>
 <OperationStatus v-if="project&&(busy||(productionPanel!=='video'&&videoRunning)||['planning','stopping','image_generating','image_stopping','exporting'].includes(project.status))" title="项目运行状态" :pending="busy" :task="{status:'running',message:project.status==='exporting'?'正在合成成片':storyboardStageLabel}" :summary="videoRunning?videoBatchSummary:''"/>
 <template v-if="!project&&!busy">
 <div class="video-start">
  <section class="video-card"><h2>新建视频任务</h2><p class="muted">可借用已有作品的配音、字幕和时间戳。画面从头设计，不导入旧图、参考图、画风、人物或旧分镜。</p><label>配音与字幕来源<select v-model="sourceId" @change="chooseSource"><option value="">请选择已完成的项目</option><option v-for="source in sources" :key="source.id" :value="source.id">{{source.name}}</option></select></label><p v-if="!sources.length" class="muted">暂无可导入项目，需要项目已完成，且配音与最终字幕文件完整。</p><label>新项目名称<input v-model="importName" maxlength="100" placeholder="自动填写，可修改"></label><label>新视频比例<select v-model="newVisual.ratio"><option value="16:9">横屏 16:9</option><option value="9:16">竖屏 9:16</option></select></label>
   <DynamicTextModeSelector v-model="newVisual.dynamic_text_mode" />
   <label class="dynamic-scene-reference-toggle"><input type="checkbox" v-model="newVisual.scene_references_enabled">启用场景参考</label><p class="muted">所有表达方式均可使用；为重复场景额外生成参考图并计费，关闭后不生成。</p>
   <details open><summary>新任务画面设定（不继承来源项目）</summary><label>统一画风<textarea v-model="newVisual.style" rows="2"/></label><label>人物设定<textarea v-model="newVisual.characters" rows="2" placeholder="例如：主讲人是红色身体、戴红围巾的火柴人"/></label><label>世界与场景<textarea v-model="newVisual.world" rows="2"/></label></details><button class="primary-btn" :disabled="busy||!sourceId" @click="importProject">新建任务并导入配音字幕</button><p class="muted">独立保存，不影响原作品。直接从文案／音频开始的完整入口将在后续接入。</p></section>
  <section class="video-card"><h2>继续编辑</h2><p class="muted" v-if="!items.length">还没有视频分镜草案。</p><button class="video-record" v-for="item in items" :key="item.id" @click="open(item.id)"><strong>{{item.settings.name}}</strong><span>{{item.scenes.length}} 条字幕 · {{item.shots.length}} 个镜头</span></button></section>
 </div>
 <details class="video-srt-import"><summary>单独导入 SRT（测试入口，不包含配音）</summary>
 <div class="video-start">
  <section class="video-card"><h2>新建分镜草案</h2><label>项目名称<input v-model="input.name" maxlength="100"></label><label>视频比例<select v-model="input.ratio"><option value="16:9">横屏 16:9</option><option value="9:16">竖屏 9:16</option></select></label>
   <DynamicTextModeSelector v-model="input.dynamic_text_mode" />
   <label class="dynamic-scene-reference-toggle"><input type="checkbox" v-model="input.scene_references_enabled">启用场景参考</label><p class="muted">所有表达方式均可使用；为重复场景额外生成参考图并计费，关闭后不生成。</p>
   <label>导入已校对字幕<input type="file" accept=".srt" @change="loadSrt"></label><textarea v-model="input.srt" rows="4" placeholder="粘贴 SRT（保留时间戳），或选择字幕文件"/><details><summary>画风与人物设定</summary><label>统一画风<textarea v-model="input.style" rows="2"/></label><label>人物设定<textarea v-model="input.characters" rows="2"/></label><label>世界与场景<textarea v-model="input.world" rows="2"/></label></details><button class="primary-btn" :disabled="busy||!input.srt" @click="create">创建草案</button></section>
 </div>
 </details>
 </template>
 <section v-if="project&&view==='audio'&&nativeSpeech" class="video-card video-history-panel">
  <h2>故事与对话</h2><p class="muted">角色音色在上方选择。{{externalDialogue?'对白时间轴使用实测 TTS 音频位置；请核对台词、说话角色与嘴部动作。':'这里的时间是分镜计划，最终字幕会按视频实际语音检查结果定位。'}}</p>
  <details><summary>原始故事</summary><p style="white-space:pre-wrap">{{originalScript}}</p></details>
  <div v-for="(shot,index) in project.shots" :key="shot.id"><h3>第 {{index+1}} 镜</h3><p>{{shot.action}}</p>
    <p v-for="(turn,ti) in shot.native_dialogue?.turns||[]" :key="ti"><b>{{shot.native_dialogue.speakers.find(s=>s.character_id===turn.character_id)?.name}}</b>（{{turn.start}}–{{turn.end}} 秒）：{{turn.text}}</p>
  </div>
 </section>
 <section v-if="project&&view==='audio'&&!nativeSpeech" class="video-card video-history-panel">
  <button v-if="project.source_project?.id" :disabled="busy||videoRunning||exportRunning" @click="syncAudioTimeline">校验并同步配音时间轴</button>
  <button v-if="project.source_project?.id" :disabled="busy||videoRunning||exportRunning" @click="emit('edit-audio',project)">编辑配音与字幕</button>
  <div class="history-heading"><div><p class="eyebrow">阶段 2 · 已完成</p><h2>配音与字幕</h2><p class="muted">这里播放的是本动态任务采用的配音快照；参数均来自创建任务时保存的记录。</p></div><span class="readonly-badge">只读</span></div>
  <audio v-if="project.audio" :key="narrationUrl" controls preload="metadata" :src="narrationUrl"/>
  <p v-else class="studio-notice">这个测试草案没有保存配音文件。</p>
  <div class="readonly-summary-grid"><label>配音执行方<input readonly :value="reviewRequest.tts_engine||'该任务未记录'"/></label><label>音色<input readonly :value="reviewRequest.cluster_voice_id||reviewRequest.qwen_tts_voice||reviewRequest.tts_voice_id||'该任务未记录'"/></label><label>情绪<input readonly :value="reviewRequest.tts_emotion||'参考原音频'"/></label><label>语速<input readonly :value="reviewRequest.tts_speed??'该任务未记录'"/></label></div>
  <details open><summary>最终字幕与时间戳 · 只读</summary><div class="subtitle-review-list"><p v-for="scene in project.scenes" :key="scene.slide_id"><time>{{scene.start.toFixed(1)}}～{{scene.end.toFixed(1)}}s</time><span>{{scene.text}}</span></p></div></details>
 </section>
 <ShotCastPlan v-if="project&&selectedShot&&['storyboard','motions'].includes(view)" :plan="selectedShot.cast_plan" :project-id="project.id" :characters="projectCastSnapshot?.characters||[]" />
 <NativeProjectRevision v-if="project&&nativeSpeech&&['storyboard','motions'].includes(view)" :key="accountKey+':'+project.id" :project="project" :shot="selectedShot" :account-key="accountKey"
  :disabled="busy||dirty||nativeDialogueBusy||nativeDialogueDirty||serverAutoRunning||videoRunning||imageEditRunning||exportRunning||projectVoiceBusy||projectCastBusy||projectCastDirty||videoInputBusy||videoInputDirty||['planning','stopping','image_generating','image_stopping'].includes(project.status)"
  @updated="autoAdvancePaused=true;acceptImageUpdate($event)" @busy="nativeRevisionBusy=$event" />
 <DialogueEditor v-if="project&&nativeSpeech&&selectedShot?.native_dialogue&&['storyboard','motions'].includes(view)" :key="accountKey+':'+project.id+':'+selectedShot.id" :project="project" :shot="selectedShot" :account-key="accountKey"
  :disabled="busy||dirty||nativeRevisionBusy||serverAutoRunning||videoRunning||imageEditRunning||exportRunning||projectVoiceBusy||projectCastBusy||projectCastDirty||videoInputBusy||videoInputDirty"
  @updated="acceptImageUpdate($event)" @busy="nativeDialogueBusy=$event" @dirty="nativeDialogueDirty=$event" />
 <p v-if="dirty&&localVideo&&selectedShot?.kind==='video'&&['storyboard','motions'].includes(view)" class="studio-notice">请先保存下方的动态表达与视频提示词，再采用视频输入或同步正文。</p>
 <VideoInputPanel v-if="project&&localVideo&&selectedShot?.kind==='video'&&selectedShot?.image_status==='completed'&&['storyboard','motions'].includes(view)&&['image_review',...videoStages].includes(project.status)"
  :project="project" :shot="selectedShot" :account-key="accountKey" :project-cast="projectCastSnapshot" :disabled="busy||dirty||projectCastBusy||projectCastDirty||videoRunning||imageEditRunning||exportRunning"
  @updated="acceptImageUpdate($event)" @busy-change="videoInputBusy=$event" @dirty-change="videoInputDirty=$event" @prompt-sync-pending="videoPromptSyncPending=$event" />

 <section v-if="project&&view==='export'" class="video-card video-history-panel">
  <label v-if="allVideosReady&&!exportRunning" class="motion-field">片尾文字（可选）<input v-model="endTitleText" aria-label="片尾文字" maxlength="80" placeholder="留空不添加片尾文字"><small>在最后镜头末尾1.5秒显示黑底片名，总时长不变，不重新生成视频。仅在故事需要文字结尾时填写。</small></label>
  <p v-if="nativeSpeech" class="muted">{{externalDialogue?'角色对白使用所选 TTS 引擎配音，片段按实测音频位置对齐；旁白独立加入，不传入口型约束。':'对白按 H3 原生音轨处理；旁白按独立路线设置处理。'}}</p>
  <label v-else class="export-narration-volume" style="display:grid;gap:8px;margin:14px 0">旁白音量 {{narrationVolume}}%<input type="range" min="0" max="100" step="5" v-model.number="narrationVolume" :disabled="busy||exportRunning" aria-label="旁白音量"><small>只调整成片中的旁白，保留原始配音；50% 约降低 6 dB。调整后需重新合成。</small></label>
  <div class="history-heading"><div><p class="eyebrow">阶段 5</p><h2>合成与导出</h2><p class="muted">{{nativeSpeech?(externalDialogue?'按实测 TTS 音频位置对齐片段与字幕。':'按实际片段长度拼接，保留原生对白与环境声。'):'动态片段按字幕时长裁切，静态镜头自动补帧；旁白和字幕沿用本任务确认后的时间轴。'}}</p></div><span class="readonly-badge">{{project.status==='completed'?'已完成':exportRunning?'正在合成':project.status==='export_failed'?'需要重试':'等待合成'}}</span></div>
  <p v-if="project.error" class="studio-notice error">{{project.error}}</p>
  <details class="export-layout-panel" v-if="project.shots.length"><summary>调整成片布局与字幕 <span class="muted">{{exportLayoutDirty?'有修改 · 合成时保存':'拖动画面、缩放、调整字幕位置'}}</span></summary>
   <div class="export-layout-content"><div class="export-layout-context"><label>选择预览分镜<select v-model="selected"><option v-for="(shot,index) in project.shots" :key="shot.id" :value="index">第 {{String(index+1).padStart(2,'0')}} 镜 · {{shot.summary||shot.title||shot.id}}</option></select></label><p class="muted">仅切换预览素材，布局统一用于全部镜头。调整后点击“按当前选项重新合成”或“开始合成”保存并生效，不会重新出图或生成视频。</p><label class="export-audio-option"><input type="checkbox" :checked="exportLayoutVariant!=='raw'" @change="exportLayoutVariant=$event.target.checked?'both':'raw'"><span>预览显示字幕（不改变输出版本）</span></label></div>
   <SubtitleStyleEditor :settings="exportLayoutSettings" :variant="exportLayoutVariant" dynamic hide-editions :readonly="exportRunning||busy" :preview-image="selectedShot?.image?imageUrl(selectedShot):''" :preview-shot-id="selectedShot?.id" :preview-url="base+'/'+project.id+'/layout-preview'" />
   </div>
  </details>
  <template v-if="['completed','export_failed'].includes(project.status)&&project.export">
   <p class="studio-notice" role="status">{{project.status==='export_failed'?'本次导出失败，以下保留上一次导出，可继续预览和下载。':'文件已生成。'}}{{exportReviewNotice}}</p>

   <div class="export-preview-switch" role="group" aria-label="选择成片预览版本"><button :class="{active:exportPreview==='raw'}" :aria-pressed="exportPreview==='raw'" @click="exportPreview='raw'">纯净版 · 无字幕</button><button :class="{active:exportPreview==='subtitles'}" :aria-pressed="exportPreview==='subtitles'" @click="exportPreview='subtitles'">字幕版</button><span class="muted">仅切换预览，不会重新合成</span></div>
   <video :key="exportPreview" controls playsinline preload="metadata" :src="exportUrl(exportPreview)"></video>
   <div class="export-actions"><a class="primary-btn" :href="exportUrl('subtitles')" download>下载字幕版</a><a class="ghost-btn" :href="exportUrl('raw')" download>下载纯净版</a><button type="button" class="ghost-btn" @click="openExportFolder">打开输出目录</button></div>
   <label v-if="!nativeSpeech" class="export-audio-option"><input type="checkbox" v-model="useVideoAudio"><span><strong>保留片段原声</strong><small>环境声、动作声及模型可能生成的配乐会一起与旁白混合；关闭会去掉整条原声音轨。</small></span></label>
   <button type="button" class="ghost-btn" :disabled="busy" @click="exportVideo">按当前选项重新合成</button>
   <p class="muted">{{nativeSpeech?'输出目录包含 SRT、动态分镜和声音与字幕说明。':'输出目录同时包含最终字幕 SRT、配音快照和动态分镜方案。'}}</p>
  </template>
  <template v-else-if="exportRunning"><p class="studio-notice">正在本地合成，请保持 OCV 运行。完成后本页会自动显示预览，无需刷新整个页面。</p><details v-if="project.logs?.length" open class="planning-log"><summary>合成日志</summary><div class="video-logs"><div v-for="(line,index) in project.logs" :key="index">{{line}}</div></div></details></template>
  <template v-else-if="allVideosReady"><label v-if="!nativeSpeech" class="export-audio-option"><input type="checkbox" v-model="useVideoAudio"><span><strong>保留片段原声</strong><small>环境声、动作声及模型可能生成的配乐会一起与旁白混合；关闭会去掉整条原声音轨。</small></span></label><button type="button" class="primary-btn" :disabled="busy" @click="exportVideo">{{project.status==='export_failed'||project.status==='completed'?'重新合成':'开始合成字幕版与纯净版'}}</button><p class="muted">该步骤只在本地运行，不调用图像或视频 API，不产生接口费用；可以修改选项后重新合成。</p></template>
  <template v-else><p class="studio-notice">请先完成全部动态镜头。</p><button v-if="videoStageReady" type="button" class="primary-btn" @click="view='motions'">返回动态镜头</button></template>
 </section>
 <ParameterReview v-if="project&&view==='parameters'" :request="reviewRequest" :reference-assets="[]" :status="project.status" :busy="busy" @duplicate="duplicateFromSnapshot" @reset="resetFromSnapshot" />
  <section v-if="project&&view==='storyboard'" class="video-card video-project">
  <p v-if="project.status==='planning'" class="studio-notice" role="status" aria-live="polite">{{project.native_story?'正在根据故事正文规划角色、逐句台词和镜头动作。':'正在根据配音和字幕规划动静分镜。'}}下方会显示进度；完成后自动展开镜头列表。</p>
  <p v-else-if="!project.shots.length&&(project.error||error)" class="studio-notice">{{planningCanResume?'已有规划进度已保存，点击“继续未完成规划”即可接着处理（会调用 API）。':'分镜规划尚未完成，可以点击“重试规划”重新运行（会调用 API）。'}}</p>
  <p v-if="project.planning_recovery" class="studio-notice" role="status">{{project.planning_recovery}}</p>
  <div v-if="recoverableSettingsImages" class="studio-notice"><p>历史记录中有已生成的图片，可先核对并恢复关联，无需重新出图。</p><button type="button" :disabled="busy||dirty||serverAutoRunning" @click="restoreSettingsImages">恢复已有图片</button></div>
  <div v-if="project.source_project" class="video-source-summary"><p class="muted">配音字幕来源：{{project.source_project.name}} · {{project.import_scope==='audio_subtitles'?'仅导入配音、字幕和时间戳，画面重新规划':'旧版导入草案（保留原设置）；如需全新画面设计，请重新创建任务'}}</p><audio v-if="project.audio" ref="sourceAudio" :key="narrationUrl" controls preload="metadata" :src="narrationUrl"/></div>
  <header><div><h2>{{project.settings.name}}</h2><p class="muted">{{storyboardStageLabel}} · {{project.native_story?'按故事动作和台词时序分镜':'所有切分都遵循字幕边界'}}</p></div><div class="video-header-actions"><button v-if="['draft','storyboard_review'].includes(project.status)" class="primary-btn" :disabled="busy||!project.shots.length" @click="review">确认分镜方案并生成核心图</button><button v-if="['planning','stopping'].includes(project.status)" class="primary-btn" :disabled="busy||project.status==='stopping'" @click="stopPlanning">{{project.status==='stopping'?'正在停止…':'停止规划'}}</button><button v-else-if="project.status==='generation_ready'" class="primary-btn" :disabled="busy" @click="generateImages">开始生成核心分镜图</button><button v-else-if="['image_generating','image_stopping'].includes(project.status)" class="primary-btn" :disabled="busy||project.status==='image_stopping'" @click="stopImages">{{project.status==='image_stopping'?'正在停止…':'停止生成'}}</button><button v-else-if="project.status==='image_review'" :disabled="busy||imageEditRunning||!imageRetryNeeded" @click="generateImages">{{staleCoreShots.length||staleSceneShots.length?'更新依赖变化的图片':'重试未完成图片'}}</button><button v-else-if="!storyboardLocked" :disabled="busy" @click="plan">{{planningButtonLabel}}</button><button v-if="project.status==='image_review'" type="button" :disabled="busy||imageEditRunning" @click="plan">重新规划分镜</button></div></header>
  <section v-if="project.native_story&&['image_review','video_review','video_generation_ready'].includes(project.status)" class="video-card core-image-review">
   <strong>画面检查与局部修复</strong>
   <p class="muted">检查已完成图片及相邻画面的连续性；未生成的镜头不算通过。手动检查不自动重绘；一键制作可按明确报告为每镜局部修图一次，仍有问题会暂停。辅助检查不能保证成片质量。</p>
   <button type="button" :disabled="busy||imageEditRunning||serverAutoRunning" @click="checkCoreImages">检查已完成核心图</button>
   <button v-if="project.status==='image_review'" type="button" :disabled="busy||dirty||imageEditRunning||serverAutoRunning" @click="checkDynamicOpenings">检查动态开场（不生成视频）</button>
   <div v-if="project.shots.some(s=>s.opening_preflight)" role="status">
    <p>动态开场沿用视频提交规则，输入不变时复用已通过结果。以下为上次检查记录，不代表成片验收通过。</p>
    <p v-for="shot in project.shots.filter(s=>s.opening_preflight)" :key="shot.id">第 {{project.shots.findIndex(s=>s.id===shot.id)+1}} 镜：{{shot.opening_preflight.message}}</p>
   </div>
   <p v-if="cachedCoreReviewError" class="muted">{{cachedCoreReviewError}}</p>
   <div v-if="currentCoreReview" role="status">
    <p>已检查 {{currentCoreReview.reviewed_ids.length}} / {{project.shots.length}} 镜；未检查 {{currentCoreReview.unchecked_ids.length}} 镜。</p>
    <p v-if="!coreReviewIssues.length&&!coreReviewUncertainties.length">本次检查未发现明确问题；请继续核对实际画面。</p>
    <article v-for="(issue,index) in coreReviewIssues" :key="index">
     <strong>第 {{project.shots.findIndex(s=>s.id===issue.shot_id)+1}} 镜</strong>
     <p>{{issue.evidence}}</p><p>修正目标：{{issue.correction}}</p>
     <button type="button" :disabled="busy||dirty||imageEditRunning||serverAutoRunning||!issue.correction" @click="repairCoreIssue(issue.shot_id)">按报告修复本镜</button>
    </article>
    <p v-for="(note,index) in coreReviewUncertainties" :key="'uncertain-'+index">待核实：{{note}}</p>
   </div>
  </section>
  <div v-if="['image_generating','image_stopping','image_review',...videoStages].includes(project.status)" class="video-next-stage" role="status">
   <div><strong>{{project.status==='image_review'?(imageEditRunning?'核心分镜图正在修复':failedImageRepairIndex>=0?'核心分镜图修复未完成':serverAutoRunning?'核心分镜图正在自动检查':'核心分镜图等待确认'):videoStageReady?'核心分镜图已确认':project.status==='image_stopping'?'正在安全停止':'正在生成核心分镜图'}}</strong><p>已完成 {{project.shots.filter(s=>s.image_status==='completed').length}} / {{project.shots.length}} 张<span v-if="project.shots.some(s=>s.image_status==='failed')">，失败 {{project.shots.filter(s=>s.image_status==='failed').length}} 张</span>。{{imageEditRunning?'已有图片正在修复，张数齐全不代表修复已结束。':'图片确认后，只为动态镜头生成视频。'}}</p><p v-if="project.status==='image_review'&&imageConfirmationBlock" id="image-confirmation-reason" class="image-confirmation-reason">{{imageConfirmationBlock}}</p></div>
   <button v-if="project.status==='image_review'" class="primary-btn" :disabled="Boolean(imageConfirmationBlock)" :aria-describedby="imageConfirmationBlock?'image-confirmation-reason':undefined" @click="confirmImages()">{{project.reedit_shot_id?'完成返修，返回动态镜头':imageEditRunning?'正在修复核心图…':failedImageRepairIndex>=0?'请先处理修图失败':serverAutoRunning?'正在自动检查核心图…':'确认核心分镜图，进入动态视频生成'}}</button>
   <button v-if="project.status==='image_review'&&failedImageRepairIndex>=0" type="button" class="ghost-btn" @click="selectVideoShot(failedImageRepairIndex)">查看第 {{failedImageRepairIndex+1}} 镜修图失败</button>

   <button v-else-if="videoStageReady" type="button" class="primary-btn" @click="view='motions'">{{completedVideos?'查看动态镜头':'进入动态镜头生成'}}</button>
   <span v-else>请在下方查看实时日志</span>
  </div>
  <p v-if="project.error" class="studio-notice error">{{project.error}}</p>
  <section v-if="warnedShots" class="prompt-warning-index" aria-label="建议核对的镜头"><div><strong>{{warnedShots}} 个镜头存在需要留意的提示词差异</strong><p class="muted">可能涉及遗漏的短文字、阶段内容混用或要求冲突，不代表生成失败。点击镜头查看详情；可检查后继续确认。</p></div><button v-for="row in warnedShotRows" :key="row.shot.id" type="button" :class="{active:selected===row.index}" @click="showWarning(row.index)"><b>第 {{String(row.index+1).padStart(2,'0')}} 镜</b><span>{{row.notes.map(note=>note.label).join('；')}}</span></button></section>
  <details v-if="promptWarnings.length" :key="selected" class="shot-subtitles current-shot-warning"><summary>第 {{String(selected+1).padStart(2,'0')}} 镜{{shotHasPromptWarning(selectedShot)?'核对详情':'措辞比对记录'}} · {{promptWarnings.length}} 条</summary><p class="muted">以下来自字面比对，不是语义判定。称呼、说法或气泡名称不同，不一定影响画面；请结合实际提示词判断。</p><p v-for="(note,i) in promptWarnings" :key="i" :class="{'prompt-note-warning':note.severity==='warning'}"><span class="prompt-note-level">{{note.severity==='warning'?'建议核对':'措辞记录'}}</span>{{note.label}}</p></details>
  <details v-if="project.planning_recovery&&project.shots[selected]?.visual_description&&!project.shots[selected]?.image_prompt" open class="shot-subtitles"><summary>当前镜头已保存的核心画面草案（尚未定稿）</summary><p>{{project.shots[selected].visual_description}}</p><p class="muted">可参考此草案，在下方补齐核心分镜图提示词和视频提示词；保存并确认前不会生成图片。</p></details>
  <details v-if="sceneReferencesEnabled||sceneAssets.length" class="scene-reference-assets" :open="sceneReferencesBusy||project.scene_references_status==='failed'||(project.status==='image_review'&&sceneReferencesBlockConfirm)">
   <summary><strong>场景参考资产</strong><span class="muted">{{sceneAssets.length}} 张 · {{sceneReferencesBusy?'正在准备场景参考':project.scene_references_status==='failed'?'需要重试':staleSceneShots.length?'有关联镜头仍使用旧版场景':sceneReferencesBlockConfirm?'场景参考尚未准备完成':'仅用于相关镜头，不占用视频时间轴'}}</span></summary>
   <div class="scene-reference-heading">
    <div><p class="muted">先固定场景中的空间布局、家具和道具，再将场景图提供给相关分镜。修改场景后，继续生成核心图会更新受影响的镜头，保留原图和生成记录。</p><p v-if="sceneReferencesBusy" class="image-edit-status running" role="status">{{project.scene_references_status==='planning'?'正在规划需要共用的场景…':'正在生成场景参考图…'}}</p><p v-else-if="sceneReferenceMessage" class="image-edit-status" :class="project.scene_references_status==='failed'?'failed':''">{{sceneReferenceMessage}}</p><p v-else-if="!sceneAssets.length" class="muted">{{project.scene_references_status==='completed'?'本次规划没有需要共用的场景。':project.status==='image_review'?'这个任务尚未生成场景参考。可手动补充，完成后选择相关核心图重绘。':'已启用场景参考，生成核心分镜图时会先规划和生成场景。'}}</p></div>
    <button v-if="sceneReferencesEnabled&&project.status==='image_review'&&sceneReferencesNeedGeneration" type="button" :disabled="busy||imageEditRunning" @click="generateSceneReferences">{{project.scene_references_status==='failed'||sceneAssets.length?'重试场景参考（调用 API）':'补充场景参考（调用 API）'}}</button>
   </div>
   <p v-if="project.status==='image_review'&&staleSceneShots.length" class="studio-notice">有 {{staleSceneShots.length}} 张核心图仍使用旧版场景，请重绘并检查这些镜头后再进入视频生成。</p>
   <button v-if="project.status==='image_review'&&staleSceneShots.length&&project.scene_asset_archive?.length" type="button" :disabled="busy||imageEditRunning||dirty" @click="restoreSceneBindings">保留现有画面，恢复原场景绑定</button>
   <p v-else-if="project.status==='image_review'&&sceneReferencesBlockConfirm" class="studio-notice">本任务已启用场景参考，场景准备尚未完成，暂不能确认进入下一阶段。请先补充或重试场景参考；完成后可按需重绘关联核心图。</p>
   <p v-if="sceneReferencesEnabled&&project.status==='image_review'&&sceneReferencesNeedGeneration" class="muted scene-cost-note">将调用语言与图像 API；已有核心分镜图保留，完成后可按需重绘相关镜头。</p>
   <div class="scene-assets-grid">
    <details v-for="asset in sceneAssets" :key="asset.id" class="scene-asset-card">
     <summary><img v-if="asset.image_status==='completed'" :src="imageUrl(asset)" :alt="asset.name||'场景参考'"><span v-else class="scene-thumbnail-placeholder">{{asset.image_status==='failed'?'未完成':'场景'}}</span><span><strong>{{asset.name||asset.id}}</strong><small class="muted">用于镜头 {{sceneUsedByLabel(asset)}}</small></span><span class="scene-asset-state">{{asset.image_task?.status==='running'?'正在重绘':asset.image_status==='completed'?'已生成':asset.image_status==='failed'?'生成失败':'等待生成'}}</span></summary>
     <div v-if="asset.image_status==='completed'" class="storyboard-result"><img :src="imageUrl(asset)" :alt="asset.name||'场景参考'"></div>
     <p v-if="asset.image_error" class="image-edit-status failed">{{asset.image_error}}</p>
     <p v-if="asset.quality_review&&!currentSceneQuality(asset)" class="studio-notice">当前图片或设计尚未由这份报告核实。旧检查记录保留，不能作为当前通过结论；请先保存修改，续跑时会核对当前场景。</p>
     <section v-if="currentSceneQuality(asset)" class="studio-notice">
      <strong>本图检查记录</strong>
      <p v-for="(issue,index) in currentSceneQuality(asset).issues" :key="'issue-'+index">{{issue.evidence}}</p>
      <div v-for="(note,index) in currentSceneQuality(asset).uncertainties" :key="'uncertain-'+index"><p>{{sceneUncertaintyLabel(asset,index)}}：{{typeof note==='string'?note:JSON.stringify(note)}}</p><p v-if="currentSceneQuality(asset).uncertainty_assessments?.[index]?.impact" class="muted">依据：{{currentSceneQuality(asset).uncertainty_assessments[index].impact}}</p></div>
      <p v-if="!currentSceneQuality(asset).issues.length&&!sceneBlockingNotes(asset).length">自动检查未发现阻断项，仍请查看实际画面。</p>
      <button v-if="project.status==='image_review'&&currentSceneQuality(asset).issues.length&&!sceneBlockingNotes(asset).length" type="button" class="primary-btn" :disabled="busy||imageEditRunning||dirty||asset.image_task?.status==='failed'" @click="repairSceneFromReview(asset)">按检查建议修复场景（调用图片 API）</button>
     </section>
     <p v-if="asset.image_task?.message" class="image-edit-status" :class="asset.image_task.status">{{asset.image_task.message}}</p>
    </details>
   </div>
  </details>
  <details v-if="project.logs.length&&!project.shots.length" class="planning-log" open><summary>任务日志 <span class="muted">{{project.logs.length}} 条</span></summary><div class="video-logs" role="log" aria-live="polite"><div v-for="(line,i) in project.logs" :key="i">{{line}}</div></div></details>
  <p v-if="sceneReferencesBusy||project.scene_references_status==='failed'" class="studio-notice">{{sceneReferenceMessage||'正在准备场景参考…'}}</p>
  <button v-if="sceneReferencesEnabled&&project.status==='image_review'&&sceneReferencesNeedGeneration" :disabled="busy||imageEditRunning" @click="generateSceneReferences">补充 / 重试场景参考（调用 API）</button>
  <template v-if="videoStageReady">   <audio v-if="!nativeSpeech&&project.audio" ref="motionAudio" :key="narrationUrl" preload="auto" :src="narrationUrl" class="motion-narration-audio"/>
   <div v-if="dynamicShots.length" class="motion-api-summary"><strong>项目默认：{{localVideo?(project.creation_parameters.comfyui_profile_id?.startsWith('ocv-h3-')?'OCV 内置 ComfyUI':'外部 ComfyUI'):'视频 API'}}</strong><span>各镜头可单独设置，批量生成尊重各镜头配置。</span><button type="button" :disabled="busy||videoRunning" @click="openClassicEditor">修改项目默认配置</button></div>
   <p v-if="videoModelError&&dynamicShots.some(shot=>optionsFor(shot).backend==='api')" class="studio-notice error">{{videoModelError}}</p>
   <div v-if="dynamicShots.length" class="motion-generation-bar">
    <div><strong>{{videoRunning?(project.status==='video_stopping'?'正在安全停止…':'正在生成动态镜头…'):completedVideos===dynamicShots.length?'动态片段已准备完成':'先试一镜，再决定是否批量生成'}}</strong><p class="muted">{{videoRunning?'按照“接口与服务”中的并发设置处理，实时进度见日志。停止不会撤销云端已提交或已计费的任务。':'只有点击生成并确认后才调用视频 API；已完成片段不会重复提交，失败镜头不会自动重新扣费。'}}</p></div>
    <button v-if="videoRunning" type="button" class="primary-btn" :disabled="busy||project.status==='video_stopping'" @click="stopVideos">{{project.status==='video_stopping'?'正在停止…':'停止生成'}}</button>
   <div v-else class="motion-batch-actions"><button type="button" class="primary-btn" :disabled="busy||videoRunning||!dynamicShots.length" @click="regenerateAllVideos">全部重新生成（{{dynamicShots.length}}）</button><button type="button" class="primary-btn" :disabled="busy||!pendingVideos.length" @click="generateVideos(pendingVideos)">生成全部未完成（{{pendingVideos.length}}）</button></div>
   <button v-if="localRetryVideos.length" type="button" :disabled="videoInputsPreparing||busy||!videoConfigured" @click="generateVideos(localRetryVideos,true)">重试失败镜头（{{localRetryVideos.length}}）</button>
    <button v-if="localQueueCandidates.length" type="button" :disabled="videoInputsPreparing||busy||!videoConfigured" @click="generateVideos(localQueueCandidates)" title="已在队列中的镜头会自动跳过，不会重复生成">将其余镜头加入队列</button>
   </div>
   <div v-if="allVideosReady&&!videoRunning&&!project.video_waiting_queue?.length" class="video-next-stage"><div><strong>{{dynamicShots.length?'全部动态片段已完成':'全部静态画面已就绪'}}</strong><p>{{dynamicShots.length?'镜头素材已就绪，可以进入最终合成。':'本项目不需要生成动态片段，可直接用图片与配音合成。'}}</p></div><button type="button" class="primary-btn" @click="view='export'">下一步：合成与导出</button></div>
   <p v-if="dynamicShots.some(shot=>shot.video_status!=='completed'&&(shot.video_terminal||shot.video_status==='failed'||(shot.video_status==='unknown'&&!shot.video_resume_available)))" class="duration-repair-note needs-review">批量处理会跳过失败或状态待核实的镜头，请在左侧选择相应镜头查看原因。确认失败的任务需单独点击“重新付费生成”。</p>
   <OperationStatus v-if="videoRunning||busy" title="动态镜头运行状态" :pending="busy" :task="{status:videoRunning?'running':'',message:project.status==='video_stopping'?'正在停止后续处理；已提交的服务端任务可能继续运行':'当前批次正在处理，可切换镜头查看各自状态'}" :summary="videoBatchSummary"/>
   <p v-if="project.error" class="studio-notice error">{{project.error}}</p>
</template>
  <div v-if="project.video_waiting_queue?.length&&!videoRunning" class="production-queue-notice"><span>已保存的生成队列待继续，刷新或关闭页面不会丢失。</span><button class="primary-btn" :disabled="busy" @click="resumeVideoQueue">继续等待队列</button></div>
  <ResizableShotWorkspace class="video-editor" v-if="project.shots.length" scope="dynamic-production"><template #sidebar><VideoShotNavigator :shots="project.shots" :selected="selected" :image-url="imageUrl" :has-warning="shotHasPromptWarning" :scene-assets="sceneAssets" motion :status-label="videoStatusLabel" @select="selectVideoShot($event)" :disabled="videoInputsPreparing" /></template>
   <div class="production-toolbar">
    <div class="production-tabs" role="tablist" aria-label="本镜制作分区"><button role="tab" :aria-selected="productionPanel==='design'" :class="{active:productionPanel==='design'}" @click="productionPanel='design'">画面与提示词</button><button role="tab" :aria-selected="productionPanel==='video'" :class="{active:productionPanel==='video'}" :disabled="selected<0||!videoStageReady" @click="productionPanel='video'">视频生成与结果</button></div>
    <div v-if="selectedShot" class="production-shot-state"><span :class="{locked:shotRenderLocked(selectedShot)}">{{shotRenderLocked(selectedShot)?selectedShot.video_status==='running'?'本镜生成中 · 编辑已锁定':'本镜已排队 · 可取消后编辑':'本镜可编辑'}}</span><button v-if="selectedShot.video_queued||selectedShot.video_waiting" :disabled="busy||selectedShot.video_status==='running'" @click="cancelVideoQueue(selectedShot)">取消排队并编辑</button></div>
   </div>
    <article v-if="selectedShot&&selected>=0&&productionPanel==='video'" :key="selectedShot.id" class="video-detail motion-detail">
     <div class="shot-local-navigation"><strong>镜头 {{String(selected+1).padStart(2,'0')}} <span>/ {{project.shots.length}}</span></strong><div><button :disabled="selected===0" @click="moveShot(-1)" aria-label="上一个镜头">← 上一镜</button><button :disabled="selected===project.shots.length-1" @click="moveShot(1)" aria-label="下一个镜头">下一镜 →</button></div></div>
     <details open class="shot-subtitles"><summary>对应字幕（只读）</summary><p v-for="id in selectedShot.slide_ids" :key="id">{{scenesById[id]?.text}}</p></details>
     <div class="motion-shot-heading"><div><h3>第 {{String(selected+1).padStart(2,'0')}} 镜 · {{videoStatusLabel(selectedShot)}}</h3><p class="muted">{{selectedShot.intent}}</p></div><div class="motion-heading-actions"><span v-if="selectedShot.kind==='video'" class="readonly-badge">使用 {{selectedShot.duration}} 秒 · 请求 {{requestedSeconds(selectedShot)}} 秒 · {{requestedResolution(selectedShot)}}</span><label v-if="selectedShot.kind==='video'" class="motion-upload-button" :class="{disabled:busy||videoRunning}">上传本地视频替换<input type="file" accept="video/mp4,.mp4" :disabled="busy||videoRunning" @change="uploadReplacementVideo(selectedShot,$event)"></label><button type="button" :disabled="busy||videoInputsPreparing" @click="nativeSpeech?reopenShot(selectedShot):productionPanel='design'">编辑本镜画面与提示词</button></div></div>
     <div class="structure-toolbar"><strong>局部返修</strong><button type="button" :disabled="!canEditStructure||selected===0" @click="selectVideoShot(selected-1);openBoundaryEditor('boundary')">与上一镜调整字幕</button><button type="button" :disabled="!canEditStructure||selected===project.shots.length-1" @click="openBoundaryEditor('boundary')">与下一镜调整字幕</button><button type="button" :disabled="!canEditStructure||selectedShot.slide_ids.length<2" @click="openBoundaryEditor('split')">拆分 / 新增分镜</button><button v-if="project.structure_history?.length" type="button" :disabled="!canEditStructure||videoRunning||Boolean(project.video_waiting_queue?.length)" @click="undoStructure">撤回结构调整</button><button v-if="shotEditingStage&&selectedShot.design_needs_review" type="button" :disabled="busy||imageEditRunning" @click="confirmDesign">确认本镜修改</button><span v-else-if="selectedShot.design_review_confirmed" class="muted">修改已确认</span></div>
     <ShotVideoOptions v-if="selectedShot.kind==='video'" v-model="selectedOptions" :profiles="comfyProfiles" :loading="comfyLoading" :disabled="shotOptionsLocked" :dirty="selectedOptionsDirty" :saved="Boolean(selectedShot.video_generation_options)" :save-disabled="shotOptionsLocked" :api-ready="Boolean(apiVideoReady)" :api-resolution="videoModel?.resolution||'720p'" :ratio="project.settings.ratio||'16:9'" :seconds="requestedSeconds(selectedShot)" :image-src="imageUrl(selectedShot)" @refresh="loadComfyProfiles" @save="saveShotOptions" @apply-all="applyAllShotOptions"/>
     <p v-if="selectedShot.video_request" class="muted">当前片段实际使用：{{selectedShot.video_request.backend==='comfyui'?'ComfyUI · '+(comfyProfiles.find(item=>item.id===selectedShot.video_request.profile_id)?.name||selectedShot.video_request.profile_id):'视频 API'}} · {{requestedResolution(selectedShot)}}。上方修改在下次生成时生效。</p>
     <p v-if="selectedLocalVideo&&comfyError" class="studio-notice error">工作流读取失败：{{comfyError}}</p>
     <div v-if="selectedShot.kind==='video'&&(!shotRenderLocked(selectedShot)||(selectedShot.video_resume_available&&!videoRunning))" class="motion-shot-actions">
      <button v-if="selectedShot.video_status==='completed'" type="button" class="primary-btn" :disabled="busy||rerunQueued(selectedShot)||selectedShot.video_queued||!selectedVideoConfigured" @click="regenerateVideo(selectedShot)">{{rerunQueued(selectedShot)?'已加入重生成队列':videoRunning?'加入重生成队列':'按此配置重新生成本镜'}}</button>
      <button v-else-if="selectedShot.video_terminal" type="button" class="primary-btn" :disabled="busy||!selectedVideoConfigured" @click="generateVideos([selectedShot],true,false,true)">{{selectedLocalVideo?'重新生成本镜':'重新付费生成本镜'}}</button>
      <button v-else-if="selectedShot.video_resume_available" type="button" class="primary-btn" :disabled="videoInputsPreparing||busy||!(selectedVideoConfigured||(selectedLocalVideo&&selectedShot.video_task_id))" @click="generateVideos([selectedShot],false,false,true)">{{selectedShot.video_not_submitted?'继续生成（尚未提交）':'继续查询原任务'}}</button>
      <button v-else-if="canProcessVideo(selectedShot)" type="button" class="primary-btn" :disabled="busy||!selectedVideoConfigured||Boolean(videoRunning&&selectedShot.video_queued)" @click="generateVideos([selectedShot],false,false,true)">{{videoRunning&&selectedShot.video_queued?'已加入生成队列':selectedLocalVideo&&videoRunning?'加入本地生成队列':'试生成当前镜头'}}</button>
     </div>
     <WorkspacePanels scope="dynamic-motions" :log-count="project.logs?.length||0">
      <template #preview>
     <div v-if="selectedShot.kind==='video'&&selectedShot.video_status==='completed'" class="motion-preview">
      <video :key="videoUrl(selectedShot)+':'+(project.audio_version||'')" controls :muted="!nativeSpeech" playsinline preload="metadata" :src="videoUrl(selectedShot)" :poster="imageUrl(selectedShot)" @loadedmetadata="syncMotionPreview($event,'metadata')" @play="syncMotionPreview($event,'play')" @pause="syncMotionPreview($event,'pause')" @ended="syncMotionPreview($event,'ended')" @seeking="syncMotionPreview($event,'seeking')" @timeupdate="syncMotionPreview($event,'timeupdate')" @ratechange="syncMotionPreview($event,'seeking')"></video>
      <label v-if="!nativeSpeech&&project.audio" class="motion-audio-toggle"><input type="checkbox" v-model="previewNarration">同步播放本镜配音</label>
      <p v-else-if="!nativeSpeech" class="muted">该任务没有配音快照，只能预览画面。</p>
      <div v-if="selectedShot.narration_preview_error" role="alert"><p>旁白混合未完成。{{selectedShot.narration_preview_error}}</p><button type="button" :disabled="busy||videoRunning" @click="retryNarrationPreview(selectedShot)">重新检查并混合旁白</button><p class="muted">复用本镜原视频及已有旁白，不重新生成，不调用语言模型。原声仍有异常时会保留提示。</p></div>
      <p v-else-if="selectedShot.narration_preview">{{externalDialogue?'当前预览采用 TTS 对白与旁白音轨，H3原片另行保留；配音引擎以本次提交记录为准，请核对发言位置和口型。':'当前为独立旁白混合预览，H3原片保留；口型和角色对应仍需观看确认。'}}</p>
      <p v-if="selectedShot.dialogue_alignment?.status==='calibrated'" class="muted">已按原片实际发声区间校准画面时序，保留原 TTS 声音；角色与逐音素口型仍需观看确认。</p>
      <p v-if="selectedShot.dialogue_alignment?.status==='audio_placed'" class="muted">已按原片实际发声位置移动已有配音，画面与声音长度保持不变；角色与口型仍需观看确认，也可拖动音轨微调。</p>
      <p v-if="selectedShot.dialogue_alignment?.status==='unverified'" role="alert">未能可靠校准口型时序：{{selectedShot.dialogue_alignment.warning}}。原片与配音已保留，当前预览尚未通过口型验收。</p>
      <p v-if="selectedShot.dialogue_alignment?.status==='manual'" class="muted">已采用手动音轨位置；声音和原始画面保持不变，口型及说话角色仍需观看确认。</p>
      <DubTimeline v-if="selectedShot.video_request?.external_dialogue" :key="project.id+'|'+selectedShot.id" :project-id="project.id" :shot="selectedShot" :revision="project.revision" :disabled="busy||videoRunning||videoInputsPreparing" @updated="project=$event" @busy-change="busy=$event" />
      <button v-if="selectedShot.video_request?.external_dialogue" type="button" :disabled="busy||videoRunning||videoInputsPreparing" @click="retryNarrationPreview(selectedShot)">校准本镜配音时序</button>
      <p v-if="selectedShot.video_request?.external_dialogue" class="muted">复用本次提交的原视频和配音，不重跑 H3，不调用语言模型。此操作不更换声音；更换音色需重新生成本镜。</p>
      <a :href="videoUrl(selectedShot)" :download="'镜头'+String(selected+1).padStart(2,'0')+'.mp4'" class="motion-download">↓ {{selectedShot.narration_preview?'下载本镜混合预览':'下载本镜原片'}}</a>
     </div>
     <OperationStatus v-if="selectedShot.kind==='video'" title="本镜运行状态" :task="videoTask(selectedShot)"/>
     <p v-if="selectedShot.video_error" class="studio-notice error">{{selectedShot.video_error}}</p>
     <ManualH3Prompt v-if="externalDialogue&&selectedShot.native_dialogue" :key="project.id+'|'+selectedShot.id+'|manual'" :project-id="project.id" :shot="selectedShot" :revision="project.revision" :disabled="busy||videoRunning||videoInputsPreparing" @updated="project=$event" @busy-change="busy=$event" />
     <a v-if="selectedShot.video_status==='failed'&&selectedShot.h3_prompt_diagnostic?.file" class="motion-download" :href="`/api/video-studio/${encodeURIComponent(project.id)}/shots/${encodeURIComponent(selectedShot.id)}/h3-diagnostic`" download="h3-diagnostic.json">查看转换失败草稿</a>
     <p v-if="selectedShot.video_task_id" class="motion-task-id muted">{{localVideo?'本地':'云端'}}任务编号：<code>{{selectedShot.video_task_id}}</code><span v-if="selectedShot.video_attempt"> · 第 {{selectedShot.video_attempt}} 次请求</span></p>
     <div v-if="externalDialogue&&selectedShot.video_status!=='completed'&&selectedShot.narration_preview_error" role="alert"><p>画面已保存，TTS配音预览尚未完成：{{selectedShot.narration_preview_error}}</p><button type="button" :disabled="busy||videoRunning" @click="retryNarrationPreview(selectedShot)">只重试配音合成</button><p class="muted">复用已有视频和配音，不重新运行H3，也不调用语言模型。</p></div>
     <p v-if="selectedShot.kind==='video'&&selectedShot.video_status==='unknown'&&!selectedShot.video_resume_available" class="duration-repair-note needs-review">本次提交结果未知，且无法安全查询原任务。请先向服务商核实，不要重新提交，避免重复扣费。</p>
     <p v-if="selectedShot.kind==='video'" class="motion-preview-note muted">{{nativeSpeech?(externalDialogue?'角色对白由所选 TTS 引擎合成，按实测音频位置对齐片段；请观看核对配音、说话者与嘴部动作。':'预览直接播放 H3 生成的原声，包括对白与环境声。请核对台词、说话者与画面；导出保留完整原声，并按实际片段长度拼接。'):'本页视频预览静音，可同步试听本镜旁白；这不代表成片删除了环境声。导出时可选择保留片段原声。请求秒数可能向上取整，最终合成只使用本镜字幕时长。'}}</p>
     <p v-else class="studio-notice">这是静态镜头，保留已确认的图片，不提交视频 API，也不产生视频生成费用。</p>
     <details v-if="selectedShot.kind==='video'&&selectedVideoHistory.length" class="motion-history"><summary>历史生成结果 · {{selectedVideoHistory.length}} 个</summary><p class="muted">重生成和本地替换都不会覆盖旧片段。可先预览，再决定采用或删除。</p><div class="motion-history-list"><div v-for="(row,position) in selectedVideoHistory" :key="row.index+':'+(row.entry.video_version||row.entry.video)" class="motion-history-row"><span><b>{{historyVersionLabel(row,position)}}</b><small>{{row.entry.invalidated_reason||'此前生成的动态片段'}}</small></span><div class="actions"><button type="button" @click="historyPreviewIndex=historyPreviewIndex===row.index?null:row.index">{{historyPreviewIndex===row.index?'收起':'预览'}}</button><button type="button" class="primary-btn" :disabled="busy||videoRunning" @click="adoptHistoryVersion(selectedShot,row.index)">采用</button><button type="button" :disabled="busy||videoRunning" @click="deleteHistoryVersion(selectedShot,row.index)">删除</button></div><video v-if="historyPreviewIndex===row.index" controls :muted="!nativeSpeech" playsinline preload="metadata" :src="historyVideoUrl(selectedShot,row.index)"></video></div></div></details>
     <details class="motion-core-reference" :open="selectedShot.video_status!=='completed'"><summary>已确认的核心分镜图 · 图1</summary><div class="storyboard-result"><img :src="imageUrl(selectedShot)" :alt="'第 '+(selected+1)+' 镜核心分镜图'"></div></details>
      </template>
      <template #prompts><NativePromptReview v-if="selectedShot.kind==='video'&&nativeSpeech" :shot="selectedShot" /><template v-else><p class="muted">{{selectedShot.video_request?'本次实际提交内容（只读）':'将提交的视频提示词（只读）'}}</p><p class="motion-final-prompt">{{selectedShot.video_request?.prompt||selectedShot.video_prompt||'本镜为静态画面，无视频提示词。'}}</p></template></template>
      <template #logs><div class="video-logs" role="log" aria-live="polite"><div v-for="(line,index) in project.logs" :key="index">{{line}}</div><p v-if="!project.logs?.length">暂无任务日志</p></div></template>
     </WorkspacePanels>
    </article>

   <template v-if="selected>=0&&selectedShot&&productionPanel==='design'"><div class="structure-toolbar"><strong>局部返修</strong><button type="button" :disabled="!canEditStructure||selected===0" @click="selectVideoShot(selected-1);openBoundaryEditor('boundary')">与上一镜调整字幕</button><button type="button" :disabled="!canEditStructure||selected===project.shots.length-1" @click="openBoundaryEditor('boundary')">与下一镜调整字幕</button><button type="button" :disabled="!canEditStructure||selectedShot.slide_ids.length<2" @click="openBoundaryEditor('split')">拆分 / 新增分镜</button><button v-if="project.structure_history?.length" type="button" :disabled="!canEditStructure||videoRunning||Boolean(project.video_waiting_queue?.length)" @click="undoStructure">撤回结构调整</button><button v-if="shotEditingStage&&selectedShot.design_needs_review" type="button" :disabled="busy||imageEditRunning" @click="confirmDesign">确认本镜修改</button><span v-else-if="selectedShot.design_review_confirmed" class="muted">修改已确认</span></div></template>
   <div v-for="{shot,i} in storyboardRows" :key="shot.id" v-show="i===selected&&productionPanel==='design'" class="video-detail">
    <div class="shot-local-navigation"><strong>{{i<0?'场景 '+(-i):'镜头 '+String(i+1).padStart(2,'0')}} <span v-if="i>=0">/ {{project.shots.length}}</span></strong><div v-if="i>=0"><button :disabled="selected===0" @click="moveShot(-1)" aria-label="上一个镜头">← 上一镜</button><button :disabled="selected===project.shots.length-1" @click="moveShot(1)" aria-label="下一个镜头">下一镜 →</button></div></div>
    <p v-if="i>=0&&storyboardLocked" class="readonly-stage-note">{{shotEditingStage?'字幕分组与时长已经确认；你仍可切换动静态、修改本镜提示词、重绘或替换图片。切换类型自动保存，不会重新生成图片。':'本阶段已经确认，以下内容仅供回顾，不会提供修改入口。'}}</p>
    <p v-if="i<0" class="readonly-stage-note">场景参考 · 用于镜头 {{sceneUsedByLabel(shot)}}。不占用视频时长；相关镜头下次重绘才采用新版场景。</p>
    <div @input="i>=0&&!storyboardLocked&&(dirty=true)">
     <WorkspacePanels scope="dynamic-storyboard" :log-count="project.logs.length">
      <template #preview><fieldset :disabled="busy||['planning','stopping'].includes(project.status)">
     <details v-if="i>=0" open class="shot-subtitles"><summary>对应字幕（只读）</summary><p v-for="id in shot.slide_ids" :key="id">{{project.scenes.find(s=>s.slide_id===id)?.text}}</p></details>
     <ShotReferencePreview :shot="shot" :references="project.references||[]" :scene="sceneAssetFor(shot)" :scene-enabled="sceneReferencesEnabled" :reference-url="redrawReferenceUrl" :image-url="imageUrl" />

     <div v-if="shot.image_status" class="storyboard-result" :class="shot.image_status"><img v-if="shot.image_status==='completed'" :src="imageUrl(shot)" :alt="'核心分镜图 '+(i+1)"><div v-else><strong>{{shot.image_status==='running'?'正在生成核心分镜图':shot.image_status==='failed'?'生成失败':'等待生成'}}</strong><p v-if="shot.image_error">{{shot.image_error}}</p></div></div>
     <p v-if="shot.core_reference_error" class="studio-notice">{{shot.core_reference_error}}；请更新本镜画面，原图仍保留。</p>
     <div v-if="sceneAssetFor(shot)" class="shot-scene-reference">
      <img v-if="sceneAssetFor(shot).image_status==='completed'" :src="imageUrl(sceneAssetFor(shot))" :alt="sceneAssetFor(shot).name||'场景参考'">
      <div><strong>场景参考 · {{sceneAssetFor(shot).name||sceneAssetFor(shot).id}}</strong><p class="muted">{{sceneUsageLabel(shot)}}</p><label v-if="project.status==='image_review'" class="scene-reference-toggle"><input type="checkbox" v-model="useSceneReference" :disabled="imageAssetRunning(shot)||sceneAssetFor(shot).image_status!=='completed'">重绘时使用场景参考</label><p v-if="sceneAssetFor(shot).image_status!=='completed'" class="muted">场景图尚未完成，可在上方场景参考资产中查看或重试。</p></div>
     </div>
      </fieldset></template>
      <template #prompts><fieldset :disabled="busy||imageAssetRunning(shot)||['planning','stopping'].includes(project.status)">
     <label v-if="i>=0">这一镜头想表达什么<input v-model="shot.intent" :disabled="busy||imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)" @input="rememberMotionDraft(shot)"></label>
     <div v-if="i>=0" class="video-row"><label>画面类型<select :value="shot.kind" @change="changeShotKind(shot,$event)" :disabled="nativeSpeech||imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)"><option value="static">静态画面</option><option value="video">动态视频</option></select></label><div v-if="!nativeSpeech&&shot.kind==='video'&&projectReferenceAudio" class="shot-audio-settings"><span>参考音频</span><label><input type="checkbox" :checked="shot.reference_audio_enabled!==false" :disabled="imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)" @change="shot.reference_audio_enabled=$event.target.checked;rememberMotionDraft(shot)">本镜启用</label><label :class="{disabled:shot.reference_audio_enabled===false}"><input type="checkbox" :checked="shot.reference_audio_lipsync!==false" :disabled="shot.reference_audio_enabled===false||imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)" @change="shot.reference_audio_lipsync=$event.target.checked;rememberMotionDraft(shot)">人物对口型</label></div><p class="muted">使用 {{shot.duration}} 秒<span v-if="shot.kind==='video'"> · 请求 {{Math.max(4,Math.ceil(shot.duration))}} 秒</span></p></div>
     <p v-if="shot.audio_timing_adjustment&&!shot.design_needs_review" class="image-edit-status completed">新配音时长已同步：已有动态片段继续复用；导出时会自动裁切较长片段，或匀速放慢较短片段以贴合本镜配音。</p>
     <p v-if="shot.duration_repair?.note" class="duration-repair-note" :class="{'needs-review':['boundary_fallback','single_subtitle_static'].includes(shot.duration_repair.method)}">{{shot.duration_repair.note}}</p>
     <p v-if="shot.warning" class="studio-notice">{{shot.warning}}</p>
     <label v-if="shot.kind==='video'" class="motion-field">动态表达<textarea v-model="shot.action" rows="3" @input="rememberMotionDraft(shot)" :disabled="imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)"/></label>
     <div v-if="i>=0&&shot.kind==='video'&&project.creation_parameters?.presenter_mode" class="shot-audio-settings"><label><input type="checkbox" :checked="shot.visual_design?.presenter_speaking===true" :disabled="imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)" @change="setPresenterSpeaking(shot,$event.target.checked)">本镜讲解员出镜开口</label><small class="muted">仅在图中确有指定讲解员时开启；勾选且本镜音频、对口型均启用时，将按本镜台词和配音驱动口型。不重新生图。</small><button v-if="project.status==='image_review'" :disabled="busy||!dirty||imageAssetRunning(shot)" @click="run(save)">保存讲解设置</button></div>
     <label class="image-prompt-field">{{i<0?'场景图提示词':'核心分镜图提示词'}} <small v-if="project.status==='image_review'" class="muted">修改后点击“按当前提示词重绘”提交，当前图片不会自动变化。</small><textarea v-model="shot.image_prompt" rows="4" :disabled="imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)" @input="shotEditingStage&&rememberImagePrompt(shot)"/></label>
     <label v-if="shot.kind==='video'" class="video-prompt-field">{{nativeSpeech?'动作预览（只读）':'视频模型最终提示词'}}<textarea v-model="shot.video_prompt" rows="5" @input="rememberMotionDraft(shot)" :readonly="nativeSpeech" :disabled="imageAssetRunning(shot)||(storyboardLocked&&!shotEditingStage)"/></label>
     <p v-if="nativeSpeech" class="muted">修改上方“动态表达”后，点击“按动态表达更新两个提示词”同步实际动作。台词在台词编辑器中修改；提交内容在动态镜头页查看。</p>
     <p v-if="shot.native_staging_stale" class="studio-notice" role="status">动作尚未同步，暂不能生成；请更新提示词后继续。</p>
     <p v-if="motionQueueState(shot)" role="status" class="muted">{{({queued:'本镜已排队，可继续切换其他镜头。',running:'正在处理本镜动静态切换及动态提示词规划…',completed:'本镜切换与规划已完成。',failed:'本镜切换或规划失败，可检查后重试；其他镜头继续处理。'})[motionQueueState(shot)]}} 不会生成图片或视频。</p>
     <p v-else-if="autoMotionBusy" role="status" class="muted">正在按核心图规划动态表达与视频模型最终提示词…不会生成图片或视频。</p>
     <button v-if="shotEditingStage&&shot.kind==='video'&&!shot.action?.trim()" type="button" :disabled="busy||imageEditRunning||autoMotionBusy" @click="run(()=>autoFillMotion(shot))">自动补写动态表达与视频提示词</button>
     <button v-if="shotEditingStage&&Object.keys(motionDrafts).length" type="button" :disabled="busy||imageEditRunning" @click="run(save)">保存动态表达与视频提示词</button>
     <p v-if="shotEditingStage&&shot.kind==='video'&&!shot.video_prompt?.trim()" class="muted">本镜尚未填写视频提示词。可直接按核心图生成，或先填写动态表达，再更新两个提示词；不会自动重绘图片。</p>
     <p v-if="shot.prompt_refresh_note" class="image-edit-status completed">{{shot.prompt_refresh_note}}</p>
     <p v-if="shot.image_prompt_out_of_sync" class="duration-repair-note needs-review">核心图提示词已经更新，但当前图片还是上一版。请按新提示词重绘，或改用“按当前核心图更新视频提示词”。</p>
      </fieldset></template>
      <template #logs><div v-if="i===selected" class="video-logs" role="log" aria-live="polite"><div v-for="(line,index) in project.logs" :key="index">{{line}}</div><p v-if="!project.logs.length">暂无任务日志</p></div></template>
     </WorkspacePanels>
    </div>
    <section v-if="project.status==='image_review'&&project.reedit_shot_id===shot.id" class="shot-reedit-next video-next-stage" role="status">
     <div><strong>正在返修第 {{String(i+1).padStart(2,'0')}} 镜</strong><p>未修改的图片、视频和成片会直接保留。{{autoAdvanceEnabled?'完成后将按本项目的一键设置继续处理尚未完成的镜头并合成，可能调用已配置的生成服务。':'完成后返回同一镜头的动态页，由你决定是否生成。'}}</p></div>
     <p v-if="localReeditBlock" class="image-confirmation-reason">{{localReeditBlock}}</p>
     <button type="button" class="primary-btn" :disabled="Boolean(localReeditBlock)" @click="finishShotReedit(shot)">完成返修，返回动态镜头</button>
    </section>
    <section v-if="project.status==='image_review'&&shot.image_task?.status==='failed'&&shot.image_task?.action==='auto_repair'" class="storyboard-edit-tools">
     <strong>上次辅助修图未完成</strong><p class="muted">原图已保留。重试会复用上次修图指令与参考图，并保留原请求回执；不会重新生成其他镜头。</p>
     <button type="button" :disabled="busy||imageEditRunning||dirty" @click="retryImageRepair(shot)">重试上次辅助修图</button>
    </section>
    <section v-if="project.status==='image_review'&&shot.image_status==='failed'" class="storyboard-edit-tools">
     <strong>恢复这一镜</strong><p class="muted">使用本镜提示词、角色素材和当前场景生成新版本，其他镜头保留。原失败请求记录也会保留。</p>
     <button type="button" class="primary-btn" :disabled="busy||imageEditRunning||sceneReferencesNeedGeneration&&sceneReferencesEnabled||!shot.image_prompt?.trim()" @click="retryShotImage(shot)">只重新生成这一镜</button>
     <p v-if="shot.image_task?.message" class="image-edit-status" :class="shot.image_task.status">{{shot.image_task.message}}</p>
    </section>
    <section v-if="shot.kind==='video'&&(shotEditingStage||project.status==='storyboard_review')" class="single-shot-refresh">
     <div><strong>重新规划本镜</strong><p class="muted">完整重规划从字幕与项目设定重新设计；局部更新沿用当前编辑内容。均不改变字幕、时长或其他镜头。</p></div>
     <button v-if="!nativeSpeech" type="button" :disabled="busy||imageAssetRunning(shot)" @click="refreshShotPrompts(shot,'full')">完整重规划本镜</button>
     <button type="button" :disabled="busy||imageAssetRunning(shot)||!shot.action?.trim()" @click="refreshShotPrompts(shot,'action')">按动态表达更新两个提示词</button>
     <button type="button" :disabled="busy||imageAssetRunning(shot)||!shot.image_prompt?.trim()" @click="refreshShotPrompts(shot,'image')">按核心图更新视频提示词</button>
     <p v-if="shot.image_origin==='reference_redraw'||shot.image_origin==='upload'" class="muted">当前图片来自{{shot.image_origin==='upload'?'本地替换':'参考模式重绘'}}；第二个按钮会先识别最终图片，再更新视频提示词。</p>
    </section>
    <section v-if="shotEditingStage" class="storyboard-edit-tools">
     <p v-if="shot.opening_preflight" role="status">上次动态开场检查：{{shot.opening_preflight.message}}。修改图片、动作或台词后需重新检查。</p>
     <div v-if="openingRepairFor(shot)" class="studio-notice" role="status">
      <strong>已识别开场图偏差，可直接局部修图</strong>
      <p>{{openingRepairFor(shot).evidence}}</p>
      <p>修复目标：{{openingRepairFor(shot).opening}}</p>
      <p class="muted">自动使用当前核心图和已有诊断，不需要手写修图指令。修图完成后请检查；不表示已经通过验收。</p>
      <button type="button" :disabled="videoInputsPreparing||busy||dirty||imageEditRunning||serverAutoRunning" @click="repairOpeningImage(shot)">按已识别问题修图</button>
     </div>

     <div class="storyboard-edit-heading"><div><strong>{{i<0?'修正这张场景图':'修正这张核心分镜图'}}</strong><p class="muted">提示词修改只在点击重绘时提交；参考图最多 3 张，编号按下方选中顺序传给图像模型。</p><p v-if="!useCurrentReference&&!redrawReferenceIds.length" class="muted">当前没有选择参考图，本次只按提示词重绘。</p><p v-if="sceneAssetFor(shot)&&useSceneReference" class="muted">场景图自动附在这些参考素材之后，只约束空间布局。</p></div><select v-model="redrawResolution" aria-label="重绘分辨率"><option value="">跟随任务清晰度</option><template v-if="redrawIcan"><option value="2K">2K</option><option value="2.5K">2.5K</option></template><template v-else><option v-for="resolution in redrawResolutions" :key="resolution" :value="resolution">{{resolution.toUpperCase()}}</option></template></select></div>

     <div class="storyboard-reference-library">
      <div class="reference-library-heading"><div><strong>本次重绘参考图</strong><p class="muted">点击图片选择或取消；绿色卡片会按图号顺序传给图像模型。</p></div><div><label class="file-action">＋ 上传新参考图<input type="file" accept="image/jpeg,image/png,image/webp" multiple :disabled="busy||imageEditRunning" @change="uploadRedrawReferences"></label><button v-if="uploadedRedrawReferences.length" type="button" :disabled="busy||imageEditRunning" @click="clearUploadedRedrawReferences">清空新增图</button></div></div>
      <div class="storyboard-reference-grid">
       <article v-if="shot.image_status==='completed'" class="reference-image-card" :class="{active:useCurrentReference}">
        <button type="button" :disabled="busy||imageAssetRunning(shot)" @click="toggleCurrentReference"><img :src="imageUrl(shot)" alt="当前核心分镜图"><span><b>当前核心图</b><small>重绘前的本图</small></span><em>{{useCurrentReference?'图1 · 正在使用':'未选择'}}</em></button>
       </article>
       <article v-for="asset in visibleRedrawReferences" :key="asset.origin+':'+asset.id" class="reference-image-card" :class="{active:redrawReferenceIds.includes(asset.id)}">
        <button type="button" :disabled="busy||imageAssetRunning(shot)" @click="toggleRedrawReference(asset.id)"><img :src="redrawReferenceUrl(asset)" :alt="asset.displayName"><span><b>{{asset.displayName}}</b><small>{{asset.retired?'历史角色参考（已被替换）':asset.origin==='scene'?'项目场景资产':asset.origin==='project'?'项目素材':'本页后来上传'}}</small></span><em>{{redrawReferenceIds.includes(asset.id)?'图'+redrawReferenceNumber(asset.id)+' · 正在使用':'未选择'}}</em></button>

        <button v-if="asset.origin==='uploaded'" type="button" class="reference-card-delete" :disabled="busy||imageEditRunning" title="删除这张新增参考图" @click.stop="deleteRedrawReference(asset)">删除</button>
       </article>
       <p v-if="!redrawReferenceGallery.length" class="reference-library-empty">创建任务时没有上传参考图；可在右上角补充新图片。</p>
      </div>
      <label v-if="projectRedrawReferences.some(asset=>asset.retired)" class="muted"><input v-model="showRetiredReferences" type="checkbox">显示已替换的历史角色参考</label>
     </div>
     <OperationStatus title="本镜图片状态" :task="shot.image_task||{status:shot.image_status,message:shot.image_status==='running'?'正在生成核心图':''}"/>
     <div class="storyboard-edit-actions">
      <button type="button" class="primary-btn" :disabled="videoInputsPreparing||busy||imageAssetRunning(shot)||!shot.image_prompt?.trim()" @click="redrawShot(shot)">▶ 按当前提示词重绘</button>
      <label class="file-action">↕ 替换本地图片<input type="file" accept="image/jpeg,image/png,image/webp" :disabled="busy||imageAssetRunning(shot)" @change="replaceShotImage($event,shot)"></label>
      <button type="button" :disabled="busy||imageAssetRunning(shot)||!shot.image_history?.length" @click="undoShot(shot)">↶ 撤回上一版</button>
      <button type="button" :disabled="busy||imageAssetRunning(shot)||!shot.baseline_image_prompt" @click="resetShotPrompt(shot)">恢复初始提示词</button>
     </div>
    </section>
    <section v-if="!storyboardLocked&&!['planning','stopping'].includes(project.status)&&(project.manual_groups?.length||shot.design_needs_review||shot.previous_designs?.length)" class="video-actions">
     <span v-if="project.manual_groups?.length" class="muted">手动字幕分组已锁定，重新规划也会保留。</span>
     <template v-if="shot.design_needs_review"><p>字幕范围已调整，原提示词保留为草稿，请按新字幕更新设计，或检查修改后确认沿用。</p><button :disabled="busy" @click="repairDesigns">只更新受影响镜头设计</button><button :disabled="busy" @click="confirmDesign">确认沿用当前设计</button></template>
     <details v-if="shot.previous_designs?.length"><summary>查看调整前的设计</summary><article v-for="(old,index) in shot.previous_designs" :key="index"><b>{{old.intent}}</b><p>{{old.action}}</p><p>{{old.image_prompt}}</p><p>{{old.video_prompt}}</p></article></details>
    </section>
    <div v-if="i>=0&&canEditStructure" class="video-actions"><button v-if="!storyboardLocked" class="primary-btn" :disabled="busy||!dirty||['planning','stopping'].includes(project.status)" @click="run(save)">保存镜头修改</button><button :disabled="busy||(shot.slide_ids.length<2&&i===project.shots.length-1)||['planning','stopping'].includes(project.status)" @click="openBoundaryEditor()">调整分镜…</button><button v-if="!storyboardLocked&&project.structure_history?.length" :disabled="busy||['planning','stopping'].includes(project.status)" @click="undoStructure">撤回结构调整</button><button :disabled="busy||shot.slide_ids.length<2||['planning','stopping'].includes(project.status)" @click="openBoundaryEditor('split')">新增分镜（选择字幕切口）</button><button :disabled="busy||project.shots.length<2||['planning','stopping'].includes(project.status)" @click="structure('delete')">删除画面并合并字幕</button></div>
    <p v-if="structureFeedback" class="studio-notice" :class="{error:!!error}" role="status">{{structureFeedback}}</p>
   </div>
  </ResizableShotWorkspace>
 </section>
</section>
 <Teleport to="body">
  <div v-if="boundaryEditor.open&&project&&selectedShot" class="structure-overlay" @click.self="!busy&&closeBoundaryEditor()">
   <audio v-if="project.audio" ref="boundaryAudio" :key="narrationUrl" :src="narrationUrl" preload="metadata" />
    <section v-if="selected>=0&&boundaryEditor.open&&boundaryPreview" class="video-boundary-editor structure-dialog" role="dialog" aria-modal="true" aria-label="调整字幕与分镜">
     <header><div><strong>调整字幕与分镜 · 第 {{String(selected+1).padStart(2,'0')}} 镜</strong><p class="muted">按现有字幕切口调整。配音保持连续；受影响镜头需检查设计与素材，其他镜头保留。</p></div><button type="button" @click="closeBoundaryEditor">关闭</button></header>
     <div class="boundary-modes" role="group" aria-label="选择分镜调整方式">
      <button type="button" :aria-pressed="boundaryEditor.mode==='boundary'" :class="{active:boundaryEditor.mode==='boundary'}" :disabled="busy||selected===project.shots.length-1" @click="openBoundaryEditor('boundary')">调整字幕归属</button>
      <button type="button" :aria-pressed="boundaryEditor.mode==='split'" :class="{active:boundaryEditor.mode==='split'}" :disabled="nativeSpeech||busy||selectedShot.slide_ids.length<2" @click="openBoundaryEditor('split')">拆分 / 新增分镜</button>
      <button type="button" :aria-pressed="boundaryEditor.mode==='merge'" :class="{active:boundaryEditor.mode==='merge'}" :disabled="busy||selected===project.shots.length-1" @click="openBoundaryEditor('merge')">合为一镜</button>

     </div>
     <p class="boundary-mode-help muted">{{boundaryEditor.mode==='boundary'?'在当前镜头与后一个镜头之间移动字幕，镜头数量不变。':boundaryEditor.mode==='split'?'在当前镜头内选择字幕切口，一个镜头拆成两个。':'将当前镜头与后一个镜头合并，以下显示合并前的两段内容。'}}</p>
     <label v-if="boundaryEditor.mode!=='merge'" class="video-boundary-slider"><span>{{boundaryEditor.mode==='boundary'?'调整相邻镜头边界':'选择切口'}}：第 {{boundary}} 条字幕之后 · {{boundaryPreview.boundary.toFixed(2)}} 秒</span><input v-model.number="boundary" type="range" min="1" :max="selectedShot.slide_ids.length+(boundaryEditor.mode==='boundary'?(project.shots[selected+1]?.slide_ids.length||0):0)-1" step="1"></label>
     <div class="video-boundary-grid">
      <article><div><b>{{boundaryEditor.mode==='split'?'拆分后的前镜头':'当前镜头'}}</b><span>{{(boundaryPreview.left.end-boundaryPreview.left.start).toFixed(2)}} 秒</span></div><p v-for="row in boundaryPreview.left.rows" :key="row.slide_id">{{row.text}}</p><button type="button" @click="playBoundaryPart('left')">▶ 试听整段</button></article>
      <article><div><b>{{boundaryEditor.mode==='split'?'拆分后的后镜头':'后一镜头'}}</b><span>{{(boundaryPreview.right.end-boundaryPreview.right.start).toFixed(2)}} 秒</span></div><p v-for="row in boundaryPreview.right.rows" :key="row.slide_id">{{row.text}}</p><button type="button" @click="playBoundaryPart('right')">▶ 试听整段</button></article>
     </div>
     <div class="video-boundary-listen"><button type="button" @click="playBoundaryEdge('left')">▶ 试听前段末尾</button><button type="button" @click="playBoundaryEdge('right')">▶ 试听后段开头</button><button type="button" @click="playBoundaryEdge('continuous')">▶ 连续试听交界</button><span>共同边界 {{boundaryPreview.boundary.toFixed(2)}} 秒</span></div>
     <p v-if="boundaryEditor.mode==='merge'&&boundaryPreview.combinedDuration>15" class="duration-repair-note needs-review">合并后共 {{boundaryPreview.combinedDuration.toFixed(2)}} 秒，超过建议的 15 秒；建议拆分，也可保留并按工作流能力尝试。</p>
     <p v-else-if="boundaryEditor.mode!=='merge'&&(boundaryPreview.left.end-boundaryPreview.left.start>15||boundaryPreview.right.end-boundaryPreview.right.start>15)" class="duration-repair-note needs-review">调整后仍有镜头超过建议的 15 秒，可能增加资源占用；你可以继续保留，也可保存后拆分。</p>
     <p class="muted">保存后返回动态分镜检查：受影响的现有图片保留，新镜头需要生成图片；受影响视频存入历史，需按新字幕范围重新生成。此操作不调用生成 API。</p>
     <p v-if="error" class="studio-notice error" role="alert">{{error}}</p><footer><button type="button" @click="closeBoundaryEditor">取消</button><button class="primary-btn" type="button" :disabled="busy||!canEditStructure" @click="applyBoundaryEdit">{{boundaryEditor.mode==='boundary'?'确认调整边界':boundaryEditor.mode==='split'?'确认拆成两个镜头':'确认合并镜头'}}</button></footer>
    </section>

  </div>
 </Teleport>
</template>
<style scoped>
.production-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;padding:16px;margin-bottom:16px;background:color-mix(in srgb,var(--accent) 7%,var(--panel));border:1px solid var(--border);border-radius:14px}
.production-tabs{display:flex;gap:4px;padding:4px;border-radius:10px;background:var(--bg,#111a25);border:1px solid var(--border)}
.production-tabs button{border:1px solid transparent!important;background:transparent!important;color:var(--muted);padding:10px 18px!important;border-radius:8px;font-weight:650}
.production-tabs button.active{color:var(--text)!important;background:color-mix(in srgb,var(--accent) 22%,var(--panel))!important;border-color:var(--accent)!important}
.production-tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.production-shot-state{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--muted)}
.production-shot-state .locked{color:var(--warning,#d6bc87)}
.production-shot-state button{border-color:var(--warning,#d6bc87);color:var(--warning,#d6bc87)}
.production-queue-notice{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border:1px solid var(--border);border-radius:12px;margin:14px 0;background:color-mix(in srgb,var(--accent) 8%,var(--panel))}
@media(max-width:700px){.production-toolbar,.production-queue-notice{align-items:stretch;flex-direction:column}.production-tabs button{flex:1;padding:10px!important}.production-shot-state{justify-content:space-between}}
.structure-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:12px;margin:12px 0;border:1px solid var(--border,#35423f);border-radius:10px}.structure-toolbar strong{margin-right:8px}.structure-overlay{position:fixed;inset:0;z-index:2000;background:#000a;display:flex;align-items:center;justify-content:center;padding:20px}.structure-dialog{width:min(940px,100%);max-height:90vh;overflow:auto;box-sizing:border-box;background:var(--panel,#1e2625);color:var(--text,#eef3f1);padding:24px;border:1px solid var(--border,#35423f);border-radius:16px}.structure-dialog button{cursor:pointer}.structure-dialog button:disabled{opacity:.45;cursor:default}.structure-dialog footer{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}
.motion-batch-actions button:first-child:not(.primary-btn){color:var(--accent);border:1px solid var(--accent);background:color-mix(in srgb,var(--accent) 18%,var(--panel));font-weight:650}
.motion-batch-actions button:first-child:not(.primary-btn):hover:not(:disabled){background:color-mix(in srgb,var(--accent) 30%,var(--panel));box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 15%,transparent)}
.motion-batch-actions button:first-child:not(.primary-btn):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.replan-description{flex:1}
.replan-options{margin-top:8px;max-width:780px}
.replan-options summary{cursor:pointer;font-size:13px;line-height:1.7}
.replan-options fieldset{border:0;padding:14px 0 0;margin:0;min-width:0}
.replan-options fieldset:disabled{opacity:.6}
.replan-options .replan-regroup{display:flex;align-items:center;gap:8px;margin:16px 0 6px;font-size:13px}
.replan-regroup input{width:16px;height:16px;margin:0;accent-color:var(--accent,#81d9bd)}
.export-layout-panel{margin:18px 0;padding:16px 20px;border:1px solid var(--line,#394440);border-radius:14px;background:#19211f}.export-layout-panel>summary{cursor:pointer;font-weight:600;display:flex;gap:12px;flex-wrap:wrap;align-items:center}.export-layout-panel>summary span{font-size:12px;font-weight:400}.export-layout-content{max-width:760px;margin:24px auto 8px;display:grid;gap:20px}.export-layout-context{display:grid;gap:10px;border-bottom:1px solid #394440;padding-bottom:16px}.export-layout-context label{display:grid;gap:8px}.export-layout-context select{width:100%;min-width:0}.export-layout-context p{font-size:13px;line-height:1.7}
.prompt-note-level{display:inline-block;margin-right:8px;color:var(--muted,#aab8b3);font-size:12px}.prompt-note-warning .prompt-note-level{color:#e6ba62}.current-shot-warning p{overflow-wrap:anywhere}
.video-card label.dynamic-scene-reference-toggle{display:flex;align-items:center;gap:8px;margin-top:14px}.video-card .dynamic-scene-reference-toggle input{width:16px;height:16px;margin:0;accent-color:var(--accent,#81d9bd)}.video-card :deep(.dynamic-text-mode-row){grid-template-columns:1fr}.video-card :deep(.dynamic-text-mode-row .director-strategy-options){justify-self:start}
.motion-heading,.motion-generation-bar,.motion-shot-heading{display:flex;align-items:center;justify-content:space-between;gap:18px}.motion-heading h2,.motion-shot-heading h3{margin:3px 0 7px}.motion-heading p,.motion-shot-heading p{margin:0;line-height:1.55}.motion-generation-bar{padding:15px 17px;border:1px solid color-mix(in srgb,var(--accent,#81d9bd) 45%,var(--border,#35423f));border-radius:12px;background:color-mix(in srgb,var(--accent,#81d9bd) 7%,var(--panel,#1e2625));margin-bottom:15px}.motion-generation-bar p{margin:5px 0 0;line-height:1.6}.motion-generation-bar>button{flex-shrink:0}.motion-batch-actions{display:flex;justify-content:flex-end;gap:10px;flex-shrink:0;flex-wrap:wrap}.motion-shot-heading{align-items:flex-start;margin-bottom:14px}.motion-shot-heading .readonly-badge{white-space:normal;line-height:1.5}.motion-preview{display:grid;gap:12px;margin:16px 0}.motion-preview video{display:block;width:100%;max-height:560px;background:#070b0a;border:1px solid var(--border,#35423f);border-radius:12px}.motion-download{justify-self:start;display:inline-flex;padding:9px 13px;border:1px solid var(--border,#35423f);border-radius:8px;color:var(--accent,#81d9bd);text-decoration:none}.motion-progress{padding:25px;text-align:center;border:1px solid var(--border,#35423f);border-radius:12px;background:var(--bg,#141918);color:var(--muted,#aab8b3)}.motion-progress p{margin-bottom:0}.motion-task-id{font-size:12px;overflow-wrap:anywhere}.motion-shot-actions{display:flex;gap:10px;flex-wrap:wrap;margin:15px 0}.motion-preview-note{font-size:13px;line-height:1.65}.motion-core-reference,.motion-request-review{padding:14px;border:1px solid var(--border,#35423f);border-radius:11px}.motion-core-reference .storyboard-result{margin-bottom:0}.motion-request-review p{line-height:1.65;overflow-wrap:anywhere}.motion-final-prompt{white-space:pre-wrap;color:var(--text,#eef3f1);font-size:14px}.motion-editor aside button.motion-failed{border-left:3px solid #d8aa4b}.motion-detail{min-width:0}
@media(max-width:900px){.motion-heading,.motion-generation-bar,.motion-shot-heading{align-items:flex-start;flex-direction:column}.motion-generation-bar>button,.motion-batch-actions,.motion-batch-actions button{width:100%}}
.single-shot-refresh{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:14px 0;padding:14px 16px;border:1px solid var(--border,#35423f);border-radius:11px;background:color-mix(in srgb,var(--accent,#81d9bd) 5%,var(--panel,#1e2625))}.single-shot-refresh>div{flex:1 1 320px}.single-shot-refresh>div p{margin:5px 0 0}.single-shot-refresh>p{flex-basis:100%;margin:2px 0 0}
.prompt-warning-index{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:14px 0;padding:14px 16px;border:1px solid color-mix(in srgb,#d8aa4b 62%,var(--border,#35423f));border-radius:11px;background:color-mix(in srgb,#d8aa4b 7%,var(--panel,#1e2625))}.prompt-warning-index>div{flex:1 1 360px}.prompt-warning-index p{margin:5px 0 0;line-height:1.6}.prompt-warning-index button{display:grid;gap:3px;text-align:left;max-width:320px}.prompt-warning-index button span{font-size:12px;color:var(--muted,#aab8b3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.prompt-warning-index button.active{border-color:#d8aa4b}.video-editor aside button.warning b{display:flex;align-items:center;justify-content:space-between;gap:6px}.video-editor aside button.warning em{font-size:11px;font-style:normal;color:#e6ba62;border:1px solid color-mix(in srgb,#d8aa4b 55%,transparent);border-radius:999px;padding:2px 6px}
.boundary-modes{display:flex;gap:4px;padding:4px;margin-top:16px;border:1px solid var(--border,#35423f);border-radius:10px;background:var(--bg,#141918);max-width:480px}.boundary-modes button{flex:1;min-width:0;padding:10px 8px;border:1px solid transparent;border-radius:7px;background:transparent;color:inherit}.boundary-modes button.active{background:color-mix(in srgb,var(--accent,#81d9bd) 16%,var(--panel,#1e2625));border-color:var(--accent,#81d9bd);color:var(--accent,#81d9bd)}.boundary-mode-help{margin:10px 0 14px;line-height:1.6;font-size:13px}.boundary-modes button:focus-visible{outline:2px solid var(--accent,#81d9bd);outline-offset:2px}.export-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px;align-items:center}.export-actions a{text-decoration:none;display:inline-flex;align-items:center;justify-content:center}.video-history-panel>video{display:block;width:100%;max-height:68vh;margin-top:18px;background:#080b0c;border-radius:10px}
.video-workspace{max-width:1500px}.video-start{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}.video-card{border:1px solid var(--border,#35423f);background:var(--panel,#1e2625);border-radius:18px;padding:24px;min-width:0}.video-card h2{margin:0 0 16px}.video-card label{display:grid;gap:8px;margin-bottom:14px}.video-card input,.video-card textarea,.video-card select{width:100%;box-sizing:border-box;color:inherit;background:var(--bg,#141918);border:1px solid var(--border,#35423f);border-radius:9px;padding:10px}.video-card textarea{resize:vertical}.video-card details{margin:14px 0}.video-card summary{cursor:pointer;margin-bottom:12px}.video-record{display:grid;text-align:left;gap:6px;width:100%;padding:14px;margin-bottom:8px}.video-project header,.video-row{display:flex;align-items:center;justify-content:space-between;gap:20px}.video-editor{display:grid;grid-template-columns:220px minmax(0,1fr);gap:24px;margin-top:20px}.video-editor aside{max-height:780px;overflow:auto}.video-editor aside button{display:grid;gap:8px;text-align:left;width:100%;padding:14px;margin:0 0 8px}.video-editor aside button.active{border-color:var(--accent,#81d9bd);background:#30443c}.video-detail fieldset{border:0;padding:0;margin:0;min-width:0}.video-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;align-items:center}.video-actions select{width:auto}.video-logs{background:#101514;border-radius:10px;padding:14px;line-height:1.8;margin-top:15px;color:#9fbbb1}.video-card button{cursor:pointer}.video-card button:disabled{opacity:.4;cursor:default}@media(max-width:900px){.video-start,.video-editor{grid-template-columns:1fr}.video-editor aside{max-height:220px}.video-project header{align-items:start;flex-wrap:wrap}}
.video-stage-nav{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:18px 0 22px;padding:7px;border:1px solid var(--border,#35423f);border-radius:14px;background:var(--panel,#1e2625)}
.video-stage-nav button{display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto;column-gap:9px;align-items:center;text-align:left;min-width:0;padding:11px 13px;border:1px solid transparent;border-radius:10px;background:transparent;color:var(--muted,#aab8b3);cursor:pointer}
.video-stage-nav button>span{grid-row:1/3;display:grid;place-items:center;width:25px;height:25px;border-radius:50%;background:var(--bg,#141918);color:inherit}.video-stage-nav b{color:inherit}.video-stage-nav small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.video-stage-nav button.done{color:var(--accent,#81d9bd)}.video-stage-nav button.current{color:var(--text,#eef3f1)}.video-stage-nav button.upcoming{opacity:.62}.video-stage-nav button.active{border-color:color-mix(in srgb,var(--accent,#81d9bd) 58%,var(--border,#35423f));background:color-mix(in srgb,var(--accent,#81d9bd) 10%,transparent);color:var(--text,#eef3f1)}
.video-history-panel{margin-top:0}.history-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:20px}.history-heading h2{margin:3px 0 6px}.history-heading p{margin:0}.readonly-badge{flex:0 0 auto;padding:6px 10px;border:1px solid var(--border,#35423f);border-radius:999px;color:var(--muted,#aab8b3);font-size:12px}.video-history-panel textarea{line-height:1.65;resize:vertical}.video-history-panel audio{width:min(560px,100%);margin-bottom:18px}.readonly-summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.subtitle-review-list{display:grid;gap:8px;max-height:440px;overflow:auto}.subtitle-review-list p{display:grid;grid-template-columns:110px minmax(0,1fr);gap:12px;margin:0;padding:10px 12px;border:1px solid var(--border,#35423f);border-radius:9px}.subtitle-review-list time{color:var(--muted,#aab8b3);font-variant-numeric:tabular-nums}
.readonly-stage-note{margin:0 0 14px;padding:10px 12px;border:1px solid var(--border,#35423f);border-radius:9px;color:var(--muted,#aab8b3);background:var(--bg,#141918)}
@media(max-width:900px){.video-stage-nav{grid-template-columns:1fr 1fr}.readonly-summary-grid{grid-template-columns:1fr 1fr}.subtitle-review-list p{grid-template-columns:1fr}}
.video-header-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px}
@media(max-width:900px){.video-header-actions{width:100%;justify-content:flex-start;flex-wrap:wrap}}
.video-workspace .video-detail{min-width:0}
.video-workspace .video-card textarea{min-height:0;height:auto;line-height:1.6}
.video-workspace .video-detail textarea{height:136px;min-height:80px;max-height:380px;overflow-y:auto}
.video-workspace .video-detail .motion-field textarea{height:88px}
.video-workspace .video-detail .video-prompt-field textarea{height:152px}
.video-workspace .video-editor{align-items:start;gap:20px}
.video-workspace .video-editor aside{min-width:0;overflow-x:hidden;overflow-y:auto;max-height:620px}
.video-workspace .video-editor aside button{box-sizing:border-box;min-width:0;max-width:100%;white-space:normal;gap:5px}
.video-workspace .video-editor aside button span{min-width:0;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.5}
.video-workspace .video-row label{margin-bottom:10px}
.shot-audio-settings{display:flex;align-items:center;gap:10px;padding:8px 11px;border:1px solid color-mix(in srgb,var(--accent,#81d9bd) 32%,var(--border,#35423f));border-radius:9px;background:#202c28}.shot-audio-settings>span{font-size:11px;color:var(--muted,#9fb0aa)}.video-card .shot-audio-settings label{display:flex;align-items:center;gap:6px;margin:0;white-space:nowrap;font-size:12px}.video-card .shot-audio-settings input{width:15px;height:15px;margin:0;padding:0}.shot-audio-settings label.disabled{opacity:.45}
.duration-repair-note{margin:0 0 14px;padding:9px 12px;border-left:3px solid var(--accent,#81d9bd);border-radius:0 7px 7px 0;background:color-mix(in srgb,var(--accent,#81d9bd) 6%,transparent);color:var(--muted,#aab8b3);font-size:13px;line-height:1.6}.duration-repair-note.needs-review{border-left-color:#d8b86d;background:color-mix(in srgb,#d8b86d 7%,transparent)}
.video-workspace .planning-log{margin:12px 0 0;border-top:1px solid var(--border,#35423f);padding-top:12px}
.video-workspace .planning-log summary{display:list-item;font-size:13px;margin:0}
.video-workspace .planning-log .video-logs{max-height:160px;overflow:auto;margin-top:10px;font-size:13px}
.motion-api-summary{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:11px 13px;margin-bottom:12px;border:1px solid var(--border,#35423f);border-radius:10px;background:var(--panel,#1e2625)}.motion-api-summary span{color:var(--muted,#aab8b3);margin-right:auto}.motion-api-summary button{padding:6px 10px}
.video-workspace .video-source-summary audio{height:36px;max-width:100%}
.video-workspace .video-actions{border-top:1px solid var(--border,#35423f);padding-top:14px;margin-top:12px}
.video-boundary-editor{margin:18px 0 6px;padding:16px;border:1px solid color-mix(in srgb,var(--accent,#81d9bd) 55%,var(--border,#35423f));border-radius:12px;background:color-mix(in srgb,var(--accent,#81d9bd) 6%,var(--bg,#141918))}.video-boundary-editor>header,.video-boundary-editor>footer,.video-boundary-listen,.video-boundary-grid article>div{display:flex;align-items:center;justify-content:space-between;gap:12px}.video-boundary-editor>header p{margin:5px 0 0}.video-boundary-editor>footer{justify-content:flex-end;margin-top:14px;padding-top:13px;border-top:1px solid var(--border,#35423f)}.video-boundary-slider{margin:16px 0!important;padding:12px;border:1px solid var(--border,#35423f);border-radius:9px;background:var(--panel,#1e2625)}.video-boundary-slider input{padding:0!important;accent-color:var(--accent,#81d9bd)}.video-boundary-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.video-boundary-grid article{min-width:0;padding:14px;border:1px solid var(--border,#35423f);border-radius:10px;background:var(--panel,#1e2625)}.video-boundary-grid article span{color:var(--muted,#aab8b3);font-variant-numeric:tabular-nums}.video-boundary-grid article p{margin:9px 0;line-height:1.65;overflow-wrap:anywhere}.video-boundary-grid article button{margin-top:7px}.video-boundary-listen{justify-content:flex-start;flex-wrap:wrap;margin-top:12px}.video-boundary-listen span{margin-left:auto;color:var(--muted,#aab8b3);font-size:13px;font-variant-numeric:tabular-nums}
.video-workspace .shot-subtitles{margin:0 0 18px;padding:14px 16px;border:1px solid var(--border,#35423f);border-radius:10px;background:color-mix(in srgb,var(--panel,#1e2625) 72%,var(--bg,#141918))}
.video-workspace .shot-subtitles summary{margin:0;font-weight:700}.video-workspace .shot-subtitles[open] summary{margin-bottom:9px}.video-workspace .shot-subtitles p{margin:5px 0;line-height:1.65}
.video-workspace .video-next-stage{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:16px 0 4px;padding:14px 16px;border:1px solid color-mix(in srgb,var(--accent,#81d9bd) 52%,var(--border,#35423f));border-radius:12px;background:color-mix(in srgb,var(--accent,#81d9bd) 9%,var(--panel,#1e2625))}.video-workspace .video-next-stage p{margin:5px 0 0;color:var(--muted,#aab8b3);line-height:1.55}.video-workspace .video-next-stage span{flex:0 0 auto;padding:8px 11px;border-radius:8px;background:var(--bg,#141918);color:var(--muted,#aab8b3);font-size:13px}
.video-workspace .storyboard-result{display:grid;place-items:center;min-height:180px;margin:0 0 18px;border:1px solid var(--border,#35423f);border-radius:12px;overflow:hidden;background:#080d0c;text-align:center}.video-workspace .storyboard-result img{display:block;width:100%;max-height:520px;object-fit:contain}.video-workspace .storyboard-result.failed{padding:18px;border-color:#85504f;color:#ffaaa7}.video-workspace .storyboard-result.running{padding:18px;color:var(--accent,#81d9bd)}
.storyboard-edit-tools{margin-top:14px;padding:16px;border:1px solid color-mix(in srgb,var(--accent,#81d9bd) 38%,var(--border,#35423f));border-radius:12px;background:color-mix(in srgb,var(--accent,#81d9bd) 6%,var(--bg,#141918))}.storyboard-edit-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.storyboard-edit-heading p{margin:5px 0 0}.storyboard-edit-heading select{width:auto;min-width:150px;padding:8px 10px;color:inherit;background:var(--bg,#141918);border:1px solid var(--border,#35423f);border-radius:8px}.storyboard-reference-list,.storyboard-edit-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:13px}.storyboard-reference-list button.active{border-color:var(--accent,#81d9bd);background:color-mix(in srgb,var(--accent,#81d9bd) 18%,var(--panel,#1e2625));color:var(--text,#eef3f1)}.file-action{display:inline-flex!important;align-items:center;justify-content:center;width:auto!important;margin:0!important;padding:8px 12px;border:1px solid var(--border,#35423f);border-radius:8px;background:var(--panel,#1e2625);cursor:pointer}.file-action input{display:none}.image-edit-status{margin:12px 0 0;padding:9px 11px;border-radius:8px;background:var(--panel,#1e2625);color:var(--muted,#aab8b3)}.image-edit-status.running{color:var(--accent,#81d9bd)}.image-edit-status.failed{color:#ffaaa7}
.storyboard-reference-library{margin-top:14px;padding:13px;border:1px solid var(--border,#35423f);border-radius:11px;background:var(--bg,#141918)}.reference-library-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.reference-library-heading p{margin:4px 0 0}.reference-library-heading>div:last-child{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.storyboard-reference-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:10px;margin-top:12px}.reference-image-card{position:relative;min-width:0;border:1px solid var(--border,#35423f);border-radius:10px;overflow:hidden;background:var(--panel,#1e2625)}.reference-image-card.active{border-color:var(--accent,#81d9bd);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent,#81d9bd) 22%,transparent)}.reference-image-card>button:first-child{display:grid;width:100%;height:100%;padding:0;border:0;border-radius:0;background:transparent;text-align:left;overflow:hidden}.reference-image-card img{display:block;width:100%;height:112px;object-fit:cover;background:#080d0c}.reference-image-card span{display:grid;gap:3px;padding:9px 10px 5px;min-width:0}.reference-image-card b,.reference-image-card small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.reference-image-card small{color:var(--muted,#aab8b3);font-size:11px}.reference-image-card em{margin:0 10px 10px;padding:4px 7px;border-radius:6px;background:var(--bg,#141918);color:var(--muted,#aab8b3);font-size:11px;font-style:normal;justify-self:start}.reference-image-card.active em{background:color-mix(in srgb,var(--accent,#81d9bd) 18%,var(--bg,#141918));color:var(--accent,#81d9bd)}.reference-card-delete{position:absolute;top:7px;right:7px;min-height:26px!important;padding:4px 7px!important;border-color:#7b4646!important;background:#241817dd!important;color:#ffb3af!important;font-size:11px!important}.reference-library-empty{grid-column:1/-1;margin:0;padding:18px;text-align:center;color:var(--muted,#aab8b3);border:1px dashed var(--border,#35423f);border-radius:8px}
.video-workspace .scene-reference-assets{margin:18px 0 0;padding:16px;border:1px solid var(--border,#35423f);border-radius:12px;background:color-mix(in srgb,var(--accent,#81d9bd) 4%,var(--bg,#141918))}.scene-reference-assets>summary{margin-bottom:0}.scene-reference-assets>summary>.muted{display:inline-block;margin-left:12px;font-size:13px}.scene-reference-assets[open]>summary{margin-bottom:14px}.scene-reference-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.scene-reference-heading p{margin:0 0 8px;line-height:1.6}.scene-reference-heading>button{flex:0 0 auto}.scene-cost-note{font-size:13px;margin:5px 0 12px}.scene-assets-grid{display:grid;gap:12px}.video-card .scene-asset-card{margin:0;padding:12px;border:1px solid var(--border,#35423f);border-radius:10px;background:var(--panel,#1e2625)}.scene-asset-card>summary{display:flex;align-items:center;gap:12px;margin:0}.scene-asset-card[open]>summary{margin-bottom:14px}.scene-asset-card>summary>img,.scene-thumbnail-placeholder{display:block;flex:0 0 76px;width:76px;height:50px;object-fit:cover;border:1px solid var(--border,#35423f);border-radius:6px;background:#080d0c}.scene-thumbnail-placeholder{display:grid;place-items:center;color:var(--muted,#aab8b3);font-size:12px}.scene-asset-card summary small{display:block;margin-top:5px}.scene-asset-state{margin-left:auto;color:var(--muted,#aab8b3);font-size:12px;white-space:nowrap}.scene-asset-card summary::after{content:'⌄';color:var(--muted,#aab8b3);font-size:20px}.scene-asset-card[open] summary::after{transform:rotate(180deg)}.scene-asset-card .storyboard-result img{max-height:360px}.scene-asset-card textarea{min-height:100px;max-height:360px}.shot-scene-reference{display:flex;align-items:center;gap:14px;padding:12px;margin:0 0 16px;border:1px solid var(--border,#35423f);border-radius:10px;background:color-mix(in srgb,var(--accent,#81d9bd) 5%,var(--bg,#141918))}.shot-scene-reference>img{display:block;width:112px;height:72px;object-fit:cover;border-radius:7px;flex:0 0 auto}.shot-scene-reference p{margin:5px 0;line-height:1.5;font-size:13px}.video-card label.scene-reference-toggle{display:flex;align-items:center;gap:8px;margin:8px 0 0;font-size:13px}.video-card .scene-reference-toggle input{width:15px;height:15px;margin:0;accent-color:var(--accent,#81d9bd)}
@media(max-width:900px){.video-workspace .video-editor aside{max-height:240px}}
@media(max-width:900px){.video-workspace .video-next-stage{align-items:flex-start;flex-direction:column}.storyboard-edit-heading{flex-direction:column}.storyboard-edit-heading select{width:100%}.scene-reference-heading{flex-direction:column;gap:8px}.scene-reference-assets>summary>.muted{display:block;margin:7px 0 0}.shot-scene-reference{align-items:flex-start}.shot-scene-reference>img{width:88px;height:60px}.scene-asset-state{display:none}.video-boundary-grid{grid-template-columns:1fr}.video-boundary-listen span{width:100%;margin-left:0}.video-boundary-editor>header{align-items:flex-start}}
.motion-narration-audio{display:none}.motion-audio-toggle{justify-self:start;display:flex;align-items:center;gap:8px}.motion-audio-toggle input{width:auto;margin:0}
.export-audio-option{display:flex;align-items:flex-start;gap:10px;margin:16px 0;padding:12px 14px;border:1px solid var(--border,#35423f);border-radius:10px;background:#1d2825}.export-audio-option input{width:17px;height:17px;margin:2px 0 0}.export-audio-option span{display:grid;gap:3px}.export-audio-option small{color:var(--muted,#aab8b3);line-height:1.5}
.motion-heading-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap}
.motion-upload-button{display:inline-flex;align-items:center;padding:9px 13px;border:1px solid var(--border,#35423f);border-radius:8px;cursor:pointer;font-size:13px;color:var(--text,#eef3f1);background:var(--panel,#1e2625)}.motion-upload-button:hover{border-color:var(--accent,#81d9bd)}.motion-upload-button.disabled{opacity:.5;cursor:not-allowed}.motion-upload-button input{display:none}.motion-history{padding:14px;border:1px solid var(--border,#35423f);border-radius:11px;margin:14px 0}.motion-history>summary{cursor:pointer;font-weight:700}.motion-history-list{display:grid;gap:10px;margin-top:12px}.motion-history-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px;border:1px solid var(--border,#35423f);border-radius:9px;background:var(--bg,#141918)}.motion-history-row>span{display:grid;gap:4px}.motion-history-row small{color:var(--muted,#aab8b3)}.motion-history-row video{grid-column:1/-1;width:100%;max-height:480px;background:#070b0a;border-radius:8px}.motion-history-row .actions{display:flex;gap:8px;flex-wrap:wrap}
.reference-image-card img{height:128px;object-fit:contain}
</style>
<style scoped>
/* Dynamic workspace: persistent navigation, bounded previews and a single reading column. */
.video-workspace{max-width:1600px;margin-inline:auto;padding-bottom:40px;--workspace-gap:24px}
.workspace-project-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:18px}.workspace-project-heading h1{margin:4px 0;font-size:25px;line-height:1.35;overflow-wrap:anywhere}.workspace-project-heading .eyebrow{margin:0;font-size:11px;letter-spacing:.12em;color:var(--muted)}.workspace-project-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;flex-shrink:0}.workspace-draft{font-size:12px;color:#e6ba62}
.video-stage-nav{position:sticky;top:12px;z-index:15;margin:0 0 24px;box-shadow:0 8px 24px #0002;background:var(--panel,#1e2625)}.video-stage-nav button{padding:10px;transition:background .15s,border-color .15s}.video-stage-nav button:hover{background:color-mix(in srgb,var(--accent,#81d9bd) 7%,transparent)}.video-stage-nav b{font-size:13px}.video-stage-nav small{font-size:10px;margin-top:3px}
.workspace-stage-heading{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:18px}.workspace-stage-heading h2{margin:0 0 6px;font-size:20px}.workspace-stage-heading p{margin:0;font-size:13px;line-height:1.65}.video-card{padding:22px;border-radius:14px}.video-card .muted{line-height:1.65}.video-workspace button:focus-visible,.video-workspace summary:focus-visible{outline:2px solid var(--accent,#81d9bd);outline-offset:3px}.video-workspace button{min-height:36px}.video-workspace .video-editor{grid-template-columns:248px minmax(0,1fr);gap:var(--workspace-gap);padding-top:20px;border-top:1px solid var(--border,#35423f)}
.video-workspace .video-editor>.shot-navigator{max-height:calc(100vh - 132px);overflow:hidden}.video-detail{background:color-mix(in srgb,var(--bg,#141918) 25%,transparent);padding:20px;border:1px solid var(--border,#35423f);border-radius:12px}.shot-local-navigation{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}.shot-local-navigation strong{font-size:14px}.shot-local-navigation strong span{font-weight:400;color:var(--muted);font-size:12px}.shot-local-navigation>div{display:flex;gap:6px}.shot-local-navigation button{font-size:12px;padding:6px 10px;min-height:30px}
.video-workspace .shot-subtitles{padding:12px 14px;margin-bottom:16px;font-size:13px;background:var(--panel,#1e2625)}.shot-subtitles[open]{max-height:230px;overflow:auto}.video-workspace .shot-subtitles p{line-height:1.8}.video-workspace .storyboard-result{min-height:120px;background:#0c100f}.video-workspace .storyboard-result img{max-height:min(56vh,560px);width:100%;object-fit:contain}.motion-preview video{max-height:58vh;min-height:180px}.video-workspace .video-detail label{font-size:13px;line-height:1.5}.video-workspace .video-detail textarea{font-size:13px;line-height:1.8;padding:12px;min-height:96px;height:148px}.video-workspace .video-detail .motion-field textarea{height:112px}.video-workspace .video-detail .video-prompt-field textarea{height:160px}.video-workspace .video-detail input{font-size:13px}.video-row{flex-wrap:wrap}.video-row>p{margin-left:auto;font-size:12px}
.motion-heading{padding-bottom:15px;margin-bottom:14px;border-bottom:1px solid var(--border,#35423f)}.motion-heading .eyebrow{display:none}.motion-heading h2{font-size:16px}.motion-heading p{font-size:12px}.motion-generation-bar{padding:14px;gap:16px}.motion-generation-bar strong{font-size:14px}.motion-generation-bar p{font-size:12px;max-width:580px}.motion-batch-actions button{font-size:13px;white-space:nowrap}.motion-shot-heading{flex-direction:column;gap:12px}.motion-shot-heading h3{font-size:16px}.motion-shot-heading p{font-size:13px}.motion-heading-actions{width:100%;justify-content:flex-start;gap:8px}.motion-heading-actions .readonly-badge{margin-right:auto}.motion-heading-actions button,.motion-upload-button{font-size:12px}.motion-preview{grid-template-columns:minmax(0,1fr) auto;align-items:center}.motion-preview video{grid-column:1/-1}.video-card label.motion-audio-toggle{display:flex;align-items:center;margin:0;gap:8px}.video-card .motion-audio-toggle input{width:16px;height:16px;padding:0;accent-color:var(--accent,#81d9bd)}.motion-download{font-size:12px;padding:7px 10px}.motion-task-id{font-size:11px}.motion-preview-note{font-size:12px;line-height:1.7}.motion-core-reference,.motion-request-review,.motion-history{font-size:13px;background:var(--panel,#1e2625)}
.video-workspace .planning-log{padding:10px 12px;border:1px solid var(--border,#35423f);border-radius:8px;margin:12px 0;background:var(--bg,#141918)}.video-workspace .planning-log .video-logs{max-height:120px;line-height:1.7;font-size:12px}.video-workspace .video-source-summary{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-bottom:14px;margin-bottom:16px;border-bottom:1px solid var(--border,#35423f)}.video-source-summary p{font-size:12px;margin:0}.video-source-summary audio{width:290px;flex-shrink:0}.video-project>header h2{font-size:16px;margin:0 0 6px}.video-project>header p{font-size:12px;margin:0}.video-header-actions{flex-wrap:wrap}.video-workspace .video-next-stage{font-size:13px}.video-next-stage button{font-size:13px;flex-shrink:0}.video-workspace .scene-reference-assets{padding:12px;font-size:13px}.readonly-stage-note{font-size:12px;line-height:1.6}.storyboard-edit-tools,.single-shot-refresh{font-size:13px}.single-shot-refresh button,.storyboard-edit-actions button{font-size:12px}.prompt-warning-index{font-size:13px}.prompt-warning-index>button{max-width:200px}.export-preview-switch{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px}.export-preview-switch button.active{background:color-mix(in srgb,var(--accent,#81d9bd) 12%,var(--panel,#1e2625));border-color:var(--accent,#81d9bd);color:var(--accent,#81d9bd)}.export-preview-switch span{font-size:12px;margin-left:auto}.video-history-panel>video{margin-top:0;max-height:62vh}.video-card label.export-audio-option{display:flex;gap:10px}.video-card .export-audio-option input{width:17px;flex-shrink:0}
@media(min-width:1500px){.video-workspace .video-editor{grid-template-columns:270px minmax(0,1fr)}.video-detail{padding:24px}}
@media(max-width:1100px){.workspace-project-heading{align-items:flex-start}.workspace-project-actions{max-width:280px}.motion-generation-bar{align-items:flex-start;flex-direction:column}.motion-batch-actions{width:100%}.video-workspace .video-editor{grid-template-columns:210px minmax(0,1fr);gap:16px}.video-detail{padding:14px}}
@media(max-width:900px){.video-stage-nav{top:0;display:flex;overflow-x:auto;gap:4px;padding:5px;border-radius:10px}.video-stage-nav button{flex:0 0 125px}.video-workspace .video-editor{grid-template-columns:1fr}.video-workspace .video-editor>.shot-navigator{max-height:none}.workspace-project-heading{flex-direction:column;gap:10px}.workspace-project-actions{max-width:none;justify-content:flex-start}.video-card{padding:16px}.video-workspace .video-source-summary{align-items:flex-start;flex-direction:column;gap:8px}.motion-preview{grid-template-columns:1fr}.motion-download{justify-self:start}.motion-batch-actions button{width:auto;flex:1}.video-next-stage button{width:100%}.workspace-stage-heading .readonly-badge{display:none}.video-workspace .shot-subtitles{max-height:260px}.motion-history-row{grid-template-columns:1fr}.export-preview-switch span{width:100%;margin:4px 0}.video-row>p{margin-left:0}}
@media(prefers-reduced-motion:reduce){.video-workspace *{transition:none!important;scroll-behavior:auto!important}}
.motion-preview>p,.motion-preview>button{grid-column:1/-1;min-width:0;max-width:100%}.motion-preview>button{justify-self:start}.motion-preview>p{overflow-wrap:anywhere}
.auto-pilot-status{display:flex;align-items:center;gap:12px;margin:-4px 0 18px;padding:11px 14px;border:1px solid color-mix(in srgb,var(--accent,#81d9bd) 42%,var(--border,#35423f));border-radius:10px;background:color-mix(in srgb,var(--accent,#81d9bd) 7%,var(--panel,#1e2625))}.auto-pilot-dot{width:9px;height:9px;flex:0 0 auto;border-radius:50%;background:var(--accent,#81d9bd);box-shadow:0 0 0 5px color-mix(in srgb,var(--accent,#81d9bd) 14%,transparent)}.auto-pilot-status strong{font-size:13px}.auto-pilot-status p{margin:3px 0 0;color:var(--muted,#aab8b3);font-size:11px}
</style>
