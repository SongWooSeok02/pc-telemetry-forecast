export class QueryError extends Error {
  readonly retryable: boolean
  readonly status: number
  readonly code: string
  constructor(message: string, status: number, code: string, retryable: boolean) {
    super(message)
    this.name = 'QueryError'
    this.status = status
    this.code = code
    this.retryable = retryable
  }
}

export async function responseError(response: { status: number; json(): Promise<unknown> }): Promise<QueryError> {
  let code = `HTTP_${response.status}`
  let retryable = response.status === 408 || response.status === 429 || response.status >= 500
  try {
    const body = await response.json() as { error?: { code?: unknown; retryable?: unknown } }
    if (typeof body?.error?.code === 'string') code = body.error.code
    // An explicit permanent server error can narrow, never broaden, HTTP policy.
    if (body?.error?.retryable === false) retryable = false
  } catch { /* HTTP status remains authoritative when the error body is invalid. */ }
  return new QueryError(`조회 오류 ${response.status} · ${code}`, response.status, code, retryable)
}

/** A render-time scope revision also rejects callbacks before effect cleanup runs. */
export class QueryVersion {
  private scope = ''
  private revision = 0
  setScope(scope: string) {
    if (scope !== this.scope) { this.scope = scope; this.revision++ }
  }
  capture(scope: string) { return scope === this.scope ? this.revision : -1 }
  accepts(revision: number, signal: AbortSignal) {
    return revision >= 0 && revision === this.revision && !signal.aborted
  }
}
