import test from 'node:test'
import assert from 'node:assert/strict'
import { useDrawerOperation } from '../src/useDrawerOperation.js'

test('all drawer close paths preserve a pending adoption, then unlock on success or failure', () => {
  const state = useDrawerOperation()
  state.studioDrawer.value = '声音设置'
  state.voiceAdoptionPending.value = true
  state.studioDrawer.value = '' // Done, backdrop, Escape and close all use this setter.
  assert.equal(state.studioDrawer.value, '声音设置')
  state.studioDrawer.value = '作品风格'
  assert.equal(state.studioDrawer.value, '声音设置')
  state.voiceAdoptionPending.value = false
  state.studioDrawer.value = ''
  assert.equal(state.studioDrawer.value, '')
})

test('changing account or project releases the drawer without keeping its pending operation', () => {
  const state = useDrawerOperation()
  state.studioDrawer.value = '声音设置'
  state.voiceAdoptionPending.value = true
  state.resetDrawerOperation()
  assert.equal(state.studioDrawer.value, '')
  assert.equal(state.voiceAdoptionPending.value, false)
  state.studioDrawer.value = '接口与服务'
  assert.equal(state.studioDrawer.value, '接口与服务')
})
