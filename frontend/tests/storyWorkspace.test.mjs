import assert from 'node:assert/strict'
import test from 'node:test'
import { ref } from 'vue'
import { useStoryWorkspace } from '../src/useStoryWorkspace.js'
import { deferred, installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

const A = 'a'.repeat(32), B = 'b'.repeat(32)
const body = '  第一行保留原文和空格。\n第二行也是作者亲自写下的故事，直到最后一个标点。  '
const record = (id = A, changes = {}) => ({
  id, revision: 1, brief: { intent: 'idea', idea: '一个想法', preset: 'general', duration_seconds: 90 },
  title: '故事', story: body, synopsis: '梗概', derived_status: 'current', status: 'ready',
  story_revision: 1, story_sha256: 'c'.repeat(64), brief_sha256: 'b'.repeat(64), task: { status: 'idle' }, versions: [], ...changes,
})
const review = (saved, changes = {}) => ({ status: 'current', verdict: 'ready', issues: [], story_sha256: saved.story_sha256, brief_sha256: saved.brief_sha256, ...changes })
const proposal = (saved, start = 2, end = 8, replacement = '新的选区文字') => {
  const points = Array.from(saved.story)
  return { id: 'proposal-one', status: 'pending', base_story_revision: saved.story_revision, base_story_sha256: saved.story_sha256, base_brief_sha256: saved.brief_sha256,
    start, end, original: points.slice(start, end).join(''), replacement, story: points.slice(0, start).join('') + replacement + points.slice(end).join(''), instruction: '保留事实，改善表达。', review: review(saved) }
}
async function workspace(t, route = async () => { throw new Error('unexpected request') }, list = () => []) {
  const browser = installBrowserGlobals(), owner = ref('1'), calls = [], adopted = []
  const request = async (url, options = {}) => {
    const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null }
    calls.push(call)
    if (url === '/api/stories/presets') return { presets: [{ id: 'general', label: '通用故事' }] }
    if (url === '/api/stories' && call.method === 'GET') return { stories: structuredClone(list()) }
    return route(call)
  }
  const factory = () => useStoryWorkspace({ owner: () => owner.value, request, onAdopt: item => adopted.push(item) })
  let mounted = mountComposable(factory)
  t.after(() => { try { mounted.unmount() } finally { browser.restore() } })
  await mounted.flush()
  return { browser, owner, calls, adopted, get s() { return mounted.instance },
    flush: () => mounted.flush(), async remount() { mounted.unmount(); mounted = mountComposable(factory); await mounted.flush() } }
}

test('background story titles refresh after switching projects without overwriting cached content', async t => {
  let server = record(A, { title: '生成前的标题', updated_at: '2026-09-29T10:00:00Z' })
  const other = record(B, { title: '另一个项目', updated_at: '2026-09-29T11:00:00Z' })
  const c = await workspace(t, async call => structuredClone(call.url.endsWith(A) ? server : other), () => [server, other])
  await c.s.openStory(A)
  await c.s.openStory(B)
  server = record(A, { revision: 2, title: '后台完成的新标题', story: body + '新的结尾。', updated_at: '2026-09-30T10:00:00Z' })
  await c.s.refreshList()
  assert.equal(c.s.entries.value[0].id, A)
  assert.equal(c.s.entries.value[0].title, server.title)
  assert.equal(c.s.entries.value[0].local, false)
  assert.equal(c.s.draft.value.id, B)
  await c.s.openStory(A)
  assert.equal(c.s.draft.value.story, server.story)
  assert.equal(c.s.conflict.value, null)
  assert.equal(c.calls.some(call => call.method !== 'GET'), false)
})

test('list refresh preserves unsaved titles and local-only stories', async t => {
  let server = record()
  const c = await workspace(t, async call => structuredClone(call.url.endsWith(A) ? server : record(B)), () => [server])
  await c.s.openStory(A)
  c.s.draft.value.title = '作者未保存的标题'
  await c.s.openStory(B)
  server = record(A, { revision: 2, title: '另一窗口保存的标题' })
  c.s.createStory('manuscript')
  const localId = c.s.selectedId.value
  c.s.draft.value.title = '本机新稿'
  await c.s.refreshList()
  assert.equal(c.s.entries.value.find(item => item.id === A).title, '作者未保存的标题')
  assert.equal(c.s.entries.value.find(item => item.id === A).local, true)
  assert.equal(c.s.entries.value.find(item => item.id === localId).title, '本机新稿')
  assert.equal(c.s.entries.value.find(item => item.id === localId).local, true)
  await c.s.openStory(A)
  assert.equal(c.s.draft.value.title, '作者未保存的标题')
  assert.equal(c.s.conflict.value.title, server.title)
})

test('manuscript saves and adopts exact text with no model or media call', async t => {
  let saved
  const c = await workspace(t, async call => {
    if (call.method === 'POST' && call.url === '/api/stories') {
      saved = record(call.body.id, { ...call.body, revision: 1, brief: call.body })
      return saved
    }
    if (call.url.endsWith('/adopt')) return { record: { ...saved, revision: 2, status: 'adopted' }, source: { story_id: saved.id, story_revision: 1, story_sha256: saved.story_sha256 }, title: saved.title, story: saved.story }
    throw new Error('unexpected')
  })
  c.s.createStory('manuscript'); c.s.draft.value.title = '原稿'; c.s.draft.value.story = body
  await c.s.generateStory()
  assert.equal(c.calls.some(item => item.url.endsWith('/develop')), false)
  await c.s.adoptStory()
  assert.equal(saved.story, body)
  assert.equal(c.adopted[0].story, body)
  assert.equal(c.adopted[0].owner, '1')
  assert.equal(c.s.dirty.value, false)
  assert.deepEqual(c.calls.filter(item => item.method === 'POST').map(item => item.url), ['/api/stories', `/api/stories/${saved.id}/adopt`])
})

test('a completed direct picturebook reopens with review and no phantom directions', async t => {
  const saved = record(A, { brief: { intent: 'topics', idea: '', preset: 'picturebook', duration_seconds: 120 },
    editorial_review: 'ready', development: { plan: null, status: 'current', brief_sha256: 'b'.repeat(64) },
    task: { status: 'completed', operation: 'develop', call_count: 2 } })
  saved.review = review(saved)
  const c = await workspace(t, async call => {
    assert.equal(call.method, 'GET')
    return structuredClone(saved)
  })
  await c.s.openStory(A)
  assert.deepEqual(c.s.directions.value, [])
  assert.equal(c.s.currentReview.value.verdict, 'ready')
  assert.equal(c.s.locked.value, false)
  await c.remount()
  assert.equal(c.s.draft.value.story, saved.story)
  assert.deepEqual(c.s.directions.value, [])
  assert.equal(c.calls.some(call => call.method === 'POST'), false)
})

test('local edits survive remount and explicit conflict fork preserves both stories', async t => {
  let server = record()
  const c = await workspace(t, async () => structuredClone(server))
  await c.s.openStory(A)
  c.s.draft.value.story = '作者未保存的修改，完整保留这一段并且绝不能被后台结果覆盖。'
  const localText = c.s.draft.value.story
  assert.equal(c.s.synopsis.value, '')
  assert.equal(c.s.synopsisStale.value, true)
  server = record(A, { revision: 2, story: '另一个窗口已经保存的新故事正文，需要用户明确决定采用哪一篇。' })
  await c.remount()
  assert.equal(c.s.draft.value.story, localText)
  assert.equal(c.s.conflict.value.revision, 2)
  await c.s.saveStory()
  assert.equal(c.calls.some(item => item.method === 'PUT'), false)
  c.s.keepLocalAsNew()
  assert.notEqual(c.s.selectedId.value, A)
  assert.equal(c.s.draft.value.story, localText)
  assert.equal(c.s.draft.value.persisted, false)
  await c.s.openStory(A)
  assert.equal(c.s.draft.value.story, server.story)
})

test('late story reads cannot replace the selected story or cross accounts', async t => {
  const slow = deferred()
  const c = await workspace(t, call => call.url.endsWith(A) ? slow.promise : Promise.resolve(record(B)))
  const first = c.s.openStory(A)
  await c.s.openStory(B)
  slow.resolve(record(A)); await first
  assert.equal(c.s.draft.value.id, B)
  c.s.draft.value.story = '只属于第一个账号的本机修改，不应出现在第二个账号的编辑器里。'
  c.owner.value = '2'; await c.flush()
  assert.equal(c.s.draft.value, null)
  assert.equal(c.s.entries.value.length, 0)
  assert.match(c.browser.localStorage.getItem('ocv.story.drafts.v1:1'), /只属于第一个账号/)
  assert.doesNotMatch(c.browser.localStorage.getItem('ocv.story.drafts.v1:2') || '', /只属于第一个账号/)
})

test('generation runs in background, locks edits, and reopens without another POST', async t => {
  let server = record(), requests = 0
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/develop')) {
      requests++
      server = record(A, { revision: 2, status: 'generating', task: { status: 'running', request_id: call.body.request_id } })
    }
    return structuredClone(server)
  })
  await c.s.openStory(A); await c.s.generateStory()
  assert.equal(c.s.locked.value, true)
  await c.s.saveStory(); await c.s.adoptStory()
  c.s.createStory('topics')
  assert.notEqual(c.s.selectedId.value, A)
  server = record(A, { revision: 3, story: body + '新结局。', task: { status: 'completed', request_id: server.task.request_id } })
  await c.s.openStory(A)
  assert.equal(c.s.draft.value.story, server.story)
  assert.equal(c.s.locked.value, false)
  assert.equal(requests, 1)
  assert.equal(c.adopted.length, 0)
})

test('unknown generation outcome persists its request id and retry deduplicates', async t => {
  let server = record(), count = 0
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/develop')) {
      if (++count === 1) throw new Error('network lost')
      server = record(A, { revision: 2, status: 'generating', task: { status: 'running', request_id: call.body.request_id } })
    }
    return structuredClone(server)
  })
  await c.s.openStory(A); await c.s.generateStory()
  const requestId = c.s.pending.value.request_id
  assert.equal(c.s.locked.value, true)
  await c.remount()
  assert.equal(c.s.pending.value.request_id, requestId)
  await c.s.generateStory()
  const posts = c.calls.filter(item => item.url.endsWith('/develop'))
  assert.equal(posts.length, 2)
  assert.deepEqual(posts[0].body, posts[1].body)
  assert.equal(c.s.pending.value, null)
})

test('LAN HTTP without randomUUID creates valid story ids and reuses its generation retry id', async t => {
  const originalCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  let randomCalls = 0
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: {
    getRandomValues(bytes) {
      assert.ok(bytes instanceof Uint8Array)
      assert.equal(bytes.length, 16)
      bytes.fill(++randomCalls)
      return bytes
    },
  } })
  t.after(() => { if (originalCrypto) Object.defineProperty(globalThis, 'crypto', originalCrypto); else delete globalThis.crypto })
  let saved, attempts = 0
  const c = await workspace(t, async call => {
    if (call.url === '/api/stories' && call.method === 'POST') {
      saved = record(call.body.id, { ...call.body, revision: 1, brief: call.body })
      return saved
    }
    if (call.url.endsWith('/develop')) {
      if (++attempts === 1) throw new Error('response lost')
      saved = { ...saved, revision: 2, status: 'generating', task: { status: 'running', request_id: call.body.request_id } }
    }
    return saved
  })
  c.browser.window.location.href = 'http://192.168.1.20/'
  c.s.createStory('topics')
  const firstId = c.s.draft.value.id
  c.s.createStory('topics')
  const secondId = c.s.draft.value.id
  assert.match(firstId, /^[a-f0-9]{32}$/)
  assert.match(secondId, /^[a-f0-9]{32}$/)
  assert.notEqual(firstId, secondId)
  await c.s.generateStory()
  const requestId = c.s.pending.value.request_id
  assert.match(requestId, /^[a-f0-9]{32}$/)
  assert.notEqual(requestId, secondId)
  assert.equal(c.s.locked.value, true)
  await c.remount()
  await c.s.generateStory()
  const posts = c.calls.filter(item => item.url.endsWith('/develop'))
  assert.equal(posts.length, 2)
  assert.deepEqual(posts[0].body, posts[1].body)
  assert.equal(posts[1].body.request_id, requestId)
  assert.equal(c.s.pending.value, null)
  assert.equal(randomCalls, 3)
})

test('missing browser crypto reports an actionable error and preserves the current manuscript', async t => {
  const c = await workspace(t, async () => record())
  await c.s.openStory(A)
  c.s.draft.value.story = body + '保留本机修改。'
  const originalCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: undefined })
  t.after(() => { if (originalCrypto) Object.defineProperty(globalThis, 'crypto', originalCrypto); else delete globalThis.crypto })
  assert.doesNotThrow(() => c.s.createStory())
  assert.match(c.s.error.value, /浏览器无法创建故事标识/)
  assert.equal(c.s.draft.value.id, A)
  assert.equal(c.s.draft.value.story, body + '保留本机修改。')
})

test('polling recovers after a read failure and reports generation failure without replacing the saved text', async t => {
  let server = record(), failRead = false
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/develop')) server = record(A, { revision: 2, status: 'generating', task: { status: 'running', request_id: call.body.request_id } })
    else if (failRead) { failRead = false; throw new Error('temporary read failure') }
    return structuredClone(server)
  })
  await c.s.openStory(A); await c.s.generateStory()
  failRead = true; c.browser.timers.advance(2000); await c.flush()
  assert.match(c.s.error.value, /temporary read failure/)
  assert.equal(c.s.locked.value, true)
  server = { ...server, revision: 3, status: 'failed', task: { ...server.task, status: 'failed', error: '模型服务失败' } }
  c.browser.timers.advance(2000); await c.flush()
  assert.equal(c.s.record.value.task.error, '模型服务失败')
  assert.equal(c.s.draft.value.story, body)
  assert.equal(c.s.locked.value, false)
  assert.equal(c.s.error.value, '')
  assert.equal(c.calls.filter(item => item.url.endsWith('/develop')).length, 1)
  assert.equal(c.browser.timers.pendingCount, 0)
})

test('save conflicts retain local text and do not retry writes', async t => {
  let server = record()
  const c = await workspace(t, async call => {
    if (call.method === 'PUT') {
      server = record(A, { revision: 2, story: '服务器已经存入另外一份正文，不能拿本机旧版本静默覆盖。' })
      throw Object.assign(new Error('version conflict'), { status: 409 })
    }
    return structuredClone(server)
  })
  await c.s.openStory(A)
  c.s.draft.value.story = '作者的本机修改，即使保存失败也要保留这一整段文字不能丢掉。'
  await c.s.saveStory()
  assert.match(c.s.draft.value.story, /作者的本机修改/)
  assert.equal(c.s.conflict.value.revision, 2)
  assert.equal(c.calls.filter(item => item.method === 'PUT').length, 1)
  c.s.useServerVersion()
  assert.equal(c.s.draft.value.story, server.story)
  assert.equal(c.s.dirty.value, false)
})

test('a retried create returning an advanced server story cannot discard the local manuscript', async t => {
  const c = await workspace(t, async call => record(call.body.id, { revision: 4, story: '在服务器上经过其他编辑的故事正文，与当前本机稿已经不同。' }))
  c.s.createStory('manuscript'); c.s.draft.value.story = body
  await c.s.saveStory()
  assert.equal(c.s.draft.value.story, body)
  assert.equal(c.s.conflict.value.revision, 4)
  assert.equal(c.s.locked.value, true)
})

test('a delayed older read cannot undo a successful save', async t => {
  let slow = null, server = record()
  const c = await workspace(t, async call => {
    if (call.method === 'PUT') { server = record(A, { revision: 2, story: call.body.story }); return structuredClone(server) }
    return slow ? slow.promise : structuredClone(server)
  })
  await c.s.openStory(A)
  slow = deferred()
  const oldRead = c.s.refreshCurrent()
  c.s.draft.value.story = '已经保存的新故事正文，迟到的旧读取不应该恢复之前那一段。'
  await c.s.saveStory()
  slow.resolve(record()); await oldRead
  assert.equal(c.s.record.value.revision, 2)
  assert.equal(c.s.draft.value.story, server.story)
  assert.equal(c.s.dirty.value, false)
})

test('default development and explicit directions use distinct endpoints with one shared task lock', async t => {
  let saved = record()
  const plan = { directions: ['one', 'two', 'three'].map(id => ({ id, title: id, premise: '各有不同的故事走向' })), selected_direction_id: 'two', selection_reason: '因果关系明确' }
  const c = await workspace(t, async call => {
    if (call.method === 'POST') {
      assert.equal(Object.hasOwn(call.body, 'operation'), false)
      saved = { ...saved, revision: saved.revision + 1, status: 'generating', task: { status: 'running', operation: call.url.split('/').at(-1), stage: 'directions', message: '正在比较三个方向', request_id: call.body.request_id } }
    }
    return structuredClone(saved)
  })
  await c.s.openStory(A); await c.s.findDirections()
  assert.equal(c.s.taskMessage.value, '正在比较三个方向')
  await c.s.reviewStory(); await c.s.generateStory()
  assert.equal(c.calls.filter(call => call.method === 'POST').length, 1)
  saved = { ...saved, revision: 3, status: 'directions_ready', task: { ...saved.task, status: 'completed' }, development: { status: 'current', brief_sha256: saved.brief_sha256, plan } }
  c.browser.timers.advance(2000); await c.flush()
  assert.equal(c.s.directions.value.length, 3)
  assert.equal(c.s.directionsCurrent.value, true)
  await c.s.developDirection('two')
  const writes = c.calls.filter(call => call.method === 'POST')
  assert.deepEqual(writes.map(call => call.url), [`/api/stories/${A}/directions`, `/api/stories/${A}/develop`])
  assert.equal(writes[1].body.direction_id, 'two')
  assert.equal(writes[1].body.revision, 3)
})

test('dirty or stale direction plans cannot be developed, and default development requests a fresh plan', async t => {
  let saved = record()
  saved.development = { status: 'current', brief_sha256: saved.brief_sha256, plan: { directions: [{ id: 'one' }] } }
  const c = await workspace(t, async call => {
    if (call.method === 'POST') return { ...saved, revision: saved.revision + 1, status: 'generating', task: { status: 'running', request_id: call.body.request_id } }
    return structuredClone(saved)
  })
  await c.s.openStory(A)
  c.s.draft.value.idea = '我已经改变了想法'
  await c.s.developDirection('one')
  assert.equal(c.calls.filter(call => call.method === 'POST').length, 0)
  c.s.draft.value.idea = saved.brief.idea
  saved.development.status = 'stale'; await c.s.refreshCurrent()
  assert.equal(c.s.directionsCurrent.value, false)
  await c.s.developDirection('one')
  assert.equal(c.calls.filter(call => call.method === 'POST').length, 0)
  await c.s.generateStory()
  assert.equal(c.calls.at(-1).url, `/api/stories/${A}/develop`)
  assert.equal(Object.hasOwn(c.calls.at(-1).body, 'direction_id'), false)
})

test('reviewing an imported manuscript leaves it intact and stale advice never appears current', async t => {
  let saved = record(A, { brief: { intent: 'manuscript', idea: '', preset: 'general', duration_seconds: 90 } })
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/review')) saved = { ...saved, revision: 2, status: 'generating', task: { status: 'running', operation: 'review', request_id: call.body.request_id } }
    return structuredClone(saved)
  })
  await c.s.openStory(A); await c.s.reviewStory()
  assert.equal(c.s.draft.value.story, body)
  saved = { ...saved, revision: 3, status: 'needs_revision', editorial_review: 'needs_revision', review: review(saved, { verdict: 'needs_revision', issues: [{ severity: 'major', dimension: 'causality', quote: '作者亲自写下', reason: '动机不清', suggestion: '交代决定的原因' }] }), task: { ...saved.task, status: 'completed' } }
  c.browser.timers.advance(2000); await c.flush()
  assert.equal(c.s.draft.value.story, body)
  assert.equal(c.s.needsRevision.value, true)
  c.s.draft.value.idea = '新要求'
  assert.equal(c.s.currentReview.value, null)
  assert.equal(c.s.reviewStale.value, true)
  c.s.draft.value.idea = ''
  saved.review.brief_sha256 = 'e'.repeat(64); await c.s.refreshCurrent()
  assert.equal(c.s.currentReview.value, null)
  assert.equal(c.calls.filter(call => call.method === 'POST').length, 1)
})

test('Unicode selections submit codepoint offsets and rewriting keeps the source text until acceptance', async t => {
  const original = '🌟开场：小猫走过桥。\n它把🎁放在门前，等待朋友把门打开，然后一起回家。'
  let saved = record(A, { story: original }), submitted
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/rewrite')) {
      submitted = call.body
      saved = { ...saved, revision: 2, status: 'generating', task: { status: 'running', operation: 'rewrite', request_id: call.body.request_id } }
    }
    if (call.url.endsWith('/rewrite/accept')) saved = { ...saved, revision: 4, story_revision: 2, story_sha256: 'e'.repeat(64), story: saved.rewrite_proposal.story, rewrite_proposal: { ...saved.rewrite_proposal, status: 'accepted' } }
    return structuredClone(saved)
  })
  await c.s.openStory(A)
  const start = original.indexOf('它'), end = original.indexOf('，等待')
  c.s.selectRewriteRange(start, end); c.s.rewriteInstruction.value = '保留礼物，把动作写得更自然。'
  await c.s.rewriteStory()
  assert.equal(submitted.start, Array.from(original.slice(0, start)).length)
  assert.equal(submitted.end, Array.from(original.slice(0, end)).length)
  assert.equal(Object.hasOwn(submitted, 'original'), false)
  assert.equal(Object.hasOwn(submitted, 'operation'), false)
  assert.equal(c.s.draft.value.story, original)
  saved = { ...saved, revision: 3, status: 'ready', task: { ...saved.task, status: 'completed' }, rewrite_proposal: proposal(saved, submitted.start, submitted.end, '它轻轻放下礼物') }
  c.browser.timers.advance(2000); await c.flush()
  assert.equal(c.s.proposalCurrent.value, true)
  assert.equal(c.s.draft.value.story, original)
  await c.s.reviewStory()
  assert.match(c.s.error.value, /先采用或丢弃/)
  const expected = saved.rewrite_proposal.story
  await c.s.resolveRewrite('accept')
  assert.equal(c.s.draft.value.story, expected)
  assert.equal(c.s.record.value.story_revision, 2)
  assert.equal(c.s.rewriteSelection.value, null)
  assert.deepEqual(c.calls.at(-1).body, { revision: 3, proposal_id: 'proposal-one' })
})

test('rewriting requires an intact selection tied to the current text, brief, story and account', async t => {
  const c = await workspace(t, async call => record(call.url.endsWith(B) ? B : A, { story: '🌟这是一个包含中文和表情的完整故事，原始内容不能被错误的偏移量截断。' }))
  await c.s.openStory(A)
  c.s.rewriteInstruction.value = '更自然'
  await c.s.rewriteStory()
  assert.match(c.s.error.value, /先在正文中选中/)
  c.s.selectRewriteRange(1, 5)
  assert.equal(c.s.rewriteSelection.value, null)
  c.s.selectRewriteRange(2, 8)
  assert.equal(c.s.selectionCurrent.value, true)
  c.s.draft.value.story += '新增。'
  assert.equal(c.s.rewriteSelection.value, null)
  c.s.selectWholeStory(); assert.equal(c.s.rewriteSelection.value.whole, true)
  c.s.draft.value.preset = 'warm_relationship'
  assert.equal(c.s.rewriteSelection.value, null)
  c.s.selectWholeStory(); await c.s.openStory(B)
  assert.equal(c.s.rewriteSelection.value, null)
  assert.equal(c.s.rewriteInstruction.value, '')
  c.s.selectWholeStory(); c.owner.value = '2'; await c.flush()
  assert.equal(c.s.rewriteSelection.value, null)
  assert.equal(c.s.rewriteInstruction.value, '')
  assert.equal(c.calls.some(call => call.method === 'POST'), false)
})

test('unknown rewrite outcome retries the same endpoint and payload after remount', async t => {
  let saved = record(), attempts = 0
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/rewrite')) {
      if (++attempts === 1) throw new Error('connection lost')
      saved = { ...saved, revision: 2, status: 'generating', task: { status: 'running', operation: 'rewrite', request_id: call.body.request_id } }
    }
    return structuredClone(saved)
  })
  await c.s.openStory(A); c.s.selectWholeStory(); c.s.rewriteInstruction.value = '整篇压缩，但保留结局。'
  await c.s.rewriteStory()
  assert.equal(c.s.pending.value.operation, 'rewrite')
  await c.remount()
  assert.equal(c.s.rewriteInstruction.value, '整篇压缩，但保留结局。')
  assert.equal(c.s.rewriteSelection.value, null)
  await c.s.retryPending()
  const writes = c.calls.filter(call => call.method === 'POST')
  assert.equal(writes.length, 2)
  assert.equal(writes[0].url, `/api/stories/${A}/rewrite`)
  assert.equal(writes[0].url, writes[1].url)
  assert.deepEqual(writes[0].body, writes[1].body)
  assert.equal(Object.hasOwn(writes[0].body, 'operation'), false)
})

test('an A-version pending request is retried at generate rather than develop', async t => {
  const c = await workspace(t, async call => record(A, call.method === 'POST' ? { revision: 2, status: 'generating', task: { status: 'running', request_id: call.body.request_id } } : {}))
  await c.s.openStory(A)
  const stored = JSON.parse(c.browser.localStorage.getItem('ocv.story.drafts.v1:1'))
  stored.drafts[A].pending = { revision: 1, request_id: 'legacy-request-id' }
  c.browser.localStorage.setItem('ocv.story.drafts.v1:1', JSON.stringify(stored))
  // Loading another story before remount would flush over the old pending entry.
  c.s.pending.value = stored.drafts[A].pending
  await c.remount(); await c.s.retryPending()
  assert.equal(c.calls.at(-1).url, `/api/stories/${A}/generate`)
  assert.deepEqual(c.calls.at(-1).body, { revision: 1, request_id: 'legacy-request-id' })
})

test('stale or source-mismatched proposals cannot be adopted and discarding preserves local edits', async t => {
  let saved = record(); saved.rewrite_proposal = proposal(saved)
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/rewrite/discard')) saved = { ...saved, revision: saved.revision + 1, rewrite_proposal: { ...saved.rewrite_proposal, status: 'discarded' } }
    return structuredClone(saved)
  })
  await c.s.openStory(A)
  assert.equal(c.s.proposalCurrent.value, true)
  c.s.draft.value.story += '作者自己补充的一句。'
  const localText = c.s.draft.value.story
  await c.s.resolveRewrite('accept')
  assert.equal(c.calls.some(call => call.method === 'POST'), false)
  await c.s.resolveRewrite('discard')
  assert.equal(c.s.draft.value.story, localText)
  assert.equal(c.s.dirty.value, true)
  assert.equal(c.s.draft.value.revision, 2)
  c.s.draft.value.story = saved.story
  saved = { ...saved, rewrite_proposal: { ...proposal(saved), base_brief_sha256: 'e'.repeat(64) } }
  await c.s.refreshCurrent(); await c.s.resolveRewrite('accept')
  assert.equal(c.s.proposalCurrent.value, false)
  saved.rewrite_proposal = { ...proposal(saved), story: '错误拼接，外围内容也被改了。' }
  await c.s.refreshCurrent(); await c.s.resolveRewrite('accept')
  assert.equal(c.s.proposalCurrent.value, false)
  saved.rewrite_proposal = { ...proposal(saved), status: 'stale' }
  await c.s.refreshCurrent(); await c.s.resolveRewrite('accept')
  assert.equal(c.calls.filter(call => call.url.endsWith('/rewrite/accept')).length, 0)
})

test('a rewrite acceptance conflict keeps the original text and reads the stale proposal without retry', async t => {
  let saved = record(); saved.rewrite_proposal = proposal(saved)
  const c = await workspace(t, async call => {
    if (call.url.endsWith('/rewrite/accept')) {
      saved = { ...saved, revision: 2, rewrite_proposal: { ...saved.rewrite_proposal, status: 'stale' } }
      throw Object.assign(new Error('proposal expired'), { status: 409 })
    }
    return structuredClone(saved)
  })
  await c.s.openStory(A); await c.s.resolveRewrite('accept')
  assert.equal(c.s.draft.value.story, body)
  assert.equal(c.s.proposalCurrent.value, false)
  assert.equal(c.s.rewriteProposal.value.status, 'stale')
  assert.equal(c.calls.filter(call => call.method === 'POST').length, 1)
})

test('late rewrite acceptance cannot replace the next selected story', async t => {
  const gate = deferred(), saved = record(); saved.rewrite_proposal = proposal(saved)
  const c = await workspace(t, async call => call.url.endsWith('/rewrite/accept') ? gate.promise : call.url.endsWith(B) ? record(B, { story: '另外一个故事，任何旧故事的修改都不应该替换这里的正文。' }) : saved)
  await c.s.openStory(A)
  const acceptance = c.s.resolveRewrite('accept')
  await c.s.openStory(B)
  gate.resolve({ ...saved, revision: 2, story: saved.rewrite_proposal.story })
  await acceptance
  assert.equal(c.s.draft.value.id, B)
  assert.match(c.s.draft.value.story, /另外一个故事/)
})

test('an interrupted task exposes its last completed candidate for reading without replacing the author manuscript', async t => {
  const candidate = '中断前已完成的候选正文，只供查看，尚未通过后续审稿也没有替换作者原文。'
  const saved = record(A, { status: 'interrupted', task: { status: 'interrupted', operation: 'develop', checkpoints: [
    { stage: 'planning', directions: [{ title: '方向一' }, { title: '方向二' }, { title: '方向三' }] },
    { stage: 'writing', draft: { title: '初稿', story: '较早的一稿，不应挤掉最后完成的候选。' } },
    { stage: 'revising', draft: { title: '最后候选', story: candidate } },
  ] } })
  const c = await workspace(t, async call => call.url.endsWith(B) ? record(B) : saved)
  await c.s.openStory(A)
  assert.equal(c.s.failedWork.value.story, candidate)
  assert.deepEqual(c.s.failedWork.value.directions, ['方向一', '方向二', '方向三'])
  assert.deepEqual(c.s.failedWork.value.stages, ['构思方向', '写作初稿', '生成修订候选'])
  assert.equal(c.s.draft.value.story, body)
  assert.equal(c.s.currentReview.value, null)
  await c.s.openStory(B)
  assert.equal(c.s.failedWork.value, null)
  assert.equal(c.calls.some(call => call.method !== 'GET'), false)
})

for (const accountSwitch of [false, true]) {
  test(`late adoption cannot navigate after ${accountSwitch ? 'account' : 'story'} switch`, async t => {
    const adopt = deferred()
    const c = await workspace(t, async call => call.url.endsWith('/adopt') ? adopt.promise : record())
    await c.s.openStory(A)
    const task = c.s.adoptStory()
    await c.flush()
    if (accountSwitch) c.owner.value = '2'
    else c.s.createStory()
    adopt.resolve({ record: record(), source: { story_id: A }, story: body })
    await task
    assert.equal(c.adopted.length, 0)
  })
}
