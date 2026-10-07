'use strict';

(() => {
  const scriptURL = document.currentScript?.src || new URL('./runtime.js', document.baseURI).href;
  const workerURL = new URL('./python-worker.js', scriptURL).href;
  const allowedPaths = new Set(['/api/import', '/api/analyze', '/api/report', '/api/health', '/api/shutdown', '/api/archive/clear', '/api/archive/posts', '/api/compliance/weights', '/api/webcheck/plan', '/api/webcheck/apply', '/api/webcheck/abandon', '/api/webcheck/result']);
  const INIT_TIMEOUT = 120000;
  const REQUEST_TIMEOUT = 180000;
  const ARCHIVE_TIMEOUT = 1800000;
  const MAX_UPLOAD_BYTES = 300 * 1024 * 1024;
  let worker = null;
  let ready = false;
  let nextID = 1;
  let initPromise = null;
  let initResolve;
  let initReject;
  let initTimer;
  const pending = new Map();
  const progressHandlers = new Set();

  function notifyProgress(message) {
    for (const handler of progressHandlers) {
      try { handler(message); } catch { /* A UI callback must not break the worker. */ }
    }
  }

  function stopWithError(message) {
    const error = new Error(message);
    ready = false;
    clearTimeout(initTimer);
    if (worker) worker.terminate();
    worker = null;
    if (initReject) initReject(error);
    initResolve = undefined;
    initReject = undefined;
    initPromise = null;
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    pending.clear();
    progressHandlers.clear();
  }

  function handleMessage(event, currentWorker) {
    if (worker !== currentWorker) return; // Ignore late messages from a stopped engine.
    const message = event.data;
    if (!message || typeof message !== 'object') return;
    if (message.type === 'progress') {
      const progress = String(message.message || '正在准备浏览器引擎…');
      if (Number.isSafeInteger(message.id) && pending.has(message.id)) {
        try { pending.get(message.id).onProgress?.(progress); } catch { /* UI callback only. */ }
      } else notifyProgress(progress);
      return;
    }
    if (message.type === 'failure') {
      stopWithError(String(message.message || '浏览器引擎失败，请重新加载页面。'));
      return;
    }
    if (message.type === 'ready') {
      if (!initResolve || typeof message.version !== 'string') return;
      clearTimeout(initTimer);
      ready = true;
      const resolve = initResolve;
      initResolve = undefined;
      initReject = undefined;
      progressHandlers.clear();
      resolve({version: message.version});
      return;
    }
    if (message.type !== 'result' || !pending.has(message.id)) return;
    let response;
    try {
      if (typeof message.result_json !== 'string') throw new Error();
      response = JSON.parse(message.result_json);
      if (!response || typeof response.ok !== 'boolean') throw new Error();
    } catch {
      stopWithError('浏览器引擎返回了无效数据。请重新加载页面并重新选择归档。');
      return;
    }
    const request = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(request.timer);
    if (response.ok) request.resolve(response.result);
    else request.reject(new Error(response.error || '材料处理失败。'));
  }

  async function inspectArchive(file, onProgress) {
    if (!file || typeof file.size !== 'number' || !Number.isSafeInteger(file.size) || file.size < 1 || typeof file.slice !== 'function') throw new Error('请选择从 X 下载的归档 ZIP 文件。');
    if (file.size > MAX_UPLOAD_BYTES) throw new Error('归档 ZIP 超过 300 MB 上限；未读取文件内容。');
    if (typeof file.name !== 'string' || !/\.zip$/i.test(file.name)) throw new Error('请选择 ZIP 格式的 X 归档。');
    if (!ready || !worker) throw new Error('浏览器引擎尚未就绪。请重新加载页面。');
    if (pending.size) throw new Error('已有材料正在处理中，请等待或取消后重试。');
    const id = nextID++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => stopWithError('归档分析超时；未给出部分结果。请重新加载页面并重试。'), ARCHIVE_TIMEOUT);
      pending.set(id, {resolve, reject, timer, onProgress: typeof onProgress === 'function' ? onProgress : undefined});
      try { worker.postMessage({type: 'archive', id, file}); }
      catch { stopWithError('无法将 ZIP 交给浏览器引擎。请重新加载页面并重试。'); }
    });
  }

  function init(onProgress) {
    if (typeof onProgress === 'function' && !ready) progressHandlers.add(onProgress);
    if (initPromise) return initPromise;
    initPromise = new Promise((resolve, reject) => {
      initResolve = resolve;
      initReject = reject;
    });
    const promise = initPromise;
    try {
      if (typeof Worker === 'undefined') throw new Error('Web Worker unavailable');
      worker = new Worker(workerURL, {name: 'originality-python'});
      const currentWorker = worker;
      worker.addEventListener('message', event => handleMessage(event, currentWorker));
      worker.addEventListener('error', event => {
        event.preventDefault();
        if (worker === currentWorker) stopWithError('浏览器引擎运行失败。请重新加载页面并重新选择归档；材料没有上传。');
      });
      worker.addEventListener('messageerror', () => {
        if (worker === currentWorker) stopWithError('浏览器引擎通信失败。请重新加载页面并重新选择归档。');
      });
      initTimer = setTimeout(() => stopWithError('浏览器引擎加载超时。请检查网络连接并重新加载页面。'), INIT_TIMEOUT);
    } catch {
      stopWithError('此浏览器无法启动评估引擎。请使用较新的 Chrome、Edge、Firefox 或 Safari 并重新加载页面。');
    }
    return promise;
  }

  async function request(path, data = {}) {
    if (!allowedPaths.has(path)) throw new Error('接口不存在。');
    if (path === '/api/shutdown') {
      stopWithError('浏览器引擎已停止。可以重新选择归档后重试。');
      return {ok: true};
    }
    if (!ready || !worker) throw new Error('浏览器引擎尚未就绪。请重新加载页面。');
    const requestJSON = JSON.stringify({path, data});
    const id = nextID++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => stopWithError('本次处理超时。请重新加载页面并重新选择归档。'), REQUEST_TIMEOUT);
      pending.set(id, {resolve, reject, timer});
      try { worker.postMessage({type: 'request', id, request_json: requestJSON}); }
      catch { stopWithError('无法将材料交给浏览器引擎。请重新加载页面并重新选择归档。'); }
    });
  }

  window.OriginalityRuntime = Object.freeze({init, request, inspectArchive, MAX_UPLOAD_BYTES, get ready() { return ready; }});
})();
