<script setup>
import {computed,ref} from 'vue'
const props=defineProps({project:Object})
const emit=defineEmits(['recovered'])
const busy=ref(false),error=ref('')
const count=computed(()=>props.project?.project_cast?.characters?.length||0)
const recoverable=computed(()=>!count.value&&!props.project?.references?.length&&['draft','storyboard_review','generation_ready','image_review','video_generation_ready','video_review'].includes(props.project?.status))
async function recover(){
 if(!window.confirm('将设计本故事的角色图，并重新规划角色在各镜中的出场。已有配音和镜头时间轴保留，旧图片保存到历史记录；会调用已配置的语言和图片接口。继续吗？'))return
 busy.value=true;error.value=''
 const id=props.project.id
 try{
  const response=await fetch('/api/video-studio/'+id+'/auto-characters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.project.revision})})
  const result=await response.json()
  if(!response.ok)throw new Error(result.detail||'角色设计未启动')
  if(props.project.id===id)emit('recovered',result)
 }catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<template>
 <section v-if="project" class="auto-cast-notice">
  <template v-if="count"><strong>已绑定 {{count}} 个项目角色</strong><span>{{project.project_cast.source==='auto_designed_project_identity'?'由系统按本故事设计，可在“项目角色”中查看和修改。':'分镜按出场情况使用已绑定的角色参考。'}}</span></template>
  <template v-else><strong>{{project.status==='planning'&&project.creation_parameters?.auto_character_design?'正在准备角色与分镜':'尚未绑定角色图'}}</strong><span>只有画风或文字描述不能锁定角色外貌。{{project.references?.length?'请在“项目角色”中绑定已上传的参考图。':'自动设计会先生成本故事的角色图，再用于分镜规划。'}}</span><button v-if="recoverable" type="button" :disabled="busy" @click="recover">{{busy?'正在启动…':'补齐角色并重新规划'}}</button></template>
  <span v-if="error" role="alert">{{error}}</span>
 </section>
</template>
<style scoped>
.auto-cast-notice{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:14px;margin:12px 0;border:1px solid var(--ocv-line,#ffffff25);border-radius:10px;font-size:13px}.auto-cast-notice span{line-height:1.6;flex:1;min-width:180px}.auto-cast-notice button{padding:8px 12px}.auto-cast-notice [role=alert]{color:#f99}
</style>
