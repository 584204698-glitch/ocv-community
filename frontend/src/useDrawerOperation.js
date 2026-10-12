import { computed, ref } from 'vue'

// Closing settings must not silently cancel an adopted voice before its
// reference upload and current-project selection have both completed.
export function useDrawerOperation() {
  const current = ref(''), voiceAdoptionPending = ref(false)
  const studioDrawer = computed({
    get: () => current.value,
    set: value => { if (!voiceAdoptionPending.value) current.value = value },
  })
  function resetDrawerOperation() {
    voiceAdoptionPending.value = false
    current.value = ''
  }
  return { studioDrawer, voiceAdoptionPending, resetDrawerOperation }
}
