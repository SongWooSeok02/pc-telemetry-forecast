import { QueryError } from './queryErrors.ts'

export type RefreshMode = number | 'manual'
export type ScheduledRequest = (signal: AbortSignal) => Promise<void> | void

export interface SchedulerClock {
  now(): number
  setTimeout(callback: () => void, delayMs: number): ReturnType<typeof setTimeout>
  clearTimeout(timer: ReturnType<typeof setTimeout>): void
}

const browserClock: SchedulerClock = {
  now: () => performance.now(),
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (timer) => window.clearTimeout(timer),
}

type PendingReason = 'initial' | 'manual' | 'scheduled' | 'resume' | 'period'
type Task = {
  key: string
  request: ScheduledRequest
  pending: PendingReason | null
  lastStartedAt: number | null
  active: boolean
  running: boolean
  controller?: AbortController
  failures: number
  retryAt: number | null
  blocked: boolean
}

export type QueryStatus = { key: string; running: boolean; failures: number; blocked: boolean; retryInMs: number | null }

/** One monotonic clock and one in-flight request shared by all dashboard readers. */
export class QueryScheduler {
  private readonly tasks = new Map<string, Task>()
  private period: RefreshMode = 5
  private visible = false
  private runningTask: Task | null = null
  private timer: ReturnType<typeof setTimeout> | undefined
  private disposed = false
  private readonly clock: SchedulerClock
  private readonly listeners = new Set<() => void>()

  constructor(clock: SchedulerClock = browserClock) { this.clock = clock }

  subscribe(listener: () => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  status(): QueryStatus[] {
    return [...this.tasks.values()].map((task) => ({ key: task.key, running: task.running,
      failures: task.failures, blocked: task.blocked,
      retryInMs: this.visible && this.period !== 'manual' && !task.blocked && task.retryAt !== null
        ? Math.max(0, task.retryAt - this.clock.now()) : null }))
  }
  private notify() { for (const listener of this.listeners) listener() }
  private backoff(failures: number) { return Math.min(60_000, this.periodSeconds() * 1000 * 2 ** Math.min(failures - 1, 10)) }

  setPeriod(period: RefreshMode) {
    if (this.disposed || this.period === period) return
    if (period !== 'manual' && ![3, 5, 10, 30].includes(period)) throw new Error('Invalid refresh period')
    const wasManual = this.period === 'manual'
    this.period = period
    this.clearTimer()
    if (period === 'manual') {
      for (const task of this.tasks.values()) if (task.pending === 'scheduled' || task.pending === 'resume' || task.pending === 'period') task.pending = null
      for (const task of this.tasks.values()) task.retryAt = null
      this.notify()
      return
    }
    // A changed automatic interval applies immediately. If its request is already
    // running, the next due time is still measured from that request's start.
    for (const task of this.tasks.values()) {
      if (task.blocked || task.running) continue
      if (task.failures && !wasManual) task.retryAt = this.clock.now() + this.backoff(task.failures)
      else this.enqueue(task, 'period')
    }
    this.notify()
    this.drain()
  }

  setVisible(visible: boolean) {
    if (this.disposed || this.visible === visible) return
    this.visible = visible
    if (!visible) {
      this.clearTimer()
      for (const task of this.tasks.values()) if (task.pending === 'scheduled' || task.pending === 'resume' || task.pending === 'period') task.pending = null
      this.runningTask?.controller?.abort()
      this.notify()
      return
    }
    if (this.period !== 'manual') {
      for (const task of this.tasks.values()) if (!task.blocked) this.enqueue(task, 'resume')
    }
    this.drain()
  }

  register(key: string, request: ScheduledRequest) {
    if (this.disposed) return () => {}
    const previous = this.tasks.get(key)
    if (previous) this.removeTask(previous)
    const task: Task = { key, request, pending: 'initial', lastStartedAt: null, active: true, running: false, failures: 0, retryAt: null, blocked: false }
    this.tasks.set(key, task)
    this.notify()
    this.drain()
    return () => this.removeTask(task)
  }

  refreshNow() {
    if (this.disposed) return
    for (const task of this.tasks.values()) {
      if (task.running) task.pending = 'manual'
      else this.enqueue(task, 'manual')
    }
    this.drain()
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    this.clearTimer()
    for (const task of this.tasks.values()) {
      task.active = false
      task.controller?.abort()
    }
    this.tasks.clear()
    this.notify()
    this.listeners.clear()
  }

  private enqueue(task: Task, reason: PendingReason) {
    if (!task.active || this.disposed) return
    if (task.running && reason !== 'resume') return
    if (task.pending === 'manual' || task.pending === 'initial') return
    if (reason === 'manual' || reason === 'initial' || reason === 'resume' || !task.pending) task.pending = reason
  }

  private removeTask(task: Task) {
    if (!task.active) return
    task.active = false
    task.pending = null
    task.controller?.abort()
    if (this.tasks.get(task.key) === task) this.tasks.delete(task.key)
    this.notify()
    this.clearTimer()
    this.drain()
  }

  private drain() {
    if (this.disposed || !this.visible || this.runningTask) return
    const task = [...this.tasks.values()].find((candidate) => candidate.active && candidate.pending &&
      (!candidate.blocked || candidate.pending === 'manual' || candidate.pending === 'initial'))
    if (!task) {
      this.scheduleNext()
      return
    }
    const reason = task.pending
    task.pending = null
    if (reason === 'scheduled' && this.period === 'manual') {
      this.drain()
      return
    }
    task.running = true
    task.lastStartedAt = this.clock.now()
    task.retryAt = null
    const controller = new AbortController()
    task.controller = controller
    this.runningTask = task
    this.notify()
    Promise.resolve()
      .then(() => { if (task.active && !controller.signal.aborted) return task.request(controller.signal) })
      .then(() => {
        if (!task.active || controller.signal.aborted) return
        task.failures = 0
        task.retryAt = null
        task.blocked = false
      })
      .catch((error: unknown) => {
        if (!task.active || controller.signal.aborted) return
        task.failures++
        task.blocked = error instanceof QueryError && !error.retryable
        task.retryAt = task.blocked || this.period === 'manual' ? null : this.clock.now() + this.backoff(task.failures)
        if (task.blocked && task.pending !== 'manual') task.pending = null
      })
      .finally(() => {
        task.running = false
        task.controller = undefined
        if (this.runningTask === task) this.runningTask = null
        this.notify()
        this.drain()
      })
  }

  private scheduleNext() {
    this.clearTimer()
    if (this.disposed || !this.visible || this.period === 'manual' || this.runningTask || !this.tasks.size) return
    const now = this.clock.now()
    const periodMs = this.period * 1000
    const dueTimes = [...this.tasks.values()].filter((task) => task.active && !task.pending && !task.blocked)
      .map((task) => task.failures ? task.retryAt ?? Infinity : (task.lastStartedAt ?? now) + periodMs)
    if (!dueTimes.length) return
    if (!Number.isFinite(Math.min(...dueTimes))) return
    const delay = Math.max(0, Math.min(...dueTimes) - now)
    this.timer = this.clock.setTimeout(() => {
      this.timer = undefined
      const tick = this.clock.now()
      for (const task of this.tasks.values()) {
        const dueAt = task.failures ? task.retryAt ?? Infinity : (task.lastStartedAt ?? tick) + this.periodSeconds() * 1000
        if (task.active && !task.blocked && !task.pending && !task.running && dueAt <= tick) this.enqueue(task, 'scheduled')
      }
      this.drain()
    }, delay)
  }

  private periodSeconds() {
    return this.period === 'manual' ? 0 : this.period
  }

  private clearTimer() {
    if (this.timer !== undefined) this.clock.clearTimeout(this.timer)
    this.timer = undefined
  }
}

type HostVisibilityMessage = { type: 'pc-telemetry:host-visibility'; visible: boolean }

declare global {
  interface Window {
    chrome?: { webview?: Pick<EventTarget, 'addEventListener' | 'removeEventListener'> }
  }
}

/** Combines page visibility with the .NET WebView2 host's minimized state. */
export function bindDashboardVisibility(scheduler: QueryScheduler) {
  let hostVisible = true
  const update = () => scheduler.setVisible(document.visibilityState === 'visible' && hostVisible)
  const onDocumentVisibility = () => update()
  const onHostMessage = (event: Event) => {
    const data = (event as MessageEvent<unknown>).data
    if (!data || typeof data !== 'object') return
    const message = data as Partial<HostVisibilityMessage>
    if (message.type === 'pc-telemetry:host-visibility' && typeof message.visible === 'boolean') {
      hostVisible = message.visible
      update()
    }
  }
  document.addEventListener('visibilitychange', onDocumentVisibility)
  window.chrome?.webview?.addEventListener('message', onHostMessage)
  update()
  return () => {
    document.removeEventListener('visibilitychange', onDocumentVisibility)
    window.chrome?.webview?.removeEventListener('message', onHostMessage)
    scheduler.setVisible(false)
  }
}
