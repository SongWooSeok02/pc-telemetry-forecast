import { useEffect, useMemo, useRef, useState } from 'react'
import type { BrowserSession } from './browserSession'
import HistoryChart, { type HistoryPoint, type HistoryResponse } from './HistoryChart'
import { bindDashboardVisibility, QueryScheduler, type ScheduledRequest } from './queryScheduler'
import { readRefreshPeriod, REFRESH_SECONDS, saveRefreshPeriod, type RefreshPeriod } from './refreshSettings'

type SensorReading = {
  sensorId?: string
  label: string
  value: number | null
  unit: string
  quality: 'ok' | 'unsupported' | 'missing' | 'error'
  observedAt?: string
}

type CurrentSnapshot = {
  machineId: string
  machineName: string
  receivedAt: string | null
  readings: SensorReading[]
}

type LoadState = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error'

const sensorDefaults = [
  { sensorId: 'cpu_temperature', label: 'CPU 온도', unit: '°C', quality: 'missing' as const },
  { sensorId: 'gpu_temperature', label: 'GPU 온도', unit: '°C', quality: 'missing' as const },
  { sensorId: 'cpu_usage', label: 'CPU 사용률', unit: '%', quality: 'missing' as const },
  { sensorId: 'gpu_usage', label: 'GPU 사용률', unit: '%', quality: 'missing' as const },
  { sensorId: 'cpu_power', label: 'CPU 전력', unit: 'W', quality: 'missing' as const },
  { sensorId: 'gpu_power', label: 'GPU 전력', unit: 'W', quality: 'missing' as const },
]
const rangeMs: Record<string, number> = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000, '30d': 2592000000, '90d': 7776000000 }

function useScheduledQuery(scheduler: QueryScheduler, key: string, identity: string, enabled: boolean, request: ScheduledRequest) {
  const requestRef = useRef(request)
  requestRef.current = request
  useEffect(() => {
    if (!enabled) return
    return scheduler.register(key, (signal) => requestRef.current(signal))
  }, [scheduler, key, identity, enabled])
}

function formatTime(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

function formatAge(value?: string | null) {
  if (!value) return '—'
  const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return '시각 확인 필요'
  return seconds < 60 ? `${seconds}초` : seconds < 3600 ? `${Math.floor(seconds / 60)}분` : `${Math.floor(seconds / 3600)}시간`
}

function formatLag(observedAt?: string, receivedAt?: string | null) {
  if (!observedAt || !receivedAt) return '—'
  const seconds = Math.floor((new Date(receivedAt).getTime() - new Date(observedAt).getTime()) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return '시각 확인 필요'
  return `${seconds}초`
}

function ReadingCard({ reading, receivedAt }: { reading: SensorReading; receivedAt?: string | null }) {
  const unavailable = reading.quality !== 'ok' || reading.value === null
  const status = reading.quality === 'unsupported' ? '미지원' : reading.quality === 'error' ? '오류' : reading.quality === 'missing' ? '측정 대기' : '데이터 없음'
  return (
    <article className="reading-card">
      <div className="reading-card__top"><span>{reading.label}</span><span className="reading-dot" /></div>
      <div className={`reading-card__value${unavailable ? ' is-muted' : ''}`}>
        {unavailable ? <span className="reading-card__empty">{status}</span> : <>{reading.value}<small>{reading.unit}</small></>}
      </div>
      <div className="reading-card__foot">관측 {formatTime(reading.observedAt)} · 경과 {formatAge(reading.observedAt)} · 수신 지연 {formatLag(reading.observedAt, receivedAt)}</div>
    </article>
  )
}

export default function App({ machineId, setMachineId, session, onLogout }: {
  machineId: string
  setMachineId: (value: string) => void
  session: BrowserSession
  onLogout: () => void
}) {
  const [snapshot, setSnapshot] = useState<CurrentSnapshot | null>(null)
  const [state, setState] = useState<LoadState>('idle')
  const [lastRefresh, setLastRefresh] = useState<string | undefined>()
  const [period, setPeriod] = useState<RefreshPeriod>(() => readRefreshPeriod())
  const [scheduler] = useState(() => new QueryScheduler())
  const [historyRange, setHistoryRange] = useState('24h')
  const [selectedSensorId, setSelectedSensorId] = useState('cpu_temperature')
  const [history, setHistory] = useState<HistoryResponse | null>(null)
  const [historyBusy, setHistoryBusy] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const historyCache = useRef<{ scope: string; cursor: string | null; points: HistoryPoint[] }>({ scope: '', cursor: null, points: [] })

  const currentIdentity = machineId.trim().toLowerCase()
  const validMachine = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(currentIdentity)
  const shownSnapshot = snapshot?.machineId === currentIdentity ? snapshot : null
  const selectedMachine = useRef(currentIdentity)

  const sensors = useMemo(() => {
    if (!shownSnapshot?.readings.length) return sensorDefaults
    return shownSnapshot.readings.map((reading) => ({
      sensorId: reading.sensorId ?? sensorDefaults.find((sensor) => sensor.label === reading.label)?.sensorId ?? reading.label,
      label: reading.label,
      unit: reading.unit,
      quality: reading.quality,
    }))
  }, [shownSnapshot])
  const selectedSensor = sensors.find((sensor) => sensor.sensorId === selectedSensorId) ?? sensors[0]

  useEffect(() => {
    if (sensors.some((sensor) => sensor.sensorId === selectedSensorId)) return
    setSelectedSensorId(sensors[0]?.sensorId ?? 'cpu_temperature')
  }, [sensors, selectedSensorId])

  useEffect(() => {
    return bindDashboardVisibility(scheduler)
  }, [scheduler])

  useEffect(() => { scheduler.setPeriod(period) }, [scheduler, period])

  useEffect(() => {
    if (!machineId.trim()) {
      setState('idle')
      setSnapshot(null)
    }
  }, [machineId])

  const clearProtectedData = () => {
    setSnapshot(null)
    setLastRefresh(undefined)
    setHistory(null)
    historyCache.current = { scope: '', cursor: null, points: [] }
  }
  const denyMachine = () => {
    clearProtectedData()
    setState('unavailable')
  }
  const changeMachine = (value: string) => {
    const next = value.trim().toLowerCase()
    if (next !== selectedMachine.current) {
      selectedMachine.current = next
      clearProtectedData()
      setState('idle')
    }
    setMachineId(value)
  }
  const loadCurrent: ScheduledRequest = async (signal) => {
    if (!currentIdentity) return
    setState('loading')
    try {
      const response = await session.request(`/v1/dashboard/machines/${encodeURIComponent(currentIdentity)}/current`, AbortSignal.any([signal, AbortSignal.timeout(10_000)]))
      if (signal.aborted || selectedMachine.current !== currentIdentity) return
      if (response.status === 404 || response.status === 403) { denyMachine(); return }
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json() as CurrentSnapshot
      if (signal.aborted || selectedMachine.current !== currentIdentity) return
      if (data.machineId !== currentIdentity) throw new Error('Machine mismatch')
      if (historyCache.current.scope !== historyIdentity) historyCache.current = { scope: historyIdentity, cursor: null, points: [] }
      setSnapshot(data)
      setLastRefresh(new Date().toISOString())
      setState('ready')
    } catch (error) {
      if (!signal.aborted && selectedMachine.current === currentIdentity && !(error instanceof DOMException && error.name === 'AbortError')) { clearProtectedData(); setState('error') }
    }
  }
  useScheduledQuery(scheduler, 'current', currentIdentity, validMachine, loadCurrent)

  const historyIdentity = `${currentIdentity}|${selectedSensor?.sensorId ?? ''}|${historyRange}`
  const historyEnabled = Boolean(shownSnapshot && selectedSensor && selectedSensor.quality !== 'unsupported')
  useEffect(() => {
    if (historyCache.current.scope === historyIdentity) return
    historyCache.current = { scope: historyIdentity, cursor: null, points: [] }
    setHistory(null)
    setHistoryError(null)
    setHistoryBusy(false)
  }, [historyIdentity])

  useEffect(() => {
    if (historyEnabled) return
    setHistory(null)
    setHistoryError(null)
    setHistoryBusy(false)
  }, [historyEnabled, historyIdentity])

  const loadHistory: ScheduledRequest = async (signal) => {
    if (!currentIdentity || !selectedSensor || selectedSensor.quality === 'unsupported') return
    setHistoryBusy(true)
    setHistoryError(null)
    const duration = rangeMs[historyRange] ?? rangeMs['24h']
    const resolution = duration <= rangeMs['6h'] ? 'raw' : duration <= rangeMs['24h'] ? '10s' : duration <= rangeMs['7d'] ? '1m' : duration <= rangeMs['30d'] ? '5m' : '1h'
    const scope = historyIdentity
    const cache = historyCache.current.scope === scope ? historyCache.current : { scope, cursor: null, points: [] }
    const to = new Date().toISOString()
    const from = new Date(Date.now() - duration).toISOString()
    const query = cache.cursor ? new URLSearchParams({ cursor: cache.cursor }) : new URLSearchParams({ from, to, resolution })
    try {
      const response = await session.request(`/v1/dashboard/machines/${encodeURIComponent(currentIdentity)}/sensors/${encodeURIComponent(selectedSensor.sensorId)}/history?${query}`, AbortSignal.any([signal, AbortSignal.timeout(10_000)]))
      if (signal.aborted || selectedMachine.current !== currentIdentity) return
      if (response.status === 403 || response.status === 404) {
        const body = await response.json() as { error?: { code?: string } }
        if (signal.aborted || selectedMachine.current !== currentIdentity) return
        if (response.status === 403 || body.error?.code === 'MACHINE_NOT_FOUND') denyMachine()
        throw new Error('선택한 PC 또는 센서에 접근할 수 없습니다.')
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json() as HistoryResponse
      const validPoints = Array.isArray(data.points) && data.points.every((point) =>
        point && Number.isFinite(Date.parse(point.bucketStart)) && Number.isFinite(Date.parse(point.bucketEnd)) &&
        (point.lastObservedAt === null || Number.isFinite(Date.parse(point.lastObservedAt))) &&
        (point.receivedAt === null || Number.isFinite(Date.parse(point.receivedAt))) &&
        ['ok', 'unsupported', 'missing', 'error'].includes(point.quality) &&
        (point.value === null || (typeof point.value === 'number' && Number.isFinite(point.value))) &&
        (point.quality === 'ok' ? typeof point.value === 'number' : point.value === null) &&
        (point.max === null || (typeof point.max === 'number' && Number.isFinite(point.max))) &&
        Number.isInteger(point.sampleCount) && point.sampleCount >= 0)
      if (!validPoints || typeof data.aggregationPending !== 'boolean' || data.unit !== selectedSensor.unit || data.resolution !== resolution || typeof data.nextCursor !== 'string' || !data.nextCursor || data.machineId !== currentIdentity || data.sensorId !== selectedSensor.sensorId) throw new Error('조회 응답 형식 또는 센서·단위 매핑이 올바르지 않습니다.')
      if (signal.aborted || selectedMachine.current !== currentIdentity || historyCache.current.scope !== scope) return
      const merged = new Map(cache.points.map((point) => [point.bucketStart, point]))
      for (const point of data.points) merged.set(point.bucketStart, point)
      const points = [...merged.values()].filter((point) => new Date(point.bucketEnd).getTime() >= Date.now() - duration).sort((a, b) => a.bucketStart.localeCompare(b.bucketStart))
      historyCache.current = { scope, cursor: data.nextCursor, points }
      setHistory({ ...data, points })
    } catch (error) {
      if (!signal.aborted && selectedMachine.current === currentIdentity && !(error instanceof DOMException && error.name === 'AbortError')) setHistoryError(error instanceof Error ? error.message : '알 수 없는 오류')
    } finally {
      if (!signal.aborted && selectedMachine.current === currentIdentity) setHistoryBusy(false)
    }
  }
  useScheduledQuery(scheduler, 'history', historyIdentity, historyEnabled, loadHistory)

  const choosePeriod = (value: RefreshPeriod) => {
    setPeriod(value)
    saveRefreshPeriod(value)
  }
  const statusText = state === 'ready' ? '연결됨' : state === 'loading' ? '조회 중' : state === 'error' ? '연결 오류' : state === 'unavailable' ? '데이터 없음' : 'PC 선택 필요'
  const statusClass = state === 'ready' ? 'is-connected' : state === 'error' ? 'is-error' : ''
  const shownHistory = historyEnabled && history?.machineId === currentIdentity && history.sensorId === selectedSensor?.sensorId ? history : null

  return (
    <main className="app-shell">
      <aside className="rail" aria-label="주 메뉴">
        <a className="brand-mark" href="#home" aria-label="PC Telemetry 홈"><span>◈</span></a>
        <button className="rail-button is-active" aria-label="대시보드">▦</button>
        <button className="rail-button" aria-label="장비">▣</button>
        <div className="rail-spacer" />
        <button className="rail-button rail-button--user" aria-label="로그아웃" onClick={onLogout}>↪</button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="breadcrumb"><span>모니터링</span><b>/</b><strong>대시보드</strong></div>
          <div className="topbar-actions">
            <label className="refresh-control">갱신
              <select value={period} onChange={(event) => choosePeriod(event.target.value === 'manual' ? 'manual' : Number(event.target.value) as RefreshPeriod)} aria-label="갱신 주기">
                {REFRESH_SECONDS.map((seconds) => <option key={seconds} value={seconds}>{seconds}초</option>)}
                <option value="manual">수동</option>
              </select>
            </label>
            <button className="refresh-button" onClick={() => scheduler.refreshNow()} aria-label="지금 갱신">↻ <span>지금 갱신</span></button>
          </div>
        </header>

        <div className="content">
          <section className="page-heading">
            <div><p className="eyebrow">PC TELEMETRY / OVERVIEW</p><h1>시스템 현황</h1><p className="subtitle">등록된 PC의 현재 센서 상태를 한눈에 확인합니다.</p></div>
            <div className={`connection-pill ${statusClass}`}><span className="status-indicator" />{statusText}</div>
          </section>

          <section className="machine-panel">
            <div className="machine-panel__title"><div className="machine-icon">▰</div><div><span className="section-label">모니터링 대상</span><strong>{shownSnapshot?.machineName ?? 'PC 선택'}</strong></div></div>
            <label className="machine-input">Machine ID<input value={machineId} onChange={(event) => changeMachine(event.target.value)} placeholder="등록된 PC ID 입력" /></label>
            <div className="machine-panel__meta"><span>마지막 수신</span><strong>{formatTime(shownSnapshot?.receivedAt)}</strong><small>{formatAge(shownSnapshot?.receivedAt)} 전</small><i /> <span>화면 갱신</span><strong>{formatTime(lastRefresh)}</strong></div>
          </section>

          <section className="section-block">
            <div className="section-heading"><div><p className="eyebrow">LIVE TELEMETRY</p><h2>현재 센서값</h2></div><span className="section-caption">수신 시각 기준 최신 관측값</span></div>
            <div className="reading-grid">
              {(shownSnapshot?.readings ?? [
                { label: 'CPU 온도', value: null, unit: '°C', quality: 'missing' as const },
                { label: 'GPU 온도', value: null, unit: '°C', quality: 'missing' as const },
                { label: 'CPU 사용률', value: null, unit: '%', quality: 'missing' as const },
                { label: 'GPU 사용률', value: null, unit: '%', quality: 'missing' as const },
                { label: 'CPU 전력', value: null, unit: 'W', quality: 'missing' as const },
                { label: 'GPU 전력', value: null, unit: 'W', quality: 'missing' as const },
              ]).map((reading) => <ReadingCard key={reading.sensorId ?? reading.label} reading={reading} receivedAt={shownSnapshot?.receivedAt} />)}
            </div>
          </section>

          <section className="section-block">
            <label className="history-sensor">센서<select value={selectedSensor?.sensorId ?? ''} onChange={(event) => setSelectedSensorId(event.target.value)} aria-label="차트 센서">
              {sensors.map((sensor) => <option key={sensor.sensorId} value={sensor.sensorId}>{sensor.label} · {sensor.unit}</option>)}
            </select></label>
            <HistoryChart response={shownHistory} busy={historyBusy} error={historyError} sensorUnsupported={selectedSensor?.quality === 'unsupported'} selectedLabel={selectedSensor?.label ?? '센서'} rangeLabel={historyRange} onRangeChange={setHistoryRange} />
          </section>

          <section className="lower-grid">
            <article className="panel-placeholder"><div className="section-heading"><div><p className="eyebrow">COLLECTION</p><h2>수집 상태</h2></div><span className="mini-badge">준비 중</span></div><div className="empty-state"><span className="empty-state__icon">⌁</span><strong>수집 상태 데이터가 없습니다</strong><p>에이전트 상태 조회가 연결되면 여기에서 확인할 수 있습니다.</p></div></article>
            <article className="panel-placeholder"><div className="section-heading"><div><p className="eyebrow">RECENT ACTIVITY</p><h2>최근 활동</h2></div><span className="mini-badge">준비 중</span></div><div className="empty-state"><span className="empty-state__icon">◷</span><strong>표시할 활동이 없습니다</strong><p>센서 이벤트와 상태 변화가 이 영역에 표시됩니다.</p></div></article>
          </section>
          {state === 'error' && <p className="notice" role="status">현재값을 불러오지 못했습니다. 서버 연결과 권한을 확인한 뒤 다시 갱신해 주세요.</p>}
          {state === 'unavailable' && <p className="notice" role="status">선택한 PC에 접근할 수 없거나 PC가 존재하지 않습니다.</p>}
        </div>
        <footer className="footer"><span>PC TELEMETRY <b>·</b> v0.1</span><span>모든 시각은 사용자 현지 시간으로 표시됩니다.</span></footer>
      </section>
    </main>
  )
}
