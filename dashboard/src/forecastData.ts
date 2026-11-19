export type ForecastScope = { machineId: string; sensorId: string; unit: string; hours: number }
export type Prediction = { target_at: string; generated_at: string; value: number | null; unit: string; model_version: string; status: 'ok' | 'fallback' | 'unavailable'; reason: string | null }
export type Actual = { observedAt: string; receivedAt: string | null; value: number | null; quality: 'ok' | 'missing' | 'error' | 'unsupported' }
export type ForecastData = { scope: string; predictions: Prediction[]; actuals: Actual[]; actualCursor: string | null; through: number | null; updatedAt: string | null; aggregationUpdatedAt: string | null; aggregationPending: boolean }
export type Comparison = { prediction: Prediction; actual: Actual | null; state: 'future' | 'missing' | 'ok' | 'error' | 'unsupported' }
export type Fetcher = (url: string, init: { signal: AbortSignal; credentials: 'include' }) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>
export const scopeKey = (scope: ForecastScope) => JSON.stringify([scope.machineId, scope.sensorId, scope.unit, scope.hours])
export const emptyForecast = (scope: ForecastScope): ForecastData => ({ scope: scopeKey(scope), predictions: [], actuals: [], actualCursor: null, through: null, updatedAt: null, aggregationUpdatedAt: null, aggregationPending: false })
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const timestamp = (value: unknown): value is string => typeof value === 'string' && /(?:Z|[+]00:00)$/.test(value) && Number.isFinite(Date.parse(value))
const numberOrNull = (value: unknown) => value === null || typeof value === 'number' && Number.isFinite(value)
const abort = (signal: AbortSignal) => { if (signal.aborted) throw new DOMException('Query cancelled', 'AbortError') }

export function parsePredictions(body: unknown, expectedUnit: string): { items: Prediction[]; truncated: boolean } {
  if (!record(body) || !Array.isArray(body.items) || typeof body.truncated !== 'boolean') throw new Error('예측 응답 형식 오류')
  const items = body.items.map((item: unknown) => {
    if (!record(item) || !timestamp(item.target_at) || !timestamp(item.generated_at) ||
        Date.parse(item.target_at) - Date.parse(item.generated_at) !== 60_000 ||
        item.unit !== expectedUnit || typeof item.model_version !== 'string' || !item.model_version ||
        !['ok', 'fallback', 'unavailable'].includes(String(item.status)) || !numberOrNull(item.value) ||
        (item.status === 'unavailable' ? item.value !== null : item.value === null) ||
        !(item.reason === null || typeof item.reason === 'string')) throw new Error('예측 시각·단위·모델·값 응답 오류')
    return item as unknown as Prediction
  })
  return { items, truncated: body.truncated }
}

export function parseActuals(body: unknown, scope: ForecastScope) {
  if (!record(body) || body.machineId !== scope.machineId || body.sensorId !== scope.sensorId || body.unit !== scope.unit ||
      body.resolution !== 'raw' || !Array.isArray(body.points) || typeof body.nextCursor !== 'string' || !body.nextCursor ||
      !(body.aggregationUpdatedAt === null || timestamp(body.aggregationUpdatedAt)) || typeof body.aggregationPending !== 'boolean') throw new Error('실제값 PC·센서·단위 응답 오류')
  const actuals = body.points.map((point: unknown): Actual => {
    if (!record(point) || !timestamp(point.lastObservedAt) ||
        !(point.receivedAt === null || timestamp(point.receivedAt)) || !numberOrNull(point.value) ||
        !['ok', 'missing', 'error', 'unsupported'].includes(String(point.quality)) ||
        (point.quality === 'ok' ? point.value === null : point.value !== null)) throw new Error('원시 실제값 형식 오류')
    return { observedAt: point.lastObservedAt, receivedAt: point.receivedAt as string | null,
      value: point.value as number | null, quality: point.quality as Actual['quality'] }
  })
  return { actuals, cursor: body.nextCursor, updatedAt: body.aggregationUpdatedAt as string | null, pending: body.aggregationPending }
}

/** Pure matching: never substitute an average or an earlier observation for the target. */
export function matchActuals(predictions: Prediction[], actuals: Actual[], now: number): Comparison[] {
  const sorted = [...actuals].sort((a, b) => Date.parse(a.observedAt) - Date.parse(b.observedAt))
  return predictions.map((prediction) => {
    const target = Date.parse(prediction.target_at)
    if (target > now) return { prediction, actual: null, state: 'future' }
    // Lower bound avoids O(predictions * one-second history) scans.
    let low = 0, high = sorted.length
    while (low < high) {
      const mid = (low + high) >>> 1
      if (Date.parse(sorted[mid].observedAt) < target) low = mid + 1
      else high = mid
    }
    const actual = sorted[low]
    if (!actual || Date.parse(actual.observedAt) >= target + 1000 || Date.parse(actual.observedAt) > now) return { prediction, actual: null, state: 'missing' }
    return { prediction, actual, state: actual.quality === 'ok' ? 'ok' : actual.quality === 'unsupported' ? 'unsupported' : actual.quality === 'error' ? 'error' : 'missing' }
  })
}

export function predictionKey(point: Prediction) {
  return JSON.stringify([Date.parse(point.target_at), Date.parse(point.generated_at), point.model_version])
}

/** Split on gaps, unavailable points, status and version changes. Input is time ordered. */
export function predictionSegments(points: Prediction[]): Prediction[][] {
  const segments: Prediction[][] = []
  let segment: Prediction[] = []
  for (const point of points) {
    const previous = segment[segment.length - 1]
    if (point.value === null || point.status === 'unavailable' || previous && (
      point.model_version !== previous.model_version || point.status !== previous.status ||
      Date.parse(point.target_at) <= Date.parse(previous.target_at) || Date.parse(point.target_at) - Date.parse(previous.target_at) > 15_000)) {
      if (segment.length) segments.push(segment)
      segment = []
    }
    if (point.value !== null && point.status !== 'unavailable') segment.push(point)
  }
  if (segment.length) segments.push(segment)
  return segments
}

/** One scheduler slot, sequential HTTP reads, atomic cache commit by the caller. */
export async function readForecastUpdate(fetcher: Fetcher, apiBase: string, scope: ForecastScope, cache: ForecastData, signal: AbortSignal, now: number): Promise<ForecastData> {
  if (cache.scope !== scopeKey(scope) || ![1, 6].includes(scope.hours)) throw new Error('예측 조회 범위 오류')
  const from = now - scope.hours * 3_600_000
  const end = now + 60_000
  const base = `${apiBase}/v1/machines/${encodeURIComponent(scope.machineId)}/predictions`
  let pages = 0
  const read = async (start: number, stop: number): Promise<Prediction[]> => {
    abort(signal)
    if (++pages > 32) throw new Error('예측 조회가 너무 조밀합니다. 범위를 줄여 주세요.')
    const query = new URLSearchParams({ sensor_id: scope.sensorId, start: new Date(start).toISOString(), end: new Date(stop).toISOString(), limit: '5000' })
    const response = await fetcher(`${base}?${query}`, { signal, credentials: 'include' })
    if (!response.ok) throw new Error(`예측 조회 HTTP ${response.status}`)
    const page = parsePredictions(await response.json(), scope.unit)
    abort(signal)
    if (page.items.some((point) => Date.parse(point.target_at) < start || Date.parse(point.target_at) >= stop)) throw new Error('예측 응답이 요청 기간을 벗어났습니다')
    if (!page.truncated) return page.items
    if (stop - start <= 1) throw new Error('동일 시각 예측이 너무 많습니다')
    const middle = Math.floor((start + stop) / 2)
    return [...await read(start, middle), ...await read(middle, stop)]
  }
  // S3-05 appends immutable forecasts; overlap covers worker/request boundary races.
  const predictions = await read(cache.through === null ? from : Math.max(from, cache.through - 120_000), end)
  const historyQuery = cache.actualCursor ? new URLSearchParams({ cursor: cache.actualCursor }) :
    new URLSearchParams({ from: new Date(from).toISOString(), to: new Date(now).toISOString(), resolution: 'raw' })
  abort(signal)
  const response = await fetcher(`${apiBase}/v1/dashboard/machines/${encodeURIComponent(scope.machineId)}/sensors/${encodeURIComponent(scope.sensorId)}/history?${historyQuery}`, { signal, credentials: 'include' })
  if (!response.ok) throw new Error(`실제값 조회 HTTP ${response.status}`)
  const actualUpdate = parseActuals(await response.json(), scope)
  abort(signal)
  const predictionMap = new Map(cache.predictions.map((point) => [predictionKey(point), point]))
  for (const point of predictions) predictionMap.set(predictionKey(point), point)
  const actualMap = new Map(cache.actuals.map((point) => [Date.parse(point.observedAt), point]))
  const batch = new Map<number, Actual>()
  for (const point of actualUpdate.actuals) {
    const key = Date.parse(point.observedAt)
    const previous = batch.get(key)
    if (previous && (previous.value !== point.value || previous.quality !== point.quality)) throw new Error('같은 관측 시각의 실제값이 모호합니다')
    batch.set(key, point)
    actualMap.set(key, point)
  }
  return { scope: cache.scope, predictions: [...predictionMap.values()].filter((point) => Date.parse(point.target_at) >= from && Date.parse(point.target_at) < end)
      .sort((a, b) => Date.parse(a.target_at) - Date.parse(b.target_at) || Date.parse(a.generated_at) - Date.parse(b.generated_at) || a.model_version.localeCompare(b.model_version)),
    actuals: [...actualMap.values()].filter((point) => Date.parse(point.observedAt) >= from && Date.parse(point.observedAt) <= now),
    actualCursor: actualUpdate.cursor, through: end, updatedAt: new Date(now).toISOString(),
    aggregationUpdatedAt: actualUpdate.updatedAt, aggregationPending: actualUpdate.pending }
}
