'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
require('./webcheck-client.js');

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
  const unavailable = OriginalityWebCheck.create('https://service.example', {fetch: async () => ({ok:false,status:503})});
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
