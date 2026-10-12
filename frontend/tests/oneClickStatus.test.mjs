import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const componentUrl = new URL('../src/components/OneClickStatus.vue', import.meta.url)
const source = await readFile(componentUrl, 'utf8')
const compiled = compileScript(parse(source).descriptor, { id: 'video-image-options' }).content
  .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (line, names, specifier) => {
    if (specifier.endsWith('.vue')) return `const ${names} = {}`
    const target = specifier === 'vue' ? import.meta.resolve('vue') : new URL(specifier.endsWith('.js') ? specifier : specifier + '.js', componentUrl).href
    return `import ${names} from '${target}'`
  })
const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))
test('polling success preserves a rejected resume reason until the next action', async t => {
  let failPoll = false
  const browser = installBrowserGlobals({fetch: async (url, options = {}) => {
    if (options.method === 'POST') return Response.json({detail:'Repair the failed shot first'}, {status:409})
    if (failPoll) throw new Error('Network offline')
    return Response.json({state:'paused', message:'Waiting'})
  }})
  const mounted=mountComposable(()=>component.setup({jobId:'project'}, {expose(){},emit(){}}))
  t.after(()=>{try{mounted.unmount()}finally{browser.restore()}})
  await mounted.flush()
  const panel=mounted.instance
  await panel.control('resume')
  const failure=panel.error.value
  assert.match(failure,/Repair the failed shot/)
  await panel.refresh()
  assert.equal(panel.error.value,failure)
  failPoll=true;await panel.refresh();assert.match(panel.pollError.value,/Network offline/)
  failPoll=false;await panel.refresh()
  assert.equal(panel.pollError.value,'')
  assert.equal(panel.error.value,failure)
})
