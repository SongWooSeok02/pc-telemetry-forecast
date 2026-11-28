import { useEffect, useMemo, useRef, useState } from 'react'
import HistoryChart, { type HistoryPoint, type HistoryResponse } from './HistoryChart'
import { fetchHistorySeries } from './historyQuery.ts'
import { bindDashboardVisibility, QueryScheduler, type ScheduledRequest } from './queryScheduler'
import { runQueryActivity } from './queryActivity'
import ForecastPanel from './ForecastPanel'
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
  receivedAt: string
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

function useScheduledQuery(scheduler: QueryScheduler, key: string, identity: string, enabled: boolean, request: ScheduledRequest) {
  const requestRef = useRef(request)
  requestRef.current = request
  useEffect(() => {
    if (!enabled) return
    return scheduler.register(key, (signal) => requestRef.current(signal))
  }, [scheduler, key, identity, enabled])
}

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

function formatAge(value?: string) {
  if (!value) return '—'
  const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return '시각 확인 필요'
  return seconds < 60 ? `${seconds}초` : seconds < 3600 ? `${Math.floor(seconds / 60)}분` : `${Math.floor(seconds / 3600)}시간`
}

function formatLag(observedAt?: string, receivedAt?: string) {
  if (!observedAt || !receivedAt) return '—'
  const seconds = Math.floor((new Date(receivedAt).getTime() - new Date(observedAt).getTime()) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return '시각 확인 필요'
  return `${seconds}초`
}

function ReadingCard({ reading, receivedAt }: { reading: SensorReading; receivedAt?: string }) {
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

export default function App() {
  const queryMachine = new URLSearchParams(window.location.search).get('machine_id') ?? ''
  const [machineId, setMachineId] = useState(queryMachine)
  const [snapshot, setSnapshot] = useState<CurrentSnapshot | null>(null)
  const [state, updateState] = useState<LoadState>('idle')
  const stateRef = useRef<LoadState>('idle')
  const setState = (value: LoadState) => { stateRef.current = value; updateState(value) }
  const [lastRefresh, setLastRefresh] = useState<string | undefined>()
  const [period, setPeriod] = useState<RefreshPeriod>(() => readRefreshPeriod())
  const [scheduler] = useState(() => new QueryScheduler())
  const [historyRange, setHistoryRange] = useState('24h')
  const [selectedSensorId, setSelectedSensorId] = useState('cpu_temperature')
  const [history, setHistory] = useState<HistoryResponse | null>(null)
  const [historyBusy, updateHistoryBusy] = useState(false)
  const historyBusyRef = useRef(false)
  const setHistoryBusy = (value: boolean) => { historyBusyRef.current = value; updateHistoryBusy(value) }
  const [historyError, setHistoryError] = useState<string | null>(null)
  const historyCache = useRef<{ scope: string; cursor: string | null; points: HistoryPoint[] }>({ scope: '', cursor: null, points: [] })

  const sensors = useMemo(() => {
    if (!snapshot?.readings.length) return sensorDefaults
    return snapshot.readings.map((reading) => ({
      sensorId: reading.sensorId ?? sensorDefaults.find((sensor) => sensor.label === reading.label)?.sensorId ?? reading.label,
      label: reading.label,
      unit: reading.unit,
      quality: reading.quality,
    }))
  }, [snapshot])
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

  const currentIdentity = machineId.trim()
  const loadCurrent: ScheduledRequest = async (signal) => {
    if (!currentIdentity) return
    return runQueryActivity(signal, { get: () => stateRef.current, set: setState, busy: 'loading' as LoadState }, async () => {
      try {
        const response = await fetch(`${apiBase}/v1/dashboard/machines/${encodeURIComponent(currentIdentity)}/current`, { signal })
        if (signal.aborted) return
        if (response.status === 404) { setSnapshot(null); setState('unavailable'); return }
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json() as CurrentSnapshot
        if (signal.aborted || data.machineId !== currentIdentity) return
        setSnapshot(data)
        setLastRefresh(new Date().toISOString())
        setState('ready')
      } catch (error) {
        if (!signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setState('error')
      }
    })
  }
  useScheduledQuery(scheduler, 'current', currentIdentity, Boolean(currentIdentity), loadCurrent)

  const historyIdentity = `${currentIdentity}|${selectedSensor?.sensorId ?? ''}|${historyRange}`
  const historyEnabled = Boolean(currentIdentity && selectedSensor && selectedSensor.quality !== 'unsupported')
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
    const previousError = historyError
    return runQueryActivity(signal, {
      get: () => historyBusyRef.current, set: setHistoryBusy, busy: true,
      onCancel: () => setHistoryError(previousError),
    }, async () => {
      setHistoryError(null)
      const scope = historyIdentity
      const cache = historyCache.current.scope === scope ? historyCache.current : { scope, cursor: null, points: [] }
      try {
        const result = await fetchHistorySeries({
          apiBase,
          machineId: currentIdentity,
          sensorId: selectedSensor.sensorId,
          unit: selectedSensor.unit,
          range: historyRange,
          scope,
          cache,
          signal,
          onCursorReset: () => {
            if (!signal.aborted && historyCache.current.scope === scope) {
              historyCache.current = { scope, cursor: null, points: [] }
            }
          },
        })
        if (signal.aborted || historyCache.current.scope !== scope) return
        historyCache.current = result.cache
        setHistory(result.response)
      } catch (error) {
        if (!signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setHistoryError(error instanceof Error ? error.message : '알 수 없는 오류')
      }
    })
  }
  useScheduledQuery(scheduler, 'history', historyIdentity, historyEnabled, loadHistory)

  const choosePeriod = (value: RefreshPeriod) => {
    setPeriod(value)
    saveRefreshPeriod(value)
  }
  const statusText = state === 'ready' ? '연결됨' : state === 'loading' ? '조회 중' : state === 'error' ? '연결 오류' : state === 'unavailable' ? '데이터 없음' : 'PC 선택 필요'
  const statusClass = state === 'ready' ? 'is-connected' : state === 'error' ? 'is-error' : ''
  const shownHistory = history?.machineId === machineId.trim() && history.sensorId === selectedSensor?.sensorId ? history : null

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
            <div className="machine-panel__title"><div className="machine-icon">▰</div><div><span className="section-label">모니터링 대상</span><strong>{snapshot?.machineName ?? 'PC 선택'}</strong></div></div>
            <label className="machine-input">Machine ID<input value={machineId} onChange={(event) => setMachineId(event.target.value)} placeholder="등록된 PC ID 입력" /></label>
            <div className="machine-panel__meta"><span>마지막 수신</span><strong>{formatTime(snapshot?.receivedAt)}</strong><small>{formatAge(snapshot?.receivedAt)} 전</small><i /> <span>화면 갱신</span><strong>{formatTime(lastRefresh)}</strong></div>
          </section>

          <section className="section-block">
            <div className="section-heading"><div><p className="eyebrow">LIVE TELEMETRY</p><h2>현재 센서값</h2></div><span className="section-caption">수신 시각 기준 최신 관측값</span></div>
            <div className="reading-grid">
              {(snapshot?.readings ?? [
                { label: 'CPU 온도', value: null, unit: '°C', quality: 'missing' as const },
                { label: 'GPU 온도', value: null, unit: '°C', quality: 'missing' as const },
                { label: 'CPU 사용률', value: null, unit: '%', quality: 'missing' as const },
                { label: 'GPU 사용률', value: null, unit: '%', quality: 'missing' as const },
                { label: 'CPU 전력', value: null, unit: 'W', quality: 'missing' as const },
                { label: 'GPU 전력', value: null, unit: 'W', quality: 'missing' as const },
              ]).map((reading) => <ReadingCard key={reading.label} reading={reading} receivedAt={snapshot?.receivedAt} />)}
            </div>
          </section>

          <section className="section-block">
            <label className="history-sensor">센서<select value={selectedSensor?.sensorId ?? ''} onChange={(event) => setSelectedSensorId(event.target.value)} aria-label="차트 센서">
              {sensors.map((sensor) => <option key={sensor.sensorId} value={sensor.sensorId}>{sensor.label} · {sensor.unit}</option>)}
            </select></label>
            <HistoryChart response={shownHistory} busy={historyBusy} error={historyError} sensorUnsupported={selectedSensor?.quality === 'unsupported'} selectedLabel={selectedSensor?.label ?? '센서'} rangeLabel={historyRange} onRangeChange={setHistoryRange} />
          </section>

          <ForecastPanel machineId={currentIdentity} sensors={snapshot?.machineId === currentIdentity ? sensors : []} scheduler={scheduler} apiBase={apiBase}/>

          <section className="lower-grid">
            <article className="panel-placeholder"><div className="section-heading"><div><p className="eyebrow">COLLECTION</p><h2>수집 상태</h2></div><span className="mini-badge">준비 중</span></div><div className="empty-state"><span className="empty-state__icon">⌁</span><strong>수집 상태 데이터가 없습니다</strong><p>에이전트 상태 조회가 연결되면 여기에서 확인할 수 있습니다.</p></div></article>
            <article className="panel-placeholder"><div className="section-heading"><div><p className="eyebrow">RECENT ACTIVITY</p><h2>최근 활동</h2></div><span className="mini-badge">준비 중</span></div><div className="empty-state"><span className="empty-state__icon">◷</span><strong>표시할 활동이 없습니다</strong><p>센서 이벤트와 상태 변화가 이 영역에 표시됩니다.</p></div></article>
          </section>
          {state === 'error' && <p className="notice" role="status">현재값을 불러오지 못했습니다. 서버 연결과 권한을 확인한 뒤 다시 갱신해 주세요.</p>}
          {state === 'unavailable' && <p className="notice" role="status">선택한 PC의 현재값을 찾지 못했습니다.</p>}
        </div>
        <footer className="footer"><span>PC TELEMETRY <b>·</b> v0.1</span><span>모든 시각은 사용자 현지 시간으로 표시됩니다.</span></footer>
      </section>
    </main>
  )
}
