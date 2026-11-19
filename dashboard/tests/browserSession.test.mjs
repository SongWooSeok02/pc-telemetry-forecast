import assert from 'node:assert/strict'
import { test } from 'node:test'
import { BrowserSession, checkBrowserSession, logoutBrowserSession } from '../src/browserSession.ts'

const scope = 'a'.repeat(64)

test('expiry response during PC change stops the entire session', async (t) => {
  const responses = []
  const fetch = t.mock.method(globalThis, 'fetch', (_path, options) => new Promise((resolve) => {
    responses.push({ resolve, signal: options.signal })
  }))
  const ended = []
  const session = new BrowserSession(scope, (reason) => ended.push(reason))
  const selection = new AbortController()
  const old = session.request('/old-PC', selection.signal)
  const next = session.request('/new-PC')
  responses[0].resolve(Response.json({ error: { code: 'USER_SESSION_EXPIRED' } }, { status: 401 }))
  selection.abort()
  await assert.rejects(old, { name: 'AbortError' })
  assert.equal(responses[1].signal.aborted, true)
  responses[1].resolve(Response.json({ private: 'never returned' }))
  await assert.rejects(next, { name: 'AbortError' })
  await assert.rejects(session.request('/history'), { name: 'AbortError' })
  assert.equal(fetch.mock.callCount(), 2)
  assert.deepEqual(ended, ['expired'])
})

test('logout discards old responses without ending a different session', async (t) => {
  let release
  t.mock.method(globalThis, 'fetch', (path, options) => {
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.cache, 'no-store')
    assert.equal(options.redirect, 'error')
    if (path === '/v1/user/session/logout') {
      assert.equal(options.headers['X-Pct-Session'], scope)
      assert.equal(options.method, 'POST')
      return new Response(null, { status: 204 })
    }
    return new Promise((resolve) => { release = resolve })
  })
  const old = new BrowserSession(scope, () => assert.fail('retired session fired'))
  const pending = old.request('/old-session')
  old.stop()
  await logoutBrowserSession(scope)
  release(new Response(null, { status: 401 }))
  await assert.rejects(pending, { name: 'AbortError' })
})

test('401 requests stop; PC denial stays scoped and is not retried by the boundary', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', (path, options) => {
    if (path === '/v1/user/session') return Response.json({ scope, expires_in_ms: 1000 })
    assert.equal(options.headers['X-Pct-Session'], scope)
    return new Response(null, { status: path === '/forbidden' ? 404 : 401 })
  })
  const checked = await checkBrowserSession(new AbortController().signal)
  assert.equal(checked.scope, scope)
  const reasons = []
  const session = new BrowserSession(scope, (reason) => reasons.push(reason))
  assert.equal((await session.request('/forbidden')).status, 404)
  assert.deepEqual(reasons, [])
  await assert.rejects(session.request('/revoked'), { name: 'AbortError' })
  assert.deepEqual(reasons, ['signed-out'])
  assert.equal(fetch.mock.callCount(), 3)
})
