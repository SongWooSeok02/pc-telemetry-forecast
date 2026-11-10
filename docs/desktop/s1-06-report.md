# S1-06 Windows 프로그램 셸(.NET + WebView2)·설치 구성 — 구현·검증 보고서

상태: **구현·PC-A 실측 완료. 완료 조건 중 "개발 도구 없는 PC 초기 실행"은 미검증. 다른 팀원 리뷰 전. 완료로 변경하지 않는다.**
담당 A. 실행 2026-11-09 KST. 선행: S1-02a 판정(LocalSystem·PawnIO 조건), S1-02b 에이전트, S1-04 대시보드, S2-03 `Credentials.psm1`.

코드·설치 구성: [desktop/windows](../../desktop/windows/README.md). 증거: [tests/results/s1-06](../../tests/results/s1-06/).
시각은 KST(UTC+9). 원본 JSON의 `*_utc`는 UTC다.

## 1. 완료 조건 대조

| 조건 | 결과 | 근거 |
|---|---|---|
| S1-02a 결과를 설치 구성에 반영 | **PC-A 충족** | 4절 I1·I2. LocalSystem, 따옴표 경로, PawnIO 확인, 데이터 폴더 ACL |
| .NET 셸에서 WebView2 대시보드 실행 | **충족** | 4절 D2·I5. 설치된 셸(일반 사용자 토큰)이 S1-04 대시보드 표시 |
| WebView2 런타임 검증(있음/없음) | 있음: 충족. 없음: **가상**(빈 고정 런타임 폴더) | 4절 D1·D3 |
| 설치 경로 검증 | 설치·업그레이드·제거(데이터 유지/삭제) 충족. **부트스트래퍼 설치 흐름은 미실행**(런타임이 이미 있음) | 4절 I1~I8 |
| 개발 도구 없는 PC 초기 실행 | **미검증** | 6절 차단 사항 |
| 서버 오프라인·서비스 중지 시 안내 | 충족 | 4절 D4·I4·I6 |
| 다른 팀원 1명 리뷰·자동 검사 통과 | **대기** | CI `.github/workflows/desktop.yml` 추가(원격 실행 결과는 PR에서 확인) |

## 2. 구현 요약

- **셸**(`PcTelemetry.Shell`, WPF, `asInvoker`, self-contained win-x64, WebView2 SDK `1.0.4314.35` 고정)
  - 런타임 판정 → 없으면 안내 화면([WebView2 런타임 설치](동봉 부트스트래퍼) / [다운로드 페이지 열기] / [다시 확인]).
  - 대시보드 주소는 설치 폴더 `shell\shell.json`. https만(루프백 http 예외), 사용자 정보·인증 쿼리(`token`·`password`·`code` 등) 거부.
  - 같은 origin만 셸에서 열고, 외부 https는 기본 브라우저, 그 외 차단. 비밀번호 자동 저장·자동 채우기 끔, Release에서 DevTools 끔.
  - 상단 상태 줄: 수집 서비스(SCM 조회만) · 서버 연결 · 전송 대기 배치 수 · 전송 상태. 5초마다 갱신, 최소화 중 정지.
  - 오프라인: WebView2를 숨기고(에어스페이스) 연결 상태·로컬 대기 데이터 수와 [다시 연결]을 표시. 자동 재시도 없음.
  - 최소화: WebView2 숨김 → 웹에 `visibilitychange: hidden` 전달(README 15.6).
  - 창 닫기: 셸만 종료. 서비스는 건드리지 않는다.
- **세션 분리**: 셸은 장비 토큰·`state\`에 접근하지 않는다. WebView2 프로필은 Windows 사용자별 `%LOCALAPPDATA%\PcTelemetry\Shell\WebView2`.
- **공개 상태 파일**: 에이전트가 10초마다 `%ProgramData%\PcTelemetry\status\agent-status.json`(`pc-telemetry/agent-status/1`)을 쓴다.
  수집 여부·전송 상태·대기 배치 수·마지막 성공 시각만 있고 토큰·machine_id·센서값은 없다. 60초 이상 갱신이 없으면 "갱신 지연".
- **설치 스크립트**(`desktop/windows/install/`, 정식 MSI는 S3-01): `Publish-PcTelemetry.ps1`(배포 폴더·manifest),
  `Install-PcTelemetry.ps1`(설치·업그레이드), `Uninstall-PcTelemetry.ps1`. 단계별 결과를 JSON으로 남긴다.

## 3. 검증 환경

| 항목 | 값 |
|---|---|
| PC | PC-A(S1-02a·S1-02b와 동일 노트북): Intel Core Ultra 9 185H, NVIDIA RTX 4060 Laptop + Intel Arc, Windows 11 Pro 26200 |
| 개발 도구 | **있음**: .NET SDK 10.0.300, Node.js 26.13.0 LTS(대시보드 빌드·`vite preview`용), Git. PowerShell 7 없음(Windows PowerShell 5.1) |
| WebView2 | Evergreen 157.0.4341.42, per-machine, `C:\Program Files (x86)\Microsoft\EdgeWebView\Application` |
| 드라이버 | PawnIO 2.2.0(설치 스크립트가 확인만 함) |
| 빌드 | 셸·에이전트 0.1.0.0 Release win-x64 self-contained. 기반 커밋 `e1eee3f` + 미커밋 설치 스크립트(manifest `dirty: true`) |
| 배포 해시 | `PcTelemetry.Shell.dll` `d2008a6d4c73…`, `PcTelemetry.Shell.Core.dll` `62d878f7dfdd…`, `PcTelemetry.Agent.dll` `2351b638d6f3…`, 부트스트래퍼 1.3.315.17 `cf2f2445fef0…`(서명 Microsoft Corporation) |
| 대시보드 | S1-04 `dashboard/` 빌드 결과를 `vite preview`(127.0.0.1:4173)로 제공. 백엔드 조회 경로가 없어 센서 카드는 "측정 대기" |
| 전송 서버 | 의도적으로 닿지 않는 `http://127.0.0.1:4199`(전송 대기 누적 확인용). 시험용 machine_id·토큰은 무작위 값이며 저장소에 없다 |
| 일반 사용자 실행 | 관리자 세션에서 `runas /trustlevel:0x20000`. Administrators는 거부 전용(deny-only), 무결성 수준은 High로 남음(5절) |

## 4. 결과

자동: `dotnet test desktop/windows/tests/PcTelemetry.Shell.Tests` — 2026-11-09 KST **51/51 통과**, 경고 0.

### 개발 빌드(설치 전)

| # | 시각 | 재현 | 기대값 | 실제값 | 결과 |
|---|---|---|---|---|---|
| D1 | 10:37 | `--check`(설정 없음) | 종료 3, `config_error/missing` | 동일. [check-no-config](../../tests/results/s1-06/check-no-config.json) | 성공 |
| D1 | 10:37 | `--check --dashboard-url http://…` (비루프백 http) | 종료 3, `insecure_scheme` | 동일. [check-insecure-url](../../tests/results/s1-06/check-insecure-url.json) | 성공 |
| D2 | 10:37 | `--check --dashboard-url http://127.0.0.1:5173/` | 종료 0, `dashboard` | 동일. [check-dashboard](../../tests/results/s1-06/check-dashboard.json) | 성공 |
| D3 | 10:38 | `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`=빈 폴더 후 실행 | 종료 2 `runtime_not_found`, 안내 화면 | 동일. [check-runtime-missing](../../tests/results/s1-06/check-runtime-missing.json), [화면](../../tests/results/s1-06/runtime-missing.png). 변수 제거 후 종료 0 | 성공(가상) |
| D4 | 10:47~10:49 | 대시보드 서버 없이 실행 → 서버 시작 → [다시 연결](UIA) | 오프라인 안내(대기 배치 수) → 대시보드, "연결됨" | 동일. "전송 대기 6개 배치 · 재시도 중" → "연결됨" | 성공 |
| D5 | 10:49 | 최소화 → 복원(UIA), 테스트 페이지가 `visibilitychange` 기록 | `hidden` → `visible` | 동일. [화면](../../tests/results/s1-06/minimize-visibility.png) | 성공 |

### 설치 PC(PC-A)

| # | 시각 | 재현 | 기대값 | 실제값 | 결과 |
|---|---|---|---|---|---|
| I1 | 12:44:45~51 | 관리자: `Install-PcTelemetry.ps1 -DashboardUrl http://127.0.0.1:4173/ -ServerUrl http://127.0.0.1:4199 -MachineId <시험용> -TokenFile <시험용>` | 전 단계 성공, 서비스 Running·collecting | 9단계 성공(6.1초). PawnIO 2.2.0 ok, WebView2 present. [install](../../tests/results/s1-06/install.json) | 성공 |
| I2 | 12:45 | `sc qc`·`sc qfailure`, ACL | LocalSystem, AUTO_START, 따옴표 경로, 재시작 60/60/300초. 루트·`state` 상속 해제 SYSTEM·Admins, `status` + Users 읽기 | 동일. 명령줄에는 토큰 파일 경로만 있고 토큰 값은 없음 | 성공 |
| I2 | 12:46 | S2-03 `Assert-CredentialAcl`(서비스 SID S-1-5-18)을 데이터 폴더에 실행 | 루트 통과(`New-CredentialStore` 상위 조건) | 루트·`state` 통과, `status`는 Users 읽기 때문에 거부(자격 증명을 두지 않는 폴더라 의도대로) | 성공 |
| I3 | 12:47 | 일반 사용자 토큰: 상태 파일·`state`·토큰·루트 읽기, 셸 `--check` | 상태 파일만 읽힘, 셸 종료 0·서비스 running | 상태 파일 읽기 OK, 나머지 3개 `UnauthorizedAccessException`. 셸 종료 0, running·fresh·42개 배치. [acl](../../tests/results/s1-06/basic-user-acl.txt), [check](../../tests/results/s1-06/basic-user-check.json) | 성공 |
| I4 | 12:49 | 일반 사용자로 설치된 셸 실행(대시보드 서버 없음) → 서버 시작 → [다시 연결] | 오프라인 안내 → 대시보드 | "수집 서비스: 실행 중 · 서버: 연결 안 됨(연결 실패) · 전송 대기: 71개 배치" → "연결됨". [오프라인](../../tests/results/s1-06/installed-server-offline.png), [대시보드](../../tests/results/s1-06/installed-dashboard.png) | 성공 |
| I5 | 12:50 | 셸을 연 채 서비스 정지 → 시작 | 상태 줄 중지됨 → 실행 중 | 셸 로그 Running → StopPending → Stopped → Running. 정지 직후 상태 파일 `collecting=false`. [check](../../tests/results/s1-06/service-stopped-check.json) | 성공(문구 결함 1건, 아래) |
| I6 | 12:50 | 셸 창 닫기(UIA) | 셸만 종료, 서비스 유지 | 셸 프로세스 없음, 서비스 Running, 상태 파일 계속 갱신. 로그 "shell closed (collection service untouched)" | 성공 |
| I7 | 12:53:12~40 | 재배포 후 같은 인자로 설치 재실행(업그레이드) | 서비스 정지→파일 교체→재시작, 대기 데이터·machine_id 유지 | upgrade=true, 대기 129 → 132(유지·누적), machine_id 동일, 설치 dll 해시 = manifest. 26.8초. [upgrade](../../tests/results/s1-06/upgrade.json) | 성공 |
| I5' | 12:54 | 수정한 셸로 I5 반복 | 정지 시 전송 "멈춤" | "전송: 멈춤(수집 서비스 중지)" → 재시작 후 "대기". [정지](../../tests/results/s1-06/installed-service-stopped-strip.png), [재시작](../../tests/results/s1-06/installed-service-restarted-strip.png) | 성공 |
| I8 | 12:54:52~55:13 | `Uninstall-PcTelemetry.ps1` | 서비스·프로그램·바로 가기 제거, 데이터 유지 | 잔여: 서비스·설치 폴더·바로 가기 없음, `state`·`status` 유지(스풀 155개). [결과](../../tests/results/s1-06/uninstall-keep-data.json) | 성공 |
| I8 | 12:55:13 | `Uninstall-PcTelemetry.ps1 -RemoveData` | 데이터 폴더 제거, PawnIO 유지 | `%ProgramData%\PcTelemetry` 없음, 서비스 레지스트리 키 없음, PawnIO 유지. 사용자별 셸 프로필 1개 남음(설계상 유지·개수만 표시). [결과](../../tests/results/s1-06/uninstall-remove-data.json) | 성공 |

### 검증 중 발견·조치

- **서비스 정지 시 전송 상태 문구**: 정지 직후 상태 파일이 `state=retrying`이라 셸이 "전송: 재시도 중"을 계속 보였다.
  `collecting=false`이면 "멈춤(수집 서비스 중지)"로 표시하도록 고치고 단위 테스트를 추가했다(I5').
- **업그레이드 확인 근거**: self-contained(단일 파일 아님) exe는 공통 실행 호스트라 코드가 바뀌어도 해시가 같았다.
  manifest와 설치 결과에 코드가 든 dll 해시를 함께 기록하도록 바꿨다(I7).
- **서비스 정지 20.3초**: S1-02b의 종료 시 최대 20초 전송 시도(전송 서버 오프라인) 때문이다. .NET 기본 종료 대기(30초) 안이며 변경하지 않았다.
  업그레이드·제거 시간 대부분이 이 대기다.

## 5. 한계

- 이 PC는 개발 도구가 있고 WebView2가 Windows 11에 기본 포함되어 있다. 런타임 없음은 고정 런타임 폴더 변수로만 재현했다.
- 동봉 부트스트래퍼의 서명 확인은 했지만 `/silent /install` 실제 설치·UAC 취소 흐름은 실행하지 않았다.
- 일반 사용자 실행은 관리자 세션에서 만든 제한 토큰이다(Administrators deny-only, 무결성 High). 별도 표준 사용자 계정·Medium 무결성 로그인은 아니다.
- `Credentials.psm1`은 PowerShell 7용이지만 이 PC에 7이 없어 `Assert-CredentialAcl`(Get-Acl만 사용)을 5.1에서 실행했다. `New-CredentialStore`로 실제 저장소는 만들지 않았다.
- 장비 토큰은 S2-03 DPAPI 저장소 연동 전까지 `state\device.token`(SYSTEM·Admins 전용) 평문 파일이다.
- 대시보드 조회 API와 로그인 세션은 아직 없다(B·C). 셸→웹 호스트 상태 메시지는 S2-06. 정식 설치 파일(MSI)·서명은 S3-01.
- 실제 링크 클릭에 대한 이동 정책은 단위 테스트로만 확인했다.

## 6. 차단 사항

| 항목 | 원인 | 담당 | 다음 확인 |
|---|---|---|---|
| 개발 도구 없는 PC 초기 실행·실제 런타임 없음·부트스트래퍼 설치 | 깨끗한 PC 없음, 이 PC는 Windows Sandbox 꺼짐 | A | 팀원 PC 또는 Windows 10 VM에 배포 폴더만 복사해 I1·I4 반복, 런타임 제거 상태에서 부트스트래퍼 흐름 확인 |
| 표준 사용자 계정 실행 | 이 PC에 별도 표준 계정 없음 | A | 표준 계정 로그인 후 I3·I4 반복 |
| 다른 팀원 리뷰 | PR 전 | 리뷰어 1명 | PR에서 요청 |
