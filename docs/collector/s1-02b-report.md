# S1-02b CPU·GPU 센서 1초 수집 — 구현·검증 보고서

상태: **구현·PC-A 실측(참조 API·S1-03 FastAPI/PostgreSQL) 완료, 실제 절전/재개 확인, 완료 조건 일부 미충족(2번째 PC).
다른 팀원 리뷰 전. 완료로 변경하지 않는다.** 담당 A. 기간 2026-10-29~11-03 KST(마감 초과).
선행: S1-01 `0.1.0-draft.2`(변경 없음), S1-02a 판정 초안(범위 축소 조건부 채택).

## 1. 완료 조건 대조

| 조건 | 결과 | 근거 |
|---|---|---|
| S1-02a 통과 후 CPU·GPU 센서 탐색, 1초 주기 수집 | **PC-A 충족** | 5절 실측. PC-B 미관측 |
| 온도·전력·사용률·클럭·팬을 단위·출처별로 구분 | 충족 | [센서 표 2절](../sensor-support.md), 등록 메타데이터 kind/unit/source/scope |
| 미지원 값을 0으로 만들지 않음 | 충족 | 드라이버 없음 → unsupported, 끊김 → error. 실측 `non_ok_with_value=0` |
| 한 센서 오류로 전체 수집을 멈추지 않음 | 충족(가상) | 4절 T5·T6. 실제 장치 오류는 관측되지 않음 |
| 재부팅·절전/재개·센서 변경 이벤트 생성 | 절전/재개: PC-A 실제 Modern Standby 1회 확인. 센서 변경: 가상 검증. 재부팅: 같은 상태 디렉터리 재시작으로만 확인 | 4절, 6절 |
| 실제 PC 2대의 원시 샘플을 API 경로에 연결 | **PC-A 1대.** 참조 HTTP API와 S1-03 FastAPI/PostgreSQL(compose) 모두 수락. PC-B 미연결 | 5절, 6절 |

## 2. 구현

코드: [agents/windows](../../agents/windows/README.md).

- **매핑**: LHM 원천 → 논리 sensor_id 15개(CPU 8, GPU 7). 원칙·표·제외 사유는 [sensor-support.md](../sensor-support.md).
- **1초 루프**: `PeriodicTimer(1s)`. 관측 시각 `observed_at`(벽시계 UTC, 6자리)과 `monotonic_ms`
  (`QueryUnbiasedInterruptTime/10000` 내림)를 읽기 직전에 함께 기록한다. 시작 후 2회 읽기는 워밍업으로 버린다(순번 미사용).
  주기 지연 시 다음 틱으로 넘어가며(누락 허용) 순번은 실제 샘플에만 쓴다.
- **실패 격리**: 장치별 `Update()` 예외는 그 장치 센서만 `error/read_failed`. 원천 전체 예외도 해당 주기를 error로 보고하고
  루프는 계속한다. 예기치 못한 주기 예외는 `collector_cycle_failed` 이벤트 후 계속.
- **부팅 세션·순번**: `HKLM\...\PrefetchParameters\BootId`(부팅마다 증가) → 상태 파일의 BootId와 다르면 새 UUID·순번 0.
  같으면 서비스 재시작으로 보고 세션 유지. 순번은 600개 단위로 예약해 디스크에 먼저 기록하므로, 재시작 시 예약 상한부터
  이어간다(누락 허용·재사용 없음). BootId를 읽지 못하면 `collection_held` 후 30초마다 재시도(계약: 수집 유보).
- **절전/재개·시계 급변**: 주기마다 절전 포함 `QueryInterruptTime`과 절전 제외 시계의 차이가 1초 이상이면 `suspend_resume`
  (절전 길이·직전 순번), 벽시계와 절전 포함 시계의 차이가 2초 이상이면 `clock_jump`. 재개 시 대기 샘플을 먼저 동결해
  절전을 가로지르는 배치를 만들지 않고, 간격 통계에서도 제외한다.
- **센서 변경**: 하드웨어·센서 구조가 바뀌거나 드라이버 접근이 5주기 연속 바뀌면 재탐색. 정의가 달라지면 새
  `sensor_config_id`(`sensor_config_changed`: 추가·제거·지원 변경 목록), 이전 구성 샘플은 먼저 동결한다.
  드라이버가 끊긴 디바운스 동안 CPU 전력은 0 대신 `error/driver_unavailable`.
- **품질 이벤트**: 센서별 품질 전이(`sensor_quality_changed`), 센서당 60초 쿨다운·억제 횟수 기록.
- **배치·전송**: 3샘플(3초)마다 1MiB·3600개 상한으로 ID 발급 전 분할 → UUID v4 발급과 정규 본문(`ascii-json-v1`)을
  한 파일로 원자 기록(`spool/`). 업로더는 오래된 순으로 같은 본문·해시만 재전송한다. 응답 처리: 200 삭제,
  연결 실패·429/5xx/504·`SENSOR_CONFIG_PENDING` 재시도(3/6/12/24/48/60초 +0~20%), 401/403 중지, 그 외 409/413/422 격리.
  스풀 상한 24시간/500MB 초과 시 오래된 배치부터 제거하고 `buffer_pruned` 기록.
- **이벤트 저장**: `events.jsonl`(로컬). 계약에 이벤트 전송 경로가 없어 서버로 보내지 않는다(7절).

## 3. 검증 환경

| 항목 | 값 |
|---|---|
| PC | PC-A(S1-02a와 동일 노트북): Intel Core Ultra 9 185H, NVIDIA RTX 4060 Laptop(드라이버 32.0.15.9282), Windows 11 Pro 26200 |
| 보안 | Secure Boot·HVCI·취약 드라이버 차단 목록·Defender 실시간/변조 방지 켜짐 |
| 권한·드라이버 | 관리자 PowerShell(LocalSystem 아님), PawnIO 2.2.0 |
| 빌드 | .NET SDK 10.0.300, PcTelemetry.Agent 0.1.0 Release win-x64 self-contained, LibreHardwareMonitorLib 0.9.6 |
| 기반 커밋 | `2630383`(main, PR #8) + 미커밋 S1-02b 변경 |
| API ① | `tests/reference_api.py`(S1-01 HTTP 참조 API, SQLite, loopback, 합성 토큰·machine_id) |
| API ② | S1-03 `backend/` FastAPI + PostgreSQL 16, `infra/compose.yaml`(별도 프로젝트 `pctf-s1-02b`, 임시 비밀번호), Docker Engine 29.9.2, API 이미지 `sha256:bcbd9f90482a…`. 장비 토큰 SHA-256·구성은 db 컨테이너 psql로 관리자 등록(backend/README). 11-04~11-08 실행은 S2-03 병합 전 main `2630383` 기준 |

## 4. 자동 검증 (가상 센서·가짜 시계)

`dotnet test agents/windows/tests/PcTelemetry.Collector.Tests` — 2026-11-04 KST, **41/41 통과**. CI: `.github/workflows/agent.yml`(windows-latest).

| # | 사례 | 기대값 | 결과 |
|---|---|---|---|
| T1 | 계약 공유 해시 벡터 5개, fixture 배치 재현 | 정규 본문·SHA-256 일치 | 통과 |
| T2 | 숫자 상한·비ASCII·중복 키 | 거부 | 통과 |
| T3 | 매핑 | 15개, ID 형식·유일, iGPU·TjMax·CPU Memory·중복 식별자 제외, 노트북 팬 unsupported | 통과 |
| T4 | 정상 1초 | 순번 0..8 연속, 단조 1000ms, 3샘플 배치 3개, 비 ok는 null | 통과 |
| T5 | GPU 장치 예외 → 값 null → 복구 | GPU error/read_failed → missing/no_value → ok, CPU 계속 ok, 전이 이벤트 3 | 통과 |
| T6 | 원천 전체 예외 | 해당 주기 error, 다음 순번 정상 | 통과 |
| T7 | 드라이버 없음(전력 0) | CPU 온도·전력·클럭 unsupported+null, 0 없음 | 통과 |
| T8 | 드라이버 끊김 | 디바운스 동안 error/driver_unavailable, 이후 새 구성, 배치 구성 혼합 없음 | 통과 |
| T9 | GPU 제거·복귀 | 새 구성(component_absent), 직전 샘플 먼저 동결, 복귀 시 기존 구성 ID 재사용 | 통과 |
| T10 | GPU 교체 | `gpu0-r2/*` 새 ID | 통과 |
| T11 | 1시간 절전 | `suspend_resume` 1, clock_jump 0, 단조 시간 절전 제외, 재개 전 배치 분리 | 통과 |
| T12 | 벽시계 -5초 | `clock_jump` -6000ms | 통과 |
| T13 | 서비스 재시작(같은 부팅) | 세션 유지, 순번 재사용 없음, 구성 ID 유지 | 통과 |
| T14 | 재부팅(BootId 변경) | 새 세션, 순번 0, `boot_session_started: reboot` | 통과 |
| T15 | BootId 불가 | 수집 유보 예외 | 통과 |
| T16 | 3601개 샘플, 응답 코드 12종, 스풀 24시간 상한 | 분할 후 ID 2개 발급, 계약 처리표, 만료 배치 제거·이벤트 | 통과 |

## 5. 실제 PC 검증 (PC-A)

재현: `python tests/run_agent_integration.py --exe <PcTelemetry.Agent.exe> --seconds <N> [--restarts 1] --pc-label pc-a`
(② S1-03 경로는 `infra/README` 절차로 compose를 띄운 뒤 `--backend compose --compose-env <env 파일>` 추가).
구성 등록은 5초 늦게 해 `SENSOR_CONFIG_PENDING` 재시도 경로를 거친다. 보고서에는 집계만 남긴다(호스트명·사용자명·원시 시계열 제외).

| 실행(UTC) | 사례 | 기대값 | 실제값 | 결과 |
|---|---|---|---|---|
| 2026-11-04 07:34:49~07:35:49 | 60초 | 수집=수락, 중복·누락 0, 간격 ±100ms ≥99%, 지원 센서 ok ≥99%, 0 대신 null | 58/58 수락, 중복 0·누락 0, 간격 987~1013ms(100%), ok 100%, 409 PENDING 1회 후 수락, 격리·잔여 0 | 성공 |
| 2026-11-04 07:37:43~07:38:27 (20초×2) | 같은 부팅 재시작 1회 | 세션 1개, 순번 재사용 없음, 누락은 예약 건너뜀 1곳 | 세션 1, 순번 0~17 → 600~, 중복 0, 간격 988~1006ms(100%) | 성공 |
| 2026-11-04 07:38:36~07:48:38 | ① 10분 | 위와 같음 | 598/598 수락(배치 200), 중복 0·누락 0, 간격 984~1017ms(597/597 정시), 지원 센서 ok 100%, 0 대신 null 위반 0, 격리·잔여 0 | 성공 |
| 2026-11-06 00:44:41~00:45:45 | ② FastAPI/PostgreSQL 60초 | ①과 같음 | 58/58 수락(배치 20, 측정 870행), 중복 0·누락 0, 간격 정시 100%, ok 100%, 409 PENDING 1회 후 수락, 격리·잔여 0. DB: ok 754행 모두 값 있음, unsupported 116행 모두 value NULL | 성공 |
| 2026-11-06 00:46 | ② 재전송·충돌 | 같은 본문 200·최초 응답 동일, 같은 ID 값 변경 409 BATCH_CONFLICT, 새 ID 같은 샘플 409 SAMPLE_CONFLICT | 200(응답 동일) / 409 BATCH_CONFLICT / 409 SAMPLE_CONFLICT | 성공 |
| 2026-11-06 00:46:16~00:56:18 | ② FastAPI/PostgreSQL 10분 | ①과 같음 | 599/599 수락(배치 200, 측정 8,985행), 중복 0·누락 0, 간격 984~1015ms(598/598 정시), 지원 센서 ok 100%, 0 대신 null 위반 0, 격리·잔여 0 | 성공 |
| 2026-11-08 23:58:10~11-09 00:17:55 | ② 실제 절전/재개(300초, `--expect-resume`). 수집 약 70초 후 시작 메뉴 절전(Modern Standby, S0 저전력 유휴) → 약 14.7분 후 재개 | `suspend_resume` ≥1, 절전 가로지르는 배치 0, `clock_jump` 0, 순번 연속, 단조 시간 절전 제외, 나머지 ①과 같음 | `suspend_resume` 1(절전 881,752ms, 직전 순번 68, 벽시계 경과 882,757ms·단조 경과 1,005ms), 가로지르는 배치 0, `clock_jump` 0, 298/298 수락(배치 100, 측정 4,470행), 순번 0~297 중복·누락 0, 간격 986~1015ms(297/297 정시), ok 100%, 업로드 재시도 1(PENDING), 격리·잔여 0 | 성공 |

증거: [tests/results/s1-02b/](../../tests/results/s1-02b/).

## 6. 미검증·차단

| 항목 | 원인 | 해결 담당 / 다음 확인 |
|---|---|---|
| 실제 PC 2대 | 테스트 PC 1대. PC-B 미확보 | A(팀원 PC 대여) / Sprint 2 첫 주 |
| 최대 절전·S3 절전 | PC-A는 Modern Standby만 실측(S3 미지원). 최대 절전(S4)은 미실행 | A / PC-B 검증 때 함께 |
| 실제 재부팅 중 서비스 연속 수집 | 에이전트 서비스 설치·자동 시작은 S2-02 범위. 재부팅 판정은 BootId 가상 검증만 | A / S2-02 |
| 원격 배포·TLS·등록 코드 경로 연동 | 에이전트는 로컬 compose(loopback HTTP)와 psql 관리자 등록만 검증. S2-03 등록 API·HTTPS 수명 주기는 B가 별도 검증([s2-03-validation](../s2-03-validation.md))했으나 이 에이전트와의 연결은 미검증 | A·B / S2-02 |
| 일반 사용자 권한 실측 | 관리자 셸에서만 실행. 가상 검증(T7)과 S1-02a 대조 실행으로 대체 | A / PC-B 검증 때 함께 |
| LocalSystem 서비스로 에이전트 실행 | 스파이크 도구로만 확인(S1-02a 5.5) | A / S2-02 |
| 장시간·메모리 | 10분 실측만. 스파이크는 4일 10시간 무오류 | A / S3 사전 운영 |

## 7. 계약·연동 합의 필요 (B·C)

1. **이벤트 전송 경로**: `collection_events`(README 5절)에 대응하는 API가 계약에 없다. 현재 로컬 `events.jsonl`만 남긴다.
   이벤트 종류: `agent_started/stopped`, `boot_session_started`(first_run/reboot), `suspend_resume`, `clock_jump`,
   `sensor_config_selected/changed`, `sensor_quality_changed`, `source_read_failed`, `collector_cycle_failed`,
   `collection_held`, `buffer_pruned`, `sensor_config_pending`, `batch_quarantined`, `upload_paused_auth`. — B
2. **센서 구성 등록**: 등록 API가 미합의라 `sensor-configs/<id>.json`(backend/README 설계안 형식)을 관리자가 등록해야 한다. — B
3. **reason 코드**: `driver_unavailable`, `sensor_not_exposed`, `component_absent`, `no_value`, `sensor_absent`, `read_failed`, `out_of_range`. — B·C(UI 표시)
4. **sensor_id·scope 명칭**(`cpu0/package/temp`, `gpu_board` 등)과 `source` 문자열 형식. — B
5. **센서 범위**: 15개(지원 13개)가 모델 입력에 충분한지, 코어별 값·GPU 메모리 온도 추가 여부. — C

## 8. 남은 제한

- 스풀은 파일 기반 최소 구현이다. SQLite 버퍼·한 트랜잭션 발급·장애 복구 검증은 S2-01.
- 장비 토큰은 평문 파일 경로(`--token-file`)로 받는다. S2-03 자격 증명 모듈(`agents/windows/Credentials.psm1`, DPAPI LocalMachine + ACL)이
  main에 있으나 C# 에이전트에서 읽는 연동은 설치 서비스(S2-02)와 함께 후속으로 한다.
- 시각 오프셋 측정 API(계약 4절) 클라이언트는 이 작업 범위 밖이다.
- LHM은 실행 중 GPU 핫플러그를 다시 열거하지 않는다. 장치 추가는 재시작 시 새 구성으로 반영된다.
