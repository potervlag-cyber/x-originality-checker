'use strict';

// The browser sends only explicitly selected post text to the configured service.
// Search-provider credentials belong to that service, never to this module.
(() => {
  const STATUS_TIMEOUT_MS = 120000;
  const STATUS_RETRY_MS = 2000;
  const POST_TIMEOUT_MS = 300000;
  function endpoint(value) {
    let url;
    try { url = new URL(String(value)); } catch { throw new Error('请输入查重服务的完整网址。'); }
    const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) || url.username || url.password || url.search || url.hash) {
      throw new Error('查重服务须使用 HTTPS；本机服务可使用 localhost 或 127.0.0.1 的 HTTP。网址不能包含凭据。');
    }
    return url.href.replace(/\/+$/, '');
  }

  function create(base, options = {}) {
    const root = endpoint(base), fetcher = options.fetch || globalThis.fetch;
    const timers = options.timers || globalThis;
    const setTimer = (callback, duration) => timers.setTimeout(callback, duration);
    const clearTimer = timer => timers.clearTimeout(timer);
    const token = typeof options.accessToken === 'string' ? options.accessToken.trim() : '';
    if (/[\r\n]/.test(token)) throw new Error('服务访问码格式无效。');

    function statusWait(phase, attempt) {
      try { options.onStatusWait?.({phase, attempt, max_wait_ms: STATUS_TIMEOUT_MS}); }
      catch { /* A display callback cannot interrupt the connection. */ }
    }

    async function abortable(promise, signal) {
      let onAbort;
      try {
        return await Promise.race([promise, new Promise((_resolve, reject) => {
          onAbort = () => reject(new Error('Request aborted'));
          signal.addEventListener('abort', onAbort, {once: true});
          if (signal.aborted) onAbort();
        })]);
      } finally { signal.removeEventListener('abort', onAbort); }
    }

    function retryDelay(signal) {
      return new Promise((resolve, reject) => {
        let timer;
        const onAbort = () => {
          clearTimer(timer);
          signal.removeEventListener('abort', onAbort);
          reject(new Error('Request aborted'));
        };
        if (signal.aborted) { reject(new Error('Request aborted')); return; }
        signal.addEventListener('abort', onAbort, {once: true});
        timer = setTimer(() => { signal.removeEventListener('abort', onAbort); resolve(); }, STATUS_RETRY_MS);
      });
    }

    function serviceError(status, data) {
      const messages = {401: '查重服务需要有效的访问码。', 403: '查重服务未允许此网页连接。',
        429: '搜索额度或并发已用完，请稍后继续。', 503: '查重服务暂时不可用，本地结果已保留。'};
      const error = new Error(data?.code === 'provider_not_configured' ? '查重服务尚未配置搜索 API，暂时不能联网检索。'
        : messages[status] || `查重服务请求失败（${status}），本地结果已保留。`);
      error.httpStatus = status;
      // These service responses reject a batch before provider execution.
      error.searchNotExecuted = [401, 403, 429].includes(status) || data?.code === 'provider_not_configured';
      return error;
    }

    async function send(path, body, signal) {
      const statusRequest = !body;
      const controller = new AbortController();
      const onAbort = () => controller.abort();
      if (signal?.aborted) controller.abort();
      else signal?.addEventListener('abort', onAbort, {once: true});
      // One deadline covers every GET attempt and retry delay. POSTs are never
      // replayed: a lost response can still represent a paid search execution.
      const timer = setTimer(() => controller.abort(), statusRequest ? STATUS_TIMEOUT_MS : POST_TIMEOUT_MS);
      let requestStarted = false;
      try {
        const headers = {Accept: 'application/json'};
        if (body) headers['Content-Type'] = 'application/json';
        if (token) headers.Authorization = `Bearer ${token}`;
        let attempt = 0;
        if (statusRequest) statusWait('connecting', 0);
        while (true) {
          if (controller.signal.aborted) throw new Error('Request aborted');
          attempt += 1;
          let response, text;
          try {
            requestStarted = true;
            response = await abortable(fetcher(root + path, {method: body ? 'POST' : 'GET', headers,
              credentials: 'omit', redirect: 'error', cache: 'no-store', signal: controller.signal,
              ...(body ? {body: JSON.stringify(body)} : {})}), controller.signal);
            // Authentication and access errors are final, even if their body is HTML.
            if ([401, 403, 429].includes(response.status)) throw serviceError(response.status);
            text = typeof response.text === 'function' ? await abortable(response.text(), controller.signal) : '';
          } catch (error) {
            // A sleeping cross-origin service may return a proxy page without CORS
            // headers, which fetch exposes as TypeError rather than an HTTP status.
            if (!statusRequest || controller.signal.aborted || !(error instanceof TypeError)) throw error;
            statusWait('waking', attempt);
            await retryDelay(controller.signal);
            continue;
          }
          if (text.length > 2 * 1024 * 1024) throw new Error('查重服务返回的证据过大。');
          let data, parsedJSON = false;
          try { data = JSON.parse(text); parsedJSON = true; } catch { /* HTML may be a bounded cold-start response. */ }
          const html = /(?:text\/html|application\/xhtml\+xml)/i.test(response.headers?.get?.('Content-Type') || '') || /^\s*(?:\uFEFF)?(?:<!doctype\s+html|<html\b)/i.test(text);
          const temporaryStatus = !parsedJSON && [502, 503].includes(response.status);
          if (statusRequest && (temporaryStatus || response.ok && html && !parsedJSON)) {
            statusWait('waking', attempt);
            await retryDelay(controller.signal);
            continue;
          }
          // JSON business failures (including an absent provider key) are never
          // mistaken for cold starts and are not retried.
          if (!response.ok) throw serviceError(response.status, data);
          if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('查重服务返回了无效数据。');
          return data;
        }
      } catch (error) {
        const reported = controller.signal.aborted ? new Error(signal?.aborted ? '联网查重已取消，已完成的结果保留。' : statusRequest ? '等待查重服务唤醒超时（最多 2 分钟），本地结果已保留；请稍后再连接。' : '查重服务超时，本地结果已保留。')
          : error instanceof TypeError ? new Error('无法连接查重服务，请检查服务地址及网页访问许可。') : error;
        if (!statusRequest && (!requestStarted || error?.searchNotExecuted)) reported.searchNotExecuted = true;
        throw reported;
      } finally {
        clearTimer(timer);
        signal?.removeEventListener('abort', onAbort);
      }
    }
    return Object.freeze({
      status: signal => send('/api/webcheck/status', null, signal),
      async check(posts, signal) {
        if (!Array.isArray(posts) || !posts.length || posts.length > 10) throw new Error('每批需提供 1 至 10 条帖子。');
        const payload = posts.map(post => {
          if (!post || typeof post.id !== 'string' || typeof post.text !== 'string' || post.text.length > 5000) throw new Error('联网查询片段格式无效。');
          // Do not serialize the plan, archive project, account, or media metadata.
          return {id: post.id, text: post.text, url: String(post.url || ''), created_at: String(post.created_at || '')};
        });
        const report = await send('/api/webcheck', {consent: true, posts: payload}, signal);
        if (report.schema_version !== 1 || !Array.isArray(report.posts) || report.posts.length !== payload.length ||
            !report.coverage || new Set(report.posts.map(post => post.id)).size !== payload.length ||
            report.posts.some(post => !payload.some(item => item.id === post.id))) throw new Error('查重证据与本次查询帖子不一致。');
        return report;
      }
    });
  }
  globalThis.OriginalityWebCheck = Object.freeze({create, endpoint});
})();
