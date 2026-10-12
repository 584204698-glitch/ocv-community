<script setup>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useProjectVoices } from '../useProjectVoices'
import VoiceDesignPanel from './VoiceDesignPanel.vue'
const props = defineProps({ project: { type: Object, required: true }, accountKey: String, disabled: Boolean })
const emit = defineEmits(['busy', 'updated'])
const { characters, busy, loading, error, message, load, bind } = useProjectVoices({
  project: () => props.project, accountKey: () => props.accountKey, disabled: () => props.disabled,
  onUpdated: value => emit('updated', value),
})
const selectedId = ref(''), adopting = ref(false)
const selected = computed(() => characters.value.find(c => c.character_id === selectedId.value))
const totalBusy = computed(() => busy.value || adopting.value)
const externalDialogue=computed(()=>props.project.creation_parameters?.dialogue_route==='external_tts')
watch(totalBusy, value => emit('busy', value), { immediate: true })
watch([() => props.accountKey, () => props.project.id], () => { selectedId.value = ''; adopting.value = false }, { flush: 'sync' })
async function selectVoice(voiceId) {
  const id = selectedId.value
  return await bind(id, voiceId)
}
onBeforeUnmount(() => emit('busy', false))
</script>
<template>
  <section class="project-voice-panel" aria-label="角色与旁白音色" :aria-busy="totalBusy">
    <h3>角色与旁白音色</h3>
    <p v-if="externalDialogue">每位发言者采用一份固定音色，角色对白和旁白由所选配音引擎合成。Index 情绪可在本镜台词中调整；Qwen 克隆沿用参考音色的表达。</p>
    <p v-else>每位说话者可选择固定音色，也可让 H3 按角色特点自行生成声音，无需先设计。自行生成的声音在成片中试听，跨镜可能有变化。</p>
    <p v-if="loading" role="status">正在读取音色绑定…</p>
    <p v-if="error" role="alert">{{ error }} <button :disabled="totalBusy" @click="load">刷新</button></p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="disabled" role="status">当前项目正在处理或有未保存修改，暂不能绑定声音。音色库中的声音会保留，完成当前阶段或暂停后即可采用。</p>
    <p v-if="project.shots?.some(s=>s.dialogue_stale)" role="status">部分镜头仍是旧音色，请在动态镜头中局部重做；其他镜头和角色图继续保留。</p>
    <article v-for="character in characters" :key="character.character_id">
      <strong>{{ character.name }}</strong>
      <span>{{ character.status==='generated'?'H3 自行生成声音':character.status==='current'?'已绑定固定音色':character.status==='stale'?'需要重新核对':'尚未选择声音方式' }}</span>
      <audio v-if="character.audio_url" :src="character.audio_url" controls preload="none" :aria-label="character.name+'的音色'"/>
      <button type="button" :disabled="disabled||totalBusy" @click="selectedId=character.character_id">{{ character.voice_id?'更换音色':'选择或设计音色' }}</button>
      <button v-if="!externalDialogue" type="button" :disabled="disabled||totalBusy||character.status==='generated'" @click="bind(character.character_id,null,'generated')">{{character.status==='generated'?'已选择 H3 自行生成':'不设计，让 H3 自行生成声音'}}</button>
    </article>
    <p v-if="!loading&&!characters.length">角色准备好后，在这里为他们选择声音。</p>
    <div v-if="selected" class="voice-selection">
      <header><strong>为 {{ selected.name }} 选择声音</strong><button :disabled="totalBusy" @click="selectedId=''">收起</button></header>
      <fieldset :disabled="disabled||busy">
        <VoiceDesignPanel :key="accountKey+':'+project.id+':'+selectedId" :voice-id="selected.status==='current'?(selected.voice_id||''):''" :project-id="project.id" :character-id="selectedId" :character-name="selected.name" :bound-voice-ids="characters.map(c=>c.voice_id).filter(Boolean)" :adopt-voice="selectVoice" :external-dialogue="externalDialogue" native-h3 @adopting="adopting=$event" />
      </fieldset>
    </div>
  </section>
</template>
<style scoped>
.project-voice-panel{border:1px solid var(--line,#ffffff18);border-radius:12px;padding:16px;margin:16px 0}.project-voice-panel p,.project-voice-panel span{font-size:13px;color:var(--muted,#b0bbb7)}article{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0}article audio{max-width:100%;height:36px}.voice-selection{margin-top:20px}header{display:flex;justify-content:space-between;gap:12px;margin-bottom:14px}fieldset{border:0;padding:0;min-width:0}[role=alert]{color:#efb1a2!important}
</style>
