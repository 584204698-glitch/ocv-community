import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { reactive } from 'vue'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const componentUrl = new URL('../src/components/VideoStudio.vue', import.meta.url)
const compiled = compileScript(parse(await readFile(componentUrl, 'utf8')).descriptor, { id: 'shot-selection' }).content
  .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (line, names, specifier) => {
    if (specifier.endsWith('.vue')) return `const ${names} = {}`
    const target = specifier === 'vue' ? import.meta.resolve('vue') : new URL(specifier.endsWith('.js') ? specifier : specifier + '.js', componentUrl).href
    return `import ${names} from '${target}'`
  })
const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))

const project = id => ({id, revision: 1, status: 'video_generating', settings: {name: id, ratio: '16:9'},
  creation_parameters: {dynamic_auto_advance: true, scene_references_enabled: false}, scenes: [], logs: [],
  shots: Array.from({length: 5}, (_, index) => ({id: `${id}-${index}`, kind: 'video', slide_ids: [], image_status: 'completed', video_status: index === 3 ? 'completed' : 'pending'}))})

async function editor(t, respond = () => null) {
  const records = {a: project('a'), b: project('b')}, calls = []
  const browser = installBrowserGlobals({fetch: async (url, options = {}) => {
    calls.push({url, method: options.method || 'GET', ...(options.body?{body:JSON.parse(options.body)}:{})})
    const override=respond(url,options)
    if(override)return override
    if (url.endsWith('/project-cast')) return Response.json({project_id: url.split('/')[3], revision: 1, characters: [], assets: []})
    if (url.startsWith('/api/video-studio/') && records[url.split('/')[3]]) return Response.json(records[url.split('/')[3]])
    return Response.json({items: []})
  }})
  const props = reactive({accountKey: 'owner-one', suppressAutoAdvance: true})
  const mount = () => mountComposable(() => component.setup(props, {expose() {}, emit() {}}))
  let mounted = mount()
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  return {records, calls, browser, props, get panel() {return mounted.instance},
    async flush() {await mounted.flush()},
    async open(id) {await mounted.instance.open(id);await mounted.flush()},
    async remount() {mounted.unmount();mounted=mount();await mounted.flush()}}
}

test('manual export click event still asks for confirmation and cancellation writes nothing', async t => {
  const e=await editor(t)
  e.records.a.status='video_review'
  for(const shot of e.records.a.shots)shot.video_status='completed'
  await e.open('a')
  let confirmations=0
  t.mock.method(globalThis,'confirm',()=>{confirmations++;return false})
  await e.panel.exportVideo({type:'click',isTrusted:true})
  assert.equal(confirmations,1)
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
})

test('explicit automatic export continues without a confirmation', async t => {
  const e=await editor(t)
  e.records.a.status='video_review'
  for(const shot of e.records.a.shots)shot.video_status='completed'
  await e.open('a')
  t.mock.method(globalThis,'confirm',()=>{throw Error('Unexpected automatic confirmation')})
  await e.panel.exportVideo(true)
  assert.equal(e.calls.filter(call=>call.method==='POST'&&call.url.endsWith('/export')).length,1)
})

test('returning after another project and remount keeps that project’s selected shot without writes', async t => {
  const e=await editor(t)
  await e.open('a');e.panel.selectVideoShot(3);await e.flush()
  await e.open('b');e.panel.selectVideoShot(1);await e.flush()
  await e.open('a');assert.equal(e.panel.selectedShot.value.id,'a-3')
  await e.remount();await e.open('b');assert.equal(e.panel.selectedShot.value.id,'b-1')
  await e.open('a');assert.equal(e.panel.selectedShot.value.id,'a-3')
  assert.ok(e.calls.every(call=>call.method==='GET'))
})

test('restoration follows shot identity across reordering and falls back when it was deleted', async t => {
  const e=await editor(t)
  await e.open('a');e.panel.selectVideoShot(3);await e.flush();await e.open('b')
  e.records.a.shots.reverse();await e.open('a')
  assert.equal(e.panel.selectedShot.value.id,'a-3');assert.equal(e.panel.selected.value,1)
  await e.open('b');e.records.a.shots=e.records.a.shots.filter(s=>s.id!=='a-3');await e.open('a')
  assert.equal(e.panel.selected.value,0);assert.equal(e.panel.selectedShot.value.id,'a-4')
})

test('opening from the home-page navigation intent restores the shot on component mount', async t => {
  const e=await editor(t)
  await e.open('a');e.panel.selectVideoShot(3);await e.flush()
  e.browser.sessionStorage.setItem('ocv-video-open','a')
  await e.remount()
  assert.equal(e.panel.project.value.id,'a')
  assert.equal(e.panel.selectedShot.value.id,'a-3')
  assert.equal(e.browser.sessionStorage.getItem('ocv-video-open'),null)
  assert.ok(e.calls.every(call=>call.method==='GET'))
})

test('account changes cannot borrow another account’s remembered shot', async t => {
  const e=await editor(t)
  await e.open('a');e.panel.selectVideoShot(3);await e.flush()
  e.props.accountKey='owner-two';await e.flush();await e.open('a')
  assert.equal(e.panel.selected.value,0)
  e.props.accountKey='owner-one';await e.flush();await e.open('a')
  assert.equal(e.panel.selectedShot.value.id,'a-3')
})

test('narration gain stays project-scoped, survives polling, and is sent only on export', async t => {
  const e=await editor(t)
  e.records.a.status='video_review'
  for(const shot of e.records.a.shots)shot.video_status='completed'
  e.records.a.export_settings={use_video_audio:true,narration_volume:.5}
  e.records.b.export_settings={use_video_audio:true,narration_volume:0}
  await e.open('a')
  assert.equal(e.panel.narrationVolume.value,50)
  e.panel.narrationVolume.value=35
  e.panel.project.value={...e.panel.project.value,revision:2};await e.flush()
  assert.equal(e.panel.narrationVolume.value,35,'polling must not reset the slider')
  await e.open('b');assert.equal(e.panel.narrationVolume.value,0,'zero is a valid saved volume')
  await e.open('a');assert.equal(e.panel.narrationVolume.value,50)
  assert.ok(e.calls.every(call=>call.method==='GET'),'editing slider does not submit a render')
  e.panel.narrationVolume.value=35
  await e.panel.exportVideo(true);await e.flush()
  assert.deepEqual(e.calls.filter(call=>call.method==='POST'&&call.url.endsWith('/export')),
    [{url:'/api/video-studio/a/export',method:'POST',body:{revision:1,use_video_audio:true,narration_volume:.35,end_title_text:''}}])
})

test('remembered shot does not override the current completed stage or unsaved-input guard', async t => {
  const e=await editor(t)
  await e.open('a');e.panel.selectVideoShot(3);await e.flush()
  e.panel.videoInputDirty.value=true;e.panel.selectVideoShot(1);await e.flush()
  assert.equal(e.panel.selected.value,3)
  e.panel.videoInputDirty.value=false;await e.open('b')
  e.records.a.status='completed';await e.open('a')
  assert.equal(e.panel.view.value,'export');assert.equal(e.panel.selectedShot.value.id,'a-3')
})

test('unavailable browser storage leaves selection usable and opening safe', async t => {
  const e=await editor(t)
  t.mock.method(e.browser.sessionStorage,'getItem',()=>{throw Error('storage disabled')})
  t.mock.method(e.browser.sessionStorage,'setItem',()=>{throw Error('storage full')})
  await e.open('a');e.panel.selectVideoShot(3);await e.flush()
  assert.equal(e.panel.selected.value,3)
  await e.open('b');await e.open('a');assert.equal(e.panel.selected.value,0)
  assert.equal(e.panel.error.value,'');assert.ok(e.calls.every(call=>call.method==='GET'))
})

test('one-click project stays in a user-opened shot repair instead of confirming and advancing it', async t => {
  const e=await editor(t)
  e.records.a.status='image_review'
  e.records.a.reedit_shot_id='a-3'
  e.records.a.reedit_return_status='completed'
  for(const shot of e.records.a.shots){shot.action='A small movement';shot.video_prompt='A small movement';shot.video_status='completed'}
  await e.open('a')
  e.props.suppressAutoAdvance=false
  await e.panel.advanceAutoPilot();await e.flush()
  assert.deepEqual(e.calls.filter(call=>call.method!=='GET'),[])
  assert.equal(e.panel.project.value.reedit_shot_id,'a-3')
  assert.equal(e.panel.view.value,'storyboard')
})

test('reopened one-click project requires one explicit resume and does not duplicate submission', async t => {
  const e=await editor(t)
  e.records.a.status='generation_ready'
  await e.open('a')
  assert.equal(e.panel.canResumeAutoAdvance.value,true)
  await e.panel.advanceAutoPilot();await e.flush()
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
  await e.panel.resumeAutoAdvance();await e.flush()
  await e.panel.resumeAutoAdvance();await e.panel.advanceAutoPilot();await e.flush()
  assert.deepEqual(e.calls.filter(call=>call.method!=='GET'),[
    {url:'/api/video-studio/a/images/generate',method:'POST',body:{revision:1}},
  ])
})

test('resume authorization expires across project, account and component changes', async t => {
  const e=await editor(t)
  await e.open('a');await e.panel.resumeAutoAdvance()
  assert.equal(e.panel.autoAdvanceEnabled.value,true)
  await e.open('b');await e.open('a')
  assert.equal(e.panel.autoAdvanceEnabled.value,false)
  await e.panel.resumeAutoAdvance()
  e.props.accountKey='owner-two';await e.flush();await e.open('a')
  assert.equal(e.panel.autoAdvanceEnabled.value,false)
  await e.panel.resumeAutoAdvance();await e.remount();await e.open('a')
  assert.equal(e.panel.autoAdvanceEnabled.value,false)
  assert.ok(e.calls.every(call=>call.method==='GET'))
})

test('resume retains unsaved-input, shot repair and failed-image protections', async t => {
  const e=await editor(t)
  e.records.a.status='image_review'
  e.records.a.shots[0].image_status='failed'
  await e.open('a')
  for(const flag of ['dirty','videoInputDirty']){
    e.panel[flag].value=true
    await e.panel.resumeAutoAdvance()
    assert.equal(e.panel.autoAdvanceEnabled.value,false)
    e.panel[flag].value=false
  }
  await e.panel.resumeAutoAdvance();await e.flush()
  assert.match(e.panel.error.value,/失败/)
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
  await e.open('b')
  e.records.a.reedit_shot_id='a-3';await e.open('a')
  assert.equal(e.panel.canResumeAutoAdvance.value,false)
  await e.panel.resumeAutoAdvance()
  assert.equal(e.panel.autoAdvanceEnabled.value,false)
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
})

test('leaving an unchanged shot repair preserves its video instead of requiring a reroll', async t => {
  const e=await editor(t)
  e.records.a.status='image_review'
  e.records.a.reedit_shot_id='a-3'
  e.records.a.shots[3].video_prompt='Approved original motion'
  await e.open('a')
  t.mock.method(globalThis,'confirm',()=>{throw Error('Unexpected confirmation')})
  await e.panel.finishShotReedit(e.panel.project.value.shots[3]);await e.flush()
  const writes=e.calls.filter(call=>call.method!=='GET')
  assert.equal(writes.length,1)
  assert.equal(writes[0].url,'/api/video-studio/a/images/confirm')
  assert.deepEqual(writes[0].body,{revision:1})
  assert.equal(e.panel.project.value.shots[3].video_status,'completed')
})

test('a revision conflict refreshes current state, keeps local prompts and never repeats the write', async t => {
  const e=await editor(t,(url,options)=>options.method==='POST'?Response.json({detail:'项目已被修改'},{status:409}):null)
  e.records.a.status='image_review'
  await e.open('a')
  e.panel.project.value.shots[0].image_prompt='作者刚编辑的图片提示词'
  e.panel.rememberImagePrompt(e.panel.project.value.shots[0])
  e.records.a.revision=2
  await e.panel.confirmImages();await e.flush()
  assert.equal(e.panel.project.value.revision,2)
  assert.equal(e.panel.project.value.shots[0].image_prompt,'作者刚编辑的图片提示词')
  assert.match(e.panel.error.value,/已刷新项目状态/)
  assert.equal(e.panel.autoAdvancePaused.value,true)
  assert.equal(e.calls.filter(call=>call.method==='POST').length,1)
  e.panel.acceptImageUpdate({...e.records.a,revision:1})
  assert.equal(e.panel.project.value.revision,2)
  e.panel.acceptImageUpdate({...e.records.b,revision:10})
  assert.equal(e.panel.project.value.id,'a')
})

test('automatic generation pauses on revision conflict instead of submitting the refreshed version', async t => {
  const e=await editor(t,(url,options)=>options.method==='POST'?Response.json({detail:'项目已被修改'},{status:409}):null)
  e.records.a.status='generation_ready'
  await e.open('a')
  e.records.a.revision=2
  await e.panel.resumeAutoAdvance();await e.flush()
  await e.panel.advanceAutoPilot();await e.flush()
  assert.equal(e.panel.project.value.revision,2)
  assert.equal(e.panel.autoAdvanceEnabled.value,false)
  assert.equal(e.panel.canResumeAutoAdvance.value,true)
  assert.equal(e.calls.filter(call=>call.method==='POST').length,1)
})

test('opening a shot repair needs one action and still protects unsaved inputs', async t => {
  const e=await editor(t)
  await e.open('a')
  t.mock.method(globalThis,'confirm',()=>{throw Error('Unexpected confirmation')})
  e.panel.videoInputDirty.value=true
  await e.panel.reopenShot(e.panel.project.value.shots[3]);await e.flush()
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
  e.panel.videoInputDirty.value=false
  await e.panel.reopenShot(e.panel.project.value.shots[3]);await e.flush()
  assert.deepEqual(e.calls.filter(call=>call.method!=='GET'),[
    {url:'/api/video-studio/a/shots/a-3/reopen',method:'POST',body:{revision:1}},
  ])
})

test('local bulk retry lists only explicit terminal failures and does not submit on page open', async t => {
  const e=await editor(t)
  e.records.a.creation_parameters.video_generation_backend='comfyui'
  e.records.a.shots[0].video_status='failed';e.records.a.shots[0].video_terminal=true
  e.records.a.shots[1].video_status='unknown';e.records.a.shots[1].video_terminal=false
  e.records.a.shots[2].video_status='running'
  await e.open('a')
  assert.deepEqual(e.panel.localRetryVideos.value.map(shot=>shot.id),['a-0'])
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
  await e.open('b')
  assert.deepEqual(e.panel.localRetryVideos.value,[])
})

test('local video dimensions never fall back to unrelated cloud 720p settings', async t => {
  const e=await editor(t)
  e.records.a.creation_parameters.video_generation_backend='comfyui'
  await e.open('a')
  assert.equal(e.panel.requestedResolution(e.panel.selectedShot.value),'按所选工作流')
  e.panel.selectedShot.value.video_request={resolution:'1344×768'}
  assert.equal(e.panel.requestedResolution(e.panel.selectedShot.value),'1344×768')
})

test('local queue append submits only pending shots after an explicit action', async t => {
  const e=await editor(t, url => url === '/api/comfyui' ? Response.json({profiles:[{id:'local-test',kind:'video'}]}) : null)
  e.records.a.creation_parameters.video_generation_backend='comfyui'
  e.records.a.creation_parameters.comfyui_profile_id='local-test'
  e.records.a.shots[0].video_status='running'
  e.records.a.shots[1].video_status='unknown'
  e.records.a.shots[2].video_status='failed';e.records.a.shots[2].video_terminal=true
  await e.open('a')
  assert.deepEqual(e.panel.localQueueCandidates.value.map(shot=>shot.id),['a-4'])
  assert.equal(e.calls.filter(call=>call.method!=='GET').length,0)
  t.mock.method(globalThis,'confirm',()=>true)
  await e.panel.generateVideos(e.panel.localQueueCandidates.value);await e.flush()
  assert.deepEqual(e.calls.filter(call=>call.method==='POST'),[
    {url:'/api/video-studio/a/videos/generate',method:'POST',body:{revision:1,shot_ids:['a-4'],retry_failed:false,regenerate_completed:false,
      shot_options:{'a-4':{backend:'comfyui',profile_id:'local-test',resolution:'',h3_prompt_agent:false}}}},
  ])
  e.panel.project.value.status='video_stopping'
  assert.deepEqual(e.panel.localQueueCandidates.value,[])
  await e.open('b')
  assert.deepEqual(e.panel.localQueueCandidates.value,[])
})
