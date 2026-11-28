import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)
const toolsPkgUrl = new URL('../../local-evidence/s4-06-tools/package.json', import.meta.url)
const distUrl = new URL('../dist/', import.meta.url)
const observeScriptPath = fileURLToPath(new URL('./observeRefresh.mjs', import.meta.url))

function listenLoopback(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve(server.address().port))
  })
}

test('observeRefresh rejects non-loopback endpoint or invalid intervals before attaching', async () => {
  await assert.rejects(
    execFileAsync(process.execPath, [observeScriptPath, 'http://example.com:9222', 'https://test.example', 'out.jsonl', '10', '5']),
    /Usage: node observeRefresh\.mjs/,
  )
  await assert.rejects(
    execFileAsync(process.execPath, [observeScriptPath, 'http://127.0.0.1:9222', 'https://test.example/path', 'out.jsonl', '10', '5']),
    /Usage: node observeRefresh\.mjs/,
  )
})

test('observeRefresh start record logs non-sensitive bundleSha256 matching built dist bundle', async (t) => {
  if (!existsSync(toolsPkgUrl) || !existsSync(new URL('assets/', distUrl))) {
    t.skip('playwright-core tools or built dist not present in this environment')
    return
  }

  const assets = await readdir(new URL('assets/', distUrl))
  const expectedBundle = assets.find((name) => /^index-.*\.js$/.test(name))
  assert.ok(expectedBundle, 'expected built index-*.js asset in dashboard/dist/assets')
  const expectedSha256 = createHash('sha256')
    .update(await readFile(new URL('assets/' + expectedBundle, distUrl)))
    .digest('hex')

  const server = createServer(async (req, res) => {
    const pathname = new URL(req.url ?? '/', 'http://127.0.0.1').pathname
    if (pathname === '/v1/user/session') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ authenticated: true, user_id: '00000000-0000-4000-8000-000000000099', expires_at: new Date(Date.now() + 3_600_000).toISOString() }))
      return
    }
    const target = pathname === '/' ? new URL('index.html', distUrl) : new URL('.' + pathname, distUrl)
    try {
      const body = await readFile(target)
      const type = pathname.endsWith('.js') ? 'text/javascript; charset=utf-8'
        : pathname.endsWith('.css') ? 'text/css; charset=utf-8'
        : 'text/html; charset=utf-8'
      res.writeHead(200, { 'Content-Type': type })
      res.end(body)
    } catch {
      res.writeHead(404).end()
    }
  })

  const appPort = await listenLoopback(server)
  const origin = `http://127.0.0.1:${appPort}`

  const cdpReservation = createServer()
  const cdpPort = await listenLoopback(cdpReservation)
  await new Promise((resolve) => cdpReservation.close(resolve))

  const tempDir = await mkdtemp(join(tmpdir(), 'pct-observe-test-'))
  const logPath = join(tempDir, 'observe.jsonl')

  const require = createRequire(toolsPkgUrl)
  const { chromium } = require('playwright-core')
  const browser = await chromium.launch({
    channel: 'msedge',
    headless: true,
    args: [`--remote-debugging-port=${cdpPort}`, '--remote-debugging-address=127.0.0.1'],
  })

  try {
    const context = await browser.newContext()
    const page = await context.newPage()
    await page.goto(origin, { waitUntil: 'domcontentloaded' })

    await execFileAsync(process.execPath, [
      observeScriptPath,
      `http://127.0.0.1:${cdpPort}`,
      origin,
      logPath,
      '1',
      '1',
    ])

    const rawLog = await readFile(logPath, 'utf8')
    assert.equal(rawLog.includes(origin), false, 'log must not contain test origin')
    assert.equal(rawLog.includes(String(cdpPort)), false, 'log must not contain CDP endpoint port')

    const records = rawLog.trim().split('\n').map((line) => JSON.parse(line))
    assert.ok(records.length >= 3, 'expected start, sample, and end records')
    const [startRecord] = records
    assert.equal(startRecord.type, 'start')
    assert.equal(startRecord.bundle, expectedBundle)
    assert.equal(startRecord.bundleSha256, expectedSha256)
    assert.equal(startRecord.durationSeconds, 1)
    assert.equal(startRecord.sampleSeconds, 1)
    assert.equal(typeof startRecord.runtime, 'string')
    assert.equal(records.at(-1).type, 'end')
  } finally {
    await browser.close().catch(() => {})
    server.close()
    await rm(tempDir, { recursive: true, force: true })
  }
})
