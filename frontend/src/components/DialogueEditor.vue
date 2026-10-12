<script setup>
import { computed, toRef, watch, onBeforeUnmount } from 'vue'
import { useDialogueEditor } from '../useDialogueEditor'
const props=defineProps({project:Object,shot:Object,accountKey:String,disabled:Boolean})
const emit=defineEmits(['updated','busy','dirty'])
const {turns,dirty,busy,error,conflict,save,reset}=useDialogueEditor({
  project:toRef(props,'project'),shot:toRef(props,'shot'),account:toRef(props,'accountKey'),disabled:toRef(props,'disabled'),
  onUpdated:value=>emit('updated',value),
})
const locked=computed(()=>props.disabled||busy.value)
const externalDialogue=computed(()=>props.project?.creation_parameters?.dialogue_route==='external_tts')
const indexEmotion=computed(()=>externalDialogue.value&&props.project?.creation_parameters?.dialogue_tts_engine!=='qwen3')
const emotions=[['auto','自动'],['neutral','自然'],['happy','开心'],['angry','生气'],['sad','悲伤'],['afraid','害怕'],['disgusted','厌恶'],['melancholic','低落'],['surprised','惊讶'],['calm','平静']]
watch(dirty,value=>emit('dirty',value),{immediate:true,flush:'sync'})
watch(busy,value=>emit('busy',value),{immediate:true,flush:'sync'})
onBeforeUnmount(()=>{emit('dirty',false);emit('busy',false)})
</script>
<template>
  <section class="dialogue-editor">
    <h3>本镜台词 · {{shot.generation_duration}} 秒</h3>
    <p>声音来自各角色采用的音色。修改这里的短句、说话者或时间，只需重新生成本镜视频。</p>
    <fieldset :disabled="locked">
      <div v-for="(turn,index) in turns" :key="index" class="dialogue-turn">
        <label>说话者<select v-model="turn.character_id"><option v-for="speaker in shot.native_dialogue.speakers" :key="speaker.character_id" :value="speaker.character_id">{{speaker.name}}</option></select></label>
        <label class="dialogue-words">台词<input v-model="turn.text" maxlength="100"></label>
        <label>开始（秒）<input v-model.number="turn.start" type="number" min="0" :max="shot.generation_duration" step="0.1"></label>
        <label>结束（秒）<input v-model.number="turn.end" type="number" min="0.1" :max="shot.generation_duration" step="0.1"></label>
        <label v-if="indexEmotion">声音情绪<select :value="turn.emotion||'auto'" @change="turn.emotion=$event.target.value"><option v-for="[value,label] in emotions" :key="value" :value="value">{{label}}</option></select></label>
        <label v-if="indexEmotion">情绪强度<input :value="turn.emotion_strength??0.35" type="number" min="0" max="1" step="0.05" :disabled="turn.emotion==='neutral'" @input="turn.emotion_strength=$event.target.value===''?0.35:Number($event.target.value)"></label>
        <button type="button" :disabled="turns.length<=1" @click="turns.splice(index,1)">删除这句</button>
      </div>
      <button type="button" :disabled="turns.length>=6" @click="turns.push({character_id:shot.native_dialogue.speakers[0].character_id,text:'',start:turns.at(-1)?.end||0,end:shot.generation_duration,delivery:'naturally and clearly'})">添加一句</button>
    </fieldset>
    <p v-if="indexEmotion">自动情绪沿用本句表演要求，无明确情绪时保持自然声线。默认强度 0.35；修改后重新生成本镜，配音实际时长会参与安排画面时序。</p>
    <p v-else-if="externalDialogue">Qwen 克隆沿用已采用参考音色的表达；原 Index 情绪设置保留，切回 Index 后生效。口型时序使用实测音频。</p>
    <p v-if="conflict" role="alert">另一页面已修改本镜台词。当前草稿保留，请复制需要的内容后重新读取。</p>
    <p v-if="error" role="alert">{{error}}</p>
    <div><button type="button" :disabled="locked||!dirty||conflict" @click="save">{{busy?'保存中…':'保存本镜台词'}}</button> <button type="button" :disabled="locked||(!dirty&&!conflict)" @click="reset">放弃修改并重新读取</button></div>
  </section>
</template>
<style scoped>
.dialogue-editor{border:1px solid var(--border,#40504a);border-radius:12px;padding:16px;margin:12px 0}.dialogue-editor p{line-height:1.6}.dialogue-editor fieldset{border:0;padding:0;margin:12px 0}.dialogue-turn{display:flex;flex-wrap:wrap;align-items:end;gap:10px;margin-bottom:12px}.dialogue-turn label{display:grid;gap:5px}.dialogue-turn input[type=number]{width:95px}.dialogue-words{flex:1;min-width:180px}[role=alert]{color:#efb1a2}
</style>
