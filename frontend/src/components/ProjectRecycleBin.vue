<script setup>
import {ref} from 'vue'
import ProjectArchiveButton from './ProjectArchiveButton.vue'
defineProps({projects:{type:Array,default:()=>[]}})
defineEmits(['changed'])
const open=ref(false)
</script>
<template><section v-if="projects.some(p=>p.archived)" class="project-recycle-bin"><button type="button" @click="open=!open" :aria-expanded="open">回收站（{{projects.filter(p=>p.archived).length}}）</button><div v-if="open"><article v-for="project in projects.filter(p=>p.archived)" :key="project.id"><span>{{project.settings?.name||project.id}}</span><ProjectArchiveButton :project="project" @changed="$emit('changed')" /></article></div></section></template>
<style scoped>
.project-recycle-bin{margin:20px 0}.project-recycle-bin>button{min-height:44px;padding:8px 14px;color:inherit;background:transparent;border:1px solid #aeb6c6;border-radius:8px;cursor:pointer}.project-recycle-bin article{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #758090}.project-recycle-bin span{overflow-wrap:anywhere}
</style>
