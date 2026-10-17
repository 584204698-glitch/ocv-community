<script setup>
import {ref, onMounted, onUnmounted} from 'vue'
import {requestJSON} from '../api'
const props=defineProps({endpoint:{type:String,required:true}})
const usage=ref(null),error=ref('')
let disposed=false,pending=false,timer
const labels={story_writing:'写稿',story_reviewing:'文案审阅',story_revising:'文案修订',story_planning:'方向构思',story_selecting:'方向选择',storyboard_planning:'分镜规划',storyboard_director:'分镜导演',storyboard_understanding:'全文理解',storyboard_grouping:'镜头分组',storyboard_core_design:'核心画面设计',storyboard_motion_design:'动作设计',storyboard_image_prompt:'图像提示词',storyboard_video_prompt:'视频提示词',story_review:'故事表达审核',renderability_review:'可生成性审核',image_preparation:'图片与场景准备',scene_review:'场景审核',core_image_review:'核心图审核',opening_preflight:'开场预检',video_preparation:'视频准备',h3_prompt_conversion:'H3提示词转换',video_review:'视频画面审核',other:'其他'}
function seconds(value){return `${Number(value||0).toFixed(1)} 秒`}
async function refresh(){
 if(disposed||pending)return
 pending=true
 try{const value=await requestJSON(props.endpoint);if(!disposed){usage.value=value;error.value=''}}
 catch{if(!disposed)error.value='调用统计暂时未能读取，稍后自动重连。'}
 finally{pending=false}
}
onMounted(()=>{refresh();timer=setInterval(refresh,5000)})
onUnmounted(()=>{disposed=true;clearInterval(timer)})
</script>
<template>
 <details class="production-usage" aria-label="模型调用与耗时">
  <summary>模型调用与耗时<span v-if="usage"> · {{usage.requests}} 次请求<span v-if="usage.running">，{{usage.running}} 次未见结束记录</span> · 复用 {{usage.cache_hits}} 次</span></summary>
  <p v-if="error" role="status">{{error}}</p>
  <template v-if="usage">
   <p>完成 {{usage.completed}} 次，失败 {{usage.failed}} 次；累计模型请求耗时 {{seconds(usage.elapsed_seconds)}}。并行时长会重叠，不等于整片制作时间。</p>
   <div class="usage-scroll"><table v-if="usage.stages.length"><thead><tr><th>阶段</th><th>请求</th><th>失败</th><th>复用</th><th>累计耗时</th><th>模型</th></tr></thead><tbody><tr v-for="stage in usage.stages" :key="stage.stage"><td>{{labels[stage.stage]||'其他'}}</td><td>{{stage.requests}}</td><td>{{stage.failed}}</td><td>{{stage.cache_hits}}</td><td>{{seconds(stage.elapsed_seconds)}}</td><td>{{stage.models.join('、')||'—'}}</td></tr></tbody></table></div>
   <p>供应商返回用量 {{usage.token_reports}} / {{usage.completed+usage.failed}} 次；已报告输入 {{usage.input_tokens}}、输出 {{usage.output_tokens}} token。实际费用未知。</p>
   <p>{{usage.note}} 请求次数包含格式修复；HTTP发送尝试 {{usage.http_attempts}} 次，可能含接口原有重试。</p>
   <p v-if="usage.history_truncated">记录较多，当前只显示最近部分统计。</p>
  </template>
 </details>
</template>
<style scoped>
.production-usage{border:1px solid var(--border,#40504a);border-radius:8px;padding:10px 14px;margin:12px 0;font-size:13px}.production-usage summary{cursor:pointer}.production-usage p{line-height:1.6;color:var(--muted,#9aaba5)}.usage-scroll{overflow:auto}.production-usage table{width:100%;border-collapse:collapse;text-align:left}.production-usage th,.production-usage td{padding:6px;border-bottom:1px solid var(--border,#40504a)}
</style>
