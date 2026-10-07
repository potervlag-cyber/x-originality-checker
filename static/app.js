'use strict';

(() => {
  const MAX_ARCHIVE_BYTES = 300 * 1024 * 1024;
  const $ = selector => document.querySelector(selector);
  const runtime = window.OriginalityRuntime;
  let busy = false;
  let generation = 0;
  let latestResult = null;
  let latestFilename = '';

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
  }

  function count(value) {
    return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—';
  }

  function percent(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? `${Number(value.toFixed(1))}%` : '—';
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

  function renderResult(result, file) {
    if (!result || typeof result.summary !== 'object' || !result.summary || !Number.isSafeInteger(result.summary.total)) {
      throw new Error('分析工具未返回有效结果，请重新选择归档后重试。');
    }
    const summary = result.summary;
    const coverage = result.coverage || {};
    const types = summary.types || {};
    const period = [dateOnly(coverage.actual_start), dateOnly(coverage.actual_end)].filter(Boolean);
    $('#result-meta').textContent = `${file.name} · 已检测 ${count(summary.total)} 条归档记录${period.length === 2 ? ` · ${period[0]} 至 ${period[1]}` : ''}`;
    $('#probability-explanation').textContent = summary.probability_explanation || 'X 未公开审核模型，当前工具也没有经官方审核结果校准的数据。';
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

    const reasons = Array.isArray(result.reasons) ? result.reasons : [];
    const reasonFragment = document.createDocumentFragment();
    const displayedReasons = reasons.length ? reasons : [{title:'检测结果需要结合创作背景复核',detail:'未提供可解释的风险原因。这不代表已经证明原创或满足官方审核。'}];
    displayedReasons.slice(0, 20).forEach((reason, index) => {
      const item = element('div', 'reason-item');
      const copy = element('div', 'reason-copy');
      copy.append(element('h4', '', reason.title || '检测发现'), element('p', '', reason.detail || '请结合原帖与创作背景复核。'));
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
    scope.append(element('p', '', `读取 ${count(Array.isArray(coverage.post_files) ? coverage.post_files.length : coverage.post_files)} 个帖子文件；归档包含 ${count(coverage.archive_records)} 条记录，已检测 ${count(coverage.analyzed_posts)} 条。没有按日期筛选或只抽取申请样本。`));
    const notes = [...(Array.isArray(coverage.notes) ? coverage.notes : []), ...(Array.isArray(result.warnings) ? result.warnings : []), ...(Array.isArray(result.limitations) ? result.limitations : [])];
    const list = element('ul');
    for (const note of [...new Set(notes.map(String))]) list.append(element('li', '', note));
    if (list.childElementCount) scope.append(list);
    $('#scope-content').replaceChildren(...scope.childNodes);
    $('#results').hidden = false;
  }

  async function inspectFiles(files) {
    if (busy || !files.length) return;
    clearError();
    if (files.length !== 1) { showError('请一次选择一个从 X 下载的归档 ZIP。'); return; }
    const file = files[0];
    if (!/\.zip$/i.test(file.name)) { showError('请上传 ZIP 压缩包。直接选择从 X 下载的原始归档，无需先解压。'); return; }
    if (file.size > MAX_ARCHIVE_BYTES) { showError('归档超过 300 MB（300 MiB）上限，请选择不超过该大小的 ZIP。'); return; }
    if (!file.size) { showError('这个 ZIP 是空文件，请检查下载是否完整。'); return; }
    if (!runtime?.inspectArchive) { showError('分析工具暂不可用，请刷新页面后重试。'); return; }

    const ownGeneration = ++generation;
    latestResult = null;
    latestFilename = '';
    $('#results').hidden = true;
    $('.upload-card').hidden = false;
    $('#file-name').textContent = file.name;
    $('#file-size').textContent = `${(file.size / (1024 * 1024)).toFixed(2)} MB · 自动检测全部归档帖子`;
    $('#progress-message').textContent = '正在准备分析工具…';
    $('#engine-state').textContent = '';
    setBusy(true);
    try {
      await runtime.init(message => { if (generation === ownGeneration) $('#progress-message').textContent = String(message); });
      if (generation !== ownGeneration) return;
      const result = await runtime.inspectArchive(file, message => { if (generation === ownGeneration) $('#progress-message').textContent = String(message); });
      if (generation !== ownGeneration) return;
      renderResult(result, file);
      latestResult = result;
      latestFilename = file.name;
      $('.upload-card').hidden = true;
      $('#engine-state').textContent = '';
      $('#result-title').focus({preventScroll:true});
      $('#results').scrollIntoView({block:'start',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    } catch (error) {
      if (generation !== ownGeneration) return;
      showError(error?.message || '无法完成分析，请检查归档下载是否完整并重试。');
      $('#engine-state').textContent = '';
    } finally {
      if (generation === ownGeneration) { setBusy(false); $('#file-input').value = ''; }
    }
  }

  function chooseFile() {
    if (busy) return;
    $('#file-input').value = '';
    $('#file-input').click();
  }

  $('#choose-file').addEventListener('click', chooseFile);
  $('#change-file').addEventListener('click', chooseFile);
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
    ++generation;
    try { await runtime.request('/api/shutdown'); } catch { /* A terminated worker already stopped the current analysis. */ }
    setBusy(false);
    $('#file-input').value = '';
    $('#engine-state').textContent = '已取消分析，可以重新选择 ZIP。';
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
