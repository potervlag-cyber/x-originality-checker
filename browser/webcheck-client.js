'use strict';

// The browser sends only explicitly selected post text to the configured service.
// Search-provider credentials belong to that service, never to this module.
(() => {
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
    const token = typeof options.accessToken === 'string' ? options.accessToken.trim() : '';
    if (/[\r\n]/.test(token)) throw new Error('服务访问码格式无效。');
    async function send(path, body, signal) {
      const controller = new AbortController();
      const onAbort = () => controller.abort();
      if (signal?.aborted) controller.abort();
      else signal?.addEventListener('abort', onAbort, {once: true});
      const timer = setTimeout(() => controller.abort(), body ? 300000 : 20000);
      try {
        const headers = {Accept: 'application/json'};
        if (body) headers['Content-Type'] = 'application/json';
        if (token) headers.Authorization = `Bearer ${token}`;
        const response = await fetcher(root + path, {method: body ? 'POST' : 'GET', headers,
          credentials: 'omit', redirect: 'error', cache: 'no-store', signal: controller.signal,
          ...(body ? {body: JSON.stringify(body)} : {})});
        if (!response.ok) {
          const messages = {401: '查重服务需要有效的访问码。', 403: '查重服务未允许此网页连接。',
            429: '搜索额度或并发已用完，请稍后继续。', 503: '查重服务尚未配置搜索 API，暂时不能联网检索。'};
          throw new Error(messages[response.status] || `查重服务请求失败（${response.status}），本地结果已保留。`);
        }
        const text = await response.text();
        if (text.length > 2 * 1024 * 1024) throw new Error('查重服务返回的证据过大。');
        let data;
        try { data = JSON.parse(text); } catch { throw new Error('查重服务返回了无效数据。'); }
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('查重服务返回了无效数据。');
        return data;
      } catch (error) {
        if (controller.signal.aborted) throw new Error(signal?.aborted ? '联网查重已取消，已完成的结果保留。' : '查重服务超时，本地结果已保留。');
        throw error instanceof TypeError ? new Error('无法连接查重服务，请检查服务地址及网页访问许可。') : error;
      } finally {
        clearTimeout(timer);
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
