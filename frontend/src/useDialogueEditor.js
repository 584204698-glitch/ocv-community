import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { requestJSON } from './api'

export function useDialogueEditor({ project, shot, account, disabled, onUpdated, request = requestJSON }) {
  const turns = ref([]), baseline = ref('[]'), busy = ref(false), error = ref(''), conflict = ref(false)
  const dirty = computed(() => JSON.stringify(turns.value) !== baseline.value)
  const key = () => `${account.value}:${project.value?.id}:${shot.value?.id}`
  let epoch = 0, disposed = false
  const reset = () => {
    turns.value = JSON.parse(JSON.stringify(shot.value?.native_dialogue?.turns || []))
    baseline.value = JSON.stringify(turns.value); conflict.value = false; error.value = ''
  }
  watch(key, () => { epoch++; busy.value = false; reset() }, { immediate:true, flush:'sync' })
  watch(() => JSON.stringify(shot.value?.native_dialogue?.turns || []), value => {
    if (value === baseline.value) return
    if (dirty.value) { conflict.value = true; return }
    reset()
  }, { flush:'sync' })
  async function save() {
    if (disabled.value || busy.value || conflict.value || !dirty.value) return false
    const current = key(), serial = epoch
    const saved = JSON.stringify(turns.value)
    busy.value = true; error.value = ''
    try {
      const result = await request(`/api/video-studio/${project.value.id}/shots/${shot.value.id}/dialogue`, {
        method:'PUT', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({revision:project.value.revision, turns:JSON.parse(saved)}),
      })
      if (disposed || epoch !== serial || key() !== current) return false
      baseline.value = saved
      onUpdated(result)
      return true
    } catch (failure) {
      if (!disposed && epoch === serial && key() === current) error.value = failure.message
      return false
    } finally { if (!disposed && epoch === serial && key() === current) busy.value = false }
  }
  onBeforeUnmount(() => {disposed = true; epoch++})
  return {turns,dirty,busy,error,conflict,save,reset}
}
