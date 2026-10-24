/* ModelProof Web —— Cloudscape 风格渲染，原生 JS，零构建。 */

const MP = (() => {

  /* ---------------------------------------------------------------- 常量 */

  const LAYER_LABEL = {
    vendor: '厂商',
    family: '家族',
    series: '系列',
    version: '版本',
  };

  const GW_LABEL = {
    official_direct: '官方直连',
    official_cloud: '官方云网关',
    subscription_relay: '订阅反代',
    third_party_relay: '第三方中转',
    unknown: '无法判定',
  };

  const GW_TONE = {
    official_direct: 'green',
    official_cloud: 'blue',
    subscription_relay: 'amber',
    third_party_relay: 'red',
    unknown: '',
  };

  const STATUS_CLASS = {
    success: 'cs-status--success',
    error: 'cs-status--error',
    warning: 'cs-status--warning',
    info: 'cs-status--info',
    pending: 'cs-status--pending',
  };

  const TRUTH_TONE = {
    consistent: 'success',
    suspicious: 'warning',
    inconsistent: 'error',
    unknown: 'info',
  };

  const TRUTH_LABEL = {
    consistent: '与声明一致',
    suspicious: '存疑',
    inconsistent: '与声明不符',
    unknown: '无法判定',
  };

  /* 通道 ID → 中文名（证据表用） */
  const CHANNEL_LABEL = {
    tokenizer: '词表指纹',
    backend_fp: '后端指纹',
    gateway: '接入链路',
    behavior: '行为特征',
    distribution: '数字分布指纹',
    onetoken: '单 token 分布指纹',
    capability: '能力悬崖',
    style: '排版风格',
    trace: '调用链 trace',
    validity: '结果验真',
    consistency: '请求一致性',
  };

  /* 阶段标识 → 中文名（进度条下方的阶段圆点用） */
  const PHASE_LABEL = {
    prepare: '准备',
    protocol: '协议探测',
    tokenizer: '词表指纹',
    behavior: '行为特征',
    gateway: '链路探测',
    trace: '调用链 trace',
    validity: '结果验真',
    backend_fp: '后端指纹',
    distribution: '数字分布指纹',
    onetoken: '单 token 分布指纹',
    capability: '能力悬崖',
    style: '排版风格',
    consistency: '请求一致性',
    finalize: '汇总判定',
  };

  /* ------------------------------------------------------------ 小工具 */

  const $ = (id) => document.getElementById(id);

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function pct(value) {
    return Math.round((value || 0) * 100);
  }

  /* ------------------------------------------------- Cloudscape 组件片段 */

  function container(title, desc, content, footer) {
    return (
      '<section class="cs-container">' +
        '<div class="cs-container__header">' +
          `<h2>${title}</h2>` +
          (desc ? `<p class="cs-container__desc">${desc}</p>` : '') +
        '</div>' +
        `<div class="cs-container__content">${content}</div>` +
        (footer ? `<div class="cs-container__footer">${footer}</div>` : '') +
      '</section>'
    );
  }

  function status(kind, text) {
    return `<span class="cs-status ${STATUS_CLASS[kind] || ''}">${text}</span>`;
  }

  function badge(text, tone = '') {
    const cls = tone ? ` cs-badge--${tone}` : '';
    return `<span class="cs-badge${cls}">${esc(text)}</span>`;
  }

  function alert(kind, body, icon) {
    const marks = { info: 'i', success: '✓', warning: '!', error: '×' };
    return (
      `<div class="cs-alert cs-alert--${kind}">` +
        `<span class="cs-alert__icon">${icon || marks[kind] || 'i'}</span>` +
        `<div class="cs-alert__body">${body}</div>` +
      '</div>'
    );
  }

  function table(headers, rows, empty = '（无）') {
    if (!rows.length) return `<div class="cs-table__empty">${empty}</div>`;
    const th = headers.map((h) => `<th>${h}</th>`).join('');
    const body = rows.join('');
    return (
      '<div class="cs-table-wrap"><table class="cs-table">' +
        `<thead><tr>${th}</tr></thead><tbody>${body}</tbody>` +
      '</table></div>'
    );
  }

  function columns(boxes) {
    return `<div class="cs-columns">${boxes.join('')}</div>`;
  }

  function box(label, value, note) {
    return (
      '<div class="cs-box">' +
        `<div class="cs-box__label">${esc(label)}</div>` +
        `<div class="cs-box__value">${value}</div>` +
        (note ? `<div class="cs-box__note">${esc(note)}</div>` : '') +
      '</div>'
    );
  }

  /* -------------------------------------------- 数量与大类（界面不再列明细） */

  // 目录只拉一次：上下限、默认值、取项顺序。前端靠 order 的前 N 项自己算出覆盖的大类，
  // 这样改数量是纯本地的，不用每次回服务端问一遍。
  let _catalog = null;

  async function getCatalog() {
    if (_catalog) return _catalog;
    try {
      _catalog = await (await fetch('/v1/catalog')).json();
    } catch (err) {
      _catalog = null;
    }
    return _catalog;
  }

  /** 数量输入框读值：留空表示「用默认值」，交给后端 clamp。 */
  function readCount(id) {
    const el = $(id);
    if (!el || el.value === '') return null;
    const n = parseInt(el.value, 10);
    return isNaN(n) ? null : n;
  }

  /** 当前测试模式：manual | auto（auto 下再分 quick / category）。 */
  function currentMode() {
    const el = document.querySelector('input[name="test-mode"]:checked');
    return el ? el.value : 'auto';
  }

  function currentAutoMode() {
    const el = document.querySelector('input[name="auto-mode"]:checked');
    return el ? el.value : 'quick';
  }

  /** 手动模式勾选到的测试项 id。 */
  function pickedManualItems() {
    return Array.from(document.querySelectorAll('#manual-list input:checked')).map((i) => i.value);
  }

  /** 自动·特定项目勾选到的大类 id。 */
  function pickedCategories() {
    return Array.from(document.querySelectorAll('#category-list input:checked')).map((i) => i.value);
  }

  /** 按当前模式组装 payload 里的选段。 */
  function selectionPayload() {
    if (currentMode() === 'manual') {
      const items = pickedManualItems();
      return { items: items.length ? items.join(',') : null };
    }
    if (currentAutoMode() === 'category') {
      const cats = pickedCategories();
      return { categories: cats.length ? cats : null };
    }
    return { item_count: readCount('f-item-count') };
  }

  function itemCheckbox(item, checked) {
    return (
      `<label class="cs-item${checked ? ' is-checked' : ''}" data-id="${esc(item.id)}">` +
        `<input type="checkbox" value="${esc(item.id)}"${checked ? ' checked' : ''}>` +
        '<span class="cs-item__body">' +
          `<span class="cs-item__title">${esc(item.name)}</span>` +
          (item.description ? `<span class="cs-item__desc">${esc(item.description)}</span>` : '') +
          `<span class="cs-item__meta">${item.requests} 次请求 · <code>${esc(item.id)}</code></span>` +
        '</span>' +
      '</label>'
    );
  }

  function categoryCheckbox(cat, checked) {
    return (
      `<label class="cs-item${checked ? ' is-checked' : ''}" data-id="${esc(cat.id)}">` +
        `<input type="checkbox" value="${esc(cat.id)}"${checked ? ' checked' : ''}>` +
        '<span class="cs-item__body">' +
          `<span class="cs-item__title">${esc(cat.name)}</span>` +
          `<span class="cs-item__meta">${cat.items} 项 · ${cat.requests} 次请求</span>` +
        '</span>' +
      '</label>'
    );
  }

  /** 渲染手动列表（按大类分组，默认勾选硬信号那几项）。 */
  function renderManualList(meta) {
    const host = $('manual-list');
    if (!host || !meta) return;
    const hard = new Set(['tokenizer', 'backend_fp', 'distribution']);
    host.innerHTML = (meta.categories || []).map((cat) =>
      '<div class="cs-section">' +
        `<h3>${esc(cat.name)}</h3>` +
        '<div class="cs-item-list">' +
          (cat.members || []).map((it) =>
            itemCheckbox(it, hard.has(cat.id))
          ).join('') +
        '</div>' +
      '</div>'
    ).join('');
    bindCheckRows(host);
  }

  function renderCategoryList(meta) {
    const host = $('category-list');
    if (!host || !meta) return;
    const hard = new Set(['tokenizer', 'backend_fp', 'distribution']);
    host.innerHTML = (meta.categories || [])
      .map((cat) => categoryCheckbox(cat, hard.has(cat.id)))
      .join('');
    bindCheckRows(host);
  }

  /** 勾选后高亮所在卡片，并刷新计数器。 */
  function bindCheckRows(host) {
    if (!host) return;
    const refresh = () => host.querySelectorAll('.cs-item').forEach((row) => {
      const cb = row.querySelector('input');
      if (cb) row.classList.toggle('is-checked', cb.checked);
    });
    host.addEventListener('change', refresh);
    refresh();
  }

  function syncModePanels() {
    const manual = currentMode() === 'manual';
    const el = (id) => $(id);
    if (el('panel-auto')) el('panel-auto').style.display = manual ? 'none' : '';
    if (el('panel-manual')) el('panel-manual').style.display = manual ? '' : 'none';
    const hint = el('mode-hint');
    if (hint) {
      hint.textContent = manual
        ? '手动：自己挑要跑的项目 —— 适合只想补某一条证据，或复算特定通道。'
        : '自动：工具按通道优先级替你挑 —— 硬信号与零成本通道排在前。';
    }
    const category = !manual && currentAutoMode() === 'category';
    if (el('field-quick')) el('field-quick').style.display = category ? 'none' : '';
    if (el('field-category')) el('field-category').style.display = category ? '' : 'none';
    if (el('item-coverage')) el('item-coverage').style.display = category ? 'none' : '';
  }

  /** 把数量夹到 [min, max]，非法输入回落到默认值。 */
  function clampCount(value, meta, fallback) {
    const lo = (meta && meta.min) || 1;
    const hi = (meta && meta.max) || lo;
    const n = parseInt(value, 10);
    if (isNaN(n)) return fallback || hi;
    return Math.min(hi, Math.max(lo, n));
  }

  /** 取 order 的前 N 项，按大类聚合 —— 这就是「N 项覆盖哪些大类」的答案。 */
  function coverageOf(order, n) {
    const picked = (order || []).slice(0, n);
    const seen = [];
    picked.forEach((it) => {
      const key = it.name || it.channel;
      const hit = seen.find((c) => c.name === key);
      if (hit) hit.count += 1;
      else seen.push({ name: key, count: 1 });
    });
    return seen;
  }

  function renderCoverage(hostId, meta, n) {
    const host = $(hostId);
    if (!host || !meta) return;
    const total = (meta.order || []).length;
    const cats = coverageOf(meta.order, n);
    host.innerHTML =
      '<div class="cs-section">' +
        `<h3>${cats.length} 个大类 · 共 ${n} / ${total} 项</h3>` +
        '<div class="cs-badge-row">' +
          cats.map((c) => badge(c.count > 1 ? `${c.name} · ${c.count} 项` : c.name, 'blue')).join('') +
        '</div>' +
      '</div>';
  }

  /** 把基准库里带指纹的条目填进「对照模型」单选框。
   *
   * 所有数据来源已经融合进默认库，所以不再有库切换，固定读默认库。
   */
  async function loadCandidates() {
    const box = $('f-candidates');
    if (!box) return;
    try {
      const data = await (await fetch('/v1/baselines')).json();
      const entries = (data.entries || []).slice().sort((a, b) => {
        const left = `${a.identity.vendor}/${a.identity.family}`;
        const right = `${b.identity.vendor}/${b.identity.family}`;
        return left.localeCompare(right) || a.model_id.localeCompare(b.model_id);
      });
      // 首项恒为「不选」：留空 = 在整个数据来源里自由比对
      box.innerHTML =
        '<option value="" selected>（不选 · 在所选数据来源内自由比对）</option>' +
        (entries.map((e) => {
          const label = `${e.identity.vendor}/${e.identity.family} · ${e.model_id}`;
          return `<option value="${esc(e.model_id)}">${esc(label)}</option>`;
        }).join('') || '<option value="" disabled>该库还没有条目</option>');
    } catch (err) {
      box.innerHTML = '<option value="" selected>（不选 · 在所选数据来源内自由比对）</option>';
    }
  }

  /** 对照模型只允许一个：单选，不选就返回空数组（= 库内自由比对）。 */
  function pickedCandidates() {
    const box = $('f-candidates');
    if (!box || !box.value) return [];
    return [box.value];
  }

  /* ------------------------------------------------------------- 预估 */

  async function estimate() {
    const res = await fetch('/v1/estimates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign(selectionPayload(), {
        model: $('f-model').value,
        reasoning: $('f-reasoning').checked,
      })),
    });
    const data = await res.json();
    const e = data.estimate || {};
    const inTok = e.input_tokens || [0, 0];
    const outTok = e.output_tokens || [0, 0];
    const secs = e.seconds || [0, 0];

    const cats = (data.categories || []).map((c) => c.name).join(' · ') || '—';
    const html =
      '<dl class="cs-kv">' +
        `<dt>测试项数量</dt><dd>${data.item_count || 0} 项</dd>` +
        `<dt>测试大类</dt><dd>${esc(cats)}</dd>` +
        `<dt>请求数</dt><dd>${e.requests || 0}</dd>` +
        `<dt>输入 token</dt><dd>${inTok[0]} – ${inTok[1]}</dd>` +
        `<dt>输出 token</dt><dd>${outTok[0]} – ${outTok[1]}</dd>` +
        `<dt>预计耗时</dt><dd>${secs[0]}s – ${secs[1]}s</dd>` +
      '</dl>';

    $('estimate-box').style.display = 'block';
    $('estimate-box').innerHTML = html;
  }

  /* ------------------------------------------------------------- 运行 */

  async function run() {
    const base = $('f-base').value.trim();
    if (!base) return void showStatus('请填写端点 Base URL', true);

    // 判定层级固定打到最后一层、没有声称身份、不设费用预算 —— 这几个旋钮
    // 已经从界面移除，一律用后端默认口径。
    const payload = Object.assign({
      base_url: base,
      api_key: $('f-key').value,
      model: $('f-model').value.trim(),
      reasoning: $('f-reasoning').checked,
      max_seconds: $('f-maxsec').value ? parseFloat($('f-maxsec').value) : null,
      candidates: pickedCandidates(),
      async: 1,
    }, selectionPayload());

    $('btn-run').disabled = true;
    showStatus('');
    $('result').innerHTML = '';
    const resumeNode = $('resume');
    if (resumeNode) resumeNode.innerHTML = '';
    resetPhases();
    showProgress('正在提交…');

    const res = await fetch('/v1/verifications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const created = await res.json();
    if (!res.ok) {
      showProgress('');
      $('btn-run').disabled = false;
      return void showStatus(created.error || '提交失败', true);
    }
    await poll(created.id);
  }

  function showStatus(text, bad) {
    const node = $('run-status');
    if (!node) return;
    node.textContent = text;
    node.style.color = bad ? 'var(--color-text-error)' : '';
  }

  /* ------------------------------------------------------------- 进度条 */

  let _phases = [];

  function resetPhases() { _phases = []; }

  function notePhases(p) {
    if (!p || !p.phase) return;
    if (_phases.indexOf(p.phase) === -1) _phases.push(p.phase);
  }

  /** 阶段圆点：跑过的打勾，当前的高亮。只在多于一个阶段时才显示。 */
  function phaseSteps(p) {
    if (_phases.length < 2) return '';
    const chips = _phases.map((ph, i) => {
      const last = i === _phases.length - 1;
      const cls = last ? (p && p.done ? 'is-done' : 'is-active') : 'is-done';
      return `<span class="cs-step ${cls}"><span class="cs-step__dot"></span>${esc(PHASE_LABEL[ph] || ph)}</span>`;
    }).join('');
    return `<div class="cs-bar__steps">${chips}</div>`;
  }

  /** 完整进度条：阶段名 + 百分比 + 当前项名称 + 已完成/总数。 */
  function progressBar(p) {
    const value = Math.round(p.pct || 0);
    const cls = p.done ? ' is-done' : '';
    return (
      `<div class="cs-bar${cls}">` +
        '<div class="cs-bar__head">' +
          `<span class="cs-bar__label">${esc(p.label || p.phase || '准备中')}</span>` +
          `<span class="cs-bar__value"><b>${value}</b>%${p.done ? ' · 完成' : ''}</span>` +
        '</div>' +
        `<div class="cs-bar__track"><i style="width:${value}%"></i></div>` +
        '<div class="cs-bar__meta">' +
          (p.item
            ? `<span class="cs-bar__item">正在测：${esc(p.item)}</span>`
            : '<span class="cs-bar__item cs-bar__empty">—</span>') +
          (p.counter ? `<span class="cs-bar__counter">${esc(p.counter)}</span>` : '') +
        '</div>' +
        (p.detail ? `<div class="cs-bar__detail">${esc(p.detail)}</div>` : '') +
        phaseSteps(p) +
      '</div>'
    );
  }

  /**
   * 渲染进度。传结构化对象走进度条，传字符串走「一行提示」。
   */
  function showProgress(p, hostId) {
    const node = $(hostId || 'progress');
    if (!node) return;
    if (!p) return void (node.innerHTML = '');
    if (typeof p === 'string') {
      node.innerHTML =
        `<div class="cs-progress-row"><span class="cs-spinner"></span><span>${esc(p)}</span></div>`;
      return;
    }
    notePhases(p);
    node.innerHTML = progressBar(p);
  }

  /**
   * 通用轮询。onProgress 收结构化 progress；结束后交给 onDone。
   */
  async function pollTask(id, opts = {}) {
    const url = opts.url || `/v1/verifications/${id}`;
    window._taskId = id;
    for (let i = 0; i < 1200; i++) {
      const task = await (await fetch(url)).json();
      if (task.status === 'running') {
        if (opts.onProgress) opts.onProgress(task.progress || {}, opts.host);
        await sleep(500);
        continue;
      }
      if (opts.onProgress) opts.onProgress(null, opts.host);
      if (opts.onDone) opts.onDone(task);
      return task;
    }
    if (opts.onProgress) opts.onProgress(null, opts.host);
    if (opts.onDone) opts.onDone({ status: 'timeout' });
    return null;
  }

  async function poll(id) {
    $('btn-run').disabled = true;
    const task = await pollTask(id, {
      onProgress: (p) => showProgress(p),
      onDone: (t) => {
        $('btn-run').disabled = false;
        if (t.status === 'failed') return void showStatus('运行失败：' + (t.error || '未知错误'), true);
        if (t.status === 'timeout') return void showStatus('等待超时', true);
        if (t.report) renderReport(t.report);
        showStatus('');
      },
    });
    return task;
  }

  async function loadReport(id) {
    return pollTask(id, {
      onProgress: (p) => showProgress(p),
      onDone: (t) => { if (t.report) renderReport(t.report); },
    });
  }

  /**
   * 页面回到验证页时，把仍在跑的任务接回来继续轮询。
   * 任务状态存在服务端磁盘上，所以这里只需要一个 task_id。
   */
  async function resume() {
    try {
      const data = await (await fetch('/v1/tasks/active')).json();
      const running = (data.tasks || []).filter((t) => t.kind === 'verification');
      if (!running.length) return;
      const target = running[0];
      const detail = await (await fetch(`/v1/history/${target.id}`)).json();
      restoreForm((detail.task || {}).input || {});
      const node = $('resume');
      if (node) {
        node.innerHTML = alert('info',
          `检测到仍未完成的验证任务 <code>${esc(target.id)}</code>，已自动恢复进度。`);
      }
      $('btn-run').disabled = true;
      await poll(target.id);
      if (node) node.innerHTML = '';
    } catch (err) {
      /* 恢复失败不影响正常使用 */
    }
  }

  /** 用任务里记下的输入回填表单（密钥不会落盘，所以填不回来）。 */
  function restoreForm(input) {
    const set = (id, value) => { const el = $(id); if (el && value) el.value = value; };
    set('f-base', input.base_url);
    set('f-model', input.model);
    // 回填后要重新拉一次候选列表，否则 restoreForm 里的赋值会落在空列表上。
    // 对照模型只有一个，取第一个即可。
    loadCandidates().then(() => {
      const wanted = input.candidates || input.include || [];
      if (!wanted.length) return;
      const box = $('f-candidates');
      if (box) box.value = wanted[0];
    });
    // 恢复选法：手动回勾具体项，特定项目回勾大类，快速回填数量
    if (input.items && typeof input.items === 'string') {
      const wanted = new Set(input.items.split(','));
      const manual = document.querySelector('input[name="test-mode"][value="manual"]');
      if (manual) { manual.checked = true; }
      document.querySelectorAll('#manual-list input').forEach((cb) => {
        cb.checked = wanted.has(cb.value);
        const row = cb.closest('.cs-item');
        if (row) row.classList.toggle('is-checked', cb.checked);
      });
    } else if (input.categories && input.categories.length) {
      const wanted = new Set(input.categories);
      const cat = document.querySelector('input[name="auto-mode"][value="category"]');
      if (cat) cat.checked = true;
      document.querySelectorAll('#category-list input').forEach((cb) => {
        cb.checked = wanted.has(cb.value);
        const row = cb.closest('.cs-item');
        if (row) row.classList.toggle('is-checked', cb.checked);
      });
    } else if (input.item_count) {
      set('f-item-count', String(input.item_count));
    }
    syncModePanels();
    refreshItemCoverage();
  }

  /* ------------------------------------------------------- 报告渲染 */

  function renderReport(report, hostId) {
    const host = $(hostId || 'result');
    if (!host) return;
    window._lastReport = report;
    const tid = window._taskId || '';
    const exports =
      '<div class="cs-btn-row">' +
        '<button class="cs-btn cs-btn--normal" onclick="MP.downloadJson()">下载 report.json</button>' +
        (tid ? '<a class="cs-btn cs-btn--link" href="/v1/history/' + tid + '/export?format=md">导出 Markdown</a>' : '') +
        (tid ? '<a class="cs-btn cs-btn--link" href="/v1/history/' + tid + '/export?format=json">导出 JSON</a>' : '') +
        '<a class="cs-btn cs-btn--link" href="/v1/verifications/' + tid + '">查看原始 JSON</a>' +
      '</div>';
    host.innerHTML = [
      renderTopline(report),
      renderMultiBackend(report),
      renderRanking(report),
      renderRecommendations(report),
      // 以下都属细节：第一屏只回答「像不像」，其余收进展开，
      // 想复核的人自己点开，不想看的人不被表格埋掉。
      '<details class="cs-details">' +
        '<summary>测试过程与证据明细</summary>' +
        '<div class="cs-details__body">' +
          [
            renderSampleTags(report),
            renderState(report),
            renderVerdict(report),
            renderConsistency(report),
            report.gateway ? renderGateway(report.gateway) : '',
            renderTruth(report),
            renderLineage(report),
            renderOpenset(report),
            renderLeads(report),
            renderValidity(report),
            renderConflicts(report),
            renderEvidence(report),
            renderCost(report),
            renderWarnings(report),
          ].join('') +
        '</div>' +
      '</details>' +
      container('导出', '同一份 JSON 可被 CLI 与 REST API 直接读入。', exports),
    ].join('');
  }

  /**
   * 第一屏结论。对齐 ModelTrace 的结果页布局：四个指标卡 + 排名表，
   * 让人一眼看到「最可能是谁、概率多少、哪个家族、用了多少样本」。
   *
   * 与 ModelTrace 的唯一差别：**概率旁边永远跟着一句「不是确证」**。
   * 闭集 softmax 必然归一到 1，把 84% 直接念成「就是它」正是 ModelTrace
   * 仓库 issue #34 里 91.7%–99.9% 假报「掉包」的来路。
   */
  function renderTopline(report) {
    const v = report.verdict || {};
    const dist = (report.evidence || []).find((e) => e.channel === 'distribution');
    const robust = ((dist || {}).metric || {}).robust || {};
    const top = (robust.top || [])[0] || null;
    const familyTop = (robust.family_top || [])[0] || null;
    const beh = (report.evidence || []).find((e) => e.channel === 'behavior');
    const vector = ((beh || {}).metric || {}).vector || {};
    const claimed = (report.input || {}).model || '';
    const samples = ((dist || {}).metric || {}).samples || 0;
    const calibrated = (robust.calibration || {}).queries || '—';

    const tone = v.state === 'MATCH' ? 'success'
      : v.state === 'MISMATCH' ? 'error' : 'warning';
    const headline = {
      MATCH: '与声明一致',
      MISMATCH: '与声明不符',
      INCONCLUSIVE: '证据不足，无法确证',
    }[v.state] || v.state || '—';

    // 四个卡片：最可能模型 / 归因概率 / 识别家族 / 有效查询
    const answerModel = v.state === 'MATCH' || v.state === 'MISMATCH'
      ? (v.basis || '—')
      : (top ? top.model : (vector.identity ? `自述 ${vector.identity}` : '无可用观测'));
    const answerProb = v.state === 'MATCH' || v.state === 'MISMATCH'
      ? '—' : (top ? (top.probability * 100).toFixed(1) + '%' : '—');
    const answerFamily = familyTop
      ? `${familyTop.family} · ${(familyTop.probability * 100).toFixed(1)}%`
      : '—';
    const answerSamples = `${samples}/${calibrated}`;

    const notes = [];
    if (v.state === 'INCONCLUSIVE') {
      notes.push('概率是**闭集内**的排序结果（softmax 必然归一到 1），只说明「这批候选里最像」，不是确证。');
    }
    if (top && claimed && top.model !== claimed
        && v.state !== 'MATCH' && v.state !== 'MISMATCH') {
      notes.push(`与声称的 <code>${esc(claimed)}</code> 不是同一个候选。`);
    }
    if (robust.gap_ratio != null && robust.gap_ratio < 0.05 && (robust.top || []).length > 1) {
      notes.push(
        `前两名差距仅 ${(robust.gap_ratio * 100).toFixed(1)}%` +
        `（第二名 ${esc(robust.top[1].model)}），分布上几乎并列 —— 排名不能当分辨。`
      );
    }
    if (robust.out_of_scope && robust.best_score != null) {
      notes.push(
        `得分 ${robust.best_score.toFixed(2)} 未过库外阈值 ${robust.out_of_scope_threshold}，按库外处理。`
      );
    }

    const body =
      '<div class="cs-topline">' +
        '<div class="cs-btn-row" style="margin-bottom:var(--space-s)">' +
          `<span class="cs-status ${STATUS_CLASS[tone]}">${esc(headline)}</span>` +
          (claimed ? `<span class="cs-muted-s">被测模型 ${esc(claimed)}</span>` : '') +
        '</div>' +
        columns([
          box('最可能模型', esc(answerModel),
              top && top.family ? `家族 ${esc(top.family)}` : ''),
          box('归因概率', esc(answerProb),
              v.state === 'INCONCLUSIVE' ? '闭集内相对值' : ''),
          box('识别家族', esc(answerFamily), '同家族候选概率合计'),
          box('有效查询', esc(answerSamples), `概率标定按 ${calibrated} 份`),
        ]) +
        (notes.length
          ? '<ul class="cs-topline__warnings">' +
            notes.map((n) => `<li>${n}</li>`).join('') + '</ul>'
          : '') +
      '</div>';

    return container('结论', '', body);
  }

  /** 候选排名表：对齐 ModelTrace 的排名视图（概率条 + 分布相似度）。 */
  function renderRanking(report) {
    const dist = (report.evidence || []).find((e) => e.channel === 'distribution');
    const robust = ((dist || {}).metric || {}).robust || {};
    const rows = (robust.top || []);
    if (!rows.length) return '';
    const max = Math.max.apply(null, rows.map((r) => r.probability || 0)) || 1;
    const body = rows.map((r, index) => {
      const pct = ((r.probability || 0) * 100);
      const width = ((r.probability || 0) / max) * 100;
      return (
        '<tr>' +
          `<td class="cs-nowrap">${index + 1}</td>` +
          `<td><code>${esc(r.model)}</code></td>` +
          `<td>${esc(r.family || '—')}</td>` +
          '<td class="cs-rank">' +
            '<div class="cs-rank__track">' +
              `<i style="width:${width.toFixed(1)}%"></i>` +
            '</div>' +
            `<span class="cs-rank__pct">${pct.toFixed(1)}%</span>` +
          '</td>' +
          `<td class="cs-nowrap">${r.marginal_score != null ? r.marginal_score.toFixed(3) : '—'}</td>` +
        '</tr>'
      );
    }).join('');
    const note = robust.out_of_scope
      ? '这些候选都**没有过库外阈值**，所以整张表只是「谁相对更像」，不构成命中。'
      : '概率为闭集 softmax 结果；分布相似度越高代表边际直方图越接近。';
    return container(
      '候选排名',
      note,
      table(['排序', '候选模型', '家族', '归因概率', '边际分'], body)
    );
  }

  /** 测试过程标签行：每次采样要了多少个数字、是否计入。 */
  function renderSampleTags(report) {
    const dist = (report.evidence || []).find((e) => e.channel === 'distribution');
    const metric = (dist || {}).metric || {};
    const diags = metric.diagnostics || [];
    if (!diags.length) return '';
    const tags = diags.map((d, index) => {
      const cls = d.accepted ? 'done' : (d.ok ? 'invalid' : 'error');
      const label = d.accepted ? '计入' : (d.ok ? '数字不足' : '接口失败');
      return `<span class="cs-tag cs-tag--${cls}">` +
        `<b>${index + 1}</b> · ${d.expected || d.asked || 0} 个数字 · ${label}</span>`;
    }).join('');
    const ok = diags.filter((d) => d.accepted).length;
    return container(
      '采样明细',
      `共 ${diags.length} 次尝试，${ok} 次计入。`,
      `<div class="cs-tag-row">${tags}</div>`
    );
  }

  /** 顶层三态裁决。给人的第一句话：对得上 / 对不上 / 证据不够。 */
  function renderState(report) {
    const v = report.verdict;
    if (!v) return '';
    const tone = v.state === 'MATCH' ? 'success'
      : v.state === 'MISMATCH' ? 'error' : 'warning';
    const label = {
      MATCH: '与声明一致',
      MISMATCH: '与声明不符',
      INCONCLUSIVE: '证据不足，无法判定',
    }[v.state] || v.state;

    const body =
      '<div class="cs-btn-row" style="margin-bottom:var(--space-m)">' +
        `<span class="cs-status ${STATUS_CLASS[tone]}">${esc(label)}</span>` +
        `<strong>${esc(v.basis || '')}</strong>` +
      '</div>' +
      '<ul style="margin:0;padding-left:18px">' +
        (v.reasons || []).map((r) => `<li class="cs-muted-s">${esc(r)}</li>`).join('') +
      '</ul>' +
      '<p class="cs-muted-s" style="margin-top:var(--space-s)">' +
      '三态而非真假二值：统计指纹必然有误报，「证据不足」是有效结论。' +
      '</p>';
    return container('裁决', '', body);
  }

  /** 多后端分派：这种「测不出来」是答案，不是失败。 */
  function renderMultiBackend(report) {
    const block = report.multi_backend;
    if (!block) return '';
    const votes = block.votes || {};
    const rows = Object.entries(votes).map(
      ([family, n]) => `<tr><td>${esc(family)}</td><td>${n}</td></tr>`
    );
    const boxes = [
      box('整体相对差', (block.best_diff ?? 0).toFixed(3),
        `最接近的单一参考 ${esc(block.best_reference || '—')}`),
      box('可解释格数', `${(block.considered || 0) - (block.unresolved || 0)}/${block.considered || 0}`,
        `容差 ±${block.tolerance}`),
      box('最高家族占比', ((block.top_share ?? 0) * 100).toFixed(0) + '%',
        esc(block.top_family || '—')),
      box('命中家族数', String(Object.keys(votes).length), '逐格投票的分布'),
    ];
    const html =
      alert('warning',
        '这 <strong>不是「库里没有这个词表」</strong>：每一格都能被某个已知词表解释，' +
        '但近的不是同一个 —— 同一批探针落到了不同后端上。中转商按请求分派时就是这个样子。' +
        '此时<strong>不存在一个可以鉴定的单一身份</strong>，弃权是正确结果。') +
      columns(boxes) +
      '<div style="margin-top:var(--space-m)">' + table(['家族', '命中的格数'], rows) + '</div>' +
      '<p class="cs-muted-s" style="margin-top:var(--space-s)">' +
        '建议交叉看「请求一致性」与「接入链路」：前者会抓到同题不同值，' +
        '后者能说明这个入口中间有几层。' +
      '</p>';
    return container('多后端分派', '词表探针命中多个不同词表。', html);
  }

  /** 未确认线索：全通道弃权时，把观测到的方向摆出来（明确标注不是判定）。 */
  function renderLeads(report) {
    const block = report.leads;
    if (!block || !(block.items || []).length) return '';
    const rows = (block.items || []).map((it) => {
      const tone = it.strength === 'match' ? 'green' : 'amber';
      return (
        '<tr>' +
          `<td>${esc(CHANNEL_LABEL[it.channel] || it.channel)}</td>` +
          `<td>${esc(it.text)}</td>` +
          `<td>${badge(it.strength === 'match' ? '容差内' : '弱', tone)}</td>` +
          `<td class="cs-muted-s">${esc(it.caveat || '')}</td>` +
        '</tr>'
      );
    });
    let html =
      alert('warning', esc(block.note || '')) +
      table(['通道', '观测到什么', '强度', '为什么不能直接采信'], rows);
    const consensus = block.consensus || [];
    if (consensus.length) {
      html += '<p class="cs-muted-s" style="margin-top:var(--space-s)">软共识：' +
        consensus.map((c) =>
          `<strong>${esc(c.family)}</strong>（${(c.from || []).map((ch) => esc(CHANNEL_LABEL[ch] || ch)).join(' + ')}）`
        ).join('；') +
        ' —— 两条不同通道指向同一方向，但仍然不是判定。</p>';
    }
    return container('未确认线索', '只在全部通道都弃权时出现。', html);
  }

  /** 下一步建议：弃权时最该看的一块 —— 缺什么、补什么、补完能解锁哪一层。 */
  function renderRecommendations(report) {
    const items = report.recommendations || [];
    if (!items.length) return '';
    const html = items.map((item, index) =>
      '<div class="cs-section">' +
        `<h3>${index + 1}. ${esc(item.title)}</h3>` +
        '<dl class="cs-kv">' +
          `<dt>为什么</dt><dd>${esc(item.why)}</dd>` +
          `<dt>怎么做</dt><dd>${esc(item.how)}</dd>` +
          `<dt>能解锁</dt><dd>${esc(item.unlocks)}</dd>` +
        '</dl>' +
      '</div>'
    ).join('');
    return container(
      '下一步建议',
      '证据不足时，这些是现在就能做的动作 —— 每条都指向一个具体步骤。',
      html
    );
  }

  /** 请求一致性（C12）：这个入口对你发的每个请求是否始终如一。 */
  function renderConsistency(report) {
    const item = (report.evidence || []).find((e) => e.channel === 'consistency');
    if (!item) return '';
    const m = item.metric || {};
    const verdict = m.verdict || 'unknown';
    const tone = verdict === 'consistent' ? 'success'
      : verdict === 'unstable' ? 'error' : 'warning';
    const label = {
      consistent: '始终如一', caution: '有一处异常', unstable: '多处异常',
    }[verdict] || verdict;

    const boxes = [
      box('参数完整性', m.max_tokens_respected ? '正常' : '疑似被缩减',
        `请求 ${m.list_expected} 行，回传 ${m.list_lines} 行`),
      box('采样自由度', `${m.freedom_unique}/${m.freedom_samples} 种`,
        m.temperature_locked ? 'temperature 疑似被锁死' : 'temperature 生效'),
      box('难度倒挂', m.difficulty_inversion ? '疑似' : '无',
        `简单 ${m.easy_correct} vs 复杂 ${m.hard_correct}`),
      box('事实一致性', `${m.fact_correct}/${m.fact_repeats}`, '同一事实重复问'),
    ];

    let html =
      '<div class="cs-btn-row" style="margin-bottom:var(--space-m)">' +
        `<span class="cs-status ${STATUS_CLASS[tone]}">${esc(label)}</span>` +
        `<span class="cs-muted-s">${esc(item.observed || '')}</span>` +
      '</div>' + columns(boxes);

    const issues = m.issues || [];
    if (issues.length) {
      html += '<div style="margin-top:var(--space-m)">' +
        issues.map((i) => alert(tone === 'success' ? 'info' : tone, esc(i))).join('') +
        '</div>';
    }
    return container(
      '请求一致性',
      '不看入口静态长什么样，看它对你发的每个请求是否始终如一。每条结论都只是「疑似」。',
      html
    );
  }

  function renderVerdict(report) {
    const layers = report.layers || {};
    const order = ['vendor', 'family', 'series', 'version'];
    const cards = order.filter((l) => layers[l]).map((layer) => {
      const v = layers[layer];
      const resolved = !v.abstained && v.value;
      const conf = pct(v.confidence);
      return (
        `<div class="cs-box cs-box--status ${resolved ? 'is-resolved' : 'is-abstained'}">` +
          `<div class="cs-box__label">${LAYER_LABEL[layer] || layer}</div>` +
          `<div class="cs-box__value">${resolved ? esc(v.value) : '弃权'}</div>` +
          `<div class="cs-box__note">置信度 ${conf}%</div>` +
          `<div class="cs-progress ${resolved ? 'cs-progress--success' : 'cs-progress--muted'}">` +
            `<i style="width:${conf}%"></i>` +
          '</div>' +
          (v.detail ? `<div class="cs-box__note" style="margin-top:8px">${esc(v.detail)}</div>` : '') +
        '</div>'
      );
    }).join('');

    const meta =
      `<dl class="cs-kv" style="margin-bottom:var(--space-l)">` +
        `<dt>run</dt><dd><code>${esc(report.run_id)}</code></dd>` +
        `<dt>测试集</dt><dd>${esc((report.input || {}).suite || '—')}</dd>` +
        `<dt>协议</dt><dd>${esc((report.protocol || {}).detected || '—')}</dd>` +
      '</dl>';

    return container(
      '结论',
      '逐层独立判定：通道只能证明到它声明的那一层，达不到阈值就弃权。',
      meta + columns(cards ? [cards] : [])
    );
  }

  function renderGateway(gw) {
    const tone = GW_TONE[gw.verdict] || '';
    const sigs = [].concat(gw.behaviour_signals || [], gw.link_signals || []);
    const sigHtml = sigs.length
      ? sigs.map((s) => {
          const kind = s.level === 'warning' ? 'warning' : (s.level === 'notice' ? 'warning' : 'info');
          return alert(kind, esc(s.text) + (s.note ? `<div class="cs-muted-s">${esc(s.note)}</div>` : ''));
        }).join('')
      : '<span class="cs-muted-s">未捕获到链路信号</span>';

    const head =
      '<div class="cs-btn-row" style="margin-bottom:var(--space-m)">' +
        badge(gw.label || GW_LABEL[gw.verdict] || gw.verdict, tone) +
        `<span class="cs-muted-s">${esc(gw.host || '')} · 置信度 ${pct(gw.confidence)}%` +
        ` · 链路信号 ${gw.link_signal_count || 0} 条 / 行为信号 ${gw.behaviour_signal_count || 0} 条</span>` +
      '</div>' +
      (gw.reason ? `<p class="cs-muted">${esc(gw.reason)}</p>` : '');

    return container(
      '接入链路',
      '这条不问模型身份，只问「这个入口是谁搭的」。中间层本身不是问题，改流量才是；可疑结论需 ≥2 个独立行为信号。',
      head + `<div style="margin-top:var(--space-m)">${sigHtml}</div>`,
      '<span class="cs-muted-s">链路结果不参与身份打分</span>'
    );
  }

  function renderTruth(report) {
    const t = report.truthfulness;
    if (!t || t.verdict === 'unknown') return '';
    const rows = (t.checks || []).map((c) => {
      const mark = c.match === true
        ? status('success', '一致')
        : c.match === false ? status('error', '不符') : '<span class="cs-muted-s">—</span>';
      return `<tr><td>${esc(c.field)}</td><td>${esc(c.claimed)}</td><td>${esc(c.observed)}</td><td>${mark}</td></tr>`;
    });
    return container(
      '真伪核验',
      '拿观测到的自述去对基准里声明的字段。',
      '<div class="cs-btn-row" style="margin-bottom:var(--space-m)">' +
        `<span class="cs-muted-s">声明</span><code>${esc(t.claim)}</code>` +
        status(TRUTH_TONE[t.verdict] || 'info', TRUTH_LABEL[t.verdict] || t.verdict) +
        `<span class="cs-muted-s">${pct(t.confidence)}%</span>` +
      '</div>' +
      table(['字段', '声明', '观测', '一致'], rows) +
      (t.note ? `<p class="cs-muted" style="margin-top:var(--space-m)">${esc(t.note)}</p>` : '')
    );
  }

  function renderConflicts(report) {
    const list = report.conflicts || [];
    if (!list.length) return '';
    const body = list.map((c) =>
      alert('warning',
        `<div class="cs-alert__title">${esc((c.values || []).join(' vs '))}</div>` +
        `<div class="cs-muted-s">层：${LAYER_LABEL[c.layer] || c.layer} · 通道：${esc((c.channels || []).map((x) => CHANNEL_LABEL[x] || x).join('、'))} · ${esc(c.severity)} · 置信度 ×${c.penalty}</div>` +
        (c.hint ? `<div>${esc(c.hint)}</div>` : '')
      )
    ).join('');
    return container('冲突', '通道之间打架。冲突往往是最有价值的发现，不是要被平滑掉的噪声。', body);
  }

  /** 数字分布通道的稳健打分明细：概率 + 交叉验证准确率 + 库外守卫。
      概率旁边必须同时给出「这个百分数值几分信任」，否则会被当成定论读。 */
  function renderRobust(metric) {
    const r = metric && metric.robust;
    if (!r) return '';
    const cal = r.calibration || {};
    const guard = r.out_of_scope === true
      ? alert('warning', `库外守卫触发：最高分 ${r.best_score} < ${r.out_of_scope_threshold}。`
          + `闭集里最像的是「${esc(r.prediction)}」（${pct(r.probability)}%），`
          + '但那只是「在这批候选里最像它」，不代表它是。')
      : '';
    const boxes = [
      box('闭集概率', pct(r.probability) + '%', esc(r.prediction || '')),
      box('最高分', r.best_score, `阈值 ${r.out_of_scope_threshold}`),
      box('交叉验证准确率',
        cal.cv_accuracy === null || cal.cv_accuracy === undefined ? '—' : pct(cal.cv_accuracy) + '%',
        (cal.cv_samples ? `${cal.cv_samples} 个样本 · ` : '') + `${cal.queries || '?'} 条回答档位`),
      box('候选数', r.candidates || 0, esc(r.method || '')),
    ];
    return container('数字分布稳健打分', '边际 Hellinger 0.75 + 有序块 0.25，'
      + '剔除提示词干扰方向后与类心比对；beta 由分组交叉验证标定。',
      guard + columns(boxes));
  }

  function renderEvidence(report) {
    let robustHtml = '';
    const rows = (report.evidence || []).map((e) => {
      if (e.channel === 'distribution' && e.metric && e.metric.robust) {
        robustHtml = renderRobust(e.metric);
      }
      const mark = e.match === true
        ? status('success', '命中')
        : e.match === false ? status('error', '不符') : '<span class="cs-muted-s">未比对</span>';
      return (
        '<tr>' +
          `<td>${esc(CHANNEL_LABEL[e.channel] || e.channel)}<div class="cs-table__sub">${esc(e.item)}</div></td>` +
          `<td>${esc(e.observed)}${e.note ? `<div class="cs-table__sub">${esc(e.note)}</div>` : ''}</td>` +
          `<td>${mark}</td>` +
          `<td class="cs-nowrap">${(e.weight || 0).toFixed(2)}</td>` +
        '</tr>'
      );
    });
    if (robustHtml) {
      return container('证据', '每条证据只能证明到它声明的那一层。',
        table(['通道', '观测', '命中', '权重'], rows, '本次运行没有产生证据')) + robustHtml;
    }
    return container('证据', '每条证据只能证明到它声明的那一层。',
      table(['通道', '观测', '命中', '权重'], rows, '本次运行没有产生证据'));
  }

  function renderCost(report) {
    // 没有价格概念：只报请求数与耗时，费用相关的一律不展示。
    const c = report.cost || {};
    const boxes = [
      box('请求数', c.requests || 0),
      box('耗时', `${((c.elapsed_seconds) || 0).toFixed(1)}<span style="font-size:16px">s</span>`),
      box('基准候选', (report.baseline || {}).candidates_considered || 0, '参与本次比对的条目数'),
    ];
    let html = columns(boxes);
    if (c.budget && (c.budget.max_seconds || c.budget.interrupted)) {
      html += alert(c.budget.interrupted ? 'warning' : 'info',
        `时间上限 ${c.budget.max_seconds ?? '不限'} 秒` +
        (c.budget.interrupted ? ' · 已触发中断' : ''));
    }
    return container('消耗', '记录本次实际开销，便于事后校准。', html);
  }

  /** 结果可信度：C10 的观测 + 本地指标自检。它回答「这份报告能不能信」。 */
  function renderValidity(report) {
    const block = report.validity;
    if (!block) return '';
    const observed = block.observed || null;
    const level = block.level || 'caution';
    const tone = level === 'ok' ? 'success' : level === 'caution' ? 'warning' : 'error';
    const label = { ok: '站得住', caution: '需要留意', weak: '不宜直接采信' }[level] || level;

    const checks = block.checks || {};
    const coverage = checks.bank_coverage || null;
    const boxes = [
      box('独立信号源', checks.independent_channels || 0,
        (checks.channels || []).join('、') || '无'),
      box('顶层余量', checks.margin === null || checks.margin === undefined
        ? '—' : pct(checks.margin) + '%', '第一名与第二名的概率差'),
      box('冲突', `${checks.conflicts || 0}（硬 ${checks.hard_conflicts || 0}）`),
      box('证据产出', `${checks.produced_items || 0}/${checks.planned_items || 0}`,
        '实际产出 / 计划测试项'),
    ];
    if (coverage) {
      const inBank = coverage.declared_in_bank;
      boxes.push(box('参考库覆盖',
        inBank === false ? '库外' : inBank === true ? '已收录' : '—',
        `${coverage.candidates} 个候选` +
        (inBank === false ? `；「${coverage.declared_model}」不在其中` : '')));
    }
    // 库里有多少条**真的带指纹**：这决定闭集判定的实际候选池大小。
    // 「为什么测不出来」最常见的答案就在这个数字里。
    const have = (coverage && coverage.with_fingerprints);
    if (have !== null && have !== undefined) {
      const fams = (coverage.fingerprint_families || []).join('、') || '—';
      boxes.push(box('带指纹条目',
        `${have}/${coverage.bank_entries || 0}`,
        have === 0 ? '没有实测指纹，所有身份通道只能弃权' : `覆盖 ${fams} 家族`));
    }

    let html =
      '<div class="cs-btn-row" style="margin-bottom:var(--space-m)">' +
        badge(label, tone === 'success' ? 'green' : tone === 'warning' ? 'amber' : 'red') +
        `<span class="cs-muted-s">${esc(block.note || '')}</span>` +
      '</div>' + columns(boxes);

    const factors = block.factors || [];
    if (factors.length) {
      html += '<div style="margin-top:var(--space-m)">' +
        factors.map((f) => alert(tone === 'success' ? 'info' : tone, esc(f))).join('') +
        '</div>';
    }

    if (observed) {
      const rows = [
        ['释义一致率', _fmt(observed.paraphrase_consistency)],
        ['自一致率', _fmt(observed.self_consistency)],
        ['重复翻转率', _fmt(observed.flip_rate)],
        ['不可回答问题', observed.hallucinated_on_unanswerable ? '编造了数值' : '未编造'],
        ['canary 文献', observed.canary_fabricated ? '编造了细节' : '未编造'],
      ];
      html += '<div style="margin-top:var(--space-m)">' +
        table(['验真探针', '结果'], rows.map(
          (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`)) +
        '</div>';
    }

    return container(
      '结果可信度',
      '这一块回答的是「这份报告能不能信」，与上面「模型是谁」是两件事。',
      html
    );
  }

  function _fmt(value) {
    if (value === null || value === undefined) return '—';
    return `${Math.round(value * 100)}%`;
  }

  function renderLineage(report) {
    const note = report.lineage_note;
    if (!note) return '';
    const rows = Object.entries(note.favored_by || {}).map(
      ([ch, mid]) => `<tr><td>${esc(CHANNEL_LABEL[ch] || ch)}</td><td>${esc(mid)}</td></tr>`
    );
    const resistant = (note.resistant_channels || [])
      .map((ch) => esc(CHANNEL_LABEL[ch] || ch)).join('、');
    const html =
      alert('warning', esc(note.note)) +
      (rows.length
        ? table(['可迁移通道', '判定相符的候选'], rows)
        : '') +
      `<div class="cs-muted-s" style="margin-top:var(--space-s)">已发言的抗蒸馏通道：${resistant || '—'}</div>`;
    return container(
      '血缘提示',
      '相似可能来自蒸馏，而不是同一个服务在应答 —— 统计指纹证明不了训练血缘。',
      html
    );
  }

  function renderOpenset(report) {
    const block = report.openset;
    if (!block) return '';
    const stateMap = {
      in_bank: ['库内有对应条目', 'green'],
      likely_unseen: ['候选池里大概没有正确答案', 'red'],
      ambiguous: ['说不准', 'amber'],
      unknown: ['无法判断', 'amber'],
    };
    const [label, tone] = stateMap[block.verdict] || [block.verdict, 'amber'];
    const s = block.stats || {};
    const boxes = [
      box('最佳相似度', (s.max ?? 0).toFixed(3),
        `阈值 ${block.calibrated ? (block.calibration.top_floor ?? '—') : '未标定'}`),
      box('相对领先度', (s.gap_ratio ?? 0).toFixed(3),
        `次佳落后 ${(s.gap ?? 0).toFixed(3)}`),
      box('候选数', String(s.n || 0),
        `归一化熵 ${(s.entropy ?? 0).toFixed(2)}`),
      box('峰度 z', (s.z ?? 0).toFixed(2),
        `归一 ${(s.z_norm ?? 0).toFixed(2)}（跨库可比）`),
    ];
    const html =
      '<div class="cs-btn-row" style="margin-bottom:var(--space-m)">' +
        badge(label, tone) +
        `<span class="cs-muted-s">${esc(block.note || '')}</span>` +
      '</div>' +
      columns(boxes) +
      '<div style="margin-top:var(--space-m)">' +
        block.reasons.map((r) => alert(tone === 'green' ? 'info' : tone, esc(r))).join('') +
      '</div>' +
      `<div class="cs-muted-s" style="margin-top:var(--space-s)">` +
        `经验预测集（无法排除的候选，半宽 ${block.set_delta}）：` +
        (block.prediction_set || []).map((m) => `<code>${esc(m)}</code>`).join('、') +
      `</div>`;
    return container(
      '开集识别',
      '先问「库里有它吗」，再谈「它是谁」。闭集归因一定会给答案，这一块负责说「这次该不该给」。',
      html
    );
  }

  function renderWarnings(report) {
    const list = report.warnings || [];
    if (!list.length) return '';
    return container('提示', '', list.map((w) => alert('info', esc(w))).join(''));
  }

  /* ------------------------------------------------------- 指纹台账 */

  async function loadFingerprints() {
    const host = $('fp-ledger');
    if (!host) return;
    try {
      const data = await (await fetch('/v1/fingerprints')).json();
      const ledger = data.ledger || {};
      const keys = Object.keys(ledger);

      if (!keys.length) {
        const known = data.known || {};
        const rows = Object.entries(known).slice(0, 6).map(([fp, info]) =>
          `<tr><td class="cs-mono-sm">${esc(fp.slice(0, 24))}…</td>` +
          `<td>${esc(info.model || '')}</td><td>${esc(info.as_of || '')}</td>` +
          `<td class="cs-muted-s">${esc(info.source || '')}</td></tr>`
        );
        host.innerHTML =
          '<p class="cs-muted-s" style="margin-bottom:var(--space-m)">' +
          '还没有任何采集记录。每跑一次验证，指纹会自动记入台账；指纹一变即告警。' +
          '</p>' +
          '<h3 style="font-size:16px;margin-bottom:8px">已知公开指纹</h3>' +
          table(['指纹', '对应模型', '时间', '来源'], rows, '暂无');
        return;
      }

      const rows = keys.map((key) => {
        const v = ledger[key];
        const changed = v.history && v.history.length > 1;
        return (
          '<tr>' +
            `<td><code>${esc(v.endpoint)}</code><div class="cs-table__sub">${esc(v.model)}</div></td>` +
            `<td class="cs-mono-sm">${esc((v.current || '').slice(0, 20))}…</td>` +
            `<td>${changed ? status('warning', '已变更') : status('success', '稳定')}</td>` +
            `<td class="cs-nowrap cs-muted-s">${esc((v.last_seen || '').replace('T', ' ').replace('Z', ''))}</td>` +
          '</tr>'
        );
      });
      host.innerHTML = table(['端点 / 模型', '当前指纹', '状态', '最近采集'], rows);
    } catch (e) {
      host.innerHTML = `<span class="cs-muted-s">台账加载失败：${esc(e.message)}</span>`;
    }
  }

  /* ------------------------------------------------------------- 导出 */

  function downloadJson() {
    if (!window._lastReport) return;
    const blob = new Blob([JSON.stringify(window._lastReport, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${window._lastReport.run_id}.json`;
    a.click();
  }

  /* --------------------------------------------------------- 通用小组件 */

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return { ok: res.ok, data: await res.json() };
  }

  function kv(pairs) {
    return '<dl class="cs-kv">' +
      pairs.map((p) => `<dt>${esc(p[0])}</dt><dd>${p[1]}</dd>`).join('') +
      '</dl>';
  }

  /* -------------------------------------------------------- 采集基准页 */

  function collectStatus(text, bad) {
    const node = $('collect-status');
    if (!node) return;
    node.textContent = text;
    node.style.color = bad ? 'var(--color-text-error)' : '';
  }

  function resolveBank() {
    const sel = $('c-bank');
    if (!sel) return 'default';
    return sel.value === '__new__'
      ? ($('c-new-bank').value || '').trim()
      : sel.value;
  }

  async function collectPlan() {
    const { ok, data } = await post('/v1/baselines/collections?dry_run=1', {
      channel_count: readCount('c-channel-count'),
    });
    if (!ok) return void collectStatus(data.error || '预估失败', true);
    const plan = data.plan || {};
    const cats = data.categories || [];
    const rows = cats.map((c) =>
      `<tr><td>${esc(c.name)}</td><td class="cs-nowrap">${c.requests} 次请求</td></tr>`
    );
    $('collect-result').innerHTML = container(
      '采集计划',
      '纯计数，不会发起任何请求。',
      kv([['采集项数量', `${data.channel_count || 0} 项`],
          ['请求总数', String(plan.requests || 0)],
          ['采集大类', cats.map((c) => badge(c.name, 'blue')).join(' ')]]) +
      table(['采集大类', '请求数'], rows)
    );
    collectStatus('');
  }

  async function runCollect() {
    const base = $('c-base').value.trim();
    const model = $('c-model').value.trim();
    const vendor = $('c-vendor').value.trim();
    const family = $('c-family').value.trim();
    if (!base || !model) return void collectStatus('请填写 Base URL 与模型 ID', true);
    if (!vendor || !family) return void collectStatus('请填写厂商与家族（基准必须知道它自己是谁）', true);

    let bank = resolveBank();
    if (!bank) return void collectStatus('请填写新基准库的 ID', true);

    // 新库先建库，再采集
    if ($('c-bank').value === '__new__') {
      const { ok, data } = await post('/v1/banks', {
        id: bank,
        name: $('c-new-bank-name').value.trim() || bank,
        description: '自定义采集',
      });
      if (!ok) return void collectStatus(data.error || '建库失败', true);
    }

    $('btn-collect').disabled = true;
    collectStatus('');
    resetPhases();
    showProgress('正在提交…', 'collect-progress');

    const { ok, data } = await post('/v1/baselines/collections', {
      base_url: base,
      api_key: $('c-key').value,
      model,
      vendor, family,
      series: $('c-series').value.trim(),
      version: $('c-version').value.trim(),
      tier: $('c-tier').value,
      bank,
      aliases: $('c-aliases').value.split(',').map((s) => s.trim()).filter(Boolean),
      note: $('c-note').value.trim(),
      channel_count: readCount('c-channel-count'),
    });
    if (!ok) {
      showProgress('', 'collect-progress');
      $('btn-collect').disabled = false;
      return void collectStatus(data.error || '提交失败', true);
    }

    await pollTask(data.id, {
      url: `/v1/baselines/collections/${data.id}`,
      onProgress: (p) => showProgress(p, 'collect-progress'),
      onDone: (t) => {
        $('btn-collect').disabled = false;
        if (t.status === 'failed') {
          showProgress('', 'collect-progress');
          return void collectStatus('采集失败：' + (t.error || '未知错误'), true);
        }
        renderCollectResult((t.result || {}), bank);
        collectStatus('');
      },
    });
  }

  function renderCollectResult(result, bank) {
    const notes = result.notes || [];
    const host = $('collect-result');
    if (!host) return;
    const cats = (result.channels || []).map((c) => CHANNEL_LABEL[c] || c)
      .filter((n, i, a) => a.indexOf(n) === i);
    host.innerHTML = container(
      '采集完成',
      '这条基准已写入库，验证时选它即可参与闭集比对。',
      kv([
        ['模型 ID', `<code>${esc(result.model_id || '')}</code>`],
        ['基准库', esc(bank)],
        ['采集项数量', String((result.channels || []).length)],
        ['写入大类', cats.length ? cats.map((n) => badge(n, 'green')).join(' ') : '（无）'],
      ]) +
      (notes.length
        ? notes.map((n) => alert('warning', esc(n))).join('')
        : alert('success', '所有选定通道均采集成功。')) +
      '<div class="cs-btn-row" style="margin-top:var(--space-m)">' +
        `<a class="cs-btn cs-btn--normal" href="/baselines?bank=${encodeURIComponent(bank)}">查看该库</a>` +
        '<a class="cs-btn cs-btn--normal" href="/">去跑一次验证</a>' +
      '</div>'
    );
  }

  /* -------------------------------------------------------- 历史记录页 */

  async function removeTask(id) {
    await fetch(`/v1/history/${id}`, { method: 'DELETE' });
    const row = document.querySelector(`tr[data-id="${id}"]`);
    if (row) row.remove();
  }

  async function initHistoryDetail(taskId) {
    const res = await fetch(`/v1/history/${taskId}`);
    if (!res.ok) {
      $('task-meta').innerHTML = alert('error', '未找到该任务');
      return;
    }
    const { task, summary } = await res.json();
    const inp = (task || {}).input || {};

    const isCollect = task.kind === 'collection';
    $('task-meta').innerHTML = kv([
      ['任务 ID', `<code>${esc(taskId)}</code>`],
      ['类型', isCollect ? '基准采集' : '模型验证'],
      ['状态', status(
        task.status === 'completed' ? 'success' : task.status === 'failed' ? 'error' : 'pending',
        task.status === 'completed' ? '完成' : task.status === 'failed' ? '失败' : '运行中')],
      ['被测模型', `<code>${esc(inp.model || '—')}</code>`],
      ['端点', esc(inp.base_url || '—')],
      ['基准库', esc(inp.bank || 'default')],
      // 只报数量，不列具体项
      isCollect
        ? ['采集项数量', inp.channel_count ? String(inp.channel_count) : '—']
        : ['测试项数量', inp.item_count ? String(inp.item_count) : '—'],
      ['开始时间', esc((task.created_at || '').replace('T', ' ').replace('Z', ''))],
    ]) + (task.error ? alert('error', esc(task.error)) : '');

    if (task.report) {
      const actions = $('task-actions');
      if (actions) actions.style.display = '';
      $('btn-export-json').href = `/v1/history/${taskId}/export?format=json`;
      $('btn-export-md').href = `/v1/history/${taskId}/export?format=md`;
      window._taskId = taskId;
      renderReport(task.report, 'task-report');
    } else if (task.result) {
      const res = task.result || {};
      const cats = (res.channels || []).map((c) => CHANNEL_LABEL[c] || c)
        .filter((n, i, a) => a.indexOf(n) === i);
      $('task-report').innerHTML = container('采集结果', '', kv([
        ['模型 ID', `<code>${esc(res.model_id || '')}</code>`],
        ['基准库', esc(res.bank || '')],
        ['采集项数量', String((res.channels || []).length)],
        ['采集大类', cats.length ? cats.map((n) => badge(n, 'green')).join(' ') : '（无）'],
      ]));
    } else if (task.status === 'running') {
      resetPhases();
      await pollTask(taskId, {
        url: `/v1/history/${taskId}`,
        onProgress: (p) => showProgress(p, 'task-progress'),
        onDone: (t) => {
          if ((t.task || {}).report) renderReport(t.task.report, 'task-report');
        },
      });
    } else {
      $('task-report').innerHTML = container('结果', '',
        `<span class="cs-muted-s">尚无结果${summary && summary.conclusion ? '' : ''}</span>`);
    }
  }

  /* ------------------------------------------------------------- 初始化 */

  /** 数量变化时顺手 clamp 一下，并刷新「覆盖哪些大类」。 */
  async function refreshItemCoverage() {
    const meta = await getCatalog();
    if (!meta) return;
    const n = clampCount(readCount('f-item-count'), meta.test, meta.test.default);
    renderCoverage('item-coverage', meta.test, n);
  }

  async function refreshChannelCoverage() {
    const meta = await getCatalog();
    if (!meta) return;
    const n = clampCount(readCount('c-channel-count'), meta.collect, meta.collect.default);
    renderCoverage('channel-coverage', meta.collect, n);
  }

  /** 数量输入框：失焦时夹到合法区间，同时更新大类预览。 */
  function bindCountInput(id, refresh) {
    const el = $(id);
    if (!el) return;
    const onChange = () => {
      const n = parseInt(el.value, 10);
      if (!isNaN(n)) {
        const lo = parseInt(el.min, 10) || 1;
        const hi = parseInt(el.max, 10) || lo;
        el.value = String(Math.min(hi, Math.max(lo, n)));
      }
      refresh();
    };
    el.addEventListener('change', onChange);
    el.addEventListener('blur', onChange);
  }

  function initPage() {
    const est = $('btn-estimate');
    if (est) est.addEventListener('click', estimate);

    const runBtn = $('btn-run');
    if (runBtn) runBtn.addEventListener('click', run);

    // 模式切换只影响显示哪一组控件，选法本身由 selectionPayload() 统一收口
    document.querySelectorAll('input[name="test-mode"]').forEach((el) => {
      el.addEventListener('change', syncModePanels);
    });
    document.querySelectorAll('input[name="auto-mode"]').forEach((el) => {
      el.addEventListener('change', syncModePanels);
    });

    bindCountInput('f-item-count', refreshItemCoverage);

    loadCandidates();
    loadFingerprints();
    // 目录拉到手才能渲染手动列表与大类列表
    getCatalog().then((meta) => {
      if (!meta) return;
      renderCategoryList(meta.test);
      renderManualList(meta.test);
      syncModePanels();
      refreshItemCoverage();
    });
    resume();
  }

  function initCollect() {
    bindCountInput('c-channel-count', refreshChannelCoverage);
    refreshChannelCoverage();

    const sel = $('c-bank');
    if (sel) {
      const toggle = () => {
        const isNew = sel.value === '__new__';
        const f1 = $('c-new-bank-field');
        const f2 = $('c-new-bank-name-field');
        if (f1) f1.style.display = isNew ? '' : 'none';
        if (f2) f2.style.display = isNew ? '' : 'none';
      };
      sel.addEventListener('change', toggle);
      toggle();
    }

    const plan = $('btn-plan');
    if (plan) plan.addEventListener('click', collectPlan);

    const go = $('btn-collect');
    if (go) go.addEventListener('click', runCollect);
  }

  function initHistory() {
    const refresh = $('btn-refresh');
    if (refresh) refresh.addEventListener('click', () => location.reload());
    // 有任务在跑时，页面自身每 5 秒刷新一次列表状态
    if (document.querySelector('.cs-status--pending')) {
      setTimeout(() => location.reload(), 5000);
    }
  }

  return {
    initPage, initCollect, initHistory, initHistoryDetail, removeTask,
    renderReport, loadReport, downloadJson, resume,
    estimate, run, collectPlan, runCollect, loadFingerprints,
  };
})();
