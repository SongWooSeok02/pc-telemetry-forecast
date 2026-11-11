# S1-01 검증

전체 실행: `python tests/run_contracts.py` (Python 3.12+, Node.js 22+, 추가 패키지 없음).
Node가 PATH에 없으면 NODE_BINARY 환경 변수에 실행 파일 경로를 지정한다.
결과: `tests/results/s1-01-contracts.json`. 사례별 기대값·실제 결과·실행 UTC·성공/실패,
빌드 기반 커밋·정확한 소스 해시·PC/OS/런타임을 보관한다.
기존 단일 모델 검사는 `python tests/verify_contract.py`와 `s1-01-local.json`으로 유지한다.
PC 센서 실측값과 인증 정보는 사용하지 않는다.

독립 JavaScript 생산자는 Decimal 토큰을 직접 정규화하며 Python 코드를 가져오지 않는다.
Python HTTP 참조 API에 실제 요청을 보내 SQLite 원장·측정·재집계 트랜잭션을 검사한다.
두 런타임 모두 고정 hash-vectors.json과 비교한다. 운영 인증 토큰은 사용하지 않으며
참조 API는 테스트 프로세스 안에서 127.0.0.1 임시 포트에만 열린다.
시각 동기화 상태·해상도는 테스트 주입 값이다. 실제 서버 동기화 증거가 아니다.
A의 .NET 생산자/B의 FastAPI·PostgreSQL 운영 스택으로도 아래 시나리오를 별도 재현한다.

| 사례 | 재현 순서 | 기대값 |
|---|---|---|
| 정상/미지원/결측 | fixture 전송 | 200, 품질·null 보존 |
| 응답 유실 | commit 후 응답 폐기, 동일 ID 재전송 | 같은 200, 추가 행 없음 |
| 충돌/동시성 | 같은 ID 동시 전송, 이후 값 변경 | 동일 내용 200, 변경 409 |
| 다른 ID 중복 | 기존 순번을 새 ID로 전송 | 409, 추가 행 없음 |
| 원자 커밋 | 마지막 샘플 오류 / commit 전 DB 중단 | 422/503, 원장·측정·재집계 모두 변경 없음 |
| 인증 | 만료·회수·다른 장비 토큰 | 401/403, 쓰기 없음 |
| 절전 | 시각 요청 도중 실제 절전·재개 | 구간 폐기, 기준 초기화, 새 수락 전 unknown |
| 시간 역행 | 벽시계 3초 역행 | 원본 보존, jump 이벤트, 재측정 |
| 큰 RTT | 망 지연 300ms 추가 | 판정 유보 |
| 타임아웃 | 시각 2초 / 수집 10초 응답 차단 | 시각 새 ID, 수집 고정 ID 재시도 |

통합 보고서에 빌드/버전·PC/OS·단조 소스·실측 해상도·재현 순서·기대/실제·실행 UTC·
성공/실패·담당자·로그를 기록한다. 위 표의 운영 스택·실제 절전 항목은 아직 미실행이다.

## 실제 Windows 단조 소스 확인

1. `python tests/probe_windows_clock.py --output tests/results/windows-before.json`
2. 같은 OS 부팅 세션에서 수동으로 절전 후 재개한다. 재부팅하지 않는다.
3. `python tests/probe_windows_clock.py --previous tests/results/windows-before.json --output tests/results/windows-after.json`
4. 같은 부팅인지 확인한 뒤 biased 경과와 unbiased 경과의 차이가 실제 절전 시간과
   맞는지 기록한다. actual sleep 이벤트 로그·에이전트 재개 기준 초기화 로그도 첨부한다.

스크립트는 PC를 절전시키거나 시각을 변경하지 않는다. 1단계 결과만으로 절전 검증을
통과 처리하지 않는다. 최소 호출 간격은 정확도/보장 해상도가 아니다. 시간 역행 실측은
별도 시험 PC에서 A가 수행하고 원본·이벤트·재측정 결과를 기록한다.
로컬 windows-probe-local.json은 Windows 10의 API 호출 확인 결과다. 목표 플랫폼
Windows 11의 절전 검증 또는 서버 동기화/정확도 검증으로 취급하지 않는다.
절전 포함 카운트 비교는 [QueryInterruptTime](https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-queryinterrupttime)을 사용한다.

CI: .github/workflows/contracts.yml에서 Windows/Ubuntu 각각 전체 검사를 실행하고
보고서를 artifact로 보관한다. workflow 파일이 존재하는 것만으로 CI 통과는 아니다.
최종 공동 검토 절차는 contracts/REVIEW.md를 따른다.

## S1-03 FastAPI / PostgreSQL

`pip install -r backend/requirements.txt` 후 전용 테스트 DB를 `TEST_DATABASE_URL`로
지정하고 `python tests/run_backend.py`를 실행한다. DB가 없으면 통합 검사는 미실행으로 기록한다.
[실행·설계·제한](../backend/README.md), [증거](results/s1-03-backend.json)를 참고한다.
S1-01 회귀 증거는 [s1-03-contract-regression.json](results/s1-03-contract-regression.json)이다.

## S2-01 SQLite 버퍼 장애 복구

실제 에이전트(`PcTelemetry.Agent.exe run`)를 참조 API `Store`(SQLite 원장)에 붙여 장애를 만든 뒤
서버 원장·샘플 행과 에이전트 `state/buffer.sqlite3`(전송 원장·누락 범위)·순번 범위를 대조한다.
토큰·machine_id는 매 실행 합성값이며 보고서에 쓰지 않는다. 결과: `tests/results/s2-01/`.

| 시나리오 | 재현 | 기대값 |
|---|---|---|
| `outage` | 서버 30분 중지(열린 keep-alive 연결도 끊음), 중지 중 에이전트 강제 종료 1회 | 장애 중 관측 샘플 전송, 중복 0, 원장 일치, 잔여 0 |
| `kill` | 서버 정상 중 `TerminateProcess` 3회 후 재시작 | 미동결 샘플 복원, 중복 0, 순번 구멍은 재시작 예약 건너뛰기만 |
| `response-loss` | 서버가 커밋 후 응답 없이 연결 종료(5번째 새 배치마다, 10회) | 같은 ID 재전송에 저장된 200, 서버 배치 = 고유 ID |
| `cap-age` / `cap-bytes` | 시험용 상한(120초 / 150,000바이트)보다 오래 서버 중지 | 오래된 미전송부터 제거, 누락 범위가 gaps·이벤트·상태 파일에 남고 서버 순번과 겹치지 않음 |

```powershell
dotnet build agents/windows/src/PcTelemetry.Agent -c Release
python tests/run_buffer_recovery.py --exe <PcTelemetry.Agent.exe> --scenario outage --pc-label pc-a
```

S1-03 FastAPI/PostgreSQL 경로의 같은 시나리오는 S2-04와 함께 검증한다(미실행).
