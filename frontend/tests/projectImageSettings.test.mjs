import assert from 'node:assert/strict'
import test from 'node:test'
import { useWorkspace } from '../src/useWorkspace.js'
import { useStudio } from '../src/useStudio.js'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const projectId = 'a'.repeat(32)
async function editor(t, fromPool) {
  const writes = []
  const browser = installBrowserGlobals({ fetch: async (url, options = {}) => {
    if (url === `/api/video-studio/${projectId}/settings`) {
      writes.push(JSON.parse(options.body))
      return Response.json({ id: projectId, revision: 2, status: 'storyboard_review', shots: [{ id: 'kept-shot' }] })
    }
    return Response.json(url === '/api/session' ? { user: null, auth_mode: 'account' } : {})
  } })
  const mounted = mountComposable(() => { const w = useWorkspace(); return { w, s: useStudio(w) } })
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  const { w, s } = mounted.instance
  w.session.value = { user: { id: 1 } }
  const form = JSON.parse(JSON.stringify(w.form))
  Object.assign(form, { dynamic_video: true, script: 'Existing narration.', project_name: 'Existing project', use_cloud_image_pool: fromPool, image_profile_id: 'same-saved-profile', image_resolution: '1k', image_profile_snapshot: { base_url: 'https://old.example/v1', model_id: 'old-model' } })
  s.newProject('dynamic', { id: 'settings-edit', form, rerun: true, source_project_id: projectId, source_project_revision: 1, rerun_base: form })
  return { w, s, browser, writes }
}

for (const fromPool of [true, false]) {
  test(`saving existing dynamic settings persists the cloud-pool switch from ${fromPool} and refreshes the image profile`, async t => {
    const { w, s, browser, writes } = await editor(t, fromPool)
    assert.equal(writes.length, 0, 'opening settings never updates the task snapshot')
    w.form.use_cloud_image_pool = !fromPool
    await s.launch()
    assert.equal(writes.length, 1)
    assert.equal(writes[0].revision, 1)
    assert.deepEqual(writes[0].parameters, { image_profile_id: 'same-saved-profile', image_resolution: '1k', use_cloud_image_pool: !fromPool })
    assert.equal(Object.hasOwn(writes[0].parameters, 'image_profile_snapshot'), false)
    assert.equal(s.studioPage.value, 'videos')
    assert.equal(browser.sessionStorage.getItem('ocv-video-autoplan'), null)
  })
}

test('explicitly saving an unchanged image selection refreshes its server snapshot without requesting replanning', async t => {
  const { s, browser, writes } = await editor(t, false)
  assert.equal(writes.length, 0)
  await s.launch()
  assert.deepEqual(writes[0].parameters, { image_profile_id: 'same-saved-profile', image_resolution: '1k' })
  assert.equal(browser.sessionStorage.getItem('ocv-video-autoplan'), null)
})
