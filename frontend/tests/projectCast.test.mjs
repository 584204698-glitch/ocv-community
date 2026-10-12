import assert from 'node:assert/strict'
import test from 'node:test'
import { ref } from 'vue'
import { useProjectCast } from '../src/useProjectCast.js'
import { deferred, fakeStorage, mountComposable } from './support/composableHarness.mjs'

const asset = id => ({ id, name: `Image ${id}`, available: true, url: `/images/${id}` })
const character = (extra = {}) => ({ id: 'pc_' + 'a'.repeat(32), name: '小兔', reference_id: 'rabbit-image', constraints: '红围巾', status: 'current', identity_sha256: 'f'.repeat(64), ...extra })
const view = (extra = {}) => ({ project_id: 'project-a', revision: 1, characters: [character()], assets: [asset('rabbit-image')], ...extra })

test('background roster updates at the same revision but never replaces unsaved edits',async t=>{
  let rows=[]
  const c=await editor(t,async()=>view({characters:rows}))
  rows=[character()]
  c.project.value={...c.project.value,project_cast:{characters:rows}}
  await c.flush()
  assert.equal(c.panel.draft.value[0].name,'小兔')
  assert.equal(c.project.value.revision,1)
  c.panel.draft.value[0].name='我的名字'
  rows=[character({name:'后台名字'})]
  c.project.value={...c.project.value,project_cast:{characters:rows}}
  await c.flush()
  assert.equal(c.panel.draft.value[0].name,'我的名字')
  assert.equal(c.panel.dirty.value,true)
  assert.equal(c.calls.length,2)
})
async function editor(t, request = async () => view(), storage = fakeStorage()) {
  const project = ref({ id: 'project-a', revision: 1 }), accountKey = ref('account-a'), disabled = ref(false), calls = [], updates = []
  const mounted = mountComposable(() => useProjectCast({ project, accountKey, disabled, storage,
    request: (url, options = {}) => { calls.push({ url, options }); return request(url, options) },
    onUpdated: value => { updates.push(value); project.value = { ...project.value, revision: value.revision } },
  }))
  t.after(() => mounted.unmount()); await mounted.flush()
  return { ...mounted, panel: mounted.instance, project, accountKey, disabled, storage, calls, updates }
}

test('project cast saves one explicit full list with no client identity hashes or coverage mutation', async t => {
  const context = await editor(t, (url, options) => options.method === 'PUT' ? view({ revision: 2, characters: [character({ name: '兔先生' })] }) : view())
  const { panel, calls } = context
  panel.draft.value[0].name = '兔先生'
  assert.equal(await panel.save(), true)
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), { revision: 1, characters: [{ id: character().id, name: '兔先生', reference_id: 'rabbit-image', constraints: '红围巾' }] })
  assert.equal(calls.at(-1).url, '/api/video-studio/project-a/project-cast')
  assert.equal(panel.dirty.value, false)
  assert.equal(context.project.value.revision, 2)
  panel.add()
  assert.equal(panel.canSave.value, false)
  panel.draft.value[1].name = '狐狸'; panel.draft.value[1].reference_id = 'missing'
  assert.equal(panel.canSave.value, false)
  assert.match(panel.validation.value, /可用的项目图片/)
})

test('project cast conflicts retain edits and do not automatically retry', async t => {
  const context = await editor(t, (url, options) => {
    if (options.method === 'PUT') throw Object.assign(new Error('conflict'), { status: 409 })
    return view()
  })
  context.panel.draft.value[0].constraints = '保留我的修改'
  context.project.value = { id: 'project-a', revision: 4 }; await context.flush()
  assert.equal(context.calls.length, 1)
  assert.equal(await context.panel.save(), false)
  assert.equal(context.panel.draft.value[0].constraints, '保留我的修改')
  assert.equal(context.panel.conflict.value, true)
  assert.equal(context.panel.canSave.value, false)
  assert.equal(await context.panel.save(), false)
  assert.equal(context.calls.length, 2)
})

test('late project cast reads cannot replace another visit to the same project or the account', async t => {
  const first = deferred(), second = deferred(), latest = deferred(), nextAccount = deferred()
  const pending = [first, second, latest, nextAccount]
  const context = await editor(t, () => pending.shift().promise)
  context.project.value = { id: 'project-b', revision: 1 }
  context.project.value = { id: 'project-a', revision: 1 }
  latest.resolve(view({ characters: [character({ name: '最新角色' })] })); await context.flush()
  first.resolve(view({ characters: [character({ name: '旧角色' })] }))
  second.resolve(view({ project_id: 'project-b' })); await context.flush()
  assert.equal(context.panel.draft.value[0].name, '最新角色')
  context.accountKey.value = 'account-b'
  assert.equal(context.panel.snapshot.value, null)
  assert.equal(context.panel.loading.value, true)
  nextAccount.resolve(view({ characters: [] })); await context.flush()
  assert.deepEqual(context.panel.draft.value, [])
})

test('account switch and unmount reject pending project cast writes', async t => {
  const save = deferred(), read = deferred()
  let gets = 0
  const context = await editor(t, (url, options) => options.method === 'PUT' ? save.promise : ++gets === 1 ? view() : read.promise)
  context.panel.draft.value[0].name = '旧账号编辑'
  const pending = context.panel.save()
  context.accountKey.value = 'account-b'
  save.resolve(view({ revision: 2 })); assert.equal(await pending, false)
  assert.equal(context.updates.length, 0)
  assert.equal(context.panel.loading.value, true)
  read.resolve(view({ characters: [] })); await context.flush()
  assert.deepEqual(context.panel.draft.value, [])
  const delayed = deferred()
  const other = await editor(t, (url, options) => options.method === 'PUT' ? delayed.promise : view())
  other.panel.draft.value[0].name = 'Unmounted'; const operation = other.panel.save(); other.unmount()
  delayed.resolve(view({ revision: 2 })); assert.equal(await operation, false)
  assert.equal(other.updates.length, 0)
})

test('a wrong-project response is rejected and a stale role requires explicit reconfirmation', async t => {
  const wrong = await editor(t, () => view({ project_id: 'project-b' }))
  assert.equal(wrong.panel.snapshot.value, null)
  assert.match(wrong.panel.error.value, /不属于当前项目/)
  const stale = await editor(t, () => view({ characters: [character({ status: 'stale' })] }))
  assert.equal(stale.panel.dirty.value, false)
  assert.equal(stale.panel.canSave.value, true)
  assert.equal(stale.calls.length, 1, 'loading never saves a stale binding')
})

test('uploading a cast candidate preserves edited roles and does not bind or adopt the image', async t => {
  const context = await editor(t, (url, options) => options.method === 'POST'
    ? { project: { id: 'project-a', revision: 2 }, asset: asset('new-image') } : view())
  context.panel.add(); context.panel.draft.value[1].name = '新角色'
  assert.equal(await context.panel.upload(new Blob(['image'], { type: 'image/png' })), true)
  assert.equal(context.panel.draft.value[1].name, '新角色')
  assert.equal(context.panel.draft.value[1].reference_id, '')
  assert.equal(context.panel.dirty.value, true)
  assert.equal(context.panel.snapshot.value.revision, 2)
  assert.equal(context.panel.assets.value.at(-1).id, 'new-image')
  assert.equal(context.calls.at(-1).url, '/api/video-studio/project-a/video-input-assets')
  assert.equal(context.calls.filter(call => call.options.method === 'PUT').length, 0)
})

test('project cast drafts survive leaving and reload, stay owner-scoped, and retain their original revision', async t => {
  const storage = fakeStorage()
  const context = await editor(t, (url) => view({ project_id: url.includes('project-b') ? 'project-b' : 'project-a' }), storage)
  context.panel.draft.value[0].constraints = 'A 项目的最后一次输入'
  context.project.value = { id: 'project-b', revision: 1 }; await context.flush()
  assert.equal(context.panel.draft.value[0].constraints, '红围巾')
  context.panel.draft.value[0].name = 'B 的角色'
  context.project.value = { id: 'project-a', revision: 1 }; await context.flush()
  assert.equal(context.panel.draft.value[0].constraints, 'A 项目的最后一次输入')
  context.accountKey.value = 'account-b'; await context.flush()
  assert.equal(context.panel.draft.value[0].constraints, '红围巾')
  context.unmount()
  const restored = await editor(t, (url, options) => {
    if (options.method === 'PUT') throw Object.assign(new Error('conflict'), { status: 409 })
    return view({ revision: 7 })
  }, storage)
  restored.project.value = { id: 'project-a', revision: 7 }
  assert.equal(restored.panel.draft.value[0].constraints, 'A 项目的最后一次输入')
  assert.equal(restored.panel.dirty.value, true)
  assert.equal(restored.panel.revisionChanged.value, true)
  assert.equal(await restored.panel.save(), false)
  assert.equal(JSON.parse(restored.calls.at(-1).options.body).revision, 1)
  await restored.panel.load({ discardDraft: true })
  assert.equal(restored.panel.draft.value[0].constraints, '红围巾')
  assert.equal(storage.getItem('ocv.project-cast.draft.v1:["account-a","project-a"]'), null)
})
