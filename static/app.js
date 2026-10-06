'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const statusClass = {high_risk:'high', review:'review', insufficient:'unknown', low_signal:'clear'};
const typeLabels = {original:'主帖', reply:'回复', quote:'引用帖', repost:'普通转帖', article:'Article'};
const titles = {overview:'评估概览', materials:'导入与资料', posts:'帖子与证据', candidates:'申请候选', report:'导出报告'};
const browserDeployment = window.ORIGINALITY_DEPLOYMENT === 'browser';
const freshProject = () => ({schema_version:1, account:'', scope:{start:'',end:'',timezone:'Asia/Shanghai',complete:null,total_expected:null,selection:'full_period',known_missing:''}, posts:[], selected_candidates:[]});
let project = freshProject();
let analysis = null;
let localToken = '';
let engineReady = false;
let selectedPost = '';
let dirty = false;
let stale = false;
let busy = false;
let toastTimer;
let pendingConfirm;
let editingSources = [];
let editingMedia = [];

function toast(message, error = false) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, error ? 8000 : 4300);
}

function confirmAction(title, message, action) {
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  pendingConfirm = action;
  $('#confirm-modal').showModal();
}

function navigate(view) {
  if (!(view in titles)) return;
  $$('.view').forEach(element => element.classList.toggle('active', element.id === `view-${view}`));
  $$('.nav-item').forEach(element => element.classList.toggle('active', element.dataset.view === view));
  $('#page-title').textContent = titles[view];
  window.scrollTo({top:0, behavior:'instant'});
}

function changeProject() {
  dirty = true;
  stale = true;
  analysis = null;
  project.selected_candidates = project.selected_candidates.filter(id => project.posts.some(post => post.id === id));
  render();
}

function setBusy(value) {
  busy = value;
  $('#analyze').disabled = value || !engineReady;
  $('#analyze').textContent = value ? '正在处理…' : '运行评估 →';
  $$('#file-input,#load-demo,#add-post,#add-post-second,#save-project,#export-project,#export-html,#export-md').forEach(element => element.disabled = value);
  $('#drop-zone').setAttribute('aria-disabled', String(value));
}

async function api(path, data) {
  if (browserDeployment) {
    if (!engineReady) throw new Error('浏览器引擎尚未就绪，请点击侧栏重新启动引擎。');
    try { return await window.OriginalityRuntime.request(path, data); }
    catch (error) {
      if (!window.OriginalityRuntime.ready) {
        engineReady=false;$('#connection-state').textContent='引擎已停止，当前材料仍保留';
        $('#shutdown').textContent='重新启动引擎';
      }
      throw error;
    }
  }
  if (!localToken) throw new Error('尚未连接本机服务，请重新打开启动器。');
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Local-Token':localToken},body:JSON.stringify(data)});
  let result;
  try { result = await response.json(); } catch { throw new Error('本机服务未返回有效数据，请检查运行状态。'); }
  if (!response.ok) throw new Error(result.error || `处理失败（${response.status}）`);
  return result;
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.readAsDataURL(file);
  });
}

async function importFiles(files) {
  if (busy || !files.length) return;
  if (!$('#import-append').checked && project.posts.length) {
    confirmAction('替换当前材料', '当前材料会被替换。需要保留时，请先保存 JSON 项目。', () => processImports(files));
  } else {
    await processImports(files);
  }
}

async function processImports(files) {
  setBusy(true);
  const messages = [];
  let working = $('#import-append').checked ? structuredClone(project) : freshProject();
  let success = 0;
  try {
    for (const file of files) {
      try {
        if (file.size > 30 * 1024 * 1024) throw new Error('单文件超过 30 MB，请筛选后分批导入。');
        const result = await api('/api/import', {filename:file.name,content_base64:await readFileAsBase64(file)});
        const incoming = structuredClone(working);
        let next;
        if (!incoming.posts.length) {
          next = result.project;
        } else {
          next = incoming;
          const oldIds = new Set(next.posts.map(post => post.id));
          let duplicateCount = 0;
          for (const record of result.project.posts) {
            const post = structuredClone(record);
            const previous = next.posts.find(item => item.id === post.id || (post.url && item.url === post.url));
            if (previous && previous.text === post.text) { duplicateCount++; continue; }
            if (oldIds.has(post.id)) {
              const originalId = post.id;
              let suffix = 2;
              while (oldIds.has(`${originalId}__${suffix}`)) suffix++;
              post.id = `${originalId}__${suffix}`;
              messages.push(`${file.name}：${originalId} 编号重复但正文不同，保留为 ${post.id}，请核对。`);
            }
            oldIds.add(post.id);
            next.posts.push(post);
          }
          if (duplicateCount) messages.push(`${file.name}：跳过 ${duplicateCount} 条相同编号记录（原记录已保留）。`);
          if (!next.account && result.project.account) next.account = result.project.account;
        }
        if (next.posts.length > 2000) throw new Error('第一版最多处理 2,000 条，请缩小导入范围。');
        next.import_warnings = [...new Set([...(next.import_warnings || []), ...(result.project.import_warnings || []), ...(result.warnings || [])])];
        working = next;
        success++;
        messages.push(`${file.name}：读取 ${result.project.posts.length} 条帖子。`);
        messages.push(...(result.warnings || []));
      } catch (error) {
        messages.push(`${file.name}：${error.message}`);
      }
    }
    if (success) {
      project = working;
      selectedPost = project.posts[0]?.id || '';
      syncScopeForm();
      changeProject();
      toast(`已导入材料，当前 ${project.posts.length} 条。请确认范围后运行评估。`);
    } else toast('未导入有效材料。请查看下方说明。', true);
    $('#import-feedback').replaceChildren();
    for (const message of messages) { const p = document.createElement('p'); p.textContent = message; $('#import-feedback').append(p); }
  } finally { setBusy(false); $('#file-input').value = ''; }
}

function syncScopeForm() {
  const scope = project.scope || {};
  $('#account').value = project.account || '';
  $('#scope-selection').value = ['full_period','sample','partial'].includes(scope.selection) ? scope.selection : 'partial';
  $('#scope-complete').value = scope.complete === true ? 'yes' : scope.complete === false ? 'no' : 'unknown';
  $('#scope-start').value = scope.start || '';
  $('#scope-end').value = scope.end || '';
  $('#scope-timezone').value = scope.timezone === 'UTC' ? 'UTC' : 'Asia/Shanghai';
  $('#scope-total').value = scope.total_expected ?? '';
  $('#scope-missing').value = Array.isArray(scope.known_missing) ? scope.known_missing.join('\n') : (scope.known_missing || '');
}

function saveScope(silent = false) {
  if (busy) return false;
  const start = $('#scope-start').value, end = $('#scope-end').value;
  if (start && end && start > end) { toast('开始日期不能晚于结束日期。', true); return false; }
  const totalText = $('#scope-total').value;
  const total = totalText === '' ? null : Number(totalText);
  if (total !== null && (!Number.isInteger(total) || total < 0)) { toast('总帖数需要是非负整数。', true); return false; }
  const nextScope = {
    ...project.scope, start, end, timezone:$('#scope-timezone').value,
    complete:$('#scope-complete').value === 'yes' ? true : $('#scope-complete').value === 'no' ? false : null,
    total_expected:total, selection:$('#scope-selection').value,
    known_missing:$('#scope-missing').value.split('\n').map(v=>v.trim()).filter(Boolean).join('；'),
  };
  const account = $('#account').value.trim();
  if (JSON.stringify(nextScope) !== JSON.stringify(project.scope) || account !== project.account) {
    project.account = account;
    project.scope = nextScope;
    changeProject();
  }
  if (!silent) toast('账号资料已保存。运行评估会应用当前范围。');
  return true;
}

async function runAnalysis(showMessage = true) {
  if (busy) return false;
  if (!saveScope(true)) return false;
  if (!project.posts.length) { toast('请先导入材料或手动添加帖子。', true); navigate('materials'); return false; }
  setBusy(true);
  try {
    const result = await api('/api/analyze', {project});
    project = result.project;
    analysis = result.analysis;
    stale = false;
    render();
    if (showMessage) toast(`评估完成，实际检查 ${analysis.summary.total} 条。`);
    return true;
  } catch (error) { toast(error.message, true); return false; }
  finally { setBusy(false); }
}

function metric(label, value, hint, extraClass = '') {
  return `<div class="metric-card ${extraClass}"><div class="metric-label">${escapeHTML(label)}</div><strong class="metric-value">${escapeHTML(value)}</strong><p>${escapeHTML(hint)}</p></div>`;
}

function render() {
  $('#nav-count').textContent = project.posts.length;
  $('#project-state').textContent = `${project.account || (project.posts.length ? '未填写账号' : '未导入材料')}${dirty ? ' · 未保存' : ''}`;
  const counts = analysis?.summary.counts || {};
  $('#metrics').innerHTML = [
    metric('已导入帖子',project.posts.length,analysis ? `本次范围内评估 ${analysis.summary.total} 条` : '先确认资料范围，再运行评估'),
    metric('高风险',analysis ? counts.high_risk || 0 : '—','存在需要优先处理的对比证据','high'),
    metric('需复核 / 材料不足',analysis ? (counts.review || 0)+(counts.insufficient || 0) : '—','补充来源、正文或创作背景','review'),
    metric('可供复核的候选',analysis ? analysis.candidates.length : '—','最多推荐 10 篇，不代表官方认可','clear'),
  ].join('');
  const scope = project.scope || {};
  const coverage = analysis?.coverage || {};
  const complete = scope.complete === true ? '本人声明完整' : scope.complete === false ? '部分材料' : '完整性未知';
  const missing = Array.isArray(scope.known_missing) ? scope.known_missing.join('；') : scope.known_missing;
  $('#coverage-overview').innerHTML = `<dl class="meta-grid"><dt>账号</dt><dd>${escapeHTML(project.account || '未填写')}</dd><dt>分析窗口</dt><dd>${escapeHTML(scope.start || '不限开始')} — ${escapeHTML(scope.end || '不限结束')}</dd><dt>时区</dt><dd>${escapeHTML(scope.timezone || 'Asia/Shanghai')}</dd><dt>完整性</dt><dd>${escapeHTML(complete)}</dd><dt>已知缺失</dt><dd>${escapeHTML(missing || '未声明缺失，不等于完整')}</dd></dl>${analysis ? `<details><summary>查看实际覆盖统计</summary><pre class="coverage-json">${escapeHTML(JSON.stringify(coverage,null,2))}</pre></details>` : '<p class="muted">运行评估后显示实际覆盖统计。</p>'}${(project.import_warnings || []).length ? `<div class="notice warning"><strong>导入记录</strong><ul>${project.import_warnings.map(item=>`<li>${escapeHTML(item)}</li>`).join('')}</ul></div>` : ''}`;
  const priority = analysis?.posts.filter(post=>['high_risk','review','insufficient'].includes(post.status)).slice(0,4) || [];
  $('#priority-list').innerHTML = priority.length ? priority.map(post=>`<button class="priority-row" data-post="${escapeHTML(post.id)}"><span class="chip ${statusClass[post.status]}">${escapeHTML(post.status_label)}</span><span>${escapeHTML(post.text.slice(0,60) || post.url || post.id)}</span><span>→</span></button>`).join('') : `<div class="empty-state compact"><strong>${analysis ? '当前没有优先处理项' : '从材料开始'}</strong><p>${analysis ? '仍需查看来源覆盖和未实现的检查。' : '导入帖子后，问题和证据会显示在这里。'}</p></div>`;
  renderPosts();
  renderCandidates();
}

function assessedPost(id) { return analysis?.posts.find(post=>post.id===id); }

function renderPosts() {
  const term = $('#post-search').value.toLowerCase().trim(), filter = $('#post-filter').value;
  const posts = project.posts.filter(post => {
    const finding = assessedPost(post.id);
    return (!term || `${post.text} ${post.id} ${post.url}`.toLowerCase().includes(term)) && (filter === 'all' || finding?.status === filter);
  });
  $('#posts-description').textContent = `当前显示 ${posts.length} / ${project.posts.length} 条 · ${analysis ? '点击查看理由与证据' : '编辑材料后运行评估'}`;
  $('#post-list').innerHTML = posts.length ? posts.map(post=> {
    const finding = assessedPost(post.id);
    return `<button class="post-row ${selectedPost===post.id?'selected':''}" data-post="${escapeHTML(post.id)}"><div class="post-main"><div class="post-meta"><span>${escapeHTML(post.id)}</span><span>${escapeHTML(typeLabels[post.type] || '主帖')}</span><span class="chip ${finding ? statusClass[finding.status] : 'unknown'}">${escapeHTML(finding?.status_label || (analysis ? '范围外 / 时间待补充' : '待评估'))}</span></div><div class="post-title">${escapeHTML(post.text || '只有链接，需补充正文')}</div><div class="muted">${escapeHTML(post.created_at || '时间未知')} · 来源 ${post.sources?.length || 0} · 媒体 ${post.media?.length || 0}</div></div></button>`;
  }).join('') : `<div class="empty-state"><strong>${project.posts.length?'没有匹配帖子':'还没有帖子'}</strong><p>导入材料，或手动添加帖子和来源。</p></div>`;
  const post = project.posts.find(item=>item.id===selectedPost);
  if (!post) { $('#post-detail').innerHTML='<div class="empty-state"><strong>选择一篇帖子</strong><p>查看检查理由，补充来源和本人贡献。</p></div>'; return; }
  const finding = assessedPost(post.id);
  const reasons = finding?.reasons || [];
  const safeUrl = safeExternal(post.url);
  $('#post-detail').innerHTML = `<div class="panel-header"><div><div class="detail-kicker">${escapeHTML(post.id)} · ${escapeHTML(typeLabels[post.type])}</div><h3>材料与证据</h3></div><button class="button secondary small" data-edit="${escapeHTML(post.id)}">编辑资料</button></div><div class="detail-section"><span class="chip ${finding ? statusClass[finding.status] : 'unknown'}">${escapeHTML(finding?.status_label || '未评估')}</span>${safeUrl ? `<a class="post-link" href="${escapeHTML(safeUrl)}" target="_blank" rel="noreferrer">打开原帖 ↗</a>` : ''}<p class="post-text">${escapeHTML(post.text || '未提供正文')}</p></div><div class="detail-section"><h4>检查理由</h4>${reasons.length ? reasons.map(reason=>`<div class="evidence-item"><p>${escapeHTML(reason.message)}</p>${reason.evidence ? `<pre>${escapeHTML(typeof reason.evidence === 'string' ? reason.evidence : JSON.stringify(reason.evidence,null,2))}</pre>`:''}</div>`).join('') : `<p class="muted">${analysis ? '该帖未纳入本次窗口，请检查日期或补充发布时间。' : '运行评估后显示逐项理由。'}</p>`}</div><div class="detail-section"><h4>本人贡献</h4><p>${escapeHTML(post.contribution || '未说明')}</p><h4>创作证据</h4><p>${escapeHTML((post.evidence || []).map(item=>typeof item==='string'?item:JSON.stringify(item)).join('；') || '未提供')}</p></div><div class="detail-section"><h4>来源 ${post.sources?.length || 0} 项</h4>${(post.sources || []).map(source=>`<div class="source-card"><span class="tag">${escapeHTML(ownerLabel(source.owner))}</span><p>${escapeHTML(source.url || '未提供链接')}</p><p>${escapeHTML(source.text ? source.text.slice(0,250) : '未提供来源原文，未做文本对比')}</p></div>`).join('') || '<p class="muted">未提供来源，不能据此证明无外部重复。</p>'}<h4>媒体 ${post.media?.length || 0} 项</h4>${(post.media || []).map(media=>`<p>${escapeHTML(media.name)}<br><span class="muted">${media.hash ? `已提供文件哈希 · ${escapeHTML(media.hash.slice(0,12))}…` : '文件内容未提供'}</span></p>`).join('') || '<p class="muted">没有关联媒体。</p>'}</div>`;
}

function ownerLabel(owner) { return {self:'本人素材声明',other:'他人素材',third_party:'他人素材',mixed:'混合素材',unknown:'归属未知'}[owner] || '归属未知'; }
function safeExternal(value) { try { const u = new URL(value); return ['https:','http:'].includes(u.protocol) ? u.href : ''; } catch { return ''; } }

function renderCandidates() {
  const candidates = analysis?.candidates || [];
  const selected = new Set(project.selected_candidates);
  $('#selected-count').textContent = `已选 ${selected.size} / 10`;
  $('#select-recommended').disabled = !candidates.length;
  $('#export-candidates').disabled = !selected.size;
  $('#candidate-list').innerHTML = candidates.length ? candidates.map(candidate=> {
    const finding = assessedPost(candidate.id);
    return `<div class="candidate-card"><div class="candidate-number">${escapeHTML(candidate.rank)}</div><div class="candidate-body"><div class="post-meta"><strong>${escapeHTML(candidate.id)}</strong><span class="chip ${statusClass[finding?.status] || 'unknown'}">${escapeHTML(candidate.status_label)}</span></div><p>${escapeHTML(candidate.text.slice(0,200))}</p><p class="muted">${escapeHTML(finding?.candidate_reason || '材料较完整，可人工复核')}</p></div><label class="candidate-actions checkbox-label"><input type="checkbox" data-candidate="${escapeHTML(candidate.id)}" ${selected.has(candidate.id)?'checked':''}>选入</label></div>`;
  }).join('') : `<div class="empty-state"><strong>${analysis ? '暂未找到可推荐候选' : '评估后生成候选'}</strong><p>${analysis ? '先处理风险和材料缺口。没有足够材料时，不凑满 10 篇。' : '导入作品并补充证据，然后运行评估。'}</p></div>`;
  if (analysis && candidates.length < 10) $('#candidate-list').insertAdjacentHTML('beforeend', `<p class="muted">当前推荐 ${candidates.length} 篇，距离 10 篇还差 ${10-candidates.length} 篇。推荐数量不代表申请结果。</p>`);
  $('#selected-review').innerHTML = selected.size ? [...selected].map(id=> {
    const post = project.posts.find(item=>item.id===id), finding=assessedPost(id);
    return `<div class="selected-row"><div><strong>${escapeHTML(id)}</strong><p>${escapeHTML(post?.text.slice(0,90) || post?.url || '记录已缺失')}</p><span class="muted">${escapeHTML(finding?.candidate_eligible ? finding.candidate_reason : '当前材料变动或风险状态需要复核')}</span></div><button class="button ghost small" data-unselect="${escapeHTML(id)}">移出</button></div>`;
  }).join('') : '<p class="muted">尚未选入申请样本。</p>';
}

function nextId() { let n = 1; while (project.posts.some(post=>post.id===`P${String(n).padStart(3,'0')}`)) n++; return `P${String(n).padStart(3,'0')}`; }

function setCreationMethod(value = '') {
  const select = $('#edit-method');
  select.querySelectorAll('[data-imported-method]').forEach(option => option.remove());
  const method = String(value || '');
  if (method && ![...select.options].some(option => option.value === method)) {
    const option = document.createElement('option');
    option.value = method;
    option.textContent = method;
    option.dataset.importedMethod = 'true';
    select.append(option);
  }
  select.value = method;
}

function openEditor(id = '') {
  if (busy) return;
  const post = project.posts.find(item=>item.id===id);
  $('#edit-id').value=id;
  $('#edit-post-id').value=post?.id || nextId();
  $('#post-modal-title').textContent=post?'编辑帖子与证据':'添加帖子';
  $('#edit-type').value=post?.type || 'original';
  $('#edit-url').value=post?.url || '';
  $('#edit-created').value=post?.created_at || '';
  $('#edit-text').value=post?.text || '';
  $('#edit-text-complete').value=post?.text_complete === false ? 'no' : post?.text_complete === true || !post ? 'yes' : 'unknown';
  setCreationMethod(post?.creation_method);
  $('#edit-thread').value=post?.thread_id || '';
  $('#edit-thread-complete').value=post?.thread_complete === false ? 'no' : post?.thread_complete === true || !post?.thread_id ? 'yes' : 'unknown';
  $('#edit-contribution').value=post?.contribution || '';
  $('#edit-evidence').value=(post?.evidence || []).map(item=>typeof item==='string'?item:JSON.stringify(item)).join('\n');
  $('#edit-notes').value=post?.notes || '';
  $('#edit-error').textContent='';
  editingSources=structuredClone(post?.sources || []);
  editingMedia=structuredClone(post?.media || []);
  renderSourceEditors(); renderMediaEditors();
  $('#delete-post').hidden=!post;
  $('#post-modal').showModal();
}

function renderSourceEditors() {
  $('#source-editors').innerHTML=editingSources.map((source,index)=>`<div class="source-card source-editor" data-source-index="${index}"><div class="panel-header"><strong>来源 ${index+1}</strong><button class="button ghost small" type="button" data-remove-source="${index}">移除</button></div><label class="field">素材归属<select class="form-control" data-source-field="owner"><option value="unknown" ${source.owner==='unknown'?'selected':''}>不确定</option><option value="self" ${source.owner==='self'?'selected':''}>本人素材／跨平台发布</option><option value="other" ${['other','third_party'].includes(source.owner)?'selected':''}>他人来源</option><option value="mixed" ${source.owner==='mixed'?'selected':''}>混合素材</option></select></label><label class="field">来源链接<input class="form-control" data-source-field="url" value="${escapeHTML(source.url || '')}" placeholder="https://…"></label><label class="field">来源原文<textarea class="form-control" rows="3" data-source-field="text" placeholder="粘贴对比所需原文">${escapeHTML(source.text || '')}</textarea></label><label class="field">来源说明<input class="form-control" data-source-field="notes" value="${escapeHTML(source.notes || '')}"></label></div>`).join('') || '<p class="muted">暂未填写来源。</p>';
}

function readSourceEditors() {
  return $$('.source-editor').map(element=>Object.fromEntries($$('[data-source-field]',element).map(field=>[field.dataset.sourceField,field.value.trim()])));
}

function renderMediaEditors() {
  $('#media-editors').innerHTML=editingMedia.map((media,index)=>`<div class="selected-row"><div><strong>${escapeHTML(media.name)}</strong><div class="muted">${escapeHTML(media.kind)} · ${media.hash?'已计算文件哈希':'文件内容未提供'}</div></div><button class="button ghost small" type="button" data-remove-media="${index}">移除</button></div>`).join('') || '<p class="muted">暂未关联媒体。媒体文件保留在本机，不嵌入项目文件。</p>';
}

async function attachMedia(files) {
  $('#edit-error').textContent='';
  try {
    for (const file of files) {
      if (file.size>100*1024*1024) throw new Error(`${file.name} 超过 100 MB，请提供较小的对应媒体文件。`);
      const bytes=await file.arrayBuffer();
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
      const kind=file.type.startsWith('video/')?'video':file.type==='image/gif'?'animated_gif':'image';
      if (!editingMedia.some(media=>media.hash===hash && media.name===file.name)) editingMedia.push({name:file.name,hash,kind,hash_origin:'local_file'});
    }
    renderMediaEditors();
  } catch (error) { $('#edit-error').textContent=error.message; }
  $('#media-input').value='';
}

function savePost(event) {
  event.preventDefault();
  const oldId=$('#edit-id').value, id=$('#edit-post-id').value.trim();
  if (!id) { $('#edit-error').textContent='请填写帖子编号。'; return; }
  if (project.posts.some(post=>post.id===id && post.id!==oldId)) { $('#edit-error').textContent='帖子编号重复，请使用不同编号。'; return; }
  const existing=project.posts.find(post=>post.id===oldId);
  const post={...existing,id,url:$('#edit-url').value.trim(),text:$('#edit-text').value,created_at:$('#edit-created').value.trim(),type:$('#edit-type').value,thread_id:$('#edit-thread').value.trim(),thread_complete:$('#edit-thread-complete').value==='yes'?true:$('#edit-thread-complete').value==='no'?false:null,text_complete:$('#edit-text-complete').value==='yes'?true:$('#edit-text-complete').value==='no'?false:null,contribution:$('#edit-contribution').value.trim(),creation_method:$('#edit-method').value,evidence:$('#edit-evidence').value.split('\n').map(v=>v.trim()).filter(Boolean),sources:readSourceEditors(),media:editingMedia,notes:$('#edit-notes').value.trim()};
  if (!post.text && !post.url && !post.media.length) { $('#edit-error').textContent='至少提供正文、链接或媒体之一。'; return; }
  if (post.text.length>50000) { $('#edit-error').textContent='单篇正文最多 50,000 字符。'; return; }
  if (existing) project.posts[project.posts.indexOf(existing)]=post;
  else {
    if (project.posts.length>=2000) { $('#edit-error').textContent='第一版最多处理 2,000 条帖子。'; return; }
    project.posts.push(post);
  }
  project.selected_candidates=project.selected_candidates.map(value=>value===oldId?id:value);
  selectedPost=id;
  $('#post-modal').close();
  changeProject();
  navigate('posts');
  toast('帖子已保存，请重新运行评估。');
}

function download(content,filename,type) {
  const url=URL.createObjectURL(new Blob([content],{type}));
  const link=document.createElement('a'); link.href=url; link.download=filename; link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function filenameSuffix() { return (project.account.match(/[A-Za-z0-9_]+$/)?.[0] || '项目') + '_' + new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date()); }

function saveProject() {
  if (!saveScope(true)) return;
  download(JSON.stringify(project,null,2),`原作_${filenameSuffix()}.json`,'application/json;charset=utf-8');
  dirty=false; render(); toast('项目下载已发起。请保留 JSON 文件，下次导入继续。');
}

async function exportReport(format) {
  if (!(await runAnalysis(false))) return;
  setBusy(true);
  try {
    const result=await api('/api/report',{project,format});
    download(result.content,`原创评估_${filenameSuffix()}.${format==='html'?'html':'md'}`,format==='html'?'text/html;charset=utf-8':'text/markdown;charset=utf-8');
    toast('报告下载已发起，包含当前材料与最新评估。');
  } catch(error) { toast(error.message,true); }
  finally { setBusy(false); }
}

document.addEventListener('click',event=> {
  const nav=event.target.closest('[data-view]'); if(nav) navigate(nav.dataset.view);
  const go=event.target.closest('[data-go]'); if(go) navigate(go.dataset.go);
  const close=event.target.closest('[data-close]'); if(close) { $('#'+close.dataset.close).close(); if(close.dataset.close==='confirm-modal') pendingConfirm=null; }
  const row=event.target.closest('[data-post]'); if(row) { selectedPost=row.dataset.post; navigate('posts'); renderPosts(); }
  const edit=event.target.closest('[data-edit]'); if(edit) openEditor(edit.dataset.edit);
  const removeSource=event.target.closest('[data-remove-source]'); if(removeSource) { editingSources=readSourceEditors(); editingSources.splice(Number(removeSource.dataset.removeSource),1);renderSourceEditors(); }
  const removeMedia=event.target.closest('[data-remove-media]'); if(removeMedia) { editingMedia.splice(Number(removeMedia.dataset.removeMedia),1);renderMediaEditors(); }
  const unselect=event.target.closest('[data-unselect]'); if(unselect) { project.selected_candidates=project.selected_candidates.filter(id=>id!==unselect.dataset.unselect);dirty=true;render(); }
});

$('#confirm-form').addEventListener('submit',event=> {event.preventDefault();$('#confirm-modal').close();const action=pendingConfirm;pendingConfirm=null;if(action) action();});
$('#confirm-modal').addEventListener('cancel',()=>{pendingConfirm=null;});
$('#scope-form').addEventListener('submit',event=>{event.preventDefault();saveScope();});
$('#analyze').addEventListener('click',()=>runAnalysis());
$('#save-project').addEventListener('click',saveProject);
$('#export-project').addEventListener('click',saveProject);
$('#export-html').addEventListener('click',()=>exportReport('html'));
$('#export-md').addEventListener('click',()=>exportReport('markdown'));
$('#add-post').addEventListener('click',()=>openEditor());
$('#add-post-second').addEventListener('click',()=>openEditor());
$('#post-form').addEventListener('submit',savePost);
$('#post-search').addEventListener('input',renderPosts);
$('#post-filter').addEventListener('change',renderPosts);
$('#add-source').addEventListener('click',()=>{editingSources=readSourceEditors();editingSources.push({owner:'unknown',url:'',text:'',notes:''});renderSourceEditors();});
$('#attach-media').addEventListener('click',()=>$('#media-input').click());
$('#media-input').addEventListener('change',event=>attachMedia([...event.target.files]));
$('#delete-post').addEventListener('click',()=>{
  const id=$('#edit-id').value;
  $('#post-modal').close();
  confirmAction('移除帖子','只从当前项目移除这篇材料。需要保留时，请先保存 JSON 项目。',()=>{project.posts=project.posts.filter(post=>post.id!==id);selectedPost=project.posts[0]?.id || '';changeProject();toast('已移除帖子。');});
});

const zone=$('#drop-zone');
zone.addEventListener('click',()=>{if(!busy)$('#file-input').click();});
zone.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();if(!busy)$('#file-input').click();}});
zone.addEventListener('dragover',event=>{event.preventDefault();zone.classList.add('drag-over');});
zone.addEventListener('dragleave',()=>zone.classList.remove('drag-over'));
zone.addEventListener('drop',event=>{event.preventDefault();zone.classList.remove('drag-over');importFiles([...event.dataTransfer.files]);});
$('#file-input').addEventListener('change',event=>importFiles([...event.target.files]));
$('#paste-links').addEventListener('click',()=>{$('#links-text').value='';$('#links-modal').showModal();});
$('#links-form').addEventListener('submit',event=> {
  event.preventDefault();
  const urls=$('#links-text').value.split('\n').map(value=>value.trim()).filter(Boolean);
  if (!urls.length) return;
  const unique=[...new Set(urls)].filter(value=>!project.posts.some(post=>post.url===value));
  if (unique.some(value=>!/^https:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/[A-Za-z0-9_]+\/status\/\d+(?:[/?#].*)?$/.test(value))) { toast('请逐行填写有效的 X 帖子链接（包含 status/数字编号）。',true);return; }
  if(project.posts.length+unique.length>2000){toast('第一版最多登记 2,000 条。',true);return;}
  for(const url of unique){const id=nextId();project.posts.push({id,url,text:'',created_at:'',type:'original',sources:[],media:[],evidence:[],text_complete:false});}
  $('#links-modal').close();selectedPost=project.posts.at(-1)?.id || '';changeProject();navigate('posts');toast(`登记 ${unique.length} 个链接。请补充正文和媒体后评估。`);
});

async function loadDemo() {
  setBusy(true);
  try {
    const response=await fetch(browserDeployment?'./sample-data.json':'/api/sample');if(!response.ok)throw new Error('示例文件未找到。');
    project=await response.json();dirty=true;analysis=null;selectedPost=project.posts[0]?.id || '';syncScopeForm();render();
  } catch(error){toast(error.message,true);}
  finally{setBusy(false);}
  await runAnalysis(false);navigate('overview');toast('已载入虚构示例，用于体验检查流程。');
}
$('#load-demo').addEventListener('click',()=>{if(project.posts.length)confirmAction('载入示例','当前材料会被示例替换。需要保留时，请先保存 JSON 项目。',loadDemo);else loadDemo();});
$('#download-template').addEventListener('click',async()=>{try{const response=await fetch(browserDeployment?'./template.md':'/api/template');if(!response.ok)throw new Error('提交表暂不可用。');download(await response.text(),'X原创检测提交表模板.md','text/markdown;charset=utf-8');}catch(error){toast(error.message,true);}});
$('#show-guide').addEventListener('click',async()=>{try{const response=await fetch(browserDeployment?'./guide.md':'/api/guide');if(!response.ok)throw new Error('提交说明暂不可用。');$('#guide-text').textContent=browserDeployment?await response.text():(await response.json()).text;$('#guide-modal').showModal();}catch(error){toast(error.message,true);}});
$('#candidate-list').addEventListener('change',event=> {
  const input=event.target.closest('[data-candidate]');if(!input)return;
  if(input.checked && project.selected_candidates.length>=10){input.checked=false;toast('最多选择 10 篇。',true);return;}
  project.selected_candidates=input.checked?[...new Set([...project.selected_candidates,input.dataset.candidate])]:project.selected_candidates.filter(id=>id!==input.dataset.candidate);
  dirty=true;render();
});
$('#select-recommended').addEventListener('click',()=>{project.selected_candidates=(analysis?.candidates || []).slice(0,10).map(post=>post.id);dirty=true;render();toast(`已选 ${project.selected_candidates.length} 篇推荐候选。`);});
$('#export-candidates').addEventListener('click',()=>{
  const content=project.selected_candidates.map(id=>{const post=project.posts.find(item=>item.id===id);return `${id}\t${post?.url || '未提供链接'}`;}).join('\n');
  download(content,`申请候选_${filenameSuffix()}.txt`,'text/plain;charset=utf-8');toast('已选候选链接下载已发起。');
});
$('#reset-project').addEventListener('click',()=>confirmAction('清空当前项目','当前浏览器中的材料会清空。需要保留时，请先保存 JSON 项目。',()=>{project=freshProject();analysis=null;selectedPost='';dirty=false;syncScopeForm();render();toast('当前项目已清空。');navigate('materials');}));
$('#shutdown').addEventListener('click',()=>{
  if (browserDeployment && !engineReady) { startBrowserEngine(); return; }
  confirmAction(browserDeployment?'停止浏览器引擎':'停止本机服务',browserDeployment?'评估引擎将停止，当前页面的材料会保留。需要时可重新启动；关闭或刷新前请先保存 JSON 项目。':'后台服务将停止。尚未保存的材料请先下载 JSON 项目；下次双击启动器即可重新打开。',async()=>{
    try {
      await api('/api/shutdown',{});localToken='';engineReady=false;setBusy(false);
      $('#connection-state').textContent=browserDeployment?'浏览器引擎已停止':'服务已停止';
      if(browserDeployment) $('#shutdown').textContent='重新启动引擎';
      toast(browserDeployment?'引擎已停止，材料仍在当前页面。':'本机服务已停止，可以关闭此页面。');
    } catch(error){toast(error.message,true);}
  });
});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});

async function init() {
  syncScopeForm();render();setBusy(true);
  if (browserDeployment) {
    $('#runtime-title').textContent='浏览器内运行';
    $('#runtime-note').textContent='材料只在你的浏览器处理。关闭或刷新前下载 JSON 保存项目。';
    $('#shutdown').textContent='停止浏览器引擎';
    await startBrowserEngine();
    return;
  }
  try {
    const response=await fetch('/api/health');const result=await response.json();if(!result.ok)throw new Error();
    localToken=result.token;engineReady=true;$('#connection-state').textContent=`服务已连接 · v${result.version}`;
  } catch {$('#connection-state').textContent='连接失败，请重新运行启动器';toast('无法连接本机服务。请重新打开启动器。',true);}
  finally{setBusy(false);}
}
async function startBrowserEngine() {
  setBusy(true);engineReady=false;$('#shutdown').disabled=true;
  try {
    const result=await window.OriginalityRuntime.init(message=>{$('#connection-state').textContent=String(message);});
    engineReady=true;$('#connection-state').textContent=`浏览器引擎已就绪 · v${result.version}`;
    $('#shutdown').textContent='停止浏览器引擎';
  } catch(error) {
    $('#connection-state').textContent='加载失败，当前材料仍保留';
    $('#shutdown').textContent='重新启动引擎';
    toast(error.message || '运行组件加载失败，请检查网络后重新启动引擎。',true);
  } finally {$('#shutdown').disabled=false;setBusy(false);}
}
init();
