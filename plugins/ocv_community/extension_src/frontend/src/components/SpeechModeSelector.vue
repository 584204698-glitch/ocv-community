<script setup>
import { computed } from 'vue'
const props = defineProps({ form: { type: Object, required: true }, disabled: Boolean })
const native = computed(() => ['dialogue', 'mixed'].includes(props.form.speech_mode))
</script>
<template>
  <fieldset class="speech-modes" :disabled="disabled">
    <legend>故事怎样讲</legend>
    <label><input v-model="form.speech_mode" type="radio" value="narration">旁白讲述</label>
    <label><input v-model="form.speech_mode" type="radio" value="dialogue">角色对话</label>
    <label><input v-model="form.speech_mode" type="radio" value="mixed">旁白与对话混合</label>
    <template v-if="native">
      <label>角色对白生成路线<select v-model="form.dialogue_route">
        <option value="external_tts">音频驱动与后期配音</option>
        <option value="h3_native">H3 原生对白（可选）</option>
      </select></label>
      <label v-if="form.dialogue_route==='external_tts'">配音引擎<select :value="form.dialogue_tts_engine||'indextts25'" @change="form.dialogue_tts_engine=$event.target.value"><option value="indextts25">IndexTTS2.5</option><option value="qwen3">Qwen3-TTS · 克隆已采用音色</option></select></label>
      <p v-if="form.dialogue_route==='external_tts'">使用已采用的角色音色先合成台词，时间轴按实测音频安排；H3 生成画面与嘴部动作，口型仍需观看确认。混合模式的旁白使用同一配音引擎，不传入口型输入。Qwen 克隆沿用参考声音的表达，不使用 Index 情绪参数。</p>
      <p v-else>H3 使用角色音色参考生成原生对白；台词、说话者与口型需观看核对。</p>
      <p>先设计本故事角色，再为每位发言者选择或设计音色。视频生成时会自动绑定对应的角色图和声音。</p>
      <p>时长和镜头数默认按故事、台词和动作自动安排，无需填写。</p>
      <details class="speech-planning">
        <summary>手动调整时长与镜头数（可选）{{ form.native_duration_seconds || form.native_minimum_shots ? ' · 已设置' : '' }}</summary>
        <label>期望时长（秒）<input v-model.number="form.native_duration_seconds" type="number" min="16" max="600" placeholder="自动"></label>
        <label>至少几个镜头<input v-model.number="form.native_minimum_shots" type="number" min="2" max="40" placeholder="自动"></label>
        <button type="button" @click="form.native_duration_seconds=null;form.native_minimum_shots=null">恢复自动安排</button>
        <small>留空即自动；不会为了凑时长添加剧情。已有分镜需重新规划才会应用。</small>
      </details>
      <p class="muted">对话流程仍在实片验证中。发现台词或画面异常时会暂停，保留素材供检查。</p>
    </template>
  </fieldset>
</template>
<style scoped>
.speech-modes{border:1px solid var(--border,#40504a);border-radius:12px;padding:16px;margin:16px 0;display:flex;gap:16px;flex-wrap:wrap}.speech-modes>label{display:flex;flex-direction:row;gap:6px;align-items:center;width:auto;white-space:nowrap}.speech-modes>label input{width:auto;margin:0}.speech-modes p{flex-basis:100%;margin:0;line-height:1.6}.speech-planning{width:100%}.speech-planning summary{cursor:pointer}.speech-planning label{display:inline-grid;gap:6px;margin:12px 16px 12px 0}.speech-planning input{max-width:160px}.speech-planning small{display:block;margin-top:8px}
</style>
