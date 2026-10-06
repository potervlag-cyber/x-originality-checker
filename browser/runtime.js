'use strict';

(() => {
  const scriptURL = document.currentScript?.src || new URL('./runtime.js', document.baseURI).href;
  const workerURL = new URL('./python-worker.js', scriptURL).href;
  const allowedPaths = new Set(['/api/import', '/api/analyze', '/api/report', '/api/health', '/api/shutdown']);
  const INIT_TIMEOUT = 120000;
  const REQUEST_TIMEOUT = 180000;
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
      notifyProgress(String(message.message || '正在准备浏览器引擎…'));
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
      stopWithError('浏览器引擎返回了无效数据。请保存当前项目并重新加载页面。');
      return;
    }
    const request = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(request.timer);
    if (response.ok) request.resolve(response.result);
    else request.reject(new Error(response.error || '材料处理失败。'));
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
        if (worker === currentWorker) stopWithError('浏览器引擎运行失败。请保存当前项目并重新加载页面；材料没有上传。');
      });
      worker.addEventListener('messageerror', () => {
        if (worker === currentWorker) stopWithError('浏览器引擎通信失败。请保存当前项目并重新加载页面。');
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
      stopWithError('浏览器引擎已停止。当前材料仍在页面中，请保存后重新加载页面。');
      return {ok: true};
    }
    if (!ready || !worker) throw new Error('浏览器引擎尚未就绪。请重新加载页面。');
    const requestJSON = JSON.stringify({path, data});
    const id = nextID++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => stopWithError('本次处理超时。当前材料仍在页面中，请分批处理或保存后重新加载页面。'), REQUEST_TIMEOUT);
      pending.set(id, {resolve, reject, timer});
      try { worker.postMessage({type: 'request', id, request_json: requestJSON}); }
      catch { stopWithError('无法将材料交给浏览器引擎。请保存当前项目并重新加载页面。'); }
    });
  }

  window.OriginalityRuntime = Object.freeze({init, request, get ready() { return ready; }});
})();
