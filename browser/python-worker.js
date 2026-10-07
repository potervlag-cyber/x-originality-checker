'use strict';

// A fixed, same-origin runtime is supplied by the static-site build.
const PYODIDE_VERSION = '0.27.7';
const RUNTIME_URL = new URL('./vendor/pyodide/', self.location.href).href;
const PYTHON_FILES = ['importers.py', 'engine.py', 'reports.py', 'policy_checks.py', 'archive_adapter.py', 'browser_api.py'];
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
    importScripts(new URL('archive.js', self.location.href).href);
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
import hashlib, zlib
from browser_api import dispatch_json, prepare_archive_json, finish_archive_json, clear_archive, clear_prepared_archive
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

function createHasher() {
  const digest = python.runPython('hashlib.sha256()');
  let closed = false;
  return {
    update(chunk) {
      const converted = python.toPy(chunk);
      try { digest.update(converted); }
      finally { converted.destroy(); }
    },
    digest() {
      try { return digest.hexdigest(); }
      finally { if (!closed) digest.destroy(); closed = true; }
    },
    dispose() { if (!closed) digest.destroy(); closed = true; },
  };
}

// Older browsers without native raw DEFLATE can still stream via stdlib zlib.
// max_length bounds each emitted block rather than materializing an expanded file.
function inflateRaw(stream, entry) {
  const input = stream.getReader();
  const inflater = python.runPython('zlib.decompressobj(-15)');
  let destroyed = false;
  let pendingBytes = new Uint8Array(0);
  let expanded = 0;
  const cleanup = () => { if (!destroyed) inflater.destroy(); destroyed = true; };
  return new ReadableStream({
    async pull(controller) {
      try {
        while (true) {
          if (!pendingBytes.byteLength) {
            const {value, done} = await input.read();
            if (done) {
              if (!inflater.eof) throw new Error('ZIP 解压数据不完整。');
              cleanup(); controller.close(); return;
            }
            pendingBytes = value;
          }
          const converted = python.toPy(pendingBytes);
          let output;
          let tail;
          let extra;
          let block;
          try {
            output = inflater.decompress(converted, 65536);
            tail = inflater.unconsumed_tail;
            extra = inflater.unused_data;
            block = output.toJs();
            pendingBytes = tail.toJs();
            if (extra.toJs().byteLength) throw new Error('ZIP 压缩条目含额外数据。');
          } finally {
            converted.destroy(); output?.destroy(); tail?.destroy(); extra?.destroy();
          }
          expanded += block.byteLength;
          if (expanded > entry.size) throw new Error('ZIP 展开长度超过声明，已停止解压。');
          if (block.byteLength) { controller.enqueue(block); return; }
        }
      } catch (error) { cleanup(); controller.error(error); await input.cancel(); }
    },
    async cancel(reason) { cleanup(); await input.cancel(reason); },
  });
}

async function processArchive(message) {
  if (!ready || !python) {
    self.postMessage({type: 'result', id: message.id, result_json: JSON.stringify({ok: false, error: '浏览器引擎尚未就绪。'})});
    return;
  }
  const archiveProgress = value => self.postMessage({type: 'progress', id: message.id, message: String(value)});
  let completed = false;
  try {
    python.runPython('clear_archive()');
    const options = {onProgress: archiveProgress};
    try { new DecompressionStream('deflate-raw'); }
    catch { options.inflateRaw = inflateRaw; }
    const reader = await self.ArchiveZip.open(message.file, options);
    archiveProgress('正在解压全部发帖分片…');
    let files = await reader.readPostFiles();
    python.globals.set('_archive_json', JSON.stringify({files, metadata: {...reader.metadata, unread_note_files: reader.noteEntries.length}}));
    files = null;
    let prepared;
    try { prepared = JSON.parse(python.runPython('prepare_archive_json(_archive_json)')); }
    finally { python.globals.delete('_archive_json'); }
    if (!prepared.ok) throw new Error(prepared.error);
    archiveProgress('正在核对归档中关联的媒体文件…');
    const media = await reader.hashMedia(prepared.result.media_names, {createHasher, onProgress: archiveProgress});
    prepared = null;
    archiveProgress('正在分析全部帖子正文与重复信号…');
    python.globals.set('_archive_media_json', JSON.stringify(media));
    let resultJSON;
    try { resultJSON = python.runPython('finish_archive_json(_archive_media_json)'); }
    finally { python.globals.delete('_archive_media_json'); }
    if (typeof resultJSON !== 'string') throw new Error('归档分析返回数据异常。');
    completed = JSON.parse(resultJSON).ok === true;
    self.postMessage({type: 'result', id: message.id, result_json: resultJSON});
  } catch (error) {
    self.postMessage({type: 'result', id: message.id, result_json: JSON.stringify({ok: false, error: error instanceof Error ? error.message : '归档处理失败；未进行部分分析。'})});
  } finally {
    try { python.runPython(completed ? 'clear_prepared_archive()' : 'clear_archive()'); } catch { /* Worker stop handles runtime failure. */ }
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
    failure('浏览器引擎处理失败。请重新加载页面并重新选择归档；材料没有上传。');
  } finally {
    try { python.globals.delete('_browser_request_json'); } catch { /* A failed WASM runtime may already be unavailable. */ }
  }
}

self.onmessage = event => {
  const message = event.data;
  if (!message || !Number.isSafeInteger(message.id)) return;
  if (message.type === 'archive') {
    queue = queue.then(() => processArchive(message));
  } else if (message.type === 'request' && typeof message.request_json === 'string') {
    queue = queue.then(() => processRequest(message));
  }
};

initialize();
