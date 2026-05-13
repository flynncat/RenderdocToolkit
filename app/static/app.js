let currentCmpJobId = null;
let currentPerfJobId = null;
let currentExportJobId = null;
let currentPerfAnalysis = null;
let setupStatusCache = null;
let scannedAssetPasses = [];
let perfPreviewPinned = false;
let perfPreviewHideTimer = null;
let pendingAssetExportDraft = null;
let pendingAssetExportPreview = null;

function hasDesktopBridge() {
  return Boolean(window.pywebview && window.pywebview.api);
}

async function pickDesktopFile(apiMethod, targetInputId) {
  if (!hasDesktopBridge()) {
    alert("当前环境未启用桌面文件对话框，请手动输入本地路径。");
    return;
  }
  try {
    const value = await window.pywebview.api[apiMethod]();
    if (value) {
      document.getElementById(targetInputId).value = value;
    }
  } catch (error) {
    alert(error.message || "打开文件对话框失败");
  }
}

async function pickDesktopDirectory(targetInputId) {
  if (!hasDesktopBridge()) {
    alert("当前环境未启用桌面目录对话框，请手动输入本地路径。");
    return;
  }
  try {
    const value = await window.pywebview.api.pick_directory();
    if (value) {
      document.getElementById(targetInputId).value = value;
    }
  } catch (error) {
    alert(error.message || "打开目录对话框失败");
  }
}

async function pickDesktopCsvFiles(targetInputId) {
  if (!hasDesktopBridge()) {
    alert("当前环境未启用桌面文件对话框，请手动输入本地路径。");
    return;
  }
  try {
    const value = await window.pywebview.api.pick_csv_files();
    if (value) {
      document.getElementById(targetInputId).value = value;
    }
  } catch (error) {
    alert(error.message || "打开 CSV 多选对话框失败");
  }
}

async function revealDesktopPath(path) {
  if (!path || !hasDesktopBridge()) {
    return;
  }
  try {
    await window.pywebview.api.reveal_path(path);
  } catch (error) {
    console.warn("打开目录失败", error);
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "请求失败");
  }
  return data;
}

async function copyTextFromElement(elementId) {
  const element = document.getElementById(elementId);
  const text = element ? (element.value || element.textContent || "") : "";
  if (!text) {
    throw new Error("当前没有可复制的内容。");
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  if (element && typeof element.select === "function") {
    element.focus();
    element.select();
    const ok = document.execCommand("copy");
    if (ok) {
      return;
    }
  }
  throw new Error("当前环境不支持剪贴板写入。");
}

function setSummaryBusy(elementId, lines) {
  const element = document.getElementById(elementId);
  if (!element) {
    return;
  }
  const rows = (lines || []).map((line) => `<div>${escapeHtml(line)}</div>`).join("");
  element.innerHTML = rows || '<div class="empty-state">处理中...</div>';
}

function setLogBusy(elementId, text) {
  const element = document.getElementById(elementId);
  if (!element) {
    return;
  }
  element.textContent = text || "处理中，请稍候...";
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .split("&").join("&amp;")
    .split("<").join("&lt;")
    .split(">").join("&gt;")
    .split("\"").join("&quot;")
    .split("'").join("&#39;");
}

function formatBytesText(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }
  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(3)} MB`;
  }
  if (value >= 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${value} B`;
}

function renderHealth(health) {
  document.getElementById("health-output").textContent = JSON.stringify(health, null, 2);
  document.getElementById("cmp-health-output").textContent = JSON.stringify({
    renderdoc_cmp: health.renderdoc_cmp || {},
    rdc: health.rdc || {},
    doctor: health.doctor || {},
  }, null, 2);
}

function fillSetupForm(statusPayload) {
  const settings = statusPayload.settings || {};
  document.getElementById("setup-renderdoc-python-path").value = settings.renderdoc_python_path || "";
  document.getElementById("setup-cmp-root").value = settings.renderdoc_cmp_root || "";
  document.getElementById("setup-status-output").textContent = JSON.stringify(statusPayload, null, 2);
}

function showSetupModal() {
  document.getElementById("setup-modal").classList.remove("hidden");
}

function hideSetupModal() {
  document.getElementById("setup-modal").classList.add("hidden");
}

function showAssetExportMappingModal() {
  document.getElementById("asset-export-mapping-modal").classList.remove("hidden");
}

function hideAssetExportMappingModal() {
  document.getElementById("asset-export-mapping-modal").classList.add("hidden");
  pendingAssetExportDraft = null;
  pendingAssetExportPreview = null;
}

function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-workspace").forEach((node) => {
    node.classList.toggle("active", node.id === `workspace-${tabName}`);
  });
}

async function loadHealth() {
  const health = await fetchJson("/api/health");
  renderHealth(health);
}

async function loadSetupStatus() {
  const status = await fetchJson("/api/setup-status");
  setupStatusCache = status;
  fillSetupForm(status);
  if (status.wizard && status.wizard.needs_setup) {
    showSetupModal();
  }
}

function renderCmpSummary(detail) {
  const metadata = detail.metadata || {};
  const inputs = metadata.inputs || {};
  const reportUrl = detail.report_url || "";
  const cmpRdSource = (inputs.renderdoc_source || "").trim();
  const cmpRdResolved = (inputs.renderdoc_dir_resolved || "").trim();
  const cmpRdLine = cmpRdResolved
    ? `<div><strong>RenderDoc:</strong> ${escapeHtml(cmpRdResolved)}${cmpRdSource ? ` (${escapeHtml(cmpRdSource)})` : ""}</div>`
    : "";
  document.getElementById("cmp-summary").innerHTML = `
    <div><strong>Job:</strong> ${metadata.job_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>Base:</strong> ${inputs.base_file || "-"}</div>
    <div><strong>New:</strong> ${inputs.new_file || "-"}</div>
    <div><strong>Strict:</strong> ${String(inputs.strict_mode == null ? "-" : inputs.strict_mode)}</div>
    ${cmpRdLine}
  `;
  document.getElementById("cmp-run-log").textContent = detail.run_log || "暂无日志";
  const linkWrap = document.getElementById("cmp-report-link-wrap");
  if (reportUrl) {
    linkWrap.innerHTML = `<a href="${reportUrl}" target="_blank" rel="noopener">在新窗口打开 HTML 报告</a>`;
    document.getElementById("cmp-report-frame").src = reportUrl;
  } else {
    linkWrap.innerHTML = "";
    document.getElementById("cmp-report-frame").src = "about:blank";
  }
}

function renderCmpJobs(jobs) {
  const container = document.getElementById("cmp-jobs-list");
  container.innerHTML = "";
  if (!jobs.length) {
    container.innerHTML = '<div class="empty-state">暂无 cmp 任务</div>';
    return;
  }
  jobs.forEach((item) => {
    const div = document.createElement("div");
    div.className = "session-item" + (item.job_id === currentCmpJobId ? " active" : "");
    div.innerHTML = `
      <div class="title">${item.title || item.job_id}</div>
      <div class="meta">${item.updated_at || ""}</div>
      <div class="meta">状态: ${item.status || "-"}</div>
    `;
    div.addEventListener("click", async () => {
      await loadCmpJob(item.job_id);
    });
    container.appendChild(div);
  });
}

function renderPerfSummary(detail) {
  const metadata = detail.metadata || {};
  const inputs = metadata.inputs || {};
  const analysis = detail.analysis || {};
  const overview = analysis.overview || {};
  const captureInfo = analysis.capture_info || {};
  const features = analysis.analysis_features || {};
  currentPerfAnalysis = analysis;
  const rdSource = (inputs.renderdoc_source || "").trim();
  const rdResolved = (inputs.renderdoc_dir_resolved || "").trim();
  const rdLine = rdResolved
    ? `<div><strong>RenderDoc:</strong> ${escapeHtml(rdResolved)}${rdSource ? ` (${escapeHtml(rdSource)})` : ""}</div>`
    : "";

  const analysisMode = (inputs.analysis_mode || "").trim();
  const isXmlFallback = analysisMode === "xml_fallback";
  const modeBadge = isXmlFallback
    ? `<div class="perf-mode-badge perf-mode-fallback">⚠️ XML 回退分析模式 (自定义 RenderDoc)</div>`
    : "";

  let thumbBlock = "";
  if (analysis.capture_thumbnail_url) {
    const note = isXmlFallback
      ? "回退模式下仅可显示抓帧的整体缩略图，无法生成单 Draw 的线框预览（缺少 Python 回放 API）"
      : "";
    thumbBlock = `
      <div class="perf-capture-thumb-block">
        <div class="perf-capture-thumb-title">Capture 缩略图</div>
        <button type="button" class="perf-preview-trigger" data-preview-src="${escapeHtml(analysis.capture_thumbnail_url)}" data-preview-title="Capture 缩略图" data-preview-meta="${escapeHtml(inputs.capture_file || "")}">
          <img src="${analysis.capture_thumbnail_url}" alt="capture thumbnail" class="perf-capture-thumb">
        </button>
        ${note ? `<div class="perf-capture-thumb-note">${escapeHtml(note)}</div>` : ""}
      </div>
    `;
  }

  document.getElementById("perf-summary").innerHTML = `
    ${modeBadge}
    <div><strong>Job:</strong> ${metadata.job_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>Capture:</strong> ${inputs.capture_file || "-"}</div>
    ${rdLine}
    <div><strong>驱动:</strong> ${captureInfo.driver_name || "-"}</div>
    <div><strong>总 ${features.api_duration_from_chrome_json ? "API" : "GPU"} 耗时:</strong> ${Number(overview.total_gpu_duration_ms || 0).toFixed(3)} ms${features.api_duration_from_chrome_json ? " <em>(CPU API 近似)</em>" : ""}</div>
    <div><strong>Draw 数:</strong> ${overview.draw_count || 0}</div>
    <div><strong>总三角面:</strong> ${overview.total_triangles || 0}</div>
    <div><strong>总顶点:</strong> ${overview.total_vertices_read || 0}</div>
    <div><strong>总指令${features.instruction_count_estimated ? "(估算)" : ""}:</strong> ${overview.total_instruction_count || 0}</div>
    <div><strong>稳定总分:</strong> ${Number(overview.total_stable_sort_score || 0).toFixed(3)}</div>
    <div><strong>总贴图:</strong> ${Number(overview.total_texture_mb || 0).toFixed(3)} MB</div>
    ${thumbBlock}
  `;
  document.getElementById("perf-run-log").textContent = detail.run_log || "暂无日志";
  renderPerfSortFields(analysis.sort_fields || []);
  renderPerfWarnings(analysis.warnings || []);
  renderPerfTable();
  renderPerfChart(analysis.pass_chart || []);
  renderPerfHotspotHints(analysis.hotspot_hints || []);
}

function renderPerfWarnings(warnings) {
  const container = document.getElementById("perf-warnings");
  container.innerHTML = "";
  if (!warnings.length) {
    container.innerHTML = '<div class="empty-state">当前没有额外的计时风险提示。</div>';
    return;
  }
  warnings.forEach((warning) => {
    const item = document.createElement("div");
    item.className = "perf-warning-item";
    item.textContent = warning;
    container.appendChild(item);
  });
}

function renderPerfSortFields(fields) {
  const select = document.getElementById("perf-sort-field");
  const currentValue = select.value || "stable_sort_score";
  select.innerHTML = "";
  fields.forEach((field) => {
    const option = document.createElement("option");
    option.value = field.id;
    option.textContent = field.label;
    option.selected = field.id === currentValue;
    select.appendChild(option);
  });
  if (!select.value && fields.length) {
    select.value = fields[0].id;
  }
}

function renderPerfDrawPreviewMarkup(row) {
  const previewKind = (row.draw_preview_kind || "").toLowerCase();
  if (row.draw_preview_url) {
    const isTexFallback = previewKind === "texture";
    const baseTitle = `EID ${row.eid || "-"} | ${row.pass_name || "-"}`;
    const title = isTexFallback ? `${baseTitle}（贴图预览 · 回退模式）` : baseTitle;
    const meta = `Score ${Number(row.stable_sort_score || 0).toFixed(3)} | Cover ${Number(row.screen_coverage_percent || 0).toFixed(3)}% | Tri ${row.triangles || 0}`;
    const altText = isTexFallback ? `draw-${row.eid}-texture-fallback` : `draw-${row.eid}`;
    const hoverNote = isTexFallback
      ? "自定义 RenderDoc 无 Python 回放 API；以绑定贴图作为视觉提示替代线框预览。"
      : "";
    const cls = isTexFallback ? "perf-preview-thumb perf-preview-thumb--fallback" : "perf-preview-thumb";
    return `<button type="button" class="perf-preview-trigger" data-preview-src="${escapeHtml(row.draw_preview_url)}" data-preview-title="${escapeHtml(title)}" data-preview-meta="${escapeHtml(meta)}" title="${escapeHtml(hoverNote)}"><img src="${row.draw_preview_url}" alt="${altText}" class="${cls}" loading="lazy"></button>`;
  }
  if (previewKind === "unavailable") {
    return '<span class="perf-preview-empty" title="自定义 RenderDoc 不支持 Python 回放 API，且该 Draw 未绑定可解码贴图。">回退</span>';
  }
  return '<span class="perf-preview-empty">无</span>';
}

function renderPerfTextureSummaryMarkup(row) {
  const items = row.texture_summary_items || [];
  if (!items.length) {
    return '<span class="perf-preview-empty">无</span>';
  }
  return items.map((item) => {
    const slot = item.slot == null ? "-" : item.slot;
    const label = `T${slot} ${item.width || 0}x${item.height || 0}`;
    const detail = `${label} | ${item.format || "Unknown"} | ${formatBytesText(item.byte_size || 0)}`;
    return `<span class="perf-texture-chip" title="${escapeHtml(detail)}">${escapeHtml(label)}</span>`;
  }).join("");
}

function positionPerfPreviewPanel(panel, anchorX = 0, anchorY = 0) {
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1280;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 720;
  const margin = 24;
  const gap = 14;
  const panelWidth = Math.min(560, viewportWidth - margin * 2);
  const panelHeight = Math.max(240, Math.min(panel.offsetHeight || 460, viewportHeight - margin * 2));

  let left = anchorX ? (anchorX - panelWidth - gap) : Math.max(margin, Math.round(viewportWidth * 0.18));
  if (left < margin) {
    left = Math.min(viewportWidth - panelWidth - margin, (anchorX || margin) + gap);
  }
  left = Math.max(margin, Math.min(left, viewportWidth - panelWidth - margin));

  let top;
  if (!anchorY) {
    top = Math.max(margin, Math.round((viewportHeight - panelHeight) * 0.38));
  } else if (anchorY <= viewportHeight * 0.5) {
    top = anchorY - 18;
  } else {
    top = anchorY - panelHeight + 18;
  }
  top = Math.max(margin, Math.min(top, viewportHeight - panelHeight - margin));

  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}

function showPerfPreviewPanel({ src = "", title = "", meta = "", pinned = false, anchorX = 0, anchorY = 0 }) {
  if (!src) {
    return;
  }
  const panel = document.getElementById("perf-preview-panel");
  const image = document.getElementById("perf-preview-panel-image");
  const titleNode = document.getElementById("perf-preview-panel-title");
  const metaNode = document.getElementById("perf-preview-panel-meta");
  perfPreviewPinned = pinned;
  if (perfPreviewHideTimer) {
    window.clearTimeout(perfPreviewHideTimer);
    perfPreviewHideTimer = null;
  }
  image.src = src;
  image.alt = title || "preview";
  titleNode.textContent = title || "预览";
  metaNode.textContent = meta || "";
  panel.classList.remove("hidden");
  panel.classList.toggle("pinned", perfPreviewPinned);
  positionPerfPreviewPanel(panel, anchorX, anchorY);
  image.onload = () => positionPerfPreviewPanel(panel, anchorX, anchorY);
}

function hidePerfPreviewPanel(force = false) {
  if (perfPreviewPinned && !force) {
    return;
  }
  const panel = document.getElementById("perf-preview-panel");
  const image = document.getElementById("perf-preview-panel-image");
  panel.classList.add("hidden");
  panel.classList.remove("pinned");
  image.src = "";
  perfPreviewPinned = false;
}

function scheduleHidePerfPreview() {
  if (perfPreviewPinned) {
    return;
  }
  if (perfPreviewHideTimer) {
    window.clearTimeout(perfPreviewHideTimer);
  }
  perfPreviewHideTimer = window.setTimeout(() => {
    hidePerfPreviewPanel(false);
  }, 120);
}

function renderPerfTable() {
  const container = document.getElementById("perf-table-wrap");
  const rows = [...((currentPerfAnalysis && currentPerfAnalysis.rows) || [])];
  if (!rows.length) {
    container.innerHTML = '<div class="empty-state">暂无性能结果。</div>';
    return;
  }
  const sortField = document.getElementById("perf-sort-field").value || "stable_sort_score";
  const sortDirection = document.getElementById("perf-sort-direction").value || "desc";
  rows.sort((a, b) => {
    const av = Number((a && a[sortField]) || 0);
    const bv = Number((b && b[sortField]) || 0);
    return sortDirection === "asc" ? av - bv : bv - av;
  });

  const body = rows.map((row) => {
    return `
      <tr>
        <td>${row.eid || "-"}</td>
        <td>${row.scene_pass || "-"}</td>
        <td title="${row.pass_name || ""}">${row.pass_name || "-"}</td>
        <td>${Number(row.stable_sort_score || 0).toFixed(3)}</td>
        <td>${Number(row.screen_coverage_percent || 0).toFixed(4)}</td>
        <td>${Number(row.gpu_duration_ms || 0).toFixed(3)}</td>
        <td>${row.triangles || 0}</td>
        <td>${row.vertices_read || 0}</td>
        <td>${row.input_primitives || 0}</td>
        <td>${row.instruction_total || 0}</td>
        <td>${row.ps_instruction_count || 0}</td>
        <td>${row.vs_instruction_count || 0}</td>
        <td>${row.ps_invocations || 0}</td>
        <td id="perf-draw-preview-${row.eid || ""}"><div class="perf-preview-strip">${renderPerfDrawPreviewMarkup(row)}</div></td>
        <td>${row.texture_count || 0}</td>
        <td>${Number(row.texture_total_mb || 0).toFixed(3)}</td>
        <td>${Number(row.texture_bandwidth_risk || 0).toFixed(3)}</td>
        <td title="${escapeHtml(row.texture_summary_text || "")}"><div class="perf-preview-strip">${renderPerfTextureSummaryMarkup(row)}</div></td>
      </tr>
    `;
  }).join("");

  container.innerHTML = `
    <table class="perf-table">
      <thead>
        <tr>
          <th>EID</th>
          <th>Scene Pass</th>
          <th>Pass</th>
          <th>稳定得分</th>
          <th>覆盖率%</th>
          <th>GPU ms</th>
          <th>三角面</th>
          <th>顶点</th>
          <th>图元</th>
          <th>总指令</th>
          <th>PS指令</th>
          <th>VS指令</th>
          <th>PS调用</th>
          <th>线框预览</th>
          <th>贴图数</th>
          <th>贴图总量(MB)</th>
          <th>纹理带宽风险</th>
          <th>贴图摘要</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderPerfChart(items) {
  const container = document.getElementById("perf-chart-wrap");
  if (!items.length) {
    container.innerHTML = '<div class="empty-state">暂无饼图数据。</div>';
    return;
  }
  const colors = ["#2f81f7", "#30a46c", "#f59e0b", "#ef4444", "#8b5cf6", "#14b8a6", "#64748b"];
  let start = 0;
  const segments = items.map((item, index) => {
    const end = start + Number(item.percent || 0);
    const color = colors[index % colors.length];
    const segment = `${color} ${start}% ${end}%`;
    start = end;
    return segment;
  });
  const legend = items.map((item, index) => `
    <div class="perf-chart-legend-item">
      <span class="perf-chart-color" style="background:${colors[index % colors.length]}"></span>
      <span>${item.name} · ${item.percent}% · ${Number(item.gpu_duration_ms || 0).toFixed(3)} ms</span>
    </div>
  `).join("");
  container.innerHTML = `
    <div class="perf-chart-pie" style="background: conic-gradient(${segments.join(", ")});"></div>
    <div class="perf-chart-legend">${legend}</div>
  `;
}

function renderPerfHotspotHints(hints) {
  const container = document.getElementById("perf-hotspot-hints");
  container.innerHTML = "";
  if (!hints.length) {
    container.innerHTML = '<div class="empty-state">暂无热点提示。</div>';
    return;
  }
  hints.forEach((hint) => {
    const item = document.createElement("div");
    item.className = "session-item";
    item.innerHTML = `<div class="meta">${hint}</div>`;
    container.appendChild(item);
  });
}

function renderPerfJobs(jobs) {
  const container = document.getElementById("perf-jobs-list");
  container.innerHTML = "";
  if (!jobs.length) {
    container.innerHTML = '<div class="empty-state">暂无性能分析任务</div>';
    return;
  }
  jobs.forEach((item) => {
    const summary = item.summary || {};
    const div = document.createElement("div");
    div.className = "session-item" + (item.job_id === currentPerfJobId ? " active" : "");
    div.innerHTML = `
      <div class="title">${item.title || item.job_id}</div>
      <div class="meta">${item.updated_at || ""}</div>
      <div class="meta">状态: ${item.status || "-"}</div>
      <div class="meta">热点: ${summary.hottest_pass || "-"}</div>
    `;
    div.addEventListener("click", async () => {
      await loadPerfJob(item.job_id);
    });
    container.appendChild(div);
  });
}

function populateSelect(selectId, values, selectedValue = "") {
  const select = document.getElementById(selectId);
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "请选择";
  select.appendChild(empty);
  values.forEach((value) => {
    const option = document.createElement("option");
    if (typeof value === "string") {
      option.value = value;
      option.textContent = value;
      option.dataset.passName = value;
      option.selected = value === selectedValue;
    } else {
      option.value = value.id || value.name || "";
      const passIndex = value.index == null ? "" : value.index;
      option.textContent = value.selection_label || (passIndex !== "" ? `Pass ${passIndex}` : (value.display_name || value.name || ""));
      option.dataset.passIndex = String(passIndex);
      option.dataset.passName = value.name || "";
      option.dataset.passLabel = value.selection_label || value.display_name || value.name || "";
      option.dataset.passDisplayName = value.display_name || value.name || "";
      option.dataset.passSource = value.source || "";
      option.selected = option.value === selectedValue;
    }
    select.appendChild(option);
  });
}

function renderAssetPassScan(payload) {
  scannedAssetPasses = payload.passes || [];
  const markerPasses = scannedAssetPasses.filter((item) => item && item.source === "marker" && item.first_eid);
  const selectablePasses = markerPasses.length ? markerPasses : scannedAssetPasses;
  document.getElementById("asset-pass-scan-output").textContent = JSON.stringify(payload, null, 2);
  populateSelect("asset-pass-name", selectablePasses);
  populateSelect("asset-pass-start", selectablePasses);
  populateSelect("asset-pass-end", selectablePasses);
}

function getSelectedPassMeta(selectId) {
  const select = document.getElementById(selectId);
  const option = select.options[select.selectedIndex];
  const dataset = option ? option.dataset || {} : {};
  const text = option ? option.textContent || "" : "";
  return {
    id: option ? option.value || "" : "",
    name: dataset.passName || text,
    label: dataset.passLabel || text,
    displayName: dataset.passDisplayName || text,
    source: dataset.passSource || "",
  };
}

function renderMappingOptionsToPrefix(prefix, headers, suggested = {}) {
  const values = headers || [];
  [
    [`${prefix}-position`, suggested.position],
    [`${prefix}-normal`, suggested.normal],
    [`${prefix}-uv0`, suggested.uv0],
    [`${prefix}-uv1`, suggested.uv1],
    [`${prefix}-uv2`, suggested.uv2],
    [`${prefix}-uv3`, suggested.uv3],
    [`${prefix}-color`, suggested.color],
    [`${prefix}-tangent`, suggested.tangent],
  ].forEach(([selectId, selected]) => populateSelect(selectId, values, selected || ""));
}

function renderMappingOptions(headers, suggested = {}) {
  renderMappingOptionsToPrefix("mapping", headers, suggested);
}

function collectMappingFromPrefix(prefix) {
  return {
    position: document.getElementById(`${prefix}-position`).value,
    normal: document.getElementById(`${prefix}-normal`).value,
    uv0: document.getElementById(`${prefix}-uv0`).value,
    uv1: document.getElementById(`${prefix}-uv1`).value,
    uv2: document.getElementById(`${prefix}-uv2`).value,
    uv3: document.getElementById(`${prefix}-uv3`).value,
    color: document.getElementById(`${prefix}-color`).value,
    tangent: document.getElementById(`${prefix}-tangent`).value,
  };
}

function buildAssetExportDraft() {
  const capturePath = document.getElementById("asset-capture-source-path").value.trim();
  const exportScope = document.getElementById("asset-export-scope").value;
  const singlePass = getSelectedPassMeta("asset-pass-name");
  const startPass = getSelectedPassMeta("asset-pass-start");
  const endPass = getSelectedPassMeta("asset-pass-end");
  const singleManualEid = document.getElementById("asset-pass-manual-eid").value.trim();
  const startManualEid = document.getElementById("asset-pass-start-manual-eid").value.trim();
  const endManualEid = document.getElementById("asset-pass-end-manual-eid").value.trim();
  const captureFile = document.getElementById("asset-capture-file").files[0] || null;

  if (exportScope === "single" && !singlePass.id && !singleManualEid) {
    throw new Error("请先读取 Pass 列表并选择一个 Pass，或手动填写单个 EID。");
  }
  if (exportScope === "range" && (!(startPass.id || startManualEid) || !(endPass.id || endManualEid))) {
    throw new Error("请先读取 Pass 列表并选择起始/结束 Pass，或手动填写起始/结束 EID。");
  }
  if (!capturePath && !captureFile) {
    throw new Error("请先选择 .rdc 文件或填写路径。");
  }

  return {
    capturePath,
    captureFile,
    exportScope,
    passId: singleManualEid || singlePass.id,
    passName: singleManualEid || singlePass.label || singlePass.displayName || singlePass.name,
    passStartId: startManualEid || startPass.id,
    passStart: startManualEid || startPass.label || startPass.displayName || startPass.name,
    passEndId: endManualEid || endPass.id,
    passEnd: endManualEid || endPass.label || endPass.displayName || endPass.name,
    exportFbx: document.getElementById("asset-export-fbx").checked,
    exportObj: document.getElementById("asset-export-obj").checked,
    textureFormat: document.getElementById("asset-texture-format").value,
    notes: document.getElementById("asset-export-notes").value.trim(),
  };
}

async function requestAssetExportMappingPreview(draft) {
  let response;
  const formData = new FormData();
  formData.append("export_scope", draft.exportScope);
  formData.append("pass_id", draft.passId);
  formData.append("pass_name", draft.passName);
  formData.append("pass_start_id", draft.passStartId);
  formData.append("pass_start", draft.passStart);
  formData.append("pass_end_id", draft.passEndId);
  formData.append("pass_end", draft.passEnd);
  if (draft.capturePath) {
    formData.append("capture_path", draft.capturePath);
    response = await fetch("/api/asset-export/export-mapping-preview/by-path", {
      method: "POST",
      body: formData,
    });
  } else {
    formData.append("capture_file", draft.captureFile);
    response = await fetch("/api/asset-export/export-mapping-preview", {
      method: "POST",
      body: formData,
    });
  }
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "批量映射预览失败");
  }
  return data;
}

function renderAssetExportMappingPreview(preview, draft) {
  pendingAssetExportPreview = preview;
  document.getElementById("asset-export-mapping-summary").innerHTML = `
    <div><strong>范围:</strong> ${(preview.selected_passes || []).join(" -> ") || draft.exportScope}</div>
    <div><strong>样本 Pass:</strong> ${preview.sample_pass || "-"}</div>
    <div><strong>样本 Draw:</strong> EID ${preview.sample_eid || "-"} | ${preview.sample_draw_label || "-"}</div>
    <div><strong>阶段:</strong> ${(preview.sample_stage || "vsin").toUpperCase()}</div>
    <div><strong>列数:</strong> ${(preview.headers || []).length}</div>
    <div><strong>执行规则:</strong> 这里只确认一次样本映射；真正导出时会对每个 draw 单独自动识别，并对缺失列自动回退。</div>
  `;
  const warnings = (preview.skipped_attributes || []).map((item) => `<div class="meta">${escapeHtml(item)}</div>`).join("");
  document.getElementById("asset-export-mapping-notes").innerHTML = warnings || '<div class="meta">当前样本未发现被跳过的顶点属性。</div>';
  renderMappingOptionsToPrefix("batch-mapping", preview.headers || [], preview.suggested_mapping || {});
  renderMappingOptions(preview.headers || [], preview.suggested_mapping || {});
}

function renderAssetCsvInspectSummary(data) {
  const summary = document.getElementById("asset-csv-inspect-summary");
  const count = Number((data && data.csv_count) || 0);
  const sourceCount = Number((data && data.source_count) || 0);
  const sourcePreviewPaths = ((data && data.source_preview_paths) || []).slice(0, 5);
  const previewPaths = ((data && data.csv_preview_paths) || []).slice(0, 5);
  if (data && data.batch_mode) {
    summary.innerHTML = `
      <div><strong>输入来源:</strong> 共选择 ${sourceCount} 个路径</div>
      <div><strong>来源预览:</strong> ${sourcePreviewPaths.join("<br>") || "-"}</div>
      <div><strong>批处理模式:</strong> 共识别 ${count} 个 CSV</div>
      <div><strong>预览样本:</strong> ${data.inspect_csv_path || data.csv_name || "-"}</div>
      <div><strong>预览文件:</strong> ${previewPaths.join("<br>") || "-"}</div>
      <div><strong>执行规则:</strong> 预览只展示样本 CSV；真正转换时会对每个 CSV 单独自动识别，并对缺失列自动回退。</div>
    `;
    return;
  }
  summary.innerHTML = `
    <div><strong>单文件模式:</strong> ${data.inspect_csv_path || data.csv_name || "-"}</div>
    <div><strong>表头列数:</strong> ${(data.headers || []).length}</div>
    <div><strong>执行规则:</strong> 当前文件会按自动识别结果进行转换，你手动指定的列会优先覆盖。</div>
  `;
}

function renderAssetExportSummary(detail) {
  const metadata = detail.metadata || {};
  const input = metadata.input || {};
  const progress = metadata.progress || {};
  const result = metadata.result || {};
  const outputRoot = result.output_root || ((metadata.artifacts || {}).output_root) || "";
  document.getElementById("asset-export-summary").innerHTML = `
    <div><strong>Job:</strong> ${metadata.job_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>范围:</strong> ${input.export_scope || "-"}</div>
    <div><strong>单 Pass:</strong> ${input.pass_name || "-"}</div>
    <div><strong>起止:</strong> ${input.pass_start || "-"} -> ${input.pass_end || "-"}</div>
    <div><strong>格式:</strong> FBX=${String(input.export_fbx == null ? false : input.export_fbx)} / OBJ=${String(input.export_obj == null ? false : input.export_obj)}</div>
    <div><strong>贴图:</strong> ${input.texture_format || "-"}</div>
    <div><strong>导出目录:</strong> ${outputRoot || "未设置"}</div>
    <div><strong>阶段:</strong> ${progress.stage || "-"}</div>
    <div><strong>说明:</strong> ${progress.message || "-"}</div>
    <div><strong>CSV:</strong> ${(result.csv_files || []).length}</div>
    <div><strong>模型:</strong> ${(result.model_files || []).length}</div>
    <div><strong>Shader:</strong> ${(result.shader_files || []).length}</div>
    <div><strong>贴图:</strong> ${(result.texture_files || []).length}</div>
    <div><strong>失败:</strong> ${(result.failed_items || []).length}</div>
    ${outputRoot ? `<div><button id="asset-export-open-output-btn" type="button" class="secondary-btn">打开输出目录</button></div>` : ""}
  `;
  const openButton = document.getElementById("asset-export-open-output-btn");
  if (openButton) {
    openButton.addEventListener("click", () => {
      revealDesktopPath(outputRoot);
    });
  }
  document.getElementById("asset-export-log").textContent = detail.job_log || "暂无日志";
  renderAssetExportFiles(metadata.job_id, detail.manifest || {});
}

function renderAssetExportJobs(jobs) {
  const container = document.getElementById("asset-export-jobs-list");
  container.innerHTML = "";
  if (!jobs.length) {
    container.innerHTML = '<div class="empty-state">暂无资产导出任务</div>';
    return;
  }
  jobs.forEach((item) => {
    const input = item.input || {};
    const div = document.createElement("div");
    div.className = "session-item" + (item.job_id === currentExportJobId ? " active" : "");
    div.innerHTML = `
      <div class="title">${input.capture_name || item.job_id}</div>
      <div class="meta">${item.updated_at || ""}</div>
      <div class="meta">范围: ${input.export_scope || "-"}</div>
      <div class="meta">状态: ${item.status || "-"}</div>
    `;
    div.addEventListener("click", async () => {
      await loadAssetExportJob(item.job_id);
    });
    container.appendChild(div);
  });
}

function renderAssetExportFiles(jobId, manifest) {
  const container = document.getElementById("asset-export-files");
  container.innerHTML = "";
  const passItems = manifest.items || [];
  const manualConversions = manifest.manual_conversions || [];
  if (!passItems.length && !manualConversions.length) {
    container.innerHTML = '<div class="empty-state">暂无导出产物</div>';
    return;
  }

  if (manualConversions.length) {
    const block = document.createElement("div");
    block.className = "session-item";
    const lines = manualConversions.map((item) => `
      <div class="meta">${item.csv_name || "-"} -> <a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(item.output_path)}" target="_blank" rel="noopener">${item.output_format || "文件"}</a> · ${item.output_path || ""}</div>
      <div class="meta">自动识别: ${Object.entries(item.mapping_suggested || {}).filter(([, value]) => value).map(([key, value]) => `${key}=${value}`).join(" | ") || "无"}</div>
      <div class="meta">实际映射: ${Object.entries(item.mapping_applied || {}).filter(([, value]) => value).map(([key, value]) => `${key}=${value}`).join(" | ") || "无"}</div>
      <div class="meta">${(item.mapping_notes || []).length ? (item.mapping_notes || []).join("；") : "未发生字段回退。"}</div>
    `).join("");
    block.innerHTML = `
      <div class="title">手工 CSV 转换</div>
      ${lines}
    `;
    container.appendChild(block);
  }

  passItems.forEach((passItem) => {
    const block = document.createElement("div");
    block.className = "session-item";
    const drawLines = (passItem.draws || []).slice(0, 20).map((draw) => {
      const links = [];
      if (draw.mesh_csv) {
        links.push(`<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(draw.mesh_csv)}" target="_blank" rel="noopener">CSV</a>`);
      }
      if (draw.mesh_obj) {
        links.push(`<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(draw.mesh_obj)}" target="_blank" rel="noopener">OBJ</a>`);
      }
      if (draw.mesh_fbx) {
        links.push(`<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(draw.mesh_fbx)}" target="_blank" rel="noopener">FBX</a>`);
      }
      if (draw.shader_vertex) {
        links.push(`<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(draw.shader_vertex)}" target="_blank" rel="noopener">VS GLSL</a>`);
      }
      if (draw.shader_fragment) {
        links.push(`<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(draw.shader_fragment)}" target="_blank" rel="noopener">FS GLSL</a>`);
      }
      if (draw.shader_params) {
        links.push(`<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(draw.shader_params)}" target="_blank" rel="noopener">参数</a>`);
      }
      const textureLinks = (draw.textures || []).slice(0, 4).map((path, index) =>
        `<a href="/api/asset-export/jobs/${jobId}/artifact?path=${encodeURIComponent(path)}" target="_blank" rel="noopener">贴图${index + 1}</a>`
      );
      const mapping = draw.mapping_suggested || {};
      const mappingSummary = Object.entries(mapping)
        .filter(([, value]) => value)
        .map(([key, value]) => `${key}=${value}`)
        .join(" | ");
      return `
        <div class="meta">EID ${draw.eid || "-"} · ${draw.label || "-"} · ${[...links, ...textureLinks].join(" | ") || "无产物"}</div>
        <div class="meta">阶段: ${(draw.mesh_stage || "-").toUpperCase()}${mappingSummary ? ` · 自动映射: ${mappingSummary}` : ""}</div>
      `;
    }).join("");
    const moreDraws = (passItem.draws || []).length > 20
      ? `<div class="meta">其余 ${(passItem.draws || []).length - 20} 个 draw 已省略显示</div>`
      : "";
    block.innerHTML = `
      <div class="title">${passItem.pass_name || "-"}</div>
      <div class="meta">Draw 数: ${(passItem.draws || []).length}</div>
      ${drawLines || '<div class="meta">该 Pass 暂无可导出 draw</div>'}
      ${moreDraws}
    `;
    container.appendChild(block);
  });
}

// (Shader convert functions moved to standalone tool)

async function loadCmpJobs() {
  const jobs = await fetchJson("/api/renderdoc-cmp/jobs");
  renderCmpJobs(jobs);
}

async function loadPerfJobs() {
  const jobs = await fetchJson("/api/renderdoc-perf/jobs");
  renderPerfJobs(jobs);
}

async function loadAssetExportJobs() {
  const jobs = await fetchJson("/api/asset-export/jobs");
  renderAssetExportJobs(jobs);
}

async function loadCmpJob(jobId) {
  const detail = await fetchJson(`/api/renderdoc-cmp/jobs/${jobId}`);
  currentCmpJobId = jobId;
  renderCmpSummary(detail);
  await loadCmpJobs();
}

async function loadPerfJob(jobId) {
  const detail = await fetchJson(`/api/renderdoc-perf/jobs/${jobId}`);
  currentPerfJobId = jobId;
  renderPerfSummary(detail);
  await loadPerfJobs();
}

async function loadAssetExportJob(jobId) {
  const detail = await fetchJson(`/api/asset-export/jobs/${jobId}`);
  currentExportJobId = jobId;
  renderAssetExportSummary(detail);
  await loadAssetExportJobs();
}

async function handleCmpRun(event) {
  event.preventDefault();

  const basePath = document.getElementById("cmp-base-path").value.trim();
  const newPath = document.getElementById("cmp-new-path").value.trim();
  const strictMode = document.getElementById("cmp-strict-mode").checked ? "true" : "false";
  const verbose = document.getElementById("cmp-verbose").checked ? "true" : "false";
  const renderdocDir = document.getElementById("cmp-renderdoc-dir").value.trim();
  const maliocPath = document.getElementById("cmp-malioc-path").value.trim();

  const button = document.getElementById("cmp-run-btn");
  button.disabled = true;
  button.textContent = "运行中...";
  setSummaryBusy("cmp-summary", [
    "状态: 运行中",
    `Base: ${basePath || document.getElementById('cmp-base-file').files[0]?.name || "-"}`,
    `New: ${newPath || document.getElementById('cmp-new-file').files[0]?.name || "-"}`,
    "说明: 正在执行性能 Diff，请稍候...",
  ]);
  setLogBusy("cmp-run-log", "正在执行 renderdoc_cmp，请稍候...");

  try {
    let response;
    if (basePath && newPath) {
      const formData = new FormData();
      formData.append("base_path", basePath);
      formData.append("new_path", newPath);
      formData.append("strict_mode", strictMode);
      formData.append("verbose", verbose);
      formData.append("renderdoc_dir", renderdocDir);
      formData.append("malioc_path", maliocPath);
      response = await fetch("/api/renderdoc-cmp/compare/by-path", {
        method: "POST",
        body: formData,
      });
    } else {
      const formData = new FormData();
      const baseFile = document.getElementById("cmp-base-file").files[0];
      const newFile = document.getElementById("cmp-new-file").files[0];
      if (!baseFile || !newFile) {
        throw new Error("请提供 base/new 两个 .rdc 路径。");
      }
      formData.append("base_file", baseFile);
      formData.append("new_file", newFile);
      formData.append("strict_mode", strictMode);
      formData.append("verbose", verbose);
      formData.append("renderdoc_dir", renderdocDir);
      formData.append("malioc_path", maliocPath);
      response = await fetch("/api/renderdoc-cmp/compare", {
        method: "POST",
        body: formData,
      });
    }
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "renderdoc_cmp 运行失败");
    }
    currentCmpJobId = data.metadata.job_id;
    renderCmpSummary(data);
    await loadCmpJobs();
    switchTab("cmp");
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "执行性能 Diff";
  }
}

async function handlePerfRun(event) {
  event.preventDefault();

  const capturePath = document.getElementById("perf-capture-path").value.trim();
  const renderdocDir = document.getElementById("perf-renderdoc-dir").value.trim();
  const button = document.getElementById("perf-run-btn");
  button.disabled = true;
  button.textContent = "分析中...";
  setSummaryBusy("perf-summary", [
    "状态: 运行中",
    `Capture: ${capturePath || document.getElementById('perf-capture-file').files[0]?.name || "-"}`,
    renderdocDir ? `RenderDoc: ${renderdocDir}` : "",
    "说明: 正在执行单帧性能分析，请稍候...",
  ].filter(Boolean));
  setLogBusy("perf-run-log", "正在读取 draw/counter 并分析性能，请稍候...");

  try {
    let response;
    if (capturePath) {
      const formData = new FormData();
      formData.append("capture_path", capturePath);
      formData.append("renderdoc_dir", renderdocDir);
      response = await fetch("/api/renderdoc-perf/analyze/by-path", {
        method: "POST",
        body: formData,
      });
    } else {
      const captureFile = document.getElementById("perf-capture-file").files[0];
      if (!captureFile) {
        throw new Error("请提供一个 .rdc 路径或文件。");
      }
      const formData = new FormData();
      formData.append("capture_file", captureFile);
      formData.append("renderdoc_dir", renderdocDir);
      response = await fetch("/api/renderdoc-perf/analyze", {
        method: "POST",
        body: formData,
      });
    }
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "性能分析失败");
    }
    currentPerfJobId = data.metadata.job_id;
    renderPerfSummary(data);
    await loadPerfJobs();
    switchTab("perf");
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "执行性能分析";
  }
}

async function handleAssetPassScan(event) {
  event.preventDefault();
  const capturePath = document.getElementById("asset-capture-source-path").value.trim();
  const button = document.getElementById("asset-pass-scan-btn");
  button.disabled = true;
  button.textContent = "读取中...";
  setLogBusy("asset-pass-scan-output", "正在读取 Pass 列表，请稍候...");
  try {
    let response;
    if (capturePath) {
      const formData = new FormData();
      formData.append("capture_path", capturePath);
      response = await fetch("/api/asset-export/scan-passes/by-path", {
        method: "POST",
        body: formData,
      });
    } else {
      const captureFile = document.getElementById("asset-capture-file").files[0];
      if (!captureFile) {
        throw new Error("请先选择 .rdc 文件或填写路径。");
      }
      const formData = new FormData();
      formData.append("capture_file", captureFile);
      response = await fetch("/api/asset-export/scan-passes", {
        method: "POST",
        body: formData,
      });
    }
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "读取 Pass 列表失败");
    }
    renderAssetPassScan(data);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "读取 Pass 列表";
  }
}

async function handleAssetCsvInspect(event) {
  event.preventDefault();
  const csvPath = document.getElementById("asset-csv-source-path").value.trim();
  const button = document.getElementById("asset-csv-inspect-btn");
  button.disabled = true;
  button.textContent = "识别中...";
  setSummaryBusy("asset-csv-inspect-summary", [
    "状态: 运行中",
    "说明: 正在识别 CSV 列映射，请稍候...",
  ]);
  try {
    let response;
    if (csvPath) {
      const formData = new FormData();
      formData.append("csv_path", csvPath);
      response = await fetch("/api/asset-export/csv-inspect/by-path", {
        method: "POST",
        body: formData,
      });
    } else {
      const csvFile = document.getElementById("asset-csv-file").files[0];
      if (!csvFile) {
        throw new Error("请先选择 CSV 文件、多个 CSV 路径，或填写目录路径。");
      }
      const formData = new FormData();
      formData.append("csv_file", csvFile);
      response = await fetch("/api/asset-export/csv-inspect", {
        method: "POST",
        body: formData,
      });
    }
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "CSV 识别失败");
    }
    renderMappingOptions(data.headers || [], data.suggested_mapping || {});
    renderAssetCsvInspectSummary(data);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "识别列映射";
  }
}

async function handleAssetExportCreate(event) {
  event.preventDefault();
  const button = document.getElementById("asset-export-create-btn");
  button.disabled = true;
  button.textContent = "正在准备样本映射...";
  try {
    const draft = buildAssetExportDraft();
    if (!draft.exportFbx && !draft.exportObj) {
      await submitAssetExportDraft(draft, {});
      return;
    }
    const preview = await requestAssetExportMappingPreview(draft);
    pendingAssetExportDraft = draft;
    renderAssetExportMappingPreview(preview, draft);
    showAssetExportMappingModal();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "确认范围并准备批量映射";
  }
}

async function submitAssetExportDraft(draft, mapping) {
  let response;
  setSummaryBusy("asset-export-summary", [
    "状态: 运行中",
    `范围: ${draft.exportScope || "-"}`,
    `单 Pass: ${draft.passName || "-"}`,
    "说明: 正在导出资产与 shader，请稍候...",
  ]);
  setLogBusy("asset-export-log", "正在执行资产导出，请稍候...");
  const commonForm = new FormData();
  commonForm.append("export_scope", draft.exportScope);
  commonForm.append("pass_id", draft.passId);
  commonForm.append("pass_name", draft.passName);
  commonForm.append("pass_start_id", draft.passStartId);
  commonForm.append("pass_start", draft.passStart);
  commonForm.append("pass_end_id", draft.passEndId);
  commonForm.append("pass_end", draft.passEnd);
  commonForm.append("export_fbx", draft.exportFbx ? "true" : "false");
  commonForm.append("export_obj", draft.exportObj ? "true" : "false");
  commonForm.append("texture_format", draft.textureFormat);
  commonForm.append("notes", draft.notes);
  Object.entries(mapping || {}).forEach(([key, value]) => {
    commonForm.append(key, value || "");
  });

  if (draft.capturePath) {
    commonForm.append("capture_path", draft.capturePath);
    response = await fetch("/api/asset-export/jobs/by-path", {
      method: "POST",
      body: commonForm,
    });
  } else {
    commonForm.append("capture_file", draft.captureFile);
    commonForm.append("capture_source_path", draft.capturePath);
    response = await fetch("/api/asset-export/jobs", {
      method: "POST",
      body: commonForm,
    });
  }
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "保存资产导出任务失败");
  }
  currentExportJobId = data.metadata.job_id;
  renderAssetExportSummary(data);
  await loadAssetExportJobs();
  const outputRoot = (((data || {}).metadata || {}).result || {}).output_root
    || ((((data || {}).metadata || {}).artifacts || {}).output_root)
    || "";
  if (outputRoot) {
    window.setTimeout(() => {
      revealDesktopPath(outputRoot);
    }, 50);
  }
  switchTab("asset-export");
}

async function handleAssetExportMappingConfirm() {
  if (!pendingAssetExportDraft) {
    alert("当前没有待确认的批量导出请求。");
    return;
  }
  const button = document.getElementById("asset-export-mapping-confirm-btn");
  button.disabled = true;
  button.textContent = "导出中...";
  try {
    const mapping = collectMappingFromPrefix("batch-mapping");
    if (!mapping.position) {
      throw new Error("批量映射确认里 Position 不能为空。");
    }
    renderMappingOptionsToPrefix("mapping", (pendingAssetExportPreview && pendingAssetExportPreview.headers) || [], mapping);
    await submitAssetExportDraft(pendingAssetExportDraft, mapping);
    hideAssetExportMappingModal();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "确认映射并开始导出";
  }
}

async function handleAssetCsvConvert() {
  const csvPath = document.getElementById("asset-csv-source-path").value.trim();

  const button = document.getElementById("asset-csv-convert-btn");
  button.disabled = true;
  button.textContent = "转换中...";
  try {
    let response;
    const formData = new FormData();
    formData.append("output_format", document.getElementById("mapping-output-format").value);
    formData.append("position", document.getElementById("mapping-position").value);
    formData.append("normal", document.getElementById("mapping-normal").value);
    formData.append("uv0", document.getElementById("mapping-uv0").value);
    formData.append("uv1", document.getElementById("mapping-uv1").value);
    formData.append("uv2", document.getElementById("mapping-uv2").value);
    formData.append("uv3", document.getElementById("mapping-uv3").value);
    formData.append("color", document.getElementById("mapping-color").value);
    formData.append("tangent", document.getElementById("mapping-tangent").value);
    if (csvPath) {
      formData.append("csv_path", csvPath);
      const targetUrl = currentExportJobId
        ? `/api/asset-export/jobs/${currentExportJobId}/convert-csv/by-path`
        : "/api/asset-export/convert-csv/by-path";
      response = await fetch(targetUrl, {
        method: "POST",
        body: formData,
      });
    } else {
      const csvFile = document.getElementById("asset-csv-file").files[0];
      if (!csvFile) {
        throw new Error("请先选择 CSV 文件、多个 CSV 路径，或填写目录路径。");
      }
      formData.append("csv_file", csvFile);
      formData.append("csv_source_path", csvPath);
      const targetUrl = currentExportJobId
        ? `/api/asset-export/jobs/${currentExportJobId}/convert-csv`
        : "/api/asset-export/convert-csv";
      response = await fetch(targetUrl, {
        method: "POST",
        body: formData,
      });
    }
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "CSV 转换失败");
    }
    currentExportJobId = (((data || {}).metadata || {}).job_id) || currentExportJobId;
    renderAssetExportSummary(data);
    await loadAssetExportJobs();
    switchTab("asset-export");
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "按当前映射开始批量转换";
  }
}

async function handleSetupSave(event) {
  event.preventDefault();

  const formData = new FormData();
  formData.append("renderdoc_python_path", document.getElementById("setup-renderdoc-python-path").value.trim());
  formData.append("llm_provider", "local");
  formData.append("openai_base_url", "");
  formData.append("openai_api_key", "");
  formData.append("openai_model", "");
  formData.append("renderdoc_cmp_root", document.getElementById("setup-cmp-root").value.trim());
  formData.append("setup_completed", "true");

  const button = document.getElementById("setup-save-btn");
  button.disabled = true;
  button.textContent = "保存中...";

  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "保存设置失败");
    }
    renderHealth(data);
    fillSetupForm(data);
    hideSetupModal();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "保存并应用";
  }
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.getElementById("cmp-form").addEventListener("submit", handleCmpRun);
document.getElementById("perf-form").addEventListener("submit", handlePerfRun);
document.getElementById("asset-pass-scan-form").addEventListener("submit", handleAssetPassScan);
document.getElementById("asset-export-form").addEventListener("submit", handleAssetExportCreate);
document.getElementById("asset-csv-inspect-form").addEventListener("submit", handleAssetCsvInspect);
document.getElementById("asset-csv-convert-btn").addEventListener("click", handleAssetCsvConvert);
document.getElementById("setup-form").addEventListener("submit", handleSetupSave);
document.getElementById("refresh-health-btn").addEventListener("click", loadHealth);
document.getElementById("open-setup-btn").addEventListener("click", showSetupModal);
document.getElementById("setup-close-btn").addEventListener("click", hideSetupModal);
document.getElementById("asset-export-mapping-confirm-btn").addEventListener("click", handleAssetExportMappingConfirm);
document.getElementById("asset-export-mapping-cancel-btn").addEventListener("click", hideAssetExportMappingModal);
document.getElementById("pick-cmp-base-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "cmp-base-path"));
document.getElementById("pick-cmp-new-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "cmp-new-path"));
document.getElementById("pick-perf-capture-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "perf-capture-path"));
document.getElementById("pick-perf-renderdoc-dir-btn").addEventListener("click", () => pickDesktopDirectory("perf-renderdoc-dir"));
document.getElementById("pick-cmp-renderdoc-dir-btn").addEventListener("click", () => pickDesktopDirectory("cmp-renderdoc-dir"));
document.getElementById("pick-asset-capture-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "asset-capture-source-path"));
document.getElementById("pick-asset-csv-path-btn").addEventListener("click", () => pickDesktopCsvFiles("asset-csv-source-path"));
document.getElementById("pick-asset-csv-dir-btn").addEventListener("click", () => pickDesktopDirectory("asset-csv-source-path"));
document.getElementById("perf-sort-field").addEventListener("change", renderPerfTable);
document.getElementById("perf-sort-direction").addEventListener("change", renderPerfTable);
document.getElementById("perf-preview-panel-close").addEventListener("click", () => hidePerfPreviewPanel(true));
document.getElementById("perf-preview-panel").addEventListener("mouseenter", () => {
  if (perfPreviewHideTimer) {
    window.clearTimeout(perfPreviewHideTimer);
    perfPreviewHideTimer = null;
  }
});
document.getElementById("perf-preview-panel").addEventListener("mouseleave", () => scheduleHidePerfPreview());
document.addEventListener("mouseover", (event) => {
  const trigger = event.target.closest(".perf-preview-trigger");
  if (!trigger) {
    return;
  }
  showPerfPreviewPanel({
    src: trigger.dataset.previewSrc || "",
    title: trigger.dataset.previewTitle || "",
    meta: trigger.dataset.previewMeta || "",
    pinned: false,
    anchorX: event.clientX || 0,
    anchorY: event.clientY || 0,
  });
});
document.addEventListener("mousemove", (event) => {
  if (perfPreviewPinned) {
    return;
  }
  const trigger = event.target.closest(".perf-preview-trigger");
  if (!trigger) {
    return;
  }
  const panel = document.getElementById("perf-preview-panel");
  if (panel.classList.contains("hidden")) {
    return;
  }
  positionPerfPreviewPanel(panel, event.clientX || 0, event.clientY || 0);
});
document.addEventListener("mouseout", (event) => {
  const trigger = event.target.closest(".perf-preview-trigger");
  if (!trigger) {
    return;
  }
  if (event.relatedTarget && trigger.contains(event.relatedTarget)) {
    return;
  }
  scheduleHidePerfPreview();
});
document.addEventListener("click", (event) => {
  const trigger = event.target.closest(".perf-preview-trigger");
  if (trigger) {
    event.preventDefault();
    showPerfPreviewPanel({
      src: trigger.dataset.previewSrc || "",
      title: trigger.dataset.previewTitle || "",
      meta: trigger.dataset.previewMeta || "",
      pinned: true,
      anchorX: event.clientX || 0,
      anchorY: event.clientY || 0,
    });
    return;
  }
  if (!event.target.closest("#perf-preview-panel")) {
    hidePerfPreviewPanel(true);
  }
});
// --- Visual Probe Simplification ---

async function handleVisualProbeRun(event) {
  event.preventDefault();
  const capturePath = document.getElementById("vp-capture-path").value.trim();
  const eid = document.getElementById("vp-eid").value.trim();
  const stage = document.getElementById("vp-stage").value;
  const ssimThreshold = document.getElementById("vp-ssim-threshold").value.trim();
  const maxProbes = document.getElementById("vp-max-probes").value.trim();
  const compileOnly = document.getElementById("vp-compile-only").checked;
  const useLlm = document.getElementById("vp-use-llm").checked;
  const summaryEl = document.getElementById("vp-result-summary");
  const btn = document.getElementById("vp-run-btn");
  const progressArea = document.getElementById("vp-progress-area");
  const progressBar = document.getElementById("vp-progress-bar");
  const progressText = document.getElementById("vp-progress-text");
  const completionLog = document.getElementById("vp-completion-log");
  const completionLogText = document.getElementById("vp-completion-log-text");

  if (!capturePath || !eid) {
    summaryEl.textContent = "请先填写 RDC 路径和 EID。";
    return;
  }

  btn.disabled = true;
  btn.textContent = "简化中...";
  summaryEl.textContent = "";
  summaryEl.className = "empty-state";
  document.getElementById("vp-result-detail").classList.add("hidden");
  completionLog.classList.add("hidden");

  progressArea.classList.remove("hidden");
  progressBar.style.width = "0%";
  progressBar.classList.add("indeterminate");
  progressText.textContent = "正在执行视觉探针简化（L0-L4 静态简化 → 候选分析 → 逐候选验证）...";

  const t0 = performance.now();
  const progressTimer = setInterval(() => {
    const elapsed = Math.round((performance.now() - t0) / 1000);
    progressText.textContent = `正在执行视觉探针简化... 已耗时 ${elapsed}s`;
  }, 1000);

  const body = new FormData();
  body.append("capture_path", capturePath);
  body.append("eid", eid);
  body.append("stage", stage);
  body.append("ssim_threshold", ssimThreshold || "0.995");
  body.append("max_probes", maxProbes || "200");
  body.append("compile_only", compileOnly ? "true" : "false");
  body.append("use_llm", useLlm ? "true" : "false");

  try {
    const data = await fetchJson("/api/visual-probe/run", { method: "POST", body });
    clearInterval(progressTimer);
    progressBar.classList.remove("indeterminate");
    progressBar.style.width = "100%";
    const totalSec = Math.round((performance.now() - t0) / 1000);
    progressText.textContent = `完成！总耗时 ${totalSec}s`;

    renderVisualProbeResult(data);

    const logLines = [];
    logLines.push(`原始 ${data.original_lines} 行 → 静态 ${data.static_simplified_lines} 行 → 最终 ${data.final_lines} 行`);
    logLines.push(`总缩减: ${data.reduction_total_pct}%  |  视觉优化: ${data.reduction_visual_pct}%`);
    logLines.push(`探针: ${data.accepted_probes}/${data.total_probes} 接受  |  ${data.rejected_probes} 拒绝  |  ${data.compile_failed_probes} 编译失败`);
    logLines.push(`耗时: ${data.elapsed_total_ms}ms  |  模式: ${data.mode}`);
    completionLogText.textContent = logLines.join("\n");
    completionLog.classList.remove("hidden");

    setTimeout(() => { progressArea.classList.add("hidden"); }, 3000);

    const heading = document.getElementById("vp-result-heading");
    if (heading) {
      const rect = heading.getBoundingClientRect();
      const offset = window.innerHeight * 0.10;
      window.scrollTo({ top: window.scrollY + rect.top - offset, behavior: "smooth" });
    }
  } catch (err) {
    clearInterval(progressTimer);
    progressBar.classList.remove("indeterminate");
    progressBar.style.width = "100%";
    progressBar.style.background = "#da3633";
    progressText.textContent = "执行失败";
    summaryEl.textContent = `简化失败: ${err.message}`;
    summaryEl.style.color = "#f44336";
  } finally {
    btn.disabled = false;
    btn.textContent = "执行视觉探针简化";
  }
}

function renderVisualProbeResult(data) {
  const summaryEl = document.getElementById("vp-result-summary");
  const detailEl = document.getElementById("vp-result-detail");

  summaryEl.textContent = `简化完成: ${data.original_lines} → ${data.static_simplified_lines} → ${data.final_lines} 行 (总缩减 ${data.reduction_total_pct}%)`;
  summaryEl.style.color = "#3fb950";
  summaryEl.style.fontWeight = "700";

  detailEl.classList.remove("hidden");
  document.getElementById("vp-lines-original").textContent = data.original_lines;
  document.getElementById("vp-lines-static").textContent = data.static_simplified_lines;
  document.getElementById("vp-lines-final").textContent = data.final_lines;
  document.getElementById("vp-reduction-pct").textContent = data.reduction_total_pct + "%";
  document.getElementById("vp-total-probes").textContent = data.total_probes;
  document.getElementById("vp-accepted-probes").textContent = data.accepted_probes;
  document.getElementById("vp-rejected-probes").textContent = data.rejected_probes;
  document.getElementById("vp-compile-failed").textContent = data.compile_failed_probes;
  document.getElementById("vp-final-source").value = data.final_source || "";

  const stepsEl = document.getElementById("vp-probe-steps");
  const steps = data.probe_steps || [];
  if (steps.length === 0) {
    stepsEl.innerHTML = '<div class="empty-state">无探针步骤记录</div>';
  } else {
    stepsEl.innerHTML = steps.map((s) => {
      const cls = s.accepted ? "accepted" : (!s.compile_ok ? "compile-fail" : "rejected");
      const statusCls = s.accepted ? "pass" : (!s.compile_ok ? "warn" : "fail");
      const statusText = s.accepted ? "ACCEPT" : (!s.compile_ok ? "COMPILE" : "REJECT");
      const ssimText = s.ssim ? ` SSIM=${s.ssim.toFixed(4)}` : "";
      const errText = s.error ? ` ${s.error}` : "";
      return `<div class="vp-probe-item ${cls}">
        <span class="vp-probe-kind">${s.kind}</span>
        <span class="vp-probe-status ${statusCls}">${statusText}</span>
        <span class="vp-probe-desc">${s.description || s.label}${ssimText}${errText}</span>
      </div>`;
    }).join("");
  }
}

document.getElementById("vp-form").addEventListener("submit", handleVisualProbeRun);
document.getElementById("pick-vp-capture-btn").addEventListener("click", () =>
  pickDesktopFile("pick_rdc_file", "vp-capture-path")
);
document.getElementById("vp-copy-final-btn").addEventListener("click", async () => {
  const el = document.getElementById("vp-final-source");
  try {
    await navigator.clipboard.writeText(el.value);
    const btn = document.getElementById("vp-copy-final-btn");
    btn.textContent = "已复制";
    setTimeout(() => { btn.textContent = "复制"; }, 1500);
  } catch (e) {
    alert("复制失败: " + e.message);
  }
});

// (Shader Verify / Simplify / HLSL Verify / OneClick moved to standalone tool)
if (false) {
async function handleShaderVerifyFetchSource(event) {
  if (event) event.preventDefault();
  const capturePath = document.getElementById("sv-capture-path").value.trim();
  const eid = document.getElementById("sv-eid").value.trim();
  const stage = document.getElementById("sv-stage").value;
  const statusEl = document.getElementById("sv-source-status");
  const sourceEl = document.getElementById("sv-original-source");
  const modifiedEl = document.getElementById("sv-modified-glsl");

  if (!capturePath || !eid) {
    statusEl.textContent = "请先填写 capture 路径和 EID。";
    return;
  }
  statusEl.textContent = "正在读取 shader 源码...";
  const body = new FormData();
  body.append("capture_path", capturePath);
  body.append("eid", eid);
  body.append("stage", stage);
  try {
    const data = await fetchJson("/api/shader-verify/get-shader-source", { method: "POST", body });
    const src = data.source || "";
    sourceEl.value = src;
    if (!modifiedEl.value.trim()) {
      modifiedEl.value = src;
    }
    statusEl.textContent = src
      ? `已读取 (${src.length} 字符, target=${data.target || "?"}, mode=${data.export_mode || "?"})`
      : "该 EID / Stage 没有可用的 shader 源码。";
  } catch (err) {
    statusEl.textContent = `读取失败: ${err.message}`;
  }
}

async function handleShaderVerifyRun(event) {
  event.preventDefault();
  const capturePath = document.getElementById("sv-capture-path").value.trim();
  const eid = document.getElementById("sv-eid").value.trim();
  const stage = document.getElementById("sv-stage").value;
  const modifiedGlsl = document.getElementById("sv-modified-glsl").value;
  const summaryEl = document.getElementById("sv-result-summary");
  const btn = document.getElementById("sv-run-btn");

  if (!capturePath || !eid || !modifiedGlsl.trim()) {
    summaryEl.textContent = "请先填写 capture 路径、EID 和修改后的 GLSL。";
    summaryEl.className = "empty-state";
    return;
  }

  btn.disabled = true;
  btn.textContent = "验证中...";
  summaryEl.textContent = "正在执行 Shader 替换验证...";
  summaryEl.className = "empty-state";

  const body = new FormData();
  body.append("capture_path", capturePath);
  body.append("eid", eid);
  body.append("stage", stage);
  body.append("modified_glsl", modifiedGlsl);

  try {
    const data = await fetchJson("/api/shader-verify/compare", { method: "POST", body });
    renderShaderVerifyResult(data);
  } catch (err) {
    summaryEl.textContent = `验证失败: ${err.message}`;
    summaryEl.className = "empty-state";
  } finally {
    btn.disabled = false;
    btn.textContent = "执行 Shader 替换验证";
  }
}

function renderShaderVerifyResult(data) {
  const summaryEl = document.getElementById("sv-result-summary");
  const metricsEl = document.getElementById("sv-metrics");
  const imagesRow = document.getElementById("sv-images-row");
  const compileInfo = document.getElementById("sv-compile-info");

  if (data.error) {
    summaryEl.textContent = `错误: ${data.error}`;
    summaryEl.className = "empty-state";
    metricsEl.classList.add("hidden");
    imagesRow.style.display = "none";
    compileInfo.textContent = data.compile_errors || "—";
    return;
  }

  const passed = data.passed;
  summaryEl.textContent = passed ? "PASSED — 视觉等效" : "FAILED — 视觉不一致";
  summaryEl.className = passed ? "empty-state" : "empty-state";
  summaryEl.style.color = passed ? "#4caf50" : "#f44336";
  summaryEl.style.fontWeight = "700";
  summaryEl.style.fontSize = "20px";

  metricsEl.classList.remove("hidden");
  const ssimEl = document.getElementById("sv-ssim");
  const psnrEl = document.getElementById("sv-psnr");
  const rmseEl = document.getElementById("sv-rmse");

  ssimEl.textContent = data.ssim != null ? data.ssim.toFixed(4) : "—";
  ssimEl.className = "metric-value " + (data.ssim >= 0.98 ? "pass" : "fail");
  psnrEl.textContent = data.psnr === Infinity ? "∞" : (data.psnr != null ? data.psnr.toFixed(2) + " dB" : "—");
  psnrEl.className = "metric-value";
  rmseEl.textContent = data.rmse != null ? data.rmse.toFixed(4) : "—";
  rmseEl.className = "metric-value";

  if (data.baseline_path || data.candidate_path || data.diff_image_path) {
    imagesRow.style.display = "";
    if (data.baseline_path) document.getElementById("sv-baseline-img").src = data.baseline_path;
    if (data.candidate_path) document.getElementById("sv-candidate-img").src = data.candidate_path;
    if (data.diff_image_path) document.getElementById("sv-diff-img").src = data.diff_image_path;
  } else {
    imagesRow.style.display = "none";
  }

  compileInfo.textContent = data.compile_ok
    ? (data.compile_errors || "编译成功，无警告。")
    : (data.compile_errors || "编译失败。");
}

document.getElementById("sv-fetch-source-btn").addEventListener("click", handleShaderVerifyFetchSource);
document.getElementById("shader-verify-form").addEventListener("submit", handleShaderVerifyRun);
document.getElementById("pick-sv-capture-path-btn").addEventListener("click", () =>
  pickDesktopFile("pick_rdc_file", "sv-capture-path")
);

// --- GLSL Simplify ---

async function handleShaderSimplifyRun(event) {
  event.preventDefault();
  const capturePath = document.getElementById("ss-capture-path").value.trim();
  const eid = document.getElementById("ss-eid").value.trim();
  const stage = document.getElementById("ss-stage").value;
  const summaryEl = document.getElementById("ss-result-summary");
  const btn = document.getElementById("ss-run-btn");

  if (!capturePath || !eid) {
    summaryEl.textContent = "请先填写 capture 路径和 EID。";
    return;
  }

  btn.disabled = true;
  btn.textContent = "简化中...";
  summaryEl.textContent = "正在执行 GLSL 自动简化（可能需要较长时间）...";
  summaryEl.className = "empty-state";

  const body = new FormData();
  body.append("capture_path", capturePath);
  body.append("eid", eid);
  body.append("stage", stage);

  try {
    const data = await fetchJson("/api/shader-simplify/run", { method: "POST", body });
    renderShaderSimplifyResult(data);
  } catch (err) {
    summaryEl.textContent = `简化失败: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "执行 GLSL 自动简化";
  }
}

function renderShaderSimplifyResult(data) {
  const summaryEl = document.getElementById("ss-result-summary");
  const detailEl = document.getElementById("ss-result-detail");

  summaryEl.textContent = `简化完成: ${data.original_line_count}→${data.simplified_line_count} 行 (缩减 ${data.reduction_pct}%), 最终 SSIM=${data.final_ssim.toFixed(4)}`;
  summaryEl.style.color = data.final_ssim >= 0.98 ? "#4caf50" : "#ff9800";
  summaryEl.style.fontWeight = "700";

  detailEl.classList.remove("hidden");
  document.getElementById("ss-lines-before").textContent = data.original_line_count;
  document.getElementById("ss-lines-after").textContent = data.simplified_line_count;
  document.getElementById("ss-reduction").textContent = data.reduction_pct + "%";
  document.getElementById("ss-reduction").className = "metric-value " + (data.reduction_pct > 20 ? "pass" : "");
  document.getElementById("ss-simplified-source").value = data.simplified_source || "";

  const steps = data.steps || [];
  const logLines = steps.map((s) =>
    `Step ${s.step} [${s.levels}] ${s.action}: ${s.lines_before}→${s.lines_after} lines, SSIM=${s.ssim.toFixed(4)}, compile=${s.compile_ok}`
  );
  document.getElementById("ss-transform-log").textContent = logLines.join("\n") || "无变换记录";
}

document.getElementById("shader-simplify-form").addEventListener("submit", handleShaderSimplifyRun);
document.getElementById("pick-ss-capture-path-btn").addEventListener("click", () =>
  pickDesktopFile("pick_rdc_file", "ss-capture-path")
);
document.getElementById("ss-copy-simplified-btn").addEventListener("click", async () => {
  try { await copyTextFromElement("ss-simplified-source"); } catch (e) { alert(e.message); }
});

// --- HLSL Verify ---

async function handleHlslVerifyRun(event) {
  event.preventDefault();
  const glsl = document.getElementById("hv-simplified-glsl").value;
  const capturePath = document.getElementById("hv-capture-path").value.trim();
  const eid = document.getElementById("hv-eid").value.trim();
  const stage = document.getElementById("hv-stage").value;
  const summaryEl = document.getElementById("hv-result-summary");
  const btn = document.getElementById("hv-run-btn");

  if (!glsl.trim()) {
    summaryEl.textContent = "请输入简化后的 GLSL。";
    return;
  }

  btn.disabled = true;
  btn.textContent = "验证中...";
  summaryEl.textContent = "正在执行 HLSL 转换验证...";
  summaryEl.className = "empty-state";

  const body = new FormData();
  body.append("simplified_glsl", glsl);
  if (capturePath) body.append("capture_path", capturePath);
  if (eid) body.append("eid", eid);
  body.append("stage", stage);

  try {
    const data = await fetchJson("/api/hlsl-verify/run", { method: "POST", body });
    renderHlslVerifyResult(data);
  } catch (err) {
    summaryEl.textContent = `HLSL 验证失败: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "执行 HLSL 转换验证";
  }
}

function renderHlslVerifyResult(data) {
  const summaryEl = document.getElementById("hv-result-summary");
  const detailEl = document.getElementById("hv-result-detail");

  if (data.success) {
    summaryEl.textContent = `转换成功 (method: ${data.method_used}, ${data.total_iterations} 次迭代)`;
    summaryEl.style.color = "#4caf50";
  } else {
    summaryEl.textContent = `转换失败: ${data.error || "所有方法均未通过"}`;
    summaryEl.style.color = "#f44336";
  }
  summaryEl.style.fontWeight = "700";

  detailEl.classList.remove("hidden");
  document.getElementById("hv-standalone-hlsl").value = data.final_hlsl || "";
  document.getElementById("hv-ue-hlsl").value = data.final_ue_custom_hlsl || "";

  const iterations = data.iterations || [];
  const logLines = iterations.map((i) =>
    `[${i.iteration}] ${i.method}: compile=${i.compile_ok}, spirv=${i.spirv_bridge_ok}, action=${i.action}`
  );
  document.getElementById("hv-iteration-log").textContent = logLines.join("\n") || "无日志";
}

document.getElementById("hlsl-verify-form").addEventListener("submit", handleHlslVerifyRun);
document.getElementById("pick-hv-capture-path-btn").addEventListener("click", () =>
  pickDesktopFile("pick_rdc_file", "hv-capture-path")
);
document.getElementById("hv-copy-hlsl-btn").addEventListener("click", async () => {
  try { await copyTextFromElement("hv-standalone-hlsl"); } catch (e) { alert(e.message); }
});
document.getElementById("hv-copy-ue-btn").addEventListener("click", async () => {
  try { await copyTextFromElement("hv-ue-hlsl"); } catch (e) { alert(e.message); }
});

// --- One-Click Convert ---

async function handleOneclickConvertRun(event) {
  event.preventDefault();
  const glsl = document.getElementById("oc-glsl-source").value;
  const fragmentPath = document.getElementById("oc-fragment-path").value.trim();
  const vertexPath = document.getElementById("oc-vertex-path").value.trim();
  const paramsPath = document.getElementById("oc-params-path").value.trim();
  const capturePath = document.getElementById("oc-capture-path").value.trim();
  const eid = document.getElementById("oc-eid").value.trim();
  const stage = document.getElementById("oc-stage").value;
  const summaryEl = document.getElementById("oc-result-summary");
  const btn = document.getElementById("oc-run-btn");

  if (!glsl.trim() && !fragmentPath && !capturePath) {
    summaryEl.textContent = "请输入 GLSL 源码、指定文件路径或 RDC + EID。";
    return;
  }

  btn.disabled = true;
  btn.textContent = "转换中...";
  summaryEl.textContent = "正在执行一键转换...";
  summaryEl.className = "empty-state";

  const body = new FormData();
  body.append("glsl_source", glsl);
  if (fragmentPath) body.append("fragment_path", fragmentPath);
  if (vertexPath) body.append("vertex_path", vertexPath);
  if (paramsPath) body.append("shader_params_path", paramsPath);
  if (capturePath) body.append("capture_path", capturePath);
  if (eid) body.append("eid", eid);
  body.append("stage", stage);

  try {
    const data = await fetchJson("/api/oneclick-convert/run", { method: "POST", body });
    renderOneclickResult(data);
  } catch (err) {
    summaryEl.textContent = `转换失败: ${err.message}`;
    summaryEl.style.color = "#f44336";
  } finally {
    btn.disabled = false;
    btn.textContent = "一键转换";
  }
}

function renderOneclickResult(data) {
  const summaryEl = document.getElementById("oc-result-summary");
  const detailEl = document.getElementById("oc-result-detail");

  if (data.success) {
    const reduction = data.original_lines > 0
      ? Math.round((1 - data.simplified_lines / data.original_lines) * 100)
      : 0;
    summaryEl.textContent = `转换成功 — 原始 ${data.original_lines} 行 → 简化 ${data.simplified_lines} 行 (减少 ${reduction}%)`;
    summaryEl.style.color = "#4caf50";
  } else {
    summaryEl.textContent = `转换失败: ${data.error || "未知错误"}`;
    summaryEl.style.color = "#f44336";
  }
  summaryEl.style.fontWeight = "700";

  detailEl.classList.remove("hidden");

  const statsEl = document.getElementById("oc-simplify-stats");
  const transforms = data.simplify_transforms || [];
  statsEl.innerHTML = transforms
    .map((t) => `<span class="stat-badge">${t.level}: ${t.lines_before}→${t.lines_after}</span>`)
    .join(" ");

  document.getElementById("oc-standalone-hlsl").value = data.standalone_hlsl || "";
  document.getElementById("oc-ue-hlsl").value = data.ue_custom_hlsl || "";

  const rules = data.rules_applied || [];
  const logLines = rules.map((r) => `[${r.rule_name}] ${r.description}: ${r.lines_before}→${r.lines_after} lines`);
  document.getElementById("oc-rules-log").textContent = logLines.join("\n") || "无规则应用记录";

  const warnings = (data.warnings || []).concat(data.unsupported || []);
  const warnSection = document.getElementById("oc-warnings-section");
  if (warnings.length) {
    warnSection.classList.remove("hidden");
    document.getElementById("oc-warnings").textContent = warnings.join("\n");
  } else {
    warnSection.classList.add("hidden");
  }
}

document.getElementById("oneclick-convert-form").addEventListener("submit", handleOneclickConvertRun);
document.getElementById("pick-oc-fragment-btn").addEventListener("click", () =>
  pickDesktopFile("pick_glsl_file", "oc-fragment-path")
);
document.getElementById("pick-oc-vertex-btn").addEventListener("click", () =>
  pickDesktopFile("pick_glsl_file", "oc-vertex-path")
);
document.getElementById("pick-oc-params-btn").addEventListener("click", () =>
  pickDesktopFile("pick_json_file", "oc-params-path")
);
document.getElementById("pick-oc-capture-btn").addEventListener("click", () =>
  pickDesktopFile("pick_rdc_file", "oc-capture-path")
);
document.getElementById("oc-copy-standalone-btn").addEventListener("click", async () => {
  try { await copyTextFromElement("oc-standalone-hlsl"); } catch (e) { alert(e.message); }
});
document.getElementById("oc-copy-ue-btn").addEventListener("click", async () => {
  try { await copyTextFromElement("oc-ue-hlsl"); } catch (e) { alert(e.message); }
});
} // end if(false) dead block

loadHealth();
loadSetupStatus();
loadCmpJobs();
loadPerfJobs();
loadAssetExportJobs();
