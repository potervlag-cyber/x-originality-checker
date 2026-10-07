'use strict';

(() => {
  const MAX_ARCHIVE_BYTES = 300 * 1024 * 1024;
  const $ = selector => document.querySelector(selector);
  const runtime = window.OriginalityRuntime;
  let busy = false;
  let generation = 0;
  let latestResult = null;
  let latestFilename = '';
  let webController = null;
  let webOffset = 0;
  let webRunning = false;
  let pendingFile = null;
  let runSettings = null;
  let runPhase = 'setup';
  let runMessage = '';
  let archiveClearPromise = Promise.resolve();
  let editingRecovery = false;
  const linkInputs = Array.from({length: 10}, (_, index) => $(`#post-link-${index + 1}`));

  function clearRetainedArchive() {
    archiveClearPromise = archiveClearPromise.catch(() => {}).then(() => runtime.request('/api/archive/clear')).catch(() => {});
    return archiveClearPromise;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function showError(message) {
    $('#error-message').textContent = String(message);
    $('#error-message').hidden = false;
    $('#error-message').scrollIntoView({block:'nearest'});
  }

  function clearError() {
    $('#error-message').hidden = true;
    $('#error-message').textContent = '';
  }

  function setBusy(value) {
    busy = value;
    $('#choose-file').disabled = value;
    $('#file-input').disabled = value;
    $('#change-file').disabled = value;
    $('#drop-zone').hidden = value;
    $('#processing').hidden = !value;
    $('#drop-zone').classList.remove('drag-over');
    $('#network-toggle-row').hidden = value;
    $('#network-enabled').disabled = value;
    $('#run-analysis').disabled = value || webRunning;
  }

  function count(value) {
    return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—';
  }

  function percent(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? `${Number(value.toFixed(1))}%` : '—';
  }

  function estimateImpact(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '';
    if (!value) return '估计参考项';
    return `估计${value > 0 ? '上调' : '下调'} ${Number(Math.abs(value).toFixed(1))} 个百分点`;
  }

  function dateOnly(value) {
    const matched = String(value || '').match(/^\d{4}-\d{2}-\d{2}/);
    return matched ? matched[0] : '';
  }

  function safePostURL(value) {
    try {
      const url = new URL(String(value));
      if (url.protocol === 'https:' && /^(?:www\.)?(?:x\.com|twitter\.com)$/.test(url.hostname) && /^\/[A-Za-z0-9_]+\/status\/\d+\/?$/.test(url.pathname) && !url.username && !url.password) return url.href;
    } catch { /* Archive values are display text unless the URL is a valid X post. */ }
    return '';
  }

  function safeSourceURL(value) {
    try {
      const url = new URL(String(value));
      if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href;
    } catch { /* Untrusted source URLs are rendered as text only. */ }
    return '';
  }

  function renderPolicy(policy) {
    const fragment = document.createDocumentFragment();
    $('#policy-description').textContent = policy?.disclaimer || '以下为工具检查到的风险线索及材料缺口，不是官方不符合评分。';
    for (const rule of Array.isArray(policy?.requirements) ? policy.requirements : []) {
      const card = element('article', 'policy-card');
      const heading = element('div', 'policy-heading');
      heading.append(element('h4', '', rule.title), element('strong', 'policy-degree', rule.degree || '无法判断'));
      card.append(heading, element('p', '', rule.official_requirement || ''));
      const rate = percent(rule.signal_percent);
      card.append(element('p', 'policy-count', `风险线索 ${count(rule.signal_count)} 条 / ${count(rule.denominator)} 条非普通转帖记录${rate !== '—' ? `（${rate}）` : ''} · 已检查 ${count(rule.assessed_count)} 条 · 证据不足 ${count(rule.unknown_count)} 条`));
      card.append(element('p', '', rule.interpretation || '未发现线索不能证明符合；需结合创作与来源证据复核。'));
      const evidence = Array.isArray(rule.evidence) ? rule.evidence.slice(0, 4) : [];
      if (evidence.length) {
        const list = element('ul');
        for (const item of evidence) list.append(element('li', '', `${item.post_id || item.id || ''}：${item.detail || item.message || item.code || '需复核'}`));
        card.append(list);
      }
      fragment.append(card);
    }
    $('#policy-list').replaceChildren(fragment);
    const source = policy?.source || {};
    const note = element('span', '', `规则参考版本：${source.reference_date || '待核实'}。${source.live_verification === 'unavailable' ? '本次官方页读取受限，未将其标记为已重新核验。' : ''}账号资格、权利授权及创作过程需另行提供证据。 `);
    const link = element('a', '', '查看官方原创内容奖励要求 ↗');
    link.href = 'https://help.x.com/en/using-x/original-content-rewards'; link.target = '_blank'; link.rel = 'noreferrer';
    $('#policy-source').replaceChildren(note, link);
  }

  function renderWebCheck(report) {
    const fragment = document.createDocumentFragment();
    if (!report) { $('#web-evidence').replaceChildren(); return; }
    const coverage = report.coverage || {};
    if (!coverage.selection_configured && !coverage.requested) {
      fragment.append(element('p', 'web-summary', `本次未选择联网查询；归档中共有 ${count(coverage.total_eligible)} 条可联网检索正文。`));
      $('#web-evidence').replaceChildren(fragment);
      return;
    }
    fragment.append(element('p', 'web-summary', `本次选中 ${count(coverage.selected_total ?? coverage.total_eligible)} 条 · 实际检索 ${count(coverage.searched)} 条 · 来源正文已核对 ${count(coverage.compared)} 条 · 失败 ${count(coverage.failed)} 条 · 所选范围尚未处理 ${count(coverage.remaining)} 条。来源证据已纳入综合结论与逐项风险说明；全网收录覆盖未知。`));
    if (coverage.unselected_eligible > 0) fragment.append(element('p', 'web-summary', `另外 ${count(coverage.unselected_eligible)} 条可检索正文未选入本次手动选择，不能根据这 10 条推断其他帖子的原创程度。`));
    if (coverage.execution_unknown_posts > 0) fragment.append(element('p', 'web-summary', `${count(coverage.execution_unknown_posts)} 条已发送但未能确认结果，记为未知；后续不会自动重复发送这些帖子。`));
    if (coverage.text_truncated_posts > 0) fragment.append(element('p', 'web-summary', `${count(coverage.text_truncated_posts)} 条正文只检索了部分文字，剩余内容未查；即使未发现相似来源也不计为完整检查。`));
    const posts = Array.isArray(report.posts) ? report.posts : [];
    const matched = posts.filter(post => Array.isArray(post.matches) && post.matches.length);
    for (const post of matched.slice(0, 30)) {
      const card = element('article', 'source-card');
      card.append(element('h4', '', `帖子 ${post.id} · 相似来源需复核`));
      if (post.matches_total > post.matches.length) card.append(element('p', 'evidence-level', `发现 ${count(post.matches_total)} 个相似来源，保留最多 3 条来源证据供复核。`));
      if (post.text_truncated) card.append(element('p', 'evidence-level', '本次只检查前 5,000 字，余下正文未检索。'));
      for (const match of post.matches.slice(0, 3)) {
        const url = safeSourceURL(match.url);
        if (url) {
          const link = element('a', '', match.title || url);
          link.href = url; link.target = '_blank'; link.rel = 'noreferrer'; card.append(link);
        }
        const fullText = match.source_kind === 'page_body' && match.page_status === 'fetched';
        const similarity = typeof match.score === 'number' ? percent(match.score * 100) : '—';
        card.append(element('p', 'evidence-level', `${fullText ? '来源正文证据' : '搜索摘要线索，来源正文未完整核对'} · 片段相似度 ${similarity} · 作者与许可未核验`));
        if (match.source_text_truncated) card.append(element('p', 'evidence-level', '来源正文只核对前 100,000 字，余下内容未比对。'));
        if (match.post_excerpt) card.append(element('blockquote', '', `归档片段：${match.post_excerpt}`));
        if (match.source_excerpt) card.append(element('blockquote', '', `来源片段：${match.source_excerpt}`));
        const relation = {earlier:'页面显示时间早于该帖',later:'页面显示时间晚于该帖',same:'页面显示时间相近',unknown:'来源发布时间未知'}[match.temporal_relation] || '来源发布时间未知';
        card.append(element('p', '', `${relation}${match.published_at ? `（${String(match.published_at).slice(0, 30)}）` : ''}；页面时间不能独立证明首发或搬运。`));
      }
      fragment.append(card);
    }
    if (matched.length > 30) fragment.append(element('p', 'web-summary', '更多来源证据保留在下载的 JSON 报告中。'));
    const noMatch = posts.filter(post => post.status === 'no_match').length;
    const partial = posts.filter(post => post.status === 'partial').length;
    if (noMatch) fragment.append(element('p', 'web-post-status', `${count(noMatch)} 条在实际检索范围内未发现明显相似片段，不代表原创。`));
    if (partial) fragment.append(element('p', 'web-post-status', `${count(partial)} 条仅完成部分检索，完整性不足。`));
    if (report.limitations?.length) fragment.append(element('p', 'web-summary', report.limitations.slice(0, 5).join('；')));
    $('#web-evidence').replaceChildren(fragment);
  }

  function setWebRunning(value) {
    webRunning = value;
    $('#web-start').disabled = value;
    $('#web-connect').disabled = value;
    $('#web-consent').disabled = value;
    $('#web-endpoint').disabled = value;
    $('#web-token').disabled = value;
    $('#run-analysis').disabled = value || busy;
    $('#cancel-network-setup').disabled = value;
    $('#cancel-network-button').disabled = value;
    linkInputs.forEach(input => { input.disabled = value; });
    $('#cancel-connection').hidden = !value || busy;
    $('#choose-file').disabled = value || busy;
    $('#change-file').disabled = value || busy;
  }

  function showNetworkProgress(message) {
    $('#setup-status').textContent = message;
    if (busy) $('#progress-message').textContent = message;
    else $('#web-status').textContent = message;
  }

  async function checkService(settings) {
    const client = window.OriginalityWebCheck.create(settings.endpoint, {accessToken: settings.token});
    showNetworkProgress('正在连接查重服务；若服务正在唤醒，可能需要约一分钟，请稍候…');
    const status = await client.status(webController?.signal);
    if (!status.ready) throw new Error('查重服务尚未配置搜索 API，当前没有执行联网检索。');
    if (status.requires_access_token && !settings.token) throw new Error('请填写服务提供者给出的访问码，再继续分析。');
    showNetworkProgress(`已连接 ${status.provider || '公开来源搜索'}。本小时剩余 ${count(status.limits?.hourly_queries_remaining)} 次查询，每条最多 ${count(status.limits?.max_queries_per_post || 2)} 次。`);
    return {client, status};
  }

  async function runWebCheck(settings) {
    const ownGeneration = generation;
    webController = new AbortController();
    setWebRunning(true);
    let currentPlan = null;
    let dispatched = false;
    try {
      runPhase = 'connecting';
      // Freeze the user's selected IDs locally before any service request.
      currentPlan = await runtime.request('/api/webcheck/plan', {mode: settings.scope, links: settings.links, offset: webOffset, limit: 3, max_chars: 5000});
      if (ownGeneration !== generation) return;
      latestResult = await runtime.request('/api/webcheck/result');
      if (!currentPlan.posts?.length) { runPhase = 'completed'; runMessage = '所选范围没有尚待查询的可检索正文。'; return; }
      const {client, status} = await checkService(settings);
      runPhase = 'network';
      while (!webController.signal.aborted) {
        const plan = await runtime.request('/api/webcheck/plan', {mode: settings.scope, offset: webOffset,
          limit: Math.min(3, status.limits?.max_posts || 10), max_chars: Math.min(5000, status.limits?.max_checked_chars || 5000)});
        if (ownGeneration !== generation) return;
        currentPlan = plan;
        if (!plan.posts?.length) break;
        showNetworkProgress(`正在联网查询第 ${count(webOffset + 1)}–${count(plan.next_offset)} 条 / 手动指定 ${count(plan.selected_total)} 条…`);
        dispatched = true;
        const report = await client.check(plan.posts, webController.signal);
        if (ownGeneration !== generation) return;
        // An already returned response is retained even if the user just cancelled.
        const updated = await runtime.request('/api/webcheck/apply', {session_id: plan.session_id, report});
        if (ownGeneration !== generation) return;
        dispatched = false;
        latestResult = updated;
        webOffset = plan.next_offset;
        if (plan.done) break;
      }
      runPhase = webController.signal.aborted ? 'cancelled' : 'completed';
      runMessage = runPhase === 'cancelled' ? '已取消后续联网查询，本地结果和已完成证据保留。'
        : '手动指定的 10 条查询已结束；未选、失败及证据不足的内容在报告中单独列明。';
    } catch (error) {
      if (ownGeneration !== generation) return;
      if (dispatched && currentPlan && !error?.searchNotExecuted) {
        try {
          latestResult = await runtime.request('/api/webcheck/abandon', {session_id: currentPlan.session_id,
            ids: currentPlan.posts.map(post => post.id), reason: webController.signal.aborted ? 'cancelled' : 'response_unknown'});
          webOffset = currentPlan.next_offset;
        } catch { runMessage = '本批已发送但结果无法确认，暂停续查以避免重复消耗额度。'; webOffset = Number.MAX_SAFE_INTEGER; }
      }
      runPhase = webController.signal.aborted ? 'cancelled' : 'incomplete';
      if (webOffset !== Number.MAX_SAFE_INTEGER) runMessage = error?.message || '联网检查未完成，本地结果与已完成证据已纳入报告。';
    } finally {
      if (ownGeneration === generation) { webController = null; setWebRunning(false); }
    }
  }

  function renderResult(result, file) {
    if (!result || typeof result.summary !== 'object' || !result.summary || !Number.isSafeInteger(result.summary.total)) {
      throw new Error('分析工具未返回有效结果，请重新选择归档后重试。');
    }
    const summary = result.summary;
    const coverage = result.coverage || {};
    const types = summary.types || {};
    const period = [dateOnly(coverage.actual_start), dateOnly(coverage.actual_end)].filter(Boolean);
    $('#result-meta').textContent = `${file.name} · 已检测 ${count(summary.total)} 条归档记录${period.length === 2 ? ` · ${period[0]} 至 ${period[1]}` : ''}`;
    const combined = summary.combined_evidence || {};
    $('#combined-title').textContent = combined.title || '归档检查结果';
    $('#combined-description').textContent = combined.conclusion || '依据归档中的文本与材料信号判断，需结合创作过程复核。';
    const webCoverage = result.web_check?.coverage || {};
    $('#combined-coverage').textContent = runSettings?.mode === 'network'
      ? `本地检查 ${count(summary.total)} 条记录；联网手动指定 ${count(webCoverage.selected_total ?? combined.selected_total)} / ${count(webCoverage.total_eligible ?? combined.total_eligible)} 条，实际检索 ${count(webCoverage.searched)} 条，正文相似来源 ${count(combined.body_matched_posts)} 条。${webCoverage.unselected_eligible > 0 ? `另有 ${count(webCoverage.unselected_eligible)} 条未选入联网范围。` : ''}`
      : '本次仅进行本地归档分析，未执行联网查重。';
    $('#combined-run-status').textContent = runMessage;
    $('#web-status').textContent = runSettings?.mode === 'network' ? runMessage : '本次选择本地分析，未进行联网查重。';
    const canResume = runSettings?.mode === 'network' && !['completed', 'local_failed'].includes(runPhase)
      && Number.isSafeInteger(webCoverage.remaining) && webCoverage.remaining > 0 && webOffset !== Number.MAX_SAFE_INTEGER;
    $('#web-recovery').hidden = !canResume;
    const estimatedProbability = percent(summary.estimated_probability);
    const hasEstimate = estimatedProbability !== '—';
    $('#probability-value').textContent = hasEstimate ? estimatedProbability : '材料不足，无法估计';
    $('#probability-value').classList.toggle('unavailable', !hasEstimate);
    $('#probability-meta').hidden = !hasEstimate;
    const range = summary.probability_range || {};
    const validRange = percent(range.low) !== '—' && percent(range.high) !== '—' && range.low <= range.high;
    $('#probability-range').textContent = validRange ? `主观参考范围 ${percent(range.low)}–${percent(range.high)}` : '主观参考范围暂不确定';
    const confidence = ['低', '中'].includes(summary.probability_confidence) ? summary.probability_confidence : '低';
    $('#probability-confidence').textContent = `判断把握：${confidence}`;
    $('#probability-explanation').textContent = summary.probability_explanation || (hasEstimate
      ? '依据归档内的文本信号与材料完整性给出参考估计，尚未用真实审核结果校准。'
      : '归档缺少本人可比较正文，无法给出参考概率。');
    $('#nonduplicate-value').textContent = percent(summary.nonduplicate_percent);
    const excludedNote = Number.isSafeInteger(summary.comparison_excluded_posts) && summary.comparison_excluded_posts > 0
      ? `另有 ${count(summary.comparison_excluded_posts)} 条非转帖记录缺少可比较正文。` : '';
    $('#nonduplicate-detail').textContent = Number.isSafeInteger(summary.nonduplicate_denominator) && summary.nonduplicate_denominator > 0
      ? `${count(summary.nonduplicate_numerator)} / ${count(summary.nonduplicate_denominator)} 条可比较正文未发现归档内重复，不含普通转帖。${excludedNote}`
      : `没有足够的可比较正文，无法计算这一占比。${excludedNote}`;
    if (coverage.approximate_comparison_limited) $('#nonduplicate-detail').textContent += '近似比对覆盖有限，此占比可能偏高。';

    const stats = [
      ['检测记录', summary.total, '归档中的全部记录'],
      ['主帖', types.posts, '类型标签不代表原创'],
      ['回复', types.reply, '纳入检测范围'],
      ['转帖 / 引用帖', null, `${count(types.repost)} 条转帖 · ${count(types.quote)} 条引用`],
      ['完全重复文本', summary.exact_duplicate_posts, '按涉及帖子计数'],
      ['近似重复文本', summary.near_duplicate_posts, '需复核实际内容'],
      ['正文缺失', summary.missing_text_posts, '缺少可比较文本'],
      ['媒体文件覆盖', null, `${count(summary.hashed_media)} / ${count(summary.media_references)} 个媒体引用`],
    ];
    const fragment = document.createDocumentFragment();
    for (const [label, value, note] of stats) {
      const cell = element('div', 'stat');
      const display = label === '媒体文件覆盖' ? percent(summary.media_completeness_percent)
        : label === '转帖 / 引用帖' ? count((types.repost || 0) + (types.quote || 0)) : count(value);
      cell.append(element('span', 'stat-label', label), element('strong', 'stat-value', display), element('span', 'stat-note', note));
      fragment.append(cell);
    }
    $('#stats-grid').replaceChildren(fragment);
    renderPolicy(result.policy_checks);
    renderWebCheck(result.web_check);

    const factors = Array.isArray(result.probability_factors) ? result.probability_factors.filter(item => item && typeof item === 'object') : [];
    const factorCodes = new Set(factors.map(item => item.code).filter(Boolean));
    const reasons = Array.isArray(result.reasons) ? result.reasons.filter(item => item && typeof item === 'object' && !factorCodes.has(item.code)) : [];
    const reasonFragment = document.createDocumentFragment();
    const mergedReasons = [...factors.map(item => ({...item, model_factor:true})), ...reasons];
    const displayedReasons = mergedReasons.length ? mergedReasons : [{title:'需要结合创作背景复核',detail:'当前材料未提供足够的判断依据，需结合实际创作过程核实。'}];
    displayedReasons.slice(0, 20).forEach((reason, index) => {
      const item = element('div', 'reason-item');
      const copy = element('div', 'reason-copy');
      const title = element('div', 'reason-heading');
      title.append(element('h4', '', reason.title || '检测发现'));
      const impact = reason.model_factor ? (reason.code === 'subjective_start' ? '规则起点 50%' : estimateImpact(reason.impact_points)) : '';
      if (impact) title.append(element('span', 'reason-impact', impact));
      copy.append(title, element('p', '', reason.detail || '请结合原帖与创作背景复核。'));
      item.append(element('span', 'reason-number', String(index + 1).padStart(2, '0')), copy);
      reasonFragment.append(item);
    });
    $('#reason-list').replaceChildren(reasonFragment);

    const examples = Array.isArray(result.examples) ? result.examples.slice(0, 12) : [];
    $('#examples-section').hidden = !examples.length;
    $('#examples-section').open = false;
    $('#examples-count').textContent = `${examples.length} 条示例`;
    const exampleFragment = document.createDocumentFragment();
    for (const example of examples) {
      const item = element('article', 'example');
      const top = element('div', 'example-top');
      top.append(element('span', '', `${example.status_label || '需复核'} · ${example.id || '未提供编号'}`));
      const url = safePostURL(example.url);
      if (url) {
        const link = element('a', '', '查看原帖 ↗');
        link.href = url; link.target = '_blank'; link.rel = 'noreferrer'; top.append(link);
      }
      item.append(top, element('p', 'example-text', example.text || '未提供正文'));
      const messages = Array.isArray(example.reasons) ? example.reasons.map(reason => reason.message || '').filter(Boolean) : [];
      if (messages.length) item.append(element('p', 'example-reasons', messages.join('；')));
      exampleFragment.append(item);
    }
    $('#example-list').replaceChildren(exampleFragment);

    const scope = element('div');
    scope.append(element('p', '', `读取 ${count(Array.isArray(coverage.post_files) ? coverage.post_files.length : coverage.post_files)} 个帖子文件；归档包含 ${count(coverage.archive_records)} 条记录，本地已检测 ${count(coverage.analyzed_posts)} 条。联网范围单独记录，手动指定的 10 条不代表全归档联网检查。`));
    const notes = [
      '原创通过概率是本工具根据归档信号给出的启发式参考估计，尚未用真实 X 审核样本校准，不能视为官方或经验证的实际通过率。',
      '主观参考范围用于表达材料与检查方法的不确定性，不是统计置信区间；材料缺失降低判断把握，不等于抄袭。',
      '这一估计只涉及原创材料判断，不包括会员、展示量、认证粉丝、地区、处罚状态等收益资格门槛。',
      ...(Array.isArray(coverage.notes) ? coverage.notes : []), ...(Array.isArray(result.warnings) ? result.warnings : []), ...(Array.isArray(result.limitations) ? result.limitations : [])];
    const list = element('ul');
    for (const note of [...new Set(notes.map(String))]) list.append(element('li', '', note));
    if (list.childElementCount) scope.append(list);
    $('#scope-content').replaceChildren(...scope.childNodes);
    $('#results').hidden = false;
  }

  async function inspectFiles(files) {
    if (busy || webRunning || $('#network-dialog').open || !files.length) return;
    clearError();
    if (files.length !== 1) { showError('请一次选择一个从 X 下载的归档 ZIP。'); return; }
    const file = files[0];
    if (!/\.zip$/i.test(file.name)) { showError('请上传 ZIP 压缩包。直接选择从 X 下载的原始归档，无需先解压。'); return; }
    if (file.size > MAX_ARCHIVE_BYTES) { showError('归档超过 300 MB（300 MiB）上限，请选择不超过该大小的 ZIP。'); return; }
    if (!file.size) { showError('这个 ZIP 是空文件，请检查下载是否完整。'); return; }
    if (!runtime?.inspectArchive) { showError('分析工具暂不可用，请刷新页面后重试。'); return; }

    ++generation;
    const replacing = !!pendingFile || !!latestResult;
    pendingFile = file;
    latestResult = null;
    latestFilename = '';
    runSettings = null;
    runPhase = 'setup';
    runMessage = '';
    webOffset = 0;
    if (replacing) $('#web-consent').checked = false;
    $('#web-status').textContent = '尚未联网查重。';
    $('#results').hidden = true;
    $('.upload-card').hidden = false;
    $('#drop-zone').hidden = false;
    $('#network-toggle-row').hidden = false;
    linkInputs.forEach(input => { input.value = ''; input.readOnly = false; });
    editingRecovery = false;
    $('#file-input').value = '';
    clearRetainedArchive();
    if ($('#network-enabled').checked) openNetworkDialog();
    else await runAnalysis({mode: 'local', scope: '', consent: false});
  }

  function manualLinks() {
    const ids = new Set();
    return linkInputs.map((input, index) => {
      const raw = input.value.trim();
      let url, matched;
      try {
        url = new URL(raw);
        matched = url.pathname.match(/^\/(?:[A-Za-z0-9_]{1,15}|i\/web)\/status\/([1-9][0-9]{0,29})(?:\/(?:photo|video)\/[1-9][0-9]*)?\/?$/);
        if (!['https:', 'http:'].includes(url.protocol) || !['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'].includes(url.hostname)
          || url.username || url.password || url.port || !matched || raw.length > 2048 || /[\u0000-\u0020\u007f\\]/.test(raw)) throw new Error();
      } catch {
        input.focus();
        throw new Error(`请在帖子 ${index + 1} 填写有效的 X 帖子链接，例如 https://x.com/用户名/status/帖子编号。`);
      }
      if (ids.has(matched[1])) { input.focus(); throw new Error(`帖子 ${index + 1} 与前面的链接重复，请选择 10 条不同的帖子。`); }
      ids.add(matched[1]);
      url.protocol = 'https:';
      url.search = ''; url.hash = '';
      return url.href;
    });
  }

  function settingsFromForm() {
    const links = editingRecovery ? [...runSettings.links] : manualLinks();
    if (!$('#web-consent').checked) {
      $('#web-consent').focus();
      throw new Error('请先确认这 10 条帖子的文字可以发送给查重服务和搜索提供商。');
    }
    let endpoint;
    try { endpoint = window.OriginalityWebCheck.endpoint($('#web-endpoint').value); }
    catch (error) { $('.service-settings').open = true; $('#web-endpoint').focus(); throw error; }
    const token = $('#web-token').value.trim();
    if (!token && !['localhost', '127.0.0.1', '[::1]'].includes(new URL(endpoint).hostname)) {
      $('#web-token').focus();
      throw new Error('请填写服务提供者给出的访问码，再开始联网分析。');
    }
    return {mode: 'network', scope: 'manual10', links, endpoint, token, consent: true};
  }

  function showDialogError(message) {
    $('#dialog-error').textContent = String(message);
    $('#dialog-error').hidden = false;
    $('#dialog-error').scrollIntoView({block: 'nearest'});
  }

  function openNetworkDialog(message = '') {
    $('#dialog-file').textContent = editingRecovery ? latestFilename : pendingFile?.name || '';
    $('#network-dialog-title').textContent = editingRecovery ? '继续所选 10 条的联网查询' : '选择 10 条帖子联网查重';
    $('#network-dialog-description').textContent = editingRecovery
      ? '本次帖子选择已固定。可修改连接信息，继续尚未发送的查询，已发送且结果未知的帖子不会重复查询。'
      : '粘贴此 ZIP 中的 10 条 X 帖子链接。确认后分析全部归档，并联网核对这 10 条的公开来源。';
    $('#run-analysis').textContent = editingRecovery ? '继续查询并更新报告' : '确认并开始分析';
    $('#dialog-error').hidden = true;
    $('#dialog-error').textContent = '';
    linkInputs.forEach((input, index) => {
      input.readOnly = editingRecovery;
      if (editingRecovery) input.value = runSettings.links[index];
    });
    $('#network-dialog').showModal();
    if (message) showDialogError(message);
    if (editingRecovery) $('#web-token').focus();
    else linkInputs[0].focus();
  }

  function cancelNetworkSetup() {
    if (busy || webRunning) return;
    $('#network-dialog').close();
    if (editingRecovery) { editingRecovery = false; return; }
    resetToSetup();
    $('#choose-file').focus();
  }

  function finishReport() {
    if (!latestResult) return;
    latestResult.analysis_run = {mode: runSettings?.mode || 'local', web_scope: runSettings?.scope || '',
      phase: runPhase, message: runMessage, consent: !!runSettings?.consent};
    renderResult(latestResult, {name: latestFilename});
    $('.upload-card').hidden = true;
    $('#engine-state').textContent = '';
    $('#result-title').focus({preventScroll: true});
    $('#results').scrollIntoView({block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  }

  async function runAnalysis(settings) {
    if (busy || webRunning || !pendingFile) return;
    clearError();
    runSettings = settings;
    const file = pendingFile;
    const ownGeneration = ++generation;
    latestResult = null;
    latestFilename = file.name;
    runPhase = 'local';
    runMessage = '';
    webOffset = 0;
    $('#results').hidden = true;
    $('#file-name').textContent = file.name;
    $('#file-size').textContent = `${(file.size / (1024 * 1024)).toFixed(2)} MB · 自动检测全部归档帖子`;
    $('#processing-note').textContent = runSettings.mode === 'network'
      ? 'ZIP 在本机处理，联网仅发送所选范围内的帖子字段。查询完成后自动生成综合报告。'
      : '全部归档分析在浏览器中进行；本次不会发送帖子文字到查重服务。';
    $('#cancel').textContent = '取消分析';
    $('#progress-message').textContent = '正在准备分析工具…';
    $('#engine-state').textContent = '';
    setBusy(true);
    let validatingSelection = false;
    try {
      await archiveClearPromise;
      if (generation !== ownGeneration) return;
      await runtime.init(message => { if (generation === ownGeneration) $('#progress-message').textContent = String(message); });
      if (generation !== ownGeneration) return;
      const result = await runtime.inspectArchive(file, message => { if (generation === ownGeneration) $('#progress-message').textContent = String(message); });
      if (generation !== ownGeneration) return;
      latestResult = result;
      if (runSettings.mode === 'network') {
        validatingSelection = true;
        await runtime.request('/api/webcheck/plan', {mode: 'manual10', links: runSettings.links, offset: 0, limit: 3, max_chars: 5000});
        if (generation !== ownGeneration) return;
        validatingSelection = false;
        $('#cancel').textContent = '停止后续联网查询';
        await runWebCheck(runSettings);
      } else { runPhase = 'completed'; runMessage = '本地检查已完成。'; }
      if (generation !== ownGeneration) return;
      finishReport();
    } catch (error) {
      if (generation !== ownGeneration) return;
      if (validatingSelection) {
        latestResult = null;
        runSettings = null;
        runPhase = 'setup';
        setBusy(false);
        openNetworkDialog(error?.message || '链接无法对应此归档中的可检索帖子，请修正后重试。');
        return;
      }
      runPhase = 'local_failed';
      showError(error?.message || '无法完成分析，请检查归档下载是否完整并重试。');
      $('#engine-state').textContent = '';
    } finally {
      if (generation === ownGeneration) { setBusy(false); $('#file-input').value = ''; }
    }
  }

  async function resumeWebCheck(settings = runSettings) {
    if (busy || webRunning || !latestResult || runSettings?.mode !== 'network') return;
    clearError();
    runSettings = settings;
    const ownGeneration = generation;
    $('.upload-card').hidden = false;
    $('#results').hidden = true;
    $('#processing-note').textContent = '继续尚未发送的联网范围；已发送但结果未知的批次不会自动重发。';
    $('#cancel').textContent = '停止后续联网查询';
    setBusy(true);
    await runWebCheck(runSettings);
    if (ownGeneration === generation) { finishReport(); setBusy(false); }
  }

  function chooseFile() {
    if (busy || webRunning) return;
    $('#file-input').value = '';
    $('#file-input').click();
  }

  function resetToSetup() {
    if (busy || webRunning) return;
    ++generation;
    pendingFile = null;
    latestResult = null;
    latestFilename = '';
    runSettings = null;
    runPhase = 'setup';
    runMessage = '';
    webOffset = 0;
    $('#web-consent').checked = false;
    $('#results').hidden = true;
    $('.upload-card').hidden = false;
    $('#network-toggle-row').hidden = false;
    $('#drop-zone').hidden = false;
    editingRecovery = false;
    linkInputs.forEach(input => { input.value = ''; input.readOnly = false; });
    clearError();
    clearRetainedArchive();
  }

  $('#choose-file').addEventListener('click', chooseFile);
  $('#change-file').addEventListener('click', () => { resetToSetup(); chooseFile(); });
  $('#network-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || webRunning) return;
    let settings;
    try { settings = settingsFromForm(); } catch (error) { showDialogError(error.message); return; }
    const resuming = editingRecovery;
    $('#network-dialog').close();
    editingRecovery = false;
    if (resuming) await resumeWebCheck(settings);
    else await runAnalysis(settings);
  });
  $('#cancel-network-setup').addEventListener('click', cancelNetworkSetup);
  $('#cancel-network-button').addEventListener('click', cancelNetworkSetup);
  $('#network-dialog').addEventListener('cancel', event => {
    event.preventDefault();
    cancelNetworkSetup();
  });
  $('#web-start').addEventListener('click', () => resumeWebCheck());
  $('#edit-connection').addEventListener('click', () => {
    if (busy || webRunning) return;
    editingRecovery = true;
    openNetworkDialog();
  });
  $('#web-connect').addEventListener('click', async () => {
    if (busy || webRunning) return;
    const ownGeneration = generation;
    webController = new AbortController();
    setWebRunning(true);
    try { await checkService({endpoint: $('#web-endpoint').value, token: $('#web-token').value.trim()}); } catch (error) {
      if (ownGeneration === generation) $('#setup-status').textContent = error?.message || '查重服务连接失败。';
    } finally {
      if (ownGeneration === generation) { webController = null; setWebRunning(false); }
    }
  });
  $('#cancel-connection').addEventListener('click', () => {
    webController?.abort();
    $('#setup-status').textContent = '已取消服务连接。';
  });
  fetch('./webcheck-config.json', {credentials:'same-origin'}).then(response => response.ok ? response.json() : null)
    .then(config => { if (config?.endpoint && !$('#web-endpoint').value) $('#web-endpoint').value = window.OriginalityWebCheck.endpoint(config.endpoint); })
    .catch(() => { /* A user may connect a service explicitly through settings. */ });
  $('#file-input').addEventListener('change', event => inspectFiles([...event.target.files]));
  $('#result-title').tabIndex = -1;
  const zone = $('#drop-zone');
  zone.addEventListener('dragover', event => { event.preventDefault(); if (!busy) { zone.classList.add('drag-over'); event.dataTransfer.dropEffect = 'copy'; } });
  zone.addEventListener('dragleave', event => { if (!zone.contains(event.relatedTarget)) zone.classList.remove('drag-over'); });
  zone.addEventListener('drop', event => { event.preventDefault(); zone.classList.remove('drag-over'); inspectFiles([...event.dataTransfer.files]); });
  window.addEventListener('dragover', event => { event.preventDefault(); });
  window.addEventListener('drop', event => { event.preventDefault(); });

  $('#cancel').addEventListener('click', async () => {
    if (!busy) return;
    if (runPhase === 'connecting' || runPhase === 'network') { webController?.abort(); return; }
    ++generation;
    try { await runtime.request('/api/shutdown'); } catch { /* A terminated worker already stopped the current analysis. */ }
    setBusy(false);
    $('#file-input').value = '';
    $('#engine-state').textContent = '已取消分析，可以重新选择 ZIP。';
    latestResult = null;
    runSettings = null;
    $('#web-consent').checked = false;
  });

  $('#download-result').addEventListener('click', () => {
    if (!latestResult) return;
    const content = JSON.stringify({filename:latestFilename,analyzed_at:new Date().toISOString(),...latestResult}, null, 2);
    const url = URL.createObjectURL(new Blob([content], {type:'application/json;charset=utf-8'}));
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'X归档原创分析.json';
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });

  if (runtime?.init) {
    const startupGeneration = generation;
    $('#engine-state').textContent = '正在准备分析工具，可先选择 ZIP…';
    runtime.init(message => { if (!busy && generation === startupGeneration) $('#engine-state').textContent = String(message); })
      .then(() => { if (!busy && generation === startupGeneration) $('#engine-state').textContent = '分析工具已就绪'; })
      .catch(() => { if (!busy && generation === startupGeneration) $('#engine-state').textContent = '暂未准备完成，选择 ZIP 时会重新尝试。'; });
  }
})();
