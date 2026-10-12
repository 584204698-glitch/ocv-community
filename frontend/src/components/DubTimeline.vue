<script setup>
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {requestJSON} from '../api'
const props=defineProps({projectId:String,shot:Object,revision:Number,disabled:Boolean})
const emit=defineEmits(['updated','busy-change'])
const open=ref(false),loading=ref(false),saving=ref(false),error=ref(''),timeline=ref(null),starts=ref([]),player=ref(null),playhead=ref(0)
let epoch=0,drag=null,frame=0,audios=[]
const base=computed(()=>'/api/video-studio/'+encodeURIComponent(props.projectId)+'/videos/'+encodeURIComponent(props.shot.id))
const rawUrl=computed(()=>base.value+'?original=true&v='+encodeURIComponent(props.shot.video_version||props.revision))
const changed=computed(()=>timeline.value&&starts.value.some((s,i)=>Math.abs(s-timeline.value.clips[i].start)>.001))
const overlap=computed(()=>timeline.value&&starts.value.some((s,i)=>starts.value.some((t,j)=>j>i&&s<t+timeline.value.clips[j].duration&&t<s+timeline.value.clips[i].duration)))
function stopAudio(){cancelAnimationFrame(frame);frame=0;audios.forEach(a=>a.pause())}
function stop(){player.value?.pause();stopAudio()}
function dispose(){stop();audios.forEach(a=>{a.removeAttribute('src');a.load()});audios=[]}
function scope(){return props.projectId+'|'+props.shot.id+'|'+(props.shot.video_version||'')+'|'+(props.shot.video_request?.external_dialogue?.key||'')}
watch(scope,()=>{epoch++;dispose();open.value=false;loading.value=false;timeline.value=null;error.value='';starts.value=[];drag=null})
onBeforeUnmount(()=>{epoch++;dispose()})
async function load(){
 if(props.disabled||saving.value||loading.value)return
 if(open.value){stop();open.value=false;return}
 const token=++epoch;loading.value=true;error.value=''
 try{
  const result=await requestJSON(base.value+'/dub-timeline')
  if(token!==epoch)return
  dispose();timeline.value=result;starts.value=result.clips.map(c=>c.start)
  audios=result.clips.map(c=>{const a=new Audio(base.value+'/dub-clips/'+c.index+'?v='+result.basis);a.preload='auto';a.volume=c.kind==='narrator'?.65:1;return a})
  open.value=true
 }catch(e){if(token===epoch)error.value=e.message}
 finally{if(token===epoch)loading.value=false}
}
function setStart(i,value){
 if(saving.value||props.disabled||!timeline.value)return
 const n=Number(value);if(!Number.isFinite(n))return
 starts.value[i]=Math.round(Math.max(0,Math.min(timeline.value.duration-timeline.value.clips[i].duration,n))*1000)/1000
}
function beginDrag(event,i){
 if(saving.value||props.disabled||event.button!==0)return
 stop();const rail=event.currentTarget.parentElement.getBoundingClientRect()
 drag={i,x:event.clientX,start:starts.value[i],width:rail.width}
 event.currentTarget.setPointerCapture(event.pointerId);event.preventDefault()
}
function moveDrag(event){if(drag)setStart(drag.i,drag.start+(event.clientX-drag.x)/drag.width*timeline.value.duration)}
function endDrag(){drag=null}
function keyMove(event,i){
 if(!['ArrowLeft','ArrowRight'].includes(event.key))return
 event.preventDefault();stop();setStart(i,starts.value[i]+(event.key==='ArrowLeft'?-1:1)*(event.shiftKey?.25:.05))
}
function sync(){
 const video=player.value;if(!video||!timeline.value)return
 const t=video.currentTime;playhead.value=t
 audios.forEach((audio,i)=>{
  const local=t-starts.value[i],duration=timeline.value.clips[i].duration
  if(video.paused||local<0||local>=duration){audio.pause();return}
  if(Math.abs(audio.currentTime-local)>.12)audio.currentTime=local
  audio.playbackRate=video.playbackRate
  if(audio.paused)audio.play().catch(()=>{})
 })
}
function tick(){sync();if(player.value&&!player.value.paused)frame=requestAnimationFrame(tick)}
function play(){stopAudio();tick()}
function seek(event){stop();if(player.value){player.value.currentTime=Number(event.target.value);playhead.value=player.value.currentTime}}
async function save(reset=false){
 if(props.disabled||saving.value||!timeline.value)return
 stop();saving.value=true;emit('busy-change',true);error.value='';const token=epoch
 try{
  const result=await requestJSON(base.value+'/dub-timeline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:props.revision,basis:timeline.value.basis,starts:starts.value,reset})})
  if(token!==epoch)return
  emit('updated',result)
  timeline.value.clips.forEach((c,i)=>{c.start=reset?c.original_start:starts.value[i]})
  starts.value=timeline.value.clips.map(c=>c.start);timeline.value.manual=!reset
 }catch(e){if(token===epoch)error.value=e.message}
 finally{saving.value=false;emit('busy-change',false)}
}
</script>

<template>
 <section class="dub-timeline">
  <button type="button" :disabled="disabled||saving||loading" @click="load">{{loading?'正在读取配音…':open?'收起音轨调整':'拖动调整本镜配音'}}</button>
  <p v-if="error" role="alert">{{error}}</p>
  <div v-if="open&&timeline">
   <p>每条是已有的一句配音。左右拖动调整开始位置，也可输入秒数；声音长度保持不变。下方播放用于试听调整，采用后更新主预览和成片时序。</p>
   <p class="muted">对照原始画面，试听只播放这些配音。声音早晚可调整；说错角色的嘴部动作无法靠移动声音修复。</p>
   <video ref="player" controls muted playsinline preload="metadata" :src="rawUrl" @play="play" @pause="stopAudio" @ended="stopAudio" @seeking="sync" @ratechange="sync" />
   <label class="scrub">定位画面 <input aria-label="音轨调整画面时间" type="range" min="0" :max="timeline.duration" step="0.01" :value="playhead" @input="seek">{{playhead.toFixed(2)}} / {{timeline.duration.toFixed(2)}} 秒</label>
   <div class="dub-ruler"><span>0 秒</span><span>{{(timeline.duration/2).toFixed(2)}} 秒</span><span>{{timeline.duration.toFixed(2)}} 秒</span></div>
   <div v-for="(clip,i) in timeline.clips" :key="clip.index" class="dub-row">
    <label>{{clip.name}} · {{clip.text}} <input :aria-label="clip.name+'配音开始秒数'" type="number" step="0.05" min="0" :max="Math.max(0,timeline.duration-clip.duration)" :value="starts[i]" :disabled="disabled||saving" @input="setStart(i,$event.target.value)"> 秒 · 长 {{clip.duration.toFixed(2)}} 秒</label>
    <div class="dub-rail">
     <span class="dub-playhead" :style="{left:playhead/timeline.duration*100+'%'}" />
     <button type="button" class="dub-strip" :class="{narrator:clip.kind==='narrator'}" :style="{left:starts[i]/timeline.duration*100+'%',width:clip.duration/timeline.duration*100+'%'}" :disabled="disabled||saving" :aria-label="'拖动'+clip.name+'配音：'+clip.text" @pointerdown="beginDrag($event,i)" @pointermove="moveDrag" @pointerup="endDrag" @pointercancel="endDrag" @keydown="keyMove($event,i)">{{clip.text}}</button>
    </div>
   </div>
   <p v-if="overlap" role="status">有配音时间重叠，试听时会同时播放，请确认是否符合你的安排。</p>
   <div class="dub-actions">
    <button type="button" :disabled="disabled||saving||!changed" @click="save(false)">{{saving?'正在合成…':'采用位置并重新合成'}}</button>
    <button type="button" :disabled="disabled||saving" @click="stop();starts=timeline.clips.map(c=>c.start)">撤回未采用的拖动</button>
    <button v-if="timeline.manual" type="button" :disabled="disabled||saving" @click="save(true)">恢复自动时序并合成</button>
   </div>
   <p class="muted">只合成已有视频和声音，不调用语言模型、TTS 或 H3。手动时序不会自动标为口型通过。</p>
  </div>
 </section>
</template>

<style scoped>
.dub-timeline{grid-column:1/-1;min-width:0;margin:16px 0;padding:14px;border:1px solid var(--border,#48534f);border-radius:10px}.dub-timeline p{line-height:1.65;font-size:13px}.dub-timeline video{width:100%;max-height:46vh;background:#111}.scrub{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px}.scrub input{flex:1;min-width:120px}.dub-ruler{display:flex;justify-content:space-between;font-size:12px;margin:14px 0 4px}.dub-row{margin:12px 0}.dub-row label{display:block;font-size:13px;margin-bottom:6px}.dub-row input{width:80px;padding:5px}.dub-rail{position:relative;height:44px;background:repeating-linear-gradient(90deg,transparent,transparent calc(10% - 1px),#66756d40 calc(10% - 1px),#66756d40 10%),var(--bg,#161c19);border:1px solid var(--border,#48534f);border-radius:6px}.dub-strip{position:absolute;top:4px;height:34px;min-height:0!important;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:4px 6px;cursor:ew-resize;touch-action:none;background:#346454;color:#fff;border:1px solid #81bba3;border-radius:5px}.dub-strip.narrator{background:#555077;border-color:#aaa0d5}.dub-strip:focus-visible{outline:2px solid #fff}.dub-playhead{position:absolute;top:0;bottom:0;width:2px;background:#e9b65f;z-index:2;pointer-events:none}.dub-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
</style>
