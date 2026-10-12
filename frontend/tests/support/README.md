# Composable tests

Run from `frontend` with Node 20.6+:

```powershell
node --import ./tests/support/registerModuleLoader.mjs --test tests/*.test.mjs
```

The loader only supplies `.js` for extensionless relative imports. Application
code and the harness import the same installed Vue instance; no source extraction,
application rewriting, Vite server, or additional dependency is required.

```js
import test from 'node:test'
import assert from 'node:assert/strict'
import { useWorkspace } from '../src/useWorkspace.js'
import { useStudio } from '../src/useStudio.js'
import { installBrowserGlobals, mountComposable } from './support/composableHarness.mjs'

test('a new project preserves its draft', async (t) => {
  const browser = installBrowserGlobals({
    fetch: async (url) => {
      if (url === '/api/session') return Response.json({ user: null, auth_mode: 'account' })
      if (url === '/api/settings' || url === '/api/health') return Response.json({})
      throw new Error(`Unexpected request: ${url}`)
    },
  })
  let mounted
  t.after(() => {
    try { mounted?.unmount() } finally { browser.restore() }
  })
  mounted = mountComposable(() => {
    const workspace = useWorkspace()
    return { workspace, studio: useStudio(workspace) }
  })
  await mounted.flush()
  const { workspace, studio } = mounted.instance
  studio.newProject('video')
  workspace.form.script = 'A saved story'
  await mounted.flush()
  browser.timers.advance(650)
  await mounted.flush()
  assert.equal(studio.studioDrafts.value[0].form.script, 'A saved story')
})
```

- `instance` preserves raw refs. Lifecycle hooks and Vue watchers really run.
- `flush()` drains Vue updates and settled promise work, and reports captured Vue
  errors. It does not resolve promises held by a test's `deferred()` response.
- `deferred()` returns `{ promise, resolve, reject }` for controlling response order.
- `fakeStorage(initial)` can be passed back into another browser installation to
  exercise page reload. Each installation otherwise gets fresh storage.
- `timers.advance(ms)` executes due callbacks; follow it with `await mounted.flush()`
  for async work. It does not fake `Date` or wait for unresolved network responses.
- Unmount before restoring browser globals. Cleanup restores original descriptors
  and clears all virtual timers. Do not run tests concurrently inside the same
  file while globals are installed; separate Node test files are process-isolated.
- Browser stubs cover storage, events, audio state and basic element operations.
  They do not test rendering, layout, actual playback or download behavior.
