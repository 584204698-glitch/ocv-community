// SPDX-License-Identifier: AGPL-3.0-only
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { requestJSON } from './api'

const clone = value => JSON.parse(JSON.stringify(value))
function newId() {
  const browserCrypto = globalThis.crypto
  if (typeof browserCrypto?.randomUUID === 'function') return browserCrypto.randomUUID().replaceAll('-', '')
  // LAN HTTP pages do not expose randomUUID, but getRandomValues remains
  // available outside secure contexts. Both paths produce the API's 32 hex id.
  if (typeof browserCrypto?.getRandomValues === 'function') {
    return Array.from(browserCrypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
  }
  throw new Error('浏览器无法创建故事标识，请使用支持随机数生成的现代浏览器，或通过 HTTPS / localhost 打开工作台。当前稿件仍保留。')
}
const fields = value => ({
  intent: value.intent || 'idea', idea: value.idea || '', preset: value.preset || 'general',
  duration_seconds: Number(value.duration_seconds) || 90, title: value.title || '', story: value.story || '',
})
const recordFields = record => fields({ ...record.brief, title: record.title, story: record.story })
const same = (left, right) => JSON.stringify(fields(left)) === JSON.stringify(fields(right))
const running = record => record?.status === 'generating' || record?.task?.status === 'running'
const validRecord = (record, id = record?.id) => record && /^[a-f0-9]{32}$/.test(record.id) && record.id === id && Number.isInteger(record.revision)
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const operations = new Set(['generate', 'develop', 'directions', 'review', 'rewrite'])
const operationLabels = { generate: '创作', develop: '创作与检查', directions: '比较故事方向', review: '检查当前故事', rewrite: '生成修改建议' }
const briefKey = draft => JSON.stringify([draft?.intent, draft?.idea, draft?.preset, draft?.duration_seconds])
const codepoints = value => Array.from(value || '')
const validHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const stageLabels = { planning: '构思方向', selecting: '比较并选择方向', writing: '写作初稿', reviewing: '独立检查', revising: '生成修订候选', completed: '本轮处理完成' }

export function useStoryWorkspace({ owner, request = requestJSON, onAdopt = () => {} }) {
  const records = ref([]), locals = ref({}), selectedId = ref(''), draft = ref(null), baseline = ref(null)
  const record = ref(null), conflict = ref(null), presets = ref([])
  const loading = ref(false), busy = ref(''), error = ref(''), notice = ref(''), storageError = ref('')
  const pending = ref(null)
  const rewriteSelection = ref(null), rewriteInstruction = ref('')
  let currentOwner = '', ownerEpoch = 0, selectionEpoch = 0, listSerial = 0, readSerial = 0
  let disposed = false, hydrating = false, pollTimer
  const key = () => `ocv.story.drafts.v1:${currentOwner}`
  const dirty = computed(() => Boolean(draft.value && (!draft.value.persisted || !baseline.value || !same(draft.value, baseline.value))))
  const generating = computed(() => running(record.value))
  const locked = computed(() => loading.value || Boolean(busy.value) || generating.value || Boolean(pending.value) || Boolean(conflict.value))
  const entries = computed(() => {
    const merged = new Map(records.value.map(item => [item.id, { id: item.id, title: item.title, status: item.status, updated_at: item.updated_at }]))
    for (const [id, item] of Object.entries(locals.value)) {
      const server = merged.get(id)
      const local = !item.draft.persisted || !same(item.draft, item.baseline || {})
      // A clean cached draft may predate a background generation. Only unsaved
      // edits (or drafts absent from the server list) override fresh metadata.
      merged.set(id, { ...server, id, title: local || !server ? item.draft.title : server.title,
        local, updated_at: local || !server ? item.updated_at : server.updated_at })
    }
    return [...merged.values()].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
  })
  const synopsis = computed(() => record.value?.derived_status === 'current' && !dirty.value ? record.value.synopsis : '')
  const synopsisStale = computed(() => Boolean(record.value?.synopsis) && (!synopsis.value || dirty.value))
  const taskMessage = computed(() => record.value?.task?.message || `${operationLabels[record.value?.task?.operation] || '创作'}正在后台进行，可以离开后返回。`)
  const failedWork = computed(() => {
    const task = record.value?.task
    if (!['failed', 'interrupted'].includes(task?.status) || !Array.isArray(task.checkpoints) || !task.checkpoints.length) return null
    const checkpoints = [...task.checkpoints].reverse()
    const written = checkpoints.find(item => typeof item.draft?.story === 'string' || typeof item.story === 'string')
    const planned = checkpoints.find(item => Array.isArray(item.plan?.directions) || Array.isArray(item.directions))
    return {
      stages: [...new Set(task.checkpoints.map(item => stageLabels[item.stage]).filter(Boolean))],
      directions: (planned?.plan?.directions || planned?.directions || []).map(item => item.title).filter(title => typeof title === 'string'),
      title: written?.draft?.title || '', story: written?.draft?.story || written?.story || '',
    }
  })
  const directions = computed(() => record.value?.development?.plan?.directions || [])
  const directionsCurrent = computed(() => !dirty.value && record.value?.development?.status === 'current'
    && validHash(record.value.brief_sha256) && record.value.development.brief_sha256 === record.value.brief_sha256)
  const currentReview = computed(() => {
    const item = record.value?.review
    return !dirty.value && item && item.status !== 'stale' && ['ready', 'needs_revision'].includes(record.value.editorial_review)
      && validHash(record.value.brief_sha256) && item.story_sha256 === record.value.story_sha256 && item.brief_sha256 === record.value.brief_sha256 ? item : null
  })
  const reviewStale = computed(() => Boolean(record.value?.review) && !currentReview.value)
  const needsRevision = computed(() => currentReview.value?.verdict === 'needs_revision' || currentReview.value?.issues?.some(issue => issue.severity === 'major'))
  const rewriteProposal = computed(() => record.value?.rewrite_proposal || null)
  const hasPendingProposal = computed(() => rewriteProposal.value?.status === 'pending')
  const proposalCurrent = computed(() => {
    const proposal = rewriteProposal.value, saved = record.value
    if (!proposal || proposal.status !== 'pending' || dirty.value || !saved
      || proposal.base_story_revision !== saved.story_revision || proposal.base_story_sha256 !== saved.story_sha256
      || !validHash(saved.brief_sha256) || proposal.base_brief_sha256 !== saved.brief_sha256) return false
    const points = codepoints(saved.story)
    return Number.isInteger(proposal.start) && Number.isInteger(proposal.end) && proposal.start >= 0 && proposal.end > proposal.start && proposal.end <= points.length
      && points.slice(proposal.start, proposal.end).join('') === proposal.original
      && points.slice(0, proposal.start).join('') + proposal.replacement + points.slice(proposal.end).join('') === proposal.story
  })
  const selectionCurrent = computed(() => Boolean(rewriteSelection.value && draft.value
    && rewriteSelection.value.storyId === selectedId.value && rewriteSelection.value.owner === currentOwner
    && rewriteSelection.value.story === draft.value.story && rewriteSelection.value.brief === briefKey(draft.value)))
  const context = () => ({ owner: currentOwner, ownerEpoch, selectionEpoch, id: selectedId.value })
  const current = ctx => !disposed && ctx.owner === currentOwner && ctx.ownerEpoch === ownerEpoch && ctx.selectionEpoch === selectionEpoch && ctx.id === selectedId.value
  const ownerCurrent = ctx => !disposed && ctx.owner === currentOwner && ctx.ownerEpoch === ownerEpoch
  function persist() {
    if (!currentOwner || hydrating) return
    if (draft.value) locals.value[draft.value.id] = {
      draft: clone(draft.value), baseline: clone(baseline.value), pending: clone(pending.value),
      rewriteInstruction: rewriteInstruction.value,
      updated_at: new Date().toISOString(),
    }
    try {
      localStorage.setItem(key(), JSON.stringify({ selectedId: selectedId.value, drafts: locals.value }))
      storageError.value = ''
    } catch { storageError.value = '本机草稿保存失败，请先复制正文备份，再检查浏览器存储空间。' }
  }
  watch(draft, persist, { deep: true, flush: 'sync' })
  watch(rewriteInstruction, persist, { flush: 'sync' })
  watch(() => [draft.value?.story, briefKey(draft.value)].join('\u0000'), () => { rewriteSelection.value = null }, { flush: 'sync' })
  function stopPoll() { clearTimeout(pollTimer) }
  function schedulePoll() {
    stopPoll()
    if (!disposed && (generating.value || pending.value)) pollTimer = setTimeout(() => { void refreshCurrent() }, 2000)
  }
  function upsert(result) {
    records.value = [result, ...records.value.filter(item => item.id !== result.id)]
  }
  function accept(result, { overwrite = false } = {}) {
    if (!validRecord(result, selectedId.value)) throw new Error('返回的故事与当前稿件不一致，请重新读取。')
    if (record.value && result.revision < record.value.revision) return
    upsert(result)
    const incoming = recordFields(result)
    // Never replace a local edit just because a background request completed.
    if (!overwrite && dirty.value && draft.value?.revision !== result.revision && !same(draft.value, incoming)) {
      record.value = result
      conflict.value = result
      if (result.task?.request_id === pending.value?.request_id) pending.value = null
      persist(); schedulePoll(); return
    }
    hydrating = true
    if (overwrite || !dirty.value || same(draft.value, incoming)) {
      draft.value = { id: result.id, revision: result.revision, persisted: true, ...incoming }
      baseline.value = incoming
    } else {
      draft.value.persisted = true
      draft.value.revision = result.revision
    }
    record.value = result
    conflict.value = null
    if (result.task?.request_id && result.task.request_id === pending.value?.request_id) pending.value = null
    hydrating = false
    persist(); schedulePoll()
  }
  function resetSelection(id = '') {
    persist(); stopPoll(); selectionEpoch++; readSerial++
    hydrating = true
    selectedId.value = id; draft.value = null; baseline.value = null; record.value = null
    conflict.value = null; pending.value = null; busy.value = ''; loading.value = false; error.value = ''; notice.value = ''
    rewriteSelection.value = null; rewriteInstruction.value = ''
    hydrating = false
  }
  function createStory(intent = 'idea', identity = '') {
    let id
    try { id = identity || newId() }
    catch (failure) { error.value = failure.message; return }
    resetSelection(id)
    draft.value = { id: selectedId.value, revision: 0, persisted: false, ...fields({ intent }) }
    persist()
  }
  async function refreshCurrent() {
    if (!selectedId.value) return
    const ctx = context(), serial = ++readSerial
    try {
      const result = await request(`/api/stories/${ctx.id}`)
      if (!current(ctx) || serial !== readSerial) return
      accept(result)
      error.value = ''
    } catch (failure) {
      if (!current(ctx) || serial !== readSerial) return
      if (!(failure.status === 404 && !draft.value?.persisted && !pending.value)) error.value = failure.message || '读取故事失败。'
      schedulePoll()
    }
  }
  async function openStory(id) {
    if (!/^[a-f0-9]{32}$/.test(id)) return
    resetSelection(id)
    const saved = locals.value[id]
    hydrating = true
    if (saved?.draft) { draft.value = clone(saved.draft); baseline.value = clone(saved.baseline || null); pending.value = clone(saved.pending || null); rewriteInstruction.value = saved.rewriteInstruction || '' }
    hydrating = false
    loading.value = true
    const ctx = context()
    await refreshCurrent()
    if (current(ctx)) { loading.value = false; persist() }
  }
  async function refreshList() {
    const ctx = context(), serial = ++listSerial
    try {
      const result = await request('/api/stories')
      if (!ownerCurrent(ctx) || serial !== listSerial) return
      records.value = (result.stories || []).filter(item => validRecord(item))
    } catch (failure) { if (ownerCurrent(ctx) && serial === listSerial) error.value = failure.message || '读取故事列表失败。' }
  }
  async function saveFor(ctx) {
    if (!draft.value || !current(ctx) || conflict.value || generating.value) return null
    if (!dirty.value) return record.value
    const snapshot = clone(draft.value)
    const result = await request(`/api/stories${snapshot.persisted ? `/${snapshot.id}` : ''}`, json(snapshot.persisted ? 'PUT' : 'POST', {
      ...fields(snapshot), ...(snapshot.persisted ? { revision: snapshot.revision } : { id: snapshot.id }),
    }))
    if (!current(ctx)) return null
    if (result.story !== snapshot.story) {
      accept(result)
      throw new Error('服务器上的正文与本机稿不同，请比较版本后再保存；本机文字仍保留。')
    }
    accept(result, { overwrite: true })
    return result
  }
  async function failed(failure, ctx) {
    if (!current(ctx)) return
    error.value = failure.message || '操作失败，请检查连接后重试。'
    if (failure.status === 409) {
      const message = error.value
      await refreshCurrent()
      if (current(ctx)) error.value = message
    }
  }
  async function saveStory() {
    if (locked.value || !draft.value) return
    const ctx = context(); busy.value = 'save'; error.value = ''; notice.value = ''
    try { if (await saveFor(ctx)) notice.value = '故事已保存。' }
    catch (failure) { await failed(failure, ctx) }
    finally { if (current(ctx)) busy.value = '' }
  }
  async function runOperation(operation, parameters = {}) {
    if (!draft.value || loading.value || busy.value || conflict.value || generating.value) return
    const replay = pending.value
    operation = replay ? (replay.operation || 'generate') : operation
    if (!operations.has(operation)) { error.value = '无法识别尚未确认的操作，请保留正文并重新读取故事。'; return }
    if (!replay) {
      if (hasPendingProposal.value) { error.value = '先采用或丢弃当前修改建议，再开始新的创作或检查。'; return }
      if (['develop', 'directions', 'generate'].includes(operation)) {
        if (draft.value.intent === 'manuscript') return
        if (draft.value.intent === 'idea' && !draft.value.idea.trim()) { error.value = '先写下一点想法，再开始创作。'; return }
        if (parameters.direction_id && (!directionsCurrent.value || !directions.value.some(item => item.id === parameters.direction_id))) {
          error.value = '这个方向已经过期，请先保存当前要求，再重新获取方向。'; return
        }
      }
      if (['review', 'rewrite'].includes(operation) && codepoints(draft.value.story.trim()).length < 20) { error.value = '先写好至少 20 字的完整正文，再检查或修改。'; return }
    }
    const ctx = context(); busy.value = operation; error.value = ''; notice.value = ''
    try {
      if (!replay) {
        const saved = await saveFor(ctx)
        if (!saved || !current(ctx)) return
        pending.value = { operation, revision: saved.revision, request_id: newId(), ...parameters }
        persist()
      }
      const { operation: ignoredOperation, ...payload } = pending.value
      const result = await request(`/api/stories/${ctx.id}/${operation}`, json('POST', payload))
      if (!current(ctx)) return
      // A successful replay acknowledges this request even if another tab has
      // since started a newer task whose id is now returned by the server.
      if (validRecord(result, ctx.id)) pending.value = null
      accept(result, { overwrite: true })
      notice.value = generating.value ? `已开始${operationLabels[operation]}，可以离开此页，稍后回来查看。` : `${operationLabels[operation]}结果已更新。`
    } catch (failure) {
      if (current(ctx) && failure.status && failure.status < 500) { pending.value = null; persist() }
      await failed(failure, ctx)
      if (current(ctx) && pending.value) notice.value = '提交结果尚未确认，正在检查后台状态；重试提交会沿用同一次请求。'
    } finally { if (current(ctx)) { busy.value = ''; schedulePoll() } }
  }
  const generateStory = () => runOperation('develop')
  const findDirections = () => runOperation('directions')
  const developDirection = directionId => runOperation('develop', { direction_id: directionId })
  const reviewStory = () => runOperation('review')
  const retryPending = () => pending.value ? runOperation(pending.value.operation || 'generate') : undefined
  function selectRewriteRange(start, end) {
    if (!draft.value || locked.value) return
    const story = draft.value.story
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > story.length) { rewriteSelection.value = null; return }
    // A DOM selection uses UTF-16 offsets. Reject a boundary inside a surrogate
    // pair and convert intact character boundaries to Python codepoint offsets.
    const splitSurrogate = offset => offset > 0 && offset < story.length && /[\uD800-\uDBFF]/.test(story[offset - 1]) && /[\uDC00-\uDFFF]/.test(story[offset])
    if (splitSurrogate(start) || splitSurrogate(end)) { rewriteSelection.value = null; error.value = '选区切开了一个字符，请重新选择完整文字。'; return }
    rewriteSelection.value = { storyId: selectedId.value, owner: currentOwner, story, brief: briefKey(draft.value),
      start: codepoints(story.slice(0, start)).length, end: codepoints(story.slice(0, end)).length,
      original: story.slice(start, end), whole: start === 0 && end === story.length }
  }
  function selectWholeStory() { if (draft.value) selectRewriteRange(0, draft.value.story.length) }
  function clearRewriteSelection() { rewriteSelection.value = null }
  async function rewriteStory() {
    if (pending.value) return retryPending()
    if (locked.value) return
    if (!selectionCurrent.value) { error.value = '先在正文中选中要改写的段落，或明确选择全文。'; return }
    const instruction = rewriteInstruction.value.trim()
    if (!instruction) { error.value = '请写下希望如何修改选中的文字。'; return }
    const { start, end } = rewriteSelection.value
    return runOperation('rewrite', { start, end, instruction })
  }
  async function resolveRewrite(action) {
    if (locked.value || !rewriteProposal.value) return
    if (rewriteProposal.value.status !== 'pending') { error.value = '这份修改建议已处理或已失效，可重新选择文字并生成新建议。'; return }
    if (action === 'accept' && !proposalCurrent.value) { error.value = '正文或创作要求已变化，这份修改建议已过期，不能采用。'; return }
    if (!['accept', 'discard'].includes(action)) return
    const ctx = context(), proposalId = rewriteProposal.value.id
    busy.value = `rewrite-${action}`; error.value = ''; notice.value = ''
    try {
      const result = await request(`/api/stories/${ctx.id}/rewrite/${action}`, json('POST', { revision: record.value.revision, proposal_id: proposalId }))
      if (!current(ctx)) return
      if (!validRecord(result, ctx.id) || (action === 'accept' && result.story !== rewriteProposal.value.story)) throw new Error('改稿结果与当前故事不一致，请重新读取后确认。')
      // Discarding a proposal must not discard unsaved author edits alongside it.
      if (action === 'discard' && dirty.value && result.story === record.value.story) {
        record.value = result; draft.value.revision = result.revision; upsert(result); persist()
      } else accept(result, { overwrite: action === 'accept' })
      notice.value = action === 'accept' ? '修改已采用，原正文保留在版本记录中。' : '修改建议已丢弃，正文保持原样。'
      if (action === 'accept') { rewriteSelection.value = null; rewriteInstruction.value = '' }
    } catch (failure) { await failed(failure, ctx) }
    finally { if (current(ctx)) busy.value = '' }
  }
  async function adoptStory() {
    if (locked.value || hasPendingProposal.value || !draft.value?.story.trim()) return
    const ctx = context(); busy.value = 'adopt'; error.value = ''; notice.value = ''
    try {
      const saved = await saveFor(ctx)
      if (!saved || !current(ctx)) return
      const result = await request(`/api/stories/${ctx.id}/adopt`, json('POST', { revision: saved.revision }))
      if (!current(ctx)) return
      if (result.source?.story_id !== ctx.id || result.source.story_revision !== saved.story_revision || result.source.story_sha256 !== saved.story_sha256 || result.story !== saved.story || result.record?.story !== saved.story || !validRecord(result.record, ctx.id)) throw new Error('采用结果与当前故事不一致，请重新读取后再采用。')
      accept(result.record, { overwrite: true })
      notice.value = '故事已采用，正在打开视频制作草稿。'
      onAdopt({ ...result, owner: currentOwner })
    } catch (failure) { await failed(failure, ctx) }
    finally { if (current(ctx)) busy.value = '' }
  }
  function useServerVersion() {
    if (!conflict.value) return
    accept(conflict.value, { overwrite: true })
    error.value = ''; notice.value = '已载入服务器版本。'
  }
  function keepLocalAsNew() {
    if (!draft.value || !conflict.value) return
    const local = fields(draft.value), previous = conflict.value
    let identity
    try { identity = newId() }
    catch (failure) { error.value = failure.message; return }
    accept(previous, { overwrite: true })
    createStory(local.intent, identity)
    Object.assign(draft.value, local)
    notice.value = '本机修改已保留为新故事，原故事仍保留服务器版本。'
  }
  watch(owner, async value => {
    persist(); stopPoll(); ownerEpoch++; selectionEpoch++; readSerial++; listSerial++
    currentOwner = String(value ?? '')
    hydrating = true
    records.value = []; locals.value = {}; presets.value = []; draft.value = null; baseline.value = null; record.value = null
    selectedId.value = ''; conflict.value = null; pending.value = null; busy.value = ''; loading.value = false; error.value = ''; notice.value = ''; storageError.value = ''
    rewriteSelection.value = null; rewriteInstruction.value = ''
    let restoredId = ''
    if (currentOwner) {
      try {
        const saved = JSON.parse(localStorage.getItem(key()) || '{}')
        for (const [id, entry] of Object.entries(saved.drafts || {})) {
          if (/^[a-f0-9]{32}$/.test(id) && entry?.draft?.id === id) locals.value[id] = entry
        }
        restoredId = saved.selectedId || ''
      } catch { storageError.value = '本机故事草稿读取失败；服务器上的故事仍可打开。' }
    }
    hydrating = false
    if (!currentOwner) return
    const ctx = context()
    const presetPromise = request('/api/stories/presets').then(result => {
      if (ownerCurrent(ctx)) presets.value = result.presets || []
    }).catch(failure => { if (ownerCurrent(ctx)) error.value = failure.message || '读取频道预设失败。' })
    void refreshList()
    if (locals.value[restoredId]) void openStory(restoredId)
    await presetPromise
  }, { immediate: true, flush: 'sync' })
  onBeforeUnmount(() => { persist(); disposed = true; ownerEpoch++; selectionEpoch++; stopPoll() })
  return { entries, selectedId, draft, record, presets, loading, busy, error, notice, storageError, dirty, generating, locked, pending, conflict, synopsis, synopsisStale,
    taskMessage, failedWork, directions, directionsCurrent, currentReview, reviewStale, needsRevision, rewriteProposal, hasPendingProposal, proposalCurrent, rewriteSelection, rewriteInstruction, selectionCurrent,
    createStory, openStory, refreshList, refreshCurrent, saveStory, generateStory, findDirections, developDirection, reviewStory, retryPending, selectRewriteRange, selectWholeStory, clearRewriteSelection, rewriteStory, resolveRewrite,
    adoptStory, useServerVersion, keepLocalAsNew }
}
