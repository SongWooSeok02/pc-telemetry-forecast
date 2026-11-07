export const REFRESH_STORAGE_KEY = 'telemetry.refresh-seconds'
export const REFRESH_SECONDS = [3, 5, 10, 30] as const

export type RefreshPeriod = typeof REFRESH_SECONDS[number] | 'manual'

export function parseRefreshPeriod(saved: string | null): RefreshPeriod {
  if (saved === null) return 5
  if (saved === 'manual' || saved === '0') return 'manual'
  return REFRESH_SECONDS.find((option) => String(option) === saved) ?? 5
}

export function readRefreshPeriod(): RefreshPeriod {
  try {
    return parseRefreshPeriod(window.localStorage.getItem(REFRESH_STORAGE_KEY))
  } catch {
    return 5
  }
}

export function saveRefreshPeriod(period: RefreshPeriod): void {
  try {
    window.localStorage.setItem(REFRESH_STORAGE_KEY, period === 'manual' ? 'manual' : String(period))
  } catch {
    // Keep the in-memory selection usable when browser storage is unavailable.
  }
}
