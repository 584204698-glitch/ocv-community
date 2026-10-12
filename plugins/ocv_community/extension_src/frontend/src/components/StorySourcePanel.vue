<script setup>
import { computed } from 'vue'

const props = defineProps({ handoff: { type: Object, default: null } })
const labels = { current: '已采用', detached: '独立文稿', legacy_body_only: '仅采用正文' }
const visible = computed(() => Boolean(props.handoff && labels[props.handoff.status]))
const requirements = computed(() => props.handoff?.status === 'current' ? String(props.handoff.brief?.idea || '') : '')
</script>

<template>
  <details v-if="visible" class="story-source-panel" :class="{ detached: handoff.status === 'detached' }">
    <summary>
      <strong>故事来源</strong>
      <span class="story-source-title">{{ handoff.title || '已采用的故事' }}</span>
      <span v-if="handoff.source?.story_revision" class="story-source-version">第 {{ handoff.source.story_revision }} 版</span>
      <span class="story-source-status">{{ labels[handoff.status] }}</span>
    </summary>
    <div class="story-source-body">
      <p>{{ handoff.message || (handoff.status === 'detached' ? '当前文稿已与采用时的故事不同，将作为独立文稿制作。' : handoff.status === 'legacy_body_only' ? '历史采用记录只保留正文，未保存当时的创作要求。' : '制作使用此故事版本及采用时保存的创作要求。') }}</p>
      <template v-if="requirements">
        <h3>采用时的创作要求 · 只读</h3>
        <p class="story-source-requirements">{{ requirements }}</p>
      </template>
    </div>
  </details>
</template>

<style scoped>
.story-source-panel{margin:0 0 18px;padding:0;border:1px solid var(--ocv-line,var(--line,#ffffff12));border-radius:12px;background:var(--ocv-panel,var(--panel,#202526));color:var(--ocv-text,var(--text,#e7e9eb));font-size:13px}
.story-source-panel summary{padding:13px 16px;cursor:pointer;line-height:1.7;overflow-wrap:anywhere}
.story-source-panel summary>*{margin-right:10px}.story-source-title{font-weight:500}.story-source-version{color:var(--ocv-muted,var(--muted,#a0aaa8))}
.story-source-status{display:inline-block;padding:1px 8px;border-radius:6px;background:color-mix(in srgb,var(--ocv-accent,var(--accent,#83dec5)) 12%,transparent);color:var(--ocv-accent,var(--accent,#83dec5));font-size:12px}.detached .story-source-status{background:color-mix(in srgb,var(--warning,#d6bc87) 12%,transparent);color:var(--warning,#d6bc87)}
.story-source-body{padding:0 16px 14px}.story-source-body p{margin:0;line-height:1.8;overflow-wrap:anywhere}.story-source-body h3{margin:12px 0 5px;font-size:13px}.story-source-requirements{white-space:pre-wrap;max-height:240px;overflow-y:auto}
@media(max-width:680px){.story-source-panel summary{padding:11px 12px}.story-source-body{padding:0 12px 12px}}
</style>
