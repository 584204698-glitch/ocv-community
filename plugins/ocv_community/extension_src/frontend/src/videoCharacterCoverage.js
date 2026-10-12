const coverageStatuses = new Set(['unknown', 'stale', 'empty', 'partial', 'covered'])

export function characterReviewPayload(characters = []) {
  return characters.map(character => ({
    id: String(character.id || ''),
    name: String(character.name || '').trim(),
    presence: ['opening', 'later'].includes(character.presence) ? character.presence : '',
    reference_id: String(character.reference_id || ''),
    ...(character.project_character_id ? { project_character_id: String(character.project_character_id) } : {}),
    in_last_frame: character.in_last_frame === true,
    ...(character.core_identity_confirmed === true && character.presence === 'opening'
      ? { core_identity_confirmed: true } : {}),
  }))
}

export function characterCoverage(value) {
  if (!value || !coverageStatuses.has(value.status)) {
    return { status: 'unknown', message: '尚未核对本镜角色，不能确认参考图是否覆盖完整。', characters: [], review: { characters: [] } }
  }
  return {
    ...value,
    characters: Array.isArray(value.characters) ? value.characters : [],
    review: { characters: characterReviewPayload(value.review?.characters || []) },
  }
}

// This prepares an all-or-nothing draft. Names are display text, never identities.
export function characterReferenceRepair({ coverage: value, draft, assets, profiles, defaultProfileId, chosenProfileId = '', adoptedReferenceIds = [] }) {
  const coverage = characterCoverage(value)
  const base = { reference_ids: [], profiles: [], profile_id: '', needsModeSwitch: draft.input_mode !== 'composition_reference', changes: false }
  const refused = reason => ({ ...base, reason })
  if (coverage.status !== 'partial') return refused(coverage.status === 'stale'
    ? '角色核对已过期，请重新核对并保存后再补参考。'
    : '请先保存本镜角色核对；仅对已确认缺图的角色补参考。')

  // Once first/last-frame mode is left, every later arrival needs an explicit
  // reference, including characters previously covered only by the last frame.
  if (!coverage.characters.some(character => !character.covered)) return refused('覆盖结果没有可核实的缺图角色，请刷新后重新核对。')
  const available = new Set(assets.filter(asset => asset.available).map(asset => asset.id))
  const suggested = new Set([...(coverage.suggested_reference_ids || []), ...adoptedReferenceIds])
  const references = [...new Set(draft.reference_ids || [])]
  if (references.some(id => !available.has(id))) return refused('已选参考图中有不可用图片，请先处理；未修改输入草稿。')
  for (const character of coverage.characters) {
    const identity = character.reference_id
    // Include explicit portraits of opening characters too. Omitting them can
    // change Picture numbers in an existing hand-written multi-reference prompt.
    if (!identity && character.presence === 'opening' && character.covered) continue
    if (!identity || !character.reference_available || !available.has(identity)) {
      return refused(`请先为“${character.name || '未命名角色'}”绑定可用参考图并保存核对；未修改输入草稿。`)
    }
    if (!references.includes(identity) && !suggested.has(identity)) return refused('角色参考建议与已确认绑定不一致，请刷新核对；未修改输入草稿。')
    if (!references.includes(identity)) references.push(identity)
  }
  if (references.length > 8) return refused('补齐后超过 8 张额外参考图，请先精简已选图片；未修改输入草稿。')
  const matching = profiles.filter(profile => profile.input_mode === 'composition_reference' && profile.available !== false)
  const usable = matching.filter(profile => Number.isFinite(Number(profile.reference_capacity)) && Number(profile.reference_capacity) >= references.length)
  if (!usable.length) return refused(matching.length
    ? `现有多图工作流容量不足，需要容纳 ${references.length} 张额外参考图；未修改输入草稿。`
    : '没有可用的构图与角色参考工作流，请先配置；未修改输入草稿。')
  const result = { ...base, profiles: usable, reference_ids: references }
  let profile = null
  if (chosenProfileId) {
    profile = usable.find(item => item.id === chosenProfileId)
    if (!profile) return { ...result, reason: '所选工作流不可用或容量不足，请重新选择；未修改输入草稿。' }
  } else {
    if (draft.input_mode === 'composition_reference') profile = usable.find(item => item.id === (draft.profile_id || defaultProfileId))
    profile ||= usable.find(item => item.id === defaultProfileId)
    if (!profile && usable.length === 1) profile = usable[0]
    if (!profile) return { ...result, needsProfileChoice: true, reason: '有多个合适的工作流，请先选择要用于本镜的一个。' }
  }
  const profileId = profile.id === defaultProfileId && !chosenProfileId ? '' : profile.id
  return { ...result, profile_id: profileId, profile_name: profile.name, reason: '',
    changes: base.needsModeSwitch || Boolean(draft.end_frame_reference_id)
      || (draft.profile_id || defaultProfileId) !== profile.id
      || JSON.stringify(references) !== JSON.stringify(draft.reference_ids),
  }
}
