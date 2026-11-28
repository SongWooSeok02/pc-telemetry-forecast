import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { join } from 'node:path'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'
import { emptyForecast } from '../src/forecastData.ts'

// Render the actual TSX component, using the existing TypeScript dependency.
const root = fileURLToPath(new URL('..', import.meta.url))
const parent = join(root, 'node_modules', '.tmp')
mkdirSync(parent, { recursive: true })
const output = mkdtempSync(join(parent, 'forecast-render-'))
for (const [file, extension] of [['queryErrors', 'ts'], ['forecastData', 'ts'], ['ForecastChart', 'tsx']]) {
  const source = readFileSync(join(root, 'src', `${file}.${extension}`), 'utf8')
  const result = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } })
  writeFileSync(join(output, `${file}.mjs`), result.outputText.replace(/(['"])\.\/(forecastData|queryErrors)(?:\.ts)?\1/g, "'./$2.mjs'"))
}
const Chart = (await import(pathToFileURL(join(output, 'ForecastChart.mjs')).href)).default
const now = Date.parse('2026-11-19T12:00:00Z')
const scope = { machineId: 'pc-a', sensorId: 'cpu', unit: 'Cel', hours: 1 }
const props = { data: null, busy: false, error: null, unsupported: false, label: 'CPU 온도', unit: 'Cel', hours: 1, onHoursChange: () => {}, now }

test('GPU unsupported state is explicit and has no plotted data', () => {
  const html = renderToStaticMarkup(React.createElement(Chart, { ...props, label: 'GPU 온도', unsupported: true }))
  assert.match(html, /미지원/)
  assert.doesNotMatch(html, /<svg/)
})
test('render distinguishes actual, learned and baseline series and includes provenance', () => {
  const data = emptyForecast(scope)
  data.predictions = [
    { target_at: '2026-11-19T11:59:40Z', generated_at: '2026-11-19T11:58:40Z', unit: 'Cel', value: 50, model_version: 'temperature-v2', status: 'ok', reason: null },
    { target_at: '2026-11-19T12:00:20Z', generated_at: '2026-11-19T11:59:20Z', unit: 'Cel', value: 51, model_version: 'builtin:persistence:v1', status: 'fallback', reason: 'no_active_model' },
  ]
  data.actuals = [{ observedAt: '2026-11-19T11:59:40Z', receivedAt: '2026-11-19T11:59:42Z', value: 49, quality: 'ok' }]
  const html = renderToStaticMarkup(React.createElement(Chart, { ...props, data }))
  for (const text of ['forecast-actual', 'forecast-model', 'forecast-baseline', 'temperature-v2', 'builtin:persistence:v1', 'no_active_model', '생성', '대상', '보고된 실제값', '실제값 대기']) assert.ok(html.includes(text), text)
})
test('server error retains existing chart and marks busy accessibly', () => {
  const html = renderToStaticMarkup(React.createElement(Chart, { ...props, busy: true, error: 'HTTP 503' }))
  assert.match(html, /aria-busy="true"/)
  assert.match(html, /HTTP 503/)
})
