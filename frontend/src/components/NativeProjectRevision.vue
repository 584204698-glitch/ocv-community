<script setup>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { requestJSON } from '../api'
const props=defineProps({project:Object,shot:Object,accountKey:String,disabled:Boolean})
const emit=defineEmits(['updated','busy'])
const instruction=ref('检查全片各镜头的时长与动作安排，根据故事含义精简为可执行的直接物理动作，合理安排说话与反应时间。保留故事、逐字台词、人物音色与核心图片，不新增剧情。')
const busy=ref(false),error=ref(''),review=ref(null),selected=ref([])
const mode=ref('project')
const repairSourceStates=ref(false)
async function syncStateReferences(){
 if(locked.value)return
 const serial=epoch,current=scope();busy.value=true;error.value=''
 try{
  const result=await requestJSON(`/api/video-studio/${encodeURIComponent(props.project.id)}/state-references/sync`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.project.revision})})
  if(!disposed&&serial===epoch&&current===scope()){review.value=null;selected.value=[];emit('updated',result)}
 }catch(failure){if(!disposed&&serial===epoch&&current===scope())error.value=failure.message}
 finally{if(!disposed&&serial===epoch&&current===scope())busy.value=false}
}
async function changeNarrationRoute(route){
 if(locked.value)return
 const serial=epoch,current=scope()
 busy.value=true;error.value=''
 try{
  const result=await requestJSON(`/api/video-studio/${encodeURIComponent(props.project.id)}/narration-route`,{
   method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.project.revision,route})})
  if(!disposed&&serial===epoch&&current===scope()){review.value=null;selected.value=[];emit('updated',result)}
 }catch(failure){if(!disposed&&serial===epoch&&current===scope())error.value=failure.message}
 finally{if(!disposed&&serial===epoch&&current===scope())busy.value=false}
}
async function changeDialogueRoute(route,tts_engine=props.project.creation_parameters?.dialogue_tts_engine||'indextts25'){
 if(locked.value)return
 const serial=epoch,current=scope()
 busy.value=true;error.value=''
 try{
  const result=await requestJSON(`/api/video-studio/${encodeURIComponent(props.project.id)}/dialogue-route`,{
   method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.project.revision,route,tts_engine})})
  if(!disposed&&serial===epoch&&current===scope()){review.value=null;selected.value=[];emit('updated',result)}
 }catch(failure){if(!disposed&&serial===epoch&&current===scope())error.value=failure.message}
 finally{if(!disposed&&serial===epoch&&current===scope())busy.value=false}
}
function prepareH3Review(){
 if(locked.value)return
 mode.value=props.shot?.native_dialogue?'shot':'project'
 instruction.value='按H3的可执行能力优化所选范围：每个节拍围绕一个主要物理变化，明确谁说话、谁听、说完后闭嘴继续什么动作。台词中的比喻和称呼不能变成新人物、头像或会说话的道具。按逐字台词和必要动作核算时长，不用等待凑满长镜。参考音频只提供对应角色声线，不提供额外台词。保留逐字台词、角色身份、音色、现有核心图及事件顺序。核心图与动作冲突或必须拆镜时明确给出建议，不偷偷改变开场或删除事件。'
 review.value=null;selected.value=[];error.value=''
}
let epoch=0,disposed=false
const scope=()=>`${props.accountKey}:${props.project?.id}`
watch(scope,()=>{epoch++;review.value=null;selected.value=[];error.value='';busy.value=false;repairSourceStates.value=false},{flush:'sync'})
watch(()=>`${mode.value}:${mode.value==='shot'?props.shot?.id:''}`,()=>{review.value=null;selected.value=[];error.value=''},{flush:'sync'})
watch(busy,value=>emit('busy',value),{flush:'sync'})
const stale=computed(()=>review.value&&review.value.project_revision!==props.project?.revision)
const locked=computed(()=>props.disabled||busy.value)
const total=computed(()=>review.value?.shots?.reduce((sum,row)=>sum+Number(row.duration||0),0)||0)
const originalTotal=computed(()=>review.value?.shots?.reduce((sum,row)=>sum+Number(row.old_duration||0),0)||0)
const speakerName=(row,id)=>props.project?.shots?.find(shot=>shot.id===row.id)?.native_dialogue?.speakers?.find(speaker=>speaker.character_id===id)?.name||id
async function run(apply=false){
 if(locked.value||apply&&(stale.value||!selected.value.length))return
 const serial=epoch,current=scope(),revision=props.project.revision
 busy.value=true;error.value=''
 try{
 const body=apply?{revision,review_id:review.value.review_id,shot_ids:[...selected.value]}:{revision,instruction:instruction.value.trim(),repair_source_states:repairSourceStates.value&&mode.value==='project',...(mode.value==='shot'?{shot_ids:[props.shot.id]}:{})}
  const result=await requestJSON(`/api/video-studio/${encodeURIComponent(props.project.id)}/dynamic-review${apply?'/apply':''}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  if(disposed||serial!==epoch||current!==scope())return
  if(apply){review.value=null;selected.value=[];emit('updated',result)}
  else{review.value=result;selected.value=result.shots.filter(row=>row.changed).map(row=>row.id)}
 }catch(failure){if(!disposed&&serial===epoch&&current===scope())error.value=failure.message}
 finally{if(!disposed&&serial===epoch&&current===scope())busy.value=false}
}
onBeforeUnmount(()=>{disposed=true;epoch++;emit('busy',false)})
</script>
<template>
 <section class="project-revision">
  <h3>全片动态方案二次编辑</h3>
  <button v-if="project.shots.some(s=>s.core_reference_used)" type="button" :disabled="locked" @click="syncStateReferences">同步已修正的道具状态引用</button>
  <div v-if="['dialogue','mixed'].includes(project.creation_parameters?.speech_mode)">
   <label>角色对白生成路线
    <select :value="project.creation_parameters?.dialogue_route||'h3_native'" :disabled="locked" @change="changeDialogueRoute($event.target.value)">
     <option value="external_tts">音频驱动与后期配音</option>
     <option value="h3_native">H3 原生对白（可选）</option>
    </select>
   </label>
   <label v-if="project.creation_parameters?.dialogue_route==='external_tts'">配音引擎
    <select :value="project.creation_parameters?.dialogue_tts_engine||'indextts25'" :disabled="locked" @change="changeDialogueRoute('external_tts',$event.target.value)">
     <option value="indextts25">IndexTTS2.5</option><option value="qwen3">Qwen3-TTS · 克隆已采用音色</option>
    </select>
   </label>
   <p>准确配音先使用已采用的角色音色合成台词，再按实测音频安排时间轴；H3 生成画面与嘴部动作，口型仍需观看确认。切换不会立即生成，相关镜头需重新生成。</p>
  </div>
  <p v-if="project.native_narrator&&project.creation_parameters?.dialogue_route==='external_tts'">旁白使用所选配音引擎，独立合成并按实测位置加入成片，不传给 H3 作为口型约束。</p>
  <div v-else-if="project.native_narrator">
   <label>旁白生成路线
    <select :value="project.creation_parameters?.narration_route||'h3_native'" :disabled="locked" @change="changeNarrationRoute($event.target.value)">
     <option value="h3_native">H3 原生旁白</option>
     <option value="external_tts">独立旁白 · IndexTTS2.5</option>
    </select>
   </label>
   <p>独立旁白先合成并测量时长，再生成画面；角色对白由单独的对白路线设置决定。切换不立即生成，含旁白镜头需重做；首次使用请先验证单镜。旁白需采用固定音色。</p>
  </div>
  <label>编辑范围<select v-model="mode" :disabled="locked"><option value="project">全项目二次编辑</option><option value="shot" :disabled="!shot?.native_dialogue">本镜二次编辑</option></select></label>
  <label v-if="mode==='project'"><input v-model="repairSourceStates" type="checkbox" :disabled="locked" @change="review=null;selected=[]">依据采用稿修正错误的道具状态描述</label>
  <p v-if="mode==='project'&&repairSourceStates" class="muted">只纠正与原文冲突的状态，先展示原文依据。共享状态需一起采用；仅涉及画外状态的镜头保留原视频，动作或可见开场发生变化的镜头需重做。</p>
  <p>语言模型检查全部镜头的时长与动作，先预览建议，再选择采用。保留核心图、人物音色和逐字台词；采用后需要重新生成的片段由你手动生成。</p>
  <button type="button" :disabled="locked" @click="prepareH3Review">填入 H3 表达优化意见</button>
  <p class="muted">针对多词、说错人、道具变脸和动作过密检查方案；有当前镜头时默认检查本镜。此按钮只填写意见，不调用模型。图片本身有问题时，需先在核心图步骤局部修正。</p>
  <label>本次修改意见<textarea v-model="instruction" rows="3" maxlength="12000" :disabled="locked" /></label>
  <p class="muted">保留现有 {{project.shots.length}} 个镜头；按建议调整每镜 4～15 秒的时长与台词时间。</p>
  <button type="button" :disabled="locked||!instruction.trim()" @click="run()">{{busy?'处理中…':mode==='shot'?'检查本镜并预览建议':'检查全片并预览建议'}}</button>
  <p class="muted">检查会调用语言 API；检查和采用均不会启动 GPU 视频生成。</p>
  <p v-if="error" role="alert">{{error}}</p>
  <template v-if="review">
   <p>{{review.summary}}</p>
   <article v-for="(correction,index) in review.state_corrections||[]" :key="'state-'+index"><strong>原稿状态修正 · {{correction.prop_id}} / {{correction.state_id}}</strong><p>原文：{{correction.source_quote}}</p><p>{{correction.reason}}</p><p>修正为：{{correction.description}}</p><p v-if="correction.visible_change===false">保留已有画面依据：{{correction.visual_reason}}</p><p class="muted">核心图保留；生成前需核对是否符合修正状态。</p></article>
   <p>{{mode==='shot'?'本镜':'全片'}}建议时长：{{originalTotal.toFixed(1)}} → {{total.toFixed(1)}} 秒</p>
   <p v-for="(warning,index) in review.warnings" :key="index" role="status">{{warning}}</p>
   <p v-if="stale" role="alert">项目已发生变化，请重新检查后再采用建议。</p>
   <article v-for="(row,index) in review.shots" :key="row.id">
    <label class="revision-selection"><input v-model="selected" type="checkbox" :value="row.id" :disabled="locked||stale||!row.changed">第 {{project.shots.findIndex(shot=>shot.id===row.id)+1}} 镜 · {{row.old_duration}} → {{row.duration}} 秒 · {{row.changed?'有修改建议':'保持原方案'}}</label>
    <p>{{row.reason}}</p><p>{{row.action}}</p><p v-if="row.changed&&row.render_changed===false" class="muted">仅更新画外状态说明，保留原视频。</p>
    <details><summary>查看建议台词时间与动作节拍</summary>
     <p v-for="(turn,i) in row.turns" :key="'turn'+i">{{turn.start}}～{{turn.end}} 秒 · {{speakerName(row,turn.character_id)}}：{{turn.text}}</p>
     <p v-for="(beat,i) in row.performance_beats" :key="'beat'+i">{{beat.start}}～{{beat.end}} 秒 · {{beat.action_zh}}</p>
    </details>
   </article>
   <button type="button" :disabled="locked||stale||!selected.length" @click="run(true)">采用所选 {{selected.length}} 镜建议</button>
   <p class="muted">只采用勾选镜头的方案；原片段保留在历史记录中。</p>
  </template>
 </section>
</template>
<style scoped>
.project-revision{border:1px solid var(--border,#40504a);border-radius:12px;padding:16px;margin:12px 0}.project-revision p{line-height:1.6}.project-revision>label{display:grid;gap:8px;margin-bottom:12px}.project-revision textarea{width:100%;box-sizing:border-box}.project-revision article{border-top:1px solid var(--border,#40504a);padding:14px 0}.revision-selection{display:flex;gap:8px;align-items:center}[role=alert]{color:#efb1a2}
</style>
