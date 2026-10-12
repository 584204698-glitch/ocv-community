<script setup>
import {ref,onBeforeUnmount} from 'vue'
import {requestJSON} from '../api.js'
import {activeProjectStatuses} from '../projectLifecycle.js'
const props=defineProps({project:{type:Object,required:true},disabled:Boolean})
const emit=defineEmits(['changed']);const busy=ref(false),error=ref('');let controller
onBeforeUnmount(()=>controller?.abort())
async function archive(){
 if(busy.value||props.disabled)return
 const restore=Boolean(props.project.archived),name=props.project.settings?.name||props.project.id
 if(!restore&&!window.confirm(`将“${name}”移入回收站？可以恢复，已有配音和画面会保留。`))return
 busy.value=true;error.value='';controller=new AbortController()
 try{await requestJSON('/api/video-studio/'+props.project.id+'/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.project.revision,archived:!restore}),signal:controller.signal});if(!controller.signal.aborted)emit('changed')}
 catch(e){if(e.name!=='AbortError')error.value=e.message||'操作失败，请刷新后重试'}finally{busy.value=false}
}
</script>
<template><div class="project-archive-action"><button type="button" :disabled="busy||disabled||activeProjectStatuses.includes(project.status)" @click="archive">{{busy?'处理中…':project.archived?'恢复项目':'删除项目'}}</button><span v-if="error" role="alert">{{error}}</span></div></template>
<style scoped>
:global(.project-row-actions){display:flex;flex-wrap:wrap;gap:8px;align-items:center}
@media(max-width:700px){:global(.live-studio .project-row-actions){grid-column:1/-1;min-width:0;justify-content:flex-end}:global(.live-studio .filters){flex-wrap:wrap}}
.project-archive-action{display:inline-flex;flex-direction:column;gap:5px}.project-archive-action button{min-height:44px;padding:8px 12px;border:1px solid #aeb6c6;border-radius:8px;background:transparent;color:inherit;cursor:pointer;white-space:nowrap}.project-archive-action span{font-size:12px;color:#d44;max-width:240px}.project-archive-action button:disabled{opacity:.5}
</style>
