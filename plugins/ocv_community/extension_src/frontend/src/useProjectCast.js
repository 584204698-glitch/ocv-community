import { computed, onBeforeUnmount, ref, toValue, watch } from 'vue'
import { requestJSON } from './api'

export function projectCastPayload(characters = []) {
  return characters.map(row => ({ ...(row.id ? { id: String(row.id) } : {}),
    name: String(row.name || '').trim(), reference_id: String(row.reference_id || ''),
    constraints: String(row.constraints || '').trim(),
  }))
}

export function useProjectCast({ project, accountKey, disabled = false, uploadEnabled = true, onUpdated = () => {}, request = requestJSON, storage = globalThis.localStorage }) {
  const snapshot = ref(null), draft = ref([]), baseline = ref([])
  const editRevision = ref(null)
  const loading = ref(false), saving = ref(false), uploading = ref(false), conflict = ref(false), error = ref(''), message = ref('')
  let epoch = 0, sequence = 0, disposed = false, activeStorageKey = '', hydrating = false, restorePending = true
  const scope = computed(() => JSON.stringify([String(toValue(accountKey) ?? ''), String(toValue(project)?.id || '')]))
  const busy = computed(() => loading.value || saving.value || uploading.value)
  const dirty = computed(() => JSON.stringify(projectCastPayload(draft.value)) !== JSON.stringify(baseline.value))
  const assets = computed(() => snapshot.value?.assets || [])
  const revisionChanged = computed(() => editRevision.value !== null && editRevision.value !== toValue(project)?.revision)
  const staleCount = computed(() => (snapshot.value?.characters || []).filter(row => row.status !== 'current').length)
  const validation = computed(() => {
    if (draft.value.length > 16) return '每个项目最多保存 16 个角色。'
    if (draft.value.some(row => !String(row.name || '').trim())) return '请填写每个角色的姓名或称呼。'
    if (draft.value.some(row => !row.reference_id || !assets.value.some(asset => asset.id === row.reference_id && asset.available))) return '请为每个角色选择一张可用的项目图片。'
    if (draft.value.some(row => String(row.constraints || '').length > 2000)) return '每个角色的外观要求最多 2000 字。'
    return ''
  })
  const editable = computed(() => Boolean(snapshot.value) && !busy.value && !toValue(disabled))
  const uploadAllowed = computed(() => Boolean(toValue(uploadEnabled)))
  const canSave = computed(() => editable.value && !validation.value && !conflict.value && (dirty.value || staleCount.value > 0))
  const ticket = () => ({ epoch, scope: scope.value, id: String(toValue(project)?.id || '') })
  const current = value => !disposed && value.epoch === epoch && value.scope === scope.value
  function persist() {
    if (hydrating || !storage || !activeStorageKey || !snapshot.value) return
    try {
      if (dirty.value) storage.setItem(activeStorageKey, JSON.stringify({ revision: editRevision.value, characters: projectCastPayload(draft.value), baseline: baseline.value }))
      else storage.removeItem(activeStorageKey)
    } catch { error.value = '本机角色草稿保存失败，请在离开前保存项目角色。' }
  }
  function accept(value, context, restoreDraft = false) {
    if (!value || value.project_id !== context.id || !Number.isInteger(value.revision) || !Array.isArray(value.characters) || !Array.isArray(value.assets)) throw new Error('项目角色资料不完整或不属于当前项目，请刷新后重试。')
    let local
    if (restoreDraft && storage) { try { local = JSON.parse(storage.getItem(activeStorageKey) || 'null') } catch { /* ignore damaged local records */ } }
    hydrating = true
    snapshot.value = value
    editRevision.value = value.revision
    draft.value = projectCastPayload(value.characters)
    baseline.value = projectCastPayload(value.characters)
    if (local && Number.isInteger(local.revision) && Array.isArray(local.characters) && Array.isArray(local.baseline)
      && local.characters.length <= 16 && local.characters.every(row => row && typeof row === 'object')) {
      draft.value = projectCastPayload(local.characters); baseline.value = projectCastPayload(local.baseline); editRevision.value = local.revision
      message.value = local.revision === value.revision ? '已恢复本机未保存的项目角色修改。' : '已恢复本机修改，服务器角色资料已有新版本。保存时会核对原版本；刷新会放弃本机修改。'
    }
    conflict.value = false
    hydrating = false; persist()
  }
  function failure(cause) {
    conflict.value = cause.status === 409
    error.value = conflict.value ? '项目已有新变化，本次角色修改未保存。本机修改仍保留，请刷新核对；不会自动重试。' : cause.message || '项目角色操作失败。'
  }
  async function load({ discardDraft = false } = {}) {
    if (disposed || saving.value || uploading.value || (dirty.value && !discardDraft)) return false
    const context = ticket(), read = ++sequence
    if (!context.id) return false
    loading.value = true; error.value = ''; message.value = ''
    try {
      const value = await request(`/api/video-studio/${encodeURIComponent(context.id)}/project-cast`)
      if (!current(context) || read !== sequence) return false
      accept(value, context, restorePending && !discardDraft)
      restorePending = false
      return true
    } catch (cause) { if (current(context) && read === sequence) failure(cause); return false }
    finally { if (current(context) && read === sequence) loading.value = false }
  }
  async function save() {
    if (!canSave.value) return false
    const context = ticket(), payload = { revision: editRevision.value, characters: projectCastPayload(draft.value) }
    ++sequence; saving.value = true; error.value = ''; message.value = ''
    try {
      const value = await request(`/api/video-studio/${encodeURIComponent(context.id)}/project-cast`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
      if (!current(context)) return false
      accept(value, context)
      onUpdated(value)
      message.value = '项目角色已保存。逐镜选用后，仍需对照实际图片确认出场时机。'
      return true
    } catch (cause) { if (current(context)) failure(cause); return false }
    finally { if (current(context)) saving.value = false }
  }
  async function upload(file) {
    if (!editable.value || !uploadAllowed.value || !file || conflict.value) return false
    const context = ticket(); ++sequence; uploading.value = true; error.value = ''; message.value = ''
    try {
      const body = new FormData(); body.append('revision', String(editRevision.value)); body.append('file', file)
      const result = await request(`/api/video-studio/${encodeURIComponent(context.id)}/video-input-assets`, { method: 'POST', body })
      if (!current(context)) return false
      if (result.project?.id !== context.id || !Number.isInteger(result.project.revision) || !result.asset?.id) throw new Error('上传结果不属于当前项目，请刷新后重试。')
      snapshot.value = { ...snapshot.value, revision: result.project.revision,
        assets: [...assets.value.filter(asset => asset.id !== result.asset.id), result.asset] }
      editRevision.value = result.project.revision; persist()
      onUpdated(snapshot.value)
      message.value = '图片已加入项目素材，请选择要绑定的角色图片并保存。'
      return true
    } catch (cause) { if (current(context)) failure(cause); return false }
    finally { if (current(context)) uploading.value = false }
  }
  function add() { if (editable.value && draft.value.length < 16) draft.value.push({ name: '', reference_id: '', constraints: '' }) }
  function remove(index) { if (editable.value && index >= 0 && index < draft.value.length) draft.value.splice(index, 1) }
  watch(scope, () => {
    persist(); hydrating = true
    ++epoch; ++sequence; snapshot.value = null; draft.value = []; baseline.value = []
    editRevision.value = null; activeStorageKey = 'ocv.project-cast.draft.v1:' + scope.value; restorePending = true
    loading.value = false; saving.value = false; uploading.value = false; conflict.value = false; error.value = ''; message.value = ''
    hydrating = false
    void load()
  }, { immediate: true, flush: 'sync' })
  watch(draft, persist, { deep: true, flush: 'sync' })
  watch(() => toValue(project)?.revision, () => { if (revisionChanged.value && !dirty.value && !saving.value && !uploading.value) void load() }, { flush: 'sync' })
  // Background planning persists the cast before it advances the project
  // revision. Refresh that roster without overwriting a user's local draft.
  watch(() => JSON.stringify(toValue(project)?.project_cast?.characters || []), () => {
    if (snapshot.value && !dirty.value && !saving.value && !uploading.value) void load()
  })
  onBeforeUnmount(() => { persist(); disposed = true; ++epoch; ++sequence })
  return { snapshot, draft, assets, busy, loading, saving, uploading, uploadAllowed, dirty, editable, canSave, validation, staleCount,
    revisionChanged, conflict, error, message, load, save, upload, add, remove }
}
