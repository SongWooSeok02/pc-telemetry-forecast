# S1-01 공동 검토와 완료 판정

검토 대상: v0.1.0-draft.2, [PR #1](https://github.com/SongWooSeok02/pc-telemetry-forecast/pull/1).
기한: 2026-10-30 23:59:59 KST. 승인 대상 버전/커밋을 반드시 적는다.

| 담당 | 확인할 계약 | 기록 |
|---|---|---|
| A | 부팅 ID·순번 영속화, 구성 버전, 해시, 원본 재전송, 단조 소스, 절전·재개 | GitHub 계정, 승인 시각, 승인 커밋, 리뷰 URL |
| B | 인증·오류, 수신 원장, 샘플 중복 키, 트랜잭션, 과거 구성 보관, 서버 시각 상태 | 동일 |
| C | 미지원/결측 구별, 단위·범위, 원본 UTC, 시각 불확실성과 학습 제외 | 동일 |

각 담당은 contracts/review-record.json의 자기 역할 값을 다음 형태로 채운다.
실제 승인 없이 대리 서명하거나 pending을 approved로 바꾸지 않는다.
```json
{"reviewer":"실제 GitHub 계정","decision":"approved","commit":"전체 Git 커밋 SHA","reviewed_at":"실제 ISO8601 시각","review_url":"해당 PR의 실제 리뷰 URL"}
```

수치 승인 시 decision_parameters.status를 approved로 바꾸고, 세 역할 모두 같은 버전을
검토했는지 확인한다. 서버 1000µs·클라이언트 16ms는 측정 상한 예산이며 보장된 정확도가
아니다. 실제 해상도와 동기화 상태가 예산을 만족하지 못하면 시각 판정을 유보하고
예산 변경을 재검토한다. 임의로 표본의 최소 간격을 정확도 보장으로 바꾸지 않는다.

완료 전 근거:

- 독립 JavaScript 생산자/Python HTTP API 검사 보고서와 CI run URL/검사 커밋.
- A의 실제 Windows 절전/재개·시간 역행·단조 소스 결과와 실제 환경/빌드/로그.
- Project 확정 일정·연동 카드 대조 기록(URL, 확인자, 실제 확인 시각).
- A/B/C 공동 승인과 작성자 외 팀원 1명 이상의 실제 PR 승인.

`ci`, `physical_windows_evidence`, `project_schedule_confirmation`에는 실제 URL 또는
저장소 파일 경로, SHA-256, 검사 커밋, 실행 시각과 성공/실패를 적는다. 빈 값은 미완료다.
현재 자동화 검증은 계약 참조 구현의 증거이며 후속 S1-03/S2-01의 운영 스택 검증을
대체하지 않는다. 실제 FastAPI/PostgreSQL 성능·파티션·서비스 복구는 해당 작업에서 수행한다.

미완료·차단 원인과 담당·다음 확인 시각은 review-record.json에서 추적한다.
파일 작성이나 합성 테스트 통과만으로 작업을 Done으로 변경하지 않는다.
