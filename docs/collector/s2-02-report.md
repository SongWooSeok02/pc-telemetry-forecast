# S2-02 Windows 서비스 수명주기·자동 복구 — 구현·검증 보고서

상태: **구현·PC-A 설치 서비스 실측 완료. PR #31 1차 리뷰(P2·P3) 반영, 재리뷰 전. 실제 사용자 로그아웃은 미검증(PC-A는 학교 관리 노트북으로
로그아웃 불가, 6절). 완료로 변경하지 않는다.** 담당 A. 기간 2026-11-05~11-10 KST(마감 초과, 11-11 실행).
선행: S1-02a(LocalSystem 필요, 5.5), S1-02b(부팅 세션·순번), S2-01(SQLite 버퍼), S1-06(설치 스크립트).

## 1. 완료 조건 대조

| 조건 | 결과 | 근거 |
|---|---|---|
| Windows 서비스 등록·자동 시작과 재부팅 후 수집 재개 | **충족** | 설치(R0): LocalSystem·자동 시작·복구 동작. 재부팅(R8): 부팅 18.8초 뒤 서비스 시작, 23.1초 뒤 첫 샘플, 새 부팅 세션 순번 0부터, 이전 세션 정상 종료 |
| 프로그램 창을 닫아도 수집 유지, 서비스 계정의 센서 권한 확인 | **충족** | UI 종료(R5): 셸만 종료, 서비스 PID 유지. 접근 점검(R1): local_system, 센서 13/15 ok(드라이버 필요 센서 권한 부족 0, 나머지 2개는 팬 미노출), 토큰 권한 `SeChangeNotifyPrivilege`만 |
| 검증 방법: 재부팅·로그아웃·UI 종료·강제 중지/재시작·권한 오류에서 자동 복구와 로그 | **로그아웃 외 충족** | R2~R8. 로그아웃은 자동 로그인 PC라 "로그인 전 수집"과 구조적 근거로 대체(6절) |

## 2. 구현

코드: [agents/windows](../../agents/windows/README.md#s2-02-windows-서비스) — `Service.cs`(신규)·`Program.cs`(Agent), `StateStore.cs`의
`RunMarker`·`Upload.cs`(Collector). 설치: [Install-PcTelemetry.ps1](../../desktop/windows/install/Install-PcTelemetry.ps1)·
[Uninstall-PcTelemetry.ps1](../../desktop/windows/install/Uninstall-PcTelemetry.ps1)(S1-06 파일에 추가).

- **등록**(설치 스크립트): `PcTelemetryAgent`, LocalSystem, 자동 시작, 따옴표 경로(S1-06 그대로). S2-02 추가:
  - 복구: 실패 시 재시작 60초·60초·5분, 하루 뒤 초기화. `failureflag=1`로 **오류 종료 코드로 멈춘 경우도** 복구 대상(기본은 충돌만).
  - 이벤트 로그 원본 `PcTelemetryAgent`(Application) 등록. 제거 스크립트가 원본 등록을 지운다.
  - **최소 권한**: 계정은 LocalSystem을 유지하되(PawnIO 장치 접근, S1-02a 5.5) 서비스 SID `unrestricted` + 필요 권한
    `SeChangeNotifyPrivilege`만(`sc privs`). 토큰에서 SeDebug·SeLoadDriver·SeTcb·SeBackup·SeRestore 등이 빠진다.
    이 상태로 CPU 온도·전력·클럭을 포함해 S1-02a와 같은 센서가 ok(R1).
  - 설치 보고서는 복구 설정을 `sc.exe` 출력이 아니라 레지스트리(`FailureActions`, `FailureActionsOnNonCrashFailures`,
    `ServiceSidType`, `RequiredPrivileges`)에서 읽는다(한국어 Windows에서 `sc.exe qfailure` 출력이 번역돼 판정이 틀렸음).
- **이벤트 로그**: 1000 시작(버전·계정 종류·상태 폴더·전송 설정 여부), 1001 정상 종료(소요 ms), 1002 시작 실패(예외 종류·메시지),
  1003 비정상 종료 감지, 1004 접근 점검, 1005 수집 중 실패, 1010~1018 주요 수집 이벤트(부팅 세션, 전송 재시도/복구, 인증 일시정지/복구,
  격리, 버퍼 초기화/상한 제거), 1019 업로더 실패. 상태 폴더를 못 쓰는 권한 오류도 남도록 파일이 아니라 이벤트 로그를 쓴다. 토큰·계정 이름·호스트 이름 없음.
- **실패 → 자동 복구**: 시작/수집 실패를 1002/1005로 남기고 종료 코드 1로 끝낸다. **`Environment.ExitCode`만 두면 .NET 서비스 호스트가
  SCM에 0을 보고해 복구가 걸리지 않았다**(파일럿 실측: `WIN32_EXIT_CODE 0`, 서비스 정지 상태로 방치). `ServiceBase.ExitCode`를 직접
  설정해 SCM이 7023(오류 종료)·7031(복구 동작)을 기록하고 다시 시작하게 했다.
  시작 실패와 수집 중 실패는 센서 구성이 정해진 시점(워밍업·탐색 완료)으로 가른다. 그 전 실패는 1002, 이후는 1005(PR #31 리뷰 P3).
- **실행 중 업로더 실패**(PR #31 리뷰 P2): 업로더 작업은 중지 요청 전에는 끝나지 않으므로 먼저 끝나면 예외로 죽은 것으로 본다
  (예: SQLite 쓰기 오류). 이전에는 수집 루프가 업로더를 종료 때만 기다려 전송 없이 수집만 계속되고 SCM 복구도 걸리지 않았다.
  이제 `upload_failed`(1019)를 남기고 수집을 정상 절차로 멈춘 뒤(동결·DB 닫기·`agent.running` 삭제) 1005·종료 코드 1로 끝내
  SCM이 다시 시작한다. 중지 요청 뒤 종료 절차 중에 업로더가 실패하면 중지한 서비스를 되살리지 않도록 기록만 하고 마무리 전송을 건너뛴다.
- **정상 종료**(SCM 중지·OS 종료): 수집 취소 → 미동결 샘플 동결 → 상한 정리 → 전송 취소 → 마무리 전송(서비스 기본 2초,
  `--stop-drain-seconds`) → 통계·상태 파일(`collecting=false`) → SQLite 닫기(WAL 체크포인트) → `agent.running` 삭제.
  S1-02b의 마무리 전송 20초는 **이 PC의 OS 서비스 종료 대기 한도(`WaitToKillServiceTimeout` 5000ms)를 넘어** OS 종료 때 강제 종료될 수
  있어 서비스 모드에서 2초로 줄였다(콘솔 `run`은 20초 유지). 못 보낸 배치는 버퍼에 남아 다음 시작 때 같은 ID로 보낸다(S2-01).
  호스트 종료 대기(`ShutdownTimeout`)는 15초.
- **비정상 종료**: 시작 때 `agent.running`(모드·PID·시작 시각)을 만들고 DB를 닫은 뒤에만 지운다. 다음 시작 때 남아 있으면
  `unclean_shutdown_detected`(이벤트 1003). 복원은 S2-01 버퍼(미동결 샘플 동결)와 S1-02b 부팅 세션(같은 부팅이면 예약 상한부터
  순번, 재부팅이면 새 세션·0부터)이 한다.
- **접근 점검**(`service_access_check`, 센서 구성이 정해진 뒤 1회): 계정 종류(`local_system` 등, 이름 아님)·상태 폴더·버퍼·
  토큰 파일(열 수 있는지만, 내용은 읽어 기록하지 않음)·센서 지원 수·드라이버 필요 센서 중 `driver_unavailable` 수·프로세스 토큰 권한
  이름. 서비스인데 LocalSystem이 아니거나 권한 부족 센서가 있거나 토큰을 못 열면 `ok=false`·이벤트 로그 경고.
- **토큰 권한 오류**: 토큰 파일을 못 읽으면 전송만 `paused_auth`(60초마다 재시도)로 멈추고 수집·버퍼는 계속한다.
  복구되면 `upload_auth_recovered`(신규, 이벤트 1015)를 남긴다.
- **세션 독립**: 서비스는 세션 0에서 실행되고 받는 제어는 중지·종료뿐이다(`STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN`,
  세션 변경 알림 없음). 사용자 프로필·HKCU·WebView2를 쓰지 않고 상태는 `%ProgramData%\PcTelemetry`에 있다.

## 3. 검증 환경

| 항목 | 값 |
|---|---|
| PC | PC-A(S1-02a·S1-02b·S2-01과 같은 노트북, 학교 지원 관리 PC, 자동 로그인) |
| OS | Windows 11 Pro 10.0.26200, `WaitToKillServiceTimeout` 5000ms |
| 드라이버 | PawnIO 2.2.0.0 |
| 빌드 | 1차(11-11): 커밋 `e54c4b5` + 이 PR 변경(작업 트리, manifest `dirty=true`), Agent dll SHA-256 `5ec0e29197e1…`(배포 폴더·설치본·재빌드 결과 동일). 2차(11-13, 리뷰 반영): 커밋 `016b2fc`(manifest `dirty=false`), Agent dll `ca4e9e28499a…`(배포 폴더 = 설치본). 러너는 `016b2fc` 뒤 `upload_fault` 시나리오 수정분(대기 배치 확보) 포함. .NET SDK 10.0.300 |
| 설치 | `Publish-PcTelemetry.ps1 -NoWebView2Bootstrapper` → `Install-PcTelemetry.ps1 -SkipWebView2`(WebView2 기설치) |
| 서버 | 참조 API(`tests/reference_api.py` `Store`, SQLite 원장)를 루프백 고정 포트로 실행, 합성 토큰·machine_id |
| 러너 | `tests/run_service_lifecycle.py`(관리자 Python 3.13.9). 단계 사이 원장·체크포인트를 작업 폴더에 보관 |

## 4. 자동 검증

`dotnet test agents/windows/tests/PcTelemetry.Collector.Tests`: **65/65 통과**(2026-11-11 KST, S2-01 60개 + 5개. 리뷰 반영 후 2026-11-13 재실행 65/65).
리뷰 반영분(업로더 감시·시작/실행 실패 구분)은 Agent 프로젝트(LHM·SCM 의존)에 있어 단위 테스트 대신 설치 서비스 실측 R6b로 확인했다.

| ID | 테스트 | 확인 |
|---|---|---|
| V1 | `Clean_release_is_not_reported_as_unclean` | 정상 해제 후 다음 시작은 비정상 아님 |
| V2 | `Marker_left_by_a_killed_process_is_reported_once` | 강제 종료로 남은 표시 → 이전 모드·시작 시각 보고, 다음 정상 해제 후 사라짐 |
| V3 | `Torn_marker_still_counts_as_unclean` | 쓰다 끊긴 표시도 비정상으로 판정 |
| V4 | `Boot_session_survives_service_restart_and_changes_on_reboot` | 서비스 재시작: 같은 세션·순번 재사용 없음, 재부팅: 새 세션·0부터·이전 BootId 기록 |
| V5 | `Unreadable_token_file_pauses_upload_and_recovers_when_restored` | 토큰 파일 없음: 토큰 없이 요청 안 함, `paused_auth`, 수집 계속, 복원 후 `upload_auth_recovered`·순서대로 전송 |

## 5. 설치 서비스 실측 (PC-A)

결과: [install](../../tests/results/s2-02/pc-a-20261111T061041Z-install.json)(R0~R7·원장),
[resume-reboot](../../tests/results/s2-02/pc-a-20261111T094642Z-resume-reboot.json)(R8),
[uninstall](../../tests/results/s2-02/pc-a-20261111T094744Z-uninstall.json)(R9). 시각은 KST.

**2차 실행(2026-11-13, 리뷰 반영 빌드 `016b2fc`)**: [install](../../tests/results/s2-02/pc-a-20261113T050906Z-install.json)(R0~R7·R6b·원장),
[uninstall](../../tests/results/s2-02/pc-a-20261113T050929Z-uninstall.json). R0~R7 모두 다시 성공(중지 1.38초/2.35초, 강제 종료 후 60.8초 뒤 SCM 재시작,
상태 폴더 쓰기 거부 → 1002 2회 → ACE 제거 297.2초 뒤 복구, 원장 53배치·152샘플·중복 0). 재부팅(R8)은 1차 빌드로만 확인했다(변경이 종료·재부팅 경로를 바꾸지 않음).
같은 날 먼저 돌린 실행은 `upload_fault` 시나리오 설계 오류(버퍼를 비운 뒤 잠가 업로더가 쓸 배치가 없음)로 그 시나리오만 실패해 결과 파일을 남기지 않고 시나리오를 고쳐 처음부터 다시 실행했다.

| ID | 시각 | 재현 | 기대값 | 실제값 | 판정 |
|---|---|---|---|---|---|
| R0 | 15:00:44~55 | 깨끗한 설치 | LocalSystem·Auto·따옴표·복구 60/60/300초·failureflag·원본·필요 권한 1개, 수집 | 모두 일치(초기화 86400초, SID 유형 1, `[SeChangeNotifyPrivilege]`), 서비스 프로세스 세션 0, 설치 dll = manifest | 성공 |
| R1 | 15:00:55~01:05 | 시작 후 접근 점검 | 계정·폴더·버퍼·토큰 ok, 권한 부족 센서 0 | `ok=true`, local_system, 센서 15개 중 13 supported, 드라이버 필요 6개 중 5 supported·`driver_unavailable` 0, 토큰 권한 `SeChangeNotifyPrivilege`만 | 성공 |
| R2 | 15:01:25~29 | 서버 정상 중 `sc stop` → `sc start` | 5초 안 중지, 표시 삭제·WAL 정리·`collecting=false`, 같은 세션·순번 재사용 없음 | 중지 1.5초, 3개 모두 확인, 재시작 next_seq 600 > 서버 최대 26(예약 건너뛰기), 비정상 표시 없음 | 성공 |
| R3 | 15:01:29~02:05 | 서버 30초 중지 후 `sc stop` → `sc start` → 서버 재개 | 대기 배치가 있어도 5초 안 중지, 재시작 후 전송 | 대기 10개 상태에서 중지 2.27초, 정리 3개 확인, 재시작 후 모두 전송 | 성공 |
| R4 | 15:02:05~03:09 | 서비스 프로세스 `taskkill /F` | 수동 시작 없이 SCM 복구(60초), 비정상 감지, 같은 세션·순번 재사용 없음 | 60.6초 뒤 SCM 재시작(7031), `unclean_shutdown_detected`(이전 모드 service), next_seq 1800 > 서버 최대 629 | 성공 |
| R5 | 15:03:09~30 | 설치된 셸 실행 → 창 닫기 | 셸만 종료, 서비스 유지 | 셸 종료, 서비스 PID 동일, 닫은 뒤 상태 파일 갱신·`collecting=true` | 성공 |
| R6 | 15:03:30~04:34 | `state\device.token` 이름 변경 → 복원 | 전송만 멈춤, 수집 계속, 자동 재개 | `upload_paused_auth`(401 `TOKEN_FILE_UNREADABLE`), 멈춘 동안 대기 6개로 증가·`collecting=true`, 복원 후 `upload_auth_recovered`, 전송 완료, PID 동일 | 성공 |
| R7 | 15:04:34~10:38 | `sc stop` → 상태 폴더에 SYSTEM 쓰기 거부 ACE → `sc start` → ACE 제거 | 원인 로그, 종료 코드 1, SCM 재시도, 고친 뒤 수동 시작 없이 복구 | 1002 "Start failed: UnauthorizedAccessException … agent.running.tmp … denied" 2회(15:04:36, 15:05:36 SCM 재시도), `WIN32_EXIT_CODE 1`, SCM 7023+7031 2회. ACE 제거 301.3초 뒤(3번째 복구 지연 5분) 수동 시작 없이 수집 재개 | 성공 |
| R6b | 11-13 13:56:07~58:59 | 참조 API 중지(대기 배치 2개) → `stateuffer.sqlite3`에 배타 쓰기 잠금 → `upload_failed` 직후 해제·API 재개 | 업로더 실패가 서비스 실패 경로로: 1019·1005, 수집 정상 종료, 종료 코드 1, SCM 재시작, 같은 세션·순번 재사용 없음, 전송 | 잠금 후 재시도 기록(쓰기)이 busy_timeout 뒤 `SqliteException` 'database is locked' → 1019·1005 "Collection failed: upload failed: SqliteException …"(13:57:53), `WIN32_EXIT_CODE 1`, `agent_stopped` 있음·`unclean_shutdown_detected` 없음, 약 60초 뒤 SCM 재시작, next_seq 2400 > 서버 최대 1883, 전송 완료 | 성공 |
| 원장 | 15:10:38 | 서버 원장 ↔ 버퍼 | 중복 0, 대기·격리 0 | 서버 49배치·146샘플(한 부팅 세션, 순번 0~1885 중 재시작 예약 건너뛰기만), 버퍼 sent 49·pending 0·quarantined 0 | 성공 |
| R8 | 15:31~18:46 | 체크포인트 → Windows 다시 시작 → 자동 로그인 → `resume --expect reboot` | 새 부팅 세션·0부터, 자동 시작, 이전 세션 정상 종료, 버퍼 전송 | 아래 | 성공 |
| R9 | 18:47 | `uninstall` | 서비스·원본·데이터 제거 | 잔여 없음(PawnIO 유지) | 성공 |

R8 상세. **재부팅이 두 번 일어났다**(15:31:51 종료 → 15:32:04 부팅, 15:33:03 종료 → 15:33:26 부팅. 두 번째 재시작의 원인은
확인하지 않았다). 부팅 세션 3개 모두 검사했다:

- 첫 종료: 마지막 샘플 15:31:46.59, `agent_stopped` 15:31:46.97, OS 종료 기록(Kernel-General 13) 15:31:51.39 → 종료 신호 후 DB를 닫고 끝남.
  체크포인트부터 종료까지 387샘플, 순번 구멍 0, 최대 간격 1.02초.
- 중간 부팅(약 1분): 서비스 시작 → `boot_session_started`(reboot) → 24샘플(순번 0~23) → `agent_stopped` 15:33:01.47 → OS 종료 15:33:03.92.
- 마지막 부팅: 서비스 시작이 부팅 18.8초 뒤, 첫 샘플 23.1초 뒤, 첫 로그인(Winlogon 7001) 27.6초 뒤. 순번 0부터 11,559샘플(약 3시간),
  구멍 0·최대 간격 1.03초. 서버 재개 후 대기 배치 전부 전송(`upload_recovered`).
- 세 세션 모두 다음 시작 때 `unclean_shutdown_detected` 없음, 복원할 미동결 샘플 0 → 두 번의 OS 종료 모두 정상 종료 경로 완료.
- 서버: 4,335배치·13,003샘플 = 세션별 1,414 + 24 + 11,565. 중복 0, 대기·격리 0, 재전송 응답 재생 0.

## 6. 미검증·차단

| 항목 | 상태 | 다음 |
|---|---|---|
| 실제 사용자 로그아웃 | **미검증**. PC-A는 학교 관리 노트북이라 임의 로그아웃 불가, 자동 로그인이라 로그인 전 구간도 짧다 | 대체 근거: (1) R8 로그인 전 수집 — 서비스가 첫 로그인보다 8.8초 먼저 시작해 로그인 전 5샘플을 구멍 없이 수집, (2) 세션 0·세션 변경 제어 미수신·사용자 프로필 미사용(2절), (3) R5. 로그아웃 가능한 PC에서 `resume --expect logoff`로 실측(A, 다중 PC 검증 때) |
| S2-03 DPAPI 자격 증명(`device.dpapi`) 읽기·등록 IPC 호스팅 | 미구현. 설치 스크립트가 복사한 `state\device.token`(SYSTEM·Administrators 전용) 접근만 검증 | 서비스(C#)가 S2-03 형식을 읽는 방식·등록 파이프 호스팅을 B와 합의 후 후속 |
| 2번째 PC·개발 도구 없는 PC | 미검증(PC-A 1대) | 다중 PC 검증 |
| S1-03 FastAPI/PostgreSQL 경로 | 미검증(참조 API로만) | S2-04와 함께 |
| 다른 팀원 리뷰 | 1차 리뷰(P2·P3) 반영, 재리뷰 전 | PR #31 |
| 센서 구성 전 실패의 1002 기록(P3) | 코드 경로만 확인. 워밍업·탐색 중 예외를 실기에서 일으킬 방법이 없어 실측하지 않았다(주기 예외는 수집기가 잡아 `collector_cycle_failed`로 남김) | — |

## 7. 남은 제한·합의 필요

- **OS 종료 중에는 이벤트 1001(정상 종료)이 기록되지 않았다.** SCM 중지에서는 기록된다(R2·R3·R7). OS 종료 시 정상 종료 여부는
  `events.jsonl`의 `agent_stopped`, `agent.running` 삭제(다음 시작의 비정상 표시 없음), 복원할 미동결 샘플 0으로 판단한다.
- 마무리 전송 2초 안에 못 보낸 배치는 다음 시작까지 기다린다(데이터는 유지). OS 종료 대기 한도가 더 짧은 PC는 확인하지 않았다.
- 복구 지연은 실패 횟수에 따라 60초·60초·5분이고 하루 동안 실패가 없어야 초기화된다. 반복 실패 시 최대 5분 수집 공백이 생긴다.
- 서비스 재시작마다 S1-02b 순번 예약(600) 때문에 순번 구멍이 생긴다(재사용은 없음, 계약상 허용).
- 최소 권한 목록은 PC-A(Intel CPU·NVIDIA GPU, PawnIO 2.2.0)에서만 확인했다. 다른 하드웨어에서 LHM이 추가 권한을 요구하면
  `service_access_check`의 `driver_unavailable`로 드러난다.
- 이벤트 로그 메시지에는 상태 폴더 경로(`C:\ProgramData\PcTelemetry\state`)가 들어간다. 사용자 이름은 없다.
- 계정은 여전히 LocalSystem이다(S1-02a: LocalService로는 CPU 센서 불가). 가상 서비스 계정 + PawnIO 장치 ACL 조정은 PawnIO 기본
  설정을 바꾸는 일이라 검토하지 않았다.
