<script setup>
import { computed } from 'vue'
import { nativePromptReview } from '../nativePromptReview.js'
const props = defineProps({shot:Object})
const review = computed(() => nativePromptReview(props.shot))
</script>
<template>
  <section v-if="review" class="native-prompt-review">
    <h4>{{review.frozen ? '本次制作指令 · 中文核对' : '制作指令预览 · 中文核对'}}</h4>
    <p class="muted">{{review.frozen ? '以下台词、角色和时间来自本次提交记录。' : '以下为当前分镜，修改并保存后会用于下一次生成。'}}这是制作要求，视频实际表现仍需观看核对。</p>
    <p v-if="review.action"><strong>镜头动作：</strong>{{review.action}}</p>
    <p v-else class="muted">这份记录没有保存中文动作说明，可展开原始提交查看；不会用后来修改的动作替代旧记录。</p>
    <p v-if="review.stale" role="alert">动作修改尚未同步，请先按动态表达更新提示词。</p>
    <p v-if="review.openingObservation"><strong>提交前开场图核对：</strong>{{review.openingObservation}}<span class="muted"> 此项是开场图观察，不代表生成的视频已通过验收。</span></p>
    <div v-if="review.beats.length"><strong>动作与台词时间线</strong>
      <ol><li v-for="(beat,index) in review.beats" :key="index"><strong>{{beat.start}}–{{beat.end}} 秒：</strong>{{beat.action}}</li></ol>
    </div>
    <p v-if="review.silent">本镜没有台词，角色通过表情和动作表现，仅保留环境声与动作声。</p>
    <ol v-else>
      <li v-for="(turn,index) in review.turns" :key="index">
        <strong>{{turn.start}}–{{turn.end}} 秒 · {{turn.name}}（{{turn.source}}{{review.externalTts ? ' · 后期配音' : ''}}）</strong>
        <p>“{{turn.text}}”</p>
        <p class="muted">{{!review.frozen ? '提交时使用本项目为该角色采用的固定音色。' : turn.voiceBound ? '使用该角色已绑定的固定参考音色。' : '本次记录未绑定参考音色。'}}
          <template v-if="turn.listeners.length">{{turn.listeners.join('、')}}在此时段闭嘴倾听；具体表情和姿势请对照本次动作时间线。</template>
          <template v-if="turn.source!=='画面内角色'">这段声音不对应画面中的说话口型。</template>
          <template v-else-if="turn.mouthHidden">本句角色嘴部被遮挡，保持遮挡发声，不让听者接嘴。</template>
        </p>
      </li>
    </ol>
    <p v-if="!review.silent && review.afterSpeech < review.duration">{{review.afterSpeech}}–{{review.duration}} 秒：台词已结束，继续已规划的动作，不追加对话{{review.externalTts ? '。本次配音预览不混入 H3 原声。' : '；保留环境声和动作声。'}}</p>
    <details><summary>H3 原始提交详情{{review.frozen ? '（只读）' : '（提交后可查看）'}}</summary>
      <p v-if="shot.video_request?.prompt" class="raw-prompt">{{shot.video_request.prompt}}</p>
      <p v-else>提交时会按 H3 格式组织角色图、音色、动作及逐字台词。这里展示中文核对内容，不需要手写英文。</p>
    </details>
  </section>
</template>
<style scoped>
.native-prompt-review{border:1px solid var(--border,#40504a);border-radius:10px;padding:16px;margin:12px 0;line-height:1.65}.native-prompt-review h4{margin:0 0 8px}.native-prompt-review ol{padding-left:22px}.native-prompt-review li{margin:12px 0}.native-prompt-review li p{margin:4px 0}.raw-prompt{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer}[role=alert]{color:#efb1a2}
</style>
