import assert from 'node:assert/strict'
import test from 'node:test'
import { fetchHistorySeries, type HistoryCache, type HistoryFetcher, type HistoryPoint } from '../src/historyQuery.ts'

const fixedNow = Date.parse('2026-11-25T12:00:00.000Z')

function makePoint(offsetSeconds: number, value: number): HistoryPoint {
  const start = new Date(fixedNow - offsetSeconds * 1000).toISOString()
  const end = new Date(fixedNow - (offsetSeconds - 10) * 1000).toISOString()
  return {
    bucketStart: start,
    bucketEnd: end,
    lastObservedAt: new Date(fixedNow - (offsetSeconds - 9) * 1000).toISOString(),
    receivedAt: new Date(fixedNow - (offsetSeconds - 10) * 1000).toISOString(),
    value,
    max: value + 1,
    sampleCount: 10,
    quality: 'ok',
    aggregationUpdatedAt: new Date(fixedNow - (offsetSeconds - 10) * 1000).toISOString(),
  }
}

function historyJson(points: HistoryPoint[], nextCursor: string, resolution = '10s') {
  return {
    machineId: 'pc-1',
    sensorId: 'cpu_temperature',
    unit: '°C',
    resolution,
    points,
    nextCursor,
    aggregationUpdatedAt: new Date(fixedNow).toISOString(),
    aggregationPending: false,
  }
}

test('clears cursor and retries full range for same PC, sensor, and period on contract INVALID_HISTORY_CURSOR', async () => {
  const requests: string[] = []
  let cache: HistoryCache = {
    scope: 'pc-1|cpu_temperature|24h',
    cursor: 'expired-cursor-v1',
    points: [makePoint(600, 48.0)],
  }
  let resets = 0

  const fetcher: HistoryFetcher = async (url) => {
    requests.push(url)
    const parsed = new URL(url, 'https://local.invalid')
    if (parsed.searchParams.get('cursor') === 'expired-cursor-v1') {
      return {
        ok: false,
        status: 422,
        json: async () => ({
          error: { code: 'INVALID_HISTORY_CURSOR', message: 'INVALID_HISTORY_CURSOR', retryable: false },
        }),
      }
    }
    return {
      ok: true,
      status: 200,
      json: async () => historyJson([makePoint(60, 53.5)], 'fresh-cursor-v2', '10s'),
    }
  }

  const result = await fetchHistorySeries({
    apiBase: '',
    machineId: 'pc-1',
    sensorId: 'cpu_temperature',
    unit: '°C',
    range: '24h',
    scope: cache.scope,
    cache,
    signal: new AbortController().signal,
    fetcher,
    now: () => fixedNow,
    onCursorReset: () => {
      resets += 1
      cache = { scope: cache.scope, cursor: null, points: [] }
    },
  })

  assert.equal(resets, 1)
  assert.equal(requests.length, 2)

  const firstUrl = new URL(requests[0], 'https://local.invalid')
  assert.equal(firstUrl.pathname, '/v1/dashboard/machines/pc-1/sensors/cpu_temperature/history')
  assert.equal(firstUrl.searchParams.get('cursor'), 'expired-cursor-v1')
  assert.equal(firstUrl.searchParams.has('from'), false)

  const recoveryUrl = new URL(requests[1], 'https://local.invalid')
  assert.equal(recoveryUrl.pathname, '/v1/dashboard/machines/pc-1/sensors/cpu_temperature/history')
  assert.equal(recoveryUrl.searchParams.has('cursor'), false)
  assert.equal(recoveryUrl.searchParams.get('resolution'), '10s')
  assert.equal(recoveryUrl.searchParams.get('from'), new Date(fixedNow - 86_400_000).toISOString())
  assert.equal(recoveryUrl.searchParams.get('to'), new Date(fixedNow).toISOString())

  assert.equal(result.cache.cursor, 'fresh-cursor-v2')
  assert.equal(result.response.points.length, 1)
  assert.equal(result.response.points[0].value, 53.5)
})

test('clears stored cursor before fallback so subsequent poll uses full range even if recovery request fails', async () => {
  const requests: string[] = []
  let cache: HistoryCache = {
    scope: 'pc-1|cpu_temperature|6h',
    cursor: 'rejected-cursor',
    points: [makePoint(120, 50.0)],
  }

  let failFullOnce = true
  const fetcher: HistoryFetcher = async (url) => {
    requests.push(url)
    const parsed = new URL(url, 'https://local.invalid')
    if (parsed.searchParams.has('cursor')) {
      return {
        ok: false,
        status: 422,
        json: async () => ({ error: { code: 'INVALID_HISTORY_CURSOR' } }),
      }
    }
    if (failFullOnce) {
      failFullOnce = false
      return { ok: false, status: 503, json: async () => ({ error: { code: 'SERVICE_UNAVAILABLE' } }) }
    }
    return {
      ok: true,
      status: 200,
      json: async () => historyJson([makePoint(30, 54.0)], 'recovered-cursor', 'raw'),
    }
  }

  await assert.rejects(
    fetchHistorySeries({
      apiBase: '',
      machineId: 'pc-1',
      sensorId: 'cpu_temperature',
      unit: '°C',
      range: '6h',
      scope: cache.scope,
      cache,
      signal: new AbortController().signal,
      fetcher,
      now: () => fixedNow,
      onCursorReset: () => {
        cache = { scope: cache.scope, cursor: null, points: [] }
      },
    }),
    /HTTP 503/,
  )
  assert.equal(cache.cursor, null)
  assert.equal(requests.length, 2)

  const nextPoll = await fetchHistorySeries({
    apiBase: '',
    machineId: 'pc-1',
    sensorId: 'cpu_temperature',
    unit: '°C',
    range: '6h',
    scope: cache.scope,
    cache,
    signal: new AbortController().signal,
    fetcher,
    now: () => fixedNow + 5_000,
  })
  assert.equal(requests.length, 3)
  const thirdUrl = new URL(requests[2], 'https://local.invalid')
  assert.equal(thirdUrl.searchParams.has('cursor'), false)
  assert.equal(thirdUrl.searchParams.get('resolution'), 'raw')
  assert.equal(nextPoll.cache.cursor, 'recovered-cursor')
})

test('preserves cursor and does not fall back to full query on non-cursor HTTP or 422 errors', async () => {
  const cases = [
    { status: 503, body: { error: { code: 'SERVICE_UNAVAILABLE', retryable: true } } },
    { status: 429, body: { error: { code: 'RATE_LIMITED', retryable: true } } },
    { status: 404, body: { error: { code: 'SENSOR_NOT_FOUND', retryable: false } } },
    { status: 422, body: { error: { code: 'INVALID_TIME_RANGE', retryable: false } } },
    { status: 422, body: { error: { code: 'HISTORY_RANGE_TOO_DENSE', retryable: false } } },
  ]

  for (const testCase of cases) {
    const requests: string[] = []
    let resetCalled = false
    const cache: HistoryCache = {
      scope: 'pc-1|cpu_temperature|24h',
      cursor: 'valid-cursor',
      points: [makePoint(120, 51.0)],
    }
    const fetcher: HistoryFetcher = async (url) => {
      requests.push(url)
      return { ok: false, status: testCase.status, json: async () => testCase.body }
    }

    await assert.rejects(
      fetchHistorySeries({
        apiBase: '',
        machineId: 'pc-1',
        sensorId: 'cpu_temperature',
        unit: '°C',
        range: '24h',
        scope: cache.scope,
        cache,
        signal: new AbortController().signal,
        fetcher,
        now: () => fixedNow,
        onCursorReset: () => { resetCalled = true },
      }),
    )
    assert.equal(resetCalled, false)
    assert.equal(cache.cursor, 'valid-cursor')
    assert.equal(requests.length, 1)
  }
})
