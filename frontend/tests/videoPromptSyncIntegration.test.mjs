import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const componentUrl = new URL('../src/components/VideoStudio.vue', import.meta.url)
const compiled = compileScript(parse(await readFile(componentUrl, 'utf8')).descriptor, { id: 'video-prompt-sync-integration' }).content
  .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (line, names, specifier) => {
    if (specifier.endsWith('.vue')) return `const ${names} = {}`
    const target = specifier === 'vue' ? import.meta.resolve('vue') : new URL(specifier.endsWith('.js') ? specifier : specifier + '.js', componentUrl).href
    return `import ${names} from '${target}'`
  })
const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))

async function editor(t, { suppressAutoAdvance = true } = {}) {
  const calls = [], emitted = []
  let current = { id: 'sync-input-project', revision: 1, status: 'image_review', settings: { name: 'Sync inputs', ratio: '16:9' },
    creation_parameters: { dynamic_auto_advance: false, video_generation_backend: 'comfyui', scene_references_enabled: false },
    scenes: [], logs: [], shots: [{ id: 'shot-a', kind: 'video', action: '原动作', video_prompt: '原正文', image_status: 'completed', slide_ids: [] }] }
  const browser = installBrowserGlobals({ fetch: async (url, options = {}) => {
    if (options.method === 'POST') {
      const body = options.body ? JSON.parse(options.body) : undefined
      calls.push({ url, body })
      current = { ...current, revision: current.revision + 1, shots: current.shots.map(shot => ({ ...shot, ...body, id: shot.id })) }
      return Response.json(current)
    }
    if (url.endsWith('/project-cast')) return Response.json({ project_id: current.id, revision: current.revision, characters: [], assets: [] })
    return Response.json(url === '/api/video-studio' ? { items: [] } : {})
  } })
  const mounted = mountComposable(() => component.setup({ accountKey: 'owner', suppressAutoAdvance }, { expose() {}, emit(...args) { emitted.push(args) } }))
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  const panel = mounted.instance
  panel.project.value = current
  await mounted.flush()
  return { ...mounted, panel, calls, emitted, timers: browser.timers }
}

test('mixing existing narration changes playback URL without changing raw video identity',async t=>{
  const {panel}=await editor(t)
  const shot={id:'shot-a',video_version:'same-original-video'}
  const original=panel.videoUrl(shot)
  const mixed=panel.videoUrl({...shot,narration_preview:'assets/narrated-a.mp4'})
  assert.notEqual(mixed,original)
  assert.notEqual(panel.videoUrl({...shot,narration_preview:'assets/narrated-b.mp4'}),mixed)
  panel.project.value.revision++
  assert.equal(panel.videoUrl(shot),original)
})

test('export notice distinguishes observation, checked and unknown historical results',async t=>{
  const {panel}=await editor(t)
  panel.project.value.export={raw:'original.mp4'}
  assert.match(panel.exportReviewNotice.value,/没有质量检查记录/)
  panel.project.value.export={raw:'original.mp4',review:{mode:'observation'}}
  assert.match(panel.exportReviewNotice.value,/观察版.*尚未人工验收/)
  panel.project.value.export={raw:'original.mp4',review:{mode:'automatic',visual_shots_checked:3,speech_shots_checked:2}}
  assert.match(panel.exportReviewNotice.value,/抽样检查 3 镜，原声文字检查 2 镜/)
  assert.match(panel.exportReviewNotice.value,/不代表成片合格/)
})

test('local edit exit ignores unrelated failed frames but checks its own scene and prompt', async t => {
  const { panel, calls } = await editor(t)
  const shot = panel.project.value.shots[0]
  panel.project.value.reedit_shot_id = shot.id
  panel.project.value.shots.push({ id:'unrelated',kind:'video',image_status:'failed',image_task:{action:'auto_repair',status:'failed'} })
  assert.equal(panel.localReeditBlock.value,'')
  assert.equal(panel.imageConfirmationBlock.value,'')
  panel.project.value.shots[0].video_prompt=''
  assert.match(panel.localReeditBlock.value,/提示词/)
  await panel.finishShotReedit(panel.project.value.shots[0])
  assert.equal(calls.length,0)
  panel.project.value.shots[0].video_prompt='已采用正文'
  panel.project.value.creation_parameters={...panel.project.value.creation_parameters,
    scene_references_enabled:true,dynamic_text_mode:'visual_first'}
  panel.project.value.shots[0].scene_reference_id='scene-a'
  panel.project.value.shots[0].scene_reference_used_version='v1'
  panel.project.value.scene_assets=[{id:'scene-a',image_status:'completed',image_version:'v2'}]
  assert.match(panel.localReeditBlock.value,/场景/)
  panel.project.value.scene_assets[0].image_version='v1'
  assert.equal(panel.localReeditBlock.value,'')
  await panel.finishShotReedit(panel.project.value.shots[0])
  assert.equal(calls.length,1)
  assert.match(calls[0].url,/\/images\/confirm$/)
  assert.equal(calls[0].body.regenerate_shot_id,undefined)
})

test('pending prompt sync blocks new generation while preserving navigation and frozen-task lookup', async t => {
  const { panel, calls } = await editor(t)
  panel.videoPromptSyncPending.value = true
  const shot = panel.project.value.shots[0]
  await panel.generateVideos([shot])
  await panel.regenerateVideo(shot)
  await panel.regenerateAllVideos()
  assert.equal(calls.length, 0)
  assert.match(panel.error.value, /正文尚未/)
  assert.equal(panel.requireAdoptedVideoInputs(), true, 'saved pending input must not trap navigation or configuration')
  panel.openStage({ key: 'motions' })
  assert.equal(panel.view.value, 'storyboard')
  assert.equal(panel.productionPanel.value, 'video')
  assert.equal(panel.requireSyncedVideoPrompts([{ ...shot, video_task_id: 'frozen-task', video_resume_available: true }], { resume: true }), true)
  assert.equal(panel.requireSyncedVideoPrompts([{ ...shot, video_task_id: 'frozen-task', video_resume_available: true }]), false)
})

test('pending sync allows saving a text draft, so the outer dirty guard can release afterwards', async t => {
  const context = await editor(t)
  const { panel, calls } = context
  panel.videoPromptSyncPending.value = true
  panel.project.value.shots[0].action = '人工改后的动作'
  panel.project.value.shots[0].video_prompt = '人工改后的正文'
  panel.rememberMotionDraft(panel.project.value.shots[0])
  assert.equal(panel.dirty.value, true)
  await panel.run(panel.save)
  await context.flush()
  assert.equal(calls.length, 1)
  assert.match(calls[0].url, /\/motion$/)
  assert.equal(calls[0].body.video_prompt, '人工改后的正文')
  assert.equal(panel.dirty.value, false)
  assert.deepEqual(panel.motionDrafts.value, {})
  assert.equal(panel.videoPromptSyncPending.value, true, 'saving prose is distinct from confirming its picture basis')
})

test('in-flight input synchronization blocks outer saves and generation before any request', async t => {
  const { panel, calls } = await editor(t)
  panel.videoInputBusy.value = true
  await panel.run(panel.save)
  await panel.generateVideos(panel.project.value.shots)
  assert.equal(calls.length, 0)
  assert.equal(panel.requireAdoptedVideoInputs(), false)
})

test('returning to projects allows background generation but protects unsaved inputs and text', async t => {
  const {panel, calls, emitted} = await editor(t)
  panel.videoInputDirty.value = true
  panel.backToProjects()
  assert.deepEqual(emitted, [])
  panel.videoInputDirty.value = false
  panel.dirty.value = true
  panel.backToProjects()
  assert.deepEqual(emitted, [])
  assert.match(panel.error.value, /未保存/)
  panel.dirty.value = false
  panel.project.value.status = 'video_generating'
  panel.backToProjects()
  assert.deepEqual(emitted, [['back']])
  assert.deepEqual(calls, [])
})

test('browsing an existing automatic project never advances it on component watchers', async t => {
  const {panel, calls, timers, flush} = await editor(t, {suppressAutoAdvance: true})
  panel.project.value.creation_parameters.dynamic_auto_advance = true
  panel.project.value.status = 'image_review'
  await flush()
  timers.advance(0)
  await flush()
  assert.deepEqual(calls, [])
  assert.equal(panel.project.value.status, 'image_review')
})

test('legacy browser auto advance cannot bypass whole-project readiness after local exit', async t => {
  const { panel, calls } = await editor(t,{suppressAutoAdvance:false})
  panel.project.value.creation_parameters.dynamic_auto_advance=true
  panel.project.value.shots.push({id:'failed',kind:'static',image_status:'failed'})
  for(const status of ['video_generation_ready','video_review']){
    panel.project.value.status=status
    await panel.advanceAutoPilot()
    assert.match(panel.error.value,/整片仍有核心图/)
  }
  assert.deepEqual(calls,[])
})

test('auto advance resumes after the input locks clear without requiring another project revision', async t => {
  const context = await editor(t, { suppressAutoAdvance: false })
  const { panel, timers } = context
  panel.videoInputBusy.value = true
  panel.videoInputDirty.value = true
  panel.videoPromptSyncPending.value = true
  panel.project.value.creation_parameters.dynamic_auto_advance = true
  panel.project.value.status = 'video_generation_ready'
  await context.flush()
  timers.advance(0)
  assert.equal(panel.error.value, '')
  panel.videoInputBusy.value = false
  panel.videoPromptSyncPending.value = false
  await context.flush()
  timers.advance(0)
  assert.equal(panel.error.value, '', 'an unadopted input still pauses auto advance')
  const revision = panel.project.value.revision
  panel.videoInputDirty.value = false
  await context.flush()
  timers.advance(0)
  await context.flush()
  assert.match(panel.error.value, /请检查各镜头的工作流或视频 API 配置/)
  assert.equal(panel.project.value.revision, revision)
  assert.deepEqual(context.calls, [], 'resume only checks readiness in this unconfigured fixture')
})
