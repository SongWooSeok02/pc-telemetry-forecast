# 공통 대시보드 (S1-04 / S1-05)

React/TypeScript shared dashboard shell for the browser and the future WebView2 host.
It presents the current sensor readings and their observation times, and distinguishes
unsupported, missing, and failed sensor values instead of showing a fabricated zero.

## Local development

```sh
cd dashboard
npm install
npm run dev
```

The dashboard expects `GET /v1/dashboard/machines/{machine_id}/current` to return:

```json
{
  "machineId": "registered-machine-id",
  "machineName": "Development PC",
  "receivedAt": "2026-11-05T01:00:00Z",
  "readings": [
    { "label": "CPU 온도", "value": 52.5, "unit": "°C", "quality": "ok", "observedAt": "2026-11-05T00:59:58Z" },
    { "label": "GPU 전력", "value": null, "unit": "W", "quality": "unsupported" }
  ]
}
```

Set `VITE_API_BASE_URL` when the API is on a different origin. The S1-03 server currently
implements ingestion and partition health only; it has no dashboard read route or user
session/access-control model. Until those are agreed and implemented, this screen shows
empty sensor states and does not invent or expose telemetry. The read path is a proposed
frontend interface, not an approved API contract.

## 갱신 제어 (S1-05)

상단 제어는 기본 5초를 사용하며 3·5·10·30초와 수동 모드를 제공합니다. 최초 설정 팝업이나 저장 버튼은 없습니다. 선택값은 현재 브라우저/WebView의 `localStorage`에 저장되어 같은 클라이언트에서 다시 열 때 복원됩니다. 저장소에 접근할 수 없거나 값이 잘못됐으면 기본 5초로 시작합니다. 이전 구현의 `0` 저장값도 수동 모드로 읽습니다.

주기를 선택하면 이전 예약과 진행 중 요청을 정리하고 새 설정으로 즉시 조회합니다. 자동 모드에서는 요청이 끝난 뒤 선택한 간격을 예약하며, 수동 모드에서는 예약하지 않습니다. `지금 갱신`은 모드와 관계없이 조회를 즉시 요청합니다. 주기 변경은 페이지를 다시 불러오지 않으며 선택 PC와 화면 위치 등 대시보드 상태를 유지합니다. 화면 주기는 센서 수집·전송·예측 주기와 분리되어 있습니다.

공통 스케줄러의 가시성 감지, 호스트 상태 연동, 요청 경합의 통합 관리는 S2-06 범위입니다.

## Historical chart (S2-05)

The dashboard offers sensor and time-range selection, resolution-aware charting, viewport
zoom, observation/receipt/aggregation timestamps, quality gaps, and cursor-based updates.
It calls the proposed `GET /v1/dashboard/machines/{machine_id}/sensors/{sensor_id}/history`
route. The current backend does not yet provide dashboard read authorization, history
queries, or S3-03 aggregates; until those dependencies land, requests show an error and
retain any previous successful chart data. On `HTTP 422` `INVALID_HISTORY_CURSOR`, the
client clears the cursor and re-queries the full `[from, to)` window for the same PC,
sensor, and range (`npm run test:history`). The full response and cursor contract is in
[`docs/s2-05-history-read-contract.md`](../docs/s2-05-history-read-contract.md).
