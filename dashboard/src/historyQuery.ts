import { QueryError, responseError } from './queryErrors.ts'

export type HistoryResolution = 'raw' | '10s' | '1m' | '5m' | '1h'

export type HistoryPoint = {
  bucketStart: string
  bucketEnd: string
  lastObservedAt: string | null
  receivedAt: string | null
  value: number | null
  max: number | null
  sampleCount: number
  quality: 'ok' | 'unsupported' | 'missing' | 'error'
  aggregationUpdatedAt: string | null
}

export type HistoryResponse = {
  machineId: string
  sensorId: string
  unit: string
  resolution: HistoryResolution
  points: HistoryPoint[]
  nextCursor: string
  aggregationUpdatedAt: string | null
  aggregationPending: boolean
}

export type HistoryCache = {
  scope: string
  cursor: string | null
  points: HistoryPoint[]
}

export type HistoryHttpResponse = {
  ok: boolean
  status: number
  json(): Promise<unknown>
}

export type HistoryFetcher = (url: string, init: { signal: AbortSignal }) => Promise<HistoryHttpResponse>

export type HistoryQueryParams = {
  apiBase: string
  machineId: string
  sensorId: string
  unit: string
  range: string
  scope: string
  cache: HistoryCache
  signal: AbortSignal
  fetcher?: HistoryFetcher
  now?: () => number
  onCursorReset?: () => void
}

export const HISTORY_RANGE_MS: Record<string, number> = {
  '1h': 3_600_000,
  '6h': 21_600_000,
  '24h': 86_400_000,
  '7d': 604_800_000,
  '30d': 2_592_000_000,
  '90d': 7_776_000_000,
}

export function historyResolution(durationMs: number): HistoryResolution {
  return durationMs <= HISTORY_RANGE_MS['6h']
    ? 'raw'
    : durationMs <= HISTORY_RANGE_MS['24h']
      ? '10s'
      : durationMs <= HISTORY_RANGE_MS['7d']
        ? '1m'
        : durationMs <= HISTORY_RANGE_MS['30d']
          ? '5m'
          : '1h'
}

function throwIfAborted(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('Query cancelled', 'AbortError')
}

export async function fetchHistorySeries(params: HistoryQueryParams): Promise<{ response: HistoryResponse; cache: HistoryCache }> {
  const fetcher: HistoryFetcher = params.fetcher ?? ((url, init) => fetch(url, init))
  const now = params.now ?? Date.now
  const duration = HISTORY_RANGE_MS[params.range] ?? HISTORY_RANGE_MS['24h']
  const resolution = historyResolution(duration)
  const endpoint = `${params.apiBase}/v1/dashboard/machines/${encodeURIComponent(params.machineId)}/sensors/${encodeURIComponent(params.sensorId)}/history`
  const fullQuery = (atMs: number) => new URLSearchParams({
    from: new Date(atMs - duration).toISOString(),
    to: new Date(atMs).toISOString(),
    resolution,
  })

  throwIfAborted(params.signal)
  let usedCursor = params.cache.cursor
  const initialQuery = usedCursor ? new URLSearchParams({ cursor: usedCursor }) : fullQuery(now())
  let response = await fetcher(`${endpoint}?${initialQuery}`, { signal: params.signal })
  throwIfAborted(params.signal)

  if (!response.ok) {
    const error = await responseError(response)
    throwIfAborted(params.signal)
    if (usedCursor === null || error.status !== 422 || error.code !== 'INVALID_HISTORY_CURSOR') throw error
    usedCursor = null
    params.onCursorReset?.()
    throwIfAborted(params.signal)
    response = await fetcher(`${endpoint}?${fullQuery(now())}`, { signal: params.signal })
    throwIfAborted(params.signal)
  }

  if (!response.ok) throw await responseError(response)

  const data = await response.json() as HistoryResponse
  throwIfAborted(params.signal)

  const validPoints = Array.isArray(data?.points) && data.points.every((point) =>
    point && Number.isFinite(Date.parse(point.bucketStart)) && Number.isFinite(Date.parse(point.bucketEnd)) &&
    (point.lastObservedAt === null || Number.isFinite(Date.parse(point.lastObservedAt))) &&
    (point.receivedAt === null || Number.isFinite(Date.parse(point.receivedAt))) &&
    ['ok', 'unsupported', 'missing', 'error'].includes(point.quality) &&
    (point.value === null || (typeof point.value === 'number' && Number.isFinite(point.value))) &&
    (point.quality === 'ok' ? typeof point.value === 'number' : point.value === null) &&
    (point.max === null || (typeof point.max === 'number' && Number.isFinite(point.max))) &&
    Number.isInteger(point.sampleCount) && point.sampleCount >= 0)
  if (!validPoints || typeof data.aggregationPending !== 'boolean' || data.unit !== params.unit ||
      data.resolution !== resolution || typeof data.nextCursor !== 'string' || !data.nextCursor ||
      data.machineId !== params.machineId || data.sensorId !== params.sensorId) {
    throw new QueryError('조회 응답 형식 또는 센서·단위 매핑이 올바르지 않습니다.', 422, 'INVALID_RESPONSE', false)
  }

  const basePoints = usedCursor !== null ? params.cache.points : []
  const merged = new Map(basePoints.map((point) => [point.bucketStart, point]))
  for (const point of data.points) merged.set(point.bucketStart, point)
  const cutoff = now() - duration
  const points = [...merged.values()]
    .filter((point) => new Date(point.bucketEnd).getTime() >= cutoff)
    .sort((a, b) => a.bucketStart.localeCompare(b.bucketStart))
  const nextCache: HistoryCache = { scope: params.scope, cursor: data.nextCursor, points }
  return { response: { ...data, points }, cache: nextCache }
}
