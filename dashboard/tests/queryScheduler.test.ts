import { QueryError, QueryVersion, responseError } from '../src/queryErrors.ts'
import { runQueryActivity } from '../src/queryActivity.ts'
import { BrowserSession } from '../src/browserSession.ts'
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

test('an expired session cancels another query’s pending retry', async (t) => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  t.after(() => scheduler.dispose())
  const reasons: string[] = []
  const session = new BrowserSession('a'.repeat(64), (reason) => {
    reasons.push(reason)
    scheduler.dispose()
  })
  const requests: string[] = []
  const statuses = [503, 401]
  t.mock.method(globalThis, 'fetch', (path: string) => {
    requests.push(path)
    const status = statuses.shift()
    return Promise.resolve(Response.json({ error: { code: status === 503
      ? 'SERVICE_UNAVAILABLE' : 'USER_SESSION_EXPIRED' } }, { status }))
  })
  const query = (path: string) => async (signal: AbortSignal) => {
    const response = await session.request(path, signal)
    if (!response.ok) throw await responseError(response)
  }

  scheduler.setVisible(true)
  scheduler.register('current', query('/current'))
  await flush()
  assert.equal(scheduler.status()[0].retryInMs, 5000)
  scheduler.register('history', query('/history'))
  await flush()
  assert.deepEqual(reasons, ['expired'])
  assert.equal(clock.pendingCount(), 0)
  await clock.advanceBy(60_000)
  assert.deepEqual(requests, ['/current', '/history'])
})

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

test('automatic backoff grows 5/10/20/40/60 seconds and success restores 5 seconds', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const starts: number[] = []
  scheduler.setVisible(true)
  scheduler.register('history', async () => {
    starts.push(clock.now())
    if (starts.length <= 6) throw new Error('temporary failure')
  })
  await flush()
  for (const delay of [5000, 10000, 20000, 40000, 60000, 60000]) {
    assert.equal(clock.pendingCount(), 1)
    const count = starts.length
    await clock.advanceBy(delay - 1)
    assert.equal(starts.length, count)
    await clock.advanceBy(1)
    assert.equal(starts.length, count + 1)
  }
  assert.deepEqual(starts, [0, 5000, 15000, 35000, 75000, 135000, 195000])
  assert.equal(scheduler.status()[0].failures, 0)
  await clock.advanceBy(5000)
  assert.equal(starts.at(-1), 200000)
  scheduler.dispose()
})

test('slow failure starts retry delay after completion and does not accumulate requests', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  const waiting = deferred()
  let calls = 0
  scheduler.setVisible(true)
  scheduler.register('current', async () => { calls++; if (calls === 1) { await waiting.promise; throw new Error('slow failure') } })
  await flush()
  await clock.advanceBy(30000)
  assert.equal(calls, 1)
  waiting.resolve()
  await flush()
  await clock.advanceBy(4999)
  assert.equal(calls, 1)
  await clock.advanceBy(1)
  assert.equal(calls, 2)
  scheduler.dispose()
})

test('manual selection cancels retry, and hidden period updates cannot leave an automatic initial request', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let calls = 0
  scheduler.setVisible(true)
  scheduler.register('current', async () => { calls++; throw new Error('network') })
  await flush()
  scheduler.setPeriod('manual')
  assert.equal(clock.pendingCount(), 0)
  await clock.advanceBy(120000)
  assert.equal(calls, 1)
  scheduler.setVisible(false)
  scheduler.setPeriod(10)
  scheduler.setPeriod('manual')
  scheduler.setVisible(true)
  await flush()
  assert.equal(calls, 1)
  scheduler.refreshNow()
  await flush()
  assert.equal(calls, 2)
  assert.equal(clock.pendingCount(), 0)
  scheduler.dispose()
})

test('changing automatic period while retry waits respects new lower bound', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let calls = 0
  scheduler.setVisible(true)
  scheduler.register('current', async () => { calls++; throw new Error('network') })
  await flush()
  scheduler.setPeriod(30)
  await clock.advanceBy(29999)
  assert.equal(calls, 1)
  await clock.advanceBy(1)
  assert.equal(calls, 2)
  assert.equal(scheduler.status()[0].retryInMs, 60000)
  scheduler.dispose()
})

test('permanent authentication/permission errors have no automatic polling or resume retry', async () => {
  for (const status of [401, 403, 404, 422]) {
    const clock = new FakeClock()
    const scheduler = new QueryScheduler(clock)
    let calls = 0
    scheduler.setVisible(true)
    scheduler.register('current', async () => { calls++; throw new QueryError('permanent', status, 'DENIED', false) })
    await flush()
    await clock.advanceBy(120000)
    scheduler.setVisible(false)
    scheduler.setPeriod(10)
    scheduler.setVisible(true)
    await flush()
    assert.equal(calls, 1)
    assert.equal(clock.pendingCount(), 0)
    scheduler.refreshNow()
    await flush()
    assert.equal(calls, 2)
    scheduler.dispose()
  }
})

test('HTTP classification cannot be widened by retryable flag on authentication errors', async () => {
  for (const status of [401, 403, 404, 422]) {
    const error = await responseError({ status, json: async () => ({ error: { code: 'USER_SESSION_EXPIRED', retryable: true } }) })
    assert.equal(error.retryable, false)
  }
  assert.equal((await responseError({ status: 503, json: async () => ({}) })).retryable, true)
  assert.equal((await responseError({ status: 503, json: async () => ({ error: { retryable: false } }) })).retryable, false)
})

test('scope versions reject old PC responses including A to B to A and cancelled reads', () => {
  const version = new QueryVersion()
  const signal = new AbortController()
  version.setScope('pc-a|sensor|range')
  const old = version.capture('pc-a|sensor|range')
  version.setScope('pc-b|sensor|range')
  assert.equal(version.accepts(old, signal.signal), false)
  version.setScope('pc-a|sensor|range')
  assert.equal(version.accepts(old, signal.signal), false)
  assert.equal(version.accepts(version.capture('wrong'), signal.signal), false)
  const current = version.capture('pc-a|sensor|range')
  assert.equal(version.accepts(current, signal.signal), true)
  signal.abort()
  assert.equal(version.accepts(current, signal.signal), false)
})

for (const surface of ['web', 'host'] as const) {
  test(`retry wait, hidden period change and automatic restore on ${surface}`, async () => {
    const clock = new FakeClock()
    const scheduler = new QueryScheduler(clock)
    const fakeDocument = new EventTarget() as Document
    Object.defineProperty(fakeDocument, 'visibilityState', { value: 'visible', writable: true })
    const webview = new EventTarget()
    const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
    const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
    Object.defineProperty(globalThis, 'document', { configurable: true, value: fakeDocument })
    Object.defineProperty(globalThis, 'window', { configurable: true, value: { chrome: { webview } } })
    let unbind = () => {}
    let calls = 0
    try {
      unbind = bindDashboardVisibility(scheduler)
      scheduler.register('history', async () => { calls++; if (calls < 3) throw new Error('network') })
      await flush()
      const visible = (value: boolean) => {
        if (surface === 'host') webview.dispatchEvent(new MessageEvent('message', { data: { type: 'pc-telemetry:host-visibility', visible: value } }))
        else {
          Object.defineProperty(fakeDocument, 'visibilityState', { value: value ? 'visible' : 'hidden', writable: true })
          fakeDocument.dispatchEvent(new Event('visibilitychange'))
        }
      }
      visible(false)
      scheduler.setPeriod(10)
      await clock.advanceBy(120000)
      assert.equal(calls, 1)
      assert.equal(clock.pendingCount(), 0)
      visible(true)
      await flush()
      assert.equal(calls, 2)
      await clock.advanceBy(20000)
      assert.equal(calls, 3)
      await clock.advanceBy(10000)
      assert.equal(calls, 4)
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

test('PC replacement cancels old backoff and starts the new scope without inherited failures', async () => {
  const clock = new FakeClock()
  const scheduler = new QueryScheduler(clock)
  let oldCalls = 0, newCalls = 0
  scheduler.setVisible(true)
  const remove = scheduler.register('current', async () => { oldCalls++; throw new Error('offline A') })
  await flush()
  remove()
  scheduler.register('current', async () => { newCalls++ })
  await flush()
  assert.equal(newCalls, 1)
  assert.equal(scheduler.status()[0].failures, 0)
  await clock.advanceBy(5000)
  assert.equal(oldCalls, 1)
  assert.equal(newCalls, 2)
  scheduler.dispose()
})

for (const surface of ['web', 'host'] as const) {
  test(`late PC response, hidden period update and restore reject old display on ${surface}`, async () => {
    const clock = new FakeClock()
    const scheduler = new QueryScheduler(clock)
    const fakeDocument = new EventTarget() as Document
    Object.defineProperty(fakeDocument, 'visibilityState', { value: 'visible', writable: true })
    const webview = new EventTarget()
    const oldDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
    const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
    Object.defineProperty(globalThis, 'document', { configurable: true, value: fakeDocument })
    Object.defineProperty(globalThis, 'window', { configurable: true, value: { chrome: { webview } } })
    const version = new QueryVersion()
    const waiting = deferred()
    let shown = '', newCalls = 0
    let unbind = () => {}
    try {
      unbind = bindDashboardVisibility(scheduler)
      version.setScope('A')
      const remove = scheduler.register('current', async (signal) => {
        const token = version.capture('A')
        await waiting.promise
        if (version.accepts(token, signal)) shown = 'A'
      })
      await flush()
      version.setScope('B')
      remove()
      scheduler.register('current', async () => { newCalls++; shown = 'B' })
      const visible = (value: boolean) => {
        if (surface === 'host') webview.dispatchEvent(new MessageEvent('message', { data: { type: 'pc-telemetry:host-visibility', visible: value } }))
        else {
          Object.defineProperty(fakeDocument, 'visibilityState', { value: value ? 'visible' : 'hidden', writable: true })
          fakeDocument.dispatchEvent(new Event('visibilitychange'))
        }
      }
      visible(false)
      scheduler.setPeriod(10)
      waiting.resolve()
      await flush()
      await clock.advanceBy(30000)
      assert.equal(shown, '')
      assert.equal(newCalls, 0)
      visible(true)
      await flush()
      assert.equal(shown, 'B')
      assert.equal(newCalls, 1)
    } finally {
      unbind()
      scheduler.dispose()
      if (oldDocument) Object.defineProperty(globalThis, 'document', oldDocument)
      else Reflect.deleteProperty(globalThis, 'document')
      if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow)
      else Reflect.deleteProperty(globalThis, 'window')
    }
  })
}

for (const surface of ['web', 'host'] as const) {
  for (const status of [429, 503] as const) {
    test(`restoring hidden ${surface} view before retryAt preserves ${status} backoff deadline`, async () => {
      const clock = new FakeClock()
      const scheduler = new QueryScheduler(clock)
      const fakeDocument = new EventTarget() as Document
      Object.defineProperty(fakeDocument, 'visibilityState', { value: 'visible', writable: true })
      const webview = new EventTarget()
      const oldDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
      const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
      Object.defineProperty(globalThis, 'document', { configurable: true, value: fakeDocument })
      Object.defineProperty(globalThis, 'window', { configurable: true, value: { chrome: { webview } } })
      let failingCalls = 0
      let healthyCalls = 0
      let unbind = () => {}
      try {
        unbind = bindDashboardVisibility(scheduler)
        scheduler.register('current', async () => { healthyCalls++ })
        scheduler.register('history', async () => {
          failingCalls++
          if (failingCalls <= 2) throw new QueryError(`HTTP ${status}`, status, `HTTP_${status}`, true)
        })
        await flush()
        assert.equal(healthyCalls, 1)
        assert.equal(failingCalls, 1)

        // First retry fires at t=5000 and fails again -> second backoff scheduled for t=15000 (10s delay).
        await clock.advanceBy(5000)
        assert.equal(failingCalls, 2)
        assert.equal(scheduler.status().find((item) => item.key === 'history')?.retryInMs, 10000)

        const visible = (value: boolean) => {
          if (surface === 'host') webview.dispatchEvent(new MessageEvent('message', { data: { type: 'pc-telemetry:host-visibility', visible: value } }))
          else {
            Object.defineProperty(fakeDocument, 'visibilityState', { value: value ? 'visible' : 'hidden', writable: true })
            fakeDocument.dispatchEvent(new Event('visibilitychange'))
          }
        }

        await clock.advanceBy(1000) // t=6000
        visible(false)
        assert.equal(clock.pendingCount(), 0)
        assert.equal(scheduler.status().find((item) => item.key === 'history')?.retryInMs, null)

        await clock.advanceBy(2000) // t=8000, still 7000ms before retryAt=15000
        visible(true)
        await flush()

        assert.equal(healthyCalls, 3) // initial(0), scheduled(5000), resume(8000)
        assert.equal(failingCalls, 2) // must NOT retry early on restore
        assert.equal(scheduler.status().find((item) => item.key === 'history')?.retryInMs, 7000)

        await clock.advanceBy(6999) // t=14999
        assert.equal(failingCalls, 2)

        await clock.advanceBy(1) // t=15000 -> exact preserved retryAt
        assert.equal(failingCalls, 3)
        assert.equal(scheduler.status().find((item) => item.key === 'history')?.failures, 0)
      } finally {
        unbind()
        scheduler.dispose()
        if (oldDocument) Object.defineProperty(globalThis, 'document', oldDocument)
        else Reflect.deleteProperty(globalThis, 'document')
        if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow)
        else Reflect.deleteProperty(globalThis, 'window')
      }
    })
  }
}
