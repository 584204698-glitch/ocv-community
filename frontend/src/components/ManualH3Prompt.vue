<script setup>
import {ref,watch} from 'vue'
import {requestJSON} from '../api'
const props=defineProps({projectId:String,shot:Object,revision:Number,disabled:Boolean})
const emit=defineEmits(['updated','busy-change'])
const text=ref(''),open=ref(false),saving=ref(false),error=ref(''),note=ref('')
function resetDraft(){text.value=props.shot.manual_h3_prompt?.prompt||props.shot.video_request?.prompt||props.shot.h3_prompt||'';error.value='';note.value=''}
watch(()=>props.projectId+'|'+props.shot.id,()=>{open.value=false;resetDraft()},{immediate:true})
async function save(reset=false){
 if(saving.value||props.disabled)return
 saving.value=true;emit('busy-change',true);error.value='';note.value=''
 try{
  const result=await requestJSON('/api/video-studio/'+encodeURIComponent(props.projectId)+'/videos/'+encodeURIComponent(props.shot.id)+'/manual-h3-prompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.revision,prompt:text.value,reset})})
  emit('updated',result);note.value=reset?'已恢复自动提示词。':'已采用，下次重新生成本镜时直接提交。当前视频没有重跑。'
 }catch(e){error.value=e.message}
 finally{saving.value=false;emit('busy-change',false)}
}
async function copy(){try{await navigator.clipboard.writeText(text.value);note.value='已复制'}catch{note.value='请在文本框内全选复制'}}
async function loadCompiled(){
 if(saving.value||props.disabled)return
 const projectId=props.projectId,shotId=props.shot.id,revision=props.revision
 saving.value=true;emit('busy-change',true);error.value='';note.value=''
 try{
  const result=await requestJSON('/api/video-studio/'+encodeURIComponent(projectId)+'/videos/'+encodeURIComponent(shotId)+'/manual-h3-prompt')
  if(props.projectId!==projectId||props.shot.id!==shotId)return
  if(props.revision!==revision||result.revision!==revision)throw new Error('项目已变化，请刷新后重新载入当前绑定稿。')
  text.value=result.prompt;note.value='已载入当前图片与声音绑定的自动稿，尚未采用。请重新粘贴核对过的动作修改，并保留新角色定义和台词。'
 }catch(e){error.value=e.message}
 finally{saving.value=false;emit('busy-change',false)}
}
</script>
<template>
 <details class="manual-h3" @toggle="open=$event.target.open">
  <summary>手动编辑 H3 最终提示词 {{shot.manual_h3_prompt?'· 已采用手动稿':''}}</summary>
  <div v-if="open">
   <p>可复制给其他 AI 优化，再粘贴回来。保持 subject_definitions 到 summary 之前的角色、图号和声音定义，以及原 &lt;d&gt; 台词块不变；主要编辑动作和镜头描述。不要把旁白写成角色对白。</p>
   <textarea v-model="text" rows="16" :disabled="disabled||saving" aria-label="手动 H3 最终提示词" />
   <p>{{text.length}} 字符 · 采用只保存，下一次重新生成才生效；不会自动转写或再调用导演 Agent。已有配音音轨和参考图沿用当前绑定。结构检查不能保证 H3 画面和口型正确。</p>
   <div class="actions"><button type="button" @click="copy">复制当前稿</button><button type="button" :disabled="disabled||saving" @click="loadCompiled">载入当前绑定自动稿（不采用）</button><button type="button" :disabled="disabled||saving||!text.trim()" @click="save(false)">{{saving?'正在保存…':'采用手动稿（不立即生成）'}}</button><button type="button" :disabled="disabled||saving" @click="save(true)">恢复自动提示词</button><button type="button" :disabled="saving" @click="resetDraft">撤回未采用编辑</button></div>
   <p v-if="error" role="alert">{{error}}</p><p v-if="note" role="status">{{note}}</p>
  </div>
 </details>
</template>
<style scoped>
.manual-h3{padding:14px;border:1px solid var(--border,#48534f);border-radius:10px;margin:14px 0}.manual-h3 p{line-height:1.7;font-size:13px}.manual-h3 textarea{width:100%;height:360px!important;box-sizing:border-box;font-family:monospace}.actions{display:flex;flex-wrap:wrap;gap:8px}
</style>
