import { requestJSON } from './api'

function audioAsset(asset) {
  return asset?.kind === 'audio' && /^[a-zA-Z0-9_.-]+\.wav$/.test(asset.id || '')
    && asset.url === `/api/editor/uploads/${asset.id}`
}

// Reuse the account-scoped upload API; never send a reference to a cloud service.
export async function importDesignedVoice(voice, { signal, request = requestJSON, fetchAudio = globalThis.fetch } = {}) {
  const check = () => signal?.throwIfAborted()
  check()
  if (!/^[a-f0-9]{32}$/.test(voice?.id || '') || !['ready', 'saved'].includes(voice.status)) {
    throw new Error('请先完成音色试听，再采用。')
  }
  const saved = await request(`/api/voice-design/${voice.id}/adopt`, { method: 'POST', signal })
  check()
  const response = await fetchAudio(`/api/voice-design/${voice.id}/audio`, { credentials: 'include', signal })
  if (!response.ok) throw new Error('参考声音读取失败，请重试。')
  const bytes = new Uint8Array(await response.arrayBuffer())
  check()
  const ascii = (start, end) => String.fromCharCode(...bytes.slice(start, end))
  if (bytes.length < 44 || bytes.length > 20 * 1024 * 1024 || ascii(0, 4) !== 'RIFF' || ascii(8, 12) !== 'WAVE') {
    throw new Error('试听文件不是有效的 WAV 参考音频，请重新设计声音。')
  }
  const name = (saved.name || voice.name || '设计音色').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 40)
  const filename = `Qwen-${name}-${voice.id}.wav`
  const inventory = await request('/api/editor/uploads', { signal })
  check()
  // A retry after a lost upload response must not create another identical file.
  for (const asset of inventory.assets || []) {
    if (!audioAsset(asset) || asset.name !== filename) continue
    const old = await fetchAudio(asset.url, { credentials: 'include', signal })
    check()
    if (!old.ok) continue
    const previous = new Uint8Array(await old.arrayBuffer())
    check()
    if (previous.length === bytes.length && previous.every((value, index) => value === bytes[index])) {
      return { saved, asset }
    }
  }
  const data = new FormData()
  data.append('file', new Blob([bytes], { type: 'audio/wav' }), filename)
  check()
  const result = await request('/api/editor/uploads', { method: 'POST', body: data, signal })
  check()
  if (!audioAsset(result.asset)) throw new Error('参考音频导入结果异常，尚未更换当前声音。')
  return { saved, asset: result.asset }
}
