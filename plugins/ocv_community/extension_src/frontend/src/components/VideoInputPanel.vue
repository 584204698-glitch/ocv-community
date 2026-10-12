<script setup>
import { computed, onBeforeUnmount, watch } from 'vue'
import { useVideoInputs } from '../useVideoInputs'

const props = defineProps({
  project: { type: Object, required: true },
  shot: { type: Object, required: true },
  accountKey: { type: [String, Number], default: '' },
  disabled: { type: Boolean, default: false },
  projectCast: { type: Object, default: null },
})
const emit = defineEmits(['updated', 'busy-change', 'dirty-change', 'prompt-sync-pending'])
const { snapshot, draft, busy, loading, saving, syncing, uploading, dirty, inputDirty, editable, canSave, error, message,
  promptSync, promptSyncPending, syncFailed, canSyncPrompts, syncPrompts,
  confirmingPrompts, canConfirmPrompts, confirmPrompts,
  conflict, revisionChanged, assets, matchingProfiles, defaultProfile, effectiveProfile, selectedReferences,
  selectedEndFrame, referenceLimit, validation, load, save, upload, setMode, selectEndFrame,
  toggleReference, moveReference, coverage, coverageDraft, coverageDirty, noCharactersConfirmed,
  canUseLastFrame, coverageValidation, savingCoverage, canSaveCoverage, saveCoverage,
  addCharacter, removeCharacter, selectProjectCharacter, projectCharacters, linkedCharacterIssue, referenceRepair, repairProfileId, repairModeConfirmed,
  canRepairReferences, repairReferences, manualPromptWarning } = useVideoInputs({
  project: () => props.project, shot: () => props.shot, accountKey: () => props.accountKey,
  projectCast: () => props.projectCast, disabled: () => props.disabled, onUpdated: record => emit('updated', record),
})
const choices = [
  { id: 'single_first', label: '首帧', hint: '从已确认的核心分镜图开始运动。' },
  { id: 'first_last', label: '首尾帧', hint: '核心分镜图作为首帧，另选一张尾帧图。' },
  { id: 'composition_reference', label: '构图与角色参考', hint: '核心图作为构图参考，可增加角色或场景参考；不锁定为严格首帧。' },
]
const modeLabel = mode => choices.find(item => item.id === mode)?.label || mode || '未记录'
const coverageLabel = computed(() => coverageDirty.value ? '角色核对尚未保存' : ({
  unknown: '尚未核对本镜角色', stale: '角色核对需要更新', empty: '已确认本镜没有角色',
  partial: '部分角色缺少图片依据', covered: '已确认角色均有图片依据',
}[coverage.value.status]))
const coverageVia = value => ({ core: '核心图', last_frame: '尾帧', reference: '角色参考图' }[value] || '缺少图片依据')
function referenceNumber(id) { return draft.value.reference_ids.indexOf(id) + 2 }
function characterReference(id) { return assets.value.find(asset => asset.id === id && asset.available) }
async function uploadFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (file) await upload(file)
}
watch(busy, value => emit('busy-change', value), { immediate: true, flush: 'sync' })
watch(dirty, value => emit('dirty-change', value), { immediate: true, flush: 'sync' })
watch(promptSyncPending, value => emit('prompt-sync-pending', value), { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { emit('busy-change', false); emit('dirty-change', false); emit('prompt-sync-pending', false) })
</script>

<template>
  <section class="video-input-panel" aria-label="本镜视频输入" :aria-busy="busy">
    <div class="input-panel-heading">
      <div><h4>本镜视频输入</h4><p>生图时使用的参考图不会自动成为视频输入。请在这里确认本镜实际使用的图片。</p></div>
      <button type="button" :disabled="busy || disabled" @click="load({ discardDraft: true })">{{ dirty ? '放弃修改并刷新' : '刷新' }}</button>
    </div>
    <p v-if="loading && !snapshot" role="status">正在读取视频输入…</p>
    <p v-if="error" class="input-notice error" role="alert">{{ error }}</p>
    <template v-if="snapshot">
      <div class="character-coverage" :class="{ verified: !coverageDirty && !inputDirty && ['covered', 'empty'].includes(coverage.status) }" aria-label="本镜角色覆盖">
        <strong>{{ coverageLabel }}</strong>
        <p>{{ coverageDirty ? '下方修改尚未保存，保存后才会重新核对覆盖结果。' : coverage.message }}</p>
        <ul v-if="coverage.characters.length && !coverageDirty" class="coverage-summary">
          <li v-for="character in coverage.characters" :key="character.id"><span>{{ character.name }} · {{ character.presence === 'later' ? '后入场' : '开场已可见' }}</span><span>{{ character.covered ? coverageVia(character.via) : character.reason || '缺少图片依据' }}</span></li>
        </ul>
        <p v-if="inputDirty" class="input-hint">覆盖结果按已保存的视频输入计算；采用输入草稿后会更新。</p>
        <details class="coverage-editor">
          <summary>核对本镜角色<span v-if="coverageDirty"> · 未保存</span></summary>
          <p class="input-hint">逐个确认开场已可见和后续入场的角色。选择“开场已可见”表示你已核对核心图；参考图必须明确绑定，不会按姓名自动匹配。</p>
          <fieldset :disabled="!editable">
            <div v-for="(character, index) in coverageDraft" :key="character.id || 'new-' + index" class="coverage-character">
              <label class="coverage-project-character">从项目角色选择<select :value="character.project_character_id || ''" @change="selectProjectCharacter(index, $event.target.value)"><option value="">手工填写</option><option v-if="character.project_character_id && !projectCharacters.some(row => row.id === character.project_character_id)" :value="character.project_character_id" disabled>原项目角色已不可用</option><option v-for="row in projectCharacters" :key="row.id" :value="row.id" :disabled="row.status !== 'current'">{{ row.name }}{{ row.status === 'current' ? '' : '（需要核对）' }}</option></select></label>
              <p v-if="linkedCharacterIssue(character)" class="coverage-project-character input-hint">{{ linkedCharacterIssue(character) }}<button v-if="projectCharacters.some(row => row.id === character.project_character_id && row.status === 'current')" type="button" @click="selectProjectCharacter(index, character.project_character_id)">重新选用项目资料</button></p>
              <label>姓名或称呼<input v-model="character.name" :readonly="Boolean(character.project_character_id)" type="text" maxlength="100" placeholder="例如：后入场的兔子"></label>
              <label>出现时机<select v-model="character.presence" @change="character.core_identity_confirmed=false"><option value="" disabled>请对照本镜图片选择</option><option value="opening">开场已在核心图中可见</option><option value="later">后续才入场</option></select></label>
              <label v-if="character.presence==='opening'" class="coverage-check"><input type="checkbox" v-model="character.core_identity_confirmed"><span>我已核对当前核心图，此角色身份清楚可辨，可用核心图作身份依据。仅背影或遮挡不清时不要勾选；后续可移除该角色的独立参考图。</span></label>
              <label class="coverage-reference">绑定参考图<select v-model="character.reference_id" :disabled="Boolean(character.project_character_id)">
                <option value="">尚未绑定（可以先保存核对）</option>
                <option v-if="character.reference_id && !assets.some(asset => asset.id === character.reference_id)" :value="character.reference_id" disabled>原绑定图片已不可用</option>
                <option v-for="asset in assets" :key="asset.id" :value="asset.id" :disabled="!asset.available">{{ asset.name }}{{ asset.available ? '' : '（不可用）' }}</option>
              </select><img v-if="characterReference(character.reference_id)" class="coverage-reference-preview" :src="characterReference(character.reference_id).url" :alt="(character.name || '当前角色') + '绑定的参考图'" loading="lazy"></label>
              <p v-if="character.project_character_id" class="coverage-project-character input-hint">姓名与参考图来自项目角色。选择“手工填写”可解除关联。</p>
              <label v-if="canUseLastFrame || character.in_last_frame" class="coverage-check"><input v-model="character.in_last_frame" type="checkbox" :disabled="!canUseLastFrame && !character.in_last_frame"><span>该角色在已采用的尾帧中可见</span></label>
              <button type="button" class="coverage-remove" :aria-label="'移除角色 ' + (character.name || index + 1)" @click="removeCharacter(index)">移除</button>
            </div>
            <label v-if="!coverageDraft.length" class="coverage-check coverage-empty"><input v-model="noCharactersConfirmed" type="checkbox"><span>我已核对，本镜没有角色</span></label>
            <div class="coverage-editor-actions"><button type="button" :disabled="coverageDraft.length >= 16" @click="addCharacter">添加角色</button><label class="input-upload" :class="{ disabled: !editable }">上传候选图片<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="!editable" @change="uploadFile"></label></div>
          </fieldset>
          <p v-if="coverageValidation" class="input-hint">{{ coverageValidation }}</p>
          <p class="input-hint">缺少角色图也可先保存核对；保存角色声明不会自动采用图片或重新生成视频。</p>
          <button type="button" :disabled="!canSaveCoverage" @click="saveCoverage">{{ savingCoverage ? '正在保存…' : '保存角色核对' }}</button>
        </details>
        <div v-if="coverage.status === 'partial'" class="coverage-repair">
          <p v-if="referenceRepair.reason" class="input-hint">{{ referenceRepair.reason }}</p>
          <label v-if="referenceRepair.profiles?.length > 1" class="input-profile">补齐参考使用的工作流
            <select v-model="repairProfileId" :disabled="!editable || coverageDirty">
              <option value="">{{ referenceRepair.profile_name ? '沿用 · ' + referenceRepair.profile_name : '请选择工作流' }}</option>
              <option v-for="profile in referenceRepair.profiles" :key="profile.id" :value="profile.id">{{ profile.name }}</option>
            </select>
          </label>
          <label v-if="referenceRepair.needsModeSwitch" class="coverage-check"><input v-model="repairModeConfirmed" type="checkbox" :disabled="!editable || coverageDirty"><span>同意改用构图与角色参考：核心图将不再锁为严格首帧，也不再锁定尾帧。</span></label>
          <button type="button" :disabled="!canRepairReferences" @click="repairReferences">补齐角色参考</button>
          <p class="input-hint">仅将明确绑定的图片追加到输入草稿，保留已有顺序；仍需点击“采用输入（保留提示词）”。</p>
          <p v-if="!referenceRepair.reason && !referenceRepair.changes" class="input-hint">草稿已包含所需角色参考，请采用后查看更新的覆盖结果。</p>
        </div>
      </div>
      <fieldset :disabled="!editable" class="input-modes">
        <legend>输入方式</legend>
        <label v-for="choice in choices" :key="choice.id" :class="{ selected: draft.input_mode === choice.id }">
          <input type="radio" :name="'video-input-mode-' + shot.id" :value="choice.id" :checked="draft.input_mode === choice.id" @change="setMode(choice.id)">
          <span><strong>{{ choice.label }}</strong><small>{{ choice.hint }}</small></span>
        </label>
      </fieldset>
      <details class="input-workflow" :open="!effectiveProfile || effectiveProfile.input_mode !== draft.input_mode"><summary>工作流 · {{ effectiveProfile?.name || '尚未选择' }}</summary>
      <label class="input-profile">本镜使用的工作流
        <select v-model="draft.profile_id" :disabled="!editable">
          <option v-if="defaultProfile?.input_mode === draft.input_mode || !draft.profile_id" value="">项目默认{{ defaultProfile ? ' · ' + defaultProfile.name : '（尚未配置）' }}</option>
          <option v-if="draft.profile_id && !matchingProfiles.some(item => item.id === draft.profile_id)" :value="draft.profile_id" disabled>{{ effectiveProfile?.name || '原工作流' }}（不支持当前输入方式）</option>
          <option v-for="profile in matchingProfiles" :key="profile.id" :value="profile.id">{{ profile.name }}</option>
        </select>
      </label>
      </details>
      <div class="input-core">
        <img v-if="snapshot.core_image?.available" :src="snapshot.core_image.url" alt="本镜已确认的核心分镜图">
        <div><strong>{{ draft.input_mode === 'composition_reference' ? 'Picture1 · 核心构图参考' : '首帧 · 核心分镜图' }}</strong><p>{{ snapshot.core_image?.available ? '使用本镜已确认的核心图。' : '本镜核心图尚未就绪，请先完成并确认分镜图。' }}</p></div>
      </div>
      <template v-if="draft.input_mode !== 'single_first'">
        <div class="input-assets-heading">
          <h5>{{ draft.input_mode === 'first_last' ? '选择尾帧' : `增加参考图 · ${draft.reference_ids.length}/${referenceLimit}` }}</h5>
          <label class="input-upload" :class="{ disabled: !editable }">{{ uploading ? '正在上传…' : '上传候选图片' }}<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="!editable" @change="uploadFile"></label>
        </div>
        <p class="input-hint">上传只加入候选库；选择图片后，点击下方按钮采用。</p>
        <p v-if="!assets.length" class="input-hint">暂无候选图片，可以上传角色图、场景图或尾帧图。</p>
        <div class="input-asset-grid">
          <button v-for="asset in assets" :key="asset.id" type="button" class="input-asset"
            :class="{ selected: draft.input_mode === 'first_last' ? draft.end_frame_reference_id === asset.id : draft.reference_ids.includes(asset.id) }"
            :aria-pressed="draft.input_mode === 'first_last' ? draft.end_frame_reference_id === asset.id : draft.reference_ids.includes(asset.id)"
            :disabled="!editable || !asset.available"
            @click="draft.input_mode === 'first_last' ? selectEndFrame(draft.end_frame_reference_id === asset.id ? '' : asset.id) : toggleReference(asset.id)">
            <img v-if="asset.available" :src="asset.url" :alt="asset.name" loading="lazy"><span v-else>图片不可用</span>
            <span>{{ asset.name }}</span><b v-if="draft.input_mode === 'composition_reference' && draft.reference_ids.includes(asset.id)">Picture{{ referenceNumber(asset.id) }}</b>
            <b v-else-if="draft.input_mode === 'first_last' && draft.end_frame_reference_id === asset.id">已选尾帧</b>
          </button>
        </div>
        <p v-if="draft.input_mode === 'first_last' && selectedEndFrame" class="input-hint">当前尾帧：{{ selectedEndFrame.name }}</p>
        <ol v-if="draft.input_mode === 'composition_reference' && selectedReferences.length" class="input-reference-order">
          <li v-for="(asset, index) in selectedReferences" :key="asset.id"><span>Picture{{ index + 2 }} · {{ asset.name }}</span><div>
            <button type="button" :aria-label="'前移参考图 ' + asset.name" :disabled="!editable || index === 0" @click="moveReference(index, -1)">前移</button>
            <button type="button" :aria-label="'后移参考图 ' + asset.name" :disabled="!editable || index === selectedReferences.length - 1" @click="moveReference(index, 1)">后移</button>
            <button type="button" :disabled="!editable" @click="toggleReference(asset.id)">移除</button>
          </div></li>
        </ol>
      </template>
      <p v-if="validation" class="input-notice">{{ validation }}</p>
      <p v-if="manualPromptWarning" class="input-notice">{{ manualPromptWarning }}</p>
      <p v-if="dirty && revisionChanged && !conflict" class="input-notice">项目已有新变化，当前选择仍保留。采用时会核对版本；如发生冲突，请刷新后重新确认。</p>
      <div v-if="snapshot.blockers?.length" class="input-notice"><strong>已保存配置仍需处理</strong><ul><li v-for="blocker in snapshot.blockers" :key="blocker">{{ blocker }}</li></ul></div>
      <div class="input-save-row"><button type="button" class="primary-btn" :disabled="!canSave" @click="save">{{ syncing ? '正在同步正文…' : saving ? '正在采用输入…' : '采用输入（保留提示词）' }}</button><span>{{ coverageDirty ? '请先保存角色核对，再采用视频输入。' : inputDirty ? '采用后保留动作与提示词，不调用语言模型。' : '与已保存的选择一致。' }}</span></div>
      <div class="input-prompt-sync" :class="{ 'input-notice': promptSyncPending }" aria-label="视频正文同步">
        <p>{{ promptSync.message }}</p>
        <button v-if="promptSync.status !== 'current' || promptSyncPending" type="button" :disabled="!canSyncPrompts" @click="syncPrompts">{{ syncing ? '正在同步正文…' : syncFailed ? '重试同步正文' : '按已采用输入同步正文' }}</button>
        <p v-if="inputDirty || coverageDirty" class="input-hint">请先保存上方修改，再同步已采用的输入。</p>
        <p v-else-if="promptSync.can_sync" class="input-hint">需要更新时将调用语言模型。完成后请按新动作核对已有角色。</p>
        <div v-if="['stale', 'unknown'].includes(promptSync.status)" class="input-manual-confirm">
          <p class="input-hint">如果你已人工检查当前动作和视频提示词，确认它们适配上方已采用的图片，可直接确认。此操作不调用模型；角色可见性仍需在“核对本镜角色”中单独确认。</p>
          <button type="button" :disabled="!canConfirmPrompts" @click="confirmPrompts">{{ confirmingPrompts ? '正在保存人工确认…' : '已人工核对正文' }}</button>
        </div>
      </div>
      <p v-if="message" class="input-success" role="status">{{ message }}</p>
      <details v-if="snapshot.last_request" class="input-last-request"><summary>上次生成准备的图片 · {{ modeLabel(snapshot.last_request.input_mode) }}</summary><p v-if="snapshot.last_request.legacy">旧记录未保存完整的图片清单，无法核实上次使用的图片。</p><ul v-else><li v-for="(item, index) in snapshot.last_request.inputs || []" :key="index">{{ item.label || item.role }}<span v-if="item.name"> · {{ item.name }}</span></li></ul><p>这些图片在提交前保存；任务是否已被接收，请以生成状态和日志为准。修改选择后，下次生成准备时更新。</p></details>
    </template>
  </section>
</template>

<style scoped>
.video-input-panel{margin:16px 0;padding:18px;border:1px solid var(--border,#35423f);border-radius:14px;background:var(--panel,#1e2625);color:var(--text,#eef3f1)}
.input-panel-heading,.input-assets-heading,.input-save-row,.input-reference-order li{display:flex;align-items:center;justify-content:space-between;gap:12px}
.input-panel-heading h4,.input-assets-heading h5{margin:0 0 6px}.input-panel-heading p,.input-core p,.input-hint,.input-last-request p{margin:4px 0;font-size:12px;line-height:1.6;color:var(--muted,#aab8b3)}
.input-panel-heading>div{min-width:0}.input-panel-heading>button{flex-shrink:0}.input-modes{border:0;padding:0;margin:16px 0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.input-modes legend{margin-bottom:8px;font-weight:600;font-size:13px}
.input-modes label{display:flex;align-items:flex-start;gap:6px;padding:10px;border:1px solid var(--border,#35423f);border-radius:9px;cursor:pointer}.input-modes label.selected,.input-asset.selected{border-color:var(--accent,#81d9bd);background:color-mix(in srgb,var(--accent,#81d9bd) 10%,var(--panel,#1e2625))}.input-modes strong,.input-modes small{display:block}.input-modes strong{font-size:13px}.input-modes small{font-size:11px;line-height:1.6;margin-top:4px;color:var(--muted,#aab8b3)}
.input-profile{display:flex;gap:12px;align-items:center;font-size:13px}.input-profile select{max-width:360px;min-width:0;flex:1;padding:7px;border:1px solid var(--border,#35423f);border-radius:6px;background:var(--bg,#141918);color:inherit}.input-core{display:flex;align-items:center;gap:12px;padding:12px 0;margin:8px 0}.input-core img{width:110px;max-height:84px;object-fit:contain;border-radius:6px}.input-core strong{font-size:13px}.input-assets-heading{margin-top:12px}.input-upload{font-size:12px;cursor:pointer;color:var(--accent,#81d9bd)}.input-upload input{display:none}.input-upload.disabled{opacity:.5;cursor:default}
.character-coverage{margin-top:14px;padding:12px;border:1px solid var(--border,#35423f);border-radius:9px;font-size:13px}.character-coverage.verified{border-color:color-mix(in srgb,var(--accent,#81d9bd) 55%,var(--border,#35423f))}.character-coverage>p{margin:6px 0;font-size:12px;line-height:1.6;color:var(--muted,#aab8b3)}.coverage-summary{list-style:none;padding:0;margin:8px 0}.coverage-summary li{display:flex;justify-content:space-between;gap:12px;padding:4px 0;font-size:12px}.coverage-summary li>span:last-child{color:var(--muted,#aab8b3);text-align:right}.coverage-editor summary,.input-workflow summary{cursor:pointer;font-size:12px;padding:7px 0}.coverage-editor fieldset{padding:0;border:0;margin:0}.coverage-character{display:grid;grid-template-columns:minmax(100px,1fr) minmax(145px,1fr);gap:9px;padding:12px 0;border-bottom:1px solid var(--border,#35423f)}.coverage-character>label{display:flex;flex-direction:column;gap:5px;font-size:12px}.coverage-character>label:nth-child(3){grid-column:1/-1}.coverage-character input[type=text],.coverage-character select{width:100%;box-sizing:border-box;min-width:0;padding:7px;border:1px solid var(--border,#35423f);border-radius:6px;background:var(--bg,#141918);color:inherit;font:inherit}.coverage-character>.coverage-check,.coverage-check{display:flex;flex-direction:row;gap:8px;align-items:flex-start;font-size:12px;line-height:1.6}.coverage-character>.coverage-check{grid-column:1/-1}.coverage-check input[type=checkbox]{width:16px;height:16px;flex:0 0 16px;margin:2px 0 0;accent-color:var(--accent,#81d9bd)}.coverage-empty{margin:12px 0}.coverage-remove{justify-self:start;font-size:12px}.coverage-editor-actions{display:flex;align-items:center;gap:15px;margin:12px 0}.coverage-repair{border-top:1px solid var(--border,#35423f);margin-top:10px;padding-top:8px}.coverage-repair>.coverage-check{margin:10px 0}.coverage-repair>.input-profile{margin:10px 0}.input-workflow{margin:8px 0}.input-workflow .input-profile{margin:8px 0}
.coverage-reference-preview{width:132px;max-width:100%;height:100px;object-fit:contain;object-position:left center;border-radius:6px;background:var(--bg,#141918);margin-top:3px}
.input-asset-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:9px;margin:10px 0;max-height:340px;overflow:auto}.input-asset{display:flex;flex-direction:column;gap:5px;padding:7px;text-align:left;border:1px solid var(--border,#35423f);border-radius:8px;background:transparent;color:inherit;font-size:12px;overflow:hidden}.input-asset img{width:100%;height:85px;object-fit:contain;background:var(--bg,#141918)}.input-asset span{max-width:100%;overflow-wrap:anywhere}.input-asset b{font-size:11px;color:var(--accent,#81d9bd)}.input-asset:disabled{opacity:.5}.input-reference-order{padding:0;list-style:none}.input-reference-order li{font-size:12px;padding:6px 0}.input-reference-order li>span{overflow-wrap:anywhere;min-width:0}.input-reference-order li>div{display:flex;gap:5px;flex-shrink:0}
.input-notice{padding:9px 11px;margin:10px 0;border-radius:7px;background:color-mix(in srgb,#d8aa4b 12%,var(--panel,#1e2625));font-size:12px;line-height:1.6}.input-notice.error{background:color-mix(in srgb,#d66b65 14%,var(--panel,#1e2625));color:var(--text,#eef3f1)}.input-notice ul,.input-last-request ul{padding-left:18px;margin:5px 0}.input-save-row{justify-content:flex-start;margin-top:14px}.input-save-row span{font-size:12px;color:var(--muted,#aab8b3)}.input-success{font-size:12px;color:var(--accent,#81d9bd)}.input-last-request{margin-top:16px;font-size:12px}.input-last-request summary{cursor:pointer}
.video-input-panel .input-modes label,.video-input-panel .input-profile{color:var(--text,#eef3f1)}
.video-input-panel .input-modes input[type=radio]{width:16px;height:16px;flex:0 0 16px;margin:2px 0 0;padding:0;accent-color:var(--accent,#81d9bd)}
.video-input-panel button:not(.primary-btn){background-color:var(--panel,#1e2625);color:var(--text,#eef3f1)}
.video-input-panel button.selected,.video-input-panel button:not(.primary-btn):hover{background:color-mix(in srgb,var(--accent,#81d9bd) 10%,var(--panel,#1e2625))}
.input-prompt-sync{margin-top:12px;font-size:12px;line-height:1.6}.input-prompt-sync>p{margin:6px 0}.input-prompt-sync button{max-width:100%;white-space:normal}
@media(max-width:640px){.video-input-panel{padding:12px}.input-modes{grid-template-columns:1fr}.input-profile,.input-save-row{align-items:flex-start;flex-direction:column}.input-profile select{width:100%;max-width:none}.input-reference-order li{align-items:flex-start;flex-direction:column}.coverage-character{grid-template-columns:1fr}.coverage-summary li{flex-direction:column;gap:2px}.coverage-summary li>span:last-child{text-align:left}}
</style>
<style scoped>
.coverage-character>.coverage-project-character,.coverage-character>.coverage-reference{grid-column:1/-1}.coverage-project-character button{margin:6px 0 0;font-size:12px}
</style>
