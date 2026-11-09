# S1-02a 센서 드라이버 스파이크 보고서

상태: **판정 초안 — 범위 축소 조건부 채택(6절). 팀 검토와 2번째 PC 검증 전.**
담당: A (Kitbomin). 기간: 2026-10-29~11-03 KST. 관련: [README 7·15.1·15.2절](../../README.md).

> 체크하지 않은 항목은 미완료다. "미확인"은 공식 자료나 실측으로 확인하지 못한 항목이다.

## 요약

| 항목 | 현재 결론 |
|---|---|
| 후보 구성 | LibreHardwareMonitorLib 0.9.6 + PawnIO 2.2.0 (별도 설치) |
| WinRing0 | 0.9.5부터 PawnIO로 교체됨. 사용하지 않는다 |
| 채택 여부 | **범위 축소 조건부 채택** (Intel + NVIDIA 노트북 1대에서 검증). 조건 3개는 6절 |
| 서비스 계정 | LocalSystem 필요. LocalService로는 CPU 센서를 읽을 수 없음 |
| 최대 위험 | PawnIO 서명 빌드의 재배포·번들 조건이 공식적으로 명시되지 않음 (B·S1-08 공유 필요) |

## 1. 버전과 출처 (1단계, 2026-10-30 확인)

| 구성요소 | 고정 버전 | 출처 | 라이선스 | 비고 |
|---|---|---|---|---|
| LibreHardwareMonitorLib | 0.9.6 (2026-02-14) | [NuGet](https://www.nuget.org/packages/LibreHardwareMonitorLib/0.9.6), [릴리스](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases) | MPL-2.0 | net472·netstandard2.0·net8.0·net9.0·net10.0. 0.9.7-pre776(2026-10-29)은 사전판이라 고정하지 않음 |
| PawnIO 드라이버·설치 파일 | 2.2.0 (2026-03-15, 연도는 릴리스 순서로 추정) | [PawnIO.Setup 릴리스](https://github.com/namazso/PawnIO.Setup/releases), [pawnio.eu](https://pawnio.eu/) | 소스 GPL-2.0(+모듈·IOCTL 예외). 서명 빌드 조건 **미확인** | 공식판은 "오픈소스와 동일, 디지털 서명"이라고 명시. 별도 상용 라이선스 문의 가능 |
| PawnIO 모듈 | LHM DLL 내장분 (LHM 0.9.6 노트: "PawnIO modules to 2.2") | [PawnIO.Modules](https://github.com/namazso/PawnIO.Modules) | LGPL-2.1 | 모듈 저장소 최신 태그는 0.2.11(2026-08-30). LHM 내장 모듈과의 대응 관계 **미확인** |
| WinRing0 | 사용 안 함 | LHM v0.9.5 "Swap WinRing0 to PawnIO" (#1857) | — | 우회 설치 금지(README 15.1) |

LHM 0.9.6의 THIRD-PARTY-NOTICES에는 PawnIO.Modules(LGPL-2.1) 항목만 있다. PawnIO 드라이버와 설치 파일 항목은 없다.

## 2. 패키지·바이너리 직접 확인 (실행하지 않고 검사, 2026-10-30)

| 대상 | SHA256 | 서명 | 확인 내용 |
|---|---|---|---|
| `LibreHardwareMonitorLib.0.9.6.nupkg` | `60DB5E0D9135F4658B385CAE139D3B9EAEAC0A8E867D322D20B5DBE4C4039197` | NuGet 패키지 서명(.signature.p7s) 포함. 내부 DLL은 Authenticode **NotSigned** | 의존 패키지에 PawnIO 없음. 설치 파일 미포함 |
| `PawnIO_setup.exe` 2.2.0.0 | `1F519A22E47187F70A1379A48CA604981C4FCF694F4E65B734AAA74A9FBA3032` | **Valid**. 게시자 `CN=namazso.eu, O=namazso, C=HU`, 발급 GLOBALTRUST 2015 CODESIGNING 1, Microsoft 타임스탬프 | 파일 버전 2.2.0.0, Copyright 2026 namazso |

LHM DLL(win-x64/net472)에 내장된 PawnIO 모듈 리소스:
`AMDFamily0F`, `AMDFamily10`, `AMDFamily17`, `IntelMSR`, `IsaBridgeEC`, `LpcACPIEC`, `LpcCrOSEC`, `LpcIO`, `RyzenSMU`, `SmbusI801`, `SmbusNCT6793`, `SmbusPIIX4` (각 `.bin`).

DLL 문자열에서 아래 두 값을 확인했다. 이를 근거로 **라이브러리는 모듈만 내장하고, 드라이버는 시스템에 미리 설치되어 있어야 하는 구조로 판단**한다. 드라이버가 없을 때의 동작은 2단계에서 실측한다.
- 장치 경로: `\\?\GLOBALROOT\Device\PawnIO`
- 설치 확인용 레지스트리: `SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO`

## 3. 요구사항 확인표

| 항목 | 결과 | 근거·남은 확인 |
|---|---|---|
| 정확한 버전 고정 | 완료(후보) | 위 표 |
| PawnIO 의존성 | 별도 설치 필요(추정) | DLL 문자열. 미설치 시 동작은 2단계 실측 |
| 재배포 | **미확인** | 설치 파일 저장소에 LICENSE 없음. 공식 사이트에도 번들·재배포 조건 없음. 설치 파일을 번들할지, 사용자가 공식 사이트에서 받게 할지 결정 필요 |
| 설치 동의 | **미확인** | 설치 파일 UI와 EULA 표시 여부를 2단계에서 화면으로 기록 |
| 무인 설치 | **확인** (2026-11-04) | `PawnIO_setup.exe -install -silent`: 창 없이 2.15초, 종료 코드 0 (5.7). 스위치 이름은 공식 문서에 없으므로 버전을 올릴 때 다시 확인 |
| 드라이버 서명·HVCI | **확인** (2026-10-31) | `PawnIO.sys`는 Microsoft Windows Hardware Compatibility Publisher 서명. HVCI·취약 드라이버 차단 목록이 켜진 상태에서 로드됨 (5.3) |
| 관리자 권한 | 설치 시 필요(UAC 승인으로 설치함). 관리자여도 PawnIO 없이는 CPU 온도 null (5.4). 수집 서비스는 **LocalSystem 필요** (5.5) | LHM README: "Some sensors require administrator privileges". 서비스 계정별 실측 결과 **LocalSystem 필요**, LocalService는 CPU 센서 읽기 불가 (5.5). PawnIO 비관리자 노출 설정은 쓰지 않음 |
| 미서명 Unrestricted edition | 사용 안 함 | 서명 검사가 없는 판이라 보안 기능 우회에 해당 |

## 4. 테스트 환경과 진행 범위

테스트 PC 2대를 확보하지 못해 **개발 PC 1대에서 실측**한다(2026-10-30 결정). 범위와 대응은 아래와 같다.

| PC | 용도 | OS | CPU / GPU / 보드 | 보안 설정 | PawnIO / WinRing0 사전 상태 |
|---|---|---|---|---|---|
| pc-a (HP OMEN Transcend 14-fb0xxx 노트북) | 실측 1번 | Windows 11 Pro 10.0.26200 | Intel Core Ultra 9 185H / NVIDIA RTX 4060 Laptop + Intel Arc 내장 / HP 8C58 | HVCI 실행 중, VBS 2(실행). Secure Boot·Defender는 `Env` 단계에서 기록 | 둘 다 없음 (2026-10-30) |
| 실측 2번 | 미확보 | | 팀원 PC 대여 시도(AMD 장비 우선) | | |

| 요구사항 | 단일 PC 대응 | 남는 제한 |
|---|---|---|
| 깨끗한 Windows 11 | 개발 PC의 사전 상태(드라이버 없음, 보안 기본값)를 기록하고 복원 지점을 만든 뒤 진행. 가능하면 Hyper-V VM·Windows Sandbox에서 설치·서명·제거만 추가 확인 | 새로 설치한 OS가 아님 |
| 장비별 지원 표 | Intel + NVIDIA 노트북 1대 | AMD CPU(AMDFamily17·RyzenSMU), AMD GPU, 데스크톱 보드(SuperIO/LPC·팬) **미검증** — 지원 대상으로 표기하지 않는다 |
| Sprint 1 "실제 PC 2대" | 팀원 PC에서 같은 스크립트로 30분 실행 시도 | 미확보 시 카드에 `blocked`(사유·해결 담당·다음 확인 시점) 기록 후 스프린트 회의에 공유 |

실행 도구: [SensorProbe](../../agents/windows/spike/SensorProbe/) (LHM 0.9.6 고정, .NET 10), [Invoke-Spike.ps1](../../agents/windows/spike/Invoke-Spike.ps1) (관리자 PowerShell에서 단계별 실행). 결과는 `tests/results/s1-02a/<PC이름>/`에 남긴다.

## 5. 2단계 검증 결과

### 5.1 대조군: PawnIO 없음, 일반 사용자 (2026-10-30, 10초·1초 주기)

결과 파일: `tests/results/s1-02a/pc-a/20261030-control-nopawnio-user-*.json`. Computer.Open 오류 없음, 10회 측정 중 오류 0회, 주기 초과 0회.

| 장치 | 센서 | 결과 |
|---|---|---|
| Intel Core Ultra 9 185H | 온도 35개, 클럭 16개 | 전부 **null** — 드라이버 필요 |
| | 부하 24개 | ok (OS 성능 카운터 경로로 추정) |
| | 전력 4개(Package·Cores·Memory·Platform) | **0으로 보고** — null이 아님 |
| NVIDIA RTX 4060 Laptop | 온도 3, 전력 1, 부하 13, 클럭 2 | 전부 ok (8.82W, 52~64°C) — PawnIO 없이 읽힘 |
| Intel Arc 내장 | 부하·클럭 ok, 전력 0 | 전력 0은 미지원 여부 확인 필요 |
| HP 8C58 메인보드 | 센서 0개 | 팬·보드 온도 없음(노트북 EC). 관리자·PawnIO 상태에서 재확인 |

에이전트 설계(S1-02b)에 반영할 관찰:
1. **0과 미지원 구분**: 드라이버 없이 CPU 전력이 0으로 나온다. 계약상 미지원을 0으로 보내면 안 되므로, 드라이버 상태와 센서 종류를 근거로 `unsupported`를 판정하는 규칙이 필요하다.
2. **LHM 식별자 중복**: `/gpu-nvidia/0/load/3`을 "GPU Bus"와 "GPU Memory" 두 센서가 함께 쓴다. 계약의 sensor_id는 장비 내 유일해야 하므로, LHM 식별자를 그대로 쓰지 않고 별도 ID로 매핑해야 한다.
3. **식별자 형식**: Intel 내장 GPU 식별자는 PCI 인스턴스 경로를 URL 인코딩한 값(128자 초과, `%` 포함)이라 계약 형식 `[a-z0-9._/-]{1,128}`에 맞지 않는다. 계약의 하드웨어 경로→ID 매핑으로 해결한다.

### 5.2 보안 설정 기록 (`Env`, 2026-10-31 00:12 KST)

| 항목 | 값 |
|---|---|
| Secure Boot | 켜짐 |
| VBS / 메모리 무결성(HVCI) | 실행 중 / 레지스트리 Enabled=1 |
| 취약 드라이버 차단 목록 | 켜짐 (1) |
| Defender | 실시간 보호 켜짐, 변조 방지 켜짐, 제품 4.18.26100.4, 서명 1.463.36.0 |
| Smart App Control | 꺼짐 (0) |
| 복원 지점 | **실패** — 시스템 보호(VSS) 서비스 비활성. 롤백 수단 없이 진행함 |

모든 보안 기능을 기본값(켜짐) 그대로 두고 진행했다. 예외 추가나 기능 해제는 하지 않았다.

### 5.3 PawnIO 설치 (`InstallPawnIO`, 대화형, 2026-10-31 00:29:45 KST 시작)

| 항목 | 결과 |
|---|---|
| 종료 코드 | 0 |
| 설치 완료 | 00:30:46 (`C:\Program Files\PawnIO` 생성, 시스템 이벤트 7045 "PawnIO Service installed") |
| 드라이버 상태 | Running, 시작 방식 **Manual** |
| 드라이버 경로 | `DriverStore\FileRepository\pawnio.inf_amd64_a72a2f969b8b7496\PawnIO.sys` |
| 드라이버 SHA256 | `FCA6E7D58B0CF38DBB913A2B9E532F48629145D395F454B16A9F58E97B8D3940` |
| 드라이버 서명 | **Valid**, `Microsoft Windows Hardware Compatibility Publisher` — HVCI·취약 드라이버 차단 목록이 켜진 상태에서 로드됨 |
| 제거 명령 | `"C:\Program Files\PawnIO\uninstall.exe" -uninstall` |
| WinRing0 | 없음 (설치 전후 모두) |

관찰:
- 설치를 마쳐도 설치 창이 자동으로 닫히지 않는다. 같은 창에서 설치를 다시 누르면 "A previous installation of PawnIO was found. Please uninstall it first before installing again."이 뜬다. 오류가 아니라 중복 설치 방지 동작이다. S3-01에서 PawnIO를 이미 설치한 사용자를 어떻게 처리할지 정할 때 반영한다.
- 시작 방식이 Manual이므로 재부팅 후 드라이버가 어떻게 올라오는지(LHM이 열 때 시작되는지) 재부팅 단계에서 확인한다.
- SmartScreen·Defender 반응: 실행자 보고 기준으로 **경고 없이 설치됨**(UAC 승인만 거침). 화면 캡처는 남기지 못했다. 다음 재설치(`-Silent`) 때 Defender 이벤트 로그로 교차 확인한다.

### 5.4 관리자 콘솔 측정 (60초·1초 주기)

| 조건 | 파일 | 오류 / 주기 초과 | CPU 온도(35) | CPU 클럭 | CPU 전력(4) | 전압 |
|---|---|---|---|---|---|---|
| PawnIO 없음, 관리자 | `20261030T151304Z-nopawnio-admin-run.json` | 0 / 0 | 전부 null | 16개 null | 4개 모두 0 | 없음 |
| PawnIO 있음, 관리자 | `20261030T154002Z-pawnio-admin-run.json` | 0 / 0 | **35개 ok** (Package 57~71°C, Core Max 56~76°C) | **17개 ok** (Bus 99MHz 포함) | Package 8.4~24.6W, Cores 2.4~16.8W, Memory·Platform은 0 | 15개 추가 |

- **관리자 권한만으로는 CPU 센서를 읽을 수 없다.** PawnIO가 있어야 한다.
- NVIDIA GPU는 PawnIO 유무와 관계없이 같은 결과가 나왔다. Intel 내장 GPU 전력은 PawnIO 설치 후 0이 아닌 값으로 바뀌었다.
- 메인보드(HP 8C58)는 PawnIO 설치 후에도 센서 0개다. 이 장비에서 팬 rpm은 **미지원**이다.

S1-02b에 반영할 관찰(5.1에 추가):

4. **드라이버 상태에 따라 센서 구성이 달라진다.** CPU 센서가 79개에서 95개로 늘었다(클럭 1개, 전압 15개 추가). 드라이버 설치·제거·로드 실패 시 새 `sensor_config_id`를 발급해야 한다.
5. **CPU Memory·Platform 전력은 PawnIO가 있어도 계속 0이다.** 이 장비에서는 미지원으로 처리할 후보다. 부하를 걸어 다시 확인한다.
6. **`Distance to TjMax`는 Temperature 종류이지만 실제 온도가 아니라 한계 온도까지 남은 차이다.** 수집 대상 온도에서 제외하거나 별도 kind로 정해야 한다.

### 5.5 서비스 계정 측정

| 계정 | 기간 (UTC) | 측정 횟수 | 오류 / 주기 초과 | CPU 온도 | 대표값 | 결과 파일 |
|---|---|---|---|---|---|---|
| LocalSystem (`NT AUTHORITY\SYSTEM`), 자동 시작 | 2026-10-30 15:46:34 ~ 15:59:43 (13분 9초) | 789 | 0 / 0 | 35/35 ok, null 0회 | CPU Package 48~86°C·7.5~22.1W, GPU Core 41~51°C·3.0~12.5W | `C:\ProgramData\SensorProbeSpike\out\service-system-20261030T154634Z.json` (`ServiceStatus`로 저장소에 복사) |
| LocalService (`NT AUTHORITY\LOCAL SERVICE`, 비관리자) | 2026-11-04 02:17:57 ~ 02:47:15 (29분) | 1,759 | 0 / 0 | **35/35 null** | CPU 클럭 16개 null, CPU 전력 4개 0, CPU 부하·NVIDIA GPU 전체는 ok | `service-local_service-20261104T021756Z.json` |

LocalSystem:

- **연장 실행(재부팅 없이 계속)**: 2026-10-30 15:46:34 ~ 11-04 02:00:49 UTC(4일 10시간 14분). 측정 382,454회(경과 초와 거의 같아 측정 누락 없음), 오류 0회, 주기 초과 0회. CPU 온도 35개 모두 null 0회. 11-04 확인 시점 프로세스 사용량은 Private 161MB, Working Set 116MB, 핸들 531, 스레드 17, 누적 CPU 6,016초(코어 1개 기준 약 1.6%). 측정 기간 중 절전·재개 이벤트(Kernel-Power 42/107)는 없었다. 메모리를 한 번만 측정해서 누수 여부는 판단할 수 없다.
- 센서 138개 중 null이 나온 것은 Intel 내장 GPU의 "D3D Shared Memory Free" 1회뿐이다(789회 중 1회). 수집 대상 센서가 아니다.
- 1초 주기로 10분 이상 연속 읽기를 통과했다.
- 서비스 등록 시 PowerShell 5.1이 `binPath`의 따옴표를 제거했다(등록된 경로에 따옴표 없음). 이 PC는 경로에 공백이 없어 영향이 없지만, 따옴표 없는 서비스 경로는 공백이 있으면 취약점이 된다. 스크립트를 수정했고 S1-06 설치 구성에서도 경로를 따옴표로 감싼다. 수정 후 LocalService 시험에서는 경로가 따옴표로 감싸져 등록된 것을 확인했다(`"C:\ProgramData\SensorProbeSpike\bin\SensorProbe.exe" service ...`).

LocalService:

**LocalService 결과는 PawnIO가 없을 때와 같다.** `Computer.Open`은 오류 없이 끝나지만 CPU 온도·클럭은 null, 전력은 0이 된다. 기본 설정의 PawnIO는 관리자가 아닌 계정에 장치를 열어 주지 않기 때문으로 판단한다(PawnIO 2.2.0 노트에 "비관리자 노출 가능, 권장하지 않음"). 따라서:
- 수집 서비스는 **LocalSystem**으로 실행한다. PawnIO의 비관리자 노출 설정은 쓰지 않는다.
- 일반 사용자로 실행되는 UI(S1-06 Windows 셸)는 센서에 직접 접근하지 않고, 접근이 통제된 로컬 IPC로 서비스에서 값을 받는다(README 7·15.8절).
- 권한이 부족해 CPU 센서가 null·0이 되는 상황을 에이전트가 감지해 `unsupported`/`error`로 보고해야 한다. 0을 그대로 보내면 안 된다(5.1 관찰 1).

### 5.6 재부팅 (2026-11-04)

| 항목 | 결과 |
|---|---|
| 재부팅 전 마지막 기록 | 11-04 02:06:09 UTC에 종료하면서 결과 파일을 저장함(측정 382,773회, 오류 0) — 종료 신호를 받아 정상 종료한 것으로 판단 |
| 부팅 시각 | 11-04 02:06:22 UTC (11:06:22 KST) |
| 서비스 자동 시작 | 02:06:42 UTC, 부팅 후 약 20초. 계정 LocalSystem |
| PawnIO 드라이버 | 부팅 후 Running (시작 방식 Manual 그대로). 시스템 로그에 시작 기록이 없어 **누가 시작했는지는 미확인**. 서비스의 첫 측정부터 CPU 온도가 읽혔으므로 수집에는 문제없음 |
| `Computer.Open` | 1,334ms (재부팅 전 746ms), 오류 없음 |
| 재부팅 후 측정 | 486회(약 8분), 오류 0, 주기 초과 0, CPU 온도 35/35 ok·null 0회, 센서 138/138 ok |

결과 파일: `tests/results/s1-02a/pc-a/20261104T021514Z-service-status.json`, `20261104T021506Z-service-system-20261104T020641Z.json`.

### 5.7 제거·잔여 항목·무인 재설치 (2026-11-04 KST 11:49~11:52)

| 단계 | 결과 |
|---|---|
| SensorProbe 서비스 제거 | 서비스 없음 확인(`probe_service: false`). 배포 폴더 `C:\ProgramData\SensorProbeSpike`는 스파이크용이라 남겨 둠 |
| PawnIO 제거 (`uninstall.exe -uninstall`) | 종료 코드 0, 약 7초. Uninstall 키·`C:\Program Files\PawnIO` 삭제됨 |
| 제거 직후 남은 항목 (재부팅 전) | **드라이버 서비스 `PawnIO`** 남음(Stopped, WIN32_EXIT_CODE 31). **드라이버 저장소 패키지 `oem101.inf`**(원래 이름 `pawnio.inf`, namazso, 2.2.0.0) 남음. 장치 노드 `ROOT\PAWNIO\0000` 남음. `System32\drivers`의 `.sys` 파일은 없음 |
| 무인 재설치 (`PawnIO_setup.exe -install -silent`) | **창 없이 2.15초 만에 끝남, 종료 코드 0**. Uninstall 키 다시 생성, 드라이버 Running. 남아 있던 드라이버 저장소 패키지(같은 해시 `FCA6E7D5…3940`)를 그대로 다시 사용함 |
| 재설치 후 관리자 측정 (60초) | 오류 0, CPU 온도 35/35 ok, 센서 138개 |

관찰:
- **제거 후 드라이버 서비스와 드라이버 저장소 패키지가 남는다.** 제거 후 재부팅을 하지 않고 바로 재설치했기 때문에, 재부팅하면 정리되는지는 **미확인**이다. 다음 제거 시 재부팅 후 `Residue`를 다시 실행해 확인한다.
- `-install -silent`가 공식 문서에는 없지만 2.2.0에서 실제로 동작함을 확인했다. S3-01 설치 파일에서 무인 설치를 쓸 수 있다.
- PawnIO는 다른 앱(Fan Control 등)도 함께 쓰는 공용 의존성이다. 우리 프로그램을 삭제할 때 PawnIO까지 지울지(다른 앱이 깨질 수 있음), 남겨 둘지 S3-01에서 정해야 한다.

### 5.8 이벤트 로그 (`Events`, 최근 2일: 2026-11-02 이후)

| 로그 | 결과 |
|---|---|
| CodeIntegrity | 경고·오류 없음. 부팅 시 정책 적용 기록(3099·3116)과 "WHQL driver enforcement" 활성화(3084)만 있음. PawnIO 차단 기록 없음 |
| Defender | **위협 탐지(1006·1116~1119) 없음.** 경고 1002 두 건은 예약 검사 생략("Scheduled scan was skipped")이고 PawnIO와 무관. 5007은 부팅 시 Defender 구성 갱신 |
| Service Control Manager | SensorProbe(LocalService) 설치 7045 1건. 재설치 때 PawnIO 7045가 없는 것은 기존 드라이버 서비스를 다시 썼기 때문(5.7) |

로그 보관 범위 때문에 **10-31 최초 대화형 설치 시점의 Defender 기록은 포함되지 않았다**. 11-04 제거·무인 재설치 구간에는 탐지가 없었다.

### 5.9 단계별 체크

- [x] PawnIO 미설치 상태에서 LHM 동작 기록 (대조군, 일반 사용자)
- [x] `Env`: Defender·메모리 무결성·취약 드라이버 차단 목록·Secure Boot·Smart App Control 기록 (복원 지점은 실패로 기록)
- [x] `ProbeAdmin`(PawnIO 없음): 관리자 대조군
- [x] `InstallPawnIO`: 대화형 설치, 종료 코드 0, 경고 없음(실행자 보고, 캡처 없음)
- [x] 드라이버 로드와 `.sys` 서명·해시 확인 (HVCI 켠 상태)
- [x] `ProbeAdmin`(PawnIO 있음): CPU 온도·클럭·전력 확인
- [x] `ServiceInstall`: LocalSystem 센서 읽기 (5.5)
- [x] `ServiceInstall`: LocalService 센서 읽기 — CPU 센서 읽기 불가, LocalSystem 필요 (5.5)
- [x] 1초 주기 10분 이상 연속 읽기 (LocalSystem 13분, 789회, 오류 0)
- [x] 재부팅 후 자동 시작과 읽기 (5.6)
- [x] `UninstallPawnIO` → `Residue`: 드라이버 서비스·드라이버 저장소 패키지 남음 (5.7)
- [ ] 제거 후 재부팅하고 `Residue` 재확인
- [x] 무인 재설치 `-install -silent` — 종료 코드 0, 창 없음 (5.7)
- [x] `Events`: CodeIntegrity·Defender·Service Control Manager 이벤트 로그 (5.8)
- [ ] 실측 2번 PC (팀원 PC 대여, AMD 우선)

## 6. 판정 (초안, 2026-11-04 — 팀 검토 전)

**결론: 범위 축소 조건부 채택.** LibreHardwareMonitorLib 0.9.6 + PawnIO 2.2.0을 S1-02b·S1-06의 기본 구성으로 쓴다.

근거 (pc-a, Intel Core Ultra 9 185H + RTX 4060 Laptop 1대):
- Secure Boot·HVCI·취약 드라이버 차단 목록·Defender를 모두 켠 상태에서, 보안 예외 없이 설치·로드됨. 드라이버는 Microsoft WHCP 서명 (5.2·5.3)
- LocalSystem 서비스로 CPU·GPU 센서를 읽음. 4일 10시간 동안 38만 회 측정, 오류 0 (5.5)
- 재부팅 후 자동 시작과 읽기 정상 (5.6), 무인 설치 동작 (5.7)
- WinRing0 미사용

지원 범위 (이 결과로 지원이라고 표기할 수 있는 것):

| 대상 | 판정 |
|---|---|
| Intel CPU 온도·클럭·패키지/코어 전력 | 지원 (Meteor Lake 1종에서 확인) |
| NVIDIA GPU 온도·전력·부하·클럭 | 지원 (PawnIO 없이도 읽힘) |
| Intel 내장 GPU 부하·클럭·전력 | 지원 후보 (전력은 PawnIO 필요) |
| 노트북 팬·보드 온도 | 이 장비에서 미지원 |
| AMD CPU·AMD GPU·데스크톱 보드(SuperIO/LPC 팬) | **미검증 — 지원 대상으로 표기하지 않음** |

조건 (채택을 확정하기 전에 해결할 것):

| # | 조건 | 담당 | 확인 시점 |
|---|---|---|---|
| 1 | PawnIO 서명 빌드의 재배포·번들 조건 확인. 확인 전까지는 설치 파일에 포함하지 않고 공식 사이트에서 받게 하는 방식을 기본으로 둔다 | B (S1-08) + A | S1-06 착수 전 |
| 2 | 2번째 실제 PC(가능하면 AMD)에서 같은 절차 실행 | A + 팀원 | Sprint 2 첫 주 |
| 3 | 제거 후 재부팅 시 잔여 항목이 정리되는지 확인. 삭제 시 PawnIO를 지울지 정책 결정 | A | S3-01 전 |

S1-02b에 넘길 설계 사항: 5.1·5.4의 관찰 1~6 (0과 미지원 구분, LHM 식별자 중복·형식, 드라이버 상태에 따른 센서 구성 변경, Memory·Platform 전력 0, `Distance to TjMax` 제외)과 LocalSystem 실행 요건(5.5).
