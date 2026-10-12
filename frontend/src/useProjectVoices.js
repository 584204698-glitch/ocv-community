import { computed, onBeforeUnmount, ref, toValue, watch } from 'vue'
import { requestJSON } from './api'

export function useProjectVoices({ project, accountKey, disabled = false, onUpdated = () => {}, request = requestJSON }) {
  const snapshot = ref(null), loading = ref(false), saving = ref(false), error = ref(''), message = ref('')
  let epoch = 0, sequence = 0, disposed = false
  const scope = computed(() => JSON.stringify([toValue(accountKey), toValue(project)?.id]))
  const busy = computed(() => loading.value || saving.value)
  const characters = computed(() => snapshot.value?.characters || [])
  const ticket = () => ({ epoch, scope: scope.value, id: toValue(project)?.id })
  const current = t => !disposed && t.epoch === epoch && t.scope === scope.value
  function accept(value, t) {
    if (value?.project_id !== t.id || !Number.isInteger(value.revision) || !Array.isArray(value.characters)) throw new Error('音色绑定结果不属于当前项目，请刷新。')
    snapshot.value = value
  }
  async function load() {
    if (disposed || saving.value) return false
    const t = ticket(), seq = ++sequence
    if (!t.id) return false
    loading.value = true; error.value = ''
    try {
      const value = await request(`/api/video-studio/${encodeURIComponent(t.id)}/project-voices`)
      if (!current(t) || seq !== sequence) return false
      accept(value, t); return true
    } catch (e) { if (current(t) && seq === sequence) error.value = e.message; return false }
    finally { if (current(t) && seq === sequence) loading.value = false }
  }
  async function bind(characterId, voiceId, voiceMode = 'reference') {
    if (disposed) return false
    if (saving.value) { error.value = '上一份音色还在保存，请稍候再采用。'; return false }
    if (toValue(disabled)) { error.value = '本次声音设置尚未绑定本项目。请等待当前制作阶段完成或暂停后重试；已有音色仍保留在音色库。'; return false }
    if (!characters.value.some(c => c.character_id === characterId)) { error.value = '当前说话者尚未准备好，本次未绑定，请刷新角色列表后重试。'; return false }
    const t = ticket(), revision = snapshot.value.revision
    ++sequence; loading.value = false; saving.value = true; error.value = ''; message.value = ''
    try {
      const value = await request(`/api/video-studio/${encodeURIComponent(t.id)}/project-voices`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision, character_id: characterId, voice_id: voiceId || null, ...(voiceMode === 'generated' ? {voice_mode:'generated'} : {}) }),
      })
      if (!current(t)) return false
      accept(value, t); onUpdated(value)
      const name=characters.value.find(c => c.character_id === characterId)?.name || '说话者'
      message.value = voiceMode === 'generated' ? `${name}已设为 H3 自行生成声音，不使用参考音频。已有片段不会自动重做。` : `${name}音色已保存，后续生成沿用这份声音参考。已有片段不会自动重做。`
      return true
    } catch (e) {
      if (current(t)) error.value = e.status === 409 ? `本次绑定未保存：${e.message}。声音仍在音色库，无需重新设计。` : e.message
      return false
    } finally { if (current(t)) saving.value = false }
  }
  watch(scope, () => {
    ++epoch; ++sequence; snapshot.value = null; loading.value = false; saving.value = false
    error.value = ''; message.value = ''; void load()
  }, { immediate: true, flush: 'sync' })
  watch(() => toValue(project)?.revision, revision => {
    if (!busy.value && snapshot.value && revision !== snapshot.value.revision) void load()
  })
  watch(() => JSON.stringify([toValue(project)?.project_cast?.characters || [], toValue(project)?.native_narrator || null]), () => {
    if (snapshot.value && !saving.value) void load()
  })
  onBeforeUnmount(() => { disposed = true; ++epoch; ++sequence })
  return { snapshot, characters, busy, loading, saving, error, message, load, bind }
}
