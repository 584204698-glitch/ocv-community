import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { reactive } from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const url=new URL('../src/components/ProjectVoicePanel.vue',import.meta.url)
const code=compileScript(parse(await readFile(url,'utf8')).descriptor,{id:'voice-panel-polling'}).content
  .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(line,names,specifier)=>{
    if(specifier.endsWith('.vue'))return `const ${names} = {}`
    const target=specifier==='vue'?import.meta.resolve('vue'):new URL(specifier.endsWith('.js')?specifier:specifier+'.js',url).href
    return `import ${names} from '${target}'`
  })
const {default:component}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))

test('real voice panel preserves selection through project polls and closes only on identity change',async t=>{
  const character='pc_'+'a'.repeat(32)
  const props=reactive({project:{id:'project-a',revision:1},accountKey:'owner',disabled:false})
  const browser=installBrowserGlobals({fetch:async()=>Response.json({project_id:props.project.id,
    revision:props.project.revision,characters:[{character_id:character,name:'小兔',status:'unbound'}]})})
  const mounted=mountComposable(()=>component.setup(props,{expose(){},emit(){}}))
  t.after(()=>{mounted.unmount();browser.restore()})
  await mounted.flush()
  mounted.instance.selectedId.value=character
  props.project={id:'project-a',revision:1,logs:['one more progress line']}
  await mounted.flush()
  assert.equal(mounted.instance.selectedId.value,character)
  props.project={id:'project-a',revision:2}
  await mounted.flush()
  assert.equal(mounted.instance.selectedId.value,character)
  props.project={id:'project-b',revision:1}
  await mounted.flush()
  assert.equal(mounted.instance.selectedId.value,'')
})
