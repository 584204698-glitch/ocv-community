export const dynamicPacingOptions = [
  {key:'slow', label:'舒缓', detail:'约 10 秒一镜', min:6, target:10, max:15},
  {key:'standard', label:'标准', detail:'约 8 秒一镜', min:5, target:8, max:12},
  {key:'fast', label:'紧凑', detail:'约 6 秒一镜 · 画面更丰富', min:4, target:6, max:8},
  {key:'auto', label:'自动', detail:'由故事内容决定'},
  {key:'custom', label:'自定义', detail:'自己调整镜头时长'},
]

export function dynamicPacingSummary(form) {
  const preset = dynamicPacingOptions.find(p=>p.key===form.visual_pacing_preset)
  if (!preset || preset.key==='auto') return '自动：按故事内容切镜'
  if (preset.key==='custom') return `自定义：目标 ${form.visual_target_duration} 秒，最长 ${Math.min(15,Number(form.visual_max_duration)||15)} 秒`
  return `${preset.label}：目标 ${preset.target} 秒，最长 ${preset.max} 秒`
}

export function selectDynamicPacing(form, key) {
  const preset=dynamicPacingOptions.find(p=>p.key===key)
  if (!preset) return
  if (key === 'auto' && form.visual_pacing_preset !== 'auto') {
    form._previous_pacing = Object.fromEntries(['visual_pacing_preset','visual_min_duration','visual_target_duration','visual_max_duration','visual_max_slides'].map(k => [k, form[k]]))
  }
  form.visual_pacing_preset=key
  if (preset.target) {
    form.visual_min_duration=preset.min
    form.visual_target_duration=preset.target
    form.visual_max_duration=preset.max
    form.visual_max_slides=6
  }
}

export function restoreDynamicPacing(form) {
  if (!form._previous_pacing) return
  Object.assign(form, form._previous_pacing)
  delete form._previous_pacing
}

export function normalizeDynamicCustom(form) {
  const number=(key, fallback, min, max)=>{
    const value=Number(form[key])
    return Math.max(min,Math.min(max,Number.isFinite(value)?value:fallback))
  }
  form.visual_min_duration=number('visual_min_duration',5,4,15)
  form.visual_target_duration=number('visual_target_duration',8,Math.max(5,form.visual_min_duration),15)
  form.visual_max_duration=number('visual_max_duration',12,Math.max(6,form.visual_target_duration),15)
  form.visual_max_slides=Math.round(number('visual_max_slides',6,1,12))
}
