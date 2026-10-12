import { createRenderer, nextTick } from 'vue'
import { setImmediate as nextTurn } from 'node:timers/promises'

export function fakeStorage(initial = {}) {
  const values = new Map(Object.entries(initial).map(([key, value]) => [key, String(value)]))
  return {
    get length() { return values.size },
    key(index) { return [...values.keys()][index] ?? null },
    getItem(key) { return values.get(String(key)) ?? null },
    setItem(key, value) { values.set(String(key), String(value)) },
    removeItem(key) { values.delete(String(key)) },
    clear() { values.clear() },
  }
}

export function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

// Only timers are virtual: Date/performance remain real. Intervals never run
// unless the test advances time, so mounting cannot accidentally poll a server.
export function fakeTimers() {
  let now = 0, sequence = 0
  const scheduled = new Map()
  function schedule(callback, delay, args, interval) {
    const id = ++sequence
    const wait = Math.max(interval ? 1 : 0, Number(delay) || 0)
    scheduled.set(id, { callback, args, due: now + wait, interval: interval ? wait : 0 })
    return id
  }
  return {
    setTimeout: (callback, delay = 0, ...args) => schedule(callback, delay, args, false),
    setInterval: (callback, delay = 0, ...args) => schedule(callback, delay, args, true),
    clearTimeout: (id) => scheduled.delete(id),
    clearInterval: (id) => scheduled.delete(id),
    get pendingCount() { return scheduled.size },
    advance(milliseconds) {
      if (!Number.isFinite(milliseconds) || milliseconds < 0) throw new Error('Expected a nonnegative finite duration')
      const end = now + milliseconds
      let calls = 0
      while (true) {
        const entry = [...scheduled].sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0]
        if (!entry || entry[1].due > end) break
        if (++calls > 1000) throw new Error('Timer callback limit exceeded')
        const [id, task] = entry
        now = task.due
        if (task.interval) task.due += task.interval
        else scheduled.delete(id)
        task.callback(...task.args)
      }
      now = end
    },
    clear() { scheduled.clear() },
  }
}

function element(tag = 'div') {
  const node = Object.assign(new EventTarget(), {
    tagName: tag.toUpperCase(), style: {}, children: [], parentNode: null,
    appendChild(child) { child.remove(); this.children.push(child); child.parentNode = this; return child },
    append(...children) { for(const child of children)this.appendChild(child) },
    contains(child) { return child===this||this.children.some(node=>node.contains(child)) },
    get isConnected() { return Boolean(this.parentNode) },
    remove() {
      if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this)
      this.parentNode = null
    },
    setAttribute(name, value) { this[name] = String(value) },
    getAttribute(name) { return this[name] ?? null },
    removeAttribute(name) { delete this[name] },
    querySelector() { return null }, querySelectorAll() { return [] },
    click() { this.onclick?.({target:this}) }, focus() {}, scrollIntoView() {},
  })
  return node
}

class FakeAudio extends EventTarget {
  constructor(src = '') {
    super()
    Object.assign(this, { src, currentTime: 0, duration: 0, paused: true, volume: 1 })
  }
  async play() { this.paused = false; this.dispatchEvent(new Event('play')) }
  pause() { this.paused = true }
  load() {}
  removeAttribute(name) { if (name === 'src') this.src = '' }
}

let browserInstalled = false

// A test file must use sequential tests while globals are installed. Node's
// test-file process isolation lets separate files run in parallel safely.
export function installBrowserGlobals({
  localStorage = fakeStorage(), sessionStorage = fakeStorage(), timers = fakeTimers(),
  fetch = async (url) => { throw new Error(`Unexpected fetch in composable test: ${url}`) },
  confirm = () => true, autoConfirmActions = true,
} = {}) {
  if (browserInstalled) throw new Error('Browser globals already installed; clean up the previous test first')
  browserInstalled = true
  const originals = new Map()
  const document = Object.assign(new EventTarget(), {
    title: 'OCV test', hidden: false, visibilityState: 'visible',
    body: element('body'), head: element('head'), documentElement: element('html'),
    createElement: element, querySelector() { return null }, querySelectorAll() { return [] },
    getElementById() { return null },
  })
  const window = Object.assign(new EventTarget(), {
    document, localStorage, sessionStorage, Audio: FakeAudio, confirm,
    alert() {}, prompt: () => null, scrollTo() {}, focus() {},
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    setInterval: timers.setInterval, clearInterval: timers.clearInterval,
    requestAnimationFrame: (callback) => timers.setTimeout(() => callback(performance.now()), 16),
    cancelAnimationFrame: timers.clearTimeout,
    location: { href: 'http://localhost/', origin: 'http://localhost/' },
  })
  // Existing controller tests express the confirmation decision through their
  // confirm stub. Apply the same decision to the in-page dialog buttons.
  if(autoConfirmActions){
    const append=document.body.append.bind(document.body)
    document.body.append=(...nodes)=>{
      append(...nodes)
      for(const overlay of nodes)if(overlay.className==='ocv-action-confirm')queueMicrotask(()=>{
        const panel=overlay.children[0],message=panel.children[1].textContent
        panel.children[2].children[globalThis.confirm(message)?1:0].click()
      })
    }
  }
  window.window = window
  const replacements = {
    window, document, localStorage, sessionStorage, Audio: FakeAudio, fetch, confirm,
    navigator: { clipboard: { writeText: async () => {} } },
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    setInterval: timers.setInterval, clearInterval: timers.clearInterval,
    requestAnimationFrame: window.requestAnimationFrame, cancelAnimationFrame: window.cancelAnimationFrame,
  }
  for (const [name, value] of Object.entries(replacements)) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name))
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  }
  let restored = false
  return {
    window, document, localStorage, sessionStorage, timers,
    restore() {
      if (restored) return
      restored = true
      timers.clear()
      for (const [name, descriptor] of originals) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor)
        else delete globalThis[name]
      }
      browserInstalled = false
    },
  }
}

function memoryNode(type, text = '') { return { type, text, children: [], parent: null } }
const renderer = createRenderer({
  createElement: (tag) => memoryNode(tag),
  createText: (text) => memoryNode('text', text),
  createComment: (text) => memoryNode('comment', text),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => { node.text = text; node.children = [] },
  patchProp: (node, key, previous, value) => { node[key] = value },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  insert(node, parent, anchor = null) {
    if (node.parent) node.parent.children = node.parent.children.filter((child) => child !== node)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(node)
    else parent.children.splice(index, 0, node)
    node.parent = parent
  },
  remove(node) {
    if (node.parent) node.parent.children = node.parent.children.filter((child) => child !== node)
    node.parent = null
  },
})

export async function flushPromises() {
  await nextTick()
  await nextTurn()
  await nextTick()
}

// setup, watchers and lifecycle hooks execute in an actual Vue component.
// instance is the raw composable return value: refs are deliberately not unwrapped.
export function mountComposable(factory) {
  let instance, mounted = false
  const errors = []
  const app = renderer.createApp({ setup() { instance = factory(); return () => null } })
  app.config.errorHandler = (error) => { errors.push(error) }
  app.mount(memoryNode('root'))
  mounted = true
  if (errors.length) { app.unmount(); throw errors.shift() }
  return {
    instance, errors,
    async flush() { await flushPromises(); if (errors.length) throw errors.shift() },
    unmount() {
      if (mounted) { mounted = false; app.unmount() }
      if (errors.length) throw errors.shift()
    },
  }
}
