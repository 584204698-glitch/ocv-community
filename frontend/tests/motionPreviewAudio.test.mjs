import assert from 'node:assert/strict'
import test from 'node:test'
import {readFile} from 'node:fs/promises'
import {compileScript,parse} from '@vue/compiler-sfc'
import {installBrowserGlobals,mountComposable} from './support/composableHarness.mjs'

const url=new URL('../src/components/VideoStudio.vue',import.meta.url)
const script=compileScript(parse(await readFile(url,'utf8')).descriptor,{id:'motion-preview-audio'}).content
 .replace(/^import\s+(.+?)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,names,specifier)=>{
  if(specifier.endsWith('.vue'))return `const ${names}={}`
  return `import ${names} from '${specifier==='vue'?import.meta.resolve('vue'):new URL(specifier.endsWith('.js')?specifier:specifier+'.js',url).href}'`
 })
const {default:component}=await import('data:text/javascript;base64,'+Buffer.from(script).toString('base64'))
async function setup(t,mode){
 const browser=installBrowserGlobals({fetch:async url=>Response.json(url==='/api/video-studio'?{items:[]}:url.endsWith('/project-cast')?{characters:[],assets:[]}: {})})
 const mounted=mountComposable(()=>component.setup({accountKey:'owner',suppressAutoAdvance:true},{expose(){},emit(){}}))
 t.after(()=>{try{mounted.unmount()}finally{browser.restore()}})
 await mounted.flush();const panel=mounted.instance
 panel.project.value={id:'preview',revision:1,status:'video_review',settings:{name:'preview'},creation_parameters:{speech_mode:mode,dynamic_auto_advance:false},scenes:[],logs:[],shots:[{id:'shot-1',start:10,duration:4,kind:'video',video_status:'completed',slide_ids:[]}]}
 await mounted.flush()
 const calls={audioPlay:0,audioPause:0,videoPause:0}
 const audio={currentTime:0,paused:true,playbackRate:1,play:async()=>calls.audioPlay++,pause:()=>calls.audioPause++}
 const video={currentTime:5,paused:false,playbackRate:1,pause:()=>calls.videoPause++}
 panel.motionAudio.value=audio
 return {panel,audio,video,calls}
}
for(const mode of ['dialogue','mixed'])test(`${mode} plays full native clip without starting a stale narration snapshot`,async t=>{
 const {panel,video,calls}=await setup(t,mode)
 await panel.syncMotionPreview({currentTarget:video},'play')
 await panel.syncMotionPreview({currentTarget:video},'timeupdate')
 assert.equal(calls.audioPlay,0);assert.equal(calls.videoPause,0)
 assert.ok(calls.audioPause>0)
})
test('narration still aligns source audio and stops at its planned shot boundary',async t=>{
 const {panel,audio,video,calls}=await setup(t,'narration')
 video.currentTime=1
 await panel.syncMotionPreview({currentTarget:video},'play')
 assert.equal(audio.currentTime,11);assert.equal(calls.audioPlay,1)
 video.currentTime=5
 await panel.syncMotionPreview({currentTarget:video},'timeupdate')
 assert.equal(calls.videoPause,1);assert.ok(calls.audioPause>0)
})
