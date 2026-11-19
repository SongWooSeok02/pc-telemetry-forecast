export type SessionEnd = 'expired' | 'signed-out'

export function sessionEnd(response: Response): Promise<SessionEnd | null> {
  if (response.status !== 401) return Promise.resolve(null)
  return response.json().then(
    (body) => body?.error?.code === 'USER_SESSION_EXPIRED' ? 'expired' : 'signed-out',
    () => 'signed-out',
  )
}

export class BrowserSession {
  private readonly controller = new AbortController()

  readonly scope: string
  private readonly onEnd: (reason: SessionEnd) => void

  constructor(scope: string, onEnd: (reason: SessionEnd) => void) {
    this.scope = scope
    this.onEnd = onEnd
  }

  stop() {
    this.controller.abort()
  }

  end(reason: SessionEnd) {
    if (this.controller.signal.aborted) return
    this.stop()
    this.onEnd(reason)
  }

  // This is the protected fetch boundary for scheduled current and history queries.
  async request(path: string, signal?: AbortSignal) {
    const combined = signal ? AbortSignal.any([signal, this.controller.signal]) : this.controller.signal
    combined.throwIfAborted()
    const response = await fetch(path, {
      signal: combined, cache: 'no-store', credentials: 'same-origin', redirect: 'error',
      headers: { 'X-Pct-Session': this.scope },
    })
    // A 401 already received for this session remains valid even if the PC changed.
    const reason = await sessionEnd(response)
    if (reason) this.end(reason)
    combined.throwIfAborted()
    return response
  }
}

export async function checkBrowserSession(signal: AbortSignal) {
  const started = performance.now()
  const response = await fetch('/v1/user/session', {
    signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error',
  })
  const reason = await sessionEnd(response)
  if (reason) return { reason }
  if (!response.ok) throw new Error('Session check failed')
  const { scope, expires_in_ms } = await response.json()
  if (typeof scope !== 'string' || !/^[a-f0-9]{64}$/.test(scope) || !Number.isSafeInteger(expires_in_ms)) throw new Error('Invalid session scope')
  const deadline = started + expires_in_ms
  return deadline > performance.now() ? { scope, deadline } : { reason: 'expired' as const }
}

export async function logoutBrowserSession(scope: string) {
  const response = await fetch('/v1/user/session/logout', {
    method: 'POST', cache: 'no-store', credentials: 'same-origin', redirect: 'error',
    signal: AbortSignal.timeout(10_000),
    headers: { 'X-Pct-Session': scope },
  })
  if (response.status !== 204 && response.status !== 401) throw new Error('Logout failed')
}
