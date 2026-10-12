<script setup>
import {ref,onMounted,onUnmounted} from 'vue'
import {requestJSON} from '../api'
const props=defineProps({jobId:{type:String,required:true},disabled:Boolean})
const emit=defineEmits(['project-ready','changed','state'])
const state=ref(null),error=ref(''),pollError=ref(''),busy=ref(false)
let timer,disposed=false,pending=false,notified='',epoch=0
const labels={plan:'规划分镜和角色',review:'检查分镜',images:'生成核心图',confirm_images:'确认画面',videos:'生成动态镜头',export:'合成成片'}
function accept(value){
 state.value=value
 emit('state',value)
 if(value.project_id&&value.project_id!==notified){notified=value.project_id;emit('project-ready',value.project_id)}
}
async function refresh(){
 if(pending||disposed||busy.value)return
 const serial=epoch
 pending=true
 try{const value=await requestJSON('/api/one-click/'+encodeURIComponent(props.jobId));if(!disposed&&serial===epoch){accept(value);pollError.value=''}}
 catch(e){if(!disposed&&serial===epoch)pollError.value=e.message}
 finally{pending=false}
}
async function control(action){
 if(busy.value||props.disabled)return
 ++epoch;busy.value=true;error.value=''
 try{
  const value=await requestJSON('/api/one-click/'+encodeURIComponent(props.jobId),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})})
  if(!disposed){accept(value);emit('changed')}
 }catch(e){if(!disposed)error.value=e.message}
 finally{if(!disposed)busy.value=false}
}
onMounted(()=>{refresh();timer=setInterval(refresh,2500)})
onUnmounted(()=>{disposed=true;clearInterval(timer)})
</script>
<template>
 <section class="one-click-status" aria-label="后台一键制作">
  <div><strong>{{state?.state==='completed'?'成片已完成':state?.state==='running'?'工作站正在自动制作':state?.state==='paused'?'一键制作已暂停':'后台一键制作'}}</strong>
   <p v-if="state?.state==='running'">可以切换项目或关闭网页，工作站会继续。{{labels[state.last_action]||(state.kind==='native_story'?'准备故事角色与对话':'准备配音和字幕')}}。</p>
   <p v-else-if="state">{{state.message}}</p><p v-else>正在读取任务状态…</p>
   <p v-if="state?.state==='unmanaged'">继续后将自动确认各阶段并制作成片，失败时停下，不会自动反复重试。</p>
   <p v-if="error" role="alert">{{error}}</p>
   <p v-if="pollError" role="status">状态读取暂时失败，正在自动重连：{{pollError}}</p>
  </div>
  <button v-if="state?.state==='running'" type="button" :disabled="busy||disabled" @click="control('pause')">暂停后编辑</button>
  <button v-else-if="state&&state.state!=='completed'" type="button" :disabled="busy||disabled" @click="control('resume')">{{busy?'正在继续…':'继续一键制作'}}</button>
 </section>
</template>
<style scoped>
.one-click-status{display:flex;align-items:center;justify-content:space-between;gap:16px;border:1px solid var(--border,#40504a);background:var(--panel,#202b28);padding:14px 18px;border-radius:12px;margin:12px 0}.one-click-status p{font-size:13px;line-height:1.6;color:var(--muted,#9aaba5);margin:5px 0 0}.one-click-status [role=alert]{color:#efb1a2}.one-click-status button{flex-shrink:0}@media(max-width:680px){.one-click-status{align-items:stretch;flex-direction:column}}
</style>
