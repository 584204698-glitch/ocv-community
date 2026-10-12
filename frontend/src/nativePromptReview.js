// Frozen submissions must never borrow later edits or another project's voices.
export function nativePromptReview(shot) {
  const request = shot?.video_request
  const frozen = Boolean(request)
  const plan = frozen ? request?.dialogue?.plan : shot?.native_dialogue
  if (!plan) return null
  const external = frozen ? request?.external_dialogue : null
  // The H3 plan excludes narration. Review the frozen final soundtrack rather
  // than losing the narrator or borrowing names/voices from a later edit.
  const speakers = new Map([
    ...(external ? request?.dialogue?.basis?.plan?.speakers || [] : []),
    ...(plan.speakers || []),
  ].map(s => [s.character_id, s]))
  const chinese = value => /[\u3400-\u9fff]/u.test(String(value || '')) ? String(value) : ''
  const action = chinese(frozen ? request.source_prompt : shot.action || shot.video_prompt)
  const turns = (external?.clips || plan.turns || []).map(turn => {
    const speaker = speakers.get(turn.character_id)
    const visualTurn = (plan.turns || []).find(t => t.character_id === turn.character_id && t.start === turn.start)
    return {
      ...turn,
      name: speaker?.name || '未绑定角色',
      source: (turn.kind || speaker?.kind) === 'narrator' ? '画外旁白' : speaker?.on_screen === false ? '画外角色' : '画面内角色',
      voiceBound: Boolean(external ? turn.voice_id && turn.voice_sha256 : speaker?.voice_id),
      mouthHidden: (external ? visualTurn?.mouth_visible : turn.mouth_visible) === false,
      listeners: [...speakers.values()].filter(s => s.character_id !== turn.character_id && s.kind !== 'narrator' && s.on_screen !== false)
        .map(s => s.name + (s.presence === 'later' ? '（入画后）' : '')),
    }
  })
  const beats = (plan.performance_beats || []).map(beat => ({start:beat.start,end:beat.end,action:beat.action_zh}))
  const receipt = frozen ? request.native_visual_grounding : shot?.native_visual_grounding
  return {frozen, externalTts: Boolean(external), action, duration: plan.duration, turns, beats,
    openingObservation: receipt?.status === 'not_rechecked' ? '' : chinese(receipt?.observations),
    silent: turns.length === 0,
    afterSpeech: turns.length ? turns.at(-1).end : 0,
    stale: !frozen && Boolean(shot.native_staging_stale),
  }
}
