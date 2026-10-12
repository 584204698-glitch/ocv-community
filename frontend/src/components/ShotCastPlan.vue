<script setup>
import { computed } from 'vue'
const props = defineProps({ plan: Object, projectId: String, characters: { type: Array, default: () => [] } })
const valid = computed(() => props.plan?.version === 1 && props.plan.source === 'director_plan'
  && props.plan.project_id === props.projectId && Array.isArray(props.plan.characters))
const rows = computed(() => !valid.value ? [] : props.plan.characters.map(row => {
  const character = props.characters.find(item => item.id === row.project_character_id)
  return { ...row, name: character?.name || '待核对的角色',
    current: character?.status === 'current' && character.identity_sha256 === row.identity_sha256 }
}))
</script>

<template>
  <details v-if="valid" class="shot-cast-plan" open>
    <summary>本镜角色计划（只读）</summary>
    <p>分镜导演的出场安排，实际图片仍待确认。</p>
    <ul v-if="rows.length"><li v-for="row in rows" :key="row.project_character_id"><strong>{{ row.name }}</strong> · {{ row.presence === 'opening' ? '计划开场' : row.presence === 'later' ? '计划后入场' : '出场计划待核对' }}<span v-if="!row.current" class="cast-plan-warning"> · 角色资料已变化或待核对，请核对后重新规划</span><small>{{ row.basis }}</small></li></ul>
    <p v-else>本镜计划未安排固定角色入画；仍需对照实际图片核对。</p>
  </details>
</template>

<style scoped>
.shot-cast-plan{margin:0 0 18px;padding:13px 16px;border:1px solid var(--ocv-line,var(--line,#ffffff12));border-radius:12px;background:var(--ocv-panel,var(--panel,#202526));font-size:13px}.shot-cast-plan summary{cursor:pointer;font-weight:600}.shot-cast-plan p,.shot-cast-plan small{font-size:12px;line-height:1.7;color:var(--ocv-muted,var(--muted,#a0aaa8))}.shot-cast-plan ul{padding-left:20px;margin-bottom:0}.shot-cast-plan li{margin-top:8px;overflow-wrap:anywhere}.shot-cast-plan small{display:block}.cast-plan-warning{color:var(--warning,#d6bc87);font-size:12px}
</style>
