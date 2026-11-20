import { appendFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setTimeout as sleep } from 'node:timers/promises'

const [endpoint, origin, output, secondsArg = '259200', sampleArg = '60'] = process.argv.slice(2)
const address = endpoint && new URL(endpoint)
const duration = Number(secondsArg) * 1000
const interval = Number(sampleArg) * 1000
if (address?.protocol !== 'http:' || address.hostname !== '127.0.0.1' || !address.port ||
    !origin || new URL(origin).origin !== origin || !output ||
    !Number.isSafeInteger(duration) || !Number.isSafeInteger(interval) || interval < 1000 || duration < interval) {
  throw new Error('Usage: node observeRefresh.mjs http://127.0.0.1:PORT https://TEST_ORIGIN LOG.jsonl [durationSeconds] [sampleSeconds]')
}

function installProbe() {
  const pendingTimeouts = new Set()
  const pendingIntervals = new Set()
  const timerStats = { peakTimeouts: 0, peakIntervals: 0 }
  const timeout = window.setTimeout.bind(window)
  const interval = window.setInterval.bind(window)
  const clearTimeout = window.clearTimeout.bind(window)
  const clearInterval = window.clearInterval.bind(window)
  window.setTimeout = (callback, delay, ...args) => {
    if (typeof callback !== 'function') return timeout(callback, delay, ...args)
    const id = timeout(function (...values) {
      pendingTimeouts.delete(id)
      return callback.apply(this, values)
    }, delay, ...args)
    pendingTimeouts.add(id)
    timerStats.peakTimeouts = Math.max(timerStats.peakTimeouts, pendingTimeouts.size)
    return id
  }
  window.setInterval = (callback, delay, ...args) => {
    const id = interval(callback, delay, ...args)
    pendingIntervals.add(id)
    timerStats.peakIntervals = Math.max(timerStats.peakIntervals, pendingIntervals.size)
    return id
  }
  window.clearTimeout = (id) => {
    pendingTimeouts.delete(id)
    pendingIntervals.delete(id)
    return clearTimeout(id)
  }
  window.clearInterval = (id) => {
    pendingTimeouts.delete(id)
    pendingIntervals.delete(id)
    return clearInterval(id)
  }

  let renderBatches = 0
  let lastRenderUtc = null
  const selector = '.machine-panel__meta, .history-svg, .forecast-panel'
  const observer = new MutationObserver((records) => {
    if (records.some(({ target }) => (target instanceof Element ? target : target.parentElement)?.closest(selector))) {
      renderBatches++
      lastRenderUtc = new Date().toISOString()
    }
  })
  observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true })
  window.__pctRefreshObservation = () => ({
    documentStartedUtc: new Date(performance.timeOrigin).toISOString(),
    visibility: document.visibilityState,
    dashboardMounted: !!document.querySelector('.app-shell'),
    timeouts: pendingTimeouts.size,
    intervals: pendingIntervals.size,
    ...timerStats,
    renderBatches,
    lastRenderUtc,
  })
}

const require = createRequire(new URL('../../local-evidence/s4-06-tools/package.json', import.meta.url))
const { chromium } = require('playwright-core')
const browser = await chromium.connectOverCDP(endpoint)
try {
  const pages = browser.contexts().flatMap((context) => context.pages())
    .filter((page) => new URL(page.url()).origin === origin)
  if (pages.length !== 1) throw new Error('Open exactly one page on the test origin before attaching')
  const [page] = pages
  const cdp = await page.context().newCDPSession(page)
  const active = { data: new Set(), auth: new Set() }
  const counter = () => ({ starts: 0, finished: 0, failed: 0, cancelled: 0, peakActive: 0, http401: 0, http429: 0, http5xx: 0 })
  const counters = { data: counter(), auth: counter() }
  const kindOf = (requestId) => active.data.has(requestId) ? 'data' : active.auth.has(requestId) ? 'auth' : null
  cdp.on('Network.requestWillBeSent', ({ requestId, request }) => {
    if (kindOf(requestId)) return // The same request ID can be reused across a redirect.
    const path = new URL(request.url).pathname
    const kind = /^\/v1\/(dashboard\/machines|machines)\//.test(path) ? 'data' : path.startsWith('/v1/user/session') ? 'auth' : null
    if (!kind) return
    active[kind].add(requestId)
    counters[kind].starts++
    counters[kind].peakActive = Math.max(counters[kind].peakActive, active[kind].size)
  })
  cdp.on('Network.responseReceived', ({ requestId, response }) => {
    const kind = kindOf(requestId)
    if (!kind) return
    if (response.status === 401) counters[kind].http401++
    if (response.status === 429) counters[kind].http429++
    if (response.status >= 500 && response.status <= 599) counters[kind].http5xx++
  })
  const finish = (requestId, outcome) => {
    const kind = kindOf(requestId)
    if (!kind) return
    active[kind].delete(requestId)
    counters[kind][outcome]++
  }
  cdp.on('Network.loadingFinished', ({ requestId }) => finish(requestId, 'finished'))
  cdp.on('Network.loadingFailed', ({ requestId, canceled }) => finish(requestId, canceled ? 'cancelled' : 'failed'))

  await cdp.send('Network.enable')
  await cdp.send('Performance.enable')
  await page.addInitScript(installProbe)
  const started = performance.now()
  const write = (type, fields, create = false) => {
    const line = JSON.stringify({ type, utc: new Date().toISOString(), elapsedMs: Math.round(performance.now() - started), ...fields }) + '\n'
    if (create) writeFileSync(output, line, { flag: 'wx' })
    else appendFileSync(output, line)
  }
  write('start', { durationSeconds: duration / 1000, sampleSeconds: interval / 1000, runtime: browser.version() }, true)
  try {
    await page.reload({ waitUntil: 'domcontentloaded' })
    const samplingStarted = performance.now()
    for (;;) {
      const { metrics } = await cdp.send('Performance.getMetrics')
      const values = Object.fromEntries(metrics.map(({ name, value }) => [name, value]))
      const display = await page.evaluate(() => window.__pctRefreshObservation?.() ?? null)
      if (!display) throw new Error('Document probe missing')
      write('sample', {
        jsHeapUsedBytes: values.JSHeapUsedSize ?? null,
        jsHeapTotalBytes: values.JSHeapTotalSize ?? null,
        nodes: values.Nodes ?? null,
        layoutCount: values.LayoutCount ?? null,
        ...display,
        data: { ...counters.data, active: active.data.size },
        auth: { ...counters.auth, active: active.auth.size },
      })
      const remaining = duration - (performance.now() - samplingStarted)
      if (remaining <= 0) break
      await sleep(Math.min(interval, remaining))
    }
    write('end', { recording: 'duration-reached' })
  } catch (error) {
    // Diagnostic messages may include the test URL. Keep the shared log free of it.
    write('interrupted', { errorType: error.name })
    throw error
  } finally {
    await cdp.detach().catch(() => {})
  }
} finally {
  // An attached browser is owned by the operator and remains open on disconnect.
  await browser.close()
}
