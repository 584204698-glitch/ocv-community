import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const componentUrl = new URL('../src/components/VideoStudio.vue', import.meta.url)
const source = await readFile(componentUrl, 'utf8')
const compiled = compileScript(parse(source).descriptor, { id: 'video-image-options' }).content
  .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (line, names, specifier) => {
    if (specifier.endsWith('.vue')) return `const ${names} = {}`
    const target = specifier === 'vue' ? import.meta.resolve('vue') : new URL(specifier.endsWith('.js') ? specifier : specifier + '.js', componentUrl).href
    return `import ${names} from '${target}'`
  })
const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))
const record = protocol => ({ id: 'image-options', revision: 1, status: 'image_review', settings: { name: 'Image options', ratio: '16:9' },
  creation_parameters: { dynamic_auto_advance: false, scene_references_enabled: false, ...(protocol ? { image_profile_snapshot: { protocol, resolution: '1k' } } : {}) },
  scenes: [], logs: [], shots: [{ id: 'shot_1', kind: 'video', image_prompt: 'One character waiting.', image_status: 'completed', image_version: 1, slide_ids: [] }] })

test('multiple reference uploads finish when the first update starts child-panel reads', async t => {
  const { panel } = await editor(t, 'openai_images')
  const originalFetch = globalThis.fetch, uploads = []
  globalThis.fetch = async (url, options = {}) => {
    if (!url.endsWith('/redraw-references')) return originalFetch(url, options)
    uploads.push(options.body.get('file').name)
    panel.videoInputBusy.value = true
    return Response.json({ ...record('openai_images'), revision: uploads.length + 1,
      redraw_references: uploads.map((name, i) => ({ id: 'ref-' + i, name })) })
  }
  await panel.uploadRedrawReferences({target:{value:'selected',files:[
    new File(['one'], 'first.png', {type:'image/png'}),
    new File(['two'], 'second.png', {type:'image/png'})
  ]}})
  assert.deepEqual(uploads, ['first.png', 'second.png'])
  assert.deepEqual(panel.redrawReferenceIds.value, ['ref-0', 'ref-1'])
  assert.equal(panel.error.value, '')
})

test('redraw shares adopted identities and separates retired references without losing an explicit selection', async t => {
  const { panel, requests } = await editor(t,'openai_images')
  Object.assign(panel.project.value, {
    references:[{id:'old',description:'Old actor'}],
    retired_character_reference_ids:['old'],
    video_input_assets:[{id:'adopted',name:'New actor'},{id:'unrelated',name:'Other'}],
    project_cast:{characters:[{id:'actor',name:'Rabbit',reference_id:'adopted'}]}
  })
  assert.deepEqual(panel.visibleRedrawReferences.value.map(a=>a.id),['adopted'])
  panel.redrawReferenceIds.value=['old']
  assert.deepEqual(panel.visibleRedrawReferences.value.map(a=>a.id),['old','adopted'])
  assert.equal(panel.visibleRedrawReferences.value[0].retired,true)
  panel.redrawReferenceIds.value=[];panel.showRetiredReferences.value=true
  assert.equal(panel.visibleRedrawReferences.value.length,2)
  panel.project.value={...record('openai_images'),id:'another'}
  assert.equal(panel.showRetiredReferences.value,false)
  assert.deepEqual(panel.visibleRedrawReferences.value,[])
  assert.deepEqual(requests,[])
})
async function editor(t, protocol, confirm = () => true) {
  const requests = []
  let current = record(protocol)
  const browser = installBrowserGlobals({ confirm, fetch: async (url, options = {}) => {
    if (options.method === 'POST') { requests.push({ url, body: JSON.parse(options.body) }); return Response.json(current) }
    if (url.endsWith('/project-cast')) return Response.json({ project_id: current.id, revision: current.revision, characters: [], assets: [] })
    return Response.json(url === '/api/video-studio' ? { items: [] } : {})
  } })
  const mounted = mountComposable(() => component.setup({ accountKey: 'owner', suppressAutoAdvance: true }, { expose() {}, emit() {} }))
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  const panel = mounted.instance
  panel.project.value = current
  await mounted.flush()
  return { panel, requests, mounted, setProtocol(next) { current = { ...current, creation_parameters: record(next).creation_parameters }; panel.project.value = current } }
}

const coreReport = () => ({project_id:'image-options',revision:1,fingerprints:['pixels-and-design'],
  reviewed_ids:['shot_1'],unchecked_ids:['shot_2'],quality_guaranteed:false,
  reports:[{review:{issues:[{shot_id:'shot_1',correction:'Keep the approved chair'}],uncertainties:['Hand partly hidden']}}]})

test('core review is explicit, preserves uncertainty, and never submits a redraw', async t => {
  const {panel,requests}=await editor(t,'openai_images')
  assert.deepEqual(requests,[])
  const originalFetch=globalThis.fetch
  globalThis.fetch=async(url,options={})=>{
    if(!url.endsWith('/images/check'))return originalFetch(url,options)
    requests.push({url,body:JSON.parse(options.body)})
    return Response.json(coreReport())
  }
  await panel.checkCoreImages()
  assert.deepEqual(requests,[{url:'/api/video-studio/image-options/images/check',body:{revision:1}}])
  assert.deepEqual(panel.currentCoreReview.value.unchecked_ids,['shot_2'])
  assert.deepEqual(panel.coreReviewUncertainties.value,['Hand partly hidden'])
  assert.equal(panel.coreReviewIssues.value.length,1)
  panel.project.value={...panel.project.value,revision:2}
  assert.equal(panel.currentCoreReview.value,null)
  await panel.repairCoreIssue('shot_1')
  assert.equal(requests.length,1)
})

test('core repair submits only server report identity and requires clean idle state', async t => {
  const {panel,requests}=await editor(t,'openai_images')
  panel.coreImageReview.value={...coreReport(),accountKey:'owner'}
  panel.dirty.value=true
  await panel.repairCoreIssue('shot_1')
  assert.deepEqual(requests,[])
  panel.dirty.value=false
  panel.busy.value=true
  await panel.checkCoreImages()
  await panel.repairCoreIssue('shot_1')
  assert.deepEqual(requests,[])
  panel.busy.value=false
  await panel.repairCoreIssue('shot_1')
  assert.deepEqual(requests,[{url:'/api/video-studio/image-options/images/repair-from-review',
    body:{revision:1,shot_id:'shot_1',fingerprints:['pixels-and-design']}}])
  assert.equal(panel.currentCoreReview.value,null)
})

test('switching projects while confirming core repair cannot submit the old repair', async t => {
  let switchProject
  const {panel,requests}=await editor(t,'openai_images',()=>{switchProject();return true})
  panel.coreImageReview.value={...coreReport(),accountKey:'owner'}
  switchProject=()=>{panel.project.value={...record('openai_images'),id:'different-project'}}
  await panel.repairCoreIssue('shot_1')
  assert.deepEqual(requests,[])
  assert.equal(panel.currentCoreReview.value,null)
})

test('an existing 2K redraw override is cleared when the same project changes to Images', async t => {
  const { panel, setProtocol, requests } = await editor(t, 'async_task')
  panel.redrawResolution.value = '2k'
  setProtocol('openai_images')
  assert.deepEqual(panel.redrawResolutions.value, ['1k'])
  assert.equal(panel.redrawResolution.value, '')
  assert.deepEqual(requests, [], 'a protocol change cannot start a redraw')
})

test('scene repair is explicit and sends only revision to the server-owned review', async t => {
  const { panel, requests, mounted } = await editor(t, 'openai_images')
  const asset = { id: 'room', image_version: 'current', review_design_fingerprint:'design1', quality_review: { design_fingerprint:'design1', image_version: 'current', issues: [{ correction: 'Correct sign' }], uncertainties: [] } }
  panel.project.value.scene_assets = [asset]
  await mounted.flush()
  assert.deepEqual(requests, [])
  assert.equal(panel.currentSceneQuality(asset), asset.quality_review)
  assert.equal(panel.currentSceneQuality({ ...asset, image_version: 'new' }), null)
  assert.equal(panel.currentSceneQuality({ ...asset, review_design_fingerprint: 'new-design' }), null)
  assert.equal(panel.currentSceneQuality({ ...asset, review_design_fingerprint: undefined }), null)
  panel.dirty.value=true
  assert.equal(panel.currentSceneQuality(asset),null)
  panel.dirty.value=false
  await panel.repairSceneFromReview(asset)
  assert.equal(requests.length, 1)
  assert.ok(requests[0].url.endsWith('/scene-references/room/repair'))
  assert.deepEqual(requests[0].body, { revision: 1 })
})

test('native redraw requests cannot submit a stale unsupported override and explicit 1K is preserved', async t => {
  const { panel, requests, mounted } = await editor(t, 'openai_images')
  panel.redrawResolution.value = '2k'
  await panel.redrawShot(panel.project.value.shots[0]); await mounted.flush()
  assert.equal(requests[0].body.image_resolution, null)
  panel.redrawResolution.value = '1k'
  await panel.redrawShot(panel.project.value.shots[0])
  assert.equal(requests[1].body.image_resolution, '1k')
})

for (const protocol of [undefined, 'async_task']) {
  test(`redraw without a native snapshot retains existing 1K/2K/4K behavior (${protocol || 'legacy'})`, async t => {
    const { panel, requests } = await editor(t, protocol)
    assert.deepEqual(panel.redrawResolutions.value, ['1k', '2k', '4k'])
    panel.redrawResolution.value = '4k'
    await panel.redrawShot(panel.project.value.shots[0])
    assert.equal(requests[0].body.image_resolution, '4k')
  })
}

test('single failed picture retry requires explicit confirmation and never invokes the batch', async t => {
  const questions = []
  const { panel, requests, mounted } = await editor(t, 'openai_images', text => { questions.push(text); return true })
  const shot = panel.project.value.shots[0]
  shot.image_status = 'failed'
  shot.image_error = 'Previous outcome unknown'
  shot.image_prompt = 'Corrected opening frame'
  await panel.retryShotImage(shot); await mounted.flush()
  assert.equal(questions.length, 1)
  assert.match(questions[0], /原请求结果未确认.*重复计费/)
  assert.deepEqual(requests, [{url: '/api/video-studio/image-options/images/shot_1/retry',
    body: {revision: 1, prompt: 'Corrected opening frame', confirm_new_attempt: true}}])
})

test('declining a new image attempt leaves the failed shot untouched', async t => {
  const { panel, requests } = await editor(t, 'openai_images', () => false)
  const shot = panel.project.value.shots[0]
  shot.image_status = 'failed'
  const before = JSON.stringify(panel.project.value)
  await panel.retryShotImage(shot)
  assert.deepEqual(requests, [])
  assert.equal(JSON.stringify(panel.project.value), before)
})

test('retry button cannot issue a second request while an image edit is running', async t => {
  const { panel, requests } = await editor(t, 'openai_images')
  const shot = panel.project.value.shots[0]
  shot.image_status = 'failed'
  shot.image_task = {status: 'running', action: 'retry'}
  await panel.retryShotImage(shot)
  assert.deepEqual(requests, [])
})

test('outdated shared scene blocks confirmation until the core uses the current version', async t => {
  const { panel, requests } = await editor(t, 'openai_images')
  const project = panel.project.value
  project.creation_parameters.dynamic_text_mode = 'visual_first'
  project.creation_parameters.scene_references_enabled = true
  project.scene_references_status = 'completed'
  project.scene_assets = [{id: 'scene-A', image_status: 'completed', image_version: 'new'}]
  const shot = project.shots[0]
  Object.assign(shot, {scene_reference_id: 'scene-A', scene_reference_used_version: 'old'})
  assert.equal(panel.sceneReferencesBlockConfirm.value, true)
  assert.deepEqual(panel.staleSceneShots.value.map(row => row.id), ['shot_1'])
  shot.scene_reference_used_version = 'new'
  assert.equal(panel.sceneReferencesBlockConfirm.value, false)
  assert.deepEqual(requests, [], 'checking scene state cannot submit or adopt anything')
})

test('scene observation notes retain their explanation and legacy uncertainties remain blocking', async t => {
  const { panel, requests } = await editor(t, 'openai_images')
  const asset={image_version:'v2',review_design_fingerprint:'d2',quality_review:{design_fingerprint:'d2',image_version:'v2',issues:[],uncertainties:['Cropped decoration']}}
  assert.deepEqual(panel.sceneBlockingNotes(asset),['Cropped decoration'])
  assert.equal(panel.sceneUncertaintyLabel(asset,0),'待核实')
  asset.quality_review.blocking_uncertainties=[]
  asset.quality_review.uncertainty_assessments=[{index:0,scope:'incidental',impact:'Approved passage is fully visible'}]
  assert.deepEqual(panel.sceneBlockingNotes(asset),[])
  assert.equal(panel.sceneUncertaintyLabel(asset,0),'观察备注（不阻断）')
  assert.deepEqual(asset.quality_review.uncertainties,['Cropped decoration'])
  asset.image_version='v3'
  assert.equal(panel.currentSceneQuality(asset),null)
  assert.deepEqual(requests,[])
})

test('completed pictures with changed continuity dependencies remain locally repairable', async t => {
  const { panel, requests } = await editor(t, 'openai_images')
  const project=panel.project.value
  project.status='image_review'
  project.creation_parameters.scene_references_enabled=false
  for(const shot of project.shots)shot.image_status='completed'
  project.shots[0].core_reference_error='连续性源图已换版'
  assert.equal(panel.imageRetryNeeded.value,true)
  assert.equal(panel.staleCoreShots.value.length,1)
  assert.match(panel.imageConfirmationBlock.value,/连续性参考已变化/)
  project.reedit_shot_id=project.shots[0].id
  assert.match(panel.localReeditBlock.value,/源图已换版/)
  delete project.reedit_shot_id
  delete project.shots[0].core_reference_error
  assert.equal(panel.imageRetryNeeded.value,false)
  assert.deepEqual(requests,[])
})
