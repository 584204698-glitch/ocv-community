import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { reactive } from 'vue'
import { api } from '../src/api.js'
import { mountComposable } from './support/composableHarness.mjs'

// Compile the actual SFC setup so these tests execute the shipping form logic.
// The browser mock separately verifies the rendered controls and interactions.
const source = await readFile(new URL('../src/components/ImageProfileSelector.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'image-profile-test' }).content
  .replaceAll("from 'vue'", `from '${import.meta.resolve('vue')}'`)
  .replaceAll("from '../api'", `from '${new URL('../src/api.js', import.meta.url).href}'`)
const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(script).toString('base64'))
const asyncProfile = { id: 'async', name: 'Existing async', protocol: 'async_task', base_url: 'https://async.example', model_id: 'image-model', text_endpoint: '/custom/text', reference_endpoint: '/custom/reference', query_endpoint: '/custom/query', resolutions: ['2k', '4k'], reference_images: true, configured: true, key_count: 2, key_hints: ['masked-a', 'masked-b'], legacy: false }
const imagesProfile = { id: 'images', name: 'Images edit', protocol: 'openai_images', credential_source: 'language_custom', base_url: 'https://pinned.example/v1', model_id: 'gpt-image-2', resolutions: ['1k'], reference_images: true, configured: true, key_count: 1 }
async function editor(t, initial = []) {
  let records = structuredClone(initial)
  const calls = []
  t.mock.method(api, 'imageProfiles', async () => ({ profiles: records }))
  t.mock.method(api, 'saveImageProfile', async payload => {
    calls.push(payload)
    const profile = { ...payload, id: payload.id || 'created', configured: true }
    records = [...records.filter(row => row.id !== profile.id), profile]
    return { profile, message: 'Saved' }
  })
  const form = reactive({ image_profile_id: '', image_resolution: '4k' })
  const mounted = mountComposable(() => component.setup({ form, manage: true }, { expose() {} }))
  t.after(() => mounted.unmount())
  await mounted.flush()
  return { ...mounted, panel: mounted.instance, form, calls }
}

test('native profile reset removes response metadata and stale async paths or resolutions', async t => {
  const { panel } = await editor(t)
  panel.reset({ ...asyncProfile, api_keys: ['must-not-copy'] })
  panel.draft.api_key = 'typed-local-key'
  panel.reset({ ...imagesProfile, text_endpoint: '/old/task', reference_endpoint: 'https://other.example/receive', query_endpoint: '/query', resolutions: ['4k'], key_hints: ['private-hint'], api_keys: ['must-not-copy'] })
  assert.equal(panel.draft.protocol, 'openai_images')
  assert.equal(panel.draft.credential_source, 'language_custom')
  assert.equal(panel.draft.text_endpoint, '/images/generations')
  assert.equal(panel.draft.reference_endpoint, '/images/edits')
  assert.equal(panel.draft.query_endpoint, '')
  assert.deepEqual(panel.draft.resolutions, ['1k'])
  for (const key of ['api_keys', 'key_hints', 'configured', 'key_count', 'legacy']) assert.equal(Object.hasOwn(panel.draft, key), false, key)
  assert.equal(panel.draft.api_key, '')
})

test('protocol changes and a fresh draft reset credentials, endpoints and supported resolutions', async t => {
  const { panel } = await editor(t)
  panel.reset(asyncProfile)
  panel.draft.api_key = 'typed-key'
  panel.changeProtocol('openai_images')
  assert.equal(panel.draft.reference_endpoint, '/images/edits')
  assert.deepEqual(panel.draft.resolutions, ['1k'])
  assert.equal(panel.draft.api_key, '')
  panel.changeCredentialSource('language_custom')
  assert.equal(panel.draft.base_url, '')
  panel.changeProtocol('async_task')
  assert.equal(panel.draft.credential_source, 'profile')
  assert.equal(panel.draft.base_url, '')
  assert.equal(panel.draft.reference_endpoint, '/openapi/v2/{model}/image-to-image')
  assert.equal(panel.draft.query_endpoint, '/openapi/v2/query')
  assert.deepEqual(panel.draft.resolutions, ['1k', '2k', '4k'])
  panel.reset(imagesProfile)
  panel.reset()
  assert.equal(panel.draft.protocol, 'async_task')
  assert.equal(panel.draft.credential_source, 'profile')
  assert.equal(panel.draft.id, '')
  assert.equal(panel.draft.model_id, '')
  assert.equal(panel.draft.name, '')
})

test('switching credential source cannot carry a typed key or a pinned shared address into the other source', async t => {
  const { panel } = await editor(t)
  panel.reset({ ...imagesProfile, credential_source: 'profile' })
  panel.draft.api_key = 'typed-private-key'
  panel.changeCredentialSource('language_custom')
  assert.equal(panel.draft.api_key, '')
  assert.equal(panel.draft.base_url, '')
  panel.reset(imagesProfile)
  panel.changeCredentialSource('profile')
  assert.equal(panel.draft.base_url, '')
  assert.equal(panel.canSave.value, false)
  panel.changeProtocol('async_task')
  panel.changeCredentialSource('language_custom')
  assert.equal(panel.draft.credential_source, 'profile')
})

test('shared Images save sends only approved fields, fixed same-service paths, 1k and no key', async t => {
  const { panel, calls, form } = await editor(t, [imagesProfile])
  panel.reset(imagesProfile)
  Object.assign(panel.draft, { api_key: 'should-not-send', api_keys: ['should-not-send'], key_hints: ['should-not-send'], text_endpoint: 'https://other.example/receive', reference_endpoint: '/old/async', query_endpoint: '/old/query', resolutions: ['4k'] })
  await panel.save()
  assert.deepEqual(calls[0], { id: 'images', name: 'Images edit', protocol: 'openai_images', credential_source: 'language_custom', base_url: '', model_id: 'gpt-image-2', text_endpoint: '/images/generations', reference_endpoint: '/images/edits', query_endpoint: '', resolutions: ['1k'], reference_images: true })
  assert.equal(form.image_resolution, '1k')
  assert.equal(panel.draft.api_key, '')
})

test('async save preserves custom paths, chosen resolutions and existing credentials with a blank key', async t => {
  const { panel, calls } = await editor(t, [asyncProfile])
  panel.reset(asyncProfile)
  await panel.save()
  assert.deepEqual(calls[0], { id: 'async', name: 'Existing async', protocol: 'async_task', credential_source: 'profile', base_url: 'https://async.example', model_id: 'image-model', text_endpoint: '/custom/text', reference_endpoint: '/custom/reference', query_endpoint: '/custom/query', resolutions: ['2k', '4k'], reference_images: true })
  assert.equal(Object.hasOwn(calls[0], 'api_key'), false)
})

test('Images can explicitly save its own endpoint and key without borrowing language credentials', async t => {
  const { panel, calls } = await editor(t)
  panel.reset()
  panel.changeProtocol('openai_images')
  Object.assign(panel.draft, { name: ' My model ', model_id: ' provider-specific-model ', base_url: ' https://images.example/v1 ', api_key: ' example-local-key ' })
  await panel.save()
  assert.equal(calls[0].credential_source, 'profile')
  assert.equal(calls[0].base_url, 'https://images.example/v1')
  assert.equal(calls[0].api_key, 'example-local-key')
  assert.equal(calls[0].model_id, 'provider-specific-model')
  assert.equal(calls[0].text_endpoint, '/images/generations')
  assert.equal(Object.hasOwn(calls[0], 'id'), false)
})

test('native task resolution is limited to 1k even when an old record advertises higher values', async t => {
  const { panel, form } = await editor(t, [{ ...imagesProfile, resolutions: ['2k', '4k'] }])
  assert.deepEqual(panel.resolutions.value, ['1k'])
  assert.equal(form.image_resolution, '1k')
  panel.reset(imagesProfile)
  panel.toggleResolution('4k')
  assert.deepEqual(panel.draft.resolutions, ['1k'])
})

test('a failed configuration save preserves the draft and supports an explicit retry without a model call', async t => {
  const { panel, calls } = await editor(t)
  panel.reset(imagesProfile)
  t.mock.method(api, 'saveImageProfile', async () => { throw new Error('文字接口地址已变化，请重新保存') })
  await panel.save()
  assert.equal(panel.editing.value, true)
  assert.equal(panel.draft.model_id, 'gpt-image-2')
  assert.match(panel.message.value, /重新保存/)
  assert.equal(panel.saving.value, false)
  assert.equal(panel.canSave.value, true)
  assert.deepEqual(calls, [])
})
