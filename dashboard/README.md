# Dashboard (S1-04)

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

The refresh selector is available in the header, defaults to five seconds, persists per
browser/WebView client, and supports manual refresh. The API request is aborted when its
machine selection or refresh mode changes.
