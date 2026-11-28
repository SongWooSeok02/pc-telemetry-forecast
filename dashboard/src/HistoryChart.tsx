import { useMemo, useState } from 'react'
import type { HistoryPoint, HistoryResponse } from './historyQuery.ts'

export type { HistoryPoint, HistoryResponse }

function time(value?: string | null, date = false) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '—'
  return new Intl.DateTimeFormat('ko-KR', date
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
    : { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(parsed)
}

function elapsed(value?: string | null) {
  if (!value) return '—'
  const ms = Date.now() - new Date(value).getTime()
  if (!Number.isFinite(ms) || ms < 0) return '시계 확인 필요'
  const seconds = Math.floor(ms / 1000)
  return seconds < 60 ? `${seconds}초 전` : seconds < 3600 ? `${Math.floor(seconds / 60)}분 전` : `${Math.floor(seconds / 3600)}시간 전`
}

function delay(observedAt?: string | null, receivedAt?: string | null) {
  if (!observedAt || !receivedAt) return '—'
  const seconds = Math.floor((new Date(receivedAt).getTime() - new Date(observedAt).getTime()) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return '시각 확인 필요'
  return `${seconds}초`
}

type Props = {
  response: HistoryResponse | null
  busy: boolean
  error: string | null
  sensorUnsupported: boolean
  selectedLabel: string
  rangeLabel: string
  onRangeChange: (range: string) => void
}

const width = 920
const height = 300
const pad = { top: 22, right: 20, bottom: 42, left: 58 }

export default function HistoryChart({ response, busy, error, sensorUnsupported, selectedLabel, rangeLabel, onRangeChange }: Props) {
  const [zoom, setZoom] = useState<[number, number]>([0, 1])
  const points = response?.points ?? []
  const shown = useMemo(() => {
    if (points.length < 2) return points
    const low = Math.floor((points.length - 1) * zoom[0])
    const high = Math.max(low + 1, Math.ceil((points.length - 1) * zoom[1]))
    return points.slice(low, high + 1)
  }, [points, zoom])

  const numeric = shown.filter((point) => point.quality === 'ok' && point.value !== null && Number.isFinite(point.value))
  const minTime = shown.length ? new Date(shown[0].bucketStart).getTime() : 0
  const maxTime = shown.length ? new Date(shown[shown.length - 1].bucketEnd).getTime() : 1
  const values = numeric.map((point) => point.value as number)
  const minValue = values.length ? Math.min(...values) : 0
  const maxValue = values.length ? Math.max(...values) : 1
  const valueSpan = maxValue - minValue || Math.max(Math.abs(maxValue) * 0.1, 1)
  const x = (value: string) => pad.left + ((new Date(value).getTime() - minTime) / Math.max(maxTime - minTime, 1)) * (width - pad.left - pad.right)
  const y = (value: number) => height - pad.bottom - ((value - (minValue - valueSpan * 0.12)) / (valueSpan * 1.24)) * (height - pad.top - pad.bottom)

  const paths: string[] = []
  let segment: string[] = []
  const maxGapMs = response?.resolution === 'raw' ? 1500 : response?.resolution === '10s' ? 15000 : response?.resolution === '1m' ? 90000 : response?.resolution === '5m' ? 450000 : 5400000
  let previousValidAt: number | null = null
  for (const point of shown) {
    if (point.quality !== 'ok' || point.value === null || !Number.isFinite(point.value)) {
      if (segment.length) paths.push(segment.join(' '))
      segment = []
      previousValidAt = null
    } else {
      const pointTime = new Date(point.bucketStart).getTime()
      if (previousValidAt !== null && pointTime - previousValidAt > maxGapMs) {
        if (segment.length) paths.push(segment.join(' '))
        segment = []
      }
      segment.push(`${segment.length ? 'L' : 'M'} ${x(point.bucketStart).toFixed(1)} ${y(point.value).toFixed(1)}`)
      previousValidAt = pointTime
    }
  }
  if (segment.length) paths.push(segment.join(' '))

  const latestPoint = points[points.length - 1]
  const lastReceipt = points.reduce<string | null>((latest, point) => point.receivedAt && (!latest || point.receivedAt > latest) ? point.receivedAt : latest, null)
  const latestStatus = sensorUnsupported || latestPoint?.quality === 'unsupported' ? '미지원 센서' : !latestPoint ? '데이터 없음' : latestPoint.quality === 'ok' && latestPoint.value !== null ? '정상 관측' : latestPoint.quality === 'error' ? '품질 오류' : '결측'

  const zoomBy = (factor: number) => {
    const span = zoom[1] - zoom[0]
    const nextSpan = Math.min(1, Math.max(0.1, span * factor))
    const center = (zoom[0] + zoom[1]) / 2
    const left = Math.max(0, Math.min(1 - nextSpan, center - nextSpan / 2))
    setZoom([left, left + nextSpan])
  }

  return <section className="history-panel" aria-labelledby="history-title">
    <div className="section-heading history-heading">
      <div><p className="eyebrow">HISTORICAL TELEMETRY</p><h2 id="history-title">과거 차트</h2></div>
      <div className="history-tools">
        <label className="history-range">기간<select value={rangeLabel} onChange={(event) => onRangeChange(event.target.value)} aria-label="과거 조회 기간">
          <option value="1h">1시간</option><option value="6h">6시간</option><option value="24h">24시간</option><option value="7d">7일</option><option value="30d">30일</option><option value="90d">90일</option>
        </select></label>
        <button className="chart-zoom" onClick={() => zoomBy(0.7)} aria-label="차트 확대" title="확대">＋</button>
        <button className="chart-zoom" onClick={() => zoomBy(1.4)} aria-label="차트 축소" title="축소">－</button>
        <button className="chart-reset" onClick={() => setZoom([0, 1])}>전체 범위</button>
      </div>
    </div>
    <div className="history-meta">
      <strong>{selectedLabel}</strong><span>{response?.unit ?? '단위 대기'}</span><span className="history-meta__status">{latestStatus}</span>
      <span>마지막 관측 {time(latestPoint?.lastObservedAt)} <i>·</i> 데이터 경과 {elapsed(latestPoint?.lastObservedAt)}</span>
      <span>마지막 수신 {time(lastReceipt)} <i>·</i> 최신 관측 수신 지연 {delay(latestPoint?.lastObservedAt, latestPoint?.receivedAt)}</span>
      {response?.aggregationPending && <span className="aggregation-state">집계 갱신 대기</span>}
    </div>
    {error && <p className="notice" role="status">과거 데이터를 불러오지 못했습니다. 표시 중인 이전 데이터는 유지됩니다. {error}</p>}
    {sensorUnsupported && <div className="history-empty">이 PC에서 지원하지 않는 센서입니다</div>}
    {busy && !points.length && <div className="history-empty">과거 데이터를 불러오는 중입니다</div>}
    {!busy && !error && !sensorUnsupported && !points.length && <div className="history-empty">이 기간에 표시할 데이터가 없습니다</div>}
    {points.length > 0 && <>
      <div className="chart-wrap">
        <svg className="history-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${selectedLabel} 과거 차트`} preserveAspectRatio="none">
          {[0, 1, 2, 3].map((index) => {
            const value = minValue - valueSpan * 0.12 + (valueSpan * 1.24 * index) / 3
            const py = y(value)
            return <g key={index}><line x1={pad.left} x2={width - pad.right} y1={py} y2={py} className="chart-gridline"/><text x={pad.left - 10} y={py + 4} className="chart-axis" textAnchor="end">{value.toFixed(valueSpan < 10 ? 1 : 0)}</text></g>
          })}
          {shown.length > 0 && [0, 1, 2, 3].map((index) => {
            const stamp = new Date(minTime + (maxTime - minTime) * index / 3).toISOString()
            return <text key={index} x={pad.left + (width - pad.left - pad.right) * index / 3} y={height - 12} className="chart-axis" textAnchor={index === 0 ? 'start' : index === 3 ? 'end' : 'middle'}>{time(stamp, true)}</text>
          })}
          {paths.map((path, index) => <path key={index} d={path} className="chart-line" />)}
          {shown.filter((point) => point.quality !== 'ok' || point.value === null).map((point) => <circle key={`${point.bucketStart}-${point.quality}`} cx={x(point.bucketStart)} cy={height - pad.bottom + 11} r="4" className={`chart-quality chart-quality--${point.quality}`}><title>{point.quality === 'unsupported' ? '미지원 센서' : point.quality === 'missing' ? '결측' : '품질 오류'} · 관측 {time(point.lastObservedAt)} · 수신 {time(point.receivedAt)}</title></circle>)}
          {numeric.map((point) => <circle key={point.bucketStart} cx={x(point.bucketStart)} cy={y(point.value as number)} r="2.5" className="chart-point"><title>{selectedLabel} 평균: {(point.value as number).toFixed(2)} {response?.unit} · 최대 {point.max?.toFixed(2) ?? '—'} {response?.unit} · 마지막 관측 {time(point.lastObservedAt)} · 마지막 수신 {time(point.receivedAt)} · 표본 {point.sampleCount}</title></circle>)}
        </svg>
      </div>
      <div className="chart-legend"><span><i className="legend-line" />구간 평균</span><span><i className="legend-gap" />결측·미지원·품질 오류 및 긴 공백은 선 연결 안 함</span><span>집계 {response?.resolution ?? '—'} · 마지막 집계 {time(response?.aggregationUpdatedAt, true)}</span></div>
    </>}
  </section>
}
