import { useEffect, useState } from 'react'
import type { QueryScheduler, QueryStatus } from './queryScheduler'

export default function QueryRecoveryNotice({ scheduler }: { scheduler: QueryScheduler }) {
  const [tasks, setTasks] = useState<QueryStatus[]>([])
  useEffect(() => {
    const update = () => setTasks(scheduler.status())
    update()
    return scheduler.subscribe(update)
  }, [scheduler])
  const names: Record<string, string> = { current: '현재값', history: '과거 차트', 'forecast-comparison': '예측 비교' }
  const failed = tasks.filter((task) => task.failures > 0)
  if (!failed.length) return null
  return <div className="notice" role="status" aria-live="polite">{failed.map((task) => <p key={task.key}>
    {names[task.key] ?? task.key}: {task.blocked ? '자동 조회 중단 · 권한/요청 조건을 확인해 주세요.' : task.running ? '재조회 중 · 이전 값과 오류를 유지합니다.' : task.retryInMs !== null ? `다음 재시도 대기 ${Math.ceil(task.retryInMs / 1000)}초 (이 상태 갱신 시점 기준)` : '자동 재시도 없음 · 지금 갱신으로 다시 조회할 수 있습니다.'}
  </p>)}</div>
}
