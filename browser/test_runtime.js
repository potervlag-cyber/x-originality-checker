'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const runtimeSource = fs.readFileSync(path.join(__dirname, 'runtime.js'), 'utf8');
const workerSource = fs.readFileSync(path.join(__dirname, 'python-worker.js'), 'utf8');

function fixture() {
  const workers = [];
  const timers = new Map();
  let nextTimer = 1;
  class FakeWorker {
    constructor(url, options) {
      this.url = url; this.options = options; this.listeners = new Map(); this.sent = []; this.terminated = false;
      workers.push(this);
    }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    postMessage(message) { this.sent.push(message); }
    terminate() { this.terminated = true; }
    emit(type, data) { this.listeners.get(type)?.(type === 'message' ? {data} : {preventDefault() {}}); }
  }
  const window = {};
  vm.runInNewContext(runtimeSource, {
    window, document: {currentScript: {src: 'https://example.test/project/runtime.js'}, baseURI: 'https://example.test/project/'},
    URL, Worker: FakeWorker, setTimeout(callback, duration) { const id = nextTimer++; timers.set(id, {callback, duration}); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  return {runtime: window.OriginalityRuntime, workers, timers};
}

async function initialized() {
  const result = fixture();
  const promise = result.runtime.init();
  result.workers[0].emit('message', {type: 'ready', version: '0.1.0'});
  await promise;
  return result;
}

test('init shares one worker and resolves version with project-relative URL', async () => {
  const {runtime, workers, timers} = fixture();
  const progress = [];
  const first = runtime.init(message => progress.push(message));
  const second = runtime.init();
  assert.equal(first, second);
  assert.equal(workers.length, 1);
  assert.equal(workers[0].url, 'https://example.test/project/python-worker.js');
  workers[0].emit('message', {type: 'progress', message: '准备中'});
  workers[0].emit('message', {type: 'ready', version: '0.1.0'});
  assert.equal((await first).version, '0.1.0');
  assert.deepEqual(progress, ['准备中']);
  assert.equal(runtime.ready, true);
  assert.equal(timers.size, 0);
});

test('independent request ids deliver the corresponding JSON response', async () => {
  const {runtime, workers, timers} = await initialized();
  const first = runtime.request('/api/analyze', {project: {posts: []}});
  const second = runtime.request('/api/report', {format: 'html'});
  const [a, b] = workers[0].sent;
  assert.notEqual(a.id, b.id);
  assert.equal(JSON.parse(a.request_json).path, '/api/analyze');
  assert.equal(Object.hasOwn(JSON.parse(a.request_json), 'token'), false);
  workers[0].emit('message', {type: 'result', id: b.id, result_json: JSON.stringify({ok: true, result: {content: '<html>'}})});
  workers[0].emit('message', {type: 'result', id: a.id, result_json: JSON.stringify({ok: true, result: {analysis: {total: 0}}})});
  assert.equal((await second).content, '<html>');
  assert.equal((await first).analysis.total, 0);
  assert.equal(timers.size, 0);
});

test('input errors reject one request without stopping a healthy engine', async () => {
  const {runtime, workers} = await initialized();
  const request = runtime.request('/api/import', {filename: 'bad.csv'});
  const rejected = assert.rejects(request, /invalid input/);
  workers[0].emit('message', {type: 'result', id: workers[0].sent[0].id, result_json: '{"ok":false,"error":"invalid input"}'});
  await rejected;
  assert.equal(runtime.ready, true);
  assert.equal(workers[0].terminated, false);
});

test('worker failure rejects every pending request and can initialize again', async () => {
  const {runtime, workers, timers} = await initialized();
  const a = assert.rejects(runtime.request('/api/analyze', {}), /engine failed/);
  const b = assert.rejects(runtime.request('/api/report', {}), /engine failed/);
  workers[0].emit('message', {type: 'failure', message: 'engine failed'});
  await Promise.all([a, b]);
  assert.equal(workers[0].terminated, true);
  assert.equal(runtime.ready, false);
  assert.equal(timers.size, 0);
  const retry = runtime.init();
  assert.equal(workers.length, 2);
  workers[0].emit('message', {type: 'ready', version: 'stale'});
  assert.equal(runtime.ready, false);
  workers[1].emit('message', {type: 'ready', version: '0.1.0'});
  assert.equal((await retry).version, '0.1.0');
});

test('shutdown terminates the worker, clears pending calls, and returns ok', async () => {
  const {runtime, workers, timers} = await initialized();
  const rejected = assert.rejects(runtime.request('/api/analyze', {}), /已停止/);
  assert.equal((await runtime.request('/api/shutdown', {})).ok, true);
  await rejected;
  assert.equal(runtime.ready, false);
  assert.equal(workers[0].terminated, true);
  assert.equal(timers.size, 0);
});

test('load timeout and corrupted JSON response both fail explicitly', async () => {
  const loading = fixture();
  const rejected = assert.rejects(loading.runtime.init(), /加载超时/);
  [...loading.timers.values()][0].callback();
  await rejected;
  assert.equal(loading.workers[0].terminated, true);
  const active = await initialized();
  const pending = assert.rejects(active.runtime.request('/api/analyze', {}), /无效数据/);
  active.workers[0].emit('message', {type: 'result', id: active.workers[0].sent[0].id, result_json: 'not json'});
  await pending;
  assert.equal(active.workers[0].terminated, true);
});

test('requests before ready and unsupported routes never dispatch', async () => {
  const {runtime, workers} = fixture();
  await assert.rejects(runtime.request('/api/analyze', {}), /尚未就绪/);
  await assert.rejects(runtime.request('https://upload.example.test', {}), /接口不存在/);
  assert.equal(workers.length, 0);
});

test('worker loads only same-origin trusted modules and dispatches serial JSON strings', async () => {
  const sent = [];
  const fetched = [];
  const pythonGlobals = new Map();
  let initialScript;
  const self = {location: {href: 'https://example.test/project/python-worker.js'}, postMessage(message) { sent.push(message); }};
  const python = {
    version: '0.27.7', FS: {mkdirTree() {}, writeFile() {}}, globals: pythonGlobals,
    runPython(code) {
      if (code.includes('sys.path.insert')) return '{"ok":true,"result":{"version":"0.1.0"}}';
      const request = JSON.parse(pythonGlobals.get('_browser_request_json'));
      return JSON.stringify({ok: true, result: {path: request.path}});
    },
  };
  vm.runInNewContext(workerSource, {
    self, URL, importScripts(url) { initialScript = url; }, async loadPyodide(options) {
      assert.equal(options.indexURL, 'https://example.test/project/vendor/pyodide/'); return python;
    }, async fetch(url) { fetched.push(String(url)); return {ok: true, async text() { return '# trusted source'; }}; },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(initialScript, 'https://example.test/project/vendor/pyodide/pyodide.js');
  assert.deepEqual(fetched.map(url => new URL(url).pathname.split('/').at(-1)).sort(), ['archive_adapter.py', 'browser_api.py', 'engine.py', 'importers.py', 'reports.py']);
  assert.equal(sent.at(-1).type, 'ready');
  self.onmessage({data: {type: 'request', id: 1, request_json: '{"path":"/api/analyze","data":{}}'}});
  self.onmessage({data: {type: 'request', id: 2, request_json: '{"path":"/api/report","data":{}}'}});
  await new Promise(resolve => setImmediate(resolve));
  const results = sent.filter(message => message.type === 'result');
  assert.deepEqual(results.map(message => message.id), [1, 2]);
  assert.equal(JSON.parse(results[0].result_json).result.path, '/api/analyze');
  assert.equal(JSON.parse(results[1].result_json).result.path, '/api/report');
  assert.equal(pythonGlobals.has('_browser_request_json'), false);
});

test('archive facade passes a File directly and routes progress to its request', async () => {
  const {runtime, workers, timers} = await initialized();
  const progress = [];
  const file = {name: 'x-archive.zip', size: 300 * 1024 * 1024, slice() { throw new Error('Main thread must never read the File'); }};
  const request = runtime.inspectArchive(file, message => progress.push(message));
  const sent = workers[0].sent[0];
  assert.equal(sent.type, 'archive');
  assert.equal(sent.file, file);
  assert.equal(Object.hasOwn(sent, 'request_json'), false);
  workers[0].emit('message', {type: 'progress', id: sent.id, message: '正在扫描分片'});
  workers[0].emit('message', {type: 'result', id: sent.id, result_json: '{"ok":true,"result":{"summary":{"total":2601,"official_probability":null}}}'});
  assert.equal((await request).summary.total, 2601);
  assert.deepEqual(progress, ['正在扫描分片']);
  assert.equal(timers.size, 0);
});

test('archive facade validates 300 MiB boundary and permits cancellation/retry', async () => {
  const {runtime, workers} = await initialized();
  await assert.rejects(runtime.inspectArchive({name: 'x.zip', size: 300 * 1024 * 1024 + 1, slice() {}}), /300 MB/);
  await assert.rejects(runtime.inspectArchive({name: 'x.json', size: 5, slice() {}}), /ZIP/);
  assert.equal(workers[0].sent.length, 0);
  const file = {name: 'x.zip', size: 50, slice() {}};
  const pending = assert.rejects(runtime.inspectArchive(file), /已停止/);
  await assert.rejects(runtime.inspectArchive(file), /已有材料/);
  await runtime.request('/api/shutdown');
  await pending;
  assert.equal(workers[0].terminated, true);
  const restarted = runtime.init();
  workers[1].emit('message', {type: 'ready', version: '0.1.0'});
  await restarted;
  assert.equal(runtime.ready, true);
});
