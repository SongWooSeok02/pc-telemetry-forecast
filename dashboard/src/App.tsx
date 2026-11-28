import { useEffect, useMemo, useRef, useState } from 'react'
import HistoryChart, { type HistoryPoint, type HistoryResponse } from './HistoryChart'
import { mountDashboardScheduler, type QueryScheduler, type ScheduledRequest } from './queryScheduler'
import { runQueryActivity } from './queryActivity'
import ForecastPanel from './ForecastPanel'
import type { BrowserSession } from './browserSession'
import { QueryError, QueryVersion, responseError } from './queryErrors'
import QueryRecoveryNotice from './QueryRecoveryNotice'
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

const apiBase = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '') ?? ''
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
  const requestRef = useRef({ identity, request })
  requestRef.current = { identity, request }
  useEffect(() => {
    if (!enabled) return
    return scheduler.register(key, (signal) => {
      if (requestRef.current.identity === identity) return requestRef.current.request(signal)
    })
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

type DashboardProps = { machineId: string; setMachineId: (value: string) => void; session: BrowserSession; onLogout: () => void }

export default function App(props: DashboardProps) {
  const [scheduler, setScheduler] = useState<QueryScheduler | null>(null)
  useEffect(() => {
    // Each effect setup owns a fresh scheduler, including StrictMode replay.
    const mounted = mountDashboardScheduler()
    setScheduler(mounted.scheduler)
    return mounted.dispose
  }, [])
  return scheduler && <Dashboard {...props} scheduler={scheduler} />
}

function Dashboard({ machineId, setMachineId, session, onLogout, scheduler }: DashboardProps & { scheduler: QueryScheduler }) {
  const [snapshot, setSnapshot] = useState<CurrentSnapshot | null>(null)
  const [state, updateState] = useState<LoadState>('idle')
  const stateRef = useRef<LoadState>('idle')
  const setState = (value: LoadState) => { stateRef.current = value; updateState(value) }
  const [lastRefresh, setLastRefresh] = useState<string | undefined>()
  const [period, setPeriod] = useState<RefreshPeriod>(() => readRefreshPeriod())
  const [historyRange, setHistoryRange] = useState('24h')
  const [selectedSensorId, setSelectedSensorId] = useState('cpu_temperature')
  const [history, setHistory] = useState<HistoryResponse | null>(null)
  const [historyBusy, updateHistoryBusy] = useState(false)
  const historyBusyRef = useRef(false)
  const setHistoryBusy = (value: boolean) => { historyBusyRef.current = value; updateHistoryBusy(value) }
  const [historyError, setHistoryError] = useState<string | null>(null)
  const historyCache = useRef<{ scope: string; cursor: string | null; points: HistoryPoint[] }>({ scope: '', cursor: null, points: [] })

  const currentIdentity = machineId.trim().toLowerCase()
  const validMachine = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(currentIdentity)
  const [deniedMachine, setDeniedMachine] = useState('')
  const [currentError, setCurrentError] = useState<{ scope: string; message: string } | null>(null)
  const shownSnapshot = snapshot?.machineId === currentIdentity && deniedMachine !== currentIdentity ? snapshot : null
  const currentScope = `${session.scope}|${currentIdentity}|${deniedMachine}`
  const currentVersion = useRef(new QueryVersion()).current
  currentVersion.setScope(currentScope)
  const denyMachine = () => {
    setDeniedMachine(currentIdentity)
    setSnapshot(null)
    setHistory(null)
    setHistoryError(null)
    historyCache.current = { scope: '', cursor: null, points: [] }
    setState('unavailable')
  }

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

  useEffect(() => { scheduler.setPeriod(period) }, [scheduler, period])

  useEffect(() => {
    setState('idle')
    setCurrentError(null)
    setLastRefresh(undefined)
    if (!currentIdentity) setSnapshot(null)
  }, [currentIdentity])

  const loadCurrent: ScheduledRequest = async (signal) => {
    const version = currentVersion.capture(currentScope)
    if (!validMachine || !currentVersion.accepts(version, signal)) return
    return runQueryActivity(signal, { get: () => stateRef.current, set: setState, busy: 'loading' as LoadState }, async () => {
      try {
        const response = await session.request(`${apiBase}/v1/dashboard/machines/${encodeURIComponent(currentIdentity)}/current`, signal)
        if (!currentVersion.accepts(version, signal)) return
        if (!response.ok) throw await responseError(response)
        const data = await response.json() as CurrentSnapshot
        if (!currentVersion.accepts(version, signal)) return
        if (data.machineId !== currentIdentity) throw new QueryError('현재값 PC 응답 불일치', 422, 'INVALID_RESPONSE', false)
        setSnapshot(data)
        setLastRefresh(new Date().toISOString())
        setCurrentError(null)
        setState('ready')
      } catch (error) {
        if (!currentVersion.accepts(version, signal) || error instanceof DOMException && error.name === 'AbortError') return
        if (error instanceof QueryError && (error.status === 404 || error.status === 403)) denyMachine()
        else setState('error')
        setCurrentError({ scope: currentIdentity, message: error instanceof Error ? error.message : '조회 실패' })
        throw error
      }
    })
  }
  useScheduledQuery(scheduler, 'current', currentScope, validMachine && deniedMachine !== currentIdentity, loadCurrent)

  const historyIdentity = `${currentScope}|${selectedSensor?.sensorId ?? ''}|${selectedSensor?.unit ?? ''}|${historyRange}`
  const historyVersion = useRef(new QueryVersion()).current
  historyVersion.setScope(historyIdentity)
  const historyEnabled = Boolean(validMachine && deniedMachine !== currentIdentity && selectedSensor && selectedSensor.quality !== 'unsupported')
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
    const version = historyVersion.capture(historyIdentity)
    if (!historyEnabled || !selectedSensor || !historyVersion.accepts(version, signal)) return
    const previousError = historyError
    return runQueryActivity(signal, {
      get: () => historyBusyRef.current, set: setHistoryBusy, busy: true,
      onCancel: () => setHistoryError(previousError),
    }, async () => {
      const duration = rangeMs[historyRange] ?? rangeMs['24h']
      const resolution = duration <= rangeMs['6h'] ? 'raw' : duration <= rangeMs['24h'] ? '10s' : duration <= rangeMs['7d'] ? '1m' : duration <= rangeMs['30d'] ? '5m' : '1h'
      const scope = historyIdentity
      const cache = historyCache.current.scope === scope ? historyCache.current : { scope, cursor: null, points: [] }
      const to = new Date().toISOString()
      const from = new Date(Date.now() - duration).toISOString()
      const query = cache.cursor ? new URLSearchParams({ cursor: cache.cursor }) : new URLSearchParams({ from, to, resolution })
      try {
        const response = await session.request(`${apiBase}/v1/dashboard/machines/${encodeURIComponent(currentIdentity)}/sensors/${encodeURIComponent(selectedSensor.sensorId)}/history?${query}`, signal)
        if (!historyVersion.accepts(version, signal)) return
        if (!response.ok) throw await responseError(response)
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
        if (!validPoints || typeof data.aggregationPending !== 'boolean' || data.unit !== selectedSensor.unit || data.resolution !== resolution || typeof data.nextCursor !== 'string' || !data.nextCursor || data.machineId !== currentIdentity || data.sensorId !== selectedSensor.sensorId) throw new QueryError('조회 응답 형식 또는 센서·단위 매핑이 올바르지 않습니다.', 422, 'INVALID_RESPONSE', false)
        if (!historyVersion.accepts(version, signal) || historyCache.current.scope !== scope) return
        const merged = new Map(cache.points.map((point) => [point.bucketStart, point]))
        for (const point of data.points) merged.set(point.bucketStart, point)
        const points = [...merged.values()].filter((point) => new Date(point.bucketEnd).getTime() >= Date.now() - duration).sort((a, b) => a.bucketStart.localeCompare(b.bucketStart))
        historyCache.current = { scope, cursor: data.nextCursor, points }
        setHistory({ ...data, points })
        setHistoryError(null)
      } catch (error) {
        if (!historyVersion.accepts(version, signal) || error instanceof DOMException && error.name === 'AbortError') return
        if (error instanceof QueryError && (error.code === 'MACHINE_NOT_FOUND' || error.status === 403)) denyMachine()
        else setHistoryError(error instanceof Error ? error.message : '알 수 없는 오류')
        throw error
      }
    })
  }
  useScheduledQuery(scheduler, 'history', historyIdentity, historyEnabled, loadHistory)

  const choosePeriod = (value: RefreshPeriod) => {
    setPeriod(value)
    saveRefreshPeriod(value)
  }
  const statusText = state === 'ready' ? '연결됨' : state === 'loading' ? '조회 중' : state === 'error' ? '연결 오류' : state === 'unavailable' ? '데이터 없음' : validMachine ? '조회 대기' : 'PC 선택 필요'
  const statusClass = state === 'ready' ? 'is-connected' : state === 'error' ? 'is-error' : ''
  const shownHistory = history?.machineId === machineId.trim() && history.sensorId === selectedSensor?.sensorId && history.unit === selectedSensor?.unit ? history : null

  return (
    <main className="app-shell">
      <aside className="rail" aria-label="주 메뉴">
        <a className="brand-mark" href="#home" aria-label="PC Telemetry 홈"><span>◈</span></a>
        <button className="rail-button is-active" aria-label="대시보드">▦</button>
        <button className="rail-button" aria-label="장비">▣</button>
        <div className="rail-spacer" />
        <button className="rail-button rail-button--user" aria-label="계정">S</button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="breadcrumb"><span>모니터링</span><b>/</b><strong>대시보드</strong></div>
          <div className="topbar-actions">
            <button className="chart-reset" onClick={onLogout}>로그아웃</button>
            <label className="refresh-control">갱신
              <select value={period} onChange={(event) => choosePeriod(event.target.value === 'manual' ? 'manual' : Number(event.target.value) as RefreshPeriod)} aria-label="갱신 주기">
                {REFRESH_SECONDS.map((seconds) => <option key={seconds} value={seconds}>{seconds}초</option>)}
                <option value="manual">수동</option>
              </select>
            </label>
            <button className="refresh-button" onClick={() => { setDeniedMachine(''); scheduler.refreshNow() }} aria-label="지금 갱신">↻ <span>지금 갱신</span></button>
          </div>
        </header>

        <div className="content">
          <QueryRecoveryNotice scheduler={scheduler}/>
          <section className="page-heading">
            <div><p className="eyebrow">PC TELEMETRY / OVERVIEW</p><h1>시스템 현황</h1><p className="subtitle">등록된 PC의 현재 센서 상태를 한눈에 확인합니다.</p></div>
            <div className={`connection-pill ${statusClass}`}><span className="status-indicator" />{statusText}</div>
          </section>

          <section className="machine-panel">
            <div className="machine-panel__title"><div className="machine-icon">▰</div><div><span className="section-label">모니터링 대상</span><strong>{shownSnapshot?.machineName ?? 'PC 선택'}</strong></div></div>
            <label className="machine-input">Machine ID<input value={machineId} onChange={(event) => { setDeniedMachine(''); setMachineId(event.target.value) }} placeholder="등록된 PC ID 입력" /></label>
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
              ]).map((reading) => <ReadingCard key={reading.label} reading={reading} receivedAt={shownSnapshot?.receivedAt} />)}
            </div>
          </section>

          <section className="section-block">
            <label className="history-sensor">센서<select value={selectedSensor?.sensorId ?? ''} onChange={(event) => setSelectedSensorId(event.target.value)} aria-label="차트 센서">
              {sensors.map((sensor) => <option key={sensor.sensorId} value={sensor.sensorId}>{sensor.label} · {sensor.unit}</option>)}
            </select></label>
            <HistoryChart response={shownHistory} busy={historyBusy} error={historyCache.current.scope === historyIdentity ? historyError : null} sensorUnsupported={selectedSensor?.quality === 'unsupported'} selectedLabel={selectedSensor?.label ?? '센서'} rangeLabel={historyRange} onRangeChange={setHistoryRange} />
          </section>

          <ForecastPanel machineId={currentIdentity} sensors={shownSnapshot ? sensors : []} scheduler={scheduler} apiBase={apiBase} session={session} onMachineDenied={denyMachine}/>

          <section className="lower-grid">
            <article className="panel-placeholder"><div className="section-heading"><div><p className="eyebrow">COLLECTION</p><h2>수집 상태</h2></div><span className="mini-badge">준비 중</span></div><div className="empty-state"><span className="empty-state__icon">⌁</span><strong>수집 상태 데이터가 없습니다</strong><p>에이전트 상태 조회가 연결되면 여기에서 확인할 수 있습니다.</p></div></article>
            <article className="panel-placeholder"><div className="section-heading"><div><p className="eyebrow">RECENT ACTIVITY</p><h2>최근 활동</h2></div><span className="mini-badge">준비 중</span></div><div className="empty-state"><span className="empty-state__icon">◷</span><strong>표시할 활동이 없습니다</strong><p>센서 이벤트와 상태 변화가 이 영역에 표시됩니다.</p></div></article>
          </section>
          {currentError?.scope === currentIdentity && <p className="notice" role="status">현재값 조회 오류: {currentError.message} 이전 값의 수신 시각과 경과 시간을 확인해 주세요.</p>}
          {state === 'unavailable' && <p className="notice" role="status">선택한 PC의 현재값을 찾지 못했습니다.</p>}
        </div>
        <footer className="footer"><span>PC TELEMETRY <b>·</b> v0.1</span><span>모든 시각은 사용자 현지 시간으로 표시됩니다.</span></footer>
      </section>
    </main>
  )
}
