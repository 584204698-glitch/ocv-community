import assert from 'node:assert/strict'
import test from 'node:test'
import { useWorkspace } from '../src/useWorkspace.js'
import { useStudio } from '../src/useStudio.js'
import { projectCastBindingError, projectCastBindingPayload, restoreProjectCastBindings } from '../src/projectCastBindings.js'
import { deferred, installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const binding = (asset_id = 'asset-rabbit', extra = {}) => ({ asset_id, name: '小兔', constraints: '红围巾', ...extra })
async function studio(t) {
  const browser = installBrowserGlobals({ fetch: async url => Response.json(url === '/api/session' ? { user: null, auth_mode: 'account' } : {}) })
  const mounted = mountComposable(() => { const w = useWorkspace(); return { w, s: useStudio(w) } })
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  const { w, s } = mounted.instance
  w.session.value = { user: { id: 1 }, auth_mode: 'account' }
  w.health.value = { tts25_online: true }
  s.newProject('dynamic')
  // New drafts load account defaults before they are ready for submission.
  await mounted.flush()
  return { w, s, browser, mounted }
}

test('creation bindings serialize only explicit identity inputs and never infer from labels or names', () => {
  const input = [binding('real-upload-id', { name: ' 小兔 ', constraints: ' 红围巾 ', project_character_id: 'old-project-role', presence: 'opening', user_confirmed: true })]
  const payload = projectCastBindingPayload(input)
  assert.deepEqual(payload, [{ asset_id: 'real-upload-id', name: '小兔', constraints: '红围巾' }])
  payload[0].name = 'changed'
  assert.equal(input[0].name, ' 小兔 ')
  assert.deepEqual(restoreProjectCastBindings(undefined, ['real-upload-id']), [])
  assert.deepEqual(restoreProjectCastBindings([binding('missing'), binding('kept'), binding('kept')], ['kept']), [binding('kept')])
})

test('creation validation requires unique names and selected asset IDs with bounded text', () => {
  assert.equal(projectCastBindingError([], []), '')
  assert.equal(projectCastBindingError([binding()], ['asset-rabbit']), '')
  assert.match(projectCastBindingError([binding('other')], ['asset-rabbit']), /已移除/)
  assert.match(projectCastBindingError([binding('a', { name: ' ' })], ['a']), /称呼/)
  assert.match(projectCastBindingError([binding('a'), binding('a', { name: '狐狸' })], ['a']), /只能绑定一个/)
  assert.match(projectCastBindingError([binding('a'), binding('b', { name: ' 小兔 ' })], ['a', 'b']), /称呼不能重复/)
  assert.match(projectCastBindingError([binding('a', { name: '兔'.repeat(161) })], ['a']), /160/)
  assert.match(projectCastBindingError([binding('a', { constraints: '兔'.repeat(2001) })], ['a']), /2000/)
  assert.match(projectCastBindingError(Array.from({ length: 7 }, (_, index) => binding(String(index))), []), /最多绑定 6/)
})

test('dynamic requests snapshot bindings and other generation modes omit them', async t => {
  const { w } = await studio(t)
  w.form.reference_image_ids = ['asset-rabbit']
  w.form.project_cast_bindings = [binding()]
  const payload = w.generationRequestPayload()
  assert.deepEqual(payload.project_cast_bindings, [binding()])
  w.form.project_cast_bindings[0].name = 'another name'
  assert.equal(payload.project_cast_bindings[0].name, '小兔')
  w.form.dynamic_video = false
  assert.equal(Object.hasOwn(w.generationRequestPayload(), 'project_cast_bindings'), false)
  w.form.dynamic_video = true
  w.form.project_cast_bindings = []
  assert.deepEqual(w.generationRequestPayload().project_cast_bindings, [])
})

test('removing a reference cleans its binding while preserving the remaining asset identity and label', async t => {
  const { w } = await studio(t)
  w.form.reference_image_ids = ['a', 'b']
  w.form.reference_image_labels = { a: '图1', b: '图2' }
  w.referenceImageNames.value = ['same.png', 'same.png']
  w.form.project_cast_bindings = [binding('a'), binding('b', { name: '狐狸' })]
  w.removeReferenceImage(0)
  assert.deepEqual(w.form.reference_image_ids, ['b'])
  assert.equal(w.form.reference_image_labels.b, '图2')
  assert.deepEqual(w.form.project_cast_bindings, [binding('b', { name: '狐狸' })])
  assert.deepEqual(w.generationRequestPayload().project_cast_bindings, [binding('b', { name: '狐狸' })])
})

test('reference-only drafts preserve bindings independently through switching and owner isolation', async t => {
  const { w, s, browser, mounted } = await studio(t)
  w.form.project_name = 'A'
  w.form.reference_image_ids = ['a']
  w.form.project_cast_bindings = [binding('a', { name: '', constraints: '最后输入' })]
  s.newProject('dynamic')
  const saved = s.studioDrafts.value.find(draft => draft.name === 'A')
  assert.ok(saved, 'uploaded images and role edits save before a script is entered')
  assert.deepEqual(w.form.project_cast_bindings, [])
  s.newProject('dynamic', saved)
  assert.equal(w.form.project_cast_bindings[0].constraints, '最后输入')
  w.form.project_cast_bindings[0].name = '小兔'
  assert.equal(saved.form.project_cast_bindings[0].name, '')
  browser.timers.advance(650); await mounted.flush()
  assert.equal(JSON.parse(browser.localStorage.getItem('ocv.studio.drafts.v1:1'))[0].form.project_cast_bindings[0].name, '小兔')
  w.session.value = { user: { id: 2 }, auth_mode: 'account' }
  await mounted.flush()
  s.newProject('dynamic')
  assert.deepEqual(w.form.project_cast_bindings, [])
  assert.equal(s.studioDrafts.value.length, 0)
})

test('legacy drafts, new projects and legacy job hydration clear previous role bindings', async t => {
  const { w, s } = await studio(t)
  const original = { id: 'copy', form: { dynamic_video: true, script: 'story', reference_image_ids: ['a'], project_cast_bindings: [binding('a')] } }
  s.newProject('dynamic', original)
  assert.deepEqual(w.form.project_cast_bindings, [binding('a')])
  w.form.project_cast_bindings[0].constraints = 'independent edit'
  assert.equal(original.form.project_cast_bindings[0].constraints, '红围巾')
  s.newProject('dynamic', { id: 'legacy', form: { dynamic_video: true, script: 'old story', reference_image_ids: ['a'] } })
  assert.deepEqual(w.form.project_cast_bindings, [])
  w.form.project_cast_bindings = [binding('a')]
  w.hydrateGuidedForm({ request: { dynamic_video: true, reference_image_ids: ['a'] } })
  assert.deepEqual(w.form.project_cast_bindings, [])
  w.form.project_cast_bindings = [binding('a')]
  s.restoreDefaultsOnNewProject.value = false
  s.newProject('dynamic')
  assert.deepEqual(w.form.project_cast_bindings, [])
})

test('legacy parameter presets cannot inherit the current project role bindings', async t => {
  const { w } = await studio(t)
  w.form.reference_image_ids = ['a']
  w.form.project_cast_bindings = [binding('a')]
  w.selectedParameterPreset.value = 'legacy'
  t.mock.method(w.api, 'parameterPreset', async () => ({ parameters: { dynamic_video: true, reference_image_ids: [] } }))
  await w.loadSelectedParameterPreset()
  assert.deepEqual(w.form.project_cast_bindings, [])
})

test('an incomplete explicit binding prevents submission without inventing a name', async t => {
  const { w } = await studio(t)
  w.form.script = '一只小兔正在等待朋友。'
  w.form.reference_image_ids = ['a']
  w.form.reference_image_labels = { a: '小兔' }
  w.form.project_cast_bindings = [binding('a', { name: '' })]
  const create = t.mock.method(w.api, 'createJob', async () => { throw new Error('must not submit') })
  assert.equal(w.generationBlockReason.value.code, '[PROJECT_CAST_INVALID]')
  await w.submit({ applyResult: false })
  assert.equal(create.mock.callCount(), 0)
  assert.equal(w.form.project_cast_bindings[0].name, '')
})

test('binding edits during submission remain a draft and cannot change the accepted job', async t => {
  const { w, s, mounted } = await studio(t)
  w.form.script = '小兔等待狐狸。'
  w.form.reference_image_ids = ['a']
  w.form.project_cast_bindings = [binding('a')]
  const response = deferred()
  let submitted
  t.mock.method(w.api, 'createJob', parameters => { submitted = parameters; return response.promise })
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 }, auth_mode: 'account' }))
  t.mock.method(w.api, 'health', async () => ({ tts25_online: true }))
  t.mock.method(w.api, 'jobs', async () => ({ jobs: [] }))
  const launch = s.launch()
  assert.deepEqual(submitted.project_cast_bindings, [binding('a')])
  w.form.project_cast_bindings[0].constraints = '提交期间的新要求'
  response.resolve({ id: 'created', status: 'queued', request: submitted })
  await launch; await mounted.flush()
  assert.equal(s.studioPage.value, 'editor')
  assert.equal(w.form.project_cast_bindings[0].constraints, '红围巾')
  assert.equal(s.studioDrafts.value[0].form.project_cast_bindings[0].constraints, '提交期间的新要求')
  assert.equal(submitted.project_cast_bindings[0].constraints, '红围巾')
})
