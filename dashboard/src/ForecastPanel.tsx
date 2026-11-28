import type { BrowserSession } from './browserSession'
import { QueryError, QueryVersion } from './queryErrors'
import { useEffect, useRef, useState } from 'react'
import ForecastChart from './ForecastChart'
import { emptyForecast, readForecastUpdate, scopeKey, type ForecastData, type ForecastScope } from './forecastData'
import { runQueryActivity } from './queryActivity'
import type { QueryScheduler } from './queryScheduler'

type Sensor = { sensorId: string; label: string; unit: string; quality: string }
type Props = { machineId: string; sensors: Sensor[]; scheduler: QueryScheduler; apiBase: string; session: BrowserSession; onMachineDenied: () => void }

export default function ForecastPanel({ machineId, sensors, scheduler, apiBase, session, onMachineDenied }: Props) {
  const [target, setTarget] = useState('cpu')
  const [sensorChoice, setSensorChoice] = useState('')
  const [hours, setHours] = useState(1)
  const [data, setData] = useState<ForecastData | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, updateError] = useState<string | null>(null)
  const errorRef = useRef<string | null>(null)
  const setError = (value: string | null) => { errorRef.current = value; updateError(value) }
  const candidates = sensors.filter((sensor) => ['C', 'Cel', '°C'].includes(sensor.unit) &&
    `${sensor.sensorId} ${sensor.label}`.toLowerCase().includes(target))
  const sensor = candidates.find((item) => item.sensorId === sensorChoice) ?? candidates[0]
  const scope: ForecastScope = { machineId, sensorId: sensor?.sensorId ?? '', unit: sensor?.unit ?? '', hours }
  const key = scopeKey(scope)
  const requestScope = `${session.scope}|${key}`
  const versionGuard = useRef(new QueryVersion()).current
  versionGuard.setScope(requestScope)
  const deniedRef = useRef(onMachineDenied)
  deniedRef.current = onMachineDenied
  const enabled = Boolean(machineId && sensor && sensor.quality !== 'unsupported')
  useEffect(() => {
    let active = true
    const revision = versionGuard.capture(requestScope)
    let cache = emptyForecast(scope)
    let activityBusy = false
    setData(null)
    setBusy(false)
    setError(null)
    if (!enabled) return
    const unregister = scheduler.register('forecast-comparison', async (signal) => {
      const previousError = errorRef.current
      return runQueryActivity(signal, {
        get: () => activityBusy,
        set: (value) => { activityBusy = value; if (active) setBusy(value) },
        busy: true,
        onCancel: () => { if (active) setError(previousError) },
      }, async () => {
        try {
          const updated = await readForecastUpdate((url, init) => session.request(url, init.signal), apiBase, scope, cache, signal, Date.now())
          if (!active || !versionGuard.accepts(revision, signal)) return
          cache = updated
          setData(updated)
          setError(null)
        } catch (failure) {
          if (!active || !versionGuard.accepts(revision, signal) || failure instanceof DOMException && failure.name === 'AbortError') return
          if (failure instanceof QueryError && (failure.code === 'MACHINE_NOT_FOUND' || failure.status === 403)) deniedRef.current()
          else setError(failure instanceof Error ? failure.message : '알 수 없는 조회 오류')
          throw failure
        }
      })
    })
    return () => { active = false; unregister() }
    // key contains all query scope fields. Registration changes only with scope.
  }, [scheduler, apiBase, requestScope, enabled, session])
  const shown = data?.scope === key && enabled ? data : null
  return <section className="section-block">
    <div className="forecast-controls">
      <label>예측 대상 <select aria-label="예측 대상" value={target} onChange={(event) => { setTarget(event.target.value); setSensorChoice('') }}><option value="cpu">CPU 온도</option><option value="gpu">GPU 온도</option></select></label>
      {candidates.length > 0 && <label>대상 센서 <select aria-label="예측 대상 센서" value={sensor?.sensorId ?? ''} onChange={(event) => setSensorChoice(event.target.value)}>{candidates.map((item) => <option key={item.sensorId} value={item.sensorId}>{item.label} · {item.sensorId}</option>)}</select></label>}
    </div>
    {!sensor && <p className="notice" role="status">선택 PC의 {target.toUpperCase()} 온도 센서 매핑을 기다립니다. 미관측을 미지원 또는 0으로 처리하지 않습니다.</p>}
    <ForecastChart data={shown} busy={enabled && busy} error={enabled ? error : null} unsupported={sensor?.quality === 'unsupported'} label={sensor?.label ?? `${target.toUpperCase()} 온도`} unit={sensor?.unit ?? ''} hours={hours} onHoursChange={setHours} now={Date.now()}/>
  </section>
}
