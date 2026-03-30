let currentSessionId = null;
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
  document.getElementById("setup-llm-provider").value = settings.llm_provider || "local";
  document.getElementById("setup-openai-base-url").value = settings.openai_base_url || "";
  document.getElementById("setup-openai-api-key").value = settings.openai_api_key || "";
  document.getElementById("setup-openai-model").value = settings.openai_model || "";
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

function renderSessionSummary(detail) {
  const metadata = detail.metadata || {};
  const summary = metadata.summary || {};
  const inputs = metadata.inputs || {};
  document.getElementById("session-summary").innerHTML = `
    <div><strong>Session:</strong> ${metadata.session_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>Pass:</strong> ${inputs.pass_name || "-"}</div>
    <div><strong>问题:</strong> ${inputs.issue || "-"}</div>
    <div><strong>Top 原因:</strong> ${summary.top_cause || "暂无"}</div>
    <div><strong>置信度:</strong> ${summary.confidence || "暂无"}</div>
  `;
  document.getElementById("analysis-output").textContent = detail.analysis_markdown || "暂无报告";
  document.getElementById("eid-output").textContent = detail.eid_deep_dive_markdown || "暂无内容";
  document.getElementById("ue-scan-output").textContent = detail.ue_scan_markdown || "暂无内容";
  document.getElementById("deep-eid-before").value = inputs.eid_before || "";
  document.getElementById("deep-eid-after").value = inputs.eid_after || "";
  if (!document.getElementById("ue-project-root").value) {
    document.getElementById("ue-project-root").value = "G:\\UGit\\LetsgoDevelop2";
  }
}

function renderSessions(sessions) {
  const container = document.getElementById("sessions-list");
  container.innerHTML = "";

  if (!sessions.length) {
    container.innerHTML = '<div class="empty-state">暂无 session</div>';
    return;
  }

  sessions.forEach((item) => {
    const inputs = item.inputs || {};
    const summary = item.summary || {};
    const div = document.createElement("div");
    div.className = "session-item" + (item.session_id === currentSessionId ? " active" : "");
    div.innerHTML = `
      <div class="title">${summary.title || item.session_id}</div>
      <div class="meta">${item.updated_at || ""}</div>
      <div class="meta">Pass: ${inputs.pass_name || "-"}</div>
      <div class="meta">状态: ${item.status || "-"}</div>
    `;
    div.addEventListener("click", async () => {
      await loadSession(item.session_id);
    });
    container.appendChild(div);
  });
}

function renderChat(history) {
  const container = document.getElementById("chat-history");
  container.innerHTML = "";

  if (!history || !history.length) {
    container.innerHTML = '<div class="empty-state">当前 session 还没有追问记录。</div>';
    return;
  }

  history.forEach((item) => {
    const block = document.createElement("div");
    block.className = `chat-message ${item.role}`;
    const sources = item.sources && item.sources.length
      ? `<div class="sources">来源: ${item.sources.join(", ")}</div>`
      : "";
    block.innerHTML = `
      <div class="chat-role">${item.role} · ${item.created_at || ""}</div>
      <div>${item.content || ""}</div>
      ${sources}
    `;
    container.appendChild(block);
  });
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

async function loadSessions() {
  const sessions = await fetchJson("/api/sessions");
  renderSessions(sessions);
}

function renderCmpSummary(detail) {
  const metadata = detail.metadata || {};
  const inputs = metadata.inputs || {};
  const reportUrl = detail.report_url || "";
  document.getElementById("cmp-summary").innerHTML = `
    <div><strong>Job:</strong> ${metadata.job_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>Base:</strong> ${inputs.base_file || "-"}</div>
    <div><strong>New:</strong> ${inputs.new_file || "-"}</div>
    <div><strong>Strict:</strong> ${String(inputs.strict_mode == null ? "-" : inputs.strict_mode)}</div>
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
  const analysis = detail.analysis || {};
  const overview = analysis.overview || {};
  const captureInfo = analysis.capture_info || {};
  currentPerfAnalysis = analysis;
  document.getElementById("perf-summary").innerHTML = `
    <div><strong>Job:</strong> ${metadata.job_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>Capture:</strong> ${(metadata.inputs || {}).capture_file || "-"}</div>
    <div><strong>驱动:</strong> ${captureInfo.driver_name || "-"}</div>
    <div><strong>总 GPU:</strong> ${Number(overview.total_gpu_duration_ms || 0).toFixed(3)} ms</div>
    <div><strong>Draw 数:</strong> ${overview.draw_count || 0}</div>
    <div><strong>总三角面:</strong> ${overview.total_triangles || 0}</div>
    <div><strong>总顶点:</strong> ${overview.total_vertices_read || 0}</div>
    <div><strong>总指令:</strong> ${overview.total_instruction_count || 0}</div>
    <div><strong>稳定总分:</strong> ${Number(overview.total_stable_sort_score || 0).toFixed(3)}</div>
    <div><strong>总贴图:</strong> ${Number(overview.total_texture_mb || 0).toFixed(3)} MB</div>
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
  if (row.draw_preview_url) {
    const title = `EID ${row.eid || "-"} | ${row.pass_name || "-"}`;
    const meta = `Score ${Number(row.stable_sort_score || 0).toFixed(3)} | Cover ${Number(row.screen_coverage_percent || 0).toFixed(3)}% | Tri ${row.triangles || 0}`;
    return `<button type="button" class="perf-preview-trigger" data-preview-src="${escapeHtml(row.draw_preview_url)}" data-preview-title="${escapeHtml(title)}" data-preview-meta="${escapeHtml(meta)}"><img src="${row.draw_preview_url}" alt="draw-${row.eid}" class="perf-preview-thumb"></button>`;
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
    `;
    return;
  }
  summary.innerHTML = `
    <div><strong>单文件模式:</strong> ${data.inspect_csv_path || data.csv_name || "-"}</div>
    <div><strong>表头列数:</strong> ${(data.headers || []).length}</div>
  `;
}

function renderAssetExportSummary(detail) {
  const metadata = detail.metadata || {};
  const input = metadata.input || {};
  const progress = metadata.progress || {};
  const result = metadata.result || {};
  document.getElementById("asset-export-summary").innerHTML = `
    <div><strong>Job:</strong> ${metadata.job_id || "-"}</div>
    <div><strong>状态:</strong> ${metadata.status || "-"}</div>
    <div><strong>范围:</strong> ${input.export_scope || "-"}</div>
    <div><strong>单 Pass:</strong> ${input.pass_name || "-"}</div>
    <div><strong>起止:</strong> ${input.pass_start || "-"} -> ${input.pass_end || "-"}</div>
    <div><strong>格式:</strong> FBX=${String(input.export_fbx == null ? false : input.export_fbx)} / OBJ=${String(input.export_obj == null ? false : input.export_obj)}</div>
    <div><strong>贴图:</strong> ${input.texture_format || "-"}</div>
    <div><strong>导出目录:</strong> ${result.output_root || ((metadata.artifacts || {}).output_root) || "未设置"}</div>
    <div><strong>阶段:</strong> ${progress.stage || "-"}</div>
    <div><strong>说明:</strong> ${progress.message || "-"}</div>
    <div><strong>CSV:</strong> ${(result.csv_files || []).length}</div>
    <div><strong>模型:</strong> ${(result.model_files || []).length}</div>
    <div><strong>贴图:</strong> ${(result.texture_files || []).length}</div>
    <div><strong>失败:</strong> ${(result.failed_items || []).length}</div>
  `;
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

async function loadSession(sessionId) {
  const detail = await fetchJson(`/api/sessions/${sessionId}`);
  currentSessionId = sessionId;
  renderSessionSummary(detail);
  renderChat(detail.chat_history || []);
  await loadSessions();
}

async function handleAnalyze(event) {
  event.preventDefault();

  const beforePath = document.getElementById("before-path").value.trim();
  const afterPath = document.getElementById("after-path").value.trim();
  const button = document.getElementById("analyze-btn");
  button.disabled = true;
  button.textContent = "分析中...";

  try {
    let response;
    if (beforePath && afterPath) {
      const formData = new FormData();
      formData.append("before_path", beforePath);
      formData.append("after_path", afterPath);
      formData.append("pass_name", document.getElementById("pass-name").value.trim());
      formData.append("issue", document.getElementById("issue").value.trim());
      formData.append("eid_before", document.getElementById("eid-before").value.trim());
      formData.append("eid_after", document.getElementById("eid-after").value.trim());
      response = await fetch("/api/analyze/by-path", {
        method: "POST",
        body: formData,
      });
    } else {
      const form = document.getElementById("analyze-form");
      const formData = new FormData(form);
      response = await fetch("/api/analyze", {
        method: "POST",
        body: formData,
      });
    }
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "分析失败");
    }
    currentSessionId = data.metadata.session_id;
    renderSessionSummary(data);
    renderChat(data.chat_history || []);
    await loadSessions();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "开始分析";
  }
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
  const button = document.getElementById("perf-run-btn");
  button.disabled = true;
  button.textContent = "分析中...";

  try {
    let response;
    if (capturePath) {
      const formData = new FormData();
      formData.append("capture_path", capturePath);
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

async function handleChat(event) {
  event.preventDefault();

  if (!currentSessionId) {
    alert("请先完成一次分析并选中 session。");
    return;
  }

  const question = document.getElementById("chat-question").value.trim();
  if (!question) {
    alert("请输入追问内容。");
    return;
  }

  const formData = new FormData();
  formData.append("question", question);

  const button = document.getElementById("chat-btn");
  button.disabled = true;
  button.textContent = "发送中...";

  try {
    const response = await fetch(`/api/sessions/${currentSessionId}/chat`, {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "追问失败");
    }
    renderChat(data.chat_history || []);
    document.getElementById("chat-question").value = "";
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "发送追问";
  }
}

async function handleAssetPassScan(event) {
  event.preventDefault();
  const capturePath = document.getElementById("asset-capture-source-path").value.trim();
  const button = document.getElementById("asset-pass-scan-btn");
  button.disabled = true;
  button.textContent = "读取中...";
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
  button.textContent = "准备映射中...";
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
    button.textContent = "保存导出任务";
  }
}

async function submitAssetExportDraft(draft, mapping) {
  let response;
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
    const outputRoot = (((data || {}).metadata || {}).result || {}).output_root
      || ((((data || {}).metadata || {}).artifacts || {}).output_root)
      || "";
    if (outputRoot) {
      window.setTimeout(() => {
        revealDesktopPath(outputRoot);
      }, 50);
    }
    switchTab("asset-export");
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "按当前映射转换 CSV";
  }
}

async function handleEidDeepDive(event) {
  event.preventDefault();

  if (!currentSessionId) {
    alert("请先完成一次分析并选中 session。");
    return;
  }

  const eidBefore = document.getElementById("deep-eid-before").value.trim();
  const eidAfter = document.getElementById("deep-eid-after").value.trim();
  if (!eidBefore || !eidAfter) {
    alert("请填写 before/after EID。");
    return;
  }

  const formData = new FormData();
  formData.append("eid_before", eidBefore);
  formData.append("eid_after", eidAfter);

  const button = document.getElementById("eid-btn");
  button.disabled = true;
  button.textContent = "深挖中...";

  try {
    const response = await fetch(`/api/sessions/${currentSessionId}/eid-deep-dive`, {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "EID 深挖失败");
    }
    renderSessionSummary(data);
    renderChat(data.chat_history || []);
    await loadSessions();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "执行 EID 深挖";
  }
}

async function handleUEScan(event) {
  event.preventDefault();

  if (!currentSessionId) {
    alert("请先完成一次分析并选中 session。");
    return;
  }

  const projectRoot = document.getElementById("ue-project-root").value.trim();
  if (!projectRoot) {
    alert("请填写 UE 项目根目录。");
    return;
  }

  const formData = new FormData();
  formData.append("project_root", projectRoot);

  const button = document.getElementById("ue-scan-btn");
  button.disabled = true;
  button.textContent = "扫描中...";

  try {
    const response = await fetch(`/api/sessions/${currentSessionId}/ue-source-scan`, {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "UE 源码扫描失败");
    }
    renderSessionSummary(data);
    renderChat(data.chat_history || []);
    await loadSessions();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "执行 UE 源码扫描";
  }
}

async function handleSetupSave(event) {
  event.preventDefault();

  const formData = new FormData();
  formData.append("renderdoc_python_path", document.getElementById("setup-renderdoc-python-path").value.trim());
  formData.append("llm_provider", document.getElementById("setup-llm-provider").value.trim());
  formData.append("openai_base_url", document.getElementById("setup-openai-base-url").value.trim());
  formData.append("openai_api_key", document.getElementById("setup-openai-api-key").value.trim());
  formData.append("openai_model", document.getElementById("setup-openai-model").value.trim());
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

document.getElementById("analyze-form").addEventListener("submit", handleAnalyze);
document.getElementById("cmp-form").addEventListener("submit", handleCmpRun);
document.getElementById("perf-form").addEventListener("submit", handlePerfRun);
document.getElementById("asset-pass-scan-form").addEventListener("submit", handleAssetPassScan);
document.getElementById("asset-export-form").addEventListener("submit", handleAssetExportCreate);
document.getElementById("asset-csv-inspect-form").addEventListener("submit", handleAssetCsvInspect);
document.getElementById("asset-csv-convert-btn").addEventListener("click", handleAssetCsvConvert);
document.getElementById("chat-form").addEventListener("submit", handleChat);
document.getElementById("eid-form").addEventListener("submit", handleEidDeepDive);
document.getElementById("ue-scan-form").addEventListener("submit", handleUEScan);
document.getElementById("setup-form").addEventListener("submit", handleSetupSave);
document.getElementById("refresh-health-btn").addEventListener("click", loadHealth);
document.getElementById("open-setup-btn").addEventListener("click", showSetupModal);
document.getElementById("setup-close-btn").addEventListener("click", hideSetupModal);
document.getElementById("asset-export-mapping-confirm-btn").addEventListener("click", handleAssetExportMappingConfirm);
document.getElementById("asset-export-mapping-cancel-btn").addEventListener("click", hideAssetExportMappingModal);
document.getElementById("pick-before-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "before-path"));
document.getElementById("pick-after-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "after-path"));
document.getElementById("pick-cmp-base-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "cmp-base-path"));
document.getElementById("pick-cmp-new-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "cmp-new-path"));
document.getElementById("pick-perf-capture-path-btn").addEventListener("click", () => pickDesktopFile("pick_rdc_file", "perf-capture-path"));
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
loadHealth();
loadSetupStatus();
loadSessions();
loadCmpJobs();
loadPerfJobs();
loadAssetExportJobs();
