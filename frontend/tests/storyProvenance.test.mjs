import assert from 'node:assert/strict'
import test from 'node:test'
import { draftStoryHandoff, restoreStoryProvenance } from '../src/storyProvenance.js'

const source = { story_id: 'a'.repeat(32), story_revision: 1, story_sha256: 'b'.repeat(64), adoption_id: 'c'.repeat(32) }

test('draft source summary follows text changes and never presents detached or legacy requirements as active', () => {
  const form = { script: 'Original story' }
  restoreStoryProvenance(form, { story_source: source, story_handoff: { status: 'current', title: 'Adopted story', story: 'Original story', brief: { idea: 'Keep a red scarf.' } } })
  assert.equal(draftStoryHandoff(form).status, 'current')
  assert.equal(draftStoryHandoff(form).brief.idea, 'Keep a red scarf.')
  form.script = 'Edited story'
  assert.equal(draftStoryHandoff(form).status, 'detached')
  assert.equal(draftStoryHandoff(form).brief, null)
  assert.match(draftStoryHandoff(form).message, /独立文稿/)
  form.script = 'Original story'
  delete form.story_source.adoption_id
  assert.equal(draftStoryHandoff(form).status, 'legacy_body_only')
  assert.equal(draftStoryHandoff(form).brief, null)
  restoreStoryProvenance(form, {})
  assert.equal(draftStoryHandoff(form), null)
})

test('restoring edited local drafts retains their original baseline and clears server-only display objects', () => {
  const form = { script: 'Edited story', story_source: source, _story_adopted_text: 'Original story', _story_title: 'Original title', _story_requirements: 'Original requirements', story_snapshot: { story: 'Different snapshot', title: 'Different title', brief: { idea: 'Different requirements' } }, story_handoff: { status: 'current' }, story_requirements: 'Other requirements' }
  restoreStoryProvenance(form, form)
  assert.equal(form.script, 'Edited story')
  assert.equal(form._story_adopted_text, 'Original story')
  assert.equal(form._story_title, 'Original title')
  assert.equal(form._story_requirements, 'Original requirements')
  for (const field of ['story_snapshot', 'story_handoff', 'story_requirements']) assert.equal(Object.hasOwn(form, field), false)
})

test('editing an existing project respects detached subtitles while an explicit new copy uses its own script', () => {
  const form = { script: 'Original story', _rerun_source_project: 'existing-project' }
  restoreStoryProvenance(form, { story_source: source, story_snapshot: { story: 'Original story', brief: { idea: 'Keep the scarf.' } }, story_handoff: { status: 'detached', message: '字幕内容已变化，本项目按独立文稿制作。' } })
  assert.equal(draftStoryHandoff(form).status, 'detached')
  assert.equal(draftStoryHandoff(form).brief, null)
  assert.match(draftStoryHandoff(form).message, /字幕内容已变化/)
  restoreStoryProvenance(form, form)
  assert.equal(draftStoryHandoff(form).status, 'detached', 'saved configuration drafts retain server status')
  form._rerun_source_project = ''
  assert.equal(draftStoryHandoff(form).status, 'current', 'a new draft will generate again from its original script')
  assert.equal(draftStoryHandoff(form).brief.idea, 'Keep the scarf.')
  form.script = 'Edited story'
  assert.equal(draftStoryHandoff(form).status, 'detached')
})
