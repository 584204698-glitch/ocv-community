import assert from 'node:assert/strict'
import test from 'node:test'
import { ref } from 'vue'
import { useVideoInputs, videoInputPayload } from '../src/useVideoInputs.js'
import { characterReferenceRepair, characterReviewPayload } from '../src/videoCharacterCoverage.js'
import { deferred, mountComposable } from './support/composableHarness.mjs'

const record = (id = 'project-a', revision = 1) => ({ id, revision, shots: [{ id: 'shot-a' }, { id: 'shot-b' }] })
const asset = id => ({ id, name: id, url: `/assets/${id}`, available: true, kind: 'image' })
function inputView(extra = {}) {
  return {
    project_id: 'project-a', shot_id: 'shot-a', revision: 1,
    selection: { input_mode: 'single_first', profile_id: '', end_frame_reference_id: '', reference_ids: [] },
    default_profile_id: 'first',
    profiles: [
      { id: 'first', name: '首帧流程', input_mode: 'single_first', reference_capacity: 0 },
      { id: 'end', name: '首尾帧流程', input_mode: 'first_last', reference_capacity: 0 },
      { id: 'refs', name: '多参流程', input_mode: 'composition_reference', reference_capacity: 2 },
    ],
    assets: [asset('a'), asset('b'), asset('c')], core_image: { available: true, url: '/core' }, blockers: [], last_request: null,
    ...extra,
  }
}

async function editor(t, request = async () => inputView()) {
  const project = ref(record()), shot = ref({ id: 'shot-a' }), accountKey = ref('account-a'), disabled = ref(false), projectCast = ref(null)
  const updates = [], calls = []
  const mounted = mountComposable(() => useVideoInputs({
    project, shot, accountKey, projectCast, disabled,
    request: (url, options = {}) => { calls.push({ url, options }); return request(url, options) },
    onUpdated: value => { updates.push(value); project.value = value },
  }))
  t.after(() => mounted.unmount())
  await mounted.flush()
  return { ...mounted, panel: mounted.instance, project, shot, accountKey, projectCast, disabled, updates, calls }
}

function chooseReferences(panel) {
  panel.setMode('composition_reference')
  panel.draft.value.profile_id = 'refs'
  panel.toggleReference('a')
}

test('choosing a project role only prefills identity and requires explicit presence before coverage save', async t => {
  let submitted
  const context = await editor(t, async (url, options) => {
    if (options.method === 'PUT') { submitted = JSON.parse(options.body); return record('project-a', 2) }
    return inputView(submitted ? { revision: 2, coverage: { status: 'partial', characters: submitted.characters, review: { characters: submitted.characters } } } : {})
  })
  const { panel, projectCast, calls } = context
  projectCast.value = { project_id: 'project-a', revision: 1, characters: [{ id: 'pc_rabbit', name: '小兔', reference_id: 'b', status: 'current' }] }
  chooseReferences(panel); panel.addCharacter()
  panel.coverageDraft.value[0].id = 'vc_existing'
  assert.equal(panel.selectProjectCharacter(0, 'pc_rabbit'), true)
  assert.deepEqual(panel.coverageDraft.value[0], { id: 'vc_existing', project_character_id: 'pc_rabbit', name: '小兔', reference_id: 'b', presence: '', in_last_frame: false })
  assert.equal(panel.canSaveCoverage.value, false)
  assert.equal(await panel.saveCoverage(), false)
  assert.equal(calls.filter(call => call.options.method === 'PUT').length, 0)
  assert.deepEqual(panel.draft.value.reference_ids, ['a'])
  panel.coverageDraft.value[0].presence = 'later'
  assert.equal(await panel.saveCoverage(), true); await context.flush()
  assert.equal(submitted.characters[0].project_character_id, 'pc_rabbit')
  assert.equal(submitted.characters[0].presence, 'later')
  assert.equal(panel.coverageDraft.value[0].project_character_id, 'pc_rabbit')
  assert.deepEqual(panel.draft.value.reference_ids, ['a'], 'saving coverage preserves the unadopted input draft')
  assert.equal(panel.selectProjectCharacter(0, ''), true)
  assert.equal(Object.hasOwn(panel.coverageDraft.value[0], 'project_character_id'), false)
  assert.equal(panel.coverageDraft.value[0].name, '小兔')
})

test('core identity confirmation is opt in, opening only and cleared for a changed role', async t => {
  assert.equal(characterReviewPayload([{ name: 'A', presence: 'opening' }])[0].core_identity_confirmed, undefined)
  assert.equal(characterReviewPayload([{ name: 'A', presence: 'opening', core_identity_confirmed: true }])[0].core_identity_confirmed, true)
  assert.equal(characterReviewPayload([{ name: 'A', presence: 'later', core_identity_confirmed: true }])[0].core_identity_confirmed, undefined)
  const { panel, projectCast } = await editor(t)
  projectCast.value = { project_id: 'project-a', characters: [{ id: 'pc_a', name: 'A', reference_id: 'a', status: 'current' }] }
  panel.addCharacter()
  panel.coverageDraft.value[0].core_identity_confirmed = true
  panel.selectProjectCharacter(0, 'pc_a')
  assert.equal(panel.coverageDraft.value[0].core_identity_confirmed, undefined)
})

test('stale core coverage never prechecks identity confirmation on refresh', async t => {
  const row = { id: 'c', name: 'A', presence: 'opening', core_identity_confirmed: true }
  const { panel } = await editor(t, async () => inputView({ coverage: {
    status: 'stale', characters: [row], review: { characters: [row] },
  } }))
  assert.equal(panel.coverageDraft.value[0].core_identity_confirmed, undefined)
  assert.equal(panel.coverageDirty.value, false)
})

test('stale or changed project roles cannot silently replace a linked shot declaration', async t => {
  const { panel, projectCast } = await editor(t)
  projectCast.value = { project_id: 'project-a', revision: 1, characters: [{ id: 'pc_rabbit', name: '小兔', reference_id: 'b', status: 'stale' }] }
  panel.addCharacter(); panel.coverageDraft.value[0].name = '手填小兔'
  assert.equal(panel.selectProjectCharacter(0, 'pc_rabbit'), false)
  assert.equal(panel.coverageDraft.value[0].name, '手填小兔')
  projectCast.value.characters[0].status = 'current'
  assert.equal(panel.selectProjectCharacter(0, 'pc_rabbit'), true)
  panel.coverageDraft.value[0].presence = 'opening'
  projectCast.value.characters[0].reference_id = 'c'
  assert.equal(panel.coverageDraft.value[0].reference_id, 'b')
  assert.equal(panel.canSaveCoverage.value, false)
  assert.match(panel.coverageValidation.value, /资料已变化/)
  panel.selectProjectCharacter(0, 'pc_rabbit')
  assert.equal(panel.coverageDraft.value[0].reference_id, 'c')
  assert.equal(panel.coverageDraft.value[0].presence, '')
  projectCast.value = { ...projectCast.value, project_id: 'project-b' }
  assert.equal(panel.projectCharacters.value.length, 0)
})

test('saving project cast refreshes coverage at the new revision while preserving unadopted input choices', async t => {
  let revision = 1
  const context = await editor(t, () => inputView({ revision }))
  chooseReferences(context.panel)
  context.projectCast.value = { project_id: 'project-a', revision: 1, characters: [] }
  revision = 2
  context.project.value = record('project-a', 2)
  context.projectCast.value = { project_id: 'project-a', revision: 2, characters: [] }
  await context.flush()
  assert.equal(context.panel.revision.value, 2)
  assert.deepEqual(context.panel.draft.value.reference_ids, ['a'])
  assert.equal(context.panel.inputDirty.value, true)
  assert.equal(context.calls.length, 2)
})

test('project cast updates retain an unsaved character review instead of overwriting it', async t => {
  const context = await editor(t)
  chooseReferences(context.panel); context.panel.addCharacter(); context.panel.coverageDraft.value[0].name = '未保存的角色'
  context.project.value = record('project-a', 2)
  context.projectCast.value = { project_id: 'project-a', revision: 2, characters: [] }
  await context.flush()
  assert.equal(context.panel.coverageDraft.value[0].name, '未保存的角色')
  assert.deepEqual(context.panel.draft.value.reference_ids, ['a'])
  assert.equal(context.panel.revision.value, 1)
  assert.equal(context.calls.length, 1)
})

test('mode changes preserve choices without silently choosing a different workflow', async t => {
  const { panel } = await editor(t)
  panel.setMode('first_last')
  assert.equal(panel.draft.value.profile_id, '')
  assert.match(panel.validation.value, /工作流/)
  panel.draft.value.profile_id = 'end'
  panel.selectEndFrame('b')
  panel.setMode('composition_reference')
  assert.equal(panel.draft.value.profile_id, 'end')
  panel.draft.value.profile_id = 'refs'
  panel.toggleReference('a')
  assert.deepEqual(videoInputPayload(panel.draft.value), {
    input_mode: 'composition_reference', profile_id: 'refs', end_frame_reference_id: '', reference_ids: ['a'],
  })
  panel.setMode('first_last')
  assert.equal(panel.draft.value.end_frame_reference_id, 'b')
  assert.deepEqual(panel.draft.value.reference_ids, ['a'])
  panel.setMode('single_first')
  assert.equal(panel.draft.value.profile_id, '')
  assert.deepEqual(videoInputPayload(panel.draft.value).reference_ids, [])
  assert.equal(videoInputPayload(panel.draft.value).end_frame_reference_id, '')
})

test('reference capacity counts additional pictures and preserves explicit ordering', async t => {
  const { panel } = await editor(t)
  chooseReferences(panel)
  panel.toggleReference('b')
  panel.toggleReference('c')
  assert.equal(panel.referenceLimit.value, 2)
  assert.deepEqual(panel.draft.value.reference_ids, ['a', 'b'])
  panel.moveReference(1, -1)
  assert.deepEqual(panel.draft.value.reference_ids, ['b', 'a'])
})

test('a same-project revision poll preserves dirty inputs and their original save revision', async t => {
  const put = deferred()
  const context = await editor(t, async (url, options) => options.method === 'PUT' ? put.promise : inputView())
  const { panel, project, calls } = context
  chooseReferences(panel)
  project.value = record('project-a', 9)
  await context.flush()
  assert.equal(panel.revision.value, 1)
  assert.equal(panel.revisionChanged.value, true)
  assert.deepEqual(panel.draft.value.reference_ids, ['a'])
  assert.equal(calls.length, 1)
  const pending = panel.save()
  assert.equal(JSON.parse(calls.at(-1).options.body).revision, 1)
  put.reject(Object.assign(new Error('revision conflict'), { status: 409 }))
  assert.equal(await pending, false)
  assert.equal(panel.conflict.value, true)
  assert.equal(panel.dirty.value, true)
  assert.equal(panel.canSave.value, false)
  assert.match(panel.error.value, /不会自动重试/)
  await panel.save()
  assert.equal(calls.length, 2)
})

test('upload only adds a candidate and advances revision without adopting it', async t => {
  const context = await editor(t, async (url, options) => options.method === 'POST'
    ? { project: record('project-a', 2), asset: asset('new') } : inputView())
  const { panel, updates, calls } = context
  chooseReferences(panel)
  assert.equal(await panel.upload(new Blob(['image'], { type: 'image/png' })), true)
  assert.equal(calls.at(-1).options.body.get('revision'), '1')
  assert.equal(calls.at(-1).url, '/api/video-studio/project-a/video-input-assets')
  assert.equal(panel.revision.value, 2)
  assert.deepEqual(panel.assets.value.map(item => item.id), ['a', 'b', 'c', 'new'])
  assert.deepEqual(panel.draft.value.reference_ids, ['a'])
  assert.equal(panel.dirty.value, true)
  assert.equal(updates.length, 1)
  assert.match(panel.message.value, /候选库/)
})

test('saving sends only active inputs and then reads the accepted configuration', async t => {
  let saved = null
  const context = await editor(t, async (url, options) => {
    if (options.method === 'PUT') { saved = JSON.parse(options.body); return record('project-a', 2) }
    return saved ? inputView({ revision: 2, selection: saved }) : inputView()
  })
  const { panel, updates } = context
  panel.setMode('first_last')
  panel.draft.value.profile_id = 'end'
  panel.selectEndFrame('b')
  panel.draft.value.reference_ids = ['a']
  assert.equal(await panel.save(), true)
  await context.flush()
  assert.deepEqual(saved, { revision: 1, input_mode: 'first_last', profile_id: 'end', end_frame_reference_id: 'b', reference_ids: [] })
  assert.equal(panel.dirty.value, false)
  assert.equal(panel.revision.value, 2)
  assert.deepEqual(panel.draft.value.reference_ids, ['a'])
  assert.equal(updates.length, 1)
})

test('late shot reads cannot overwrite a later visit to the same shot', async t => {
  const first = deferred(), second = deferred(), third = deferred()
  const reads = [first, second, third]
  const context = await editor(t, () => reads.shift().promise)
  context.shot.value = { id: 'shot-b' }
  context.shot.value = { id: 'shot-a' }
  third.resolve(inputView({ revision: 3 }))
  await context.flush()
  first.resolve(inputView({ revision: 1 }))
  second.resolve(inputView({ shot_id: 'shot-b', revision: 2 }))
  await context.flush()
  assert.equal(context.panel.revision.value, 3)
  assert.equal(context.panel.loading.value, false)
})

test('account switch rejects an in-flight upload and keeps the new loading lock', async t => {
  const pendingUpload = deferred(), accountRead = deferred()
  let reads = 0
  const context = await editor(t, (url, options) => options.method === 'POST'
    ? pendingUpload.promise : (++reads === 1 ? inputView() : accountRead.promise))
  const pending = context.panel.upload(new Blob(['image'], { type: 'image/png' }))
  context.accountKey.value = 'account-b'
  pendingUpload.resolve({ project: record('project-a', 2), asset: asset('old-account') })
  assert.equal(await pending, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.panel.loading.value, true)
  assert.equal(context.panel.uploading.value, false)
  accountRead.resolve(inputView({ assets: [asset('new-account')] }))
  await context.flush()
  assert.deepEqual(context.panel.assets.value.map(item => item.id), ['new-account'])
})

test('project switch rejects a late save without clearing the new project draft', async t => {
  const save = deferred()
  const context = await editor(t, (url, options) => options.method === 'PUT' ? save.promise
    : inputView({ project_id: url.includes('project-b') ? 'project-b' : 'project-a' }))
  chooseReferences(context.panel)
  const pending = context.panel.save()
  context.project.value = record('project-b')
  await context.flush()
  context.panel.setMode('first_last')
  context.panel.draft.value.profile_id = 'end'
  context.panel.selectEndFrame('b')
  save.resolve(record('project-a', 2))
  assert.equal(await pending, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.panel.draft.value.input_mode, 'first_last')
  assert.equal(context.panel.dirty.value, true)
})

test('the newest read wins when a clean project revision changes during loading', async t => {
  const first = deferred(), newer = deferred()
  let reads = 0
  const context = await editor(t, () => ++reads === 1 ? first.promise : newer.promise)
  context.project.value = record('project-a', 2)
  newer.resolve(inputView({ revision: 2 }))
  await context.flush()
  first.resolve(inputView({ revision: 1 }))
  await context.flush()
  assert.equal(context.panel.revision.value, 2)
})

test('unmount rejects a pending save and performs no follow-up fetch', async t => {
  const save = deferred()
  const context = await editor(t, (url, options) => options.method === 'PUT' ? save.promise : inputView())
  chooseReferences(context.panel)
  const pending = context.panel.save()
  context.unmount()
  save.resolve(record('project-a', 2))
  assert.equal(await pending, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.calls.length, 2)
})

const character = (id, extra = {}) => ({
  id, name: id, presence: 'later', reference_id: id, in_last_frame: false,
  reference_available: true, covered: false, via: '', reason: '尚未采用角色参考图', ...extra,
})
function coverageView(characters = [character('b')], extra = {}) {
  return { status: 'partial', message: '部分角色缺少图片依据。', characters,
    suggested_reference_ids: characters.map(item => item.reference_id).filter(Boolean),
    manual_prompt_warning: '',
    review: { characters: characters.map(({ id, name, presence, reference_id, in_last_frame }) => ({ id, name, presence, reference_id, in_last_frame })) },
    ...extra,
  }
}
function repairPlan(extra = {}) {
  const view = inputView()
  return characterReferenceRepair({ coverage: coverageView(), draft: view.selection,
    assets: view.assets, profiles: view.profiles, defaultProfileId: view.default_profile_id, ...extra })
}

test('missing coverage is unknown and an empty array needs explicit no-character confirmation', async t => {
  let saved = null
  const context = await editor(t, async (url, options) => {
    if (options.method === 'PUT') { saved = JSON.parse(options.body); return record('project-a', 2) }
    return inputView({ revision: saved ? 2 : 1, coverage: saved ? coverageView([], { status: 'empty' }) : undefined })
  })
  const { panel } = context
  assert.equal(panel.coverage.value.status, 'unknown')
  assert.equal(panel.noCharactersConfirmed.value, false)
  assert.equal(panel.coverageDirty.value, false)
  assert.equal(panel.canSaveCoverage.value, false)
  assert.equal(await panel.saveCoverage(), false)
  panel.noCharactersConfirmed.value = true
  assert.equal(panel.dirty.value, true)
  assert.equal(await panel.saveCoverage(), true)
  await context.flush()
  assert.deepEqual(saved, { revision: 1, characters: [] })
  assert.match(context.calls.find(call => call.options.method === 'PUT').url, /character-coverage$/)
  assert.equal(panel.coverage.value.status, 'empty')
  assert.equal(panel.noCharactersConfirmed.value, true)
  assert.equal(panel.dirty.value, false)
})

test('a partial character review saves without a reference and preserves the input draft', async t => {
  let saved = null
  const context = await editor(t, async (url, options) => {
    if (options.method === 'PUT') { saved = JSON.parse(options.body); return record('project-a', 2) }
    return inputView({ revision: saved ? 2 : 1, coverage: saved ? coverageView(saved.characters.map(item => character('new-id', { ...item, id: 'new-id', reference_available: false }))) : undefined })
  })
  const { panel } = context
  chooseReferences(panel)
  panel.addCharacter()
  panel.coverageDraft.value[0].name = '  兔子  '
  panel.coverageDraft.value[0].presence = 'later'
  assert.equal(panel.canSaveCoverage.value, true)
  assert.equal(panel.canSave.value, false)
  assert.equal(await panel.saveCoverage(), true)
  await context.flush()
  assert.deepEqual(saved.characters, [{ id: '', name: '兔子', presence: 'later', reference_id: '', in_last_frame: false }])
  assert.equal(panel.coverage.value.status, 'partial')
  assert.equal(panel.coverageDirty.value, false)
  assert.equal(panel.coverageDraft.value[0].id, 'new-id')
  assert.deepEqual(panel.draft.value.reference_ids, ['a'])
  assert.equal(panel.inputDirty.value, true)
  assert.equal(panel.canSave.value, true)
  assert.equal(panel.repairReferences(), false)
  assert.match(panel.error.value, /绑定可用参考图/)
})

test('removing the final character cannot silently turn the review into no characters', async t => {
  const { panel } = await editor(t, () => inputView({ coverage: coverageView() }))
  panel.removeCharacter(0)
  assert.equal(panel.noCharactersConfirmed.value, false)
  assert.equal(panel.coverageDirty.value, true)
  assert.equal(panel.canSaveCoverage.value, false)
  panel.noCharactersConfirmed.value = true
  assert.equal(panel.canSaveCoverage.value, true)
  panel.addCharacter()
  assert.equal(panel.noCharactersConfirmed.value, false)
  assert.equal(panel.canSaveCoverage.value, false)
})

test('last-frame visibility requires an adopted, available last frame, not a local draft', async t => {
  const { panel } = await editor(t, () => inputView({ coverage: coverageView() }))
  panel.setMode('first_last')
  panel.draft.value.profile_id = 'end'
  panel.selectEndFrame('c')
  panel.coverageDraft.value[0].in_last_frame = true
  assert.equal(panel.canUseLastFrame.value, false)
  assert.equal(panel.canSaveCoverage.value, false)
  assert.match(panel.coverageValidation.value, /先采用/)
  panel.coverageDraft.value[0].in_last_frame = false
  panel.coverageDraft.value[0].name = '重新确认的角色'
  assert.equal(panel.canSaveCoverage.value, true)
})

test('coverage conflict retains the review and original revision without a retry', async t => {
  const pending = deferred()
  const context = await editor(t, (url, options) => options.method === 'PUT' ? pending.promise : inputView({ coverage: coverageView() }))
  const { panel } = context
  panel.coverageDraft.value[0].name = '新的称呼'
  context.project.value = record('project-a', 3)
  await context.flush()
  assert.equal(context.calls.length, 1)
  assert.equal(panel.revision.value, 1)
  const save = panel.saveCoverage()
  assert.equal(JSON.parse(context.calls.at(-1).options.body).revision, 1)
  pending.reject(Object.assign(new Error('conflict'), { status: 409 }))
  assert.equal(await save, false)
  assert.equal(panel.coverageDraft.value[0].name, '新的称呼')
  assert.equal(panel.coverageDirty.value, true)
  assert.equal(panel.conflict.value, true)
  assert.equal(await panel.saveCoverage(), false)
  assert.equal(context.calls.length, 2)
  assert.match(panel.error.value, /不会自动重试/)
})

test('coverage save shares the busy lock with input saving and upload', async t => {
  const pending = deferred()
  const context = await editor(t, (url, options) => options.method === 'PUT' ? pending.promise : inputView({ coverage: coverageView() }))
  const { panel } = context
  chooseReferences(panel)
  panel.coverageDraft.value[0].name = '新称呼'
  const save = panel.saveCoverage()
  assert.equal(panel.busy.value, true)
  assert.equal(await panel.saveCoverage(), false)
  assert.equal(await panel.save(), false)
  assert.equal(await panel.upload(new Blob(['test'])), false)
  assert.equal(await panel.load({ discardDraft: true }), false)
  assert.equal(context.calls.length, 2)
  pending.resolve(record('project-a', 2))
  await save
})

test('late coverage save from another project cannot clear the new unsaved review', async t => {
  const pending = deferred()
  const context = await editor(t, (url, options) => options.method === 'PUT' ? pending.promise : inputView({
    project_id: url.includes('project-b') ? 'project-b' : 'project-a', coverage: coverageView(),
  }))
  context.panel.coverageDraft.value[0].name = '旧项目草稿'
  const save = context.panel.saveCoverage()
  context.project.value = record('project-b')
  await context.flush()
  assert.equal(context.panel.coverageDraft.value[0].name, 'b')
  context.panel.coverageDraft.value[0].name = '新项目草稿'
  pending.resolve(record('project-a', 2))
  assert.equal(await save, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.panel.coverageDraft.value[0].name, '新项目草稿')
  assert.equal(context.panel.dirty.value, true)
})

test('account switch rejects a pending role save and keeps the new read lock', async t => {
  const pending = deferred(), nextRead = deferred()
  let reads = 0
  const context = await editor(t, (url, options) => options.method === 'PUT' ? pending.promise
    : (++reads === 1 ? inputView({ coverage: coverageView() }) : nextRead.promise))
  context.panel.coverageDraft.value[0].name = '旧账户角色'
  const save = context.panel.saveCoverage()
  context.accountKey.value = 'account-b'
  pending.resolve(record('project-a', 2))
  assert.equal(await save, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.panel.loading.value, true)
  assert.equal(context.panel.savingCoverage.value, false)
  nextRead.resolve(inputView({ coverage: coverageView([character('c')]) }))
  await context.flush()
  assert.equal(context.panel.coverageDraft.value[0].name, 'c')
})

test('a late coverage read cannot overwrite another visit to the same shot', async t => {
  const first = deferred(), second = deferred(), third = deferred()
  const reads = [first, second, third]
  const context = await editor(t, () => reads.shift().promise)
  context.shot.value = { id: 'shot-b' }
  context.shot.value = { id: 'shot-a' }
  third.resolve(inputView({ coverage: coverageView([character('c')]), revision: 3 }))
  await context.flush()
  context.panel.coverageDraft.value[0].name = '保留本次草稿'
  first.resolve(inputView({ coverage: coverageView([character('a')]) }))
  second.resolve(inputView({ shot_id: 'shot-b', coverage: coverageView([character('b')]) }))
  await context.flush()
  assert.equal(context.panel.coverageDraft.value[0].name, '保留本次草稿')
  assert.equal(context.panel.revision.value, 3)
})

test('repair appends explicit bindings in order, deduplicates them, and only changes the draft', async t => {
  const context = await editor(t, () => inputView({ coverage: coverageView([character('b'), character('second-b', { reference_id: 'b' })]) }))
  const { panel } = context
  panel.draft.value.reference_ids = ['a']
  const before = videoInputPayload(panel.draft.value)
  assert.equal(panel.repairReferences(), false)
  assert.deepEqual(videoInputPayload(panel.draft.value), before)
  assert.match(panel.error.value, /严格首帧/)
  panel.repairModeConfirmed.value = true
  assert.equal(panel.repairReferences(), true)
  assert.deepEqual(panel.draft.value.reference_ids, ['a', 'b'])
  assert.equal(panel.draft.value.input_mode, 'composition_reference')
  assert.equal(panel.draft.value.profile_id, 'refs')
  assert.equal(panel.dirty.value, true)
  assert.equal(context.calls.length, 1)
  assert.equal(context.updates.length, 0)
  assert.match(panel.message.value, /草稿/)
})

test('repair cannot infer identity from matching asset names or partially apply missing bindings', async t => {
  const { panel } = await editor(t, () => inputView({ coverage: coverageView([
    character('b'), character('c', { reference_id: '', reference_available: false }),
  ]) }))
  panel.repairModeConfirmed.value = true
  assert.equal(panel.repairReferences(), false)
  assert.deepEqual(panel.draft.value.reference_ids, [])
  assert.equal(panel.draft.value.input_mode, 'single_first')
  assert.equal(panel.dirty.value, false)
  assert.match(panel.error.value, /绑定可用参考图/)
})

test('repair includes explicitly bound opening portraits before later arrivals without inventing a core-only reference', () => {
  const result = repairPlan({ coverage: coverageView([
    character('opening', { presence: 'opening', covered: true, via: 'core', reference_id: 'a' }),
    character('core-only', { presence: 'opening', covered: true, via: 'core', reference_id: '', reference_available: false }),
    character('later', { reference_id: 'b' }),
  ]) })
  assert.equal(result.reason, '')
  assert.deepEqual(result.reference_ids, ['a', 'b'])
  const refused = repairPlan({ coverage: coverageView([
    character('opening', { presence: 'opening', covered: true, via: 'core', reference_id: 'a' }),
    character('later', { reference_id: 'b' }),
  ]), profiles: [{ id: 'small', input_mode: 'composition_reference', reference_capacity: 1 }] })
  assert.match(refused.reason, /容量不足/)
  assert.deepEqual(refused.reference_ids, [])
})

test('stale, unknown, dirty, or newer-revision coverage cannot auto-fill reference drafts', async t => {
  for (const status of ['stale', 'unknown']) {
    assert.match(repairPlan({ coverage: coverageView(undefined, { status }) }).reason, /核对/)
  }
  const context = await editor(t, () => inputView({ coverage: coverageView() }))
  context.panel.repairModeConfirmed.value = true
  context.panel.coverageDraft.value[0].name = '未保存'
  assert.equal(context.panel.repairReferences(), false)
  assert.match(context.panel.error.value, /先保存/)
  context.panel.coverageDraft.value[0].name = 'b'
  context.panel.draft.value.reference_ids = ['a']
  context.project.value = record('project-a', 2)
  assert.equal(context.panel.repairReferences(), false)
  assert.deepEqual(context.panel.draft.value.reference_ids, ['a'])
})

test('repair rejects insufficient capacity or unavailable selected assets with no partial draft change', async t => {
  const { panel } = await editor(t, () => inputView({ coverage: coverageView([character('c')]) }))
  panel.draft.value.reference_ids = ['a', 'b']
  panel.repairModeConfirmed.value = true
  const before = JSON.stringify(panel.draft.value)
  assert.equal(panel.repairReferences(), false)
  assert.equal(JSON.stringify(panel.draft.value), before)
  assert.match(panel.error.value, /容量不足/)
  const result = repairPlan({ draft: { ...inputView().selection, reference_ids: ['missing'] } })
  assert.match(result.reason, /已选参考图.*不可用/)
  assert.match(repairPlan({ assets: [asset('a'), { ...asset('b'), available: false }] }).reason, /可用参考图/)
})

test('repair prefers a usable default, selects a sole option, and requires a choice among multiple alternatives', () => {
  assert.equal(repairPlan({ defaultProfileId: 'refs' }).profile_id, '')
  assert.equal(repairPlan().profile_id, 'refs')
  const profiles = [...inputView().profiles, { id: 'another', name: '另一个多图流程', input_mode: 'composition_reference', reference_capacity: 8 }]
  const ambiguous = repairPlan({ profiles })
  assert.equal(ambiguous.needsProfileChoice, true)
  assert.match(ambiguous.reason, /多个/)
  assert.equal(repairPlan({ profiles, chosenProfileId: 'another' }).profile_id, 'another')
  assert.match(repairPlan({ profiles, chosenProfileId: 'missing' }).reason, /不可用/)
  assert.match(repairPlan({ profiles: [{ ...profiles.at(-1), available: false }] }).reason, /没有可用/)
})

test('leaving first-last mode also requires references for arrivals formerly covered by the last frame', () => {
  const result = repairPlan({
    draft: { ...inputView().selection, input_mode: 'first_last', profile_id: 'end', end_frame_reference_id: 'a' },
    coverage: coverageView([character('b'), character('c', { covered: true, via: 'last_frame', reference_id: '', reference_available: false, in_last_frame: true })]),
  })
  assert.match(result.reason, /绑定可用参考图/)
  assert.equal(result.changes, false)
})

test('manual prompts are warned when pictures are added or reordered; enabled Agent is distinct', async t => {
  const { panel, project } = await editor(t)
  chooseReferences(panel)
  assert.match(panel.manualPromptWarning.value, /不会自动改写/)
  panel.toggleReference('b')
  panel.moveReference(1, -1)
  assert.match(panel.manualPromptWarning.value, /图片编号/)
  project.value = { ...project.value, creation_parameters: { comfyui_h3_prompt_agent: true } }
  assert.equal(panel.manualPromptWarning.value, '')
})

test('a stale review can be explicitly reconfirmed without altering its existing fields', async t => {
  let puts = 0
  const context = await editor(t, (url, options) => options.method === 'PUT'
    ? (++puts, record('project-a', 2)) : inputView({ revision: puts ? 2 : 1, coverage: coverageView(undefined, { status: puts ? 'partial' : 'stale' }) }))
  assert.equal(context.panel.coverageDirty.value, false)
  assert.equal(context.panel.canSaveCoverage.value, true)
  assert.equal(await context.panel.saveCoverage(), true)
  await context.flush()
  assert.equal(puts, 1)
  assert.equal(context.panel.coverage.value.status, 'partial')
})
