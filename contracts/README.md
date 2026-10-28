# S1-01 수집·시각 계약 v0.1.0-draft.2

상태: **공동 검토 대기 — 확정/완료 아님**. B: SongWooSeok02, 검토: A·C.
작업 기간: 2026-10-29~11-03 KST. 첫 이틀 검토 목표: **10-30 종료 전**.
사용 작업: S1-03, S1-02b, S1-04, S2-01/03/07a, S2-04.
현재 저장소에는 운영 API/에이전트가 없다. tests/의 독립 생산자·HTTP 참조 API로 계약을
검증하며, 운영 .NET/FastAPI/PostgreSQL 스택과 실제 절전 검증은 별도 증거가 필요하다.

## 1. 공통 형식과 데이터

HTTPS, UTF-8 JSON, `Content-Type: application/json`. 장비 토큰은
`Authorization: Bearer <device_token>`만 사용한다. URL·본문·로그에 기록하지 않는다.
서버는 토큰의 machine_id와 본문을 대조한다. 사용자 세션으로 수집할 수 없다.
알 수 없는 필드, JSON 중복 키, NaN/Infinity, 잘못된 타입은 거부한다.
선택 필드는 생략하며 명시적 null은 value 외에는 허용하지 않는다.
UTC는 `YYYY-MM-DDTHH:mm:ss.ffffffZ`(소수점 6자리)로 고정한다. 표현 정밀도는
1µs이며 실제 시계 정확도를 뜻하지 않는다. 원본 observed_at을 덮어쓰지 않는다.

| 필드 | 필수/선택 | 규칙 |
|---|---|---|
| schema_version | 필수 | `0.1.0` |
| machine_id | 필수 | 등록 서버 발급 소문자 UUID |
| boot_session_id | 필수 | OS 부팅마다 UUID, 서비스 재시작에서는 유지; OS 부팅 식별을 확인하지 못하면 수집 유보 |
| batch_id | 필수 | UUID v4, 발급과 내용 고정을 SQLite 한 트랜잭션으로 영속화 |
| sensor_config_id | 필수 | 장비별 불변 센서 구성 UUID; 배치 전체가 같은 구성 사용 |
| samples | 필수 | 1~3600개 샘플, 같은 부팅 세션; sample_seq 오름차순 |
| sample_seq | 필수 | 부팅 내 0부터 증가, JSON 안전 정수 0~9007199254740991, 영속화·재사용 금지; 누락 순번 허용 |
| observed_at | 필수 | 실제 관측 벽시계 UTC; 역행해도 원본 보존 |
| monotonic_ms | 필수 | QueryUnbiasedInterruptTime / 10000의 내림 정수, 절전/최대절전 제외 |
| readings | 필수 | sensor_config_id가 가리키는 구성의 센서 전체를 한 번씩, 최소 1개 |
| sensor_id | 필수 | 장비 내 영속 ASCII ID, `[a-z0-9._/-]{1,128}`; 하드웨어 경로→ID 매핑 저장 |
| unit | 필수 | `Cel`, `W`, `%`, `MHz`, `rpm` 중 등록 메타데이터와 일치 |
| quality | 필수 | `ok`, `unsupported`, `missing`, `error` |
| value | 필수 | ok: 유한 JSON 수; 그 외: null. 미지원·결측을 0으로 보내지 않는다 |
| reason | 선택 | 비정상 품질 코드 `[a-z0-9_]{1,64}`; ok에는 금지 |
| received_at | 서버 필수 | 서버가 최초 수신한 UTC, 클라이언트 전송 금지; 중복 응답은 최초 커밋 결과 유지 |

센서 등록 메타데이터는 component_id, kind, unit, source, scope, support를 필수로 갖는다.
kind는 temperature/power/utilization/clock/fan, unit은 위 표 순서로 대응한다.
support는 supported/unsupported. CPU 패키지·GPU 보드 전력을 scope로 구분한다.
등록 API 자체는 S1-03에서 설계하되 미등록 ID/단위 불일치는 422이다.
구성은 발급 후 수정/삭제하지 않고 장비 수명 동안 보관한다. 센서 추가·제거·지원 상태 변경에는
새 sensor_config_id를 발급한다. 에이전트는 구성과 해당 구성을 쓰는 미전송 배치를 함께 보관한다.
서버는 현재 활성 구성이 아닌 배치가 참조한 과거 구성으로 검증한다. 같은 배치 안에서 구성 변경은
금지한다. 구성 등록이 아직 서버에 도착하지 않았으면 409 SENSOR_CONFIG_PENDING(retryable=true),
동일 장비의 구성 등록을 먼저 완료하고 원래 배치를 그대로 재전송한다. 타 장비 구성은 403이다.
센서 ID 재활용 금지, 구성의 support=unsupported이면 quality=unsupported만 허용하며
supported이면 ok/missing/error를 허용한다. 참조 예시는 fixtures/sensor-configs.json이다.
미지원 논리 센서도 등록하고 null+unsupported로 보고한다. 지원 센서의 일시 결측은
null+missing, 읽기 실패는 null+error. 센서 교체·단위/측정 범위 변경에는 새 ID를 발급한다.
물리값 0은 ok일 때 허용한다. 물리 범위 필터는 S2-07a 검토 전 임의 적용하지 않는다.

샘플 식별은 `(machine_id, boot_session_id, sample_seq)`, 관측 행 식별은 여기에
sensor_id를 붙인다. 다른 batch_id에서 같은 샘플을 재사용하면 전체 409이다.
서버는 관측 역행/오래된 관측을 이유로 원본을 보정하거나 자동 폐기하지 않는다.
UI·분석은 품질을 보존하고 unsupported와 missing을 구별한다.

## 2. 불변 배치와 해시

`POST /v1/telemetry/batches`. 최대 본문 1MiB(해제 후 UTF-8 바이트), 압축은 v1 미지원.
3600개는 1시간치 1초 샘플의 상한 제안이며 바이트 상한이 우선한다.
1초 수집·3초 전송은 README 기준; 대기 데이터는 ID 발급 **전** 크기에 맞춰 분할한다.
ID 발급 후 부팅·순번·시각·센서·값·품질·배열 순서를 포함한 본문 전체를 동결한다.
재시도 때 새 관측·버퍼 체류 시간·토큰을 본문에 추가하지 않는다.

`X-Content-SHA256`은 아래 프로젝트 전용 `ascii-json-v1` 정규화의 SHA-256 소문자 hex.
RFC 8785/JCS와 동일하지 않다. .NET/Python의 숫자 직렬화 차이를 피하려고
서버는 JSON 숫자를 십진수로 읽고 고정 소수점 최소 표현으로 출력한다
(1e1→10, 1.00→1, -0→0, 후행 소수 0 제거). 숫자는 최대 32자 입력,
절댓값 9007199254740991 이하, 소수점 이하 유효 위치 최대 6자리.
센서 value는 추가로 절댓값 1e12 이하이다. 이 상한은 직렬화 안전 한계이며 물리 품질 판정은 아니다.
모든 키·문자열은 출력 가능한 ASCII(U+0020~007E). 출력 문자열은 큰따옴표로 감싸고,
U+0022는 역슬래시+큰따옴표, U+005C는 역슬래시 두 개로만 출력한다.
나머지 문자는 /, <, >, &, 작은따옴표를 포함해 문자 그대로 출력하며 선택적 이스케이프나
유니코드 이스케이프를 출력하지 않는다. 입력 이스케이프는 먼저 해석한 뒤 이 규칙을 적용한다.
객체 키는 ASCII 오름차순, 공백/개행 없음, 배열 순서 유지, UTF-8 BOM 없음.
배치 전체가 해시 대상이다. 헤더·인증·서버 필드는 제외한다.
입력에서 동등한 숫자 표현/객체 키 순서/공백은 같은 내용이며 배열 재정렬은 다른 내용이다.
해시 오류는 422 HASH_MISMATCH; 누락은 422 VALIDATION_ERROR.
서버는 직접 해시를 계산하며 클라이언트 해시만 믿지 않는다.

1. 인증·크기·형식·해시를 검증한다.
2. 원장 `(machine_id,batch_id)`가 있으면 정규 본문까지 비교한다. 같으면 저장된
   성공 응답을 그대로 200 반환, 다르면 409 BATCH_CONFLICT. 실제 SHA 충돌도 본문 비교로 거부한다.
3. 새 배치는 전체 센서/샘플을 검증하고 샘플 유일성 제약을 확인한다.
4. 원장·샘플 식별 원장·측정·재집계 작업을 한 DB 트랜잭션에서 커밋한다.
   동시 요청의 unique 경합도 같은 규칙으로 처리한다. 오류는 전부 rollback.
5. 커밋 후 200. 207/부분 성공 금지. 수락 샘플 수는 전체와 같아야 한다.

재시도는 동결된 ID·본문·해시 그대로 한다. 응답 유실/클라이언트 타임아웃은 커밋 여부 불명이다.
서버 타임아웃 504도 재시도한다. 401/403은 인증 복구 전 중지, SENSOR_CONFIG_PENDING은
구성 등록 후 원본 재시도한다. 다른 409/422는 격리·진단하며
새 ID로 우회하지 않는다. 413은 발급 후 재분할 불가이므로 격리하고 B와 해결한다.
수집 HTTP 타임아웃 10초, 재시도 3/6/12/24/48/60초(+0~20% jitter)는 초기 제안.
로컬 24시간/500MB 삭제 상한에서 손실 범위를 기록한다.
원장·샘플 중복 키는 장비 등록 수명 동안 보관(측정 7일 삭제와 분리)하는 제안이다.
이를 변경하려면 최대 재전송 수명·만료 거부 규칙부터 공동 합의한다.
buffer residence는 서버 `received_at-observed_at`으로 계산하지 않는다. 에이전트 진단에
enqueue/전송 시각·절전 이벤트를 별도 기록하고 동결 본문을 수정하지 않는다.

## 3. 수집 응답·오류 예시

요청 본문/정규화 해시 입력 예시는 [fixtures](fixtures/batch.json), 실행 가능한
검사는 [전체 검증 실행기](../tests/run_contracts.py)를 사용한다.
고정 정규 바이트·기대 SHA-256은 [공유 벡터](fixtures/hash-vectors.json)에 있다.

성공(최초·재전송 동일):
```json
{"schema_version":"0.1.0","batch_id":"33333333-3333-4333-8333-333333333333","accepted_samples":1,"received_at":"2026-10-29T00:00:03.000000Z"}
```

오류 공통 본문(각 HTTP 상태에서 동일 구조):
```json
{"error":{"code":"VALIDATION_ERROR","message":"Invalid batch","request_id":"44444444-4444-4444-8444-444444444444","retryable":false,"details":[{"path":"/samples/0/readings/0/value","rule":"quality_value"}]}}
```

| HTTP | code | 예시/처리 | retryable |
|---|---|---|---|
| 400 | INVALID_JSON | JSON 문법·중복 키 오류 | false |
| 401 | DEVICE_UNAUTHENTICATED | 누락·만료·회수 토큰, WWW-Authenticate: Bearer | false |
| 403 | DEVICE_FORBIDDEN | 토큰과 machine_id 불일치 | false |
| 409 | BATCH_CONFLICT | 동일 ID에서 52.5→53으로 변경 | false |
| 409 | SAMPLE_CONFLICT | 다른 ID에 기존 순번 포함 | false |
| 409 | SENSOR_CONFIG_PENDING | 참조 구성 등록 후 원본 배치 재시도 | true |
| 413 | PAYLOAD_TOO_LARGE | 1MiB 초과 | false |
| 422 | VALIDATION_ERROR | unsupported인데 value=0 / 단위 오류 | false |
| 422 | HASH_MISMATCH | 본문과 헤더 해시 불일치 | false |
| 429 | RATE_LIMITED | 요청 제한, Retry-After 초 제공 | true |
| 503 | SERVICE_UNAVAILABLE | DB 불가, Retry-After 초 제공 | true |
| 504 | REQUEST_TIMEOUT | 프록시/서버 시간 초과, 커밋 여부 불명 | true |

각 행의 code/retryable을 공통 본문에 대입하며 details는 해당 필드가 없는 오류에서 []이다.
오류 message는 표시용, 분기는 code 기준. 원본 값/토큰을 오류에 되돌리지 않는다.
연결 종료·클라이언트 타임아웃에는 HTTP 본문 자체가 없을 수 있다.

## 4. 장비 인증 시각 API

`POST /v1/device/time`, 같은 Bearer 인증. 요청:
```json
{"schema_version":"0.1.0","request_id":"44444444-4444-4444-8444-444444444444"}
```
200 응답:
```json
{"schema_version":"0.1.0","request_id":"44444444-4444-4444-8444-444444444444","server_received_at":"2026-10-29T00:00:00.050000Z","server_sent_at":"2026-10-29T00:00:00.060000Z","precision_us":1000,"clock_status":"synchronized"}
```
인증 middleware 진입 시 t2, 직렬화 직전 t3을 기록한다. precision_us=1000은 초기
시계 읽기 해상도 상한 제안이며 6자리 UTC 표현/정확도와 별개다. 배포 실측 전 확정 금지.
서버 시계 동기화 상태가 불명이거나 역행하면 503 CLOCK_UNTRUSTED(retryable=true),
200에는 synchronized만 허용한다. 상태·해상도 측정/감시는 S1-03·운영 구현 사항이다.
400/401/422/429/503/504는 위 오류 형식, request_id가 유효하면 오류에도 echo한다.
불량 ID·인증 실패는 서버 생성 request_id. 시각 API는 machine_id를 본문에 받지 않는다.

에이전트는 고유 UUID request_id별 t1(송신 벽시계), m1(송신 단조), t4(수신 벽시계),
m4(수신 단조), t2/t3, precision, 서버 상태를 별도 진단 기록에 보관한다.
아래 값은 ms 단위로 계산한다.

* D=m4-m1, P=t3-t2, network_rtt=D-P.
* offset=((t2-t1)+(t3-t4))/2 (양수: PC가 서버보다 늦음).
* uncertainty=network_rtt/2 + precision_us/1000 + 16ms(클라이언트 읽기 해상도 초기 예산).
* request_id 일치, 같은 부팅/재개 구간, D>=0, P>=0, 0<=network_rtt<=250ms,
  abs((t4-t1)-D)<2000ms, 동기화 서버일 때만 수락한다.

처음 두 번의 5초 초과 수락 측정은 횟수만 누적하고 판정은 unknown으로 둔다.
타임아웃은 해당 request_id를 폐기하고 연속 횟수=0/unknown으로 전환한다.
폐기·불일치 ID의 늦은 응답은 새 pending 요청이나 현재 판정에 영향을 주지 않는다.
상태 전이 참조 코드는 tests/clock_state.py이며 합성 입력 검사와 실제 OS 이벤트 검증을 구분한다.

RTT 250ms는 1초 샘플 간격의 1/4, 네트워크 비대칭 불확실성을 최대 125ms로
제한하려는 초기 예산이다. 5초 이상 오차 판정에 여유를 두며 실제 LAN/원격망 분포로 재검토한다.
시각 요청 타임아웃 2초(수락 RTT의 8배), 최초 연결/재연결/재개 즉시 측정,
정상 60초마다(3연속 판정 약 2분), 실패 시 5/10/20/40/60초 후 새 ID로 재측정한다.
수집 API 타임아웃과 혼동하지 않는다. 늦게 도착한 폐기 ID 응답은 무시한다.
이 수치는 성능 보장이 아니라 A/B/C 승인 대상이다.

QueryUnbiasedInterruptTime은 절전 시간을 제외한다. 재개 이벤트를 가로지른 측정·
벽시계 비교를 폐기하고 비교 기준·연속 초과 횟수를 초기화한다. 새 수락 측정 전에는
clock_status=unknown으로 유보한다. 서비스 재시작도 기준을 초기화한다.
평상시 abs(벽시계 경과-단조 경과)>=2초면 clock_jump 이벤트와 재측정, 연속 횟수 초기화.
수락 측정에서 abs(offset)>5000ms가 3회 연속이면 clock_skew_suspected.
거부/실패 측정은 연속 횟수를 끊고 판단을 unknown으로 유보한다. 수락 정상 측정은
횟수=0, 상태 정상. 오프셋은 추정치이며 관측 시각을 보정하지 않는다.
수신 지연은 버퍼·망·시계가 섞인 값이므로 시계 오차로 단정하지 않는다.

예: D=110ms, P=10ms→RTT=100ms; t1=0,t2=50,t3=60,t4=110→offset=0.
D=310,P=10→RTT=300으로 유보. t4=-3000,D=100→시간 역행 이벤트/유보.
절전으로 벽시계가 1시간 경과하고 단조는 100ms만 증가하면 재개 구간 폐기;
급변 경고로 분류하지 않고 기준 초기화 후 새 측정한다.

## 5. 검토·미완료·변경 관리

| 항목 | 담당 / 다음 확인 | 상태 |
|---|---|---|
| 필드·센서·UTC·오류 공동 검토 | A/B/C, 10-30 KST | 미완료 |
| 배치 해시·정수/소수 표현·수명·상한 승인 | A/B/C, 10-30 KST | 미완료 |
| RTT·주기·해상도·절전 소스 승인 및 Windows 측정 | A/B, 10-30 KST | 미완료 |
| Project 확정 일정·연동 카드 대조 | B, 10-30 KST | Project 웹 조회 실패; 사용자 제공 일정만 적용 |
| 독립 생산자·HTTP 참조 API 계약 통합 | B | JavaScript/Python/SQLite 로컬 검증; tests/results/s1-01-contracts.json |
| .NET 생산자와 실제 FastAPI/PostgreSQL 통합 | A/B, S1-03·S2-01 | 후속 운영 스택 검증 미완료 |
| 다른 팀원 1명 리뷰·CI 통과 | A 또는 C/B, PR 시 | 미완료 |

최초 계약은 승인 후 버전 태그를 고정한다. 확정 후 변경은 영향 작업 담당과 먼저
합의하고 migration/호환성/fixture를 함께 수정한다. 승인 기록에는 검토자·시각·PR 링크를 남긴다.
첫 이틀 검토가 지연되면 B가 차단 원인·해결 담당·다음 확인 시각을 작업 카드에 기록한다.
PR #1에 연결한 초안이며 다른 팀원에게 검토 요청을 발송한 상태가 아니다.
승인·차단·수치 결정 근거는 [공동 검토 절차](REVIEW.md)와 [승인 기록](review-record.json)에서 추적한다.

근거: [README 5·7·15.3·15.7·15.8](../README.md),
[Microsoft 단조 시간 정의](https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-queryunbiasedinterrupttime),
[RFC 8785 정규화 배경](https://www.rfc-editor.org/rfc/rfc8785).
검증 실행·범위·환경은 [검증 안내](../tests/README.md)를 따른다.
