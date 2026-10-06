'use strict';

// A fixed, same-origin runtime is supplied by the static-site build.
const PYODIDE_VERSION = '0.27.7';
const RUNTIME_URL = new URL('./vendor/pyodide/', self.location.href).href;
const PYTHON_FILES = ['importers.py', 'engine.py', 'reports.py', 'browser_api.py'];
let python = null;
let ready = false;
let queue = Promise.resolve();

function progress(message) {
  self.postMessage({type: 'progress', message});
}

function failure(message) {
  ready = false;
  self.postMessage({type: 'failure', message});
}

async function initialize() {
  try {
    progress('正在加载浏览器 Python 引擎，首次打开需要一些时间…');
    importScripts(new URL('pyodide.js', RUNTIME_URL).href);
    python = await loadPyodide({indexURL: RUNTIME_URL});
    if (python.version !== PYODIDE_VERSION) throw new Error('Runtime version mismatch');
    progress('正在准备本地评估规则…');
    const sources = await Promise.all(PYTHON_FILES.map(async filename => {
      const response = await fetch(new URL(filename, self.location.href), {credentials: 'same-origin'});
      if (!response.ok) throw new Error('Trusted module unavailable');
      return {filename, text: await response.text()};
    }));
    python.FS.mkdirTree('/app');
    for (const source of sources) python.FS.writeFile(`/app/${source.filename}`, source.text);
    const healthJSON = python.runPython(`
import sys
sys.path.insert(0, '/app')
from browser_api import dispatch_json
dispatch_json('{"path":"/api/health","data":{}}')
`);
    const health = JSON.parse(healthJSON);
    if (!health.ok || !health.result?.version) throw new Error('Adapter initialization failed');
    ready = true;
    self.postMessage({type: 'ready', version: health.result.version, pythonVersion: PYODIDE_VERSION});
  } catch {
    failure('浏览器引擎加载失败。请检查网络连接并重新加载页面；材料没有上传。');
  }
}

async function processRequest(message) {
  if (!ready || !python) {
    self.postMessage({type: 'result', id: message.id, result_json: JSON.stringify({ok: false, error: '浏览器引擎尚未就绪。请重新加载页面。'})});
    return;
  }
  try {
    // Requests run in one queue so this shared global is never overwritten.
    python.globals.set('_browser_request_json', message.request_json);
    const resultJSON = python.runPython('dispatch_json(_browser_request_json)');
    if (typeof resultJSON !== 'string') throw new Error('Adapter result must be a string');
    self.postMessage({type: 'result', id: message.id, result_json: resultJSON});
  } catch {
    failure('浏览器引擎处理失败。请保存当前项目并重新加载页面；材料没有上传。');
  } finally {
    try { python.globals.delete('_browser_request_json'); } catch { /* A failed WASM runtime may already be unavailable. */ }
  }
}

self.onmessage = event => {
  const message = event.data;
  if (!message || message.type !== 'request' || !Number.isSafeInteger(message.id) || typeof message.request_json !== 'string') return;
  queue = queue.then(() => processRequest(message));
};

initialize();
