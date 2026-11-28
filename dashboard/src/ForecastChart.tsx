import { useMemo, useState } from 'react'
import { matchActuals, predictionKey, predictionSegments, type ForecastData } from './forecastData'

type Props = {
  data: ForecastData | null
  busy: boolean
  error: string | null
  unsupported: boolean
  label: string
  unit: string
  hours: number
  onHoursChange: (hours: number) => void
  now: number
}
const stamp = (value: string | null) => value ? new Date(value).toLocaleString('ko-KR', { hour12: false }) : '—'
const stateLabel = { future: '실제값 대기 · 미래', missing: '실제값 결측', ok: '보고된 실제값', error: '실제값 품질 오류', unsupported: '미지원 센서' }
const modelLabel = (version: string, status: string) => status === 'unavailable' ? '예측 불가' : status === 'fallback' || version.startsWith('builtin:') ? '기준 모델' : '학습 모델'

export default function ForecastChart({ data, busy, error, unsupported, label, unit, hours, onHoursChange, now }: Props) {
  const [zoom, setZoom] = useState<[number, number]>([0, 1])
  const all = useMemo(() => matchActuals(data?.predictions ?? [], data?.actuals ?? [], now), [data, now])
  const from = now - hours * 3_600_000
  const span = hours * 3_600_000 + 60_000
  const left = from + span * zoom[0], right = from + span * zoom[1]
  const shown = all.filter((row) => Date.parse(row.prediction.target_at) >= left && Date.parse(row.prediction.target_at) <= right)
  const values = shown.flatMap((row) => [row.prediction.value, row.state === 'ok' ? row.actual?.value ?? null : null]).filter((value): value is number => value !== null)
  const minimum = values.length ? Math.min(...values) : 0, maximum = values.length ? Math.max(...values) : 1
  const spread = Math.max(maximum - minimum, 1)
  const x = (time: string) => 65 + (Date.parse(time) - left) / Math.max(right - left, 1) * 830
  const y = (value: number) => 245 - (value - minimum + spread * .1) / (spread * 1.2) * 215
  const path = (points: { time: string; value: number }[]) => points.map((point, i) => `${i ? 'L' : 'M'}${x(point.time).toFixed(2)},${y(point.value).toFixed(2)}`).join(' ')
  const predictions = predictionSegments(shown.map((row) => row.prediction))
  const actualPaths: { time: string; value: number }[][] = []
  let segment: { time: string; value: number }[] = []
  for (const row of shown) {
    const previous = segment[segment.length - 1]
    if (row.state !== 'ok' || previous && Date.parse(row.prediction.target_at) - Date.parse(previous.time) > 15_000) {
      if (segment.length) actualPaths.push(segment)
      segment = []
    }
    if (row.state === 'ok' && row.actual?.value !== null && row.actual) {
      const point = { time: row.prediction.target_at, value: row.actual.value }
      if (segment[segment.length - 1]?.time !== point.time) segment.push(point)
    }
  }
  if (segment.length) actualPaths.push(segment)
  const latest = all[all.length - 1]
  const observedTimes = (data?.actuals ?? []).map((point) => Date.parse(point.observedAt))
  const age = observedTimes.length ? Math.max(0, Math.floor((now - Math.max(...observedTimes)) / 1000)) : null
  const changeZoom = (factor: number) => {
    const width = Math.min(1, Math.max(.05, (zoom[1] - zoom[0]) * factor))
    const start = Math.max(0, Math.min(1 - width, (zoom[0] + zoom[1] - width) / 2))
    setZoom([start, start + width])
  }
  return <section className="history-panel forecast-panel" aria-labelledby="forecast-title" aria-busy={busy}>
    <div className="section-heading history-heading">
      <div><p className="eyebrow">60 SECOND FORECAST</p><h2 id="forecast-title">실제값 · 예측값 비교</h2></div>
      <div className="history-tools">
        <label>예측 조회 기간 <select aria-label="예측 조회 기간" value={hours} onChange={(event) => onHoursChange(Number(event.target.value))}><option value={1}>1시간</option><option value={6}>6시간</option></select></label>
        <button className="chart-zoom" onClick={() => changeZoom(.7)} aria-label="예측 차트 확대">＋</button>
        <button className="chart-zoom" onClick={() => changeZoom(1.4)} aria-label="예측 차트 축소">－</button>
        <button className="chart-reset" onClick={() => setZoom([0, 1])}>예측 전체 범위</button>
      </div>
    </div>
    <div className="history-meta"><strong>{label}</strong><span>{unit || '단위 대기'}</span><span>조회 갱신 {stamp(data?.updatedAt ?? null)}</span>
      <span>실제값 경과 {age === null ? '—' : `${age}초`}</span>
      <span>집계 갱신 {stamp(data?.aggregationUpdatedAt ?? null)} {data?.aggregationPending ? '· 지연 갱신 대기' : '· 원시 실제값 비교'}</span>
      {latest && <><span>생성 {stamp(latest.prediction.generated_at)}</span><span>대상 {stamp(latest.prediction.target_at)}</span><span>{modelLabel(latest.prediction.model_version, latest.prediction.status)} · {latest.prediction.model_version} · {latest.prediction.status}</span></>}
    </div>
    {error && <p className="notice" role="status">예측 비교 조회 오류: {error} 같은 조건의 이전 결과가 있으면 유지합니다.</p>}
    {unsupported ? <p className="history-empty">이 PC의 {label} 센서는 미지원입니다. 0으로 표시하지 않습니다.</p> : !all.length ? <p className="history-empty">{busy ? '예측 비교를 조회하는 중입니다' : '비교할 예측 데이터가 없습니다'}</p> : <>
      <div className="chart-wrap"><svg viewBox="0 0 920 300" className="history-svg" role="img" aria-label={`${label} 60초 후 실제값과 예측값 비교`}>
        {[0, 1, 2, 3].map((i) => <g key={i}><line className="chart-gridline" x1={65} x2={895} y1={y(minimum + spread * i / 3)} y2={y(minimum + spread * i / 3)}/><text className="chart-axis" x={55} y={y(minimum + spread * i / 3) + 4} textAnchor="end">{(minimum + spread * i / 3).toFixed(1)}</text><text className="chart-axis" x={65 + 830 * i / 3} y={286} textAnchor={i === 0 ? 'start' : i === 3 ? 'end' : 'middle'}>{new Date(left + (right - left) * i / 3).toLocaleTimeString('ko-KR')}</text></g>)}
        {actualPaths.map((points, i) => <path key={`actual-${i}`} className="forecast-actual" d={path(points)}/>)}
        {predictions.map((points, i) => <path key={`predicted-${i}`} className={modelLabel(points[0].model_version, points[0].status) === '기준 모델' ? 'forecast-baseline' : 'forecast-model'} d={path(points.map((point) => ({ time: point.target_at, value: point.value! })))}/>)}
        {shown.map((row) => <g key={predictionKey(row.prediction)}>
          <circle cx={x(row.prediction.target_at)} cy={row.prediction.value === null ? 260 : y(row.prediction.value)} r="3" className={row.prediction.status === 'unavailable' ? 'forecast-gap' : modelLabel(row.prediction.model_version, row.prediction.status) === '기준 모델' ? 'forecast-dot-baseline' : 'forecast-dot-model'}><title>{`예측 ${row.prediction.value ?? '없음'} ${unit} · 생성 ${stamp(row.prediction.generated_at)} · 대상 ${stamp(row.prediction.target_at)} · ${row.prediction.model_version} · ${row.prediction.status} · ${row.prediction.reason ?? ''}`}</title></circle>
          <circle cx={x(row.prediction.target_at)} cy={row.state === 'ok' && row.actual?.value !== null && row.actual ? y(row.actual.value) : 268} r="2.5" className={row.state === 'ok' ? 'forecast-dot-actual' : 'forecast-gap'}><title>{`${stateLabel[row.state]} ${row.actual?.value ?? '—'} ${unit} · 관측 ${stamp(row.actual?.observedAt ?? null)} · 수신 ${stamp(row.actual?.receivedAt ?? null)}`}</title></circle>
        </g>)}
      </svg></div>
      <div className="chart-legend"><span className="forecast-key-actual">실선: 보고된 원시 실제값</span><span className="forecast-key-model">파선: 학습 모델 예측</span><span className="forecast-key-baseline">점선: 기준 모델 예측</span><span>미래·결측·미지원·모델 버전 전환에서 선을 연결하지 않습니다</span></div>
      <details className="forecast-details"><summary>최근 예측의 시각·모델·실제값 확인</summary><div className="forecast-table-wrap"><table><thead><tr><th>생성 시각</th><th>대상 시각</th><th>모델/버전/상태</th><th>예측값</th><th>보고된 실제값</th></tr></thead><tbody>{[...shown].reverse().slice(0, 20).map((row) => <tr key={predictionKey(row.prediction)}><td>{stamp(row.prediction.generated_at)}</td><td>{stamp(row.prediction.target_at)}</td><td>{modelLabel(row.prediction.model_version, row.prediction.status)} · {row.prediction.model_version} · {row.prediction.status}{row.prediction.reason && ` · ${row.prediction.reason}`}</td><td>{row.prediction.value ?? '—'} {unit}</td><td>{row.state === 'ok' ? `${row.actual?.value} ${unit}` : stateLabel[row.state]}</td></tr>)}</tbody></table></div></details>
    </>}
  </section>
}
