# Windows 에이전트

이 디렉터리에는 수집 에이전트(S1-02b, A)와 자격 증명·등록 모듈(S2-03, B)이 함께 있다.

상태(S1-02b): 1초 수집·센서 매핑·이벤트·불변 배치 구현. **PC 1대 실측, 다른 팀원 리뷰 전.**
설계·검증 결과: [S1-02b 보고서](../../docs/collector/s1-02b-report.md), 센서 표: [sensor-support.md](../../docs/sensor-support.md).
상태(S2-01): SQLite 미전송 버퍼·재전송·상한. **PC-A 장애 시나리오 실측, 다른 팀원 리뷰 전.**
[S2-01 보고서](../../docs/collector/s2-01-report.md).
상태(S2-02): Windows 서비스 수명주기·자동 복구·최소 권한. **PC-A 설치 서비스 실측, 다른 팀원 리뷰 전.**
[S2-02 보고서](../../docs/collector/s2-02-report.md).

| 경로 | 내용 |
|---|---|
| `src/PcTelemetry.Collector/` | LHM 비의존 핵심: 센서 매핑, 1초 루프, 부팅 세션·순번, 절전/시계 이벤트, 불변 배치·`ascii-json-v1` 해시, SQLite 미전송 버퍼(`SqliteBuffer`)·업로더(`Uploader`) |
| `src/PcTelemetry.Agent/` | LHM 0.9.6 어댑터, `QueryUnbiasedInterruptTime` 시계, BootId, 콘솔/서비스 진입점, 상태 파일, 서비스 수명주기·이벤트 로그·접근 점검(`Service.cs`, S2-02) |
| `tests/PcTelemetry.Collector.Tests/` | 가상 센서·가짜 시계 기반 단위 테스트, 계약 공유 해시 벡터 |
| `Credentials.psm1`, `Register-Device.ps1` | S2-03 자격 증명 저장(DPAPI·ACL)·등록 IPC. 아래 [S2-03 절](#s2-03-자격-증명-및-등록-ipc) |
| `spike/` | S1-02a 스파이크 도구(운영 코드 아님) |

## 빌드·테스트

```powershell
dotnet test agents/windows/tests/PcTelemetry.Collector.Tests
dotnet build agents/windows/src/PcTelemetry.Agent -c Release
```

## 실행

CPU 온도·전력·클럭은 PawnIO 2.2.0과 관리자/LocalSystem 권한이 필요하다(S1-02a). 권한이 없으면 해당 센서를
`unsupported`로 등록하고 나머지를 수집한다.

```powershell
$exe = "agents/windows/src/PcTelemetry.Agent/bin/Release/net10.0-windows/win-x64/PcTelemetry.Agent.exe"
& $exe discover --out discover.json                 # 매핑 확인(임시 상태)
& $exe run --state-dir .\state --seconds 60          # 수집만(배치 없음)
& $exe run --state-dir .\state --server https://<host> --machine-id <uuid> --token-file <path>
python tests/run_agent_integration.py --exe $exe --seconds 600 --pc-label pc-a   # 참조 API 통합
```

상태 디렉터리(기본 `%ProgramData%\PcTelemetry\state`): `boot-session.json`(부팅 세션·순번 예약),
`components.json`(하드웨어→component_id), `sensor-configs.json`·`sensor-configs/<id>.json`(불변 구성·등록 본문),
`buffer.sqlite3`(S2-01 미전송 버퍼, 아래), `agent.running`(S2-02 실행 표시), `events.jsonl`, `stats.json`. 실제 PC 상태 디렉터리는 커밋하지 않는다.

토큰은 현재 `--token-file`에서 읽어 헤더에만 쓴다. S2-03 DPAPI 자격 증명 저장소(`device.dpapi`) 읽기와 등록 IPC
호스팅은 S2-03 담당(B)과 형식·호출 방식을 합의한 뒤 후속으로 한다(S2-02에서는 설치 스크립트가 복사한
`state\device.token`을 서비스 계정이 읽는지만 검증).

## S2-01 미전송 버퍼

`state\buffer.sqlite3`(WAL, `synchronous=FULL`). 표와 규칙:

| 표 | 내용 |
|---|---|
| `open_samples` | 수집 즉시 저장한 미동결 샘플. 강제 종료돼도 남고, 다음 시작 때 먼저 동결한다 |
| `batches` | 동결 배치(`pending`·`quarantined`·`sent`). batch_id 발급·본문 동결·샘플 제거를 한 트랜잭션으로 커밋. 트리거가 ID·본문·해시·범위 수정과 `sent` 이후 재전송을 막는다. 전송 완료 시 본문을 지우고 원장 행을 24시간 보관 |
| `gaps` | 상한으로 지운 미전송 범위(부팅 세션·순번·관측 시각·샘플 수·사유 `max_age`/`max_bytes`). 30일 보관 |
| `meta` | `machine_id`, `dropped_samples_total` |

- 전송: 가장 오래된 `pending` 한 건을 같은 ID·본문·해시로 보낸다. 실패해도 건너뛰지 않는다(순서 유지).
  응답 유실·연결 실패·타임아웃·429/5xx는 3/6/12/24/48/60초(+0~20%) 백오프, `Retry-After`가 더 길면 따르되 최대 5분.
  200의 `batch_id`·`accepted_samples`가 맞지 않으면 `upload_ack_mismatch`만 남기고 새 ID로 다시 묶지 않는다.
  본문 해시가 동결 값과 다르면(디스크 손상) 보내지 않고 격리한다.
- 상한: 미전송 데이터(대기·격리 배치 본문 + 미동결 샘플)의 동결 시각이 24시간을 넘거나 바이트 합이 500MB를 넘으면
  오래된 배치부터 지운다. 수집 중 1분마다 검사하므로 최대 1분치만큼 넘을 수 있다. 벽시계가 뒤로 가도 만료로 보지 않는다.
  지운 범위는 `gaps`·`buffer_pruned` 이벤트·공개 상태 파일(`upload.last_gap`, `dropped_samples_total`)에 남긴다.
- 재시작: 열 때 `quick_check`, 손상이면 `buffer.sqlite3.corrupt-<UTC>`로 옮겨 보존하고 새로 만든다(`buffer_reset`).
  machine_id가 바뀌면 이전 장비의 대기 배치를 격리한다. S1-02b `spool/`·`quarantine/` 파일은 같은 ID로 가져온 뒤 지운다.
- 시험용 상한: `--buffer-max-age-seconds N`, `--buffer-max-bytes N`.
- 토큰은 버퍼·이벤트·상태 파일에 쓰지 않는다(단위 테스트가 DB 파일·WAL 바이트를 검사).

장애 복구 검증: `python tests/run_buffer_recovery.py --exe <exe> --scenario outage|kill|response-loss|cap-age|cap-bytes --pc-label pc-a`.

## S2-02 Windows 서비스

설치·등록은 [`desktop/windows/install/Install-PcTelemetry.ps1`](../../desktop/windows/install/Install-PcTelemetry.ps1)(S1-06)이 한다.

| 항목 | 동작 |
|---|---|
| 계정·시작 | `PcTelemetryAgent`, LocalSystem(S1-02a 5.5), 자동 시작. 사용자 로그인·셸·WebView2와 무관하게 세션 0에서 실행 |
| 최소 권한 | 필요 권한 `SeChangeNotifyPrivilege`만(`sc privs`, 서비스 SID `unrestricted`). 시작 점검이 실제 토큰 권한을 기록 |
| 시작 점검 | `service_access_check` 이벤트: 계정 종류(이름 아님), 상태 폴더·버퍼·토큰 파일(열림 여부만), 드라이버 필요 센서의 권한 부족 수, 토큰 권한. 문제가 있으면 이벤트 로그 경고 |
| 정상 종료 | SCM 중지·OS 종료: 수집 취소 → 미동결 샘플 동결 → 전송 취소(마무리 전송 최대 2초, `--stop-drain-seconds`) → SQLite 닫기 → `agent.running` 삭제. OS 서비스 종료 대기 한도(이 PC 5초) 안에 끝낸다 |
| 비정상 종료 | 다음 시작 때 `agent.running`이 남아 있으면 `unclean_shutdown_detected`. 미동결 샘플·대기 배치는 S2-01 버퍼가 복원하고 순번은 예약 상한부터 이어 간다 |
| 실패·복구 | 시작/수집 실패는 이벤트 로그(1002/1005)에 원인을 남기고 종료 코드 1로 끝낸다. SCM 복구(60초·60초·5분, `failureflag=1`)가 다시 시작한다 |
| 권한 오류 | 토큰 파일을 못 읽으면 전송만 멈추고(`upload_paused_auth`) 수집·버퍼는 계속, 파일이 돌아오면 `upload_auth_recovered`. 상태 폴더를 못 쓰면 시작 실패 → 복구 재시도 |

이벤트 로그(Application, 원본 `PcTelemetryAgent`): 1000 시작, 1001 정상 종료(소요 ms), 1002 시작 실패, 1003 비정상 종료 감지,
1004 접근 점검, 1005 수집 중 실패, 1010~1018 주요 수집 이벤트(부팅 세션·전송 재시도/복구·인증 일시정지/복구·격리·버퍼 초기화/상한 제거).
토큰·계정 이름·호스트 이름은 쓰지 않는다. 0번은 .NET 서비스 호스트의 시작/중지 기록이다.

검증: `python tests/run_service_lifecycle.py install|resume|uninstall ...`([tests/README](../../tests/README.md#s2-02-windows-서비스-수명주기)).

## S2-03 자격 증명 및 등록 IPC

PowerShell 7 / Windows 전용 서비스 호스트 모듈이다. S2-02의 설치 서비스가 아직 저장소에
없으므로 서비스 등록·계정 생성·센서 전송 루프를 만들지는 않는다. 설치된 서비스에서 아래
호출을 연결해야 한다. 서비스 계정은 실제 프로세스 SID로 고정하고 UI 입력으로 받지 않는다.

1. 설치 관리자가 `%ProgramData%\PcTelemetry`를 생성한다. 상속을 끄고 소유자와 모든 Allow
   ACE를 서비스 계정/Administrators로 제한한다. 재분석 지점 및 사용자가 쓰는 상위 경로는 금지한다.
2. 서비스에서 `Import-Module ./Credentials.psm1` 후
   `$store = New-CredentialStore -ServiceSid '<설치 서비스 SID>'`를 호출한다.
3. 고정된 신뢰 HTTPS origin으로 `Start-RegistrationPipe -Store $store -Server 'https://서버/'`를 실행한다.
   서비스 종료 시 호스트 프로세스를 정상 종료한다. 파이프 수신 대기는 1초마다 취소 가능하다.
4. 관리자로 실행한 UI 어댑터 `./Register-Device.ps1 -ServiceSid '<설치 서비스 SID>'`에서 코드를 입력한다.
   입력은 숨김 처리하며 명령행/URL에 넣지 않는다. 일반 사용자 UI에 권한을 확장하지 않았다.
   S3-07에서 비관리자 UI가 필요하면 로그인 세션과 서비스 권한 경계를 먼저 합의한다.

파이프는 서비스/관리자 Allow, NETWORK Deny, 첫 인스턴스 강제, 클라이언트의 서버 소유자 확인,
익명 impersonation level, 4KiB 프레임 및 읽기/쓰기 시간 제한을 사용한다.
명령은 `register`(코드 소비 후 로컬 암호화 저장), `clear`(로컬 파일 삭제)뿐이다.
서비스 응답은 성공 여부뿐이며 원문 토큰 조회 IPC는 없다. HTTP redirect는 거부한다.
관리자 및 서비스 계정은 신뢰 경계 안에 있으며 악의적인 관리자에 대한 보호를 주장하지 않는다.

장비 ID와 토큰은 하나의 LocalMachine DPAPI ciphertext로 파일/임시 파일에 기록한다. 교체는 같은 디렉터리에서
원자적으로 수행하고 매 읽기/쓰기 전에 소유자/Allow ACE/재분석 지점을 검사한다.
서비스 내부 업로더는 `Get-DeviceCredential $store -IncludeMachineId`로 `machine_id`와
`device_token`을 함께 읽고 각각 배치 본문과 Authorization 헤더에 사용한다.
기존 토큰 전용 파일은 기본 조회로 계속 읽을 수 있으며 장비 ID가 필요한 경우 재발급한다.
그 반환값과 HTTP 응답/예외/본문을 로그·transcript·설정·백업에 쓰지 않는다.
자격 증명 디렉터리는 백업 대상에서 제외한다. 평문 버퍼는 지우지만 런타임의 불변 문자열/메모리 덤프
완전 제거는 보장하지 않는다. 파일 삭제는 SSD의 물리적 지우기를 보장하지 않는다.

회수: 사용자가 백엔드 `revoke`를 먼저 호출한다. 로컬 `clear`만으로 서버 인증이 회수되지는 않는다.
재발급: 소유자가 `reissue` 코드를 발급하고 UI에 입력한다. 소비 커밋과 함께 구 토큰이 회수된다.
삭제: 소유자가 백엔드 `delete` 후 UI 어댑터 `-Action clear`를 호출한다.
오프라인이면 서버 회수를 우선 완료/재시도하며 로컬 삭제만으로 완료 처리하지 않는다.
응답 유실/저장 실패 시 코드를 재사용하지 말고 소유자가 새 재발급 코드를 받아 복구한다.
재발급 전 로컬 파일이 남아도 서버에서 회수한 토큰으로 수집할 수 없다.

검증: `pwsh -NoProfile -File tests/run_windows_credentials.ps1`.
테스트는 현재 프로세스 SID를 서비스 계정으로 모사하며 실제 커널 제한 토큰으로 파일/IPC 거부를
확인한다. 별도 일반 사용자 로그인, 설치 서비스, 실제 HTTPS 등록 전체 경로 검증은 남아 있다.

추가 HTTPS 검증은 `python tests/run_https_registration.py`로 실행한다(TEST_DATABASE_URL 필요).
클라이언트 전용 테스트 CA로 실제 HTTPS API 전체 수명 주기를 확인하고 Windows UI/IPC의 기본
HTTP 클라이언트가 미신뢰 인증서를 거부하는 것도 검사한다. OS 신뢰 저장소는 변경하지 않는다.
이 검사는 설치 서비스의 실제 계정과 운영 TLS를 연결하는 검증을 대체하지 않는다.
