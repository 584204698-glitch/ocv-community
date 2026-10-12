<script setup>
const props = defineProps({ editor: { type: Object, required: true } })
const { snapshot, draft, assets, busy, loading, saving, uploading, uploadAllowed, dirty, editable, canSave, validation,
  staleCount, revisionChanged, conflict, error, message, load, save, upload, add, remove } = props.editor
const reference = id => assets.value.find(asset => asset.id === id && asset.available)
const saved = id => snapshot.value?.characters.find(character => character.id === id)
async function uploadFile(event) { const file = event.target.files?.[0]; event.target.value = ''; if (file) await upload(file) }
</script>

<template>
  <details class="project-cast-panel" :aria-busy="busy">
    <summary><strong>项目角色</strong><span>{{ snapshot ? snapshot.characters.length + ' 个' : loading ? '正在读取…' : '未读取' }}</span><span v-if="dirty" class="cast-warning">有未保存修改</span><span v-else-if="staleCount" class="cast-warning">{{ staleCount }} 个需要核对</span></summary>
    <div class="project-cast-body">
      <p class="cast-hint">创建时绑定的固定角色会用于分镜规划，也可在这里补充或修改。修改后从下一次规划生效；出场时机和实际画面仍需逐镜确认。</p>
      <p v-if="error" class="cast-error" role="alert">{{ error }}</p>
      <button type="button" :disabled="busy" @click="load({ discardDraft: true })">{{ dirty ? '放弃修改并刷新角色' : '刷新项目角色' }}</button>
      <fieldset v-if="snapshot" :disabled="!editable">
        <article v-for="(character,index) in draft" :key="character.id || 'new-' + index" class="project-cast-row">
          <label>姓名或称呼<input v-model="character.name" maxlength="160" placeholder="例如：戴红围巾的小兔"></label>
          <label>角色参考图<select v-model="character.reference_id"><option value="">选择项目图片</option><option v-if="character.reference_id && !assets.some(asset => asset.id === character.reference_id)" :value="character.reference_id" disabled>原绑定图片已不可用</option><option v-for="asset in assets" :key="asset.id" :value="asset.id" :disabled="!asset.available">{{ asset.name }}{{ asset.available ? '' : '（不可用）' }}</option></select></label>
          <img v-if="reference(character.reference_id)" :src="reference(character.reference_id).url" :alt="(character.name || '角色') + '的参考图'" loading="lazy">
          <p v-if="saved(character.id)?.status === 'stale'" class="cast-warning">{{ saved(character.id).message || '图片或资料已变化，请对照图片重新保存。' }}</p>
          <details class="cast-constraints"><summary>外观要求（可选）</summary><textarea v-model="character.constraints" rows="2" maxlength="2000" placeholder="例如：始终佩戴红围巾，不添加外耳。" /></details>
          <button type="button" :aria-label="'移除项目角色 ' + (character.name || index + 1)" @click="remove(index)">移除角色</button>
        </article>
        <p v-if="!assets.length" class="cast-hint">{{ uploadAllowed ? '暂无项目图片，可先上传角色参考图。' : '暂无项目图片，可在制作配置里添加参考素材。' }}</p>
        <div class="cast-actions"><button type="button" :disabled="draft.length >= 16" @click="add">添加项目角色</button><label v-if="uploadAllowed" class="cast-upload">{{ uploading ? '正在上传…' : '上传候选图片' }}<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="!editable || conflict" @change="uploadFile"></label></div>
      </fieldset>
      <p v-if="validation" class="cast-hint">{{ validation }}</p>
      <p v-if="revisionChanged && dirty && !conflict" class="cast-warning">项目已有新变化，当前角色修改仍保留。保存时会核对版本。</p>
      <p v-if="dirty || staleCount" class="cast-hint">更新或移除角色后，引用该角色的镜头需要重新核对；保存不会自动采用视频输入或生成内容。</p>
      <div v-if="snapshot" class="cast-actions"><button type="button" :disabled="!canSave" @click="save">{{ saving ? '正在保存…' : '保存项目角色' }}</button><span v-if="message" role="status">{{ message }}</span></div>
    </div>
  </details>
</template>

<style scoped>
.project-cast-panel{margin:0 0 18px;border:1px solid var(--ocv-line,var(--line,#ffffff12));border-radius:12px;background:var(--ocv-panel,var(--panel,#202526));color:var(--ocv-text,var(--text,#e7e9eb));font-size:13px}.project-cast-panel>summary{padding:13px 16px;cursor:pointer}.project-cast-panel>summary span{margin-left:12px;font-size:12px}.project-cast-body{padding:0 16px 16px}.project-cast-body fieldset{border:0;padding:0;margin:12px 0;min-width:0}.cast-hint,.cast-actions span{font-size:12px;line-height:1.7;color:var(--ocv-muted,var(--muted,#a0aaa8))}.cast-warning{font-size:12px;color:var(--warning,#d6bc87)}.cast-error{color:var(--danger,#ed9292);line-height:1.7}.project-cast-row{display:grid;grid-template-columns:minmax(120px,1fr) minmax(150px,1fr) 90px;gap:10px;padding:14px 0;border-top:1px solid var(--ocv-line,var(--line,#ffffff12));align-items:start}.project-cast-row label{display:grid;gap:6px;font-size:12px}.project-cast-row input,.project-cast-row select,.project-cast-row textarea{width:100%;box-sizing:border-box}.project-cast-row img{width:90px;height:80px;object-fit:contain;border-radius:7px}.project-cast-row>.cast-warning,.cast-constraints{grid-column:1/-1}.cast-constraints summary{cursor:pointer;font-size:12px}.cast-constraints textarea{margin-top:8px;min-height:70px;resize:vertical}.project-cast-row>button{justify-self:start;font-size:12px}.cast-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}@media(max-width:680px){.project-cast-row{grid-template-columns:minmax(0,1fr)}.project-cast-row img{width:110px;height:90px}.project-cast-panel>summary{line-height:1.8}}
</style>
<style scoped>.cast-upload{font-size:12px;cursor:pointer;color:var(--ocv-accent,var(--accent,#83dec5))}.cast-upload input{display:none}</style>
