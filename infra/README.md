# S1-07 개발 실행·필수 검사

상태: 실행 구성 구현, 원격 CI·새 PC 실행·실제 센서 2대·팀원 리뷰 대기.
기준 계약은 [S1-01](../contracts/README.md)이며 변경하지 않는다.
S1-02a의 버전·드라이버 관측 자료는 아직 저장소에 없다.

## 구성과 전제

| 구성 | 개발 기준 | 현재 실행/검사 |
|---|---|---|
| API | Python 3.12, backend/requirements.txt 고정 직접 의존성 | FastAPI 이미지 빌드, Ruff 치명 오류 검사, PostgreSQL 통합 검사 |
| DB | PostgreSQL 16, 빈 전용 DB | 001_ingestion.sql 1회 적용, 파티션 준비/중복/원자성 검사 |
| 작업자 | API와 같은 Python 이미지 | 구현된 파티션 관리 명령만 제공. 예측·집계 작업자는 미구현 |
| Windows 에이전트 | Windows 11 x64 목표, .NET/LHM 버전은 A의 S1-02a 결과로 확정 | 소스·프로젝트 없음. .NET 빌드/센서 검증 미연결 |
| 대시보드 | React/TypeScript 계획, Node.js 22는 현재 계약 생산자 검사에 사용 | package.json·UI 소스 없음. UI 빌드/정적 검사 미연결 |

Docker Engine와 Compose v2를 사용한다. Windows는 Linux 컨테이너를 사용할 수 있는
Docker Desktop/WSL2 환경이 필요하다. 현재 로컬에는 Docker가 없어 Compose 실행을
검증하지 못했다. Python 호스트 실행은 [API 안내](../backend/README.md)를 따른다.
컨테이너 Python 패키지는 직접 의존성만 고정되어 전이 의존성·이미지 digest까지 완전히
고정한 재현 빌드는 아니다. 팀 환경을 비교할 때 실제 이미지 ID와 패키지 목록도 기록한다.

## 처음 실행 (저장소 루트, PowerShell)

```powershell
Copy-Item infra/.env.example infra/.env
# 개인 개발용 비밀번호로 로컬 .env를 편집한다. Compose .env 형식에 맞게 인용한다.
docker compose --env-file infra/.env -f infra/compose.yaml config --quiet
docker compose --env-file infra/.env -f infra/compose.yaml up --build --wait api
Invoke-RestMethod http://127.0.0.1:8000/health/partitions
docker compose --env-file infra/.env -f infra/compose.yaml run --rm partitions
```

기대값: migrate 종료 코드 0, API healthy, health HTTP 200과 누락 없는 파티션,
작업자 종료 코드 0 및 `partition_maintenance_success` 로그.
마이그레이션 실패는 API 시작을 차단한다. 초기 토큰/센서 구성/실제 PC 데이터는 넣지 않는다.
API의 수집 요청은 관리자 등록 전 401이 정상이다. 가상 데이터 검증은 아래 테스트를 쓴다.

DB는 외부 포트를 열지 않고 API는 loopback 8000만 연다. `dev-db` 볼륨은 정지 후에도
남는다. 최초 마이그레이션은 **한 번만** 적용하므로 기존 볼륨에서 `up`으로 migrate를
다시 실행하지 않는다. 이후 재시작은 다음 명령을 사용한다.

```powershell
docker compose --env-file infra/.env -f infra/compose.yaml stop
docker compose --env-file infra/.env -f infra/compose.yaml start db api
```

`down` 이후에도 DB 볼륨은 남으므로 기존 DB 재사용 시 `up -d db` 후
`up -d --no-deps api`로 migrate를 건너뛴다. 스키마 변경은 별도 마이그레이션이 필요하다.
`down --volumes`는 이 개발 DB를 삭제하므로 폐기 가능한 데이터에만 사용한다.
파티션 작업자는 일회성 명령이다. 개발 중 장기간 실행하면 매시간 외부 스케줄러에서
호출해야 한다. Compose가 운영 스케줄러/알림을 제공하지 않는다.
개발 구성은 DB owner를 공유한다. 운영에서는 API 최소 권한·migration-owner 분리,
TLS·비밀 주입·백업·알림을 별도로 적용한다.

## 환경변수와 로컬 파일

| 변수 | 주입 위치 | 의미 |
|---|---|---|
| POSTGRES_USER/PASSWORD/DB | infra/.env, Compose | 폐기 가능한 개발 DB 계정. 예시는 합성 값 |
| DATABASE_URL | 호스트 비밀 환경 / Compose에서는 빈 문자열 | 호스트 실행용 연결 문자열. Compose는 PGHOST/PGUSER/PGPASSWORD/PGDATABASE로 연결 |
| TEST_DATABASE_URL | 호스트 테스트 프로세스 | CREATE SCHEMA 가능한 전용 테스트 DB, 운영 DB 사용 금지 |
| BACKEND_REPORT | 테스트 프로세스 | 기본 tests/results/s1-03-backend.json, 개인 실행 시 local-evidence/ 아래 지정 |
| CONTRACT_REPORT | 테스트 프로세스 | 기본 tests/results/s1-01-contracts.json, 개인 실행 시 local-evidence/ 아래 지정 |
| NODE_BINARY | 필요 시 테스트 프로세스 | Node 실행 파일 경로 |

에이전트/대시보드의 미확정 환경변수나 예측 작업자의 가짜 실행 명령은 만들지 않는다.
해당 구현 PR에서 버전·환경 예제·빌드·정적 검사·핵심 테스트를 함께 추가한다.
`.env`, 로컬 DB, 모델, `local-evidence/`는 무시한다. 추적 중인 파일에는 ignore가 소급되지
않으므로 제출 전 `git diff --cached`를 확인한다. 토큰·연결 문자열·PC 원본 자료는 붙이지 않는다.
`docker compose config` 전체 출력은 비밀번호를 포함하므로 공유하지 않고 `--quiet`을 쓴다.

## PR 검사와 로그

| Workflow / job | 필수 확인 내용 | 실패 증거 |
|---|---|---|
| environment.yml / static | Python 컴파일, Ruff E9/F63/F7/F82, Node 구문, 공백 | Actions 단계 로그와 파일/행 |
| environment.yml / compose | 이미지 빌드, 빈 DB 마이그레이션, API DB health, 파티션 작업자 | compose 상태·합성 전용 로그 artifact (7일) |
| backend.yml / ingestion | 실행기 회귀, 실제 PostgreSQL 수집·충돌·원자성 | s1-03-postgres-evidence JSON |
| contracts.yml / contract (Linux/Windows) | 독립 JS 생산자·계약·시각 상태 | OS별 s1-01-evidence JSON |

실패 단계 뒤의 일반 검사는 중단되며 `always()` 증거 업로드는 시도한다. 의존성 설치나
runner 취소로 보고서가 없으면 Actions 단계 로그를 사용한다. CI에는 운영 secrets나
실제 PC 자료를 주입하지 않는다. Compose 로그 자동 업로드는 합성 DB CI에서만 한다.
PR 필수 검사 지정은 저장소 branch protection/ruleset에서 위 job을 선택해야 하며,
이 파일 추가만으로 병합 차단 설정이 활성화되지는 않는다. 다른 팀원 1명 승인도 필요하다.

로컬 검사 (가상환경 활성화, Python 3.12 / Node 22):

```powershell
python -m pip install -r backend/requirements.txt -r infra/requirements-ci.txt
python -m compileall -q backend tests
python -m ruff check --select E9,F63,F7,F82 backend tests
node --check tests/agent_producer.mjs
New-Item -ItemType Directory -Force local-evidence
$env:CONTRACT_REPORT = 'local-evidence/contracts.json'
python tests/run_contracts.py
python -m unittest discover -s tests -p test_backend_runner.py
python -m unittest discover -s tests -p test_partitions.py
# 전용 테스트 DB 연결을 비밀 환경으로 TEST_DATABASE_URL에 주입한 뒤 실행
$env:BACKEND_REPORT = 'local-evidence/backend.json'
python tests/run_backend.py
```

TEST_DATABASE_URL 미설정은 통합 검증 통과가 아니며 종료 코드 1이다.
Compose DB는 호스트 포트를 공개하지 않는다. 호스트 통합 테스트에는 별도의 전용
PostgreSQL을 사용한다. CI backend job은 PostgreSQL 16 서비스로 자동 수행한다.

## 의도적 실패와 완료 판정

임시 검증 브랜치에서 `backend/ci_failure_probe.py`에 `def broken(:`를 넣어 PR을 열면
static의 compileall이 실패해야 한다. 파일을 제거한 커밋에서 다시 통과하는지 확인한다.
운영 코드를 망가뜨린 채 병합하지 않는다. 실패·복구 Actions run 링크, 커밋, 실행 시각,
기대/실제 종료 코드, 로그를 기록한다. 로컬 문법 오류 검출과 실제 원격 CI 실패는 구분한다.

검증 기록은 [실행 결과](../docs/s1-07-validation.md)에 남긴다. 새 환경 재현, 원격 CI의
실패/복구, [PC 2대 관측](../docs/sensor-support.md), 다른 팀원 리뷰가 남아 있어
S1-07 전체 완료로 표시하지 않는다. B는 2026-11-03 KST 마감 전에 CI/개발 환경 결과를,
A는 S1-02a 관측을, C는 대시보드 및 예측 작업자 코드 제공 시 CI 연결을 확인한다.
