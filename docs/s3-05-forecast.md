# S3-05 예측 작업자 운영 및 조회 계약

S3-03의 PostgreSQL 저장소와 사용자 소유권 검증 위에서 동작한다. 새 DB에서는 Compose의 `migrate`가 005까지 적용한다. 이미 004를 설치한 환경에서는 005만 한 번 적용한 뒤 API와 작업자를 배포한다. 기존 예측은 그대로 조회되며 `status=ok`를 받는다.

관리자는 `forecast_targets(machine_id, sensor_id, unit)`에 대상 PC·센서를 명시적으로 등록한다. 등록되지 않은 센서는 처리하지 않으며 삭제된 PC도 건너뛴다. 예측 작업자는 `python -m backend.forecast`로 실행한다. Compose에서는 `forecast`가 작업자를 실행하고 읽기 전용 `forecast-models` 볼륨을 `/models`에 연결한다. 직접 실행할 때는 `DATABASE_URL` 또는 libpq 환경 변수와 `FORECAST_MODEL_DIR`을 지정한다.

## 모델 등록·교체

- 운영자가 버전별 JSON 파일을 모델 디렉터리에 배치하고, `forecast_models`에 PC·센서·버전, `training_run_id`, 학습 시작/종료, 상대 경로, 파일 SHA-256, 입력 규격, 평가 JSON을 새 행으로 등록한다. `registered_at`은 자동으로 기록된다. 등록한 메타데이터는 고정되므로 파일이나 결과가 바뀌면 새 버전을 등록한다.
- 입력 규격 예: `{"window_seconds":300,"step_seconds":1,"horizon_seconds":60,"unit":"C"}`. 단위는 실제 관측값과 대상의 단위를 사용한다.
- 지원 파일은 `{"kind":"persistence"}`(1초), `{"kind":"moving_average"}`(300초), `{"kind":"linear","weights":[0.4,0.6],"bias":0}`(예시 2초)이다. 선형 계수의 순서는 오래된 입력부터 최신 입력까지다. 파일 크기는 최대 64 KiB다. 파일 변조, 호환되지 않는 입력 규격 또는 비유한 결과는 기준 모델로 전환한다. S3-04 공식 모델 형식은 확인 후 연동해야 하며, 이 예시 형식을 공식 S3-04 계약으로 간주하지 않는다.
- `SELECT activate_forecast_model(machine_uuid, sensor_id, version);`으로 활성화한다. 활성 버전은 대상당 한 개다. 존재하지 않는 버전으로 바꾸면 이전 버전이 유지된다. 작업자와 같은 DB 잠금을 사용하므로 전환 완료 후 시작된 주기부터 새 버전이 적용된다.

## 주기, 입력, 조회

작업자는 10초 UTC 슬롯에서 동작한다. 한 슬롯의 결과와 실행 기록은 한 트랜잭션에 저장한다. 다른 작업자가 실행 중이면 겹친 실행을 건너뛰며, 같은 슬롯에서 재기동해도 커밋된 결과는 중복되지 않는다. 트랜잭션이 중단되면 같은 슬롯에서 다시 시도할 수 있고, 이미 지나간 슬롯은 나중에 채우지 않는다. 처리 시간이 10초를 넘으면 다음 경계부터 재개한다. 현재는 모든 등록 대상을 한 트랜잭션에서 처리하므로 운영 대상 규모에서 실행 시간을 확인해야 한다.

모델 입력은 해당 PC·센서의 최신 관측으로 구성한다. 최근 10초 이내의 정상 관측이 필요하다. 최신 관측의 품질 또는 단위가 틀리면 이전 정상값으로 대체하지 않는다. 다중 입력 창은 연속된 1초 값이어야 하며 중복 초, 결측, 품질 이상, 센서 구성·부팅 세션 변경은 창을 무효 처리한다. 이때 최신값이 정상이라면 현재값 유지 기준을 사용한다.

`GET /v1/machines/{machine_id}/predictions?sensor_id=…&start=…&end=…`는 기존 대상 시각 반개구간과 페이지 형식을 유지한다. 세션과 PC 소유권을 검증한 뒤 저장값만 읽는다. 각 항목의 `target_at`은 새 예측의 `generated_at`에서 60초 뒤다. `model_version`은 실제로 사용한 모델을 나타내며, 예측 불가 시 `builtin:none`이다.

별도 DB 역할을 사용하는 환경에서는 API에 `predictions` 읽기 권한을 준다. 작업자에는 `forecast_targets`, `forecast_models`, `machines`, `measurements` 읽기 권한, `forecast_cycle` 읽기·삽입·수정 권한, `predictions` 삽입 권한이 필요하다. 모델 등록과 활성화는 관리자 역할에서 실행한다.

| status | value / model_version | reason |
| --- | --- | --- |
| `ok` | 생성값 / 등록 버전 | null |
| `fallback` | 현재값 / `builtin:persistence:v1` | `no_active_model`, `incomplete_window`, `invalid_window`, `model_rejected` |
| `unavailable` | null / `builtin:none` | `stale_or_missing_input`, `invalid_latest_input` |

## 검증 재현

전용 PostgreSQL 연결을 `TEST_DATABASE_URL`로 설정한 뒤 저장소 루트에서 `python -m unittest discover -s tests -p test_forecast.py -v`를 실행한다. 테스트가 격리 스키마에 마이그레이션을 설치하고 종료 시 스키마를 삭제한다. 실제 장비의 온도나 학습 성능을 평가하는 테스트는 아니다.

2026-11-16 03:40–03:42 KST, Windows 개발 PC `DESKTOP-CC6CD6C`, Python 3.12.14/PostgreSQL 16.15, 이 PR의 구현과 합성 입력으로 확인했다.

| 재현 | 기대 | 실제 |
| --- | --- | --- |
| 실제 작업자를 시작하고 첫 커밋 직후 종료·재기동 | 같은 슬롯 중복 없이 다음 10초 경계부터 저장, 각 대상 시각 +60초 | 03:40:40, 03:40:50, 03:41:00 슬롯 각각 1건, 각각 +60초. 통과 |
| 작업자 잠금을 잡은 연결과 두 번째 실행을 겹치고, 첫 결과 삽입 후 롤백을 유도 | 겹친 실행 제외, 실패 기록 제거 및 같은 슬롯 재시도 | 겹친 실행 0건, 롤백 후 조회 0건, 재시도 후 대상당 1건. 통과 |
| 모델을 v1 → v2로 교체하고 결측·변조 파일·비정상 품질을 입력 | 정상 예측은 사용한 버전, 일부 입력 결측은 기준 모델, 최신 입력이 비정상/오래되면 예측 불가 | v1=40, v2=70, 결측 기준값=30, 변조 기준값=45, 품질/시효 불가에서 null. 통과 |
| 같은 조회를 반복하고 다른 소유자 PC를 조회 | 반환값 유지, 다른 PC는 404 | 동일 결과, 404. 통과 |

로컬 회귀: 예측 2건, 집계 3건, 수집·계약 13건, 등록 15건. Docker 실행과 S3-04 실제 산출물 연동은 미검증이다.
