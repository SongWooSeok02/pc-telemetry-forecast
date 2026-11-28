import { runQueryActivity } from '../src/queryActivity.ts'
import assert from 'node:assert/strict'
import test from 'node:test'
import { bindDashboardVisibility, QueryScheduler, type SchedulerClock } from '../src/queryScheduler.ts'

class FakeClock implements SchedulerClock {
  private time = 0
  private nextId = 0
  private readonly jobs = new Map<number, { due: number; callback: () => void }>()

  now() { return this.time }
  setTimeout(callback: () => void, delayMs: number) {
    const id = ++this.nextId
    this.jobs.set(id, { due: this.time + delayMs, callback })
    return id as unknown as ReturnType<typeof setTimeout>
  }
  clearTimeout(timer: ReturnType<typeof setTimeout>) { this.jobs.delete(timer as unknown as number) }
  pendingCount() { return this.jobs.size }

  async advanceBy(ms: number) {
    const target = this.time + ms
    for (;;) {
      const next = [...this.jobs.entries()].sort((a, b) => a[1].due - b[1].due)[0]
      if (!next || next[1].due > target) break
      this.jobs.delete(next[0])
      this.time = next[1].due
      next[1].callback()
      await flush()
    }
    this.time = target
    await flush()
  }
}

async function flush() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((done) => { resolve = done })
  return { promise, resolve }
}

test('manual mode fetches on registration and explicit refresh only', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let calls = 0
  scheduler.setVisible(true)
  scheduler.setPeriod('manual')
  scheduler.register('current', async () => { calls += 1 })
  await flush()
  assert.equal(calls, 1)
  await clock.advanceBy(60_000)
  assert.equal(calls, 1)
  scheduler.refreshNow()
  await flush()
  assert.equal(calls, 2)
  scheduler.dispose()
})

test('slow requests do not overlap or accumulate missed periods', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const first = deferred()
  const starts: number[] = []
  let inFlight = 0
  let maxInFlight = 0
  scheduler.setVisible(true)
  scheduler.setPeriod(3)
  scheduler.register('current', async () => {
    starts.push(clock.now())
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    if (starts.length === 1) await first.promise
    inFlight -= 1
  })
  await flush()
  await clock.advanceBy(30_000)
  assert.equal(starts.length, 1)
  first.resolve()
  await flush()
  await clock.advanceBy(0)
  assert.equal(starts.length, 2)
  assert.equal(starts[1], 30_000)
  assert.equal(maxInFlight, 1)
  scheduler.dispose()
})

test('current and history queries share the single in-flight slot', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const currentBlocked = deferred()
  const starts: string[] = []
  let inFlight = 0
  let maximum = 0
  scheduler.setVisible(true)
  scheduler.register('current', async () => {
    starts.push('current')
    inFlight += 1
    maximum = Math.max(maximum, inFlight)
    await currentBlocked.promise
    inFlight -= 1
  })
  scheduler.register('history', async () => {
    starts.push('history')
    inFlight += 1
    maximum = Math.max(maximum, inFlight)
    inFlight -= 1
  })
  await flush()
  assert.deepEqual(starts, ['current'])
  currentBlocked.resolve()
  await flush()
  assert.deepEqual(starts, ['current', 'history'])
  assert.equal(maximum, 1)
  scheduler.dispose()
})

test('refresh during an in-flight request coalesces into one follow-up request', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const first = deferred()
  let calls = 0
  let inFlight = 0
  let maximum = 0
  scheduler.setVisible(true)
  scheduler.register('current', async () => {
    calls += 1
    inFlight += 1
    maximum = Math.max(maximum, inFlight)
    if (calls === 1) await first.promise
    inFlight -= 1
  })
  await flush()
  scheduler.refreshNow()
  scheduler.refreshNow()
  assert.equal(calls, 1)
  first.resolve()
  await flush()
  assert.equal(calls, 2)
  assert.equal(maximum, 1)
  scheduler.dispose()
})

test('manual mode cancels polling and changing back to automatic mode refreshes immediately', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let calls = 0
  scheduler.setVisible(true)
  scheduler.setPeriod(3)
  scheduler.register('current', async () => { calls += 1 })
  await flush()
  assert.equal(calls, 1)
  scheduler.setPeriod(10)
  await flush()
  assert.equal(calls, 2)
  scheduler.setPeriod('manual')
  await clock.advanceBy(30_000)
  assert.equal(calls, 2)
  scheduler.setPeriod(5)
  await flush()
  assert.equal(calls, 3)
  await clock.advanceBy(4_999)
  assert.equal(calls, 3)
  await clock.advanceBy(1)
  assert.equal(calls, 4)
  scheduler.dispose()
})

test('replacing a query aborts its obsolete request before running the new identity', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const oldResponse = deferred()
  let oldSignal: AbortSignal | undefined
  const starts: string[] = []
  scheduler.setVisible(true)
  const unregister = scheduler.register('current', async (signal) => {
    oldSignal = signal
    starts.push('pc-a')
    await oldResponse.promise
  })
  await flush()
  unregister()
  scheduler.register('current', async () => { starts.push('pc-b') })
  assert.equal(oldSignal?.aborted, true)
  assert.deepEqual(starts, ['pc-a'])
  oldResponse.resolve()
  await flush()
  assert.deepEqual(starts, ['pc-a', 'pc-b'])
  scheduler.dispose()
})

test('hidden state stops automatic polling; restore refreshes only in automatic mode', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let calls = 0
  scheduler.setVisible(true)
  scheduler.setPeriod(3)
  scheduler.register('current', async () => { calls += 1 })
  await flush()
  scheduler.setVisible(false)
  await clock.advanceBy(30_000)
  assert.equal(calls, 1)
  scheduler.setVisible(true)
  await flush()
  assert.equal(calls, 2)
  scheduler.setPeriod('manual')
  scheduler.setVisible(false)
  scheduler.setVisible(true)
  await flush()
  await clock.advanceBy(30_000)
  assert.equal(calls, 2)
  scheduler.refreshNow()
  await flush()
  assert.equal(calls, 3)
  scheduler.dispose()
})

test('dispose aborts in-flight work and clears the scheduled timer', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const blocked = deferred()
  let signal: AbortSignal | undefined
  scheduler.setVisible(true)
  scheduler.register('current', async (requestSignal) => { signal = requestSignal; await blocked.promise })
  await flush()
  scheduler.dispose()
  assert.equal(signal?.aborted, true)
  assert.equal(clock.pendingCount(), 0)
})

test('document and WebView host visibility events gate requests', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const fakeDocument = new EventTarget() as Document
  Object.defineProperty(fakeDocument, 'visibilityState', { value: 'visible', writable: true })
  const webview = new EventTarget()
  const fakeWindow = new EventTarget() as Window
  Object.defineProperty(fakeWindow, 'chrome', { value: { webview } })
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'document', { configurable: true, value: fakeDocument })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: fakeWindow })
  let calls = 0
  try {
    scheduler.setPeriod(3)
    const unbind = bindDashboardVisibility(scheduler)
    scheduler.register('current', async () => { calls += 1 })
    await flush()
    assert.equal(calls, 1)
    webview.dispatchEvent(new MessageEvent('message', { data: { type: 'pc-telemetry:host-visibility', visible: false } }))
    await clock.advanceBy(30_000)
    assert.equal(calls, 1)
    webview.dispatchEvent(new MessageEvent('message', { data: { type: 'pc-telemetry:host-visibility', visible: true } }))
    await flush()
    assert.equal(calls, 2)
    Object.defineProperty(fakeDocument, 'visibilityState', { value: 'hidden' })
    fakeDocument.dispatchEvent(new Event('visibilitychange'))
    await clock.advanceBy(30_000)
    assert.equal(calls, 2)
    unbind()
    scheduler.dispose()
  } finally {
    if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument)
    else Reflect.deleteProperty(globalThis, 'document')
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  }
})

for (const surface of ['web', 'host'] as const) {
  for (const query of ['current', 'history'] as const) {
    test(`manual ${query} cancellation restores display on ${surface} hide without resume fetch`, async () => {
      const scheduler = new QueryScheduler(new FakeClock())
      const fakeDocument = new EventTarget() as Document
      Object.defineProperty(fakeDocument, 'visibilityState', { value: 'visible', writable: true })
      const webview = new EventTarget()
      const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
      const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
      Object.defineProperty(globalThis, 'document', { configurable: true, value: fakeDocument })
      Object.defineProperty(globalThis, 'window', { configurable: true, value: { chrome: { webview } } })
      const blocked = deferred()
      let calls = 0
      let state: string | boolean = query === 'current' ? 'ready' : false
      const previous = state
      const busy = query === 'current' ? 'loading' : true
      const lastData = { value: 42 }
      let data = lastData
      let error: string | null = 'previous error'
      let unbind = () => {}
      try {
        scheduler.setPeriod('manual')
        unbind = bindDashboardVisibility(scheduler)
        scheduler.register(query, (signal) => runQueryActivity(signal, {
          get: () => state, set: (value) => { state = value }, busy,
          onCancel: () => { error = 'previous error' },
        }, async () => {
          calls += 1
          error = null
          await blocked.promise
          if (!signal.aborted) data = { value: 99 }
        }))
        await flush()
        assert.equal(state, busy)
        const visibility = (visible: boolean) => {
          if (surface === 'host') webview.dispatchEvent(new MessageEvent('message', { data: { type: 'pc-telemetry:host-visibility', visible } }))
          else {
            Object.defineProperty(fakeDocument, 'visibilityState', { value: visible ? 'visible' : 'hidden', writable: true })
            fakeDocument.dispatchEvent(new Event('visibilitychange'))
          }
        }
        visibility(false)
        assert.equal(state, previous, 'clear busy immediately before aborted transport settles')
        assert.equal(error, 'previous error')
        visibility(true)
        blocked.resolve()
        await flush()
        assert.equal(calls, 1, 'manual restore must not fetch')
        assert.equal(state, previous)
        assert.equal(data, lastData, 'cancelled response must not replace displayed data')
        scheduler.refreshNow()
        await flush()
        assert.equal(calls, 2)
      } finally {
        unbind()
        scheduler.dispose()
        if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument)
        else Reflect.deleteProperty(globalThis, 'document')
        if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
        else Reflect.deleteProperty(globalThis, 'window')
      }
    })
  }
}

test('manual failure clears activity without automatic retry', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let busy = false
  let calls = 0
  scheduler.setPeriod('manual')
  scheduler.setVisible(true)
  scheduler.register('history', (signal) => runQueryActivity(signal, {
    get: () => busy, set: (value) => { busy = value }, busy: true,
  }, async () => { calls += 1; throw new Error('server failure') }))
  await flush()
  assert.equal(busy, false)
  await clock.advanceBy(60_000)
  assert.equal(calls, 1)
  assert.equal(clock.pendingCount(), 0)
  scheduler.dispose()
})

test('late completion of cancelled activity cannot clear newer busy state', async () => {
  let state = 'error'
  const activity = { get: () => state, set: (value: string) => { state = value }, busy: 'loading' }
  const old = new AbortController()
  const blocked = deferred()
  const pending = runQueryActivity(old.signal, activity, () => blocked.promise)
  old.abort()
  assert.equal(state, 'error')
  const newer = new AbortController()
  const newerBlocked = deferred()
  const newerPending = runQueryActivity(newer.signal, activity, () => newerBlocked.promise)
  blocked.resolve()
  await pending
  assert.equal(state, 'loading')
  newerBlocked.resolve()
  await newerPending
  assert.equal(state, 'error')
})

test('task registered and unregistered or disposed in the same tick is never invoked', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let removedCalls = 0
  let replacementCalls = 0
  scheduler.setVisible(true)
  const unregister = scheduler.register('current', () => { removedCalls += 1 })
  unregister()
  scheduler.register('current', () => { replacementCalls += 1 })
  await flush()
  assert.equal(removedCalls, 0)
  assert.equal(replacementCalls, 1)
  scheduler.dispose()

  const disposedScheduler = new QueryScheduler(clock)
  let disposedCalls = 0
  disposedScheduler.setVisible(true)
  disposedScheduler.register('current', () => { disposedCalls += 1 })
  disposedScheduler.dispose()
  await flush()
  assert.equal(disposedCalls, 0)
})
