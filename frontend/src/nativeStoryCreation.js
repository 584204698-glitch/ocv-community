// A persisted request identity makes a lost HTTP response safe to retry.
export const isNativeStory = form => Boolean(form.dynamic_video && ['dialogue', 'mixed'].includes(form.speech_mode))

export function nativeStoryPayload(form, makeId = () => {
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, x => x.toString(16).padStart(2, '0')).join('')
}) {
  if (!isNativeStory(form)) throw new Error('请选择角色对话或旁白混合模式。')
  if (form.video_generation_backend !== 'comfyui' || !form.comfyui_profile_id) {
    throw new Error('请在画面编排中选择支持角色音频参考的 H3 多参工作流。')
  }
  if (form.reference_image_ids?.length || form.project_cast_bindings?.length) {
    throw new Error('当前对话入口会为本故事设计独立角色，请移除创建页参考图；生成后可在项目角色中编辑。')
  }
  const payload = {
    name: String(form.project_name || '').trim(), story: String(form.script || '').trim(),
    speech_mode: form.speech_mode, dialogue_route: form.dialogue_route || 'external_tts', style: String(form.visual_style_prompt || '').trim(),
    characters: String(form.global_character_prompt || ''), world: String(form.story_environment_prompt || ''),
    ratio: form.video_orientation === 'portrait' ? '9:16' : '16:9',
    duration_seconds: Number(form.native_duration_seconds) || null,
    minimum_shots: Number(form.native_minimum_shots) || null,
    visual_pacing_preset: form.visual_pacing_preset || 'auto',
    visual_min_duration: Number(form.visual_min_duration) || 5,
    visual_target_duration: Number(form.visual_target_duration) || 8,
    visual_max_duration: Number(form.visual_max_duration) || 12,
    visual_max_slides: Number(form.visual_max_slides) || 6,
    comfyui_steps: form.comfyui_steps === undefined ? 8 : form.comfyui_steps, comfyui_width: form.comfyui_width || null, comfyui_height: form.comfyui_height || null,
    comfyui_profile_id: form.comfyui_profile_id, image_profile_id: form.image_profile_id || '',
    image_resolution: form.image_resolution || '1k', use_cloud_image_pool: Boolean(form.use_cloud_image_pool),
    scene_references_enabled: form.scene_references_enabled !== false,
    one_click: Boolean(form.dynamic_auto_advance), story_source: form.story_source || null,
    dialogue_tts_engine: form.dialogue_tts_engine || 'indextts25',
  }
  const basis = JSON.stringify(payload)
  if (form._native_request_basis !== basis || !/^[a-f0-9]{32}$/.test(form._native_request_id || '')) {
    form._native_request_id = makeId()
    form._native_request_basis = basis
  }
  return { request_id: form._native_request_id, ...payload }
}
