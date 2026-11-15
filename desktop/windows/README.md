# Windows 프로그램 셸 (S1-06)

일반 사용자 권한으로 실행하는 .NET(WPF) 셸에서 WebView2로 공통 대시보드(S1-04)를 표시하고,
수집 서비스 상태와 설치 환경을 확인한다. **다른 팀원 리뷰 전.**

| 경로 | 내용 |
|---|---|
| `src/PcTelemetry.Shell.Core/` | Windows 비의존 판단 로직: 대시보드 주소 검증, 런타임 판정, 이동 정책, 상태 문구 |
| `src/PcTelemetry.Shell/` | WPF + WebView2 셸(SDK `1.0.4314.35` 고정, self-contained win-x64, `asInvoker`) |
| `tests/PcTelemetry.Shell.Tests/` | Core 단위 테스트 |
| `install/` | 배포 폴더 생성·설치·제거 스크립트(정식 설치 파일은 S3-01) |

## 동작

- **WebView2 런타임**: `GetAvailableBrowserVersionString`으로 판정하고 EdgeUpdate 레지스트리에서 범위·경로를 읽는다.
  없으면 안내 화면: [WebView2 런타임 설치](동봉 Microsoft 부트스트래퍼가 있을 때) / [다운로드 페이지 열기] / [다시 확인].
- **대시보드 주소**: 설치 폴더 `shell\shell.json`의 `dashboard_url`(개발 시 `--dashboard-url`). https만 허용(루프백 http 예외),
  사용자 정보·`token`·`password`·`code` 등 인증 쿼리는 거부한다. 같은 origin만 셸에서 열고 외부 https는 기본 브라우저, 그 외 차단.
- **세션 분리**: WebView2 사용자 데이터는 Windows 사용자별 `%LOCALAPPDATA%\PcTelemetry\Shell\WebView2`.
  셸은 장비 토큰·상태 폴더에 접근하지 않는다. 공통 웹 세션과 같은 origin의 로그인 배치는
  [S3-07 세션 계약](../../docs/s3-07-session.md)을 따른다.
- **상태 줄**: 수집 서비스(SCM 조회만, 제어 없음) · 서버 연결 · 전송 대기 배치 수 · 전송 상태.
  전송 대기는 에이전트가 10초마다 쓰는 공개 상태 파일 `%ProgramData%\PcTelemetry\status\agent-status.json`
  (`pc-telemetry/agent-status/1`, 토큰·machine_id·센서값 없음)에서 읽는다. 확인하지 못한 값은 0이 아니라 "확인 불가".
- **오프라인**: 대시보드 서버에 닿지 않으면 연결 상태·로컬 대기 데이터 수를 보여 주고 [다시 연결]을 둔다(자동 재시도 없음).
- **최소화**(README 15.6): WebView2를 숨겨 웹에 `visibilitychange: hidden`을 전달하고 상태 확인을 멈춘다. 창 상태는 S2-06의 가시성 메시지로도 전달하며, 셸은 인증 정보를 메시지로 보내지 않는다.
- **창 닫기**: 셸만 종료한다. 수집 서비스는 계속 실행된다.
- `--check [--out PATH]`: 창 없이 런타임·설정·서비스·상태 파일 판정을 JSON으로 남긴다(종료 코드 0 표시 가능, 2 런타임 없음, 3 설정 오류).

## 빌드·테스트

```powershell
dotnet test desktop/windows/tests/PcTelemetry.Shell.Tests
dotnet build desktop/windows/src/PcTelemetry.Shell -c Release
$exe = "desktop/windows/src/PcTelemetry.Shell/bin/Release/net10.0-windows/win-x64/PcTelemetry.Shell.exe"
& $exe --dashboard-url http://127.0.0.1:5173/      # dashboard: npm run dev
```

## 설치 구성 (S1-02a 반영)

| 항목 | 설정 | 근거 |
|---|---|---|
| 수집 서비스 | `PcTelemetryAgent`, **LocalSystem**, 자동 시작, 실패 시 재시작(60s/60s/300s) | S1-02a 5.5: LocalService로는 CPU 센서 null |
| 서비스 경로 | 따옴표로 감싼 `ImagePath`를 레지스트리에 쓰고 다시 읽어 확인 | S1-02a: PowerShell 5.1이 binPath 따옴표 제거 |
| 센서 드라이버 | PawnIO 2.2.0 설치 여부·버전만 확인(설치하지 않음). 없으면 CPU 온도·전력·클럭은 `unsupported` | S1-02a 판정·설치 동의 |
| UI | 셸은 `asInvoker`(일반 사용자). 센서·토큰 접근 없음 | S1-02a 결론, README 7·15.8 |
| 데이터 폴더 | `%ProgramData%\PcTelemetry`, `state\`: 상속 해제, SYSTEM·Administrators만 FullControl. `status\`: + Users 읽기 | S2-03 `Credentials.psm1` 상위 경로 조건 |
| WebView2 | 없으면 동봉 Evergreen 부트스트래퍼(Microsoft 서명 확인)로 `/silent /install` | README 7절 |
| 업데이트 | 서비스 정지 → 프로그램 파일만 교체, `%ProgramData%` 상태·미전송 데이터 유지 | README 7절 |
| 제거 | 서비스·프로그램·바로 가기 제거. 데이터는 `-RemoveData`일 때만, `Credentials\`·PawnIO·WebView2는 유지 | S2-03 회수 절차, 공유 구성요소 |

장비 토큰은 S2-03 DPAPI 저장소 연동 전까지 `-TokenFile`을 `state\device.token`(SYSTEM·Administrators 전용)에 복사한다.

```powershell
# 개발 PC: 배포 폴더 생성(관리자 불필요). 결과 desktop/windows/publish (Git 제외)
powershell -ExecutionPolicy Bypass -File desktop\windows\install\Publish-PcTelemetry.ps1

# 대상 PC: 관리자 PowerShell에서 배포 폴더의 스크립트 실행
.\Install-PcTelemetry.ps1 -DashboardUrl https://<dashboard>/ [-ServerUrl https://<api> -MachineId <uuid> -TokenFile <path>] -ReportPath install.json
.\Uninstall-PcTelemetry.ps1 [-RemoveData] -ReportPath uninstall.json
```

검증 결과: [S1-06 보고서](../../docs/desktop/s1-06-report.md).
