<script setup>
import { computed } from 'vue'
import { useStoryWorkspace } from '../useStoryWorkspace'

const props = defineProps({ accountKey: { type: String, default: '' } })
const emit = defineEmits(['adopt'])
const { entries, selectedId, draft, record, presets, loading, busy, error, notice, storageError, dirty, generating, locked, pending, conflict, synopsis, synopsisStale,
  taskMessage, failedWork, directions, directionsCurrent, currentReview, reviewStale, needsRevision, rewriteProposal, hasPendingProposal, proposalCurrent, rewriteSelection, rewriteInstruction, selectionCurrent,
  createStory, openStory, refreshList, refreshCurrent, saveStory, generateStory, findDirections, developDirection, reviewStory, retryPending, selectRewriteRange, selectWholeStory, clearRewriteSelection, rewriteStory, resolveRewrite,
  adoptStory, useServerVersion, keepLocalAsNew,
} = useStoryWorkspace({ owner: () => props.accountKey, onAdopt: value => emit('adopt', value) })
const modes = [
  { id: 'idea', label: '我有一个想法', note: '从一句话、一个场景或结局开始' },
  { id: 'topics', label: '帮我找选题', note: '按频道预设创作一篇新的故事' },
  { id: 'manuscript', label: '我已有文稿', note: '保留原文，保存后采用' },
]
const selectedPreset = computed(() => presets.value.find(item => item.id === draft.value?.preset))
const durationLabels = { 30: '约 30 秒', 60: '约 1 分钟', 90: '约 1 分 30 秒', 120: '约 2 分钟', 180: '约 3 分钟', 300: '约 5 分钟' }
const durationOptions = computed(() => {
  const values = Object.keys(durationLabels).map(Number)
  const current = draft.value?.duration_seconds
  if (Number.isInteger(current) && current >= 15 && current <= 600 && !values.includes(current)) values.push(current)
  return values.sort((a, b) => a - b).map(value => ({ value, label: durationLabels[value] || `约 ${value} 秒` }))
})
const statusLabel = value => ({ draft: '草稿', generating: '处理中', directions_ready: '方向待选择', ready: '待阅读', needs_revision: '有问题待处理', failed: '任务失败', adopted: '已采用', interrupted: '任务中断' })[value] || '本机草稿'
const characterCount = computed(() => Array.from(draft.value?.story || '').length)
const reviewLabel = value => value === 'needs_revision' ? '有问题需要处理' : value === 'ready' ? '未发现需要优先处理的问题' : '检查建议'
const dimensionLabel = value => ({ causality: '因果与动机', motivation: '人物动机', continuity: '前后连贯', ending: '结尾与铺垫', pacing: '节奏', clarity: '表达清晰度', constraints: '创作要求', opening: '开场', originality: '题材与新鲜感', speakability: '朗读表达', producibility: '可制作性' })[value] || value || '故事表达'
function captureSelection(event) { selectRewriteRange(event.target.selectionStart, event.target.selectionEnd) }
function startGeneration() {
  void generateStory()
}
const displayDate = value => {
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
}
</script>

<template>
  <section class="story-workspace content" aria-label="故事创作">
    <header class="page-heading"><div><p class="eyebrow">STORY WORKSPACE</p><h1>先写好故事。</h1><p class="muted">从想法到正文，读过、修改，再采用到视频制作。</p></div><button class="primary" :disabled="!accountKey" @click="createStory()">＋ 新故事</button></header>
    <p v-if="!accountKey" class="story-message">请先登录工作空间，再保存和创作故事。</p>
    <p v-if="storageError" class="story-message error" role="alert">{{ storageError }}</p>
    <div v-if="accountKey" class="story-layout">
      <aside class="story-library" aria-label="故事列表">
        <header><h2>我的故事</h2><button aria-label="刷新故事列表" @click="refreshList">刷新</button></header>
        <button v-for="item in entries" :key="item.id" class="story-entry" :class="{selected:item.id===selectedId}" @click="openStory(item.id)"><strong>{{item.title || '未命名故事'}}</strong><small>{{item.local ? '有本机修改' : statusLabel(item.status)}}</small></button>
        <p v-if="!entries.length" class="muted">从右侧选择一种开始方式。</p>
      </aside>
      <div class="story-editor">
        <p v-if="error" class="story-message error" role="alert">{{ error }}</p>
        <p v-if="notice" class="story-message" role="status">{{ notice }}</p>
        <div v-if="!draft" class="story-welcome"><h2>这次，从哪里开始？</h2><div class="story-start-modes"><button v-for="mode in modes" :key="mode.id" @click="createStory(mode.id)"><strong>{{mode.label}}</strong><small>{{mode.note}}</small></button></div><p class="muted">采用故事后进入制作页，再选择声音、画风与素材。</p></div>
        <template v-else>
          <div class="story-state"><span>{{ loading ? '正在读取…' : generating ? taskMessage : pending ? '正在确认提交结果' : dirty ? '修改已保留在本机，尚未保存到工作空间' : statusLabel(record?.status) }}</span><button :disabled="!!busy || loading" @click="refreshCurrent">检查后台状态</button></div>
          <p v-if="pending" class="story-message">上次操作的提交结果尚未确认。<button :disabled="!!busy || loading || generating || !!conflict" @click="retryPending">重试同一次提交</button></p>
          <div v-if="conflict" class="story-message story-conflict" role="alert"><strong>服务器上已有新版本，本机修改仍保留。</strong><p>可展开比较，然后载入服务器版本，或把本机修改另存为新故事。</p><details><summary>比较服务器版本</summary><h3>{{conflict.title}}</h3><p class="story-text">{{conflict.story}}</p></details><div class="story-actions"><button :disabled="!!busy" @click="useServerVersion">载入服务器版本</button><button :disabled="!!busy" @click="keepLocalAsNew">保留本机稿为新故事</button></div></div>
          <p v-if="record?.task?.error" class="story-message error" role="alert">{{ record.task.error }}</p>
          <details v-if="failedWork" class="story-recovery"><summary>本轮已完成的内容</summary><p class="story-help">以下内容仅供查看，尚未替换当前正文，也不表示检查已经完成。</p><p v-if="failedWork.stages.length" class="story-help">已完成阶段：{{failedWork.stages.join('、')}}</p><ul v-if="failedWork.directions.length"><li v-for="(title,index) in failedWork.directions" :key="index">{{title}}</li></ul><section v-if="failedWork.story"><h3>{{failedWork.title || '已生成的候选正文'}}</h3><p class="story-text story-recovery-text">{{failedWork.story}}</p></section></details>
          <fieldset :disabled="locked" class="story-fields">
            <legend class="sr-only">故事内容与创作要求</legend>
            <div class="story-mode-tabs" aria-label="开始方式"><button v-for="mode in modes" :key="mode.id" type="button" :class="{selected:draft.intent===mode.id}" @click="draft.intent=mode.id">{{mode.label}}</button></div>
            <div class="story-options"><label>频道预设<select v-model="draft.preset"><option v-if="!presets.length" value="general">通用故事</option><option v-for="preset in presets" :key="preset.id" :value="preset.id">{{preset.label}}</option></select></label><label>目标时长<select v-model.number="draft.duration_seconds"><option v-for="duration in durationOptions" :key="duration.value" :value="duration.value">{{duration.label}}</option></select></label></div>
            <p class="story-help">{{ selectedPreset?.description || '按所选频道的叙事偏好创作。' }} 时长是写作目标，成片以实际配音为准。</p>
            <label v-if="draft.intent!=='manuscript'">{{draft.intent==='idea'?'你的想法':'选题偏好（选填）'}}<textarea v-model="draft.idea" rows="3" maxlength="4000" :placeholder="draft.intent==='idea'?'一个场景、一段关系，或你想保留的结局…':'可以留空，或写下想讲的主题、希望避开的内容…'" /></label>
            <p v-else class="story-help">粘贴原文后保存，正文会原样保留，不会自动改写。</p>
            <label>故事标题<input v-model="draft.title" placeholder="可以稍后再起标题" maxlength="80" /></label>
            <label>故事正文<textarea v-model="draft.story" class="story-body" rows="15" maxlength="12000" @select="captureSelection" @mouseup="captureSelection" @keyup="captureSelection" :placeholder="draft.intent==='manuscript'?'在这里粘贴你的完整文稿…':'创作完成后，正文会出现在这里；你也可以直接写。'" /><small class="story-help">{{characterCount}} / 12000 字；保存正文至少需要 20 字。选中一段文字可以提出改写要求。</small></label>
          </fieldset>
          <div class="story-actions"><button :disabled="locked || !dirty" @click="saveStory">{{busy==='save'?'保存中…':'保存故事'}}</button><button v-if="draft.intent!=='manuscript'" :disabled="locked || hasPendingProposal" @click="startGeneration">{{busy==='develop'?'正在提交…':draft.story?'重新创作整篇':'开始创作'}}</button><button v-if="draft.intent!=='manuscript'" :disabled="locked || hasPendingProposal" @click="findDirections">先看三个方向</button><button :disabled="locked || hasPendingProposal || characterCount < 20" @click="reviewStory">{{busy==='review'?'正在提交检查…':'检查当前故事'}}</button><button class="primary" :disabled="locked || hasPendingProposal || characterCount < 20" @click="adoptStory">{{busy==='adopt'?'正在采用…':needsRevision?'仍采用当前稿':'采用并进入视频制作'}}</button></div>
          <p class="story-help">采用后会保存这个正文版本并准备制作草稿。可在下一页配置声音与画面，再启动制作。</p>
          <p v-if="draft.intent!=='manuscript'" class="story-help">绘本直接构思完整故事，其他题材先比较三个方向；也可主动点“先看三个方向”。正文完成后独立检查，最多修改两轮。{{draft.story?'重新创作会生成完整的新稿，旧正文保留在版本记录中。':''}}</p>
          <section v-if="directions.length" class="story-directions" aria-label="故事方向">
            <header><h3>故事方向</h3><p v-if="!directionsCurrent" class="story-help">创作要求已修改或尚未保存；请先保存，再重新获取方向。</p><p v-else class="story-help">点“用这个方向写”沿所选方向创作；再次“开始创作”会重新构思。</p></header>
            <div class="story-direction-grid"><article v-for="direction in directions" :key="direction.id" :class="{selected:direction.id===record.development.plan.selected_direction_id}"><h4>{{direction.title}}</h4><p>{{direction.premise}}</p><details><summary>展开故事走向</summary><dl><template v-for="[key,label] in [['opening','开场'],['action','行动'],['turn','变化'],['ending','结局']]" :key="key"><dt>{{label}}</dt><dd>{{direction[key]}}</dd></template></dl></details><button :disabled="locked || hasPendingProposal || !directionsCurrent || draft.intent==='manuscript'" @click="developDirection(direction.id)">用这个方向写</button></article></div>
            <details v-if="directionsCurrent && record.development.plan.selection_reason" class="story-selection-reason"><summary>方向选择说明</summary><p>{{record.development.plan.selection_reason}}</p></details>
          </section>
          <p v-if="needsRevision" class="story-message story-conflict">检查仍发现需要优先处理的问题，请阅读编辑建议。你也可以自行判断，仍采用当前稿。</p>
          <details v-if="currentReview" class="story-review"><summary>编辑建议 · {{reviewLabel(currentReview.verdict)}}</summary><p class="story-help">这些是针对当前正文的修改建议，供你判断和取舍。</p><article v-for="(issue,index) in currentReview.issues || []" :key="index" class="story-review-issue"><strong>{{issue.severity==='major'?'优先处理':'可改进'}} · {{dimensionLabel(issue.dimension)}}</strong><blockquote v-if="issue.quote">{{issue.quote}}</blockquote><p>{{issue.reason}}</p><p v-if="issue.suggestion"><b>建议：</b>{{issue.suggestion}}</p></article><p v-if="!currentReview.issues?.length" class="story-help">此次检查没有列出具体问题，仍可按你的表达意图继续修改。</p></details>
          <p v-else-if="reviewStale" class="story-help">正文或创作要求已改变，旧编辑建议已失效。可重新检查当前故事。</p>
          <p v-else-if="draft.story" class="story-help">当前故事尚未完成编辑检查。</p>
          <details v-if="draft.story" class="story-rewrite"><summary>改写选中段落</summary>
            <p class="story-help">在正文中选中要改的文字，再写修改要求。先看建议，采用后才会替换正文。</p>
            <div class="story-actions"><button :disabled="locked || hasPendingProposal" @click="selectWholeStory">明确选择全文</button><button :disabled="locked || !rewriteSelection" @click="clearRewriteSelection">清除选区</button></div>
            <section v-if="selectionCurrent" class="story-selection"><strong>{{rewriteSelection.whole?'本次范围：整篇正文':'本次范围：选中的文字'}}</strong><p class="story-text">{{rewriteSelection.original}}</p></section><p v-else class="story-help">尚未选择文字。正文或创作要求变化后，需要重新选择。</p>
            <label>修改要求<textarea v-model="rewriteInstruction" rows="3" maxlength="2000" :disabled="locked || hasPendingProposal" placeholder="例如：把这段结尾写得更自然，保留人物的决定和事实…" /></label>
            <button :disabled="locked || hasPendingProposal || !selectionCurrent || !rewriteInstruction.trim()" @click="rewriteStory">{{busy==='rewrite'?'正在提交改写…':'生成修改建议'}}</button>
          </details>
          <section v-if="rewriteProposal && ['pending','stale'].includes(rewriteProposal.status)" class="story-proposal" aria-label="修改建议预览"><h3>修改建议预览</h3><p class="story-help">正文仍是原稿。{{proposalCurrent?'阅读差异后，可采用或丢弃。手动编辑正文或要求会使这份建议失效。':'正文或创作要求已改变，这份建议已失效，不能采用。'}}</p><p v-if="rewriteProposal.instruction" class="story-help">修改要求：{{rewriteProposal.instruction}}</p><div class="story-proposal-comparison"><section><h4>原选区</h4><p class="story-text">{{rewriteProposal.original}}</p></section><section><h4>建议选区</h4><p class="story-text">{{rewriteProposal.replacement}}</p></section></div>
            <details v-if="rewriteProposal.review"><summary>改稿检查 · {{reviewLabel(rewriteProposal.review.verdict)}}</summary><article v-for="(issue,index) in rewriteProposal.review.issues || []" :key="index" class="story-review-issue"><strong>{{issue.severity==='major'?'优先处理':'可改进'}} · {{dimensionLabel(issue.dimension)}}</strong><blockquote v-if="issue.quote">{{issue.quote}}</blockquote><p>{{issue.reason}}</p><p v-if="issue.suggestion"><b>建议：</b>{{issue.suggestion}}</p></article></details>
            <div class="story-actions"><button class="primary" :disabled="locked || !proposalCurrent" @click="resolveRewrite('accept')">{{busy==='rewrite-accept'?'正在采用…':'采用修改'}}</button><button v-if="rewriteProposal.status==='pending'" :disabled="locked" @click="resolveRewrite('discard')">{{busy==='rewrite-discard'?'正在丢弃…':'丢弃修改'}}</button></div>
          </section>
          <section v-if="synopsis || synopsisStale" class="story-synopsis"><h3>简短梗概</h3><p v-if="synopsis" class="story-text">{{synopsis}}</p><p v-else class="muted">正文已修改，旧梗概已失效。</p></section>
          <details v-if="record?.versions?.length" class="story-versions"><summary>正文版本 · {{record.versions.length}}</summary><article v-for="version in [...record.versions].reverse()" :key="version.story_revision"><h3>{{version.title || '未命名'}} · 版本 {{version.story_revision}}</h3><small>{{displayDate(version.created_at)}}</small><p class="story-text">{{version.story}}</p></article></details>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.story-workspace{max-width:1500px}.story-layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px}.story-library,.story-editor{min-width:0}.story-library{border-right:1px solid var(--border,#35423f);padding-right:20px}.story-library>header,.story-state,.story-actions{display:flex;align-items:center;gap:12px;justify-content:space-between}.story-library h2{font-size:16px}.story-entry{display:flex;flex-direction:column;width:100%;text-align:left;gap:7px;margin:8px 0;padding:13px;overflow-wrap:anywhere}.story-entry strong{font-size:13px}.story-entry small,.story-help,.story-state,.story-versions small{color:var(--muted,#aab8b3);font-size:12px;line-height:1.7}.story-workspace button.selected{border-color:var(--accent,#81d9bd);background:color-mix(in srgb,var(--accent,#81d9bd) 10%,var(--panel,#1e2625))}.story-workspace button:disabled{opacity:.5;cursor:default}.story-fields{border:0;padding:0;margin:12px 0;min-width:0}.story-fields label{display:flex;flex-direction:column;gap:8px;font-size:13px;margin:16px 0}.story-fields input,.story-fields textarea,.story-fields select{box-sizing:border-box;width:100%;padding:11px;border:1px solid var(--border,#35423f);border-radius:8px;background:var(--bg,#141918);color:var(--text,#eef3f1);font:inherit}.story-fields textarea{resize:vertical;line-height:1.85}.story-fields:disabled{opacity:.8}.story-mode-tabs,.story-options,.story-start-modes{display:flex;gap:10px}.story-mode-tabs{flex-wrap:wrap}.story-options>label{flex:1;min-width:0}.story-help{margin:8px 0 16px}.story-body{min-height:300px}.story-actions{justify-content:flex-start;flex-wrap:wrap}.story-message{padding:13px 15px;border-radius:8px;background:color-mix(in srgb,var(--accent,#81d9bd) 9%,var(--panel,#1e2625));font-size:13px;line-height:1.75}.story-message.error{background:color-mix(in srgb,var(--danger,#d66b65) 12%,var(--panel,#1e2625))}.story-conflict{background:color-mix(in srgb,var(--warning,#d8aa4b) 12%,var(--panel,#1e2625))}.story-conflict .story-actions{margin-top:12px}.story-text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.9}.story-synopsis,.story-versions{border-top:1px solid var(--border,#35423f);margin-top:22px;padding-top:16px}.story-synopsis h3,.story-versions h3{font-size:14px}.story-versions summary,.story-conflict summary{cursor:pointer;font-size:13px}.story-versions article{margin-top:20px}.story-versions article+article{border-top:1px solid var(--border,#35423f)}.story-welcome{padding:25px 0}.story-welcome h2{font-size:22px}.story-start-modes{margin:25px 0;flex-wrap:wrap}.story-start-modes button{flex:1;min-width:170px;padding:22px;text-align:left}.story-start-modes strong,.story-start-modes small{display:block}.story-start-modes small{font-size:12px;color:var(--muted,#aab8b3);line-height:1.6;margin-top:10px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.story-workspace .story-entry{white-space:normal}.story-entry strong{width:100%;min-width:0;overflow-wrap:anywhere}
.story-workspace textarea:not(.story-body){min-height:100px}
.story-recovery{padding:14px;border:1px solid var(--border,#35423f);border-radius:8px;margin:14px 0}.story-recovery summary{cursor:pointer;font-size:13px}.story-recovery h3{font-size:14px}.story-recovery li{font-size:13px;line-height:1.8}.story-recovery-text{max-height:320px;overflow:auto}
.story-directions,.story-review,.story-rewrite,.story-proposal{border-top:1px solid var(--border,#35423f);padding-top:18px;margin-top:20px}.story-directions h3,.story-proposal h3{font-size:15px;margin:0 0 10px}.story-direction-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.story-direction-grid article{min-width:0;padding:14px;border:1px solid var(--border,#35423f);border-radius:9px}.story-direction-grid article.selected{border-color:var(--accent,#81d9bd)}.story-direction-grid h4,.story-proposal h4{font-size:14px;margin:0 0 10px}.story-direction-grid p,.story-direction-grid dd,.story-selection-reason p{font-size:13px;line-height:1.7;overflow-wrap:anywhere}.story-direction-grid button{margin-top:12px;white-space:normal}.story-direction-grid dt{font-size:12px;font-weight:600;margin-top:10px}.story-direction-grid dd{margin:4px 0 0}.story-directions summary,.story-review summary,.story-rewrite summary,.story-proposal summary{cursor:pointer;font-size:13px;line-height:1.7}.story-selection-reason{margin-top:14px}.story-review-issue{font-size:13px;line-height:1.8;margin-top:16px}.story-review-issue blockquote{white-space:pre-wrap;border-left:2px solid var(--accent,#81d9bd);margin:8px 0;padding:4px 12px;color:var(--muted,#aab8b3)}.story-review-issue p{margin:7px 0}.story-rewrite label{display:flex;flex-direction:column;gap:8px;font-size:13px;margin:16px 0}.story-rewrite textarea{box-sizing:border-box;width:100%;padding:11px;border:1px solid var(--border,#35423f);border-radius:8px;background:var(--bg,#141918);color:var(--text,#eef3f1);font:inherit;line-height:1.8;resize:vertical}.story-selection{padding:12px 14px;background:var(--panel,#1e2625);border:1px solid var(--border,#35423f);border-radius:8px;margin-top:14px;font-size:13px}.story-selection .story-text{max-height:220px;overflow:auto;margin-bottom:0}.story-proposal-comparison{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.story-proposal-comparison section{min-width:0;padding:14px;background:var(--panel,#1e2625);border-radius:8px}.story-proposal-comparison .story-text{max-height:340px;overflow:auto}.story-proposal>.story-actions{margin-top:16px}
@media(max-width:1100px){.story-direction-grid{grid-template-columns:1fr}}@media(max-width:680px){.story-proposal-comparison{grid-template-columns:1fr}}
@media(max-width:900px){.story-layout{grid-template-columns:1fr}.story-library{border-right:0;border-bottom:1px solid var(--border,#35423f);padding:0 0 15px;max-height:240px;overflow:auto}.story-options,.story-state{flex-wrap:wrap}.story-options>label{min-width:160px}.story-actions button{flex-grow:1}}
</style>
