# S2-03 구현·자체 리뷰

상태: 로컬 구현/자동 검증 및 자체 리뷰 완료. PR로 검토를 요청하며 카드 상태는 변경하지 않는다.
다른 팀원 리뷰와 설치 서비스 통합 검증 전이므로 완료(Done)로 처리하지 않는다.

## API 계약 추가안

`001_ingestion.sql` 다음 `002_registration.sql`을 한 번 적용한다. 빈 Compose DB는 두 파일을
순서대로 적용한다. 기존 볼륨에는 002만 관리자로 적용하며 001을 재실행하지 않는다.
기존 관리자가 등록한 장비 토큰은 유지되지만 소유자 행이 없으므로 새 관리 API로 회수할 수 없다.
해당 장비의 소유권은 운영자가 확인 후 `machines`에 별도로 연결해야 한다.

사용자 로그인 어댑터는 검증된 사용자 ID와 고엔트로피 세션의 SHA-256/만료/회수 상태를
`user_sessions`에 저장한다. 공개 세션 생성 API/기본 로그인/기본 토큰은 없다.
S3-07 로그인 어댑터가 연결되기 전에는 관리 API를 실사용 로그인으로 사용할 수 없다.
장비 토큰과 사용자 세션은 서로 다른 테이블에서 인증하며 사용자 ID 헤더를 신뢰하지 않는다.

| 요청 | 인증 | 결과 |
|---|---|---|
| POST /v1/registration-codes | 사용자 Bearer | 10분/일회용 코드 |
| POST /v1/device/registrations | 코드 capability, JSON registration_code만 | machine_id/device_token/expires_at |
| POST /v1/machines/{id}/reissue | 소유 사용자 Bearer | 같은 장비의 교체 코드 |
| POST /v1/machines/{id}/revoke | 소유 사용자 Bearer | 모든 장비 토큰 및 교체 코드 회수 |
| POST /v1/machines/{id}/delete | 소유 사용자 Bearer | soft delete + 토큰/코드 회수 |

응답 비밀은 `Cache-Control: no-store`. 초기 코드 소유자는 발급 세션으로만 결정된다.
코드 소지는 등록 권한이므로 다른 사용자에게 전달하면 그 소유자 장비를 등록할 수 있다.
소비 요청에 사용자 ID를 넣는 것은 거부하며 UI 로그인 쿠키를 서비스로 전달하지 않는다.
24바이트 난수 코드/48바이트 난수 장비 토큰, 코드·토큰 SHA-256만 DB 저장, 장비 토큰 90일 만료다.
만료 전 소유자가 재발급해야 하며 자동 회전 스케줄러는 포함하지 않는다.
코드 발급/재발급은 소유자당 합계 시간당 10개 제한이며 실패/공개 소비 요청의 IP 제한과 TLS,
요청 시간 제한·헤더 크기 제한은 배포 프록시가 설정해야 한다. 요청/응답 본문·Authorization·쿼리는
APM/프록시 로깅 대상에서 제외한다. 토큰을 URL에 넣는 클라이언트는 지원하지 않는다.

감사는 성공한 코드 발급/등록/재발급/회수/삭제의 actor/machine/time/event만 기록한다.
실패 인증 시도 IP 감시/알림은 배포 로깅 계층의 후속 작업이며 비밀 본문 기록은 금지한다.
장비/소유자 삭제는 수집 이력을 지우지 않는다. 이력 보존/개인정보 삭제 정책과 별개다.
API DB 역할에는 machines/registration_codes/device_credentials의 SELECT/INSERT/UPDATE,
user_sessions SELECT, credential_audit SELECT/INSERT 및 identity sequence USAGE만 부여한다.
세션 발급 역할과 migration-owner를 분리하고 감사 UPDATE/DELETE를 부여하지 않는다.
기존 README의 device_credentials SELECT 전용 설명은 관리 API 역할에는 적용되지 않는다.

## 검증 및 자체 리뷰

재현: `TEST_DATABASE_URL`을 전용 PostgreSQL에 설정하고 `python tests/run_registration.py`.
고유 스키마를 생성/삭제한다. 결과의 빌드 기준 커밋, 소스 SHA-256, PC/OS/버전, UTC 시각,
재현 명령 및 기대/실제 assertion은 [등록 보고서](../tests/results/s2-03-registration.json)에 있다.
Windows는 `pwsh -NoProfile -File tests/run_windows_credentials.ps1`로 재현하며
[Windows 보고서](../tests/results/s2-03-windows.json)에 제한 토큰 조건도 기록한다.
실행 시각은 UTC 원본이며 KST는 +09:00이다. CI 파일 추가는 원격 CI 통과 증거가 아니다.

로컬 결과: 등록 6개, [수집 회귀 13개](../tests/results/s2-03-ingestion-regression.json),
기존 계약 33개, 보고서 실행기 회귀 2개와 Windows 보안 검사가 통과했다.
신규 발급 토큰으로 실제 배치를 저장한 뒤 삭제 후 동일 배치를 재전송하면 401임을 확인했다.
기존 계약 상세 보고서는 `local-evidence/s203-contracts.json`에 보관한다.
Windows 표준 API 근거: [DPAPI 범위](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.dataprotectionscope),
[파이프 ACL 생성](https://learn.microsoft.com/en-us/dotnet/api/system.io.pipes.namedpipeserverstreamacl.create).

자체 리뷰에서 수정한 사항:

- 최초 인증과 저장 사이 회수 경합: 저장 트랜잭션에서도 토큰 상태 확인 및 공유 행 잠금.
  회수 응답 이후 시작하는 수집은 거부되며 이미 잠금 획득한 요청은 회수보다 먼저 완료할 수 있다.
- 코드 소비와 소유자 관리의 잠금 순서: 소유자 advisory lock을 먼저 획득해 역순 교착 방지.
- 재발급도 초기 코드와 동일한 발급 제한 적용. 삭제/회수 시 미소비 교체 코드도 무효화.
- 파일 생성 후 권한 축소하는 창 제거: 보호 ACL을 적용한 디렉터리를 생성하고 암호문만 기록.
- 파이프 원격 NETWORK 접근 거부, UI의 서버 소유자 확인, 제한된 프레임/타임아웃,
  서비스 종료를 방해하던 무기한 연결 대기 제거, HTTP 리다이렉트 거부.

남은 확인: A가 S2-02 실제 서비스 SID/설치 ACL을 연결한 뒤 별도 일반 사용자 로그인에서 파일과
파이프 거부, 서비스 재시작·설치/삭제, HTTPS 등록→수집→회수→삭제 전체 흐름을 확인해야 한다.
B가 S1-01/S3-07 담당과 위 추가 API/opaque 세션 계약을 공동 검토해야 한다.
자동 검증은 제한 토큰/현재 SID 모사 환경이며 이 후속 확인을 대체하지 않는다.
다음 확인은 S2-03 시작일 2026-11-05 KST에 선행 작업 산출물 연결 시 진행한다.
