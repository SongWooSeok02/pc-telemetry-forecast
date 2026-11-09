# S1-07 실제 PC 센서 지원 표

상태: **PC-A 1대 관측 기록(S1-02a·S1-02b, 담당 A). PC-B 미관측·미검증.** 다른 팀원 리뷰 전.
아래 PC-A/PC-B는 익명 슬롯이며 실제 장비를 식별하지 않는다. 지원/미지원은 해당 버전·권한에서
확인한 값만 기록한다. 미관측을 미지원 또는 0으로 표시하지 않는다. 자동 계약 검증·Windows 시계
probe는 센서 설치 검증을 대신하지 않는다. 계약 매핑 구현: [SensorCatalog.cs](../agents/windows/src/PcTelemetry.Collector/SensorCatalog.cs).

| 항목 | PC-A | PC-B |
|---|---|---|
| CPU/GPU/메인보드 모델 (일련번호 제외) | Intel Core Ultra 9 185H / NVIDIA RTX 4060 Laptop + Intel Arc 내장 / HP 8C58(노트북) | 미관측 |
| OS edition/build/x64 | Windows 11 Pro 10.0.26200 x64 | 미관측 |
| 에이전트 빌드·커밋/.NET 버전 | PcTelemetry.Agent 0.1.0, .NET SDK 10.0.300, 커밋은 [S1-02b 보고서](collector/s1-02b-report.md) 참조 | 미제공 |
| LHM 정확한 버전/패키지 hash | LibreHardwareMonitorLib 0.9.6, nupkg SHA-512 `nwn6J4tEuiQUcBZgxtWHByKSPE6kx9VfQFx36La0TfezjOXJTfljdQjt1jMFWGcNBLX0OHl8rz/owGPNTqJPFA==` | 미제공 |
| 센서 드라이버/PawnIO 정확한 버전·서명 | PawnIO 2.2.0, PawnIO.sys Microsoft Windows Hardware Compatibility Publisher 서명 ([S1-02a 2·3절](spike/s1-02a-report.md)) | 미관측 |
| GPU 드라이버 버전 | NVIDIA 32.0.15.9282 / Intel 32.0.101.6556 | 미관측 |
| 일반 사용자/관리자/서비스 계정 및 ACL | 일반 사용자: CPU 온도·클럭 null, 전력 0 → 드라이버 필요 센서 unsupported. 관리자·LocalSystem: 읽힘. LocalService: CPU 센서 불가 → **LocalSystem 필요** | 미검증 |
| Defender·메모리 무결성 상태 | Defender 실시간·변조 방지 켜짐, HVCI 실행, Secure Boot·취약 드라이버 차단 목록 켜짐 | 미관측 |
| CPU 온도 / 패키지 전력 / 부하 / 클럭 / 팬 | 지원 / 지원 / 지원 / 지원(코어 최대) / **미지원**(노출 안 됨) | 미관측 |
| GPU 온도 / 보드 전력 / 부하 / 클럭 / 팬 | 지원 / 지원 / 지원 / 지원 / **미지원**(노출 안 됨) | 미관측 |
| 설치 동의·드라이버 로드·서비스 읽기 | 경고 없이 설치(실행자 보고, 화면 캡처 없음)·HVCI 상태 로드·LocalSystem 서비스 읽기 확인 | 미검증 |
| 재부팅 후 자동 수집 / 제거 | 스파이크 서비스로 확인 / 제거 후 드라이버 서비스·저장소 잔여(재부팅 후 재확인 전). S1-02b 에이전트 서비스의 재부팅은 미검증(S2-02) | 미검증 |
| 관측 시각(KST/UTC)·관측자·정제 증거 링크 | 2026-10-30~11-04, 관측자 A. [S1-02a 결과](../tests/results/s1-02a/), [S1-02b 결과](../tests/results/s1-02b/) | 미제공 |
| 최종 지원 범위 / 제한·실패 원인 | 아래 2절 표. Intel CPU·NVIDIA GPU 채택, 노트북 팬 미지원, iGPU 후보 | 판정 유보 |

## 장비별 재현 기록

1. PC를 익명화하고 하드웨어·OS·빌드·센서/드라이버 버전·서명·보안 설정을 기록한다.
2. 일반 사용자와 설치에 필요한 관리자 권한을 구분해 설치·드라이버 로드를 확인한다.
3. 일반 사용자 및 실제 서비스 계정에서 센서별 ID/종류/단위/측정 범위·지원 여부를
   기록한다. 각각 CPU/GPU 온도·전력·부하·클럭·팬을 독립 행으로 확장한다.
4. 재부팅 후 서비스 읽기와 제거를 시험한다. 기대값/실제값·오류 코드·시각·성공/실패를 남긴다.
5. 원본 자료는 접근 제한 위치에 두고 저장소에는 정제된 요약/증거 링크만 제출한다.
   토큰·사용자명·호스트명·IP·일련번호·원시 텔레메트리를 포함하지 않는다.
6. 미지원 센서는 null/미지원으로 기록하고 센서 부재·권한 거부·드라이버 차단을 구분한다.

각 사례 기록 형식: `PC 별칭 | 빌드/버전 | 재현 순서 | 기대값 | 실제값 | 실행 시각 |
성공/실패/미실행 | 정제 증거 링크 | 관측자/검토자`.

PC-B 재현 명령(관리자 PowerShell, PawnIO 2.2.0 설치 후):
`PcTelemetry.Agent.exe discover --out pc-b-discover.json` →
`python tests/run_agent_integration.py --exe <PcTelemetry.Agent.exe> --seconds 600 --pc-label pc-b`.

이전 WinRing0를 우회 설치하거나 Defender/메모리 무결성 해제를 전제로 통과 처리하지
않는다. 필요한 드라이버·권한 요구사항은 관측으로 확정한다. 차단 원인: PC-B 실물 미확보
(테스트 PC 1대). 해결 담당: A(관측, 팀원 PC 대여), B(표 통합·검토). 다음 확인: Sprint 2 첫 주.

## 1. 계약 매핑 원칙

- **sensor_id는 LHM 식별자가 아니라 논리 ID**다: `<component_id>/<부위>/<종류>` (예: `cpu0/package/temp`).
  LHM 식별자는 중복(`/gpu-nvidia/0/load/3` = GPU Bus·GPU Memory), 128자 초과·`%` 포함(Intel iGPU),
  드라이버 상태에 따른 증감(79→95개)이 관측되어 계약 형식 `[a-z0-9._/-]{1,128}`에 그대로 쓸 수 없다.
- 원천 센서는 (LHM 식별자, 종류, 이름) 3개를 묶어 찾는다. 같은 식별자의 다른 센서와 섞이지 않는다.
- 하드웨어 지문(종류|모델명)→component_id 매핑을 상태 디렉터리 `components.json`에 저장한다.
  같은 슬롯에 다른 부품이 들어오면 `gpu0-r2`처럼 **새 component_id·sensor_id**를 발급한다(계약: 교체 시 새 ID, 재활용 금지).
- 센서 추가·제거·지원 상태 변경은 **새 sensor_config_id**로 발급한다. 같은 정의 집합으로 돌아오면 기존 구성 ID를 재사용한다.
  발급 구성은 수정·삭제하지 않고 `sensor-configs/<id>.json`에 등록 요청 본문(schema_version + sensors)을 남긴다.
- 미지원 논리 센서도 등록하고 `null + unsupported`로 보고한다. 미지원·결측·오류를 **0으로 보내지 않는다**.

## 2. 채택 센서와 PC-A 결과

PC-A: HP 노트북, Intel Core Ultra 9 185H, NVIDIA RTX 4060 Laptop, Windows 11 Pro 26200,
Secure Boot·HVCI·취약 드라이버 차단 목록·Defender 켜짐. 관리자 권한/LocalSystem + PawnIO 2.2.0.

| sensor_id | kind / unit | scope | LHM 원천 (종류: 이름) | 드라이버 필요 | PC-A | 비고 |
|---|---|---|---|---|---|---|
| `cpu0/package/temp` | temperature / Cel | cpu_package | Temperature: `CPU Package` (AMD 후보 `Core (Tctl/Tdie)`) | PawnIO | supported | AMD 미검증 |
| `cpu0/core-max/temp` | temperature / Cel | cpu_core_max | Temperature: `Core Max` | PawnIO | supported | |
| `cpu0/package/power` | power / W | cpu_package | Power: `CPU Package` (`Package`) | PawnIO | supported | 드라이버 없으면 0이 나옴 → unsupported |
| `cpu0/cores/power` | power / W | cpu_cores | Power: `CPU Cores` | PawnIO | supported | **패키지 전력의 일부. 합산 금지** |
| `cpu0/total/load` | utilization / % | cpu_total | Load: `CPU Total` | 아니오 | supported | |
| `cpu0/core-max/load` | utilization / % | cpu_core_max | Load: `CPU Core Max` | 아니오 | supported | |
| `cpu0/core-max/clock` | clock / MHz | cpu_core_max | Clock: `P-Core #n`·`E-Core #n`·`Core #n`의 최대값 | PawnIO | supported | 파생값(최대). Bus Speed 제외 |
| `cpu0/fan` | fan / rpm | cpu_fan | 메인보드/SuperIO/EC Fan: `CPU Fan`·`CPU` | PawnIO | **unsupported** (sensor_not_exposed) | 노트북 EC 미노출. 데스크톱 미검증 |
| `gpu0/core/temp` | temperature / Cel | gpu_core | Temperature: `GPU Core` | 아니오 | supported | NVIDIA 실측, AMD 미검증 |
| `gpu0/hotspot/temp` | temperature / Cel | gpu_hotspot | Temperature: `GPU Hot Spot` | 아니오 | supported | |
| `gpu0/board/power` | power / W | gpu_board (AMD: gpu_package) | Power: `GPU Package` | 아니오 | supported | NVIDIA는 NVML 보드 전력 |
| `gpu0/core/load` | utilization / % | gpu_core | Load: `GPU Core` | 아니오 | supported | |
| `gpu0/core/clock` | clock / MHz | gpu_core | Clock: `GPU Core` | 아니오 | supported | |
| `gpu0/memory/clock` | clock / MHz | gpu_memory | Clock: `GPU Memory` | 아니오 | supported | |
| `gpu0/fan` | fan / rpm | gpu_fan | Fan: `GPU Fan*`의 최대값 | 아니오 | **unsupported** (sensor_not_exposed) | 노트북 GPU 팬 미노출 |

외장 GPU가 없는 PC는 `gpu0/*`를 등록하되 `unsupported`(component_absent)로 보고한다.

## 3. 수집하지 않는 센서

| 원천 | 이유 |
|---|---|
| CPU `CPU Memory`·`CPU Platform` 전력 | PawnIO가 있어도 항상 0(S1-02a 관찰 5). 물리값인지 미지원인지 구분 불가 |
| `* Distance to TjMax` | 온도 종류이지만 실제 온도가 아닌 여유값(관찰 6) |
| 코어별 온도·클럭·부하(32+개) | 최대·패키지 값으로 대표. 규모 기준(PC당 센서 약 12개, README 5절) |
| NVIDIA `GPU Memory`(부하), `GPU Bus` | LHM 식별자 중복(관찰 2). 모델 입력에 필요하면 별도 ID로 추가 합의 |
| NVIDIA `GPU Memory Junction`, D3D 엔진별 부하 | 1차 범위 밖. 필요 시 새 구성으로 추가 |
| Intel 내장 GPU 전체 | 후보만 둔다. **iGPU 전력은 CPU 패키지 전력에 포함될 수 있어 중복 합산 위험**, 식별자 128자 초과 |
| 메모리·저장장치·배터리·PSU·네트워크 | LHM에서 열지 않음(S1-02a 범위) |

## 4. 품질 판정

| 상황 | quality | reason | 비고 |
|---|---|---|---|
| 유한값 | ok | — | 소수 3자리 반올림. 물리값 0은 ok |
| 구성에서 미지원 | unsupported | `driver_unavailable`·`sensor_not_exposed`·`component_absent` | 값 null |
| 값이 null/NaN | missing | `no_value` | |
| 센서·장치가 이번 주기에 없음 | missing | `sensor_absent` | 구조 변경이면 새 구성 발급 |
| 장치 갱신 예외·원천 전체 실패 | error | `read_failed` | 해당 장치만. 다른 장치는 계속 수집 |
| 지원 구성인데 드라이버 접근이 끊김 | error | `driver_unavailable` | 0 대신 error. 5주기 연속이면 새 구성(unsupported) |
| 직렬화 상한(1e12) 초과 | error | `out_of_range` | 물리 범위 필터 아님(S2-07a 전 적용 금지) |

드라이버 접근 판정은 CPU 온도(Distance to TjMax 제외) 중 하나라도 유한값인지로 한다.
CPU 전력값은 드라이버가 없어도 0이 나오므로 판정에 쓰지 않는다.

## 5. 전력 표시 규칙(README 5절)

- `cpu0/package/power`(CPU 패키지)와 `gpu0/board/power`(GPU 보드)는 **측정 범위가 다르다.** 합계를 PC 전체 소비전력으로 표시하지 않는다.
- `cpu0/cores/power`는 패키지 전력에 포함된다. 패키지와 더하지 않는다.
- iGPU 전력은 수집하지 않는다(CPU 패키지와 중복 가능).

## 6. 미검증·남은 확인(S1-02b)

| 항목 | 담당 / 다음 확인 | 상태 |
|---|---|---|
| 2번째 실제 PC(AMD 우선, 데스크톱 보드 팬) | A + 팀원, Sprint 2 첫 주 | 미검증 — 장비 미확보 |
| AMD CPU `Core (Tctl/Tdie)`·`Package`, AMD GPU scope | A, 2번째 PC 확보 시 | 규칙만 있음 |
| 데스크톱 SuperIO `CPU Fan` 이름 | A, 2번째 PC 확보 시 | 규칙만 있음 |
| 센서 범위(모델 입력) 합의 | C 검토 | 미완료 |
| `sensor_id`·scope 명칭 계약 합의 | B 검토 | 미완료 |
