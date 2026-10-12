<script setup>
import { computed, ref } from 'vue'
import { matchingVisualStyle, visualStyleCatalog, visualStyleCategories } from '../visualStyleCatalog'

const props = defineProps({
  modelValue: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'change'])
const selectedStyle = computed(() => matchingVisualStyle(props.modelValue))
const hasPrompt = computed(() => typeof props.modelValue === 'string' && Boolean(props.modelValue.trim()))
const currentLabel = computed(() => selectedStyle.value?.label || (hasPrompt.value ? '自定义画风' : '尚未指定画风'))
const category = ref('全部')
const search = ref('')
const visibleStyles = computed(() => {
  const query = search.value.trim().toLowerCase()
  return visualStyleCatalog.filter(style => (category.value === '全部' || style.category === category.value)
    && (!query || [style.label, style.description, style.category].join(' ').toLowerCase().includes(query)))
})

function chooseStyle(id) {
  if (props.disabled) return
  const style = visualStyleCatalog.find(item => item.id === id)
  if (!style) return
  emit('update:modelValue', style.prompt)
  emit('change', style.prompt)
}
</script>

<template>
  <section class="visual-style-gallery" aria-label="画风示例">
    <header>
      <h3>看看画风 · {{ visualStyleCatalog.length }} 种</h3>
      <span class="current-style" role="status">当前：{{ currentLabel }}</span>
    </header>
    <p class="gallery-note">预览帮助比较画风，示例角色不会带入你的故事。还可以在下方自由描述其他画风。</p>
    <label class="style-search"><span>搜索画风</span><input v-model="search" type="search" placeholder="例如：绘本、水墨、动画" /></label>
    <div class="style-categories" aria-label="画风分类">
      <button v-for="item in ['全部', ...visualStyleCategories]" :key="item" type="button" :aria-pressed="category === item" :class="{active:category===item}" @click="category=item">{{item}}</button>
    </div>
    <p class="gallery-note" role="status">找到 {{visibleStyles.length}} 种画风</p>
    <div class="visual-style-grid">
      <button v-for="style in visibleStyles" :key="style.id" class="visual-style-card" type="button"
        :class="{ selected: selectedStyle?.id === style.id }" :aria-pressed="selectedStyle?.id === style.id"
        :disabled="disabled" @click="chooseStyle(style.id)">
        <span class="style-preview-image">
          <img :src="style.preview" :alt="style.alt" width="640" height="640" loading="lazy" decoding="async" />
          <span class="style-preview-badge">风格示例</span>
        </span>
        <span class="style-card-copy">
          <span class="style-card-title"><strong>{{ style.label }}</strong><span v-if="selectedStyle?.id === style.id">已选</span></span>
          <small>{{ style.description }}</small>
        </span>
      </button>
    </div>
    <p v-if="!visibleStyles.length" class="gallery-note">没有匹配的画风，换个关键词，或在下方填写自定义描述。</p>
    <p class="gallery-note">选择后可继续修改下方画风描述；已有角色图和镜头不会自动重绘。</p>
    <p v-if="hasPrompt && !selectedStyle" class="custom-style-note">当前使用自定义画风，示例图仅供比较。保留当前描述即可继续制作。</p>
  </section>
</template>

<style scoped>
.visual-style-gallery{display:grid;gap:12px;margin:18px 0 24px;min-width:0}
.visual-style-gallery header{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.visual-style-gallery h3{margin:0}.current-style{font-size:12px;color:var(--ocv-accent,var(--accent,#83dec5));overflow-wrap:anywhere}
.gallery-note,.custom-style-note{margin:0;font-size:12px;line-height:1.7;color:var(--ocv-muted,var(--muted,#a0aaa8))}
.visual-style-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.visual-style-grid{max-height:580px;overflow-y:auto;align-content:start;padding:3px}.style-search{display:grid;gap:6px;font-size:12px}.style-search input{width:100%;box-sizing:border-box;min-width:0}.style-categories{display:flex;gap:6px;flex-wrap:wrap}.style-categories button{padding:6px 10px;font-size:12px;border:1px solid var(--ocv-border,var(--border,#35433e));border-radius:18px;background:transparent;color:inherit;cursor:pointer}.style-categories button.active{border-color:var(--ocv-accent,var(--accent,#83dec5));color:var(--ocv-accent,var(--accent,#83dec5))}
.visual-style-card{display:flex;flex-direction:column;min-width:0;padding:0;overflow:hidden;text-align:left;border:1px solid var(--ocv-border,var(--border,#35433e));border-radius:12px;background:var(--ocv-panel,var(--panel,#202526));color:inherit;cursor:pointer}
.visual-style-card.selected{border-color:var(--ocv-accent,var(--accent,#83dec5));box-shadow:0 0 0 2px color-mix(in srgb,var(--ocv-accent,var(--accent,#83dec5)) 20%,transparent)}
.visual-style-card:focus-visible{outline:3px solid var(--ocv-accent,var(--accent,#83dec5));outline-offset:3px}.visual-style-card:disabled{opacity:.55;cursor:default}
.style-preview-image{position:relative;display:block;width:100%;aspect-ratio:1;overflow:hidden;background:#d6d2cc}
.style-preview-image img{display:block;width:100%;height:100%;object-fit:contain}
.style-preview-badge{position:absolute;left:9px;bottom:9px;border-radius:5px;padding:3px 7px;background:#14201bdd;color:#fff;font-size:10px;line-height:1.5}
.style-card-copy{display:grid;gap:6px;padding:12px;width:100%;box-sizing:border-box;min-width:0}
.style-card-title{display:flex;justify-content:space-between;gap:6px;align-items:center}.style-card-title strong{font-size:14px}.style-card-title>span{font-size:11px;color:var(--ocv-accent,var(--accent,#83dec5));white-space:nowrap}
.style-card-copy small{font-size:12px;line-height:1.6;color:var(--ocv-muted,var(--muted,#a0aaa8));overflow-wrap:anywhere;white-space:normal}
.custom-style-note{padding:10px 12px;border-radius:8px;background:color-mix(in srgb,var(--ocv-accent,var(--accent,#83dec5)) 6%,transparent)}
@media(max-width:420px){.visual-style-grid{gap:8px}.style-card-copy{padding:9px}.style-card-title strong{font-size:13px}}
/* Keep all preview rows at their content height; scroll instead of shrinking. */
.visual-style-gallery .visual-style-grid{grid-auto-rows:max-content}
.visual-style-gallery .style-preview-image{flex:0 0 auto}
</style>
