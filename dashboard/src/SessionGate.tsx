import { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import Dashboard from './App'
import { BrowserSession, checkBrowserSession, logoutBrowserSession, type SessionEnd } from './browserSession'

type State = { kind: 'checking' | 'error' | 'signing-out' | 'logout-pending' } |
  { kind: 'active'; session: BrowserSession; deadline: number } | { kind: SessionEnd }

const selectionKey = 'pct:login-selection'

function readSelection() {
  const query = new URLSearchParams(window.location.search).get('machine_id')
  if (query) return query
  try { return sessionStorage.getItem(selectionKey) ?? '' } catch { return '' }
}

export default function SessionGate() {
  const [state, setState] = useState<State>({ kind: 'checking' })
  const [attempt, setAttempt] = useState(0)
  const [machineId, setMachineId] = useState(readSelection)
  useEffect(() => {
    try { sessionStorage.removeItem(selectionKey) } catch { /* Selection is optional. */ }
    const restore = (event: PageTransitionEvent) => {
      if (!event.persisted) return
      setState({ kind: 'checking' })
      setAttempt((n) => n + 1)
    }
    window.addEventListener('pageshow', restore)
    return () => window.removeEventListener('pageshow', restore)
  }, [])
  const [logoutScope, setLogoutScope] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    let session: BrowserSession | undefined
    void checkBrowserSession(AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)])).then((result) => {
      if (controller.signal.aborted) return
      if (result.reason) setState({ kind: result.reason })
      else {
        session = new BrowserSession(result.scope, (kind) => setState({ kind }))
        setState({ kind: 'active', session, deadline: result.deadline })
      }
    }).catch(() => {
      if (!controller.signal.aborted) setState({ kind: 'error' })
    })
    return () => { controller.abort(); session?.stop() }
  }, [attempt])

  const active = state.kind === 'active' ? state.session : null
  const deadline = state.kind === 'active' ? state.deadline : 0
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    let checking = false
    let expiry: number
    const expire = () => {
      const remaining = deadline - performance.now()
      if (remaining <= 0) active.end('expired')
      else expiry = window.setTimeout(expire, Math.min(remaining, 2_147_483_647))
    }
    expire()
    const recheck = async () => {
      if (performance.now() >= deadline) { active.end('expired'); return }
      if (checking || document.hidden) return
      checking = true
      try {
        const result = await checkBrowserSession(AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)]))
        if (!controller.signal.aborted && (result.reason || result.scope !== active.scope)) {
          active.end(result.reason ?? 'signed-out')
        }
      } catch {
        // No authenticated data is kept when we cannot confirm the current session.
        if (!controller.signal.aborted) {
          active.stop()
          setState({ kind: 'error' })
        }
      } finally {
        checking = false
      }
    }
    const interval = window.setInterval(() => { void recheck() }, 15_000)
    const onVisible = () => { void recheck() }
    const onPageHide = () => flushSync(() => active.end('signed-out'))
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('pagehide', onPageHide)
    return () => {
      controller.abort()
      window.clearInterval(interval)
      window.clearTimeout(expiry)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('pagehide', onPageHide)
    }
  }, [active, deadline])

  const logout = async (scope: string) => {
    active?.stop()
    setLogoutScope(scope)
    setState({ kind: 'signing-out' })
    try {
      await logoutBrowserSession(scope)
      setLogoutScope('')
      setState({ kind: 'signed-out' })
    } catch {
      setState({ kind: 'logout-pending' })
    }
  }

  if (active) return <Dashboard machineId={machineId} setMachineId={setMachineId}
    session={active} onLogout={() => { void logout(active.scope) }} />

  const message = {
    checking: '로그인 상태를 확인하고 있습니다.',
    error: '로그인 상태를 확인하지 못했습니다. 다시 확인해 주세요.',
    'signing-out': '로그아웃하고 있습니다.',
    'logout-pending': '서버에서 로그아웃을 확인하지 못했습니다. 다시 시도해 주세요.',
    'signed-out': '대시보드를 보려면 로그인해 주세요.',
    expired: '로그인이 만료되었습니다. 다시 로그인해 주세요.',
    active: '',
  }[state.kind]
  // The adapter handles login and returns to /. A PC ID is a selection hint only.
  const login = '/login'
  return <main className="content">
    <h1>PC Telemetry</h1>
    <p role="status">{message}</p>
    {(state.kind === 'signed-out' || state.kind === 'expired') && <a href={login} onClick={() => {
      if (/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(machineId.trim())) {
        try { sessionStorage.setItem(selectionKey, machineId.trim()) } catch { /* Login still works. */ }
      }
    }}>로그인</a>}
    {state.kind === 'error' && <button onClick={() => {
      setState({ kind: 'checking' })
      setAttempt((n) => n + 1)
    }}>다시 확인</button>}
    {state.kind === 'logout-pending' && <button onClick={() => { void logout(logoutScope) }}>로그아웃 다시 시도</button>}
  </main>
}
