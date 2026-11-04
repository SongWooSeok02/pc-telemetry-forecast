import { useEffect, useState } from 'react'

type SensorReading = {
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

const refreshOptions = [5, 10, 30] as const
const apiBase = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '') ?? ''

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

function ReadingCard({ reading }: { reading: SensorReading }) {
  const unavailable = reading.quality !== 'ok' || reading.value === null
  const status = reading.quality === 'unsupported' ? '미지원' : reading.quality === 'error' ? '오류' : reading.quality === 'missing' ? '측정 대기' : '데이터 없음'
  return (
    <article className="reading-card">
      <div className="reading-card__top"><span>{reading.label}</span><span className="reading-dot" /></div>
      <div className={`reading-card__value${unavailable ? ' is-muted' : ''}`}>
        {unavailable ? <span className="reading-card__empty">{status}</span> : <>{reading.value}<small>{reading.unit}</small></>}
      </div>
      <div className="reading-card__foot">관측 {formatTime(reading.observedAt)}</div>
    </article>
  )
}

export default function App() {
  const queryMachine = new URLSearchParams(window.location.search).get('machine_id') ?? ''
  const [machineId, setMachineId] = useState(queryMachine)
  const [snapshot, setSnapshot] = useState<CurrentSnapshot | null>(null)
  const [state, setState] = useState<LoadState>('idle')
  const [lastRefresh, setLastRefresh] = useState<string | undefined>()
  const [period, setPeriod] = useState<number | 'manual'>(5)
  const [requestKey, setRequestKey] = useState(0)

  useEffect(() => {
    const stored = Number(localStorage.getItem('telemetry.refresh-seconds'))
    if ([...refreshOptions, 0].includes(stored as 0 | 5 | 10 | 30)) setPeriod(stored === 0 ? 'manual' : stored)
  }, [])

  useEffect(() => {
    if (!machineId.trim()) {
      setState('idle')
      setSnapshot(null)
      return
    }
    let active = true
    let timer: number | undefined
    let controller: AbortController | undefined

    let schedule: () => void = () => {}
    const load = async () => {
      controller = new AbortController()
      setState('loading')
      try {
        const response = await fetch(`${apiBase}/v1/dashboard/machines/${encodeURIComponent(machineId.trim())}/current`, { signal: controller.signal })
        if (response.status === 404) {
          if (active) { setSnapshot(null); setState('unavailable') }
          return
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json() as CurrentSnapshot
        if (active) {
          setSnapshot(data)
          setLastRefresh(new Date().toISOString())
          setState('ready')
        }
      } catch (error) {
        if (active && !(error instanceof DOMException && error.name === 'AbortError')) setState('error')
      } finally {
        schedule()
      }
    }
    schedule = () => {
      if (active && period !== 'manual') timer = window.setTimeout(() => { void load() }, period * 1000)
    }

    void load()
    return () => { active = false; controller?.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [machineId, period, requestKey])

  const choosePeriod = (value: number | 'manual') => {
    setPeriod(value)
    localStorage.setItem('telemetry.refresh-seconds', value === 'manual' ? '0' : String(value))
  }
  const statusText = state === 'ready' ? '연결됨' : state === 'loading' ? '조회 중' : state === 'error' ? '연결 오류' : state === 'unavailable' ? '데이터 없음' : 'PC 선택 필요'
  const statusClass = state === 'ready' ? 'is-connected' : state === 'error' ? 'is-error' : ''

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
              <select value={period} onChange={(event) => choosePeriod(event.target.value === 'manual' ? 'manual' : Number(event.target.value))} aria-label="갱신 주기">
                {refreshOptions.map((seconds) => <option key={seconds} value={seconds}>{seconds}초</option>)}
                <option value="manual">수동</option>
              </select>
            </label>
            <button className="refresh-button" onClick={() => setRequestKey((key) => key + 1)} aria-label="지금 갱신">↻ <span>지금 갱신</span></button>
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
            <div className="machine-panel__meta"><span>마지막 수신</span><strong>{formatTime(snapshot?.receivedAt)}</strong><i /> <span>화면 갱신</span><strong>{formatTime(lastRefresh)}</strong></div>
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
              ]).map((reading) => <ReadingCard key={reading.label} reading={reading} />)}
            </div>
          </section>

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
