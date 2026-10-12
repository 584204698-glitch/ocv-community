import { computed, onBeforeUnmount, ref, toValue, watch } from 'vue'
import { requestJSON } from './api'
import { characterCoverage, characterReferenceRepair, characterReviewPayload } from './videoCharacterCoverage'

const modes = new Set(['single_first', 'first_last', 'composition_reference'])

function selection(value = {}) {
  return {
    input_mode: modes.has(value.input_mode) ? value.input_mode : 'single_first',
    profile_id: String(value.profile_id || ''),
    end_frame_reference_id: String(value.end_frame_reference_id || ''),
    reference_ids: [...new Set((Array.isArray(value.reference_ids) ? value.reference_ids : []).map(String))],
  }
}

// Keep inactive choices in the editor; only the chosen mode enters the request.
export function videoInputPayload(value) {
  const result = selection(value)
  if (result.input_mode !== 'first_last') result.end_frame_reference_id = ''
  if (result.input_mode !== 'composition_reference') result.reference_ids = []
  return result
}

export function useVideoInputs({ project, shot, accountKey, projectCast = null, disabled = false, onUpdated = () => {}, request = requestJSON }) {
  const snapshot = ref(null), draft = ref(selection()), baseline = ref(selection())
  const revision = ref(null), loading = ref(false), saving = ref(false), uploading = ref(false), syncing = ref(false)
  const confirmingPrompts = ref(false)
  const syncRequired = ref(false), syncFailed = ref(false)
  const savingCoverage = ref(false), coverageDraft = ref([]), noCharactersConfirmed = ref(false)
  const coverageBaseline = ref({ characters: [], noCharactersConfirmed: false })
  const repairProfileId = ref(''), repairModeConfirmed = ref(false)
  const error = ref(''), message = ref(''), conflict = ref(false)
  let epoch = 0, readSequence = 0, disposed = false
  const currentProject = () => toValue(project)
  const currentShot = () => toValue(shot)
  const scope = computed(() => JSON.stringify([
    String(toValue(accountKey) ?? ''), String(currentProject()?.id || ''), String(currentShot()?.id || ''),
  ]))
  const busy = computed(() => loading.value || saving.value || savingCoverage.value || uploading.value || syncing.value || confirmingPrompts.value)
  const promptSync = computed(() => {
    const value = snapshot.value?.prompt_sync
    return { status: ['current', 'retained', 'stale', 'unknown', 'unavailable'].includes(value?.status) ? value.status : 'unknown',
      message: value?.message || '本镜尚未记录按视频输入同步正文的结果。', can_sync: value?.can_sync === true }
  })
  const promptSyncPending = computed(() => syncRequired.value || promptSync.value.status === 'stale')
  const inputDirty = computed(() => JSON.stringify(videoInputPayload(draft.value)) !== JSON.stringify(videoInputPayload(baseline.value)))
  const reviewValue = () => ({ characters: characterReviewPayload(coverageDraft.value), noCharactersConfirmed: noCharactersConfirmed.value })
  const coverageDirty = computed(() => JSON.stringify(reviewValue()) !== JSON.stringify(coverageBaseline.value))
  const dirty = computed(() => inputDirty.value || coverageDirty.value)
  const coverage = computed(() => characterCoverage(snapshot.value?.coverage))
  const assets = computed(() => snapshot.value?.assets || [])
  const projectCharacters = computed(() => {
    const value = toValue(projectCast)
    return value && (!value.project_id || value.project_id === currentProject()?.id) ? value.characters || [] : []
  })
  function linkedCharacterIssue(character) {
    if (!character.project_character_id) return ''
    const linked = projectCharacters.value.find(row => row.id === character.project_character_id)
    if (!linked || linked.status !== 'current') return '关联的项目角色已失效或尚未读取，请更新项目角色，或解除关联后手工核对。'
    if (linked.name !== character.name || linked.reference_id !== character.reference_id) return '项目角色资料已变化，请重新选用资料并核对出场时机。'
    return ''
  }
  const profiles = computed(() => snapshot.value?.profiles || [])
  const matchingProfiles = computed(() => profiles.value.filter(item => item.input_mode === draft.value.input_mode))
  const defaultProfile = computed(() => profiles.value.find(item => item.id === snapshot.value?.default_profile_id))
  const effectiveProfile = computed(() => profiles.value.find(item => item.id === (draft.value.profile_id || snapshot.value?.default_profile_id)))
  const referenceLimit = computed(() => Math.max(0, Math.min(8, Number(effectiveProfile.value?.reference_capacity ?? 8))))
  const revisionChanged = computed(() => revision.value !== null && Number(currentProject()?.revision) !== Number(revision.value))
  const selectedEndFrame = computed(() => assets.value.find(item => item.id === draft.value.end_frame_reference_id))
  const selectedReferences = computed(() => draft.value.reference_ids.map(id => assets.value.find(item => item.id === id) || { id, name: '素材已不可用', available: false }))
  const canUseLastFrame = computed(() => baseline.value.input_mode === 'first_last'
    && assets.value.some(item => item.id === baseline.value.end_frame_reference_id && item.available))
  const coverageValidation = computed(() => {
    if (!coverageDraft.value.length && !noCharactersConfirmed.value) return '请添加本镜角色，或明确确认本镜没有角色。'
    if (coverageDraft.value.some(item => !String(item.name || '').trim())) return '请填写每个角色的姓名或称呼。'
    if (coverageDraft.value.some(item => !['opening', 'later'].includes(item.presence))) return '请明确选择每个角色的出场时机；项目角色资料不代表已经核对本镜图片。'
    const linkedIssue = coverageDraft.value.map(linkedCharacterIssue).find(Boolean)
    if (linkedIssue) return linkedIssue
    if (coverageDraft.value.some(item => item.in_last_frame && !canUseLastFrame.value)) return '尾帧可见需要本镜先采用一张可用尾帧；也可以取消尾帧可见声明。'
    return ''
  })
  const validation = computed(() => {
    if (!snapshot.value) return '正在读取本镜的视频输入。'
    if (!effectiveProfile.value || effectiveProfile.value.input_mode !== draft.value.input_mode) return '请先配置并选择支持此输入方式的本地工作流。'
    if (draft.value.input_mode === 'first_last' && !selectedEndFrame.value?.available) return '请选择一张可用的尾帧图。'
    if (draft.value.input_mode === 'composition_reference') {
      if (draft.value.reference_ids.length > referenceLimit.value) return `此工作流最多增加 ${referenceLimit.value} 张参考图，请移除多余选择。`
      if (selectedReferences.value.some(item => !item.available)) return '部分参考图已不可用，请重新选择。'
    }
    return ''
  })
  const editable = computed(() => Boolean(snapshot.value) && !toValue(disabled) && !busy.value)
  const canSave = computed(() => editable.value && inputDirty.value && !coverageDirty.value && !validation.value && !conflict.value)
  const canSyncPrompts = computed(() => editable.value && !dirty.value && !conflict.value && !revisionChanged.value
    && (syncFailed.value || (promptSync.value.can_sync && (syncRequired.value || promptSync.value.status !== 'current'))))
  const canConfirmPrompts = computed(() => editable.value && !dirty.value && !conflict.value && !revisionChanged.value
    && !validation.value && snapshot.value?.core_image?.available && ['stale', 'unknown'].includes(promptSync.value.status))
  const canSaveCoverage = computed(() => editable.value && !conflict.value && !coverageValidation.value
    && (coverageDirty.value || coverage.value.status === 'stale' || coverage.value.status === 'unknown'))
  const referenceRepair = computed(() => {
    if (coverageDirty.value) return { reason: '请先保存角色核对，再补齐角色参考。', profiles: [] }
    if (revisionChanged.value || conflict.value) return { reason: '项目已发生变化，请刷新并核对后再补参考。', profiles: [] }
    if (!snapshot.value?.core_image?.available) return { reason: '请先完成并确认本镜核心图。', profiles: [] }
    return characterReferenceRepair({ coverage: coverage.value, draft: draft.value, assets: assets.value,
      profiles: profiles.value, defaultProfileId: snapshot.value?.default_profile_id, chosenProfileId: repairProfileId.value,
      adoptedReferenceIds: baseline.value.reference_ids })
  })
  const canRepairReferences = computed(() => editable.value && !referenceRepair.value.reason && referenceRepair.value.changes
    && (!referenceRepair.value.needsModeSwitch || repairModeConfirmed.value))
  const manualPromptWarning = computed(() => {
    const before = videoInputPayload(baseline.value), after = videoInputPayload(draft.value)
    const picturesChanged = (before.input_mode === 'composition_reference' || after.input_mode === 'composition_reference')
      && (before.input_mode !== after.input_mode || JSON.stringify(before.reference_ids) !== JSON.stringify(after.reference_ids))
    if (!currentProject()?.creation_parameters?.comfyui_h3_prompt_agent && picturesChanged) {
      if (promptSync.value.can_sync) return 'Picture 对应关系已改变；采用后保留正文，请核对图片编号，需要时手动同步正文。'
      return 'Picture 对应关系已改变，请核对手写视频提示词中的图片编号；这里不会自动改写提示词。'
    }
    return coverage.value.manual_prompt_warning || ''
  })

  function context() {
    return { epoch, scope: scope.value, projectId: String(currentProject()?.id || ''), shotId: String(currentShot()?.id || '') }
  }
  function isCurrent(ticket) {
    return !disposed && ticket.epoch === epoch && ticket.scope === scope.value
  }
  function path(ticket) {
    return `/api/video-studio/${encodeURIComponent(ticket.projectId)}/shots/${encodeURIComponent(ticket.shotId)}/video-inputs`
  }
  function acceptProject(record, ticket) {
    if (!record || String(record.id) !== ticket.projectId || !record.shots?.some(item => String(item.id) === ticket.shotId)) {
      throw new Error('返回的项目与当前镜头不一致，请刷新后重试。')
    }
    if (!Number.isFinite(Number(record.revision)) || Number(record.revision) < Math.max(Number(revision.value), Number(currentProject()?.revision))) throw Object.assign(new Error('项目已有更新，请刷新后核对。'), { status: 409 })
    revision.value = record.revision
    onUpdated(record)
  }
  function failure(cause) {
    conflict.value = cause.status === 409
    error.value = conflict.value
      ? '项目已发生变化，本次修改未采用。请刷新核对后重新选择；不会自动重试。'
      : (cause.message || '视频输入操作失败，请重试。')
  }

  function acceptCoverage(value) {
    const accepted = characterCoverage(value)
    coverageDraft.value = characterReviewPayload(accepted.review.characters)
    if (accepted.status === 'stale') coverageDraft.value.forEach(row => { delete row.core_identity_confirmed })
    noCharactersConfirmed.value = accepted.status !== 'unknown' && coverageDraft.value.length === 0
    coverageBaseline.value = reviewValue()
    repairProfileId.value = ''; repairModeConfirmed.value = false
  }

  async function readInputs(ticket, { preserveChoices = false, expectedRevision = null, refreshProject = false } = {}) {
    const sequence = ++readSequence
    const result = await request(path(ticket))
    if (!isCurrent(ticket) || sequence !== readSequence) return false
    if (String(result.project_id) !== ticket.projectId || String(result.shot_id) !== ticket.shotId) throw new Error('返回的视频输入不属于当前镜头，请刷新后重试。')
    if (expectedRevision !== null && (!Number.isFinite(Number(result.revision)) || Number(result.revision) < Number(expectedRevision)
      || Number(currentProject()?.revision) > Number(result.revision)
      || JSON.stringify(videoInputPayload(result.selection)) !== JSON.stringify(videoInputPayload(baseline.value)))) {
      throw Object.assign(new Error('项目版本已变化，请刷新并核对输入后重新同步。'), { status: 409 })
    }
    if ((expectedRevision !== null || refreshProject) && Number(result.revision) > Number(currentProject()?.revision)) {
      // An input view does not contain the prose being reviewed. Bring the
      // parent record to the same revision before accepting a newer view.
      const record = await request(`/api/video-studio/${encodeURIComponent(ticket.projectId)}`)
      if (!isCurrent(ticket) || sequence !== readSequence) return false
      if (Number(record.revision) !== Number(result.revision)) throw Object.assign(new Error('项目在读取期间继续变化，请刷新核对后重试。'), { status: 409 })
      acceptProject(record, ticket)
    }
    snapshot.value = result
    revision.value = result.revision
    baseline.value = selection(result.selection)
    if (!preserveChoices) draft.value = selection(result.selection)
    acceptCoverage(result.coverage)
    conflict.value = false
    if (['current','retained'].includes(promptSync.value.status)) { syncRequired.value = false; syncFailed.value = false }
    return true
  }

  async function load({ discardDraft = false, preserveChoices = false, afterMutation = false } = {}) {
    if (disposed || saving.value || savingCoverage.value || uploading.value || syncing.value || confirmingPrompts.value || (dirty.value && !discardDraft && !afterMutation)) return false
    const ticket = context()
    if (!ticket.projectId || !ticket.shotId) return false
    loading.value = true
    error.value = ''
    if (discardDraft) message.value = ''
    let sequence
    try {
      const pending = readInputs(ticket, { preserveChoices, refreshProject: discardDraft })
      sequence = readSequence
      return await pending
    } catch (cause) {
      if (isCurrent(ticket) && sequence === readSequence) failure(cause)
      return false
    } finally {
      if (isCurrent(ticket) && sequence === readSequence) loading.value = false
    }
  }

  async function synchronizePrompts(ticket, { afterSave = false, manual = false } = {}) {
    let updated = false
    const reviewedRevision = currentProject()?.revision
    try {
      // A saved selection invalidates the previous sync status. Read the adopted
      // revision before deciding whether a model call is necessary.
      if (!await readInputs(ticket, { preserveChoices: true, expectedRevision: revision.value })) return false
      if (promptSync.value.status === 'current') {
        message.value = afterSave ? '已采用本镜的视频输入，正文与当前图片一致。' : '正文已与本镜实际输入同步。'
        return true
      }
      if (manual && Number(currentProject()?.revision) !== Number(reviewedRevision)) {
        message.value = '已读取项目的新正文，请检查当前动作和视频提示词后，再次点击“已人工核对正文”。'
        return false
      }
      if ((!manual && !promptSync.value.can_sync) || promptSync.value.status === 'unavailable') {
        message.value = `${afterSave ? '已采用本镜的视频输入。' : ''}${promptSync.value.message}`
        return false
      }
      if (!manual) syncing.value = true
      const result = await request(manual ? `${path(ticket)}/confirm-prompts` : path(ticket).replace(/video-inputs$/, 'refresh-prompts'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: revision.value, ...(!manual ? { basis: 'video_inputs' } : {}) }),
      })
      if (!isCurrent(ticket)) return false
      acceptProject(result, ticket)
      updated = true
      if (!await readInputs(ticket, { preserveChoices: true, expectedRevision: result.revision })) return false
      if (promptSync.value.status !== 'current') throw new Error(promptSync.value.message)
      message.value = manual ? '已确认当前动作与视频正文适配已采用输入；本镜角色仍需单独核对。'
        : '已按本镜实际输入同步动作与视频正文，请按新动作核对已有角色。'
      return true
    } catch (cause) {
      if (isCurrent(ticket)) {
        syncRequired.value = true; syncFailed.value = true
        conflict.value = cause.status === 409
        error.value = `${updated ? '正文同步结果已保存，但最新输入与角色核对状态未能确认。' : manual
          ? '人工确认未完成，本页保留原正文。' : afterSave
          ? '新视频输入已采用，正文同步未完成；本页保留原正文。' : '正文同步未完成，本页保留原正文。'}${cause.message || '请重试同步。'}${conflict.value ? '请刷新核对后重试。' : ''}`
      }
      return false
    }
  }

  async function runPromptOperation(manual) {
    if (!(manual ? canConfirmPrompts.value : canSyncPrompts.value)) return false
    const ticket = context()
    ++readSequence
    if (manual) confirmingPrompts.value = true
    else syncing.value = true
    syncRequired.value = true
    error.value = ''; message.value = ''
    try { return await synchronizePrompts(ticket, { manual }) }
    finally { if (isCurrent(ticket)) { syncing.value = false; confirmingPrompts.value = false } }
  }
  const syncPrompts = () => runPromptOperation(false)
  const confirmPrompts = () => runPromptOperation(true)

  function setMode(mode) {
    if (!editable.value || !modes.has(mode)) return
    draft.value.input_mode = mode
    if (effectiveProfile.value?.input_mode !== mode && defaultProfile.value?.input_mode === mode) draft.value.profile_id = ''
    message.value = ''
  }
  function toggleReference(id) {
    if (!editable.value) return
    const position = draft.value.reference_ids.indexOf(id)
    if (position >= 0) draft.value.reference_ids.splice(position, 1)
    else if (draft.value.reference_ids.length < referenceLimit.value && assets.value.find(item => item.id === id)?.available) draft.value.reference_ids.push(id)
    else error.value = `此工作流最多增加 ${referenceLimit.value} 张参考图。`
  }
  function moveReference(index, direction) {
    const next = index + direction
    if (!editable.value || next < 0 || next >= draft.value.reference_ids.length) return
    const values = [...draft.value.reference_ids]
    ;[values[index], values[next]] = [values[next], values[index]]
    draft.value.reference_ids = values
  }
  function selectEndFrame(id) {
    if (editable.value && (!id || assets.value.find(item => item.id === id)?.available)) draft.value.end_frame_reference_id = id
  }

  function addCharacter() {
    if (!editable.value || coverageDraft.value.length >= 16) return
    coverageDraft.value.push({ id: '', name: '', presence: 'opening', reference_id: '', in_last_frame: false })
    noCharactersConfirmed.value = false
    message.value = ''
  }
  function removeCharacter(index) {
    if (!editable.value || index < 0 || index >= coverageDraft.value.length) return
    coverageDraft.value.splice(index, 1)
    if (!coverageDraft.value.length) noCharactersConfirmed.value = false
  }
  function selectProjectCharacter(index, identity) {
    if (!editable.value || !coverageDraft.value[index]) return false
    const character = coverageDraft.value[index]
    if (!identity) { delete character.project_character_id; message.value = ''; return true }
    const selected = projectCharacters.value.find(row => row.id === identity)
    if (!selected || selected.status !== 'current') { error.value = '该项目角色尚未确认或已失效，请先更新项目角色。'; return false }
    if (coverageDraft.value.some((row, position) => position !== index && row.project_character_id === identity)) { error.value = '该项目角色已加入本镜，请调整已有角色行。'; return false }
    Object.assign(character, { project_character_id: selected.id, name: selected.name, reference_id: selected.reference_id, presence: '', in_last_frame: false })
    delete character.core_identity_confirmed
    noCharactersConfirmed.value = false; error.value = ''; message.value = '已带入项目角色资料，请对照本镜图片选择出场时机，再保存角色核对。'
    return true
  }
  function repairReferences() {
    if (!editable.value) return false
    const repair = referenceRepair.value
    error.value = ''
    if (repair.reason) { error.value = repair.reason; return false }
    if (repair.needsModeSwitch && !repairModeConfirmed.value) {
      error.value = '请先确认改用构图与角色参考；这会取消对严格首帧和尾帧的锁定。'
      return false
    }
    if (!repair.changes) { message.value = '输入草稿已包含所需参考图，请点击“采用输入（保留提示词）”。'; return false }
    draft.value = { ...draft.value, input_mode: 'composition_reference', profile_id: repair.profile_id,
      end_frame_reference_id: '', reference_ids: [...repair.reference_ids] }
    message.value = '角色参考已加入输入草稿，原有顺序已保留；点击“采用输入（保留提示词）”才会生效。'
    return true
  }

  async function saveCoverage() {
    if (!canSaveCoverage.value) return false
    const ticket = context(), characters = characterReviewPayload(coverageDraft.value)
    ++readSequence
    savingCoverage.value = true
    error.value = ''; message.value = ''
    let accepted = false
    try {
      const result = await request(path(ticket).replace(/video-inputs$/, 'character-coverage'), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: revision.value, characters }),
      })
      if (!isCurrent(ticket)) return false
      acceptProject(result, ticket)
      coverageBaseline.value = reviewValue()
      snapshot.value = { ...snapshot.value, revision: result.revision,
        coverage: { status: 'unknown', message: '角色核对已保存，正在读取覆盖结果。', characters: [], review: { characters } } }
      message.value = '已保存角色核对。缺少参考图时仍可继续补齐；不会自动改动视频输入。'
      accepted = true
      return true
    } catch (cause) {
      if (isCurrent(ticket)) failure(cause)
      return false
    } finally {
      if (isCurrent(ticket)) {
        savingCoverage.value = false
        if (accepted) void load({ preserveChoices: true, afterMutation: true })
      }
    }
  }

  async function save() {
    if (!canSave.value) return false
    const ticket = context(), payload = videoInputPayload(draft.value)
    ++readSequence
    saving.value = true
    error.value = ''; message.value = ''
    try {
      const result = await request(path(ticket), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: revision.value, ...payload }),
      })
      if (!isCurrent(ticket)) return false
      acceptProject(result, ticket)
      baseline.value = selection(payload)
      snapshot.value = { ...snapshot.value, selection: payload, revision: result.revision, blockers: [] }
      syncRequired.value = false; syncFailed.value = false
      try {
        await readInputs(ticket, { preserveChoices: true, expectedRevision: result.revision })
        if(isCurrent(ticket))message.value = '已采用本镜的视频输入，动作与提示词保留；需要修改时请手动同步正文。'
      } catch(cause) { if(isCurrent(ticket))failure(cause) }
      return isCurrent(ticket)
    } catch (cause) {
      if (isCurrent(ticket)) failure(cause)
      return false
    } finally {
      if (isCurrent(ticket)) {
        syncing.value = false
        saving.value = false
      }
    }
  }

  async function upload(file) {
    if (!editable.value || !file) return false
    const ticket = context()
    ++readSequence
    uploading.value = true
    error.value = ''; message.value = ''
    try {
      const data = new FormData()
      data.append('revision', String(revision.value))
      data.append('file', file)
      const result = await request(`/api/video-studio/${encodeURIComponent(ticket.projectId)}/video-input-assets`, { method: 'POST', body: data })
      if (!isCurrent(ticket)) return false
      if (!result.asset?.id) throw new Error('上传结果缺少素材信息，请刷新素材库。')
      acceptProject(result.project, ticket)
      snapshot.value = { ...snapshot.value, revision: result.project.revision, assets: [...assets.value.filter(item => item.id !== result.asset.id), result.asset] }
      message.value = '图片已加入候选库。选中后点击“采用输入（保留提示词）”才会用于生成。'
      return true
    } catch (cause) {
      if (isCurrent(ticket)) failure(cause)
      return false
    } finally {
      if (isCurrent(ticket)) uploading.value = false
    }
  }

  watch(scope, () => {
    ++epoch; ++readSequence
    snapshot.value = null; draft.value = selection(); baseline.value = selection(); revision.value = null
    coverageDraft.value = []; noCharactersConfirmed.value = false
    coverageBaseline.value = { characters: [], noCharactersConfirmed: false }
    repairProfileId.value = ''; repairModeConfirmed.value = false
    loading.value = false; saving.value = false; savingCoverage.value = false; uploading.value = false; syncing.value = false
    syncRequired.value = false; syncFailed.value = false; confirmingPrompts.value = false
    error.value = ''; message.value = ''; conflict.value = false
    void load()
  }, { immediate: true, flush: 'sync' })
  watch(() => currentProject()?.revision, () => {
    if ((revision.value === null || revisionChanged.value) && !dirty.value && !saving.value && !savingCoverage.value && !uploading.value && !syncing.value && !confirmingPrompts.value) void load()
  }, { flush: 'sync' })
  watch(() => toValue(projectCast)?.revision, value => {
    // A cast save changes revision, not the user's unadopted picture choices.
    if (value === currentProject()?.revision && snapshot.value && revisionChanged.value && !coverageDirty.value && !busy.value) void load({ preserveChoices: true, afterMutation: true })
  }, { flush: 'post' })
  onBeforeUnmount(() => { disposed = true; ++epoch; ++readSequence })

  return { snapshot, draft, revision, busy, loading, saving, uploading, dirty, inputDirty, editable, canSave,
    syncing, promptSync, promptSyncPending, syncFailed, canSyncPrompts, syncPrompts,
    confirmingPrompts, canConfirmPrompts, confirmPrompts,
    error, message, conflict, revisionChanged, assets, profiles, matchingProfiles, defaultProfile, effectiveProfile,
    selectedEndFrame, selectedReferences, referenceLimit, validation,
    coverage, coverageDraft, coverageDirty, noCharactersConfirmed, canUseLastFrame, coverageValidation, projectCharacters, linkedCharacterIssue,
    savingCoverage, canSaveCoverage, referenceRepair, repairProfileId, repairModeConfirmed, canRepairReferences, manualPromptWarning,
    load, save, upload, saveCoverage, addCharacter, removeCharacter, selectProjectCharacter, repairReferences,
    setMode, selectEndFrame, toggleReference, moveReference }
}
