import assert from 'node:assert/strict'
import test from 'node:test'
import { useWorkspace } from '../src/useWorkspace.js'
import { deferred, installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

async function workspace(t) {
  const browser = installBrowserGlobals({
    fetch: async (url) => Response.json(url === '/api/session'
      ? { user: null, auth_mode: 'account' } : {}),
  })
  const mounted = mountComposable(useWorkspace)
  t.after(() => { mounted.unmount(); browser.restore() })
  await mounted.flush()
  const w = mounted.instance
  w.session.value = { user: { id: 1 }, auth_mode: 'account' }
  return { w, mounted }
}

const audio = (id, text = id) => ({
  project_id: id, available: true, segments: [{ index: 1, text }], task: { status: 'idle' },
})
const job = (id, extra = {}) => ({ id, status: 'completed', request: { project_name: id }, ...extra })

test('missing IndexTTS default is blocked before submit; saved and uploaded voices remain usable', async (t) => {
  const { w } = await workspace(t)
  w.form.project_name = 'Voice prerequisite'
  w.form.script = 'A short test.'
  w.form.step_mode = true
  w.ttsEngine.value = 'indextts25'
  w.health.value = { tts25_online: true }
  w.settings.value = { tts: { voices: ['saved.wav'] } }
  w.form.tts_voice_id = 'missing.wav'
  assert.equal(w.generationBlockReason.value?.code, '[INDEXTTS25_VOICE_REQUIRED]')
  for (const voice of ['saved.wav', 'upload:owned-reference']) {
    w.form.tts_voice_id = voice
    assert.notEqual(w.generationBlockReason.value?.code, '[INDEXTTS25_VOICE_REQUIRED]')
  }
})

test('media requests retain adopted provenance only while the actual story text matches', async (t) => {
  const { w } = await workspace(t)
  const source = { story_id: 'a'.repeat(32), story_revision: 3, story_sha256: 'b'.repeat(64), adoption_id: 'c'.repeat(32) }
  w.form.story_source = source
  w.form._story_adopted_text = 'An adopted manuscript.'
  w.form._story_requirements = 'Keep the red scarf.'
  w.form._story_title = 'Saved title'
  w.form._story_project_handoff = { status: 'detached', message: 'Server state' }
  w.form.story_snapshot = { story: 'Client supplied body' }
  w.form.story_handoff = { status: 'current' }
  w.form.story_requirements = 'Client supplied requirements'
  w.form.script = '  An adopted manuscript.  '
  const adopted = w.generationRequestPayload()
  assert.deepEqual(adopted.story_source, source)
  assert.equal(Object.hasOwn(adopted, '_story_adopted_text'), false)
  for (const field of ['_story_requirements', '_story_title', '_story_project_handoff', 'story_snapshot', 'story_handoff', 'story_requirements']) assert.equal(Object.hasOwn(adopted, field), false, field)
  w.form.script = 'A different ending written in the production form.'
  assert.equal(w.generationRequestPayload().story_source, null)
  assert.deepEqual(w.form.story_source, source, 'original provenance remains available in the saved draft')
})

test('opening an independent job clears the previous story provenance', async (t) => {
  const { w } = await workspace(t)
  const source = { story_id: 'a'.repeat(32), story_revision: 3, story_sha256: 'b'.repeat(64) }
  w.hydrateGuidedForm(job('adopted', { request: { script: 'Story A', story_source: source, story_snapshot: { story: 'Story A', title: 'Title A', brief: { idea: 'Only A requirements' } } } }))
  assert.deepEqual(w.generationRequestPayload().story_source, source)
  assert.equal(w.form._story_requirements, 'Only A requirements')
  assert.equal(w.form._story_title, 'Title A')
  w.hydrateGuidedForm(job('independent', { request: { script: 'Story B' } }))
  assert.equal(w.form.story_source, null)
  assert.equal(w.form._story_adopted_text, '')
  assert.equal(w.form._story_requirements, '')
  assert.equal(w.form._story_title, '')
  assert.equal(w.form._story_project_handoff, null)
  assert.equal(w.generationRequestPayload().story_source, null)
})

test('a late TTS error cannot overwrite the new editor or release its loading lock', async (t) => {
  const { w } = await workspace(t)
  const first = deferred(), second = deferred()
  t.mock.method(w.api, 'ttsEditor', (id) => id === 'A' ? first.promise : second.promise)
  w.visualEditorProjectId.value = 'A'
  const loadA = w.loadTtsEditor()
  w.invalidateProjectRequests()
  w.visualEditorProjectId.value = 'B'
  w.ttsEditor.value = audio('B', 'existing B')
  const loadB = w.loadTtsEditor()
  first.reject(new Error('A failed'))
  await loadA
  assert.equal(w.ttsEditor.value.segments[0].text, 'existing B')
  assert.equal(w.ttsEditorLoading.value, true)
  second.resolve(audio('B', 'fresh B'))
  await loadB
  assert.equal(w.ttsEditor.value.segments[0].text, 'fresh B')
  assert.equal(w.ttsEditorLoading.value, false)
})

test('returning A -> B -> A does not accept the first visit response', async (t) => {
  const { w } = await workspace(t)
  const first = deferred(), latest = deferred()
  let calls = 0
  t.mock.method(w.api, 'ttsEditor', () => ++calls === 1 ? first.promise : latest.promise)
  w.visualEditorProjectId.value = 'A'
  const oldLoad = w.loadTtsEditor()
  w.invalidateProjectRequests()
  w.visualEditorProjectId.value = 'B'
  w.invalidateProjectRequests()
  w.visualEditorProjectId.value = 'A'
  const newLoad = w.loadTtsEditor()
  latest.resolve(audio('A', 'new visit'))
  await newLoad
  first.resolve(audio('A', 'old visit'))
  await oldLoad
  assert.equal(w.ttsEditor.value.segments[0].text, 'new visit')
})

test('the newest load of the same editor wins without a navigation', async (t) => {
  const { w } = await workspace(t)
  const first = deferred(), latest = deferred()
  let calls = 0
  t.mock.method(w.api, 'ttsEditor', () => ++calls === 1 ? first.promise : latest.promise)
  w.visualEditorProjectId.value = 'A'
  const oldLoad = w.loadTtsEditor(), newLoad = w.loadTtsEditor()
  latest.resolve(audio('A', 'latest revision'))
  await newLoad
  first.resolve(audio('A', 'older revision'))
  await oldLoad
  assert.equal(w.ttsEditor.value.segments[0].text, 'latest revision')
})

test('account identity rejects a TTS response even if the caller did not invalidate navigation', async (t) => {
  const { w } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'ttsEditor', () => response.promise)
  w.visualEditorProjectId.value = 'A'
  const pending = w.loadTtsEditor()
  w.session.value = { user: { id: 2 }, auth_mode: 'account' }
  w.ttsEditor.value = audio('A', 'account two')
  response.resolve(audio('A', 'account one'))
  await pending
  assert.equal(w.ttsEditor.value.segments[0].text, 'account two')
})

test('visual presentation follow-up cannot hydrate another project after its first response', async (t) => {
  const { w, mounted } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'visualEditor', async () => ({ items: [{ id: 'A-image' }], task: { status: 'idle' } }))
  const detail = t.mock.method(w.api, 'job', () => response.promise)
  w.visualEditorProjectId.value = 'A'
  const pending = w.loadVisualEditor({ hydrateBgm: true })
  await mounted.flush()
  assert.equal(detail.mock.callCount(), 1)
  w.invalidateProjectRequests()
  w.visualEditorProjectId.value = 'B'
  w.visualEditor.value = { items: [{ id: 'B-image' }], task: { status: 'idle' } }
  w.visualRenderMode.value = 'both'
  response.resolve(job('A', { request: { video_render_variant: 'raw' } }))
  await pending
  assert.deepEqual(w.visualEditor.value.items.map((item) => item.id), ['B-image'])
  assert.equal(w.visualRenderMode.value, 'both')
})

test('late subtitle save preserves the new project drafts and saving lock', async (t) => {
  const { w } = await workspace(t)
  const first = deferred(), second = deferred()
  const saves = t.mock.method(w.api, 'saveStepWorkflowSubtitles', (id) => id === 'A' ? first.promise : second.promise)
  w.activeJob.value = job('A')
  w.guidedSubtitles.value = [{ slide_id: 'line', text: 'old A' }]
  w.guidedSubtitleDrafts.line = 'new A'
  const saveA = w.saveGuidedSubtitles()
  w.invalidateProjectRequests()
  w.activeJob.value = job('B')
  w.guidedSubtitles.value = [{ slide_id: 'line', text: 'old B' }]
  w.guidedSubtitleDrafts.line = 'new B'
  w.guidedStageMessage.value = 'B editing'
  const saveB = w.saveGuidedSubtitles()
  first.resolve({ items: [{ slide_id: 'line', text: 'new A' }] })
  await saveA
  assert.equal(w.guidedSubtitleDrafts.line, 'new B')
  assert.equal(w.guidedStageMessage.value, 'B editing')
  assert.equal(w.guidedSubtitleSaving.value, true)
  assert.deepEqual(saves.mock.calls.map((call) => call.arguments), [['A', { line: 'new A' }], ['B', { line: 'new B' }]])
  second.resolve({ items: [{ slide_id: 'line', text: 'new B' }] })
  await saveB
  assert.equal(w.guidedSubtitleSaving.value, false)
  assert.equal(w.guidedSubtitles.value[0].text, 'new B')
})

test('a subtitle read started before save cannot restore the earlier text', async (t) => {
  const { w } = await workspace(t)
  const earlier = deferred()
  t.mock.method(w.api, 'stepWorkflowSubtitles', () => earlier.promise)
  t.mock.method(w.api, 'saveStepWorkflowSubtitles', async () => ({ items: [{ slide_id: 'line', text: 'saved' }] }))
  w.activeJob.value = job('A')
  w.guidedSubtitles.value = [{ slide_id: 'line', text: 'original' }]
  w.guidedSubtitleDrafts.line = 'saved'
  const load = w.loadGuidedSubtitles()
  await w.saveGuidedSubtitles()
  earlier.resolve({ items: [{ slide_id: 'line', text: 'original' }] })
  await load
  assert.equal(w.guidedSubtitleDrafts.line, 'saved')
  assert.equal(w.guidedSubtitleLoading.value, false)
})

test('late TTS completion cannot update the new project or start its reload', async (t) => {
  const { w } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'ttsEditorStatus', () => response.promise)
  const reload = t.mock.method(w.api, 'ttsEditor', async (id) => audio(id))
  w.visualEditorOpen.value = true
  w.visualEditorProjectId.value = 'A'
  w.ttsEditor.value = { ...audio('A'), revision: 1, task: { status: 'running' } }
  const poll = w.pollTtsEditorStatus()
  w.invalidateProjectRequests()
  w.visualEditorProjectId.value = 'B'
  w.ttsEditor.value = audio('B')
  response.resolve({ revision: 2, task: { status: 'completed' } })
  await poll
  assert.equal(w.ttsEditor.value.project_id, 'B')
  assert.equal(w.ttsEditor.value.task.status, 'idle')
  assert.equal(reload.mock.callCount(), 0)
})

test('a background selected-job response cannot restore a departed project', async (t) => {
  const { w } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'job', () => response.promise)
  w.activeJob.value = job('A')
  w.guidedCreatingNew.value = false
  const refresh = w.selectJob('A', false)
  w.invalidateProjectRequests()
  w.activeJob.value = job('B')
  response.resolve(job('A', { status: 'failed' }))
  assert.equal(await refresh, null)
  assert.equal(w.activeJob.value.id, 'B')
})

test('a retained guided task cannot hydrate a new draft through its watcher', async (t) => {
  const { w, mounted } = await workspace(t)
  w.guidedCreatingNew.value = true
  w.form.step_mode = true
  w.form.script = 'new draft'
  w.activeJob.value = job('A', { status: 'waiting_confirmation', request: {
    step_mode: true, _step_workflow_version: 2, _step_mode_stage: 'audio_review', script: 'old project',
  } })
  await mounted.flush()
  assert.equal(w.form.script, 'new draft')
  assert.equal(w.visualEditorProjectId.value, '')
})

test('a refresh waiting on the old task cannot overwrite a newly selected project', async (t) => {
  const { w, mounted } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'health', async () => ({ ok: true }))
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 }, auth_mode: 'account' }))
  t.mock.method(w.api, 'apiKeySettings', async () => ({ keys: {} }))
  t.mock.method(w.api, 'jobs', async () => ({ jobs: [job('A')], page: 1, total: 1, total_pages: 1 }))
  const jobRead = t.mock.method(w.api, 'job', () => response.promise)
  w.followLiveJob.value = false
  w.activeJob.value = job('A')
  const refresh = w.refresh()
  await mounted.flush()
  assert.equal(jobRead.mock.callCount(), 1)
  w.invalidateProjectRequests()
  w.activeJob.value = job('B')
  w.jobs.value = [job('B')]
  response.resolve(job('A', { status: 'failed' }))
  await refresh
  assert.equal(w.activeJob.value.id, 'B')
  assert.deepEqual(w.jobs.value.map((item) => item.id), ['B'])
})

test('refresh updates the task list during a new draft without attaching a previous task', async (t) => {
  const { w } = await workspace(t)
  t.mock.method(w.api, 'health', async () => ({ ok: true }))
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 }, auth_mode: 'account' }))
  t.mock.method(w.api, 'apiKeySettings', async () => ({ keys: {} }))
  t.mock.method(w.api, 'jobs', async () => ({ jobs: [job('A')], page: 1, total: 1, total_pages: 1 }))
  t.mock.method(w.api, 'editorUploads', async () => ({ assets: [] }))
  t.mock.method(w.api, 'editorJobs', async () => ({ jobs: [] }))
  const jobRead = t.mock.method(w.api, 'job', async () => job('A'))
  w.guidedCreatingNew.value = true
  w.followLiveJob.value = false
  w.activeJob.value = null
  w.form.script = 'new draft'
  await w.refresh()
  assert.deepEqual(w.jobs.value.map((item) => item.id), ['A'])
  assert.equal(w.activeJob.value, null)
  assert.equal(w.form.script, 'new draft')
  assert.equal(jobRead.mock.callCount(), 0)
})

test('late visual render completion cannot replace another editor preview', async (t) => {
  const { w, mounted } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'visualEditorStatus', async () => ({ task: { status: 'completed', action: 'render' } }))
  const detail = t.mock.method(w.api, 'job', () => response.promise)
  w.activeJob.value = job('A')
  w.visualEditorOpen.value = true
  w.visualEditorProjectId.value = 'A'
  w.visualEditor.value = { items: [], task: { status: 'running', action: 'render' }, preview_version: 1 }
  const poll = w.pollVisualEditorTaskStatus()
  await mounted.flush()
  assert.equal(detail.mock.callCount(), 1)
  w.invalidateProjectRequests()
  w.activeJob.value = job('B')
  w.visualEditorProjectId.value = 'B'
  w.visualEditor.value = { items: [], task: { status: 'idle' }, preview_version: 20 }
  response.resolve(job('A', { artifacts: { video: 'A.mp4' } }))
  await poll
  assert.equal(w.visualEditor.value.preview_version, 20)
  assert.equal(w.activeJob.value.id, 'B')
})

test('the next refresh after initial account discovery loads tasks without hydrating a draft', async (t) => {
  const { w, mounted } = await workspace(t)
  const guided = job('old', { request: { step_mode: true, _step_workflow_version: 2, script: 'old text' } })
  w.session.value = { user: null, auth_mode: 'account' }
  w.guidedCreatingNew.value = true
  w.form.step_mode = true
  w.form.script = 'draft text'
  t.mock.method(w.api, 'health', async () => ({ ok: true }))
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 }, auth_mode: 'account' }))
  t.mock.method(w.api, 'apiKeySettings', async () => ({ keys: {} }))
  const list = t.mock.method(w.api, 'jobs', async () => ({ jobs: [guided], page: 1, total: 1, total_pages: 1 }))
  t.mock.method(w.api, 'editorUploads', async () => ({ assets: [] }))
  t.mock.method(w.api, 'editorJobs', async () => ({ jobs: [] }))
  await w.refresh()
  assert.equal(w.session.value.user.id, 1)
  assert.equal(list.mock.callCount(), 0)
  await w.refresh()
  await mounted.flush()
  assert.equal(list.mock.callCount(), 1)
  assert.equal(w.jobs.value[0].id, 'old')
  assert.equal(w.activeJob.value, null)
  assert.equal(w.form.script, 'draft text')
})

test('overlapping refresh ticks wait for the current pass instead of starving a slow task read', async (t) => {
  const { w, mounted } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'health', async () => ({ ok: true }))
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 }, auth_mode: 'account' }))
  t.mock.method(w.api, 'apiKeySettings', async () => ({ keys: {} }))
  t.mock.method(w.api, 'jobs', async () => ({ jobs: [job('A')], page: 1, total: 1, total_pages: 1 }))
  t.mock.method(w.api, 'editorUploads', async () => ({ assets: [] }))
  t.mock.method(w.api, 'editorJobs', async () => ({ jobs: [] }))
  const read = t.mock.method(w.api, 'job', () => response.promise)
  w.followLiveJob.value = false
  w.activeJob.value = job('A')
  const first = w.refresh()
  await mounted.flush()
  await w.refresh()
  assert.equal(read.mock.callCount(), 1)
  response.resolve(job('A', { status: 'failed' }))
  await first
  assert.equal(w.activeJob.value.status, 'failed')
})

test('slow editor status requests survive additional polling ticks', async (t) => {
  const { w } = await workspace(t)
  const tts = deferred(), visual = deferred()
  const readTts = t.mock.method(w.api, 'ttsEditorStatus', () => tts.promise)
  const readVisual = t.mock.method(w.api, 'visualEditorStatus', () => visual.promise)
  w.visualEditorProjectId.value = 'A'
  w.visualEditorOpen.value = true
  w.ttsEditor.value = audio('A')
  w.visualEditor.value = { items: [], task: { status: 'idle' } }
  const firstTts = w.pollTtsEditorStatus(), firstVisual = w.pollVisualEditorTaskStatus()
  await w.pollTtsEditorStatus()
  await w.pollVisualEditorTaskStatus()
  assert.equal(readTts.mock.callCount(), 1)
  assert.equal(readVisual.mock.callCount(), 1)
  tts.resolve({ task: { status: 'running' }, revision: 1 })
  visual.resolve({ task: { status: 'running' }, has_active_image_tasks: true })
  await Promise.all([firstTts, firstVisual])
  assert.equal(w.ttsEditor.value.task.status, 'running')
  assert.equal(w.visualEditor.value.task.status, 'running')
})

test('slow startup settings update available options without resetting a new selection', async (t) => {
  const response = deferred()
  const browser = installBrowserGlobals({ fetch: async (url) => {
    if (url === '/api/settings') return response.promise
    return Response.json(url === '/api/session' ? { user: null, auth_mode: 'account' } : {})
  } })
  const mounted = mountComposable(useWorkspace)
  t.after(() => { mounted.unmount(); browser.restore() })
  const w = mounted.instance
  w.invalidateProjectRequests()
  w.form.project_name = 'restored project'
  w.form.tts_speed = 1.25
  w.form.visual_style_prompt = 'restored style'
  const options = {
    tts: { defaults: { speed: 0.75 }, voices: ['voice.wav'], emotions: ['happy'] },
  }
  response.resolve(Response.json(options))
  await mounted.flush()
  assert.equal(w.form.project_name, 'restored project')
  assert.equal(w.form.tts_speed, 1.25)
  assert.equal(w.form.visual_style_prompt, 'restored style')
  assert.deepEqual(w.settings.value.tts.voices, ['voice.wav'])
  assert.deepEqual(w.settings.value.tts.emotions, ['happy'])
})

test('normal startup settings still hydrate the initial creation defaults', async (t) => {
  const browser = installBrowserGlobals({ fetch: async (url) => Response.json(
    url === '/api/settings' ? { tts: { defaults: { speed: 1.3, voice_id: 'default.wav' } } }
      : url === '/api/session' ? { user: null, auth_mode: 'account' } : {},
  ) })
  const mounted = mountComposable(useWorkspace)
  t.after(() => { mounted.unmount(); browser.restore() })
  await mounted.flush()
  assert.equal(mounted.instance.form.tts_speed, 1.3)
  assert.equal(mounted.instance.form.tts_voice_id, 'default.wav')
})

test('reconnecting can refresh settings while preserving every current form field', async (t) => {
  const { w } = await workspace(t)
  const next = { tts: { defaults: { speed: 0.7, voice_id: 'server.wav' } }, scripts: ['new option'] }
  t.mock.method(w.api, 'settings', async () => next)
  w.form.project_name = 'my project'
  w.form.tts_speed = 1.4
  w.form.visual_style_prompt = 'my style'
  const before = JSON.stringify(w.form)
  assert.equal(await w.loadSettings({ hydrateForm: false }), true)
  assert.equal(JSON.stringify(w.form), before)
  assert.deepEqual(w.settings.value, next)
})

test('settings from a previous account cannot replace the current options or form', async (t) => {
  const { w } = await workspace(t)
  const response = deferred()
  t.mock.method(w.api, 'settings', () => response.promise)
  const pending = w.loadSettings()
  w.session.value = { user: { id: 2 }, auth_mode: 'account' }
  w.invalidateProjectRequests()
  w.settings.value = { tts: { voices: ['account-two.wav'] } }
  w.form.tts_speed = 1.4
  response.resolve({ tts: { voices: ['account-one.wav'], defaults: { speed: 0.8 } } })
  assert.equal(await pending, false)
  assert.deepEqual(w.settings.value.tts.voices, ['account-two.wav'])
  assert.equal(w.form.tts_speed, 1.4)
})

test('a settings response cannot supersede a newer request across project navigation', async (t) => {
  const { w } = await workspace(t)
  const first = deferred(), second = deferred()
  let calls = 0
  t.mock.method(w.api, 'settings', () => ++calls === 1 ? first.promise : second.promise)
  const oldRequest = w.loadSettings()
  w.invalidateProjectRequests()
  const newRequest = w.loadSettings({ hydrateForm: false })
  second.resolve({ tts: { voices: ['fresh.wav'] } })
  await newRequest
  first.resolve({ tts: { voices: ['stale.wav'] } })
  assert.equal(await oldRequest, false)
  assert.deepEqual(w.settings.value.tts.voices, ['fresh.wav'])
})
