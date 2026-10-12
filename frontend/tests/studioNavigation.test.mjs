import assert from 'node:assert/strict'
import test from 'node:test'
import { useWorkspace } from '../src/useWorkspace.js'
import { useStudio } from '../src/useStudio.js'
import { deferred, installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const job = (id, request = {}) => ({
  id, status: 'completed', request: { project_name: id, script: `story ${id}`, ...request },
})
const audio = (id) => ({ available: true, project_id: id, segments: [{ index: 1, text: id }], task: { status: 'idle' } })

async function studio(t, { href = 'http://localhost/', fetch: mockFetch } = {}) {
  const browser = installBrowserGlobals({
    fetch: mockFetch || (async (url) => Response.json(url === '/api/session' ? { user: null, auth_mode: 'account' } : {})),
  })
  browser.window.location.href = href
  const factory = () => { const w = useWorkspace(); return { w, s: useStudio(w) } }
  let mounted = mountComposable(factory)
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  return {
    ...mounted.instance, browser, mounted,
    async remount() {
      mounted.unmount()
      mounted = mountComposable(factory)
      await mounted.flush()
      return mounted.instance
    },
  }
}

function mockEditors(t, w) {
  t.mock.method(w.api, 'ttsEditor', async (id) => audio(id))
  t.mock.method(w.api, 'visualEditor', async (id) => ({ items: [{ id: `${id}-1` }, { id: `${id}-2` }], task: { status: 'idle' } }))
}

const videoProjectId = 'abcdef0123456789abcdef0123456789'

test('native dialogue launch creates an independent video project without submitting TTS',async t=>{
  const calls=[]
  const {w,s,mounted,browser}=await studio(t,{fetch:async(url,options={})=>{
    calls.push({url,options})
    if(url==='/api/video-studio/from-story')return Response.json({id:'a'.repeat(32),status:'draft'})
    return Response.json(url==='/api/session'?{user:null}:{})
  }})
  w.session.value={user:{id:1}};await mounted.flush();s.newProject('dynamic');await mounted.flush()
  Object.assign(w.form,{speech_mode:'dialogue',project_name:'雨伞',script:'小兔、小熊和小猫在门前商量如何共用一把伞。',visual_style_prompt:'水彩绘本',
    video_generation_backend:'comfyui',comfyui_profile_id:'h3',dynamic_auto_advance:true,
    _native_request_id:'a'.repeat(32),_video_defaults_loading:false})
  const tts=t.mock.method(w,'submit',()=>{throw new Error('Must not submit TTS')})
  await s.launch()
  assert.equal(s.studioError.value,'');assert.equal(s.studioPage.value,'videos')
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'),'a'.repeat(32))
  assert.equal(tts.mock.callCount(),0)
  const created=calls.filter(row=>row.url==='/api/video-studio/from-story')
  assert.equal(created.length,1)
  assert.equal(JSON.parse(created[0].options.body).speech_mode,'dialogue')
})

test('session polling preserves open voice settings and pending adoption; account switch clears both', async t => {
  const {w, s, mounted} = await studio(t)
  w.session.value = {user: {id: 1}}
  await mounted.flush()
  s.studioDrawer.value = '声音设置'
  s.voiceAdoptionPending.value = true
  s.studioDrawer.value = ''
  assert.equal(s.studioDrawer.value, '声音设置')
  w.session.value = {user: {id: 1}}
  await mounted.flush()
  assert.equal(s.studioDrawer.value, '声音设置')
  assert.equal(s.voiceAdoptionPending.value, true)
  w.session.value = {user: {id: 2}}
  await mounted.flush()
  assert.equal(s.studioDrawer.value, '')
  assert.equal(s.voiceAdoptionPending.value, false)
})

const independentVideo = (id, name) => ({id, revision: 1, status: 'image_review',
  settings: {name, ratio: '16:9'}, scenes: [], shots: [{id: name + '-shot'}],
  creation_parameters: {dynamic_auto_advance: true}})

for (const page of ['stories', 'images', 'comfyui', 'plugins']) {
test(`initial ${page} navigation survives the first authenticated session after a stale startup poll`, async t => {
  const firstSession = deferred()
  let sessionReads = 0
  const {w, s, mounted, browser} = await studio(t, {fetch: async url => {
    if (url === '/api/session') {
      sessionReads += 1
      return sessionReads === 1 ? firstSession.promise : Response.json({user: {id: 1}, auth_mode: 'account'})
    }
    return Response.json(url === '/api/health' ? {ok: true} : url === '/api/video-studio' ? {items: []} : {})
  }})
  assert.equal(w.session.value.user, null)
  assert.equal(sessionReads, 1)
  s.navigateStudio(page)
  await mounted.flush()
  assert.equal(s.studioPage.value, page)
  firstSession.resolve(Response.json({user: {id: 1}, auth_mode: 'account'}))
  await mounted.flush()
  assert.equal(w.session.value.user, null, 'navigation invalidated the old startup request')
  browser.timers.advance(2500)
  await mounted.flush()
  assert.equal(w.session.value.user.id, 1)
  assert.equal(s.studioPage.value, page, 'first account resolution must preserve the clicked tool page')
})

test(`${page} navigation still resets on later account switches and re-login`, async t => {
  const {w, s, mounted} = await studio(t)
  w.session.value = {user: {id: 1}}
  await mounted.flush()
  s.navigateStudio(page)
  w.session.value = {user: {id: 2}}
  assert.equal(s.studioPage.value, 'home')
  s.navigateStudio(page)
  w.session.value = {user: null}
  assert.equal(s.studioPage.value, 'home')
  s.navigateStudio(page)
  w.session.value = {user: {id: 3}}
  assert.equal(s.studioPage.value, 'home', 're-login must not reuse navigation from another account')
})
}

test('home discovers and filters independent dynamic projects with no main jobs', async t => {
  const h = independentVideo('a'.repeat(32), 'H concert'), g = independentVideo('b'.repeat(32), 'G references')
  const calls = []
  const {w, s, mounted, browser} = await studio(t, {fetch: async (url, options = {}) => {
    calls.push({url, method: options.method || 'GET'})
    if (url === '/api/video-studio') return Response.json({items: [h, g]})
    if (url === '/api/video-studio/' + h.id) return Response.json(h)
    return Response.json(url === '/api/session' ? {user: null} : {})
  }})
  w.session.value = {user: {id: 1}}
  await mounted.flush()
  assert.deepEqual(s.studioJobs.value, [])
  assert.deepEqual(s.studioVideoProjects.value.map(row => row.id), [h.id, g.id])
  s.studioFilter.value = 'dynamic'; s.studioSearch.value = 'references'
  assert.deepEqual(s.studioVideoProjects.value.map(row => row.id), [g.id])
  s.studioFilter.value = 'audio'
  assert.deepEqual(s.studioVideoProjects.value, [])
  browser.sessionStorage.setItem('ocv-video-autoplan', h.id)
  browser.sessionStorage.setItem('ocv-video-view', 'audio')
  await s.openVideoProject(h.id)
  assert.equal(s.studioPage.value, 'videos')
  assert.equal(s.deepLinkReadOnlyOpen.value, true)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), h.id)
  assert.equal(browser.sessionStorage.getItem('ocv-video-autoplan'), null)
  assert.equal(browser.sessionStorage.getItem('ocv-video-view'), null)
  assert.equal(calls.every(row => row.method === 'GET'), true)
  assert.equal(calls.some(row => row.url.startsWith('/api/jobs/')), false)
  s.goHome(); await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.equal(s.videoProjects.value.length, 2)
  assert.equal(s.deepLinkReadOnlyOpen.value, false)
})

test('late dynamic project lists cannot overwrite newer refreshes or another owner', async t => {
  const reads = [], h = independentVideo('a'.repeat(32), 'private H'), g = independentVideo('b'.repeat(32), 'new G')
  const {w, s, mounted} = await studio(t, {fetch: async url => {
    if (url === '/api/video-studio') {const pending = deferred(); reads.push(pending); return pending.promise}
    return Response.json(url === '/api/session' ? {user: null} : {})
  }})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  const refresh = s.refreshVideoProjects()
  reads[1].resolve(Response.json({items: [g]})); await refresh
  reads[0].resolve(Response.json({items: [h]})); await mounted.flush()
  assert.deepEqual(s.videoProjects.value.map(row => row.id), [g.id])
  const old = s.refreshVideoProjects()
  w.session.value = {user: {id: 2}}; await mounted.flush()
  assert.deepEqual(s.videoProjects.value, [])
  reads[2].resolve(Response.json({items: [h]})); await old
  assert.deepEqual(s.videoProjects.value, [])
  reads[3].resolve(Response.json({items: [g]})); await mounted.flush()
  assert.deepEqual(s.videoProjects.value.map(row => row.id), [g.id])
  assert.equal(s.videoProjectsLoading.value, false)
})

test('a late independent H open cannot replace G or an intentional departure', async t => {
  const h = independentVideo('a'.repeat(32), 'H'), g = independentVideo('b'.repeat(32), 'G'), reads = []
  const {w, s, mounted, browser} = await studio(t, {fetch: async url => {
    if (url === '/api/video-studio/' + h.id) {const pending = deferred(); reads.push(pending); return pending.promise}
    if (url === '/api/video-studio/' + g.id) return Response.json(g)
    return Response.json(url === '/api/session' ? {user: null} : {items: []})
  }})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  const old = s.openVideoProject(h.id)
  await s.openVideoProject(g.id)
  reads[0].resolve(Response.json(h)); await old
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), g.id)
  assert.equal(s.studioPage.value, 'videos')
  const departed = s.openVideoProject(h.id)
  s.newProject('dynamic'); w.form.script = 'Keep this new local draft'
  reads[1].resolve(Response.json({detail: 'old failure'}, {status: 404})); await departed
  assert.equal(s.studioPage.value, 'new')
  assert.equal(w.form.script, 'Keep this new local draft')
  assert.equal(s.studioError.value, '')
  assert.equal(s.studioBusy.value, false)
})

test('independent project ownership changes and failed lookups cannot create an open intent', async t => {
  const h = independentVideo('a'.repeat(32), 'H'), pending = deferred()
  const {w, s, mounted, browser} = await studio(t, {fetch: async url => {
    if (url === '/api/video-studio/' + h.id) return pending.promise
    if (url.startsWith('/api/video-studio/')) return Response.json({detail: 'missing'}, {status: 404})
    return Response.json(url === '/api/session' ? {user: null} : {items: []})
  }})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  const old = s.openVideoProject(h.id)
  w.session.value = {user: {id: 2}}; await mounted.flush()
  pending.resolve(Response.json(h)); await old
  assert.equal(s.studioPage.value, 'home')
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), null)
  await s.openVideoProject('b'.repeat(32))
  assert.match(s.studioError.value, /不存在.*访问权限/)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), null)
  assert.equal(s.deepLinkReadOnlyOpen.value, false)
})

test('main-job dynamic lookup and audio editing retain their original namespaces', async t => {
  const h = independentVideo('a'.repeat(32), 'H'), calls = []
  h.source_project = {id: 'audio-main-job'}
  const {w, s, mounted, browser} = await studio(t, {fetch: async url => {
    calls.push(url)
    if (url === '/api/video-studio/by-audio-task/audio-main-job') return Response.json({project: h})
    return Response.json(url === '/api/session' ? {user: null} : {items: []})
  }})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  mockEditors(t, w)
  const lookup = t.mock.method(w.api, 'job', async id => job(id, {dynamic_video: true}))
  assert.equal(await s.openProject({id: 'audio-main-job'}), true)
  assert.equal(s.studioPage.value, 'videos')
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), h.id)
  assert.equal(calls.filter(url => url.includes('/by-audio-task/')).length, 1)
  await s.openDynamicAudio(h)
  assert.equal(s.studioPage.value, 'editor')
  assert.equal(s.studioTab.value, '配音')
  assert.equal(s.editingDynamicAudio.value, true)
  assert.equal(lookup.mock.calls.at(-1).arguments[0], 'audio-main-job')
  assert.equal(calls.filter(url => url.includes('/by-audio-task/')).length, 1)
})

test('editing automatic dynamic audio stays in its editor until the user explicitly returns to the storyboard', async t => {
  const h = independentVideo('a'.repeat(32), 'Automatic dynamic project'), calls = []
  h.source_project = {id: 'automatic-audio-main-job'}
  const {w, s, mounted, browser} = await studio(t, {fetch: async (url, options = {}) => {
    calls.push({url, method: options.method || 'GET'})
    if (url === '/api/video-studio/by-audio-task/' + h.source_project.id) return Response.json({project: h})
    return Response.json(url === '/api/session' ? {user: null} : {items: []})
  }})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  mockEditors(t, w)
  t.mock.method(w.api, 'job', async id => ({...job(id, {
    dynamic_video: true, dynamic_auto_advance: true, _step_mode_stage: 'audio_review',
  }), status: 'waiting_confirmation'}))
  const transitionCalls = () => calls.filter(({url}) => url.includes('/by-audio-task/') || url.endsWith('/from-project'))
  function assertAudioEditor() {
    assert.equal(s.studioPage.value, 'editor')
    assert.equal(s.studioTab.value, '配音')
    assert.equal(s.editingDynamicAudio.value, true)
    assert.equal(w.activeJob.value.id, h.source_project.id)
    assert.equal(browser.sessionStorage.getItem('ocv-video-audio-edit'), h.id)
    assert.equal(browser.sessionStorage.getItem('ocv-video-open'), null)
    assert.deepEqual(transitionCalls(), [], 'viewing audio must not look up or create a storyboard')
  }

  await s.openDynamicAudio(h)
  await mounted.flush()
  browser.timers.advance(0)
  await mounted.flush()
  assertAudioEditor()

  w.activeJob.value.request._step_mode_stage = 'audio_generating'
  await mounted.flush()
  w.activeJob.value.request._step_mode_stage = 'audio_review'
  await mounted.flush()
  browser.timers.advance(0)
  await mounted.flush()
  assertAudioEditor()

  await s.enterDynamicStoryboard()
  await mounted.flush()
  assert.equal(s.studioPage.value, 'videos')
  assert.equal(s.editingDynamicAudio.value, false)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), h.id)
  assert.equal(browser.sessionStorage.getItem('ocv-video-audio-edit'), null)
  assert.equal(browser.sessionStorage.getItem('ocv-video-autoplan'), null)
  assert.deepEqual(transitionCalls(), [{url: '/api/video-studio/by-audio-task/' + h.source_project.id, method: 'GET'}])
  assert.equal(calls.every(({method}) => method === 'GET'), true, 'unchanged audio needs no import or synchronization write')
})

test('a video project replaces its exact source job in the home list while other jobs remain accessible', async t => {
  const h = independentVideo('a'.repeat(32), 'H')
  h.source_project = {id: 'audio-main-job', name: 'source audio'}
  const {w, s, mounted} = await studio(t, {fetch: async url =>
    Response.json(url === '/api/session' ? {user: null} : {items: [h]})})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  const source = job('audio-main-job', {dynamic_video: true})
  const separate = job('separate-main-job')
  w.jobs.value = [source, separate]
  assert.deepEqual(s.studioJobs.value.map(row => row.id), [separate.id])
  assert.deepEqual(s.studioVideoProjects.value.map(row => row.id), [h.id])
  assert.deepEqual(w.jobs.value.map(row => row.id), [source.id, separate.id], 'deduplication does not delete source jobs')
  mockEditors(t, w)
  const lookup = t.mock.method(w.api, 'job', async id => separate)
  assert.equal(await s.openProject(separate), true)
  assert.equal(lookup.mock.calls[0].arguments[0], separate.id)
  assert.equal(s.studioPage.value, 'editor')
  assert.equal(w.activeJob.value.id, separate.id)
})

test('referenced ordinary video, audio and subtitle originals remain visible in their own categories', async t => {
  const originals = [job('original-video'), job('original-audio', {module1_only: true}),
    job('original-subtitle', {subtitle_only: true})]
  const derived = originals.map((source, index) => ({...independentVideo('abc'[index].repeat(32), 'Derived ' + source.id),
    source_project: {id: source.id}}))
  const {w, s, mounted} = await studio(t, {fetch: async url =>
    Response.json(url === '/api/session' ? {user: null} : {items: derived})})
  w.session.value = {user: {id: 1}}; await mounted.flush()
  w.jobs.value = originals
  assert.deepEqual(s.studioJobs.value.map(row => row.id), originals.map(row => row.id))
  assert.deepEqual(s.studioVideoProjects.value.map(row => row.id), derived.map(row => row.id))
  for (const [filter, original] of [['video', originals[0]], ['audio', originals[1]], ['subtitle', originals[2]]]) {
    s.studioFilter.value = filter
    assert.deepEqual(s.studioJobs.value.map(row => row.id), [original.id], 'keep the original ' + filter + ' entry')
    assert.deepEqual(s.studioVideoProjects.value, [])
  }
  s.studioFilter.value = 'dynamic'
  assert.deepEqual(s.studioJobs.value, [])
  assert.deepEqual(s.studioVideoProjects.value.map(row => row.id), derived.map(row => row.id))
  assert.deepEqual(w.jobs.value.map(row => row.id), originals.map(row => row.id))
})

test('new dynamic defaults have no suspense genre and restored or explicitly retained modes keep their choice', async t => {
  const {w,s}=await studio(t)
  s.newProject('dynamic')
  assert.equal(w.form.content_mode,'general')
  assert.equal(w.form.agent2_director_theme,'通用视频')
  assert.equal(w.form.visual_style_prompt,'')
  w.form.content_mode='pure_science';w.form.agent2_director_theme='实验记录'
  s.restoreDefaultsOnNewProject.value=false
  s.newProject('dynamic')
  assert.equal(w.form.content_mode,'pure_science')
  assert.equal(w.form.agent2_director_theme,'实验记录')
  s.restoreDefaultsOnNewProject.value=true
  s.newProject('dynamic',{id:'saved-mode',form:{dynamic_video:true,script:'old story',content_mode:'urban_suspense',agent2_director_theme:'用户的悬念故事'}})
  assert.equal(w.form.content_mode,'urban_suspense')
  assert.equal(w.form.agent2_director_theme,'用户的悬念故事')
})

test('adopted stories prepare isolated dynamic drafts and reopen an identical version without replacing edits', async t => {
  const { w, s, browser } = await studio(t)
  w.session.value = { user: { id: 1 }, auth_mode: 'local' }
  s.newProject('dynamic')
  w.form.script = '上一项目的正文，需要留在上一项目。'
  w.form.reference_image_ids = ['unrelated-image']; w.form.source_audio_id = 'unrelated-audio'
  w.form.bgm_tracks = [{ asset_id: 'unrelated-bgm' }]
  w.form.global_character_prompt = 'old character'; w.form.story_environment_prompt = 'old world'
  const payload = { owner: '1', title: '采用故事', story: '新故事正文，准确保留作者写下的文字和每一行。\n下一行继续。', source: { story_id: 'a'.repeat(32), story_revision: 1, story_sha256: 'c'.repeat(64) } }
  s.navigateStudio('stories'); s.adoptStudioStory(payload)
  assert.equal(s.studioPage.value, 'new')
  assert.equal(w.form.dynamic_video, true)
  assert.equal(w.form.content_mode, 'general')
  assert.equal(w.form.script, payload.story)
  assert.equal(w.form._story_adopted_text, payload.story)
  assert.deepEqual(w.form.story_source, payload.source)
  assert.deepEqual(w.form.reference_image_ids, [])
  assert.equal(w.form.source_audio_id, '')
  assert.deepEqual(w.form.bgm_tracks, [])
  assert.equal(w.form.global_character_prompt, '')
  assert.equal(w.form.story_environment_prompt, '')
  const draft = s.studioDrafts.value.find(item => item.form.story_source?.story_id === payload.source.story_id)
  w.form.script = '在制作页面做的编辑，回到故事页再采用同一个版本也应该保留。'
  w.form.reference_image_ids = ['chosen-image']
  s.navigateStudio('stories'); s.adoptStudioStory(payload)
  assert.equal(w.form.script, '在制作页面做的编辑，回到故事页再采用同一个版本也应该保留。')
  assert.deepEqual(w.form.reference_image_ids, ['chosen-image'])
  assert.equal(s.studioDrafts.value.filter(item => item.form.story_source?.story_id === payload.source.story_id).length, 1)
  assert.equal(s.studioDrafts.value.find(item => item.form.story_source?.story_id === payload.source.story_id).id, draft.id)
  s.navigateStudio('stories')
  s.adoptStudioStory({ ...payload, story: '这是新版本故事，应该创建另外的草稿并保留之前的编辑。', source: { ...payload.source, story_revision: 2, story_sha256: 'd'.repeat(64) } })
  assert.equal(s.studioDrafts.value.filter(item => item.form.story_source?.story_id === payload.source.story_id).length, 2)
  assert.deepEqual(w.form.reference_image_ids, [])
  assert.equal(s.studioDrafts.value.find(item => item.id === draft.id).form.script, '在制作页面做的编辑，回到故事页再采用同一个版本也应该保留。')
  assert.match(browser.localStorage.getItem('ocv.studio.drafts.v1:1'), /story_source/)
  s.newProject('dynamic')
  assert.equal(w.form.story_source, null)
  assert.equal(w.form._story_adopted_text, '')
})

test('an adoption payload from another owner or a departed story page cannot populate production', async t => {
  const { w, s } = await studio(t)
  w.session.value = { user: { id: 2 } }
  s.navigateStudio('stories')
  const payload = { owner: '1', title: 'another account', story: 'private story', source: { story_id: 'a'.repeat(32), story_revision: 1, story_sha256: 'c'.repeat(64) } }
  s.adoptStudioStory(payload)
  assert.equal(s.studioPage.value, 'stories')
  assert.equal(w.form.script, '')
  s.goHome(); s.adoptStudioStory({ ...payload, owner: '2' })
  assert.equal(s.studioPage.value, 'home')
  assert.equal(w.form.script, '')
})

test('adoption identity keeps changed requirements and legacy copies separate without replacing local edits', async t => {
  const { w, s } = await studio(t)
  w.session.value = { user: { id: 1 } }
  const source = { story_id: 'a'.repeat(32), story_revision: 2, story_sha256: 'c'.repeat(64) }
  const payload = { owner: '1', title: '同一个正文', story: '正文没有变化。', source }
  const adopt = (value) => {
    s.navigateStudio('stories'); s.adoptStudioStory(value)
    return s.studioDrafts.value.find(item => item.form.story_source?.story_id === source.story_id && (item.form.story_source.adoption_id || '') === (value.source.adoption_id || '')).id
  }
  const legacyId = adopt(payload)
  w.form.script = '旧版制作副本中的编辑。'
  const first = { ...payload, source: { ...source, adoption_id: 'd'.repeat(32) }, record: { brief: { idea: '保留红围巾。' } } }
  const firstId = adopt(first)
  assert.notEqual(firstId, legacyId)
  assert.equal(w.form._story_requirements, '保留红围巾。')
  assert.equal(w.form._story_title, payload.title)
  w.form.script = '第一份采用记录的制作编辑。'
  w.form.reference_image_ids = ['my-reference']
  assert.equal(adopt(first), firstId)
  assert.equal(w.form.script, '第一份采用记录的制作编辑。')
  assert.deepEqual(w.form.reference_image_ids, ['my-reference'])
  const secondId = adopt({ ...first, source: { ...source, adoption_id: 'e'.repeat(32) }, story_requirements: '保留蓝帽子。' })
  assert.notEqual(secondId, firstId)
  assert.equal(w.form.script, payload.story)
  assert.equal(w.form._story_requirements, '保留蓝帽子。')
  assert.equal(s.studioDrafts.value.length, 3)
  assert.equal(adopt(payload), legacyId, 'legacy sources only reuse another legacy draft')
  assert.equal(w.form.script, '旧版制作副本中的编辑。')
  assert.equal(w.form._story_requirements, '')
  s.restoreDefaultsOnNewProject.value = false
  s.newProject('dynamic')
  assert.equal(w.form.story_source, null)
  assert.equal(w.form._story_requirements, '')
  assert.equal(w.form._story_title, '')
})

test('dynamic configuration edit, duplicate and reset restore the adopted baseline from saved parameters', async t => {
  const { w, s } = await studio(t)
  w.session.value = { user: { id: 1 } }
  const source = { story_id: 'a'.repeat(32), story_revision: 2, story_sha256: 'c'.repeat(64), adoption_id: 'd'.repeat(32) }
  const form = { dynamic_video: true, script: '采用时的正文。', story_source: source, story_snapshot: { story: '采用时的正文。', title: '保存的故事', brief: { idea: '角色始终佩戴红围巾。' } } }
  for (const action of ['edit-config', 'duplicate-config', 'reset']) {
    const draft = { id: `draft-${action}`, form, engine: 'indextts25', rerun: action === 'edit-config', source_project_id: action === 'edit-config' ? videoProjectId : '', reset: action === 'reset' }
    s.newProject('dynamic', draft)
    assert.equal(w.form._story_adopted_text, form.script, action)
    assert.equal(w.form._story_requirements, '角色始终佩戴红围巾。', action)
    assert.equal(w.form._story_title, '保存的故事', action)
    assert.deepEqual(w.generationRequestPayload().story_source, source, action)
    assert.equal(Object.hasOwn(w.generationRequestPayload(), 'story_snapshot'), false)
  }
  w.form.script = '复制后手工修改的文稿。'
  s.goHome()
  const edited = s.studioDrafts.value.find(item => item.id === 'draft-reset')
  s.newProject('dynamic', edited)
  assert.equal(w.form.script, '复制后手工修改的文稿。')
  assert.equal(w.form._story_adopted_text, form.script)
  assert.equal(w.generationRequestPayload().story_source, null)
})

function deepLinkFetch(read, calls) {
  return async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET' })
    if (url === `/api/video-studio/${videoProjectId}`) return read()
    return Response.json(url === '/api/session' ? { user: null, auth_mode: 'account' } : {})
  }
}

test('a video deep link waits for its owner and opens through GET with automatic execution suppressed', async t => {
  const calls = []
  const context = await studio(t, {
    href: `http://localhost/?video_project=${videoProjectId}&tab=kept#details`,
    fetch: deepLinkFetch(() => Response.json({ id: videoProjectId, creation_parameters: { dynamic_auto_advance: true } }), calls),
  })
  const { w, s, browser, mounted } = context
  browser.window.history = {
    state: null,
    replaceState(state, title, value) { browser.window.location.href = new URL(value, browser.window.location.href).href },
  }
  assert.equal(calls.some(call => call.url.startsWith('/api/video-studio/')), false)
  browser.sessionStorage.setItem('ocv-video-autoplan', videoProjectId)
  w.session.value = { user: { id: 1 }, auth_mode: 'local' }
  await mounted.flush()
  assert.equal(s.studioPage.value, 'videos')
  assert.equal(s.deepLinkReadOnlyOpen.value, true)
  assert.equal(s.studioBusy.value, false)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), videoProjectId)
  assert.equal(browser.sessionStorage.getItem('ocv-video-autoplan'), null)
  assert.equal(calls.filter(call => call.url.startsWith('/api/video-studio/')).length, 1)
  assert.equal(calls.every(call => call.method === 'GET'), true)
  assert.equal(new URL(browser.window.location.href).searchParams.get('video_project'), videoProjectId)
  s.goHome()
  assert.equal(s.deepLinkReadOnlyOpen.value, false)
  assert.equal(browser.window.location.href, 'http://localhost/?tab=kept#details')
  w.session.value = { user: { id: 2 }, auth_mode: 'local' }
  await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.equal(calls.filter(call => call.url.startsWith('/api/video-studio/')).length, 1)
})

test('a missing or unauthorized video deep link stays home and never starts planning or generation', async t => {
  const calls = []
  const { w, s, browser, mounted } = await studio(t, {
    href: `http://localhost/?video_project=${videoProjectId}`,
    fetch: deepLinkFetch(() => Response.json({ detail: '视频草案不存在' }, { status: 404 }), calls),
  })
  w.session.value = { user: { id: 1 } }
  await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.match(s.studioError.value, /项目不存在.*访问权限/)
  assert.equal(s.deepLinkReadOnlyOpen.value, false)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), null)
  assert.equal(calls.every(call => call.method === 'GET'), true)
})

test('a delayed video deep link cannot pull the user out of a newly created draft', async t => {
  const calls = [], pending = deferred()
  const { w, s, browser, mounted } = await studio(t, {
    href: `http://localhost/?video_project=${videoProjectId}`,
    fetch: deepLinkFetch(() => pending.promise, calls),
  })
  w.session.value = { user: { id: 1 } }
  assert.equal(s.studioBusy.value, true)
  s.newProject('video')
  w.form.script = 'Keep this new idea'
  pending.resolve(Response.json({ id: videoProjectId }))
  await mounted.flush()
  assert.equal(s.studioPage.value, 'new')
  assert.equal(w.form.script, 'Keep this new idea')
  assert.equal(s.deepLinkReadOnlyOpen.value, false)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), null)
  assert.equal(s.studioBusy.value, false)
})

test('an account change rejects an in-flight video deep link without replaying it for the new account', async t => {
  const calls = [], pending = deferred()
  const { w, s, browser, mounted } = await studio(t, {
    href: `http://localhost/?video_project=${videoProjectId}`,
    fetch: deepLinkFetch(() => pending.promise, calls),
  })
  w.session.value = { user: { id: 1 } }
  w.session.value = { user: { id: 2 } }
  pending.resolve(Response.json({ id: videoProjectId }))
  await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.equal(s.deepLinkReadOnlyOpen.value, false)
  assert.equal(browser.sessionStorage.getItem('ocv-video-open'), null)
  assert.equal(calls.filter(call => call.url.startsWith('/api/video-studio/')).length, 1)
})

test('explicit navigation while waiting for login consumes the video deep link', async t => {
  const calls = []
  const { w, s, mounted } = await studio(t, {
    href: `http://localhost/?video_project=${videoProjectId}`,
    fetch: deepLinkFetch(() => Response.json({ id: videoProjectId }), calls),
  })
  s.goHome()
  w.session.value = { user: { id: 1 } }
  await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.equal(calls.some(call => call.url.startsWith('/api/video-studio/')), false)
})

test('an invalid video deep link shows a clear error without reading any project', async t => {
  const calls = []
  const { w, s, mounted } = await studio(t, {
    href: 'http://localhost/?video_project=../../outside',
    fetch: deepLinkFetch(() => { throw new Error('Invalid IDs must not be requested') }, calls),
  })
  assert.match(s.studioError.value, /32 位十六进制/)
  w.session.value = { user: { id: 1 } }
  await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.match(s.studioError.value, /32 位十六进制/)
  assert.equal(calls.some(call => call.url.startsWith('/api/video-studio/')), false)
})

test('draft A survives switching before autosave and nested inputs remain independent', async (t) => {
  const { w, s, mounted, browser } = await studio(t)
  s.newProject('video')
  Object.assign(w.form, {
    project_name: 'A', script: 'draft A', global_character_prompt: 'Mili has no ears',
    story_environment_prompt: 'quiet concert', source_audio_id: 'voice-A',
    bgm_enabled: true, bgm_tracks: [{ asset_id: 'music-A', nested: { volume: 0.3 } }],
    reference_image_ids: ['Mili'], reference_image_notes: { Mili: { appearance: 'earless' } },
  })
  s.newProject('video')
  const savedA = s.studioDrafts.value.find((draft) => draft.name === 'A')
  assert.equal(savedA.form.script, 'draft A')
  assert.equal(w.form.script, '')
  assert.equal(w.form.global_character_prompt, '')
  assert.equal(w.form.story_environment_prompt, '')
  assert.equal(w.form.source_audio_id, '')
  assert.equal(w.form.bgm_enabled, false)
  assert.deepEqual(w.form.bgm_tracks, [])
  assert.deepEqual(w.form.reference_image_ids, [])
  Object.assign(w.form, { project_name: 'B', script: 'draft B' })
  s.newProject('video', savedA)
  assert.equal(w.form.script, 'draft A')
  w.form.bgm_tracks[0].nested.volume = 0.8
  w.form.reference_image_notes.Mili.appearance = 'same earless design'
  assert.equal(savedA.form.bgm_tracks[0].nested.volume, 0.3)
  assert.equal(savedA.form.reference_image_notes.Mili.appearance, 'earless')
  browser.timers.advance(650)
  await mounted.flush()
  assert.equal(s.studioDrafts.value.find((draft) => draft.name === 'B').form.script, 'draft B')
  assert.equal(s.studioDrafts.value.find((draft) => draft.name === 'A').form.bgm_tracks[0].nested.volume, 0.8)
})

test('unmount saves the last keystroke and remount can restore the draft', async (t) => {
  const app = await studio(t)
  app.s.newProject('audio')
  Object.assign(app.w.form, { project_name: 'reload', script: 'last keystroke' })
  const { w, s } = await app.remount()
  assert.equal(s.studioDrafts.value.length, 1)
  s.newProject('audio', s.studioDrafts.value[0])
  assert.equal(w.form.script, 'last keystroke')
  assert.equal(s.studioKind.value, 'audio')
})

test('new drafts clear project content even when retaining creation preferences', async (t) => {
  const { w, s } = await studio(t)
  s.newProject()
  s.restoreDefaultsOnNewProject.value = false
  Object.assign(w.form, { script: 'A', global_character_prompt: 'A cast', story_environment_prompt: 'A scene', bgm_enabled: true, bgm_tracks: [{ asset_id: 'A' }], reference_image_ids: ['A'] })
  s.newProject()
  assert.equal(w.form.global_character_prompt, '')
  assert.equal(w.form.story_environment_prompt, '')
  assert.equal(w.form.bgm_enabled, false)
  assert.deepEqual(w.form.bgm_tracks, [])
  assert.deepEqual(w.form.reference_image_ids, [])
})

for (const late of ['success', 'error']) {
  test(`late project A ${late} cannot replace project B or its error state`, async (t) => {
    const { w, s, mounted } = await studio(t)
    const first = deferred()
    t.mock.method(w.api, 'job', (id) => id === 'A' ? first.promise : Promise.resolve(job(id)))
    mockEditors(t, w)
    const openA = s.openProject({ id: 'A' })
    assert.equal(await s.openProject({ id: 'B' }), true)
    if (late === 'success') first.resolve(job('A'))
    else first.reject(new Error('old A failure'))
    assert.equal(await openA, false)
    await mounted.flush()
    assert.equal(s.studioJob.value.id, 'B')
    assert.equal(w.form.script, 'story B')
    assert.equal(s.studioPage.value, 'editor')
    assert.equal(s.studioError.value, '')
    assert.equal(s.studioBusy.value, false)
  })
}

for (const destination of ['home', 'new']) {
  test(`a pending open cannot return from ${destination} to the editor`, async (t) => {
    const { w, s } = await studio(t)
    const pending = deferred()
    t.mock.method(w.api, 'job', () => pending.promise)
    const opening = s.openProject({ id: 'A' })
    if (destination === 'home') s.goHome()
    else { s.newProject(); w.form.script = 'new idea' }
    pending.resolve(job('A'))
    assert.equal(await opening, false)
    assert.equal(s.studioPage.value, destination)
    assert.equal(w.activeJob.value, null)
    if (destination === 'new') assert.equal(w.form.script, 'new idea')
  })
}

test('a failed project lookup preserves the draft and can be retried', async (t) => {
  const { w, s } = await studio(t)
  s.newProject()
  w.form.script = 'unsent idea'
  let failed = true
  t.mock.method(w.api, 'job', async (id) => { if (failed) throw new Error('offline'); return job(id) })
  mockEditors(t, w)
  assert.equal(await s.openProject({ id: 'A' }), false)
  assert.equal(s.studioPage.value, 'new')
  assert.equal(w.form.script, 'unsent idea')
  assert.equal(s.studioBusy.value, false)
  failed = false
  assert.equal(await s.openProject({ id: 'A' }), true)
  assert.equal(s.studioJob.value.id, 'A')
  assert.equal(s.studioDrafts.value[0].form.script, 'unsent idea')
})

test('typing while another project opens preserves the final draft text', async (t) => {
  const { w, s } = await studio(t)
  s.newProject()
  w.form.script = 'first version'
  const pending = deferred()
  t.mock.method(w.api, 'job', () => pending.promise)
  mockEditors(t, w)
  const opening = s.openProject({ id: 'A' })
  w.form.script = 'last edit before the response'
  pending.resolve(job('A'))
  assert.equal(await opening, true)
  assert.equal(w.form.script, 'story A')
  assert.equal(s.studioDrafts.value[0].form.script, 'last edit before the response')
})

test('returning to a project restores its selected image, sentence and editor tab', async (t) => {
  const { w, s, mounted } = await studio(t)
  t.mock.method(w.api, 'job', async (id) => job(id))
  mockEditors(t, w)
  await s.openProject({ id: 'A' })
  await mounted.flush()
  s.studioTab.value = '画面与字幕'
  w.visualTimingSelectedId.value = 'A-2'
  s.studioSentence.value = 1
  await s.openProject({ id: 'B' })
  await s.openProject({ id: 'A' })
  await mounted.flush()
  assert.equal(s.studioTab.value, '画面与字幕')
  assert.equal(w.visualTimingSelectedId.value, 'A-2')
  assert.equal(s.studioSentence.value, 1)
})

test('switching accounts drops pending navigation and cannot save old content under the new owner', async (t) => {
  const { w, s, browser, mounted } = await studio(t)
  w.session.value = { user: { id: 1 } }
  s.newProject()
  w.form.script = 'owner one'
  browser.timers.advance(650)
  await mounted.flush()
  const pending = deferred()
  t.mock.method(w.api, 'job', () => pending.promise)
  const opening = s.openProject({ id: 'A' })
  const oldDrafts = browser.localStorage.getItem('ocv.studio.drafts.v1:1')
  w.form.script = 'owner one final edit'
  w.session.value = { user: { id: 2 } }
  pending.resolve(job('A'))
  assert.equal(await opening, false)
  browser.timers.advance(650)
  await mounted.flush()
  assert.equal(s.studioPage.value, 'home')
  assert.equal(w.form.script, '')
  assert.equal(s.studioJob.value, null)
  const preserved = JSON.parse(browser.localStorage.getItem('ocv.studio.drafts.v1:1'))
  assert.equal(preserved[0].id, JSON.parse(oldDrafts)[0].id)
  assert.equal(preserved[0].form.script, 'owner one final edit')
  assert.equal(browser.localStorage.getItem('ocv.studio.drafts.v1:2'), null)
  s.newProject()
  w.form.script = 'owner two'
  s.goHome()
  w.session.value = { user: { id: 1 } }
  assert.equal(s.studioDrafts.value[0].form.script, 'owner one final edit')
})

for (const kind of ['video', 'audio', 'subtitle']) {
  test(`a late ${kind} creation leaves the new draft intact and releases its own busy state`, async (t) => {
    const { w, s } = await studio(t)
    w.session.value = { user: { id: 1 } }
    w.health.value = { ok: true, tts25_online: true }
    s.newProject(kind)
    w.form.script = 'submitted story'
    w.subtitleForm.source_audio_id = 'voice-A'
    w.subtitleForm.reference_text = 'submitted subtitles'
    const pending = deferred()
    const create = t.mock.method(w.api, 'createJob', () => pending.promise)
    const launch = s.launch()
    assert.equal(create.mock.callCount(), 1)
    s.newProject('video')
    w.form.script = 'new idea'
    pending.resolve(job('created-A', { module1_only: kind === 'audio', subtitle_only: kind === 'subtitle' }))
    await launch
    assert.equal(s.studioPage.value, 'new')
    assert.equal(s.studioJob.value, null)
    assert.equal(w.form.script, 'new idea')
    assert.equal(s.studioBusy.value, false)
    assert.equal(w.submitting.value, false)
    assert.equal(w.submittingModule1.value, false)
    assert.equal(w.submittingSubtitle.value, false)
  })
}

for (const page of ['images', 'comfyui', 'plugins']) {
  test(`visiting ${page} immediately saves the draft and invalidates a pending open`, async (t) => {
    const { w, s } = await studio(t)
    s.newProject()
    w.form.script = 'save before leaving'
    const pending = deferred()
    t.mock.method(w.api, 'job', () => pending.promise)
    const opening = s.openProject({ id: 'A' })
    w.form.script = 'last edit before leaving'
    s.navigateStudio(page)
    pending.resolve(job('A'))
    assert.equal(await opening, false)
    assert.equal(s.studioPage.value, page)
    assert.equal(s.studioDrafts.value[0].form.script, 'last edit before leaving')
  })
}

test('current task logs keep refreshing and returning reopens the editor', async (t) => {
  const { w, s } = await studio(t)
  w.session.value = { user: { id: 1 } }
  const current = { ...job('A'), status: 'running', progress: 10, logs: ['started'] }
  t.mock.method(w.api, 'job', async () => ({ ...current }))
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 } }))
  t.mock.method(w.api, 'jobs', async () => ({ jobs: [current], page: 1 }))
  mockEditors(t, w)
  await s.openProject({ id: 'A' })
  await s.openLogs()
  assert.equal(w.guidedCreatingNew.value, false)
  current.progress = 75
  current.logs = ['started', 'rendering']
  await w.refresh()
  assert.equal(s.studioJob.value.progress, 75)
  assert.deepEqual(s.studioTaskLogs.value, ['started', 'rendering'])
  assert.equal(await s.returnToStudioProject(), true)
  assert.equal(s.studioPage.value, 'editor')
  assert.equal(w.guidedCreatingNew.value, false)
})

for (const kind of ['video', 'audio', 'subtitle']) {
  test(`edits during ${kind} submission stay as a draft while the submitted task opens`, async (t) => {
    const { w, s, mounted } = await studio(t)
    w.session.value = { user: { id: 1 } }
    w.health.value = { tts25_online: true }
    t.mock.method(w.api, 'session', async () => ({ user: { id: 1 } }))
    t.mock.method(w.api, 'health', async () => ({ tts25_online: true }))
    const current = job('submitted', { script: 'submitted text', module1_only: kind === 'audio', subtitle_only: kind === 'subtitle' })
    t.mock.method(w.api, 'job', async () => current)
    t.mock.method(w.api, 'jobs', async () => ({ jobs: [current] }))
    mockEditors(t, w)
    s.newProject(kind)
    w.form.script = 'submitted text'
    w.subtitleForm.source_audio_id = 'audio-A'
    w.subtitleForm.reference_text = 'submitted text'
    const response = deferred()
    const create = t.mock.method(w.api, 'createJob', () => response.promise)
    const launching = s.launch()
    assert.equal(create.mock.callCount(), 1)
    w.form.script = 'next revision'
    w.subtitleForm.reference_text = 'next revision'
    response.resolve(current)
    await launching
    await mounted.flush()
    assert.equal(s.studioPage.value, 'editor')
    assert.equal(s.studioJob.value.id, 'submitted')
    assert.equal(w.form.script, 'submitted text')
    assert.equal(s.studioDrafts.value[0].form.script, 'next revision')
    assert.equal(s.studioDrafts.value[0].subtitle.reference_text, 'next revision')
    assert.equal(create.mock.calls[0].arguments[0].script, 'submitted text')
  })
}

test('a successful unchanged submission removes only the submitted draft', async (t) => {
  const { w, s } = await studio(t)
  w.session.value = { user: { id: 1 } }
  w.health.value = { tts25_online: true }
  t.mock.method(w.api, 'session', async () => ({ user: { id: 1 } }))
  s.newProject('audio')
  w.form.script = 'keep another draft'
  s.newProject('audio')
  w.form.script = 'submit this draft'
  t.mock.method(w.api, 'createJob', async () => job('created', { module1_only: true }))
  await s.launch()
  assert.equal(s.studioPage.value, 'editor')
  assert.equal(s.studioDrafts.value.length, 1)
  assert.equal(s.studioDrafts.value[0].form.script, 'keep another draft')
})
