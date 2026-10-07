'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
require('./webcheck-client.js');

function response(status, value, contentType = 'application/json') {
  return {ok: status >= 200 && status < 300, status, headers: {get: () => contentType},
    text: async () => typeof value === 'string' ? value : JSON.stringify(value)};
}

function clock() {
  let now = 0, nextID = 1;
  const pending = new Map();
  const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  return {
    setTimeout(callback, delay) { const id = nextID++; pending.set(id, {callback, at: now + delay}); return id; },
    clearTimeout(id) { pending.delete(id); },
    get pending() { return pending.size; },
    flush,
    async advance(duration) {
      const end = now + duration;
      await flush();
      while (true) {
        const first = [...pending.entries()].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!first || first[1].at > end) break;
        now = first[1].at; pending.delete(first[0]); first[1].callback(); await flush();
      }
      now = end; await flush();
    },
  };
}

test('web check transmits the post allowlist only after explicit consent', async () => {
  let request;
  const client = OriginalityWebCheck.create('https://search.example/service/', {accessToken: 'service-access',
    fetch: async (url, options) => {
      request = {url, options};
      return {ok: true, text: async () => JSON.stringify({schema_version: 1, coverage: {}, posts: [{id:'1', status:'no_match'}]})};
    }});
  await client.check([{id:'1',text:'A public text fragment',url:'https://x.com/i/status/1',created_at:'2026-10-07',
    media:['never send'],account:'never send',text_truncated:true,original_chars:50000}]);
  assert.equal(request.url, 'https://search.example/service/api/webcheck');
  assert.deepEqual(JSON.parse(request.options.body), {consent:true,posts:[{id:'1',text:'A public text fragment',url:'https://x.com/i/status/1',created_at:'2026-10-07'}]});
  assert.equal(request.options.credentials, 'omit');
  assert.equal(request.options.redirect, 'error');
  assert.equal(request.options.headers.Authorization, 'Bearer service-access');
});

test('service URLs reject embedded credentials and non-HTTPS remote hosts', () => {
  for (const url of ['https://secret@example.com','https://example.com?token=secret','http://remote.example','file:///tmp/key']) {
    assert.throws(() => OriginalityWebCheck.create(url));
  }
  assert.equal(OriginalityWebCheck.endpoint('http://127.0.0.1:8790/'), 'http://127.0.0.1:8790');
});

test('service failure and mismatched evidence never become a successful no-match result', async () => {
  const unavailable = OriginalityWebCheck.create('https://service.example', {fetch: async () => response(503, {code:'provider_not_configured'})});
  await assert.rejects(unavailable.check([{id:'1',text:'sample'}]), /尚未配置/);
  const wrong = OriginalityWebCheck.create('https://service.example', {fetch:async () => ({ok:true,text:async () => JSON.stringify({schema_version:1,coverage:{},posts:[{id:'other'}]})})});
  await assert.rejects(wrong.check([{id:'1',text:'sample'}]), /不一致/);
});

test('cancelled web request uses AbortSignal and preserves an explicit cancellation result', async () => {
  const controller = new AbortController();
  const client = OriginalityWebCheck.create('https://service.example', {fetch:async (_url, options) => {
    controller.abort();
    assert.equal(options.signal.aborted, true);
    throw new DOMException('aborted','AbortError');
  }});
  await assert.rejects(client.status(controller.signal), /已取消/);
});

test('status wakes through HTML and proxy 502/503 with one bounded GET deadline', async () => {
  const timers = clock(), calls = [], progress = [];
  const sequence = [response(200, '<!doctype html><html>Loading service</html>', 'text/html'),
    response(502, 'Bad gateway', 'text/plain'), response(503, '<html>Starting</html>', 'text/html'), response(200, {ready:true})];
  const client = OriginalityWebCheck.create('https://service.example', {timers,
    onStatusWait: value => progress.push(value), fetch: async (_url, options) => { calls.push(options); return sequence.shift(); }});
  const result = client.status();
  await timers.flush();
  assert.equal(calls.length, 1);
  assert.equal(timers.pending, 2);
  await timers.advance(6000);
  assert.equal((await result).ready, true);
  assert.equal(calls.length, 4);
  assert.ok(calls.every(call => call.method === 'GET' && !call.body && call.credentials === 'omit' && call.redirect === 'error'));
  assert.equal(progress[0].phase, 'connecting');
  assert.ok(progress.some(value => value.phase === 'waking' && value.max_wait_ms === 120000));
  assert.equal(timers.pending, 0);
});

test('status retries temporary network TypeError caused by missing cold-start CORS', async () => {
  const timers = clock();
  let calls = 0;
  const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: async () => {
    calls += 1;
    if (calls === 1) throw new TypeError('Failed to fetch');
    return response(200, {ready:true});
  }});
  const result = client.status();
  await timers.advance(2000);
  assert.equal((await result).ready, true);
  assert.equal(calls, 2);
  assert.equal(timers.pending, 0);
});

test('invalid non-HTML status data is final and valid JSON outranks a mistaken HTML header', async () => {
  for (const [value, contentType, valid] of [['not API JSON', 'text/plain', false], [{ready:false}, 'text/html', true]]) {
    const timers = clock();
    let calls = 0;
    const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: async () => { calls += 1; return response(200, value, contentType); }});
    if (valid) assert.equal((await client.status()).ready, false);
    else await assert.rejects(client.status(), /无效数据/);
    assert.equal(calls, 1);
    assert.equal(timers.pending, 0);
  }
});

test('authentication and JSON provider failures stop immediately without cold-start retries', async () => {
  for (const [status, body, expected] of [
    [401, '<html>Access required</html>', /访问码/],
    [403, '<html>Forbidden</html>', /未允许/],
    [503, {code:'provider_not_configured',error:'Missing API key'}, /尚未配置/],
    [503, {code:'internal_error'}, /暂时不可用/],
  ]) {
    const timers = clock();
    let calls = 0;
    const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: async () => { calls += 1; return response(status, body); }});
    await assert.rejects(client.status(), expected);
    await timers.advance(120000);
    assert.equal(calls, 1);
    assert.equal(timers.pending, 0);
  }
  const timers = clock();
  let calls = 0;
  const notConfigured = OriginalityWebCheck.create('https://service.example', {timers, fetch: async () => { calls += 1; return response(200, {ready:false}); }});
  assert.equal((await notConfigured.status()).ready, false);
  assert.equal(calls, 1);
  assert.equal(timers.pending, 0);
});

test('persistent loading ends after 120 seconds and clears deadline and retry timers', async () => {
  const timers = clock();
  let calls = 0;
  const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: async () => {
    calls += 1; return response(200, '<html>Still starting</html>', 'text/html');
  }});
  const rejected = assert.rejects(client.status(), /最多 2 分钟/);
  await timers.advance(120000);
  await rejected;
  assert.equal(calls, 60);
  assert.equal(timers.pending, 0);
  await timers.advance(120000);
  assert.equal(calls, 60);
});

test('a stalled status request is bounded even when an injected fetch never resolves', async () => {
  const timers = clock();
  let calls = 0;
  const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: () => { calls += 1; return new Promise(() => {}); }});
  const rejected = assert.rejects(client.status(), /最多 2 分钟/);
  await timers.advance(120000);
  await rejected;
  assert.equal(calls, 1);
  assert.equal(timers.pending, 0);
});

test('cancellation interrupts retry sleep immediately and leaves no later requests', async () => {
  const timers = clock(), controller = new AbortController();
  let calls = 0;
  const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: async () => {
    calls += 1; return response(503, '<html>Starting</html>', 'text/html');
  }});
  const rejected = assert.rejects(client.status(controller.signal), /已取消/);
  await timers.flush();
  assert.equal(timers.pending, 2);
  controller.abort();
  await rejected;
  assert.equal(timers.pending, 0);
  await timers.advance(120000);
  assert.equal(calls, 1);
});

test('POST never retries proxy errors, HTML or a lost network response', async () => {
  for (const outcome of [response(502, '<html>Bad gateway</html>', 'text/html'),
    response(503, '<html>Starting</html>', 'text/html'), response(200, '<html>Loading</html>', 'text/html'), new TypeError('Lost response')]) {
    const timers = clock();
    let calls = 0;
    const client = OriginalityWebCheck.create('https://service.example', {timers, fetch: async (_url, options) => {
      calls += 1; assert.equal(options.method, 'POST');
      if (outcome instanceof Error) throw outcome;
      return outcome;
    }});
    await assert.rejects(client.check([{id:'1',text:'public sample'}]));
    await timers.advance(300000);
    assert.equal(calls, 1);
    assert.equal(timers.pending, 0);
  }
});
