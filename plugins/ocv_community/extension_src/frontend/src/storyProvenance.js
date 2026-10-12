// Story display hints are local metadata. Production sends only story_source;
// the server resolves the adopted snapshot and decides which requirements apply.
export function sameStorySource(left, right) {
  return Boolean(left && right
    && left.story_id === right.story_id
    && left.story_revision === right.story_revision
    && left.story_sha256 === right.story_sha256
    && (left.adoption_id || '') === (right.adoption_id || ''))
}

export function restoreStoryProvenance(form, saved = {}) {
  const source = saved.story_source
  const snapshot = saved.story_snapshot || {}
  const handoff = saved.story_handoff || {}
  // Capture these before clearing server-only fields when form === saved.
  const adoptedText = saved._story_adopted_text ?? snapshot.story ?? handoff.story ?? ''
  const requirements = saved._story_requirements ?? saved.story_requirements ?? handoff.brief?.idea ?? snapshot.brief?.idea ?? ''
  const title = saved._story_title ?? handoff.title ?? snapshot.title ?? ''
  const projectHandoff = saved._story_project_handoff ?? saved.story_handoff
  form.story_source = source ? { ...source } : null
  form._story_adopted_text = source ? String(adoptedText) : ''
  form._story_requirements = source ? String(requirements) : ''
  form._story_title = source ? String(title) : ''
  form._story_project_handoff = source && projectHandoff ? {
    status: String(projectHandoff.status || ''), message: String(projectHandoff.message || ''),
  } : null
  delete form.story_snapshot
  delete form.story_handoff
  delete form.story_requirements
}

export function draftStoryHandoff(form) {
  if (!form.story_source) return null
  const matches = String(form.script || '').trim() === String(form._story_adopted_text || '').trim()
  // An existing project's subtitles may have diverged while its original
  // creation script stayed intact. Configuration-only edits keep that status;
  // an explicit new copy can be evaluated against its own script again.
  const projectHandoff = form._rerun_source_project ? form._story_project_handoff : null
  const projectStatus = ['current', 'detached', 'legacy_body_only'].includes(projectHandoff?.status) ? projectHandoff.status : ''
  const status = matches ? projectStatus || (form.story_source.adoption_id ? 'current' : 'legacy_body_only') : 'detached'
  return {
    source: form.story_source,
    title: form._story_title || '',
    status,
    message: matches && projectStatus && projectHandoff.message ? projectHandoff.message : status === 'detached'
      ? '文稿已在制作页修改，将作为独立文稿制作；原故事版本仍保留。'
      : status === 'legacy_body_only'
        ? '此草稿采用了故事正文；历史采用记录未保存当时的创作要求。'
        : '已采用此故事版本。制作时将核对所采用的故事与创作要求。',
    brief: status === 'current' ? { idea: form._story_requirements || '' } : null,
  }
}
