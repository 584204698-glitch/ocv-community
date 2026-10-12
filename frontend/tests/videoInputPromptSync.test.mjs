import assert from 'node:assert/strict'
import test from 'node:test'
import { ref, watch } from 'vue'
import { useVideoInputs } from '../src/useVideoInputs.js'
import { deferred, mountComposable } from './support/composableHarness.mjs'

const inputPath = project => `/api/video-studio/${project}/shots/shot-a/video-inputs`
const refreshPath = project => `/api/video-studio/${project}/shots/shot-a/refresh-prompts`
const baseSelection = () => ({ input_mode: 'single_first', profile_id: '', end_frame_reference_id: '', reference_ids: [] })
const referenceSelection = () => ({ input_mode: 'composition_reference', profile_id: 'refs', end_frame_reference_id: '', reference_ids: ['rabbit'] })
const oldPrompt = '旧视频正文：保持原有动作。'
const newPrompt = '更新后的视频正文：Picture2 中的小兔随后入场。'

function record(revision = 1, { id = 'project-a', prompt = oldPrompt, selection = baseSelection() } = {}) {
  return { id, revision, status: 'image_review', creation_parameters: { comfyui_h3_prompt_agent: true },
    shots: [{ id: 'shot-a', kind: 'video', action: '原动作', image_prompt: '原核心图提示词', video_prompt: prompt,
      video_input_selection: selection }] }
}

function inputView({ project = 'project-a', revision = 1, status = 'current', canSync = true,
  selection = baseSelection() } = {}) {
  return { project_id: project, shot_id: 'shot-a', revision, selection,
    default_profile_id: 'first', profiles: [
      { id: 'first', name: '默认首帧流程', input_mode: 'single_first', reference_capacity: 0 },
      { id: 'first-alternate', name: '另一首帧流程', input_mode: 'single_first', reference_capacity: 0 },
      { id: 'end', name: '首尾帧流程', input_mode: 'first_last', reference_capacity: 0 },
      { id: 'refs', name: '角色参考流程', input_mode: 'composition_reference', reference_capacity: 2 },
    ],
    assets: [{ id: 'rabbit', name: '小兔', url: '/reference/rabbit', available: true, kind: 'image' }],
    core_image: { available: true, url: '/core' }, blockers: [], last_request: null,
    prompt_sync: { status, message: `提示词同步状态：${status}`, can_sync: canSync } }
}

async function editor(t, responder, { initialRecord = record() } = {}) {
  const project = ref(initialRecord), shot = ref(initialRecord.shots[0]), accountKey = ref('account-a')
  const disabled = ref(false)
  const calls = [], updates = []
  const mounted = mountComposable(() => useVideoInputs({ project, shot, accountKey, disabled,
    request(url, options = {}) {
      const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined }
      calls.push(call)
      return responder(call)
    },
    onUpdated(value) { updates.push(value); project.value = value; shot.value = value.shots[0] },
  }))
  t.after(() => mounted.unmount())
  await mounted.flush()
  return { ...mounted, panel: mounted.instance, project, shot, accountKey, disabled, calls, updates }
}

function chooseReferences(panel) {
  panel.setMode('composition_reference')
  panel.draft.value.profile_id = 'refs'
  panel.toggleReference('rabbit')
  assert.equal(panel.canSave.value, true)
}

const requestOrder = calls => calls.map(({ method, url }) => `${method} ${url}`)

for (const status of ['stale', 'unknown', 'retained']) {
  test(`adopting ${status} inputs preserves prompts without any model POST`, async t => {
    const adoptedRead = deferred()
    let adopted = false
    const context = await editor(t, call => {
      if (call.method === 'PUT') { adopted = true; return record(2, {selection:referenceSelection()}) }
      assert.equal(call.method, 'GET', 'adoption must never refresh prompts')
      return adopted ? adoptedRead.promise : inputView()
    })
    chooseReferences(context.panel)
    const saving = context.panel.save()
    await context.flush()
    assert.equal(context.panel.busy.value,true)
    adoptedRead.resolve(inputView({revision:2,status,selection:referenceSelection()}))
    assert.equal(await saving,true)
    assert.deepEqual(requestOrder(context.calls), [`GET ${inputPath('project-a')}`,`PUT ${inputPath('project-a')}`,`GET ${inputPath('project-a')}`])
    assert.equal(context.project.value.shots[0].video_prompt,oldPrompt)
    assert.equal(context.panel.promptSync.value.status,status)
    assert.equal(context.panel.promptSyncPending.value,status==='stale')
    assert.equal(context.panel.inputDirty.value,false)
    assert.equal(context.panel.busy.value,false)
  })
}

test('failed prompt refresh preserves adopted inputs and old text; explicit retry does not repeat PUT', async t => {
  let adopted = false, attempts = 0, complete = false
  const context = await editor(t, call => {
    if (call.method === 'PUT') { adopted = true; return record(2, { selection: referenceSelection() }) }
    if (call.method === 'POST') {
      if (++attempts === 1) throw new Error('转换服务暂时不可用')
      complete = true
      return record(3, { prompt: newPrompt, selection: referenceSelection() })
    }
    return inputView({ revision: complete ? 3 : adopted ? 2 : 1, status: adopted && !complete ? 'stale' : 'current',
      selection: adopted ? referenceSelection() : baseSelection() })
  })
  chooseReferences(context.panel)
  assert.equal(await context.panel.save(), true)
  assert.equal(attempts, 0, 'adoption is independent of conversion')
  assert.equal(await context.panel.syncPrompts(), false)
  await context.flush()
  assert.deepEqual(context.panel.draft.value.reference_ids, ['rabbit'])
  assert.deepEqual(context.project.value.shots[0].video_input_selection, referenceSelection())
  assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
  assert.equal(context.panel.inputDirty.value, false)
  assert.equal(context.panel.promptSyncPending.value, true)
  assert.equal(context.panel.canSyncPrompts.value, true)
  assert.equal(context.panel.busy.value, false)
  assert.match(context.panel.error.value, /转换服务暂时不可用/)
  assert.equal(attempts, 1, 'failure does not schedule an automatic retry')
  const count = context.calls.length
  assert.equal(await context.panel.syncPrompts(), true)
  await context.flush()
  assert.deepEqual(requestOrder(context.calls.slice(count)), [
    `GET ${inputPath('project-a')}`, `POST ${refreshPath('project-a')}`, `GET ${inputPath('project-a')}`,
  ])
  assert.equal(context.calls.filter(call => call.method === 'PUT').length, 1)
  assert.deepEqual(context.calls.findLast(call => call.method === 'POST').body, { revision: 2, basis: 'video_inputs' })
  assert.equal(context.project.value.shots[0].video_prompt, newPrompt)
  assert.equal(context.panel.promptSyncPending.value, false)
  assert.equal(context.panel.promptSync.value.status, 'current')
})

test('initial unknown state is passive and can be synchronized explicitly using the fresh GET revision', async t => {
  let reads = 0, synchronized = false
  const context = await editor(t, call => {
    if (call.method === 'POST') { synchronized = true; return record(3, { prompt: newPrompt }) }
    assert.equal(call.method, 'GET')
    if (call.url === '/api/video-studio/project-a') return record(2)
    return inputView({ revision: ++reads === 1 ? 1 : synchronized ? 3 : 2, status: synchronized ? 'current' : 'unknown' })
  })
  assert.equal(context.calls.length, 1)
  assert.equal(context.panel.promptSync.value.status, 'unknown')
  assert.equal(context.panel.promptSync.value.message, '提示词同步状态：unknown')
  assert.equal(context.panel.promptSyncPending.value, false)
  assert.equal(context.panel.canSyncPrompts.value, true)
  assert.equal(await context.panel.syncPrompts(), true)
  await context.flush()
  assert.deepEqual(context.calls.find(call => call.method === 'POST').body, { revision: 2, basis: 'video_inputs' })
  assert.equal(context.calls.filter(call => call.method === 'PUT').length, 0)
  assert.equal(context.panel.revision.value, 3)
  assert.equal(context.project.value.shots[0].video_prompt, newPrompt)
})

test('a workflow-only adoption with current prompts skips conversion', async t => {
  let adopted = false
  const accepted = { ...baseSelection(), profile_id: 'first-alternate' }
  const context = await editor(t, call => {
    if (call.method === 'PUT') { adopted = true; return record(2, { selection: accepted }) }
    assert.equal(call.method, 'GET')
    return inputView({ revision: adopted ? 2 : 1, selection: adopted ? accepted : baseSelection() })
  })
  context.panel.draft.value.profile_id = 'first-alternate'
  assert.equal(await context.panel.save(), true)
  await context.flush()
  assert.deepEqual(context.calls.map(call => call.method), ['GET', 'PUT', 'GET'])
  assert.equal(context.panel.promptSync.value.status, 'current')
  assert.equal(context.panel.promptSyncPending.value, false)
  assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
})

test('unavailable synchronization permits input adoption without an automatic POST', async t => {
  let adopted = false
  const context = await editor(t, call => {
    if (call.method === 'PUT') { adopted = true; return record(2, { selection: referenceSelection() }) }
    assert.equal(call.method, 'GET')
    return inputView({ revision: adopted ? 2 : 1, status: 'unavailable', canSync: false,
      selection: adopted ? referenceSelection() : baseSelection() })
  })
  chooseReferences(context.panel)
  assert.equal(await context.panel.save(), true)
  await context.flush()
  assert.equal(context.panel.promptSync.value.status, 'unavailable')
  assert.equal(context.panel.canSyncPrompts.value, false)
  assert.equal(context.calls.filter(call => call.method === 'POST').length, 0)
  assert.equal(context.panel.inputDirty.value, false)
  assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
})

test('switching projects while the adopted-state GET is pending prevents a later conversion request', async t => {
  const adoptedRead = deferred()
  let adopted = false
  const context = await editor(t, call => {
    if (call.url.includes('/project-b/')) return inputView({ project: 'project-b', revision: 7 })
    if (call.method === 'PUT') { adopted = true; return record(2, { selection: referenceSelection() }) }
    assert.equal(call.method, 'GET')
    return adopted ? adoptedRead.promise : inputView()
  })
  chooseReferences(context.panel)
  const saving = context.panel.save()
  await context.flush()
  context.project.value = record(7, { id: 'project-b' })
  await context.flush()
  context.panel.draft.value.profile_id = 'first-alternate'
  const count = context.calls.length
  adoptedRead.resolve(inputView({ revision: 2, status: 'stale', selection: referenceSelection() }))
  await saving
  await context.flush()
  assert.equal(context.calls.length, count)
  assert.equal(context.panel.snapshot.value.project_id, 'project-b')
  assert.equal(context.panel.revision.value, 7)
  assert.equal(context.panel.draft.value.profile_id, 'first-alternate')
  assert.equal(context.panel.inputDirty.value, true)
  assert.equal(context.panel.promptSyncPending.value, false)
})

test('a late conversion response cannot overwrite another project or release its pending read', async t => {
  const sync = deferred(), otherRead = deferred()
  let adopted = false
  const context = await editor(t, call => {
    if (call.url.includes('/project-b/')) return otherRead.promise
    if (call.method === 'PUT') { adopted = true; return record(2, { selection: referenceSelection() }) }
    if (call.method === 'POST') return sync.promise
    return inputView({ revision: adopted ? 2 : 1, status: adopted ? 'stale' : 'current',
      selection: adopted ? referenceSelection() : baseSelection() })
  })
  chooseReferences(context.panel)
  await context.panel.save()
  const saving = context.panel.syncPrompts()
  await context.flush()
  assert.equal(context.panel.syncing.value, true)
  context.project.value = record(7, { id: 'project-b' })
  const updateCount = context.updates.length, callCount = context.calls.length
  sync.resolve(record(3, { prompt: newPrompt, selection: referenceSelection() }))
  await saving
  await context.flush()
  assert.equal(context.updates.length, updateCount)
  assert.equal(context.calls.length, callCount, 'a stale POST completion must not issue its trailing GET')
  assert.equal(context.project.value.id, 'project-b')
  assert.equal(context.panel.loading.value, true)
  assert.equal(context.panel.busy.value, true)
  otherRead.resolve(inputView({ project: 'project-b', revision: 7 }))
  await context.flush()
  assert.equal(context.panel.snapshot.value.project_id, 'project-b')
  assert.equal(context.panel.busy.value, false)
  assert.equal(context.panel.syncing.value, false)
})

test('unmount during conversion rejects its response and does not issue a trailing GET', async t => {
  const sync = deferred()
  const context = await editor(t, call => call.method === 'POST' ? sync.promise : inputView({ status: 'unknown' }))
  const pending = context.panel.syncPrompts()
  await context.flush()
  assert.equal(context.calls.at(-1).method, 'POST')
  const count = context.calls.length
  context.unmount()
  sync.resolve(record(2, { prompt: newPrompt }))
  assert.equal(await pending, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.calls.length, count)
})

test('an input GET older than a concurrent project poll cannot roll back revision or submit an old conversion', async t => {
  const staleRead = deferred()
  let reads = 0
  const context = await editor(t, call => {
    assert.equal(call.method, 'GET')
    return ++reads === 1 ? inputView({ status: 'unknown' }) : staleRead.promise
  })
  const pending = context.panel.syncPrompts()
  context.project.value = record(3)
  staleRead.resolve(inputView({ revision: 2, status: 'stale' }))
  assert.equal(await pending, false)
  await context.flush()
  assert.equal(context.project.value.revision, 3)
  assert.notEqual(context.panel.revision.value, 2)
  assert.notEqual(context.panel.snapshot.value?.revision, 2)
  assert.equal(context.calls.filter(call => call.method === 'POST').length, 0)
  assert.equal(context.updates.length, 0)
})

test('manual confirmation saves only the current revision without a model or role review request', async t => {
  const confirming = deferred()
  let complete = false
  const context = await editor(t, call => {
    if (call.method === 'POST') return confirming.promise
    return { ...inputView({ revision: complete ? 2 : 1, status: complete ? 'current' : 'stale', canSync: false }),
      blockers: complete ? [] : ['正文尚未同步', '角色覆盖需要重新核对'] }
  })
  const { panel } = context
  assert.equal(panel.canSyncPrompts.value, false, 'manual review is available without an automatic sync capability')
  assert.equal(panel.canConfirmPrompts.value, true)
  const pending = panel.confirmPrompts()
  await context.flush()
  assert.equal(panel.confirmingPrompts.value, true)
  assert.equal(panel.busy.value, true)
  assert.equal(await panel.confirmPrompts(), false)
  assert.equal(await panel.syncPrompts(), false)
  assert.equal(await panel.upload(new Blob(['image'])), false)
  assert.equal(await panel.saveCoverage(), false)
  assert.equal(await panel.load({ discardDraft: true }), false)
  assert.deepEqual(context.calls.at(-1), { method: 'POST', url: `${inputPath('project-a')}/confirm-prompts`, body: { revision: 1 } })
  complete = true
  confirming.resolve(record(2))
  assert.equal(await pending, true)
  assert.equal(panel.promptSyncPending.value, false)
  assert.equal(panel.busy.value, false)
  assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
  assert.equal(panel.coverage.value.status, 'unknown', 'manual text review never fills character coverage')
  assert.equal(context.calls.filter(call => call.method === 'POST').length, 1)
})

test('manual review failure preserves text and a late confirmation cannot change another account', async t => {
  const second = deferred()
  let attempts = 0
  const context = await editor(t, call => {
    if (call.method === 'POST') {
      if (++attempts === 1) throw new Error('确认暂未保存')
      return second.promise
    }
    return inputView({ status: 'unknown' })
  })
  assert.equal(await context.panel.confirmPrompts(), false)
  assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
  assert.equal(context.panel.promptSyncPending.value, true)
  assert.equal(context.panel.canConfirmPrompts.value, true)
  const pending = context.panel.confirmPrompts()
  await context.flush()
  context.accountKey.value = 'another-account'
  await context.flush()
  const count = context.calls.length
  second.resolve(record(2))
  assert.equal(await pending, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.calls.length, count)
  assert.equal(context.panel.promptSyncPending.value, false)
})

test('unsaved outer text or local input edits must be saved before automatic or manual confirmation', async t => {
  const context = await editor(t, () => inputView({ status: 'stale' }))
  context.disabled.value = true
  assert.equal(await context.panel.confirmPrompts(), false)
  assert.equal(await context.panel.syncPrompts(), false)
  assert.equal(context.calls.length, 1)
  context.disabled.value = false
  assert.equal(context.panel.canConfirmPrompts.value, true)
  chooseReferences(context.panel)
  assert.equal(await context.panel.confirmPrompts(), false)
  assert.equal(await context.panel.syncPrompts(), false)
  assert.equal(context.calls.length, 1)
})

test('a failed final GET can be retried without submitting the completed model operation again', async t => {
  let attempts = 0, completedReads = 0
  const context = await editor(t, call => {
    if (call.method === 'POST') { attempts++; return record(2, { prompt: newPrompt }) }
    if (attempts && ++completedReads === 1) throw new Error('读取结果失败')
    return inputView({ revision: attempts ? 2 : 1, status: attempts ? 'current' : 'unknown' })
  })
  assert.equal(await context.panel.syncPrompts(), false)
  assert.equal(context.project.value.shots[0].video_prompt, newPrompt)
  assert.equal(context.panel.promptSyncPending.value, true)
  assert.equal(await context.panel.syncPrompts(), true)
  assert.equal(attempts, 1)
  assert.equal(context.panel.promptSyncPending.value, false)
})

test('a newer GET cannot silently replace the just-adopted pictures before synchronization', async t => {
  let adopted = false
  const context = await editor(t, call => {
    if (call.method === 'PUT') { adopted = true; return record(2, { selection: referenceSelection() }) }
    return inputView({ revision: adopted ? 3 : 1, status: 'stale' })
  })
  chooseReferences(context.panel)
  assert.equal(await context.panel.save(), true)
  assert.equal(context.panel.conflict.value, true)
  assert.deepEqual(context.panel.snapshot.value.selection, referenceSelection())
  assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
  assert.equal(context.calls.filter(call => call.method === 'POST').length, 0)
})

for (const mode of ['single_first', 'first_last']) {
  test(`${mode} inputs are adopted without automatic text synchronization`, async t => {
    const selection = { ...baseSelection(), input_mode: mode, profile_id: mode === 'first_last' ? 'end' : 'first-alternate',
      end_frame_reference_id: mode === 'first_last' ? 'rabbit' : '' }
    let adopted = false, completed = false
    const context = await editor(t, call => {
      if (call.method === 'PUT') { adopted = true; return record(2, { selection }) }
      if (call.method === 'POST') { completed = true; return record(3, { selection, prompt: newPrompt }) }
      return inputView({ selection: adopted ? selection : baseSelection(), revision: completed ? 3 : adopted ? 2 : 1,
        status: adopted && !completed ? 'stale' : 'current' })
    })
    context.panel.setMode(mode)
    context.panel.draft.value.profile_id = selection.profile_id
    if (mode === 'first_last') context.panel.selectEndFrame('rabbit')
    assert.equal(await context.panel.save(), true)
    assert.deepEqual(context.calls.find(call => call.method === 'PUT').body, { revision: 1, ...selection })
    assert.equal(context.calls.filter(call => call.method === 'POST').length, 0)
    assert.equal(context.project.value.shots[0].video_prompt, oldPrompt)
    assert.equal(context.panel.promptSyncPending.value, true)
  })
}

test('a newer input view refreshes the full parent before a failed POST, leaving retry available', async t => {
  let reads = 0, attempts = 0
  const context = await editor(t, call => {
    if (call.url === '/api/video-studio/project-a') return record(2, { prompt: '另一标签保存的新正文' })
    if (call.method === 'POST') { attempts++; throw new Error('模拟同步失败') }
    return inputView({ revision: ++reads === 1 ? 1 : 2, status: 'stale' })
  })
  assert.equal(await context.panel.syncPrompts(), false)
  assert.equal(context.project.value.revision, 2)
  assert.equal(context.panel.revision.value, 2)
  assert.equal(context.project.value.shots[0].video_prompt, '另一标签保存的新正文')
  assert.equal(context.panel.canSyncPrompts.value, true)
  assert.equal(await context.panel.syncPrompts(), false)
  assert.equal(attempts, 2)
})

test('manual confirmation displays newer prose and requires another explicit confirmation', async t => {
  let reads = 0, confirmed = false
  const context = await editor(t, call => {
    if (call.url === '/api/video-studio/project-a') return record(2, { prompt: '尚未看过的新正文' })
    if (call.method === 'POST') { confirmed = true; return record(3, { prompt: '尚未看过的新正文' }) }
    return inputView({ revision: ++reads === 1 ? 1 : confirmed ? 3 : 2, status: confirmed ? 'current' : 'stale' })
  })
  assert.equal(await context.panel.confirmPrompts(), false)
  assert.equal(context.project.value.shots[0].video_prompt, '尚未看过的新正文')
  assert.match(context.panel.message.value, /新正文.*再次点击/)
  assert.equal(context.calls.filter(call => call.method === 'POST').length, 0)
  assert.equal(context.panel.canConfirmPrompts.value, true)
  assert.equal(await context.panel.confirmPrompts(), true)
  assert.equal(context.calls.filter(call => call.method === 'POST').length, 1)
})
