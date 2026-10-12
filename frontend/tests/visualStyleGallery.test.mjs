import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { reactive } from 'vue'
import { compileScript, compileStyle, compileTemplate, parse } from '@vue/compiler-sfc'
import { visualStyleCatalog, videoProjectCover } from '../src/visualStyleCatalog.js'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const componentUrl = new URL('../src/components/VisualStyleGallery.vue', import.meta.url)
const { descriptor } = parse(await readFile(componentUrl, 'utf8'))
const compiled = compileScript(descriptor, { id: 'visual-style-gallery' }).content
  .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (line, names, specifier) => {
    const target = specifier === 'vue' ? import.meta.resolve('vue') : new URL(specifier.endsWith('.js') ? specifier : specifier + '.js', componentUrl).href
    return `import ${names} from '${target}'`
  })
const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))

async function gallery(t, initial = '') {
  const emitted = []
  const browser = installBrowserGlobals()
  const props = reactive({ modelValue: initial, disabled: false })
  const mounted = mountComposable(() => component.setup(props, { expose() {}, emit(...args) { emitted.push(args) } }))
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  return { props, panel: mounted.instance, emitted, flush: mounted.flush }
}

test('opening a custom style preserves it and never applies a preset', async t => {
  const custom = '写实水墨，雨天老街，保留我的自定义设定。'
  const { props, panel, emitted } = await gallery(t, custom)
  assert.equal(panel.selectedStyle.value, null)
  assert.equal(panel.currentLabel.value, '自定义画风')
  assert.equal(props.modelValue, custom)
  assert.deepEqual(emitted, [])
})

test('choosing a card emits only its visual prompt and a change event', async t => {
  const { props, panel, emitted, flush } = await gallery(t, '原有画风')
  const style = visualStyleCatalog.find(item => item.id === 'transparent-watercolor')
  panel.chooseStyle(style.id)
  await flush()
  assert.deepEqual(emitted, [['update:modelValue', style.prompt], ['change', style.prompt]])
  assert.equal(props.modelValue, '原有画风', 'the component must not mutate its parent or a story record')
})

test('project switches update the selected card without changing project settings', async t => {
  const { props, panel, emitted, flush } = await gallery(t, visualStyleCatalog[0].prompt)
  assert.equal(panel.selectedStyle.value.id, visualStyleCatalog[0].id)
  props.modelValue = visualStyleCatalog[1].prompt
  await flush()
  assert.equal(panel.selectedStyle.value.id, visualStyleCatalog[1].id)
  props.modelValue += ' 让光线更冷。'
  await flush()
  assert.equal(panel.selectedStyle.value, null)
  assert.equal(panel.currentLabel.value, '自定义画风')
  props.modelValue = ''
  await flush()
  assert.equal(panel.currentLabel.value, '尚未指定画风')
  assert.deepEqual(emitted, [])
})

test('disabled or invalid selections cannot change the current style', async t => {
  const { props, panel, emitted } = await gallery(t)
  panel.chooseStyle('unavailable-style')
  props.disabled = true
  panel.chooseStyle(visualStyleCatalog[0].id)
  assert.deepEqual(emitted, [])
})

test('every offered preview is a bundled image, with no external image request', async () => {
  for (const style of visualStyleCatalog) {
    assert.match(style.preview, /^\/style-previews\/[a-z-]+\.(webp|jpg)$/)
    const image = await readFile(new URL('../public' + style.preview, import.meta.url))
    if (style.preview.endsWith('.webp')) {
      assert.equal(image.subarray(0, 4).toString(), 'RIFF')
      assert.equal(image.subarray(8, 12).toString(), 'WEBP')
      assert.ok(image.length < 100_000)
    } else {
      assert.equal(image.subarray(0, 3).toString('hex'), 'ffd8ff')
      assert.ok(image.length < 1_000_000)
    }
  }
})

test('gallery template and scoped styles compile for the production build', () => {
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: componentUrl.pathname, id: 'visual-style-gallery' }).errors, [])
  for (const style of descriptor.styles) {
    assert.deepEqual(compileStyle({ source: style.content, filename: componentUrl.pathname, id: 'visual-style-gallery', scoped: true }).errors, [])
  }
})

test('a project cover uses only a completed core picture from that project', () => {
  const shots = [{id: 'failed', image: 'old.jpg', image_status: 'failed'},
    {id: 'core', image: 'core.jpg', image_status: 'completed', image_version: 'sha'}]
  const first = {id: 'A', revision: 4, shots}
  const second = {id: 'B', revision: 4, shots}
  assert.equal(videoProjectCover(first), '/api/video-studio/A/images/core?v=sha')
  assert.equal(videoProjectCover(second), '/api/video-studio/B/images/core?v=sha')
  assert.equal(videoProjectCover({id: 'C', shots: [shots[0]], references: shots}), '')
  assert.equal(videoProjectCover({id: 'D', shots: []}), '')
})
