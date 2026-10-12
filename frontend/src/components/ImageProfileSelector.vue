<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ form: { type: Object, required: true }, manage: Boolean })
const profiles = ref([])
const message = ref('')
const saving = ref(false)
const editing = ref(false)
function defaults(protocol='async_task') {
 const images=protocol==='openai_images'
 return {id:'',name:'',protocol,credential_source:'profile',base_url:'',model_id:'',
  text_endpoint:images?'/images/generations':'/openapi/v2/{model}/text-to-image',
  reference_endpoint:images?'/images/edits':'/openapi/v2/{model}/image-to-image',
  query_endpoint:images?'':'/openapi/v2/query',resolutions:images?['1k']:['1k','2k','4k'],reference_images:true,api_key:''}
}
const draft = reactive(defaults())
const imagesProtocol = computed(() => draft.protocol==='openai_images')
const sharedCredential = computed(() => imagesProtocol.value&&draft.credential_source==='language_custom')
const canSave = computed(() => !saving.value&&Boolean(draft.name.trim()&&draft.model_id.trim()&&(sharedCredential.value||draft.base_url.trim())))
const selected = computed(() => profiles.value.find(item => item.id === props.form.image_profile_id) || null)
const resolutions = computed(() => selected.value?.protocol==='openai_images'?['1k']:selected.value?.resolutions || ['1k','2k','4k'])

function reset(profile=null){
 const protocol=profile?.protocol==='openai_images'?'openai_images':'async_task'
 const next=defaults(protocol)
 if(profile){
  for(const key of ['id','name','base_url','model_id'])next[key]=String(profile[key]||'')
  next.reference_images=profile.reference_images!==false
  if(protocol==='openai_images')next.credential_source=profile.credential_source==='language_custom'?'language_custom':'profile'
  else{
   for(const key of ['text_endpoint','reference_endpoint','query_endpoint'])if(profile[key])next[key]=String(profile[key])
   const supported=(profile.resolutions||next.resolutions).filter(value=>['1k','2k','4k'].includes(value))
   next.resolutions=supported.length?[...supported]:next.resolutions
  }
 }
 Object.assign(draft,next);message.value='';editing.value=true
}
function changeProtocol(protocol){
 if(!['async_task','openai_images'].includes(protocol)||protocol===draft.protocol)return
 const next=defaults(protocol),wasShared=sharedCredential.value
 for(const key of ['protocol','credential_source','text_endpoint','reference_endpoint','query_endpoint','resolutions','api_key'])draft[key]=next[key]
 if(wasShared)draft.base_url=''
}
function changeCredentialSource(source){
 if(!imagesProtocol.value||!['profile','language_custom'].includes(source)||source===draft.credential_source)return
 draft.credential_source=source;draft.api_key='';draft.base_url=''
}
async function load(){try{profiles.value=(await api.imageProfiles()).profiles||[];if(!profiles.value.some(x=>x.id===props.form.image_profile_id))props.form.image_profile_id=profiles.value.find(x=>x.configured)?.id||'';syncResolution()}catch(e){message.value=e.message}}
function syncResolution(){if(!resolutions.value.includes(props.form.image_resolution))props.form.image_resolution=resolutions.value[0]||'1k'}
async function save(){
 if(!canSave.value)return
 const paths=defaults(draft.protocol)
 const payload={...(draft.id?{id:draft.id}:{}),name:draft.name.trim(),protocol:draft.protocol,
  credential_source:sharedCredential.value?'language_custom':'profile',base_url:sharedCredential.value?'':draft.base_url.trim(),model_id:draft.model_id.trim(),
  text_endpoint:imagesProtocol.value?paths.text_endpoint:draft.text_endpoint.trim(),reference_endpoint:imagesProtocol.value?paths.reference_endpoint:draft.reference_endpoint.trim(),
  query_endpoint:imagesProtocol.value?'':draft.query_endpoint.trim(),resolutions:imagesProtocol.value?['1k']:[...draft.resolutions],reference_images:Boolean(draft.reference_images)}
 if(!sharedCredential.value&&draft.api_key.trim())payload.api_key=draft.api_key.trim()
 saving.value=true;message.value=''
 try{const result=await api.saveImageProfile(payload);message.value=result.message;await load();props.form.image_profile_id=result.profile.id;syncResolution();draft.api_key='';editing.value=false}
 catch(e){message.value=e.message}finally{saving.value=false}
}
async function remove(profile){if(!confirm(`删除“${profile.name}”？`))return;try{await api.deleteImageProfile(profile.id);await load()}catch(e){message.value=e.message}}
function toggleResolution(value){if(imagesProtocol.value)return;const list=draft.resolutions;const index=list.indexOf(value);if(index>=0&&list.length>1)list.splice(index,1);else if(index<0)list.push(value)}
watch(()=>props.form.image_profile_id,syncResolution)
onMounted(load)
</script>

<template>
 <section class="image-profile-selector" :class="{manager:manage,disabled:form.use_cloud_image_pool}">
  <div class="profile-head"><div><strong>{{manage?'图像模型配置':'本任务图像模型'}}</strong><small>{{form.use_cloud_image_pool?'号池模型由云端统一管理':manage?'每个图像模型单独保存，可选择接口协议和凭据来源。':'仅影响本任务；不会改动接口与服务中的配置。'}}</small></div><button v-if="manage&&!editing" type="button" @click="reset()">＋ 新增模型</button></div>
  <template v-if="!form.use_cloud_image_pool">
   <div v-if="!manage||!editing" class="profile-select-row">
    <label><span>模型配置</span><select v-model="form.image_profile_id" aria-label="模型配置" :disabled="!profiles.length"><option value="">{{profiles.length?'请选择':'尚未配置'}}</option><option v-for="p in profiles" :key="p.id" :value="p.id" :disabled="!p.configured">{{p.name}} · {{p.model_id}}{{p.configured?'':'（请检查接口配置）'}}</option></select></label>
    <label><span>出图分辨率</span><select v-model="form.image_resolution" aria-label="出图分辨率" :disabled="!selected"><option v-for="r in resolutions" :key="r" :value="r">{{r.toUpperCase()}}</option></select></label>
   </div>
   <div v-if="manage&&!editing" class="profile-list"><article v-for="p in profiles" :key="p.id"><div><b>{{p.name}}</b><small>{{p.model_id}} · {{p.protocol==='openai_images'?'Images 图片编辑':'异步任务接口'}} · {{p.protocol==='openai_images'&&p.credential_source==='language_custom'?'沿用自定义文字接口':(p.key_count||0)+' 个 Key'}}</small></div><button type="button" @click="reset(p)">编辑</button><button v-if="!p.legacy" type="button" @click="remove(p)">删除</button></article><div v-if="!profiles.length" class="profile-empty"><div class="profile-empty-title"><div><span>填写示范</span><b>Image 2.5 · 我的接口</b></div><em>仅为格式示例</em></div><dl><div><dt>API Base URL</dt><dd>https://api.example.com</dd></div><div><dt>模型 ID</dt><dd>image-2.5</dd></div><div><dt>API Key</dt><dd>填写服务商提供的密钥</dd></div><div><dt>支持分辨率</dt><dd>依所选接口协议而定</dd></div></dl><p>实际内容请以你的接口服务商文档为准。OCV 不指定或推荐第三方服务商。</p><button class="primary" type="button" @click="reset()">＋ 按格式新增配置</button></div></div>
   <fieldset v-if="manage&&editing" class="profile-editor" :disabled="saving">
    <label><span>配置名称</span><input v-model="draft.name" maxlength="60" placeholder="例如：我的图片编辑接口" /></label>
    <label><span>接口协议</span><select :value="draft.protocol" aria-label="接口协议" @change="changeProtocol($event.target.value)"><option value="async_task">异步任务接口</option><option value="openai_images">Images 图片编辑</option></select></label>
    <label v-if="imagesProtocol" class="wide"><span>凭据来源</span><select :value="draft.credential_source" aria-label="凭据来源" @change="changeCredentialSource($event.target.value)"><option value="profile">单独配置图像接口</option><option value="language_custom">沿用自定义文字接口</option></select></label>
    <p v-if="sharedCredential" class="wide credential-note">沿用当前自定义文字接口的地址和凭据，不复制密钥。文字接口更换地址后，请重新保存此图像配置。</p>
    <label v-else class="wide"><span>API Base URL</span><input v-model="draft.base_url" maxlength="2048" :placeholder="imagesProtocol?'https://api.example.com/v1':'https://api.example.com'" /></label>
    <label><span>模型 ID</span><input v-model="draft.model_id" maxlength="256" placeholder="填写服务商提供的模型 ID" /></label>
    <label v-if="!sharedCredential"><span>API Key</span><input v-model="draft.api_key" type="password" autocomplete="new-password" maxlength="2048" :placeholder="draft.id?'已保存；不修改可留空':'填写 API Key；多个可用逗号分隔'" /></label>
    <details class="wide"><summary>{{imagesProtocol?'接口路径（只读）':'接口路径与能力（高级）'}}</summary><div class="endpoint-grid"><label><span>文生图路径</span><input v-model="draft.text_endpoint" :readonly="imagesProtocol" /></label><label><span>参考生图路径</span><input v-model="draft.reference_endpoint" :readonly="imagesProtocol" /></label><label v-if="!imagesProtocol"><span>查询路径</span><input v-model="draft.query_endpoint" /></label><p v-else class="credential-note">请求直接返回图片，无需查询路径。</p></div></details>
    <div class="wide profile-capabilities"><span>支持分辨率</span><label v-for="r in imagesProtocol?['1k']:['1k','2k','4k']" :key="r"><input type="checkbox" :checked="draft.resolutions.includes(r)" :disabled="imagesProtocol" @change="toggleResolution(r)" /> {{r.toUpperCase()}}</label><label><input v-model="draft.reference_images" type="checkbox" /> 支持参考图</label></div>
    <p v-if="imagesProtocol" class="wide credential-note">当前仅提供 1K；实际模型名称与参考图能力以服务商支持为准。</p>
    <div class="wide profile-actions"><button type="button" @click="draft.api_key='';editing=false">取消</button><button class="primary" type="button" :disabled="!canSave" @click="save">{{saving?'保存中…':'保存模型配置'}}</button></div>
   </fieldset>
  </template>
  <p v-if="message" class="profile-message">{{message}}</p>
 </section>
</template>

<style scoped>
.image-profile-selector{display:grid;gap:14px;padding:18px;border:1px solid var(--ocv-border,#35433e);border-radius:14px;background:color-mix(in srgb,var(--ocv-panel,#202825) 88%,transparent)}.profile-head,.profile-select-row,.profile-list article,.profile-actions{display:flex;gap:12px;align-items:center}.profile-head{justify-content:space-between}.profile-head>div,.profile-list article>div{display:grid;gap:4px}.profile-head small,.profile-list small{display:block;color:var(--ocv-muted,#9aa6a1)}.profile-select-row label{flex:1;display:grid;gap:7px}.profile-list{display:grid;gap:8px}.profile-list article{padding:10px 12px;border:1px solid var(--ocv-border,#35433e);border-radius:10px}.profile-list article>div{flex:1}.profile-editor{display:grid;grid-template-columns:1fr 1fr;gap:12px}.profile-editor label{display:grid;gap:6px}.wide{grid-column:1/-1}.endpoint-grid{display:grid;gap:10px;margin-top:12px}.profile-capabilities{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.profile-capabilities label{display:flex;grid-auto-flow:column;align-items:center}.profile-actions{justify-content:flex-end}.profile-message{margin:0;color:var(--ocv-accent,#7ddac5)}.disabled{opacity:.72}@media(max-width:760px){.profile-select-row,.profile-editor{display:grid;grid-template-columns:1fr}.wide{grid-column:auto}}
.profile-empty{display:grid;gap:14px;padding:16px;border:1px dashed color-mix(in srgb,var(--ocv-accent,#7ddac5) 55%,var(--ocv-border,#35433e));border-radius:12px;background:color-mix(in srgb,var(--ocv-accent,#7ddac5) 5%,transparent)}.profile-empty-title{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.profile-empty-title>div{display:grid;gap:4px}.profile-empty-title span{font-size:.8rem;color:var(--ocv-accent,#7ddac5)}.profile-empty-title em{padding:4px 8px;border-radius:999px;background:color-mix(in srgb,var(--ocv-accent,#7ddac5) 12%,transparent);color:var(--ocv-muted,#9aa6a1);font-size:.75rem;font-style:normal}.profile-empty dl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:0}.profile-empty dl>div{display:grid;gap:3px;padding:9px 10px;border-radius:8px;background:color-mix(in srgb,var(--ocv-panel,#202825) 80%,transparent)}.profile-empty dt{font-size:.76rem;color:var(--ocv-muted,#9aa6a1)}.profile-empty dd{margin:0;overflow-wrap:anywhere}.profile-empty p{margin:0;color:var(--ocv-muted,#9aa6a1);font-size:.82rem}.profile-empty button{justify-self:start}
</style>
<style scoped>.profile-editor{border:0;padding:0;margin:0;min-width:0}.profile-editor input,.profile-editor select{min-width:0;width:100%;box-sizing:border-box}.profile-editor .profile-capabilities input{width:auto}.credential-note{margin:0;color:var(--ocv-muted,#9aa6a1);font-size:12px;line-height:1.7}.profile-list article>div{min-width:0;overflow-wrap:anywhere}</style>
