# S1-03 수집 API / PostgreSQL

S2-03 등록·회수 API와 역할 권한 변경은 [구현/검증 기록](../docs/s2-03-validation.md),
Windows DPAPI/IPC는 [서비스 연동 안내](../agents/windows/README.md)를 따른다.
신규 DB는 마이그레이션을 번호순으로 적용한다. 아래 S1-03의 수동 토큰 발급/SELECT 전용
설명은 기존 수집 전용 역할에 대한 기록이며 새 관리 API 역할의 권한과 구분한다.

상태: 구현 및 로컬 PostgreSQL 통합 검증 완료, **S1-01 계약 승인·다른 팀원 리뷰 전**.
원격 자동 검증 결과는 PR checks에서 확인한다.
현재 S1-01 `0.1.0-draft.2`를 변경 없이 적용한다. 작업을 Done으로 변경하지 않았다.

## 실행

Python 3.12+, PostgreSQL 16을 사용한다. 저장소 루트에서:

```sh
python -m venv .venv
# 환경에 맞게 가상환경 활성화
python -m pip install -r backend/requirements.txt
# DATABASE_URL은 배포 환경의 비밀 설정으로 주입한다.
for migration in database/migrations/*.sql; do
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$migration" || exit 1
done
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

PowerShell에서는 `$env:DATABASE_URL`을 사용한다. 마이그레이션은 빈 전용 DB/스키마에
한 번 적용한다. 서버는 fixture·기본 토큰을 자동 등록하지 않는다. HTTPS 종료·요청 시간 제한·
요청 빈도 제한은 배포 프록시에서 설정한다. DB 연결/잠금/SQL 제한은 각각 5/5/10초다.
`POST /v1/telemetry/batches`의 요청·성공·오류 형식과 해시는 [계약](../contracts/README.md)을 따른다.
동기 DB 처리는 스레드 풀에서 실행해 비동기 요청 루프를 막지 않는다.

## 장비와 불변 센서 구성

S2-03 장비 등록 API 이전에는 관리자가 `device_credentials`에 토큰 SHA-256, machine_id,
만료 시각을 넣고 `sensor_configs`에 구성 ID, 소유 장비, 계약 fixtures와 같은 config JSON을
등록한다. 원문 토큰을 DB·로그에 남기지 않는다. 만료·회수는 매 요청 확인하며 본문 장비와 대조한다.
등록된 구성은 수정/삭제하지 않는다. API DB 역할에는 이 두 테이블의 SELECT만 부여한다.

구성 등록 API 설계안(아직 구현/합의 아님): `POST /v1/device/sensor-configs`에 schema_version과
sensors를 보내면 인증 장비의 새 UUID 구성을 201로 발급한다. sensors는 sensor_id 키와
component_id/kind/unit/source/scope/support를 포함한다. 계약의 ID·kind/unit 대응·지원 상태를
검사하고, 기존 ID의 부품·단위·측정 범위 재사용은 거부한다. 구성 추가/제거에는 새 구성 ID를
발급한다. 전송 응답 유실 시 구성 중복 생성을 막을 등록 요청 ID 계약은 A/B 합의 후 확정한다.
공개 등록 엔드포인트를 임의로 계약에 추가하지 않았다. 구성은 장비 수명 동안 보존한다.

## 원자성과 중복 방지

- `ingestion_batches(machine_id,batch_id)` 기본 키가 전체 배치 유일성을 보장한다.
  서버가 계산한 내용 해시와 정규 본문·최초 성공 응답을 저장한다. 재전송은 보존 중인 정규 본문도 비교한다.
- `ingestion_samples(machine_id,boot_session_id,sample_seq)` 기본 키는 다른 배치 ID의 중복도 막는다.
  측정 파티션의 UNIQUE에 전체 샘플 유일성을 의존하지 않는다.
- 장비별 transaction advisory lock으로 동시 요청을 정렬한다. 원장·샘플 키·측정·10초/60초
  재집계 큐를 하나의 트랜잭션에서 커밋한다. 실패는 전부 롤백한다. 커밋 이후에만 200을 반환한다.
- 응답 유실은 저장된 응답으로 복구한다. 동일 ID 변경은 BATCH_CONFLICT,
  다른 ID의 샘플 중복은 SAMPLE_CONFLICT이며 전체 409다.
- 배치 해시·최초 응답과 샘플 중복 방지 키는 **장비 등록 수명 전체** 보존한다.
  유지보수 작업은 7일이 지난 배치의 정규 본문만 비우며 이후에는 수신 시 서버가 계산한 SHA-256으로 재전송을 비교한다.
- 재집계 작업자는 현재 generation의 원시값 전체로 upsert한다. 작업 확인과 집계 게시를 한 트랜잭션으로
  처리하고, 처리 중 변경된 generation은 다음 작업에 남긴다. 자세한 동작은 [S3-03 기록](../docs/s3-03-validation.md)을 참고한다.

## UTC 일별 파티션과 감시

`measurements`는 **received_at RANGE**, UTC `[00:00, 다음 날 00:00)` 파티션이다.
received_at은 요청 진입 시 서버가 기록하며 배치 내 동일하다. observed_at은 역행/과거/미래여도
원본 보존한다. `(machine_id,sensor_id,observed_at)` 인덱스가 자식 파티션에도 생성된다.
관측 시각만 검색하면 수신 파티션 pruning은 제한되므로 보존 기간/수신 범위도 고려해야 한다.

마이그레이션과 `python -m backend.partitions`가 UTC 오늘부터 **3일 뒤까지 총 4개**를
생성한다. 별도 migration-owner 자격으로 매시간 실행하고 프로세스 종료 코드 !=0,
`partition_maintenance_failed` 로그 또는 마지막 성공 이후 2시간 경과에 운영 알림을 연결한다.
운영 스케줄러/알림 대상 연결은 배포 담당이 설정해야 한다. API 자격에 DDL 권한을 부여하지 않는다.

`GET /health/partitions`는 실제 연결된 파티션 경계를 검사하며 누락 시 503과 UTC 날짜 목록을
반환한다. 운영 모니터는 1분마다 조회하고 비정상 시 알림을 보낸다. DB 오류도 503이다.
파티션 부재 시 DEFAULT 파티션으로 우회하지 않고 수집 트랜잭션 전체를 503으로 롤백한다.
복구 후 클라이언트가 동일 배치를 재시도한다. 미래 파티션까지 없으면 미리 장애를 감지한다.
실제 운영 알림 전달 시험과 서버 시계 동기화 감시는 아직 별도 배포 검증이 필요하다.

## 검증과 증거

```sh
# 실제 테스트 DB가 필요하며 CREATE SCHEMA 권한이 있어야 한다.
# TEST_DATABASE_URL을 전용 테스트 DB로 지정한다. 임의 고유 스키마만 생성/삭제한다.
python tests/run_backend.py
python tests/run_contracts.py
```

[사례 코드](../tests/test_backend.py)와 [실행 보고서](../tests/results/s1-03-backend.json)에
환경·버전·기반 커밋·소스 SHA-256·UTC 실행 시각·재현 명령·기대값/실제 assertion·성공/실패를 기록한다.
DB 환경 변수가 없으면 PostgreSQL 검사는 `not_executed`, 보고서 success=false와 종료 코드 1이다.
DB 초기 연결/마이그레이션 실패와 실패한 subtest도 결과 JSON에 남긴다. 실행기 회귀 검사는
`python -m unittest discover -s tests -p test_backend_runner.py`로 실행한다.
CI는 PostgreSQL 16 서비스로 실행하며 보고서를 업로드한다. CI 구성 파일 존재는 통과 증거가 아니다.
가상 데이터는 [fixture](../tests/fixtures/s1-03-batch.json)에 정상/미지원/결측을 포함하며
검사가 관측 시각 이상·날짜 경계·중복·불량 요청을 파생한다. 실제 PC 센서값은 사용하지 않았다.

로컬 검증은 [EDB PostgreSQL 배포 ZIP](https://www.enterprisedb.com/download-postgresql-binaries)을
프로젝트 내부 `.tools/`에 풀어 loopback 임시 DB로 수행한다. 시스템 서비스 등록은 하지 않는다.
로컬 실행 자료는 Windows 환경 검증이며 Linux CI·실제 PC 3대·10대 규모 부하 검증을 대신하지 않는다.

남은 작업: S1-01 공동 승인(A/B/C, 10-30 KST 확인), 다른 팀원 1명 코드 리뷰,
원격 CI 실행 확인(B, PR 시), S2-03 등록·인증 발급 API, 운영 시계 신뢰도 기반 시각 API,
S2-04/S3-03/S4-03의 중복·집계·규모 확장 검증. 현재 요청 범위에 대한 자동 통합 검사는
로컬에서 실행했으며 [PR #2](https://github.com/SongWooSeok02/pc-telemetry-forecast/pull/2)에서
원격 CI와 PR 기준 재리뷰를 진행한다. 외부 작업 상태는 변경하지 않았다.

## S2-04 PC 조회

사용자 세션 Bearer 토큰으로 `GET /v1/machines`와
`GET /v1/machines/{machine_id}`를 호출한다. 목록 응답은 `{"machines": [...]}`이며
상세 응답은 같은 요약 객체 한 건이다. 다른 사용자 소유 PC, 삭제된 PC, 없는 PC의 상세 요청은
모두 `404 MACHINE_NOT_FOUND`로 응답한다.

요약은 `machine_id`, `name`, `online`, `last_received_at`,
`cpu_temperature_c`, `gpu_temperature_c`를 반환한다. 현재 등록 스키마에서는
UUID를 표시 이름으로 사용한다. 온도는 가장 최근 수락 배치의 CPU 패키지 및 GPU 코어
센서별 최대 섭씨값이며 유효 측정이 없으면 `null`이다.

온라인은 최초로 커밋된 배치의 서버 수신 시각 중 최댓값이 조회 시점 기준 최근 120초
이내일 때 참이다. 수신 이력이 없는 PC는 오프라인이며 마지막 수신 시각은 `null`이다.
정확한 재전송은 최초 응답을 재사용하므로 마지막 수신 시각을 갱신하지 않는다.

## 웹·Windows 앱 사용자 세션 (S3-07)

[사용자 세션 API, 로그인 어댑터 배치 및 DB 권한](../docs/s3-07-session.md)을 따른다.
