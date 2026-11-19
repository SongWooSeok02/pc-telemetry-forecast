type QueryActivity<T> = {
  get: () => T
  set: (value: T) => void
  busy: T
  onCancel?: () => void
}

/** Restore display state immediately on abort, even if transport settles later. */
export async function runQueryActivity<T>(signal: AbortSignal, activity: QueryActivity<T>, request: () => Promise<void>) {
  if (signal.aborted) return
  const previous = activity.get()
  let active = true
  const cancel = () => {
    if (!active) return
    active = false
    activity.set(previous)
    activity.onCancel?.()
  }
  signal.addEventListener('abort', cancel, { once: true })
  activity.set(activity.busy)
  try {
    await request()
  } finally {
    signal.removeEventListener('abort', cancel)
    // An obsolete request must not clear a newer request's busy indicator.
    if (active && activity.get() === activity.busy) activity.set(previous)
    active = false
  }
}
