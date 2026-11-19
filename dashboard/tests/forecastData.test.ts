import assert from 'node:assert/strict'
import test from 'node:test'
import { emptyForecast, matchActuals, parseActuals, parsePredictions, predictionSegments, readForecastUpdate, type Actual, type Fetcher, type ForecastScope, type Prediction } from '../src/forecastData.ts'
import { QueryScheduler, type SchedulerClock } from '../src/queryScheduler.ts'

const now = Date.parse('2026-11-19T12:00:00Z')
const iso = (time: number) => new Date(time).toISOString()
const scope: ForecastScope = { machineId: 'pc-a', sensorId: 'cpu_temperature', unit: 'Cel', hours: 1 }
const prediction = (target = now - 10_000, version = 'temperature-v1', status: Prediction['status'] = 'ok'): Prediction => ({ target_at: iso(target), generated_at: iso(target - 60_000), value: status === 'unavailable' ? null : 50, unit: 'Cel', model_version: version, status, reason: null })
const actual = (stamp = now - 10_000, quality: Actual['quality'] = 'ok'): Actual => ({ observedAt: iso(stamp), receivedAt: iso(now), value: quality === 'ok' ? 49 : null, quality })
function actualBody(points: Actual[], overrides = {}) {
  return { machineId: scope.machineId, sensorId: scope.sensorId, unit: scope.unit, resolution: 'raw', nextCursor: 'cursor-v1', aggregationUpdatedAt: null, aggregationPending: false,
    points: points.map((point) => ({ lastObservedAt: point.observedAt, receivedAt: point.receivedAt, value: point.value, quality: point.quality })), ...overrides }
}
const response = (body: unknown, status = 200) => ({ ok: status === 200, status, json: async () => body })
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve() }

test('future actual is not shown, even if server supplied a future observation', () => {
  const result = matchActuals([prediction(now + 30_000)], [actual(now + 30_000)], now)[0]
  assert.equal(result.state, 'future')
  assert.equal(result.actual, null)
})
test('target uses first observation at or after target within one second; no averages or earlier value', () => {
  const result = matchActuals([prediction()], [actual(now - 10_001), actual(now - 9_500)], now)[0]
  assert.equal(result.actual?.observedAt, iso(now - 9_500))
  assert.equal(matchActuals([prediction()], [actual(now - 9_000)], now)[0].state, 'missing')
})
test('missing and unsupported actuals remain null instead of zero', () => {
  for (const quality of ['missing', 'unsupported', 'error'] as const) {
    const result = matchActuals([prediction()], [actual(now - 10_000, quality)], now)[0]
    assert.equal(result.actual?.value, null)
    assert.equal(result.state, quality)
  }
})
test('model replacement, fallback, gaps and unavailable forecasts break lines', () => {
  const points = [prediction(now - 60_000), prediction(now - 50_000), prediction(now - 40_000, 'temperature-v2'),
    prediction(now - 30_000, 'builtin:persistence:v1', 'fallback'), prediction(now - 20_000, 'builtin:none', 'unavailable'), prediction(now)]
  assert.deepEqual(predictionSegments(points).map((segment) => segment.length), [2, 1, 1, 1])
  assert.deepEqual(predictionSegments([prediction(now - 30_000), prediction(now)]).map((segment) => segment.length), [1, 1])
})
test('invalid horizon, value, unit or PC cannot enter cache', () => {
  assert.throws(() => parsePredictions({ items: [{ ...prediction(), target_at: iso(now) }], truncated: false }, 'Cel'))
  assert.throws(() => parsePredictions({ items: [{ ...prediction(), value: Infinity }], truncated: false }, 'Cel'))
  assert.throws(() => parsePredictions({ items: [{ ...prediction(), unit: 'W' }], truncated: false }, 'Cel'))
  assert.throws(() => parsePredictions({ items: [{ ...prediction(), unit: undefined }], truncated: false }, 'Cel'))
  assert.throws(() => parseActuals(actualBody([actual()], { unit: 'W' }), scope))
  assert.throws(() => parseActuals(actualBody([actual()], { machineId: 'pc-b' }), scope))
  assert.throws(() => parseActuals(actualBody([actual()], { resolution: '10s' }), scope))
})
test('late actual arrives through cursor, updates match and preserves forecast identity', async () => {
  const urls: string[] = []
  let late = false
  const fetcher: Fetcher = async (url) => {
    urls.push(url)
    return response(url.includes('/predictions?') ? { items: [prediction()], truncated: false } : actualBody(late ? [actual()] : [], {
      aggregationUpdatedAt: late ? iso(now) : null, aggregationPending: !late,
    }))
  }
  const first = await readForecastUpdate(fetcher, '', scope, emptyForecast(scope), new AbortController().signal, now)
  assert.equal(matchActuals(first.predictions, first.actuals, now)[0].state, 'missing')
  late = true
  const next = await readForecastUpdate(fetcher, '', scope, first, new AbortController().signal, now + 5_000)
  assert.equal(next.predictions.length, 1)
  assert.equal(matchActuals(next.predictions, next.actuals, now + 5_000)[0].state, 'ok')
  const historyQuery = new URL(urls[3], 'https://local.invalid').searchParams
  assert.equal(historyQuery.get('cursor'), 'cursor-v1')
  assert.equal(historyQuery.has('from'), false)
  assert.equal(new URL(urls[2], 'https://local.invalid').searchParams.get('start'), iso(now - 60_000))
  assert.equal(next.aggregationUpdatedAt, iso(now))
  assert.equal(next.aggregationPending, false)
})
test('request error leaves previous cache immutable', async () => {
  const cache = emptyForecast(scope)
  cache.predictions.push(prediction())
  const before = JSON.stringify(cache)
  const fetcher: Fetcher = async (url) => url.includes('/predictions?') ? response({ items: [prediction(now)], truncated: false }) : response({}, 503)
  await assert.rejects(readForecastUpdate(fetcher, '', scope, cache, new AbortController().signal, now))
  assert.equal(JSON.stringify(cache), before)
})
test('late response from previous PC is aborted before subsequent actual request or commit', async () => {
  let release!: () => void
  let calls = 0
  const waiting = new Promise<void>((resolve) => { release = resolve })
  const controller = new AbortController()
  const cache = emptyForecast(scope)
  const fetcher: Fetcher = async () => { calls++; await waiting; return response({ items: [prediction()], truncated: false }) }
  const pending = readForecastUpdate(fetcher, '', scope, cache, controller.signal, now)
  controller.abort()
  release()
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(calls, 1)
  assert.equal(cache.predictions.length, 0)
  await assert.rejects(readForecastUpdate(fetcher, '', { ...scope, machineId: 'pc-b' }, cache, new AbortController().signal, now))
})
test('different model versions at the same target are preserved as separate forecasts', async () => {
  const fetcher: Fetcher = async (url) => response(url.includes('/predictions?') ? { items: [prediction(), prediction(now - 10_000, 'temperature-v2')], truncated: false } : actualBody([actual()]))
  const data = await readForecastUpdate(fetcher, '', scope, emptyForecast(scope), new AbortController().signal, now)
  assert.equal(data.predictions.length, 2)
  assert.equal(predictionSegments(data.predictions).length, 2)
})
test('truncated pages are split by time rather than skipping equal timestamps', async () => {
  let reads = 0
  const fetcher: Fetcher = async (url) => {
    if (!url.includes('/predictions?')) return response(actualBody([]))
    reads++
    if (reads === 1) return response({ items: [], truncated: true })
    const query = new URL(url, 'https://local.invalid').searchParams
    const inside = Date.parse(prediction().target_at) >= Date.parse(query.get('start')!) && Date.parse(prediction().target_at) < Date.parse(query.get('end')!)
    return response({ items: inside ? [prediction()] : [], truncated: false })
  }
  const data = await readForecastUpdate(fetcher, '', scope, emptyForecast(scope), new AbortController().signal, now)
  assert.equal(reads, 3)
  assert.equal(data.predictions.length, 1)
})
test('ambiguous raw observations at same timestamp are rejected', async () => {
  const fetcher: Fetcher = async (url) => response(url.includes('/predictions?') ? { items: [], truncated: false } : actualBody([actual(), { ...actual(), value: 99 }]))
  await assert.rejects(readForecastUpdate(fetcher, '', scope, emptyForecast(scope), new AbortController().signal, now), /모호/)
})
test('manual refresh shares single scheduler slot and restore does not refetch', async () => {
  const clock: SchedulerClock = { now: () => 0, setTimeout: () => { throw new Error('manual timer must not be set') }, clearTimeout: () => {} }
  const scheduler = new QueryScheduler(clock)
  scheduler.setPeriod('manual')
  scheduler.setVisible(true)
  let currentDone!: () => void
  const current = new Promise<void>((resolve) => { currentDone = resolve })
  let calls = 0
  scheduler.register('current', async () => { await current })
  const fetcher: Fetcher = async (url) => { calls++; return response(url.includes('/predictions?') ? { items: [prediction()], truncated: false } : actualBody([actual()])) }
  scheduler.register('forecast-comparison', async (signal) => { await readForecastUpdate(fetcher, '', scope, emptyForecast(scope), signal, now) })
  await flush()
  assert.equal(calls, 0)
  currentDone()
  await flush()
  assert.equal(calls, 2)
  scheduler.setVisible(false)
  scheduler.setVisible(true)
  await flush()
  assert.equal(calls, 2)
  scheduler.refreshNow()
  await flush()
  assert.equal(calls, 4)
  scheduler.dispose()
})
