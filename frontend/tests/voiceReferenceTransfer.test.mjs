import test from 'node:test'
import assert from 'node:assert/strict'
import { importDesignedVoice } from '../src/voiceReferenceTransfer.js'

const voice = { id: 'a'.repeat(32), name: '温暖旁白', status: 'saved' }
const filename = `Qwen-${voice.name}-${voice.id}.wav`
const asset = { id: 'reference.wav', kind: 'audio', name: filename, url: '/api/editor/uploads/reference.wav' }
function wav() {
  const bytes = new Uint8Array(48)
  bytes.set(Buffer.from('RIFF'));bytes.set(Buffer.from('WAVE'), 8)
  return bytes
}
function fixture({ existing = [], audio = wav(), changed = false, failUpload = false } = {}) {
  const calls = []
  return { calls, request: async (url, options = {}) => {
    calls.push([url, options.method || 'GET'])
    if (url.endsWith('/adopt')) return voice
    if (url === '/api/editor/uploads' && options.method !== 'POST') return { assets: existing }
    if (url === '/api/editor/uploads' && options.method === 'POST') {
      if (failUpload) throw Error('上传失败')
      const file = options.body.get('file')
      assert.equal(file.name, filename)
      assert.deepEqual(new Uint8Array(await file.arrayBuffer()), audio)
      return { asset }
    }
    throw Error('Unexpected request')
  }, fetchAudio: async (url, options) => {
    assert.equal(options.credentials, 'include')
    calls.push([url, 'AUDIO'])
    const bytes = new Uint8Array(audio)
    if (changed && url === asset.url) bytes[44] = 99
    return new Response(bytes, { status: 200 })
  } }
}

test('imports the exact designed WAV through the current account upload API', async () => {
  const io = fixture()
  assert.deepEqual(await importDesignedVoice(voice, io), { saved: voice, asset })
  assert.equal(io.calls.filter(([url, method]) => url === '/api/editor/uploads' && method === 'POST').length, 1)
  assert.ok(io.calls.every(([url]) => url.startsWith('/api/')))
})
test('reuses an existing identical reference after a lost response or repeat adoption', async () => {
  const io = fixture({ existing: [asset] })
  assert.equal((await importDesignedVoice(voice, io)).asset, asset)
  assert.ok(!io.calls.some(([url, method]) => url === '/api/editor/uploads' && method === 'POST'))
})
test('an equal filename with different bytes is not mistaken for the designed voice', async () => {
  const io = fixture({ existing: [asset], changed: true })
  await importDesignedVoice(voice, io)
  assert.ok(io.calls.some(([url, method]) => url === '/api/editor/uploads' && method === 'POST'))
})
test('invalid audio cannot be imported', async () => {
  const io = fixture({ audio: new TextEncoder().encode('<html>login</html>') })
  await assert.rejects(importDesignedVoice(voice, io), /WAV/)
  assert.ok(!io.calls.some(([url]) => url === '/api/editor/uploads'))
})
test('leaving the project while adoption is pending prevents a later upload', async () => {
  const controller = new AbortController(), io = fixture(), original = io.request
  io.request = async (...args) => { const result = await original(...args);controller.abort();return result }
  await assert.rejects(importDesignedVoice(voice, { ...io, signal: controller.signal }), { name: 'AbortError' })
  assert.equal(io.calls.length, 1)
})
test('upload failures return no reference and never submit a synthesis job', async () => {
  const io = fixture({ failUpload: true })
  await assert.rejects(importDesignedVoice(voice, io), /上传失败/)
  assert.ok(!io.calls.some(([url]) => url.includes('/jobs')))
})
