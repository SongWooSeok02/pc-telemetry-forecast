# S2-04 로컬 검증

전용 PostgreSQL의 `TEST_DATABASE_URL`을 설정하고 저장소 루트에서 실행한다.

```powershell
$env:BACKEND_REPORT = 'tests/results/s2-04-ingestion.json'
$env:REGISTRATION_REPORT = 'tests/results/s2-04-registration.json'
python tests/run_backend.py
python tests/run_registration.py
```

빌드 기준 커밋, 소스 해시, PC와 런타임, 실행 UTC 및 사례별 판정은
[수집 보고서](../tests/results/s2-04-ingestion.json)와
[등록·조회 보고서](../tests/results/s2-04-registration.json)에 기록했다.

| 재현 순서 | 기대값 | 실제값 |
| --- | --- | --- |
| 동일 배치를 8개 요청으로 동시에 전송 | 동일한 200 응답 8개, 배치 1 / 샘플 1 / 측정 3 | 일치 |
| 커밋 후 ASGI 응답 전달을 중단하고 다음 UTC 날짜에 재전송 | 원장에 저장된 200 응답 반환, 배치 1 / 샘플 1 / 측정 3 / 재집계 6 유지 | 일치 |
| 배치 ID 유지 후 본문 변경, 이어서 새 ID로 기존 샘플 재전송 | 각각 BATCH_CONFLICT / SAMPLE_CONFLICT, 행 수 유지 | 일치 |
| 수신 날짜의 측정 파티션이 없는 상태에서 삽입 | 503, 배치 / 샘플 / 측정 / 재집계 0행 | 일치 |
| 등록 직후 조회, 과거 관측값 배치 전송, 수신 후 120초와 120초 초과 시점 조회 | 오프라인 → 온라인 → 오프라인, 재전송 후 최초 수신 시각 유지 | 일치 |
| 다른 사용자 또는 삭제된 PC 조회 | 목록에서 제외, 상세 404 | 일치 |

PC 조회를 제공하는 API DB 역할에 기존 등록 권한과 함께 `ingestion_batches`,
`measurements`, `sensor_configs`의 SELECT 권한이 필요하다.

실제 Windows 에이전트와 서버의 30분 중단·재개 및 네트워크 응답 유실 공동 검증은
미실행이다. A·B가 전용 환경에서 중단 전후 큐의 배치 ID와 서버 원장·측정 행 수를 대조한다.
다음 확인 시점은 공동 테스트 환경 확보 시점이며, 원격 CI와 다른 팀원 1명의 리뷰도 남아 있다.
