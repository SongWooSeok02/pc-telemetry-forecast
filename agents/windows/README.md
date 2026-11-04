# S2-03 자격 증명 및 등록 IPC

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

토큰은 LocalMachine DPAPI ciphertext로만 파일/임시 파일에 기록한다. 교체는 같은 디렉터리에서
원자적으로 수행하고 매 읽기/쓰기 전에 소유자/Allow ACE/재분석 지점을 검사한다.
서비스 내부 업로더만 `Get-DeviceCredential`을 호출하고 Authorization 헤더에 사용한다.
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
