# Windows 에이전트

이 디렉터리에는 수집 에이전트(S1-02b, A)와 자격 증명·등록 모듈(S2-03, B)이 함께 있다.

상태(S1-02b): 1초 수집·센서 매핑·이벤트·불변 배치 구현. **PC 1대 실측, 다른 팀원 리뷰 전.**
설계·검증 결과: [S1-02b 보고서](../../docs/collector/s1-02b-report.md), 센서 표: [sensor-support.md](../../docs/sensor-support.md).

| 경로 | 내용 |
|---|---|
| `src/PcTelemetry.Collector/` | LHM 비의존 핵심: 센서 매핑, 1초 루프, 부팅 세션·순번, 절전/시계 이벤트, 불변 배치·`ascii-json-v1` 해시, 파일 스풀 |
| `src/PcTelemetry.Agent/` | LHM 0.9.6 어댑터, `QueryUnbiasedInterruptTime` 시계, BootId, 업로더, 콘솔/서비스 진입점 |
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
`spool/`(동결 배치), `quarantine/`, `events.jsonl`, `stats.json`. 실제 PC 상태 디렉터리는 커밋하지 않는다.

토큰은 현재 `--token-file`에서 읽어 헤더에만 쓴다. S2-03 자격 증명 저장소(`Get-DeviceCredential`) 연동은
설치 서비스(S2-02)와 함께 후속으로 한다. SQLite 버퍼·재전송 정책은 S2-01,
서비스 자동 시작·설치는 S2-02/S1-06 범위다.

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
