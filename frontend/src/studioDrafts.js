// SPDX-License-Identifier: AGPL-3.0-only

import { restoreStoryProvenance } from './storyProvenance.js'
import { restoreProjectCastBindings } from './projectCastBindings.js'

export function copyDraft(value) {
  return JSON.parse(JSON.stringify(value))
}

export function snapshotDraftForm(form) {
  return copyDraft(Object.fromEntries(Object.entries(form).filter(([key, value]) =>
    key !== 'auto_analyze_reference_images'
    && !/(key|password|token|secret)/i.test(key)
    && typeof value !== 'function',
  )))
}

export function restoreDraftForm(form, defaults, saved = {}) {
  // A partial historical draft must not inherit fields from the project being left.
  for (const key of Object.keys(form)) {
    if (key.startsWith('_') || (!Object.hasOwn(defaults, key) && !Object.hasOwn(saved, key))) delete form[key]
  }
  Object.assign(form, copyDraft(defaults), copyDraft(saved))
  if (Object.hasOwn(defaults, 'story_source')) restoreStoryProvenance(form, saved)
  if (Object.hasOwn(defaults, 'project_cast_bindings')) form.project_cast_bindings = restoreProjectCastBindings(saved.project_cast_bindings,
    saved.reference_image_ids?.length ? saved.reference_image_ids : [saved.protagonist_reference_image_id].filter(Boolean))
}

export function clearProjectContent(form) {
  Object.assign(form, {
    script: '', story_source: null, _story_adopted_text: '', _story_requirements: '', _story_title: '', _story_project_handoff: null, source_audio_id: '', skip_tts: false, skip_text_correction: false,
    reference_image_ids: [], protagonist_reference_image_id: '',
    project_cast_bindings: [],
    reference_image_notes: {}, reference_image_labels: {}, reference_image_kinds: {},
    global_character_prompt: '', story_environment_prompt: '',
    bgm_enabled: false, bgm_tracks: [],
  })
  delete form.story_snapshot
  delete form.story_handoff
  delete form.story_requirements
}
