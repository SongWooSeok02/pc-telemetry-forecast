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

The refresh selector is available in the header, defaults to five seconds, and offers
three, five, ten, and thirty seconds plus manual mode. Its value is stored per
browser/WebView client. Invalid or unavailable storage falls back to five seconds, and
the legacy saved value `0` restores manual mode. Current and history reads share one
monotonic scheduler, one timer, and one in-flight request. Changing the query identity
aborts its obsolete request; changing the refresh mode updates scheduling without starting
duplicate work.

## Historical chart (S2-05)

The dashboard offers sensor and time-range selection, resolution-aware charting, viewport
zoom, observation/receipt/aggregation timestamps, quality gaps, and cursor-based updates.
It calls the proposed `GET /v1/dashboard/machines/{machine_id}/sensors/{sensor_id}/history`
route. The current backend does not yet provide dashboard read authorization, history
queries, or S3-03 aggregates; until those dependencies land, requests show an error and
retain any previous successful chart data. The full response and cursor contract is in
[`docs/s2-05-history-read-contract.md`](../docs/s2-05-history-read-contract.md).

## Shared query scheduler (S2-06)

Automatic reads pause while the browser document is hidden or the WebView host reports
that it is minimized. On restore, automatic mode refreshes immediately; manual mode waits
for the user. The WebView2 host message shape and deterministic fake-clock checks are
documented in [`docs/s2-06-query-scheduler.md`](../docs/s2-06-query-scheduler.md). This
repository does not contain the .NET host project yet, so host-side message sending still
needs integration.
