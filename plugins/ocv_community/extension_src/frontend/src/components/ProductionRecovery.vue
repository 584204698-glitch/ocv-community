<script setup>
defineProps({recovery:Object,disabled:Boolean})
defineEmits(['action'])
</script>
<template>
 <section v-if="recovery" class="production-recovery" aria-label="生产问题处理" role="status">
  <strong>{{recovery.title}}</strong>
  <p>{{recovery.help}}</p>
  <p v-if="disabled">当前操作尚未结束。请等待，或先暂停正在进行的一键制作，再处理此问题。</p>
  <div><button v-for="action in recovery.actions" :key="action.id" type="button" :disabled="disabled" @click="$emit('action',action.id)">{{action.label}}</button></div>
  <details><summary>查看具体原因</summary><pre>{{recovery.message}}</pre></details>
 </section>
</template>
<style scoped>
.production-recovery{border:1px solid #b89b62;border-radius:10px;padding:16px;margin:12px 0}.production-recovery p{line-height:1.7}.production-recovery div{display:flex;flex-wrap:wrap;gap:8px}.production-recovery pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.production-recovery details{margin-top:12px}
</style>
