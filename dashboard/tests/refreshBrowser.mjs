import assert from 'node:assert/strict'
import { execFile, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, readdir } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { setTimeout as sleep } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const require = createRequire(new URL('../../local-evidence/s4-06-tools/package.json', import.meta.url))
const { chromium } = require('playwright-core')
const dist = new URL('../dist/', import.meta.url)
const machineA = '00000000-0000-4000-8000-000000000001'
const machineB = '00000000-0000-4000-8000-000000000002'
const seed = Date.now()
const iso = (ms) => new Date(ms).toISOString()
const assets = await readdir(new URL('assets/', dist))
const bundle = assets.find((name) => /^index-.*\.js$/.test(name))
const bundleSha256 = createHash('sha256').update(await readFile(new URL('assets/' + bundle, dist))).digest('hex')
const [hostExecutable, ...extraArgs] = process.argv.slice(2)
assert.equal(extraArgs.length, 0, 'optional argument: --edge-window or path to PcTelemetry.Shell.exe')
const nativeEdge = hostExecutable === '--edge-window'
const nativeHost = Boolean(hostExecutable && !nativeEdge)
let host
let server
let browser
let origin = 'http://127.0.0.1:4173'
const listen = (server) => new Promise((resolve, reject) => {
  server.once('error', reject)
  server.listen(0, '127.0.0.1', () => resolve(server.address().port))
})
async function openWebView() {
  // Serve the shell's initial navigation before its page is available for interception.
  server = createServer(async (request, response) => {
    if (new URL(request.url, origin).pathname !== '/') return response.writeHead(404).end()
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
    response.end('<!doctype html><title>S4-06 WebView2 fixture</title>')
  })
  origin = 'http://127.0.0.1:' + await listen(server)
  const reservation = createServer()
  const port = await listen(reservation)
  await new Promise((resolve) => reservation.close(resolve))
  const workspace = new URL('../../local-evidence/s4-06/', import.meta.url)
  await mkdir(workspace, { recursive: true })
  const profile = await mkdtemp(fileURLToPath(new URL('webview-', workspace)))
  host = spawn(hostExecutable, ['--dashboard-url', origin], {
    env: { ...process.env, WEBVIEW2_USER_DATA_FOLDER: profile,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port} --remote-debugging-address=127.0.0.1` },
    stdio: 'ignore',
  })
  const endpoint = 'http://127.0.0.1:' + port
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    try {
      const targets = await fetch(endpoint + '/json/list')
      if ((await targets.json()).some((target) => new URL(target.url).origin === origin)) {
        return chromium.connectOverCDP(endpoint)
      }
    } catch { /* Runtime startup is asynchronous. */ }
    await sleep(100)
  }
  throw new Error('WebView2 fixture page did not start')
}

const begin = new Date().toISOString()
const results = []
let evidence
let executionError

try {
  browser = nativeHost ? await openWebView() : await chromium.launch({
    channel: 'msedge', headless: !nativeEdge,
    ignoreDefaultArgs: nativeEdge ? ['--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding'] : undefined,
  })
  const context = nativeHost ? null : await browser.newContext({ viewport: { width: 1220, height: 720 } })
  const page = nativeHost
    ? browser.contexts().flatMap((context) => context.pages()).find((page) => new URL(page.url()).origin === origin)
    : await context.newPage()
  assert.ok(page, 'fixture page missing')
  page.setDefaultTimeout(12_000)
  let backgroundTab
  let currentDelay = 0
  let failNext = 0
  let authChecks = 0
  let peakRequests = 0
  let cancelledRequests = 0
  const active = new Map()
  const overlaps = []
  const overlappedRequests = new Map()
  const currentRequests = []
  const incremental = new Set()
  const errors = []
  evidence = () => ({ currentStarts: currentRequests.length, authChecks, peakRequests,
    overlaps: overlaps.map(({ startedAt, ...overlap }) => overlap), pageErrors: errors.length })
  const isData = (url) => /^\/v1\/(dashboard\/machines|machines)\//.test(new URL(url).pathname)
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('request', (request) => {
    if (!isData(request.url())) return
    const path = new URL(request.url()).pathname
    const kind = path.split('/').at(-1)
    const machine = path.includes(machineA) ? 'A' : 'B'
    for (const [request, pending] of active) {
      const overlap = { started: `${machine}/${kind}`, pending, startedAt: performance.now() }
      overlaps.push(overlap)
      overlappedRequests.set(request, overlap)
    }
    active.set(request, `${machine}/${kind}`)
    peakRequests = Math.max(peakRequests, active.size)
  })
  const finish = (request, outcome) => {
    if (outcome === 'net::ERR_ABORTED' && active.has(request)) cancelledRequests++
    active.delete(request)
    const overlap = overlappedRequests.get(request)
    if (overlap) {
      overlap.outcome = outcome
      overlap.overlapMs = Math.round(performance.now() - overlap.startedAt)
      overlappedRequests.delete(request)
    }
  }
  page.on('requestfinished', (request) => finish(request, 'finished'))
  page.on('requestfailed', (request) => finish(request, request.failure()?.errorText ?? 'failed'))
  await page.addInitScript(({ emulate }) => {
    let hostVisible = null
    window.chrome?.webview?.addEventListener('message', ({ data }) => {
      if (data?.type === 'pc-telemetry:host-visibility' && typeof data.visible === 'boolean') {
        hostVisible = data.visible
      }
    })
    window.fixtureVisibilitySnapshot = () => ({
      document: document.visibilityState,
      hostVisible,
      effective: document.visibilityState === 'visible' && hostVisible !== false ? 'visible' : 'hidden',
    })
    if (emulate) {
      let hidden = false
      Object.defineProperty(document, 'visibilityState', { get: () => hidden ? 'hidden' : 'visible' })
      Object.defineProperty(document, 'hidden', { get: () => hidden })
      window.fixtureVisibility = (visible) => {
        hidden = !visible
        document.dispatchEvent(new Event('visibilitychange'))
      }
    }
  }, { emulate: !nativeHost && !nativeEdge })
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) return route.abort()
    const path = url.pathname
    if (path === '/v1/user/session') {
      authChecks++
      return route.fulfill({ json: { scope: 'a'.repeat(64), expires_in_ms: 300_000 } })
    }
    if (path.startsWith('/v1/')) {
      const machine = path.split('/')[path.startsWith('/v1/dashboard/') ? 4 : 3]
      const sensorId = 'cpu_temperature'
      const baseValue = machine === machineA ? 40 : 60
      if (path.endsWith('/current')) {
        const delay = currentDelay
        const status = failNext || 200
        currentDelay = failNext = 0
        currentRequests.push({ machine, status, startedAt: Date.now(), delay })
        if (delay) await sleep(delay)
        if (status === 401) {
          return route.fulfill({ status, json: { error: { code: 'USER_SESSION_EXPIRED' } } })
        }
        if (status !== 200) return route.fulfill({ status, json: { error: { message: '검증용 일시 오류' } } })
        return route.fulfill({ json: { machineId: machine, machineName: machine === machineA ? '검증 PC A' : '검증 PC B',
          receivedAt: iso(Date.now()), readings: [{ sensorId, label: 'CPU 온도', value: baseValue, unit: '°C',
            quality: 'ok', observedAt: iso(Date.now() - 1000) }] } }).catch((error) => {
          // Chromium may discard the intercepted request on PC change or cancellation.
          if (!delay) throw error
        })
      }
      if (path.endsWith('/predictions')) return route.fulfill({ json: { items: [], truncated: false } })
      if (path.endsWith('/history')) {
        const cursor = url.searchParams.get('cursor')
        const resolution = cursor ?? url.searchParams.get('resolution')
        if (cursor) incremental.add(machine)
        const points = cursor ? [] : Array.from({ length: 9 }, (_, index) => {
          const timestamp = seed - (48 - index * 5) * 60_000
          return { bucketStart: iso(timestamp), bucketEnd: iso(timestamp + 1000), lastObservedAt: iso(timestamp),
            receivedAt: iso(timestamp + 1000), value: baseValue + index, max: baseValue + index, sampleCount: 1, quality: 'ok' }
        })
        return route.fulfill({ json: { machineId: machine, sensorId, unit: '°C', resolution, nextCursor: resolution,
          aggregationPending: false, aggregationUpdatedAt: iso(seed), points } })
      }
      throw new Error('Fixture route missing: ' + path)
    }
    const asset = assets.find((name) => path === '/assets/' + name)
    if (path === '/' || asset) {
      return route.fulfill({ path: fileURLToPath(new URL(asset ? 'assets/' + asset : 'index.html', dist)) })
    }
    return route.fulfill({ status: 404 })
  })

  const period = page.getByLabel('갱신 주기')
  const machine = page.getByLabel('Machine ID')
  const range = page.getByLabel('과거 조회 기간')
  const refresh = page.getByRole('button', { name: '지금 갱신' })
  const historyPoints = page.locator('.history-svg .chart-point')
  const connected = page.locator('.connection-pill')
  const show = async (visible) => {
    if (nativeHost) {
      await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned', '-File',
        fileURLToPath(new URL('setFixtureWindow.ps1', import.meta.url)), String(host.pid), visible ? 'restore' : 'minimize'])
    } else if (nativeEdge) {
      if (visible) {
        await page.bringToFront()
        await backgroundTab?.close()
      } else {
        [backgroundTab] = await Promise.all([
          page.waitForEvent('popup'),
          page.evaluate(() => { window.open('about:blank', '_blank') }),
        ])
        await backgroundTab.bringToFront()
      }
    } else {
      await page.evaluate((value) => window.fixtureVisibility(value), visible)
    }
    const expected = visible ? 'visible' : 'hidden'
    await page.waitForFunction((state) => window.fixtureVisibilitySnapshot().effective === state,
      expected, { polling: 100, timeout: 1500 })
      .catch(async () => {
        const actual = await page.evaluate(() => window.fixtureVisibilitySnapshot())
        console.error(JSON.stringify({ atUtc: new Date().toISOString(), expected, actual, nativeHost, nativeEdge,
          scenario: 'window visibility', status: 'fail' }))
      })
    return page.evaluate(() => window.fixtureVisibilitySnapshot())
  }
  const visibilitySource = nativeHost ? 'native WebView2 window' : nativeEdge ? 'native Edge tab' : 'synthetic visibilitychange'
  const pickPeriod = (value) => period.evaluate((element, selection) => {
    element.value = selection
    element.dispatchEvent(new Event('change', { bubbles: true }))
  }, value)
  const click = (control) => control.evaluate((element) => element.click())
  const record = (scenario, actual, pass = true) => results.push({
    scenario, actual, status: pass ? 'pass' : 'fail', checkedAtUtc: new Date().toISOString(),
  })
  const waitForCount = async (count) => {
    const deadline = Date.now() + 10_000
    while (currentRequests.length < count && Date.now() < deadline) await sleep(25)
    assert.ok(currentRequests.length >= count, 'expected current request')
  }
  const waitConnected = () => connected.filter({ hasText: /^연결됨$/ }).waitFor()

  await page.goto(origin + '/?machine_id=' + machineA)
  await waitConnected()
  assert.equal(await period.inputValue(), '5')
  await range.selectOption('6h')
  await historyPoints.first().waitFor()
  const allPoints = await historyPoints.count()
  await page.getByRole('button', { name: '차트 확대', exact: true }).click()
  const zoomPoints = await historyPoints.count()
  assert.ok(zoomPoints > 0 && zoomPoints < allPoints)
  const scrollY = await page.evaluate(() => { window.scrollTo(0, 350); return window.scrollY })
  assert.ok(scrollY > 0)
  for (const value of ['3', '10', 'manual', '5']) {
    await pickPeriod(value)
    assert.equal(await period.inputValue(), value)
    assert.equal(await machine.inputValue(), machineA)
    assert.equal(await range.inputValue(), '6h')
    assert.equal(await historyPoints.count(), zoomPoints)
    assert.equal(await page.evaluate(() => window.scrollY), scrollY)
  }
  await waitConnected()
  await pickPeriod('manual')
  await page.reload()
  await waitConnected()
  assert.equal(await period.inputValue(), 'manual')
  record('갱신 주기 변경과 저장 복원', { periods: ['3', '10', 'manual', '5'], zoomPoints, allPoints, scrollY, restored: 'manual' })

  const beforeSlow = currentRequests.length
  currentDelay = 6500
  await pickPeriod('3')
  await waitForCount(beforeSlow + 1)
  await sleep(6100)
  assert.equal(currentRequests.length, beforeSlow + 1)
  await waitConnected()
  assert.ok(peakRequests <= 1, 'only one client data request at a time')
  record('두 주기 이상 걸린 응답', { delayMs: 6500, periodMs: 3000, startsBeforeResponse: 1, peakRequests })

  await pickPeriod('manual')
  currentDelay = 1400
  const beforePC = currentRequests.length
  await click(refresh)
  await waitForCount(beforePC + 1)
  await machine.fill(machineB)
  await machine.fill(machineA)
  await machine.fill(machineB)
  await page.getByText('검증 PC B', { exact: true }).waitFor()
  await sleep(1550)
  assert.equal(await machine.inputValue(), machineB)
  assert.equal(await page.getByText('검증 PC A', { exact: true }).count(), 0)
  assert.equal(await page.locator('.reading-card__value').innerText(), '60°C')
  record('지연 응답 중 빠른 PC 변경', { selection: 'A→B→A→B', displayed: 'B (60°C)', staleAVisible: false })

  failNext = 503
  const beforeFail = currentRequests.length
  await click(refresh)
  await waitForCount(beforeFail + 1)
  await connected.filter({ hasText: '연결 오류' }).waitFor()
  assert.equal(await page.locator('.reading-card__value').innerText(), '60°C')
  await sleep(5300)
  assert.equal(currentRequests.length, beforeFail + 1)
  record('수동 실패와 값 유지', { status: 503, automaticStartsAfter5300ms: 0, retained: '60°C' })

  await click(refresh)
  await waitForCount(beforeFail + 2)
  await waitConnected()
  failNext = 503
  const beforeAuto = currentRequests.length
  await pickPeriod('5')
  await waitForCount(beforeAuto + 1)
  await waitForCount(beforeAuto + 2)
  await waitConnected()
  const retryMs = currentRequests[beforeAuto + 1].startedAt - currentRequests[beforeAuto].startedAt
  assert.ok(retryMs >= 5000 && retryMs < 10_000, `unexpected retry delay: ${retryMs} ms`)
  await waitForCount(beforeAuto + 3)
  await waitConnected()
  const recoveredPeriodMs = currentRequests[beforeAuto + 2].startedAt - currentRequests[beforeAuto + 1].startedAt
  assert.ok(recoveredPeriodMs >= 4900 && recoveredPeriodMs < 8000)
  assert.ok(incremental.has(machineB))
  record('자동 실패·재시도·증분 조회', { retryMs, recoveredPeriodMs, recovered: '연결됨', cursorSeenForB: true })

  await pickPeriod('manual')
  const beforeHide = currentRequests.length
  const beforeCancel = cancelledRequests
  currentDelay = 4000
  await click(refresh)
  await waitForCount(beforeHide + 1)
  const manualHiddenState = await show(false)
  await waitConnected()
  const cancelledBeforeRestore = cancelledRequests - beforeCancel
  const manualRestoredState = await show(true)
  await sleep(1150)
  const manualStarts = currentRequests.length - beforeHide - 1
  record('수동 중 숨김·복원', { source: visibilitySource, retained: '연결됨', automaticStarts: manualStarts,
    minimized: manualHiddenState, restored: manualRestoredState, cancelledBeforeRestore },
    manualHiddenState.effective === 'hidden' && manualRestoredState.effective === 'visible' &&
    manualStarts === 0 && cancelledBeforeRestore === 1)

  await pickPeriod('5')
  await waitForCount(beforeHide + 2 + manualStarts)
  await waitConnected()
  const autoHiddenState = await show(false)
  const hiddenCount = currentRequests.length
  await pickPeriod('10')
  await sleep(1000)
  const hiddenStarts = currentRequests.length - hiddenCount
  const autoRestoredState = await show(true)
  if (autoHiddenState.effective === 'hidden') {
    await waitForCount(hiddenCount + hiddenStarts + 1)
    await waitConnected()
  }
  const restoreStarts = currentRequests.length - hiddenCount - hiddenStarts
  record('숨김 중 주기 변경과 자동 복원', { source: visibilitySource, hiddenStarts, restoreStarts,
    minimized: autoHiddenState, restored: autoRestoredState },
    autoHiddenState.effective === 'hidden' && autoRestoredState.effective === 'visible' && hiddenStarts === 0 && restoreStarts === 1)

  await pickPeriod('manual')
  failNext = 401
  const beforeExpiry = currentRequests.length
  await click(refresh)
  await page.getByText('로그인이 만료되었습니다. 다시 로그인해 주세요.').waitFor()
  assert.equal(await machine.count(), 0)
  await sleep(1200)
  assert.equal(currentRequests.length, beforeExpiry + 1)
  assert.equal(active.size, 0)
  assert.deepEqual(errors, [])
  record('인증 만료 정리', { status: 401, protectedScreenMounted: false, additionalStarts: 0 })

} catch (error) {
  executionError = error.message
  console.error(error)
} finally {
  console.log(JSON.stringify({ beginUtc: begin, endUtc: new Date().toISOString(), browser: browser?.version(),
    node: process.version, platform: process.platform + '-' + process.arch, bundle, bundleSha256,
    surface: nativeHost ? 'WebView2' : 'Edge', fixture: true, ...evidence?.(), results, executionError }, null, 2))
  await browser?.close()
  host?.kill()
  server?.close()
}
if (executionError || results.some((result) => result.status === 'fail')) process.exitCode = 1
