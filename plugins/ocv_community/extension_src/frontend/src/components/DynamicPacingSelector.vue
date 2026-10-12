<script setup>
import { dynamicPacingOptions, dynamicPacingSummary, selectDynamicPacing, restoreDynamicPacing, normalizeDynamicCustom } from '../dynamicPacing.js'
const props=defineProps({form:{type:Object,required:true},disabled:Boolean})
const emit=defineEmits(['change'])
function select(key){selectDynamicPacing(props.form,key);emit('change')}
function custom(){normalizeDynamicCustom(props.form);emit('change')}
function restore(){restoreDynamicPacing(props.form);emit('change')}
</script>

<template>
 <section class="dynamic-pacing-selector" aria-label="镜头节奏">
  <div class="pacing-heading"><strong>镜头节奏</strong><span>{{dynamicPacingSummary(form)}}</span></div>
  <div class="pacing-options" role="group" aria-label="选择镜头节奏">
   <button v-for="option in dynamicPacingOptions" :key="option.key" type="button" :disabled="disabled" :aria-pressed="form.visual_pacing_preset===option.key" :class="{selected:form.visual_pacing_preset===option.key}" @click="select(option.key)"><b>{{option.label}}</b><small>{{option.detail}}</small></button>
  </div>
  <button v-if="form.visual_pacing_preset==='auto' && form._previous_pacing" type="button" :disabled="disabled" @click="restore">撤回自动，恢复上次节奏</button>
  <div v-if="form.visual_pacing_preset==='custom'" class="pacing-custom">
   <label>尽量不少于（秒）<input v-model.number="form.visual_min_duration" :disabled="disabled" type="number" min="4" max="15" step="0.5" @change="custom" /></label>
   <label>目标时长（秒）<input v-model.number="form.visual_target_duration" :disabled="disabled" type="number" min="5" max="15" step="0.5" @change="custom" /></label>
   <label>最长时长（秒）<input v-model.number="form.visual_max_duration" :disabled="disabled" type="number" min="6" max="15" step="0.5" @change="custom" /></label>
   <label>每镜最多字幕片段<input v-model.number="form.visual_max_slides" :disabled="disabled" type="number" min="1" max="12" step="1" @change="custom" /></label>
  </div>
  <p>不确定就选“自动”；想让画面变化更多可选“紧凑”。随时可切回其他节奏。镜头越多，制作时间和用量通常越高。</p>
  <small>按动作和台词自然切镜，保持完整表达；已有分镜不会自动重做。</small>
 </section>
</template>

<style scoped>
.dynamic-pacing-selector{margin:16px 0;padding:16px;border:1px solid var(--border,#dce1e8);border-radius:14px;background:var(--panel,#fff);color:var(--text,#253044)}
.pacing-heading{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;margin-bottom:12px}.pacing-heading span{font-size:13px;color:var(--muted,#667085)}
.pacing-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.pacing-options button{display:flex;flex-direction:column;gap:5px;align-items:flex-start;text-align:left;white-space:normal;min-height:64px;padding:10px 12px;border:1px solid #dce1e8;border-radius:10px;background:transparent;color:inherit;cursor:pointer}.pacing-options button.selected{border-color:#5264d9;background:#eef0ff;color:#27388f}.pacing-options small{font-size:12px;line-height:1.5}.dynamic-pacing-selector p{font-size:13px;line-height:1.6;margin:12px 0 4px}.dynamic-pacing-selector>small{display:block;font-size:12px;line-height:1.6;color:var(--muted,#667085)}
.pacing-custom{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}.pacing-custom label{display:flex;flex-direction:column;gap:6px;font-size:13px}.pacing-custom input{width:100%;min-width:0;box-sizing:border-box;min-height:44px;font-size:16px;border:1px solid #dce1e8;border-radius:8px;padding:8px;background:transparent;color:inherit}
@media(max-width:620px){.pacing-options{grid-template-columns:repeat(2,minmax(0,1fr))}.dynamic-pacing-selector{padding:12px}.pacing-custom{grid-template-columns:1fr}}
</style>
