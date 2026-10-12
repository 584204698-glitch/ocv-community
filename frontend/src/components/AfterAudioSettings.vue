<script setup>
import {ref,onBeforeUnmount} from 'vue'
import {requestJSON} from '../api.js'
import {audioSettingsDraft,activeProjectStatuses} from '../projectLifecycle.js'
const props=defineProps({job:Object,disabled:Boolean})
const emit=defineEmits(['edit-config'])
const busy=ref(false),error=ref('');let controller
onBeforeUnmount(()=>controller?.abort())
async function edit(){
 if(busy.value||props.disabled||!props.job?.id)return
 busy.value=true;error.value='';controller=new AbortController()
 try{const draft=await audioSettingsDraft(props.job,requestJSON,controller.signal);if(!controller.signal.aborted)emit('edit-config',draft)}
 catch(e){if(e.name!=='AbortError')error.value=e.message||'无法读取原项目，请重试'}
 finally{busy.value=false}
}
</script>
<template>
 <div v-if="job?.request?.dynamic_video" class="after-audio-settings">
  <button type="button" :disabled="disabled||busy||activeProjectStatuses.includes(job.status)" @click="edit">{{busy?'正在读取原项目…':'返回修改画面设置'}}</button>
  <small>保留已生成的配音和字幕，修改画风、镜头节奏后继续制作，无需新建项目。</small>
  <p v-if="error" role="alert">{{error}}</p>
 </div>
</template>
<style scoped>
.after-audio-settings{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:12px 0}.after-audio-settings button{min-height:44px;padding:8px 14px;border:1px solid #7a86c7;border-radius:9px;color:inherit;background:transparent;cursor:pointer}.after-audio-settings small{font-size:13px;line-height:1.6;max-width:560px}.after-audio-settings p{width:100%;color:#d44}.after-audio-settings button:disabled{opacity:.5}
</style>
