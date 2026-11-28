import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'
import { emptyForecast, readForecastUpdate, type ForecastScope } from '../src/forecastData.ts'

const dashboardRoot = fileURLToPath(new URL('..', import.meta.url))
const tmpParent = join(dashboardRoot, 'node_modules', '.tmp')
mkdirSync(tmpParent, { recursive: true })
const output = mkdtempSync(join(tmpParent, 'forecast-http-integration-'))
for (const [file, extension] of [['queryErrors', 'ts'], ['forecastData', 'ts'], ['ForecastChart', 'tsx']]) {
  const source = readFileSync(join(dashboardRoot, 'src', `${file}.${extension}`), 'utf8')
  const result = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } })
  writeFileSync(join(output, `${file}.mjs`), result.outputText.replace(/(['"])\.\/(forecastData|queryErrors)(?:\.ts)?\1/g, "'./$2.mjs'"))
}
const Chart = (await import(pathToFileURL(join(output, 'ForecastChart.mjs')).href)).default

async function withServer(
  handler: (req: IncomingMessage, res: ServerResponse) => void,
  run: (baseUrl: string) => Promise<void>,
) {
  const server = createServer(handler)
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  assert.ok(address && typeof address === 'object')
  try {
    await run(`http://127.0.0.1:${address.port}`)
  } finally {
    await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
  }
}

test('ForecastChart parses and renders contract-fixture HTTP prediction and raw history responses', async () => {
  const now = Date.parse('2026-11-19T12:00:00Z')
  const scope: ForecastScope = { machineId: '11111111-1111-1111-1111-111111111111', sensorId: 'cpu/package/temp', unit: 'Cel', hours: 1 }
  const seenPaths: string[] = []

  await withServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    seenPaths.push(url.pathname)
    res.setHeader('Content-Type', 'application/json')
    res.setHeader('Cache-Control', 'no-store')
    if (url.pathname === `/v1/machines/${scope.machineId}/predictions`) {
      assert.equal(url.searchParams.get('sensor_id'), scope.sensorId)
      res.end(JSON.stringify({
        items: [
          {
            target_at: '2026-11-19T11:59:40+00:00',
            generated_at: '2026-11-19T11:58:40+00:00',
            value: 50.5,
            unit: 'Cel',
            model_version: 'temperature-v2',
            status: 'ok',
            reason: null,
          },
          {
            target_at: '2026-11-19T12:00:20+00:00',
            generated_at: '2026-11-19T11:59:20+00:00',
            value: 51.0,
            unit: 'Cel',
            model_version: 'builtin:persistence:v1',
            status: 'fallback',
            reason: 'no_active_model',
          },
          {
            target_at: '2026-11-19T12:00:40+00:00',
            generated_at: '2026-11-19T11:59:40+00:00',
            value: null,
            unit: 'Cel',
            model_version: 'builtin:none',
            status: 'unavailable',
            reason: 'stale_or_missing_input',
          },
        ],
        truncated: false,
      }))
      return
    }
    if (url.pathname === `/v1/dashboard/machines/${scope.machineId}/sensors/${encodeURIComponent(scope.sensorId)}/history`) {
      assert.equal(url.searchParams.get('resolution'), 'raw')
      res.end(JSON.stringify({
        machineId: scope.machineId,
        sensorId: scope.sensorId,
        unit: scope.unit,
        resolution: 'raw',
        nextCursor: 'cursor-http-1',
        aggregationUpdatedAt: '2026-11-19T12:00:00+00:00',
        aggregationPending: false,
        points: [
          { lastObservedAt: '2026-11-19T11:59:40+00:00', receivedAt: '2026-11-19T11:59:41+00:00', value: 49.5, quality: 'ok' },
        ],
      }))
      return
    }
    res.statusCode = 404
    res.end(JSON.stringify({ error: { code: 'NOT_FOUND' } }))
  }, async (baseUrl) => {
    const data = await readForecastUpdate(fetch, baseUrl, scope, emptyForecast(scope), new AbortController().signal, now)
    assert.equal(seenPaths.length, 2)
    assert.equal(data.predictions.length, 3)
    assert.equal(data.predictions[0].unit, 'Cel')
    assert.equal(data.actuals.length, 1)

    const html = renderToStaticMarkup(React.createElement(Chart, {
      data,
      busy: false,
      error: null,
      unsupported: false,
      label: 'CPU 온도',
      unit: scope.unit,
      hours: scope.hours,
      onHoursChange: () => {},
      now,
    }))
    for (const expected of [
      '<svg',
      'forecast-actual',
      'forecast-model',
      'forecast-baseline',
      'temperature-v2',
      'builtin:persistence:v1',
      'no_active_model',
      'stale_or_missing_input',
      '50.5 Cel',
      '49.5 Cel',
    ]) {
      assert.ok(html.includes(expected), `expected rendered chart to include ${expected}`)
    }
  })
})

test('mixed contract-fixture HTTP page omits legacy null-unit rows while rendering valid rows', async () => {
  const now = Date.parse('2026-11-19T12:00:00Z')
  const scope: ForecastScope = { machineId: '11111111-1111-1111-1111-111111111111', sensorId: 'cpu/package/temp', unit: 'Cel', hours: 1 }

  await withServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    res.setHeader('Content-Type', 'application/json')
    if (url.pathname === `/v1/machines/${scope.machineId}/predictions`) {
      res.end(JSON.stringify({
        items: [
          {
            target_at: '2026-11-19T11:59:30+00:00',
            generated_at: '2026-11-19T11:58:30+00:00',
            value: 49.0,
            unit: null,
            model_version: 'legacy-v0',
            status: 'ok',
            reason: null,
          },
          {
            target_at: '2026-11-19T11:59:40+00:00',
            generated_at: '2026-11-19T11:58:40+00:00',
            value: 50.5,
            unit: 'Cel',
            model_version: 'temperature-v2',
            status: 'ok',
            reason: null,
          },
        ],
        truncated: false,
      }))
      return
    }
    res.end(JSON.stringify({
      machineId: scope.machineId,
      sensorId: scope.sensorId,
      unit: scope.unit,
      resolution: 'raw',
      nextCursor: 'cursor-http-mixed',
      aggregationUpdatedAt: '2026-11-19T12:00:00+00:00',
      aggregationPending: false,
      points: [
        { lastObservedAt: '2026-11-19T11:59:40+00:00', receivedAt: '2026-11-19T11:59:41+00:00', value: 49.5, quality: 'ok' },
      ],
    }))
  }, async (baseUrl) => {
    const data = await readForecastUpdate(fetch, baseUrl, scope, emptyForecast(scope), new AbortController().signal, now)
    assert.equal(data.predictions.length, 1)
    assert.equal(data.predictions[0].model_version, 'temperature-v2')
    const html = renderToStaticMarkup(React.createElement(Chart, {
      data,
      busy: false,
      error: null,
      unsupported: false,
      label: 'CPU 온도',
      unit: scope.unit,
      hours: scope.hours,
      onHoursChange: () => {},
      now,
    }))
    assert.ok(html.includes('temperature-v2'))
    assert.ok(html.includes('50.5 Cel'))
    assert.ok(!html.includes('legacy-v0'))
  })
})

test('only-legacy contract-fixture HTTP page renders empty state without guessing unit', async () => {
  const now = Date.parse('2026-11-19T12:00:00Z')
  const scope: ForecastScope = { machineId: '11111111-1111-1111-1111-111111111111', sensorId: 'cpu/package/temp', unit: 'Cel', hours: 1 }

  await withServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    res.setHeader('Content-Type', 'application/json')
    if (url.pathname === `/v1/machines/${scope.machineId}/predictions`) {
      res.end(JSON.stringify({
        items: [
          {
            target_at: '2026-11-19T11:59:40+00:00',
            generated_at: '2026-11-19T11:58:40+00:00',
            value: 49.0,
            unit: null,
            model_version: 'legacy-v0',
            status: 'ok',
            reason: null,
          },
        ],
        truncated: false,
      }))
      return
    }
    res.end(JSON.stringify({
      machineId: scope.machineId,
      sensorId: scope.sensorId,
      unit: scope.unit,
      resolution: 'raw',
      nextCursor: 'cursor-http-legacy',
      aggregationUpdatedAt: null,
      aggregationPending: false,
      points: [],
    }))
  }, async (baseUrl) => {
    const data = await readForecastUpdate(fetch, baseUrl, scope, emptyForecast(scope), new AbortController().signal, now)
    assert.deepEqual(data.predictions, [])
    const html = renderToStaticMarkup(React.createElement(Chart, {
      data,
      busy: false,
      error: null,
      unsupported: false,
      label: 'CPU 온도',
      unit: scope.unit,
      hours: scope.hours,
      onHoursChange: () => {},
      now,
    }))
    assert.match(html, /비교할 예측 데이터가 없습니다/)
    assert.doesNotMatch(html, /<svg/)
    assert.ok(!html.includes('legacy-v0'))
  })
})
