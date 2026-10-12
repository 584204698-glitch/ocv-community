// Creation bindings use upload asset IDs. Project-owned role IDs are assigned
// only after the server copies these images into the dynamic project.
export const MAX_CREATION_CHARACTERS = 6
export const MAX_CHARACTER_NAME = 160

export function projectCastBindingPayload(bindings = []) {
  return (Array.isArray(bindings) ? bindings : []).map(row => ({
    asset_id: String(row?.asset_id || ''),
    name: String(row?.name || '').trim(),
    constraints: String(row?.constraints || '').trim(),
  }))
}

export function restoreProjectCastBindings(bindings, referenceIds = []) {
  const available = new Set(referenceIds), used = new Set()
  return (Array.isArray(bindings) ? bindings : []).filter(row => {
    const id = String(row?.asset_id || '')
    if (!available.has(id) || used.has(id)) return false
    used.add(id)
    return true
  }).map(row => ({ asset_id: String(row.asset_id), name: String(row.name || ''), constraints: String(row.constraints || '') }))
}

export function projectCastBindingError(bindings, referenceIds = []) {
  if (!Array.isArray(bindings)) return '固定角色配置不完整，请重新选择参考图绑定。'
  if (bindings.length > MAX_CREATION_CHARACTERS) return '创建时最多绑定 6 个固定角色。'
  const used = new Set(), names = new Set()
  for (const row of bindings) {
    if (!row?.asset_id || !referenceIds.includes(row.asset_id)) return '固定角色的参考图已移除，请重新选择。'
    if (used.has(row.asset_id)) return '每张参考图只能绑定一个固定角色。'
    used.add(row.asset_id)
    if (!String(row.name || '').trim()) return '请填写固定角色在故事中的称呼，或取消该图片的角色绑定。'
    const name = String(row.name || '').trim()
    if (names.has(name)) return '固定角色的称呼不能重复，请填写故事中可区分的称呼。'
    names.add(name)
    if (name.length > MAX_CHARACTER_NAME) return '角色称呼最多 160 字。'
    if (String(row.constraints || '').trim().length > 2000) return '每个角色的外观要求最多 2000 字。'
  }
  return ''
}
