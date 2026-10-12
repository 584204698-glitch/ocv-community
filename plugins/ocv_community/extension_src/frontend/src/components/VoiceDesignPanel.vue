<script setup>
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {requestJSON} from '../api'
import {voiceDesignPresets,voicePresetGroups,voicePresetAuditionText} from '../voiceDesignPresets'
import {importDesignedVoice} from '../voiceReferenceTransfer'
import {voiceLibraryPage} from '../voiceLibrary'

const props=defineProps({voiceId:{type:String,default:''},indexTtsEnabled:{type:Boolean,default:false},nativeH3:{type:Boolean,default:false},externalDialogue:{type:Boolean,default:false},projectId:{type:String,default:''},characterId:{type:String,default:''},characterName:{type:String,default:''},boundVoiceIds:{type:Array,default:()=>[]},adoptVoice:{type:Function,default:null}})
const emit=defineEmits(['select','select-index','adopting'])
const name=ref(''),description=ref(''),text=ref(voicePresetAuditionText)
const voices=ref([]),ready=ref(false),busy=ref(false),error=ref(''),loading=ref(true)
let timer,disposed=false,serial=0,pendingRequest=null,transferController=null
const transferring=ref('')
const query=ref(''),scope=ref(props.projectId?'project':'all'),page=ref(1),preference=ref(''),reason=ref(''),agentBusy=ref(false)
const library=computed(()=>voiceLibraryPage(voices.value,{query:query.value,scope:scope.value,projectId:props.projectId,boundIds:props.boundVoiceIds,page:page.value}))
const currentVoice=computed(()=>voices.value.find(v=>v.voice_id===props.voiceId))
watch([query,scope],()=>{page.value=1})
watch(()=>library.value.page,value=>{page.value=value})
const generating=computed(()=>voices.value.some(v=>v.status==='generating'))
const selectedPreset=computed(()=>voiceDesignPresets.find(v=>v.name===name.value&&v.description===description.value)||null)
function choosePreset(id){
 if(busy.value||generating.value)return
 const preset=voiceDesignPresets.find(v=>v.id===id)
 if(!preset)return
 name.value=preset.name;description.value=preset.description;error.value=''
}
function downloadName(voice){return (voice.name||'参考音色').replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_').slice(0,80)+'.wav'}
async function refresh(){
 const epoch=++serial
 try{const data=await requestJSON('/api/voice-design');if(disposed||epoch!==serial)return;voices.value=data.voices;ready.value=data.health.ready;error.value=''}
 catch(e){if(!disposed&&epoch===serial)error.value=e.message}
 finally{if(!disposed&&epoch===serial){loading.value=false;schedule()}}
}
function schedule(){clearTimeout(timer);if(generating.value&&!disposed)timer=setTimeout(refresh,2500)}
async function create(automatic=false){
 if(busy.value||generating.value)return
 busy.value=true;error.value='';emit('adopting',true)
 try{
 if(automatic){
  agentBusy.value=true
  const proposal=await requestJSON('/api/voice-design/suggest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:props.projectId,character_id:props.characterId,preference:preference.value.trim()})})
  if(disposed)return
  name.value=proposal.name;description.value=proposal.description;text.value=proposal.reference_text;reason.value=proposal.reason
  agentBusy.value=false
 }
 const fields={name:name.value.trim(),description:description.value.trim(),reference_text:text.value.trim()}
 if(props.projectId&&props.characterId){fields.project_id=props.projectId;fields.character_id=props.characterId}
 if(!fields.name||fields.description.length<5||fields.reference_text.length<10){error.value='请填写音色名称、声音描述和试听文本。';return}
 // If the response is lost, the same logical submission reuses its identity.
 if(!pendingRequest||JSON.stringify(pendingRequest.fields)!==JSON.stringify(fields)){
  try{pendingRequest={fields,id:Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('')}}
  catch{error.value='浏览器无法创建请求编号，请使用支持随机数生成的现代浏览器。';return}
 }
  const row=await requestJSON('/api/voice-design',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...fields,request_id:pendingRequest.id})})
  if(disposed)return
  voices.value=[row,...voices.value.filter(v=>v.id!==row.id)];pendingRequest=null;query.value='';scope.value=props.projectId?'project':'all';page.value=1;schedule()
 }catch(e){if(!disposed)error.value=e.message}
 finally{if(!disposed){busy.value=false;agentBusy.value=false;emit('adopting',false)}}
}
async function organize(voice,changes){
 if(busy.value)return
 busy.value=true;error.value=''
 try{const saved=await requestJSON('/api/voice-design/'+voice.id+'/library',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes)});if(!disposed)voices.value=voices.value.map(v=>v.id===saved.id?saved:v)}
 catch(e){if(!disposed)error.value=e.message}
 finally{if(!disposed)busy.value=false}
}
async function select(voice){
 if(busy.value)return
 busy.value=true;error.value='';emit('adopting',true)
 try{
  const saved=await requestJSON('/api/voice-design/'+voice.id+'/adopt',{method:'POST'})
  if(disposed)return
  voices.value=voices.value.map(v=>v.id===saved.id?saved:v)
  if(props.adoptVoice){
   const bound=await props.adoptVoice(saved.voice_id)
   if(!disposed&&!bound)error.value='声音已保存到音色库，但未绑定当前说话者。请查看上方原因，处理后重新点采用，无需重新设计。'
  }else emit('select',saved.voice_id)
 }
 catch(e){if(!disposed)error.value=e.message}
 finally{if(!disposed){busy.value=false;emit('adopting',false)}}
}
async function selectIndex(voice){
 if(busy.value||!props.indexTtsEnabled||props.nativeH3)return
 busy.value=true;error.value='';transferring.value=voice.id;emit('adopting',true)
 transferController=new AbortController()
 try{
  const {saved,asset}=await importDesignedVoice(voice,{signal:transferController.signal})
  if(disposed)return
  voices.value=voices.value.map(v=>v.id===saved.id?saved:v)
  emit('select-index',asset)
 }catch(e){if(!disposed&&e.name!=='AbortError')error.value=e.message}
 finally{if(!disposed){busy.value=false;transferring.value='';emit('adopting',false)}transferController=null}
}
onMounted(refresh)
onUnmounted(()=>{disposed=true;++serial;clearTimeout(timer);transferController?.abort();emit('adopting',false)})
</script>

<template>
 <section class="voice-design-panel" aria-label="设计与选择声音">
  <p v-if="externalDialogue">选择或设计音色 → 试听并采用。角色台词由本项目选择的配音引擎合成，H3 接收角色对白音轨作为口型约束；旁白独立后期合成。</p>
  <p v-else-if="nativeH3">选模板 → Qwen 设计音色 → 试听并采用。视频生成时，H3 会自动接收这份声音参考，生成对应角色的对白。</p>
  <p v-else-if="indexTtsEnabled">选模板 → Qwen 设计并生成试听 → 采用到 IndexTTS-2.5。参考音频会自动导入，由 IndexTTS 使用同一份声音参考生成正文，无需下载再上传。</p>
  <p v-else>选一个音色模板 → 生成试听 → 采用这个声音。使用 Qwen 配音时直接复用已采用的声音，不用下载再上传。</p>
  <p v-if="loading" role="status">正在读取声音库…</p>
  <p v-else-if="!ready" role="status">工作站的本地 Qwen 音色设计服务尚未就绪。</p>
  <details v-if="!loading&&!ready" class="voice-preset-help">
   <summary>安装 Qwen 音色设计（可选）</summary>
   <p>在运行 OCV 后端的电脑，从项目目录执行下方命令。它会建立独立 Qwen 环境并下载约 9.1 GB 模型，需要支持 CUDA 的显卡和匹配驱动。</p>
   <pre>python scripts/setup_qwen3.py --install --download --cuda cu128</pre>
   <p>按命令输出把 QWEN3_TTS_ROOT 和 QWEN3_TTS_PYTHON 填入后端 .env，重启后试听。详见仓库 docs/optional-local-models.md；不安装也可继续使用原配音路线。</p>
  </details>
  <div v-if="projectId&&characterId" class="automatic-voice">
   <strong>为 {{characterName||'当前角色'}} 自动设计音色</strong>
   <p>根据角色设定和台词设计，与本项目其他角色区分。试听满意后再采用。</p>
   <label>声音偏好（选填）<input v-model="preference" maxlength="600" placeholder="可以留空，例如：更活泼、稍低沉、不要太尖" :disabled="busy||generating"></label>
   <button type="button" class="primary-btn" :disabled="loading||!ready||busy||generating" @click="create(true)">{{agentBusy?'正在分析角色并设计声音…':generating?'正在生成试听…':'按角色自动设计并试听'}}</button>
   <p v-if="reason" class="voice-preset-help">{{reason}}</p>
  </div>
  <details :open="!projectId&&!voices.some(v=>v.status==='saved')">
   <summary>设计新音色</summary>
   <label>常用音色模板（{{voiceDesignPresets.length}} 种）
    <select :value="selectedPreset?.id||''" :disabled="busy||generating" @change="choosePreset($event.target.value)">
     <option value="">选择模板，自动填写名称和声音描述</option>
     <optgroup v-for="group in voicePresetGroups" :key="group" :label="group">
      <option v-for="preset in voiceDesignPresets.filter(v=>v.group===group)" :key="preset.id" :value="preset.id">{{preset.name}} · {{preset.use}}</option>
     </optgroup>
    </select>
   </label>
   <div class="voice-preset-shortcuts" aria-label="绘本常用音色">
    <button v-for="id in ['gentle_sister','playful_female','elderly_male']" :key="id" type="button" :disabled="busy||generating" @click="choosePreset(id)">{{voiceDesignPresets.find(v=>v.id===id).name}}</button>
   </div>
   <p class="voice-preset-help">模板只填写描述，不会自动生成或更换当前声音。可继续修改；同一模板再次生成可能得到不同声音，满意后采用并持续复用。</p>
   <label>音色名称<input v-model="name" maxlength="60" placeholder="例如：自然女旁白" :disabled="busy||generating"></label>
   <label>声音描述<textarea v-model="description" maxlength="1200" rows="3" placeholder="例如：成年女性，温暖清楚，像朋友聊天，语速适中，不用播音腔。" :disabled="busy||generating"/></label>
   <label>试听文本<textarea v-model="text" maxlength="300" rows="3" :disabled="busy||generating"/></label>
   <button type="button" class="primary-btn" :disabled="loading||!ready||busy||generating" @click="create()">{{generating?'正在设计声音…':busy?'正在提交…':'生成试听'}}</button>
  </details>
  <p v-if="error" role="alert">{{error}} <button type="button" @click="refresh">刷新状态</button></p>
  <div v-if="currentVoice" class="current-voice"><strong>当前{{characterName||'角色'}}：{{currentVoice.name}}</strong><audio :src="currentVoice.audio_url" controls preload="none" aria-label="当前采用的声音"/><small v-if="currentVoice.archived">已归档，现有绑定仍有效</small></div>
  <div class="library-controls" aria-label="音色资产管理">
   <label>搜索音色<input v-model="query" placeholder="名称、特点、来源项目或角色"></label>
   <label>声音库<select v-model="scope"><option v-if="projectId" value="project">本项目音色</option><option value="all">全部音色</option><option value="favorites">收藏</option><option value="archived">已归档</option></select></label>
  </div>
  <p class="voice-preset-help">共 {{library.total}} 个音色 · 第 {{library.page}} / {{library.pages}} 页。声音库属于当前账号；只有点击采用才会绑定角色。归档可恢复，不影响已有绑定。</p>
  <p v-if="!loading&&!library.rows.length">没有匹配的音色。可以自动设计新声音，或切换到“全部音色”查找历史声音。</p>
  <article v-for="voice in library.rows" :key="voice.id" :class="{selected:voice.voice_id===props.voiceId}">
   <strong>{{voice.name}} <small v-if="voice.voice_id===props.voiceId">当前声音</small></strong>
   <p class="voice-preset-help">{{voice.source?voice.source.project_title+' · '+voice.source.character_name:'历史/通用音色（未记录项目来源）'}}</p>
   <details class="voice-description"><summary>声音特点</summary><p>{{voice.description}}</p></details>
   <div class="voice-actions"><button type="button" :disabled="busy||voice.status==='generating'" @click="organize(voice,{favorite:!voice.favorite})">{{voice.favorite?'取消收藏':'收藏'}}</button><button type="button" :disabled="busy||voice.status==='generating'" @click="organize(voice,{archived:!voice.archived})">{{voice.archived?'恢复到声音库':'归档'}}</button></div>
   <p v-if="voice.status==='generating'" role="status">正在工作站生成，离开此面板后仍会继续。</p>
   <p v-else-if="voice.status==='failed'" role="alert">{{voice.error}}</p>
   <template v-else>
    <audio :src="voice.audio_url" controls preload="none" :aria-label="voice.name+'试听'"/>
    <div class="voice-actions">
     <button v-if="indexTtsEnabled&&!nativeH3" type="button" class="primary-btn" :disabled="busy" @click="selectIndex(voice)">{{transferring===voice.id?'正在导入参考声音…':'采用并用 IndexTTS-2.5 配音'}}</button>
     <button type="button" :disabled="busy||voice.voice_id===props.voiceId" @click="select(voice)">{{voice.voice_id===props.voiceId?'当前声音':nativeH3?'给这个角色采用':indexTtsEnabled?'使用 Qwen 配音':'采用这个声音'}}</button>
     <a v-if="voice.audio_url" :href="voice.audio_url" :download="downloadName(voice)">下载参考音频 WAV</a>
    </div>
   </template>
  </article>
  <nav v-if="library.pages>1" class="voice-actions" aria-label="音色分页"><button :disabled="page<=1" @click="page--">上一页</button><span>{{library.page}} / {{library.pages}}</span><button :disabled="page>=library.pages" @click="page++">下一页</button></nav>
  <p v-if="externalDialogue" class="voice-preset-help">采用后自动绑定当前角色，无需下载再上传。Qwen 设计声音，所选配音引擎生成实际台词；H3 负责画面与嘴部动作。旧镜头需要局部重做才会换声。</p>
  <p v-else-if="nativeH3" class="voice-preset-help">采用后自动绑定当前角色，无需下载再上传。Qwen 负责设计声音，实际台词由 H3 在视频中生成；旧镜头需要局部重做才会换声。</p>
  <p v-else-if="indexTtsEnabled" class="voice-preset-help">采用后仅更换当前制作设置，不会立即生成或重做已有配音。IndexTTS 会重新合成声音，音色和表达可能与 Qwen 试听略有差别，建议先试听正文。计算在部署网站的电脑上执行。</p>
  <p v-else class="voice-preset-help">要给 IndexTTS‑2.5 使用：先生成试听，再下载 WAV，切换到 IndexTTS‑2.5 后上传为参考音色。</p>
 </section>
</template>

<style scoped>
.voice-design-panel{display:grid;gap:14px}.voice-design-panel p{margin:0;color:var(--muted,#acb8b4);line-height:1.6}.voice-design-panel details,.voice-design-panel article{padding:14px;border:1px solid var(--border,#40504a);border-radius:10px}.voice-design-panel summary{cursor:pointer;margin-bottom:12px}.voice-design-panel label{display:grid;gap:6px;margin:12px 0}.voice-design-panel textarea,.voice-design-panel input{width:100%;box-sizing:border-box}.voice-design-panel audio{width:100%;margin:12px 0}.voice-design-panel article.selected{border-color:var(--accent,#83dec5)}.voice-design-panel small{color:var(--accent,#83dec5);margin-left:8px}.voice-design-panel [role=alert]{color:#efb1a2}
.voice-design-panel select{width:100%;min-width:0;box-sizing:border-box}.voice-preset-shortcuts,.voice-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.voice-preset-shortcuts button{font-size:12px;padding:7px 10px}.voice-design-panel .voice-preset-help{font-size:12px}.voice-actions a{color:var(--accent,#83dec5);padding:8px;text-underline-offset:3px}
.automatic-voice,.current-voice{padding:14px;border:1px solid var(--accent,#83dec5);border-radius:10px}.library-controls{display:flex;gap:12px;flex-wrap:wrap}.library-controls label{flex:1;min-width:160px}.voice-design-panel .voice-description{padding:8px 0;border:0}.voice-description summary{font-size:13px;margin-bottom:4px}
</style>
