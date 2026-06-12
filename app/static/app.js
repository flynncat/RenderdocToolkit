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

// ---- Server-side filesystem picker -------------------------------------
// The browser sandbox can't hand us absolute local paths, so for the
// "server local path" fields (RenderDoc install dir, by-path .rdc/.csv) we
// browse the *server's* filesystem through /api/fs/list and let the user
// click their way to a folder/file.
const fsPicker = {
  mode: "dir",
  exts: "",
  append: false,
  targetId: null,
  current: "",
};

async function fsList(path) {
  const params = new URLSearchParams();
  params.set("path", path || "");
  params.set("mode", fsPicker.mode);
  if (fsPicker.exts) params.set("exts", fsPicker.exts);
  const response = await fetch(`/api/fs/list?${params.toString()}`);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "读取目录失败");
  }
  return data;
}

function renderFsList(data) {
  fsPicker.current = data.current || "";
  const pathInput = document.getElementById("fs-picker-path");
  if (pathInput) pathInput.value = fsPicker.current;
  const list = document.getElementById("fs-picker-list");
  const chooseBtn = document.getElementById("fs-picker-choose");
  if (chooseBtn) chooseBtn.disabled = !fsPicker.current;
  list.innerHTML = "";
  const entries = data.entries || [];
  if (!entries.length) {
    list.innerHTML = '<div class="empty-state">（此处没有可显示的项目）</div>';
    return;
  }
  entries.forEach((entry) => {
    const item = document.createElement("div");
    item.className = "fs-picker-item" + (entry.is_dir ? " is-dir" : " is-file");
    
    if (entry.is_dir) {
      item.innerHTML = `
        <span class="fs-picker-icon">📁</span>
        <span class="fs-picker-name"></span>
        <button type="button" class="fs-picker-enter-btn" title="进入此文件夹">进入 ➜</button>
      `;
      item.querySelector(".fs-picker-name").textContent = entry.name;
      
      // Click handler
      item.addEventListener("click", (e) => {
        if (e.target.classList.contains("fs-picker-enter-btn")) {
          e.stopPropagation();
          loadFsPath(entry.path);
          return;
        }
        if (fsPicker.mode === "dir") {
          // In dir mode, clicking the folder row directly selects it
          applyFsPick(entry.path);
        } else {
          // In file mode, clicking the folder row enters it to look for files
          loadFsPath(entry.path);
        }
      });
      
      // Double click handler
      item.addEventListener("dblclick", (e) => {
        e.preventDefault();
        loadFsPath(entry.path);
      });
    } else {
      item.innerHTML = `<span class="fs-picker-icon">📄</span><span class="fs-picker-name"></span>`;
      item.querySelector(".fs-picker-name").textContent = entry.name;
      item.addEventListener("click", () => {
        if (fsPicker.mode === "file") {
          applyFsPick(entry.path);
        }
      });
    }
    list.appendChild(item);
  });
}

async function loadFsPath(path) {
  const list = document.getElementById("fs-picker-list");
  if (list) list.innerHTML = '<div class="empty-state">加载中...</div>';
  try {
    const data = await fsList(path);
    renderFsList(data);
  } catch (error) {
    if (list) list.innerHTML = `<div class="empty-state">错误：${error.message}</div>`;
  }
}

function applyFsPick(value) {
  const target = fsPicker.targetId && document.getElementById(fsPicker.targetId);
  if (target && value) {
    if (fsPicker.append) {
      const existing = target.value.trim();
      const lines = existing ? existing.split(/\r?\n/).map((s) => s.trim()).filter(Boolean) : [];
      if (!lines.includes(value)) lines.push(value);
      target.value = lines.join("\n");
    } else {
      target.value = value;
    }
    target.dispatchEvent(new Event("change", { bubbles: true }));
    
    // Update file-like label if exists
    const label = document.getElementById(`${fsPicker.targetId}-label`);
    if (label) {
      label.textContent = value;
      label.title = value;
    }
  }
  closeFsPicker();
}

async function openFsPicker(button) {
  fsPicker.mode = button.dataset.fsPick === "file" ? "file" : "dir";
  fsPicker.exts = button.dataset.fsExts || "";
  fsPicker.append = button.dataset.fsAppend === "1";
  fsPicker.targetId = button.dataset.fsTarget || null;

  // Try native OS file dialog first (works when running locally)
  try {
    const params = new URLSearchParams();
    params.set("mode", fsPicker.mode);
    if (fsPicker.exts) params.set("exts", fsPicker.exts);
    params.set("title", button.dataset.fsTitle || "");
    const response = await fetch(`/api/fs/local-pick?${params.toString()}`);
    if (response.ok) {
      const data = await response.json();
      if (data.path) {
        applyFsPick(data.path);
        return;
      }
      // If data.path is empty, user cancelled the native dialog. Just return.
      return;
    }
  } catch (error) {
    console.warn("Native file picker failed, falling back to web-based explorer:", error);
  }

  // Fallback to web-based folder explorer modal
  const titleEl = document.getElementById("fs-picker-title");
  if (titleEl) titleEl.textContent = button.dataset.fsTitle || (fsPicker.mode === "file" ? "选择文件" : "选择目录");
  const chooseBtn = document.getElementById("fs-picker-choose");
  if (chooseBtn) {
    // In file mode the user picks by clicking a file row; the "choose current
    // directory" button only makes sense when selecting a folder.
    chooseBtn.classList.toggle("hidden", fsPicker.mode === "file");
    chooseBtn.textContent = fsPicker.append ? "追加此目录" : "选择此目录";
  }
  const modal = document.getElementById("fs-picker-modal");
  if (modal) modal.classList.remove("hidden");
  // Seed from the target's current value (use its directory) when present.
  const target = fsPicker.targetId && document.getElementById(fsPicker.targetId);
  let seed = "";
  if (target && target.value) {
    const firstLine = target.value.split(/\r?\n/)[0].trim();
    seed = firstLine;
  }
  loadFsPath(seed);
}

function closeFsPicker() {
  const modal = document.getElementById("fs-picker-modal");
  if (modal) modal.classList.add("hidden");
}

async function copyServerPathToClipboard(path) {
  // Web deployment: output files live on the server, so we can't open the
  // host's file explorer.  Offer the server-side path on the clipboard
  // instead so operators can locate it via their own session.
  const value = (path || "").trim();
  if (!value) {
    return;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      alert(`服务器端输出路径已复制到剪贴板：\n${value}`);
      return;
    }
  } catch (_e) {
    // fall through to prompt fallback
  }
  window.prompt("服务器端输出路径（请手动复制）：", value);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "请求失败");
  }
  return data;
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
  const replayBackend = (inputs.replay_backend || "").trim();
  const qrUsed = !!features.qr_replay_used;
  const qrUpgraded = Number(features.qr_replay_draws_upgraded || 0);
  let modeBadge = "";
  if (isXmlFallback) {
    if (qrUsed) {
      modeBadge = `<div class="perf-mode-badge perf-mode-replay-upgraded">XML 分析 + qrenderdoc 真实回放 (${qrUpgraded} draw 已升级为真实 RT)</div>`;
    } else {
      modeBadge = `<div class="perf-mode-badge perf-mode-fallback">XML 回退分析模式 (自定义 RenderDoc，未启用 qrenderdoc 后端)</div>`;
    }
  }

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
    ${features.counters_available === false ? "" : `<div><strong>总顶点:</strong> ${overview.total_vertices_read || 0}</div>`}
    <div><strong>总指令${features.instruction_count_estimated ? "(估算)" : ""}:</strong> ${overview.total_instruction_count || 0}</div>
    ${features.counters_available === false ? "" : `<div><strong>稳定总分:</strong> ${Number(overview.total_stable_sort_score || 0).toFixed(3)}</div>`}
    <div><strong>总贴图:</strong> ${Number(overview.total_texture_mb || 0).toFixed(3)} MB</div>
    ${thumbBlock}
  `;
  document.getElementById("perf-run-log").textContent = detail.run_log || "暂无日志";
  renderPerfSortFields(analysis.sort_fields || []);
  renderPerfWarnings(analysis.warnings || []);
  renderPerfTable();
  renderPerfChart(analysis.pass_chart || []);
  renderPerfHotspotHints(analysis.hotspot_hints || []);
  updatePerfExportButtonsState();
  renderPerfReportPanel(metadata.job_id || currentPerfJobId);
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
    // Distinguish four sources of the per-draw preview image so users can
    // see at a glance which technology produced it.  The visual style
    // (border colour) and tooltip text are different for each.
    const isRtReplay = previewKind === "rt_replay";
    const isTexReplay = previewKind === "tex_replay";
    const isTexFallback = previewKind === "texture";
    const isWireframe = previewKind === "wireframe_overlay";
    const overlayUrl = row.draw_preview_overlay_url || "";
    const overlayKind = (row.draw_preview_overlay_kind || "").toLowerCase();
    const hasOverlay = Boolean(overlayUrl);
    const baseTitle = `EID ${row.eid || "-"} | ${row.pass_name || "-"}`;
    let titleSuffix = "";
    let hoverNote = "";
    let cls = "perf-preview-thumb";
    if (isRtReplay) {
      titleSuffix = hasOverlay
        ? "（RT + 线框 · qrenderdoc 回放）"
        : "（真实 RT · qrenderdoc 回放）";
      hoverNote = hasOverlay
        ? "通过 qrenderdoc.exe --python 后端真实 GPU 回放，并叠加该 Draw 的几何线框 / 轮廓。"
        : "通过 qrenderdoc.exe --python 后端真实 GPU 回放得到的渲染目标。";
      cls = "perf-preview-thumb perf-preview-thumb--rt-replay";
    } else if (isTexReplay) {
      titleSuffix = "（真实绑定纹理 · qrenderdoc 回放）";
      hoverNote = "该 Draw 不写颜色 RT（如 shadow map）；展示其首张绑定纹理的真实回放像素。";
      cls = "perf-preview-thumb perf-preview-thumb--tex-replay";
    } else if (isWireframe) {
      titleSuffix = "（线框 · Python 直连回放）";
      cls = "perf-preview-thumb perf-preview-thumb--wireframe";
    } else if (isTexFallback) {
      titleSuffix = "（贴图预览 · XML 回退）";
      hoverNote = "无可用回放后端时，从 capture.zip 解码出的绑定贴图作为视觉提示。";
      cls = "perf-preview-thumb perf-preview-thumb--fallback";
    }
    const title = baseTitle + titleSuffix;
    const meta = `Score ${Number(row.stable_sort_score || 0).toFixed(3)} | Cover ${Number(row.screen_coverage_percent || 0).toFixed(3)}% | Tri ${row.triangles || 0}`;
    const altText = `draw-${row.eid}-${previewKind || "preview"}`;
    const dataOverlay = hasOverlay ? ` data-preview-overlay-src="${escapeHtml(overlayUrl)}" data-preview-overlay-kind="${escapeHtml(overlayKind || "wireframe")}"` : "";
    const wfBadge = hasOverlay
      ? `<span class="perf-preview-thumb-badge" title="${escapeHtml(overlayKind === "drawcall" ? "已叠加 Drawcall 轮廓" : "已叠加 Wireframe 线框")}">WF</span>`
      : "";
    if (hasOverlay) {
      // The overlay PNG (``wireframe_<eid>.png``) is now baked
      // server-side as "RT + wireframe" via PIL alpha_composite in
      // ``renderdoc_direct_replay.save_draw_rt_and_overlay_preview``.
      // So we display the *single* composite image in both the
      // thumbnail and the hover/click popup - no more CSS stacking
      // with mix-blend-mode (which would double-bright the wireframe
      // on the already-composite image).  This makes the SPA preview
      // bit-for-bit identical to what users see in the downloaded
      // HTML report and the ZIP bundle (they reference the same PNG).
      // We keep ``data-preview-rt-src`` available so a future "show
      // RT only" toggle can switch back if anyone needs it.
      return `<button type="button" class="perf-preview-trigger" data-preview-src="${escapeHtml(overlayUrl)}" data-preview-rt-src="${escapeHtml(row.draw_preview_url)}" data-preview-title="${escapeHtml(title)}" data-preview-meta="${escapeHtml(meta)}" title="${escapeHtml(hoverNote)}"><span class="perf-preview-stack"><img src="${overlayUrl}" alt="${altText}-composite" class="${cls}" loading="lazy">${wfBadge}</span></button>`;
    }
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

function showPerfPreviewPanel({ src = "", title = "", meta = "", pinned = false, anchorX = 0, anchorY = 0, overlaySrc = "", overlayKind = "" }) {
  if (!src) {
    return;
  }
  const panel = document.getElementById("perf-preview-panel");
  const image = document.getElementById("perf-preview-panel-image");
  const overlayImage = document.getElementById("perf-preview-panel-overlay");
  const titleNode = document.getElementById("perf-preview-panel-title");
  const metaNode = document.getElementById("perf-preview-panel-meta");
  perfPreviewPinned = pinned;
  if (perfPreviewHideTimer) {
    window.clearTimeout(perfPreviewHideTimer);
    perfPreviewHideTimer = null;
  }
  image.src = src;
  image.alt = title || "preview";
  if (overlayImage) {
    if (overlaySrc) {
      overlayImage.src = overlaySrc;
      overlayImage.classList.remove("hidden");
      overlayImage.dataset.kind = overlayKind || "wireframe";
    } else {
      overlayImage.src = "";
      overlayImage.classList.add("hidden");
      overlayImage.dataset.kind = "";
    }
  }
  titleNode.textContent = title || "预览";
  metaNode.textContent = meta || "";
  panel.classList.remove("hidden");
  panel.classList.toggle("pinned", perfPreviewPinned);
  panel.classList.toggle("has-overlay", Boolean(overlaySrc));
  positionPerfPreviewPanel(panel, anchorX, anchorY);
  image.onload = () => positionPerfPreviewPanel(panel, anchorX, anchorY);
}

function hidePerfPreviewPanel(force = false) {
  if (perfPreviewPinned && !force) {
    return;
  }
  const panel = document.getElementById("perf-preview-panel");
  const image = document.getElementById("perf-preview-panel-image");
  const overlayImage = document.getElementById("perf-preview-panel-overlay");
  panel.classList.add("hidden");
  panel.classList.remove("pinned");
  panel.classList.remove("has-overlay");
  image.src = "";
  if (overlayImage) {
    overlayImage.src = "";
    overlayImage.classList.add("hidden");
  }
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
  const features = (currentPerfAnalysis && currentPerfAnalysis.analysis_features) || {};
  // When the replay backend didn't provide pipeline-statistics counters
  // (typical for desktop replay of a mobile GLES capture), the columns
  // 稳定得分 / 覆盖率% / 顶点 / 图元 / PS调用 are all zero and misleading,
  // so we hide them and lead with the metrics that are actually real.
  const countersAvailable = features.counters_available !== false;
  const defaultSort = countersAvailable ? "stable_sort_score" : "gpu_duration_ms";
  const sortField = document.getElementById("perf-sort-field").value || defaultSort;
  const sortDirection = document.getElementById("perf-sort-direction").value || "desc";
  rows.sort((a, b) => {
    const av = Number((a && a[sortField]) || 0);
    const bv = Number((b && b[sortField]) || 0);
    return sortDirection === "asc" ? av - bv : bv - av;
  });

  const decidedByLabel = {
    marker: "marker",
    marker_raw: "marker(raw)",
    render_state: "状态推断",
    fallback: "未识别",
  };

  const body = rows.map((row) => {
    const eid = row.eid || "";
    const decidedBy = row.scene_pass_decided_by || "";
    const decidedLabel = decidedByLabel[decidedBy] || decidedBy;
    const breadcrumbs = Array.isArray(row.breadcrumbs) ? row.breadcrumbs.join(" → ") : "";
    const renderState = row.render_state || {};
    const stateTooltip = [
      breadcrumbs ? `Breadcrumbs: ${breadcrumbs}` : "",
      decidedLabel ? `来源: ${decidedLabel}` : "",
      renderState.blend_summary ? `Blend: ${renderState.blend_summary}` : "",
      `DepthW: ${renderState.depth_write ? "on" : "off"} / DepthT: ${renderState.depth_test ? "on" : "off"}`,
      renderState.cull_mode ? `Cull: ${renderState.cull_mode}` : "",
    ].filter(Boolean).join("\n");
    const sceneCellHtml = decidedLabel
      ? `${row.scene_pass || "-"}<br/><small style="color:#7d8696">[${escapeHtml(decidedLabel)}]</small>`
      : (row.scene_pass || "-");
    const stableCells = countersAvailable
      ? `<td>${Number(row.stable_sort_score || 0).toFixed(3)}</td><td>${Number(row.screen_coverage_percent || 0).toFixed(4)}</td>`
      : "";
    const geomCounterCells = countersAvailable
      ? `<td>${row.vertices_read || 0}</td><td>${row.input_primitives || 0}</td>`
      : "";
    const psInvCell = countersAvailable ? `<td>${row.ps_invocations || 0}</td>` : "";
    return `
      <tr id="perf-row-${escapeHtml(eid)}" data-eid="${escapeHtml(eid)}">
        <td>${eid || "-"}</td>
        <td title="${escapeHtml(stateTooltip)}">${sceneCellHtml}</td>
        <td title="${escapeHtml(row.pass_name || "")}">${escapeHtml(row.pass_name || "-")}</td>
        ${stableCells}
        <td>${Number(row.gpu_duration_ms || 0).toFixed(3)}</td>
        <td>${row.triangles || 0}</td>
        ${geomCounterCells}
        <td>${row.instruction_total || 0}</td>
        <td>${row.ps_instruction_count || 0}</td>
        <td>${row.vs_instruction_count || 0}</td>
        ${psInvCell}
        <td id="perf-draw-preview-${row.eid || ""}"><div class="perf-preview-strip">${renderPerfDrawPreviewMarkup(row)}</div></td>
        <td>${row.texture_count || 0}</td>
        <td>${Number(row.texture_total_mb || 0).toFixed(3)}</td>
        <td>${Number(row.texture_bandwidth_risk || 0).toFixed(3)}</td>
        <td title="${escapeHtml(row.texture_summary_text || "")}"><div class="perf-preview-strip">${renderPerfTextureSummaryMarkup(row)}</div></td>
      </tr>
    `;
  }).join("");

  const stableHeaders = countersAvailable ? "<th>稳定得分</th><th>覆盖率%</th>" : "";
  const geomCounterHeaders = countersAvailable ? "<th>顶点</th><th>图元</th>" : "";
  const psInvHeader = countersAvailable ? "<th>PS调用</th>" : "";
  const counterNotice = countersAvailable
    ? ""
    : '<div class="empty-state" style="text-align:left;margin-bottom:8px;color:#a7b0bf">本次为桌面回放，GPU 管线计数器（PS调用/覆盖率/顶点/图元/稳定得分）不可用，已隐藏这些无效列并默认按 GPU ms 排序。</div>';

  container.innerHTML = `
    ${counterNotice}
    <table class="perf-table">
      <thead>
        <tr>
          <th>EID</th>
          <th>渲染分类</th>
          <th>Pass marker</th>
          ${stableHeaders}
          <th>GPU ms</th>
          <th>三角面</th>
          ${geomCounterHeaders}
          <th>总指令</th>
          <th>PS指令</th>
          <th>VS指令</th>
          ${psInvHeader}
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

// ---- Perf export & report panel ----

const PERF_TSV_COLUMNS = [
  { key: "eid", label: "eid" },
  { key: "scene_pass", label: "scene_pass" },
  { key: "pass_name", label: "pass_name" },
  { key: "breadcrumbs_path", label: "breadcrumbs_path" },
  { key: "draw_type", label: "draw_type" },
  { key: "instances", label: "instances" },
  { key: "triangles", label: "triangles" },
  { key: "vertices_read", label: "vertices_read" },
  { key: "input_primitives", label: "input_primitives" },
  { key: "gpu_duration_ms", label: "gpu_duration_ms" },
  { key: "vs_invocations", label: "vs_invocations" },
  { key: "ps_invocations", label: "ps_invocations" },
  { key: "samples_passed", label: "samples_passed" },
  { key: "vs_instruction_count", label: "vs_instruction_count" },
  { key: "ps_instruction_count", label: "ps_instruction_count" },
  { key: "instruction_total", label: "instruction_total" },
  { key: "target_width", label: "target_width" },
  { key: "target_height", label: "target_height" },
  { key: "target_samples", label: "target_samples" },
  { key: "screen_coverage_percent", label: "screen_coverage_percent" },
  { key: "coverage_pixels_estimate", label: "coverage_pixels_estimate" },
  { key: "instruction_coverage_score", label: "instruction_coverage_score" },
  { key: "stable_sort_score", label: "stable_sort_score" },
  { key: "stable_sort_basis", label: "stable_sort_basis" },
  { key: "texture_count", label: "texture_count" },
  { key: "texture_total_mb", label: "texture_total_mb" },
  { key: "texture_bandwidth_risk", label: "texture_bandwidth_risk" },
  { key: "texture_summary_text", label: "texture_summary_text" },
  { key: "shader_id_vs", label: "shader_id_vs" },
  { key: "shader_id_ps", label: "shader_id_ps" },
];

function perfTsvCellValue(row, key) {
  if (key === "breadcrumbs_path") {
    const crumbs = row.breadcrumbs || [];
    if (Array.isArray(crumbs)) {
      return crumbs.filter(Boolean).join("/");
    }
    return String(crumbs || "");
  }
  if (key === "shader_id_vs") {
    return (row.shader_ids && (row.shader_ids.vs || row.shader_ids.program)) || "";
  }
  if (key === "shader_id_ps") {
    return (row.shader_ids && row.shader_ids.ps) || "";
  }
  const value = row[key];
  if (value == null) return "";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value.replace(/\t/g, " ").replace(/\r?\n/g, " ");
  if (Array.isArray(value) || typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function buildPerfTsv() {
  const rows = (currentPerfAnalysis && currentPerfAnalysis.rows) || [];
  if (!rows.length) return "";
  const sortField = document.getElementById("perf-sort-field").value || "stable_sort_score";
  const sortDirection = document.getElementById("perf-sort-direction").value || "desc";
  const sorted = [...rows].sort((a, b) => {
    const av = Number((a && a[sortField]) || 0);
    const bv = Number((b && b[sortField]) || 0);
    return sortDirection === "asc" ? av - bv : bv - av;
  });
  const header = PERF_TSV_COLUMNS.map((c) => c.label).join("\t");
  const body = sorted.map((row) =>
    PERF_TSV_COLUMNS.map((c) => perfTsvCellValue(row, c.key)).join("\t")
  ).join("\n");
  return header + "\n" + body + "\n";
}

async function handleCopyPerfTsv() {
  const text = buildPerfTsv();
  if (!text) {
    alert("当前没有可复制的性能数据。");
    return;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    const btn = document.getElementById("perf-copy-tsv-btn");
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = "已复制 ✓";
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  } catch (error) {
    alert("复制失败：" + (error && error.message ? error.message : error));
  }
}

function flashPerfReportStatus(message, level = "info") {
  const status = document.getElementById("perf-report-status");
  if (!status) return;
  const prev = status.textContent;
  status.textContent = message;
  status.dataset.level = level;
  window.setTimeout(() => {
    if (status.textContent === message) {
      status.textContent = prev;
      delete status.dataset.level;
    }
  }, 2400);
}

async function downloadPerfArtifactViaBlobFallback(url, suggestedName, statusLabel) {
  flashPerfReportStatus(`${statusLabel} 准备中...`);
  try {
    const response = await fetch(url);
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(text || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const blobUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = blobUrl;
    anchor.download = suggestedName;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    window.setTimeout(() => URL.revokeObjectURL(blobUrl), 5000);
    flashPerfReportStatus(`${statusLabel} 已触发下载 ✓`);
  } catch (error) {
    const msg = error && error.message ? error.message : String(error);
    flashPerfReportStatus(`${statusLabel} 失败：${msg}`, "error");
    alert(`${statusLabel} 失败：${msg}`);
  }
}

async function downloadPerfArtifactViaBlob(url, suggestedName, statusLabel) {
  // Web deployment: standard browser blob download.
  await downloadPerfArtifactViaBlobFallback(url, suggestedName, statusLabel);
}

async function copyPerfArtifactToClipboard(url, statusLabel) {
  flashPerfReportStatus(`${statusLabel} 复制中...`);
  try {
    const response = await fetch(url);
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(text || `HTTP ${response.status}`);
    }
    let text = await response.text();
    // The exporter writes utf-8-sig — strip the BOM so Excel-friendly bytes
    // don't pollute clipboard consumers.
    if (text.charCodeAt(0) === 0xFEFF) text = text.substring(1);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    flashPerfReportStatus(`${statusLabel} 已复制到剪贴板 ✓`);
  } catch (error) {
    const msg = error && error.message ? error.message : String(error);
    flashPerfReportStatus(`${statusLabel} 复制失败：${msg}`, "error");
    alert(`${statusLabel} 复制失败：${msg}`);
  }
}

function handleDownloadPerfZip() {
  if (!currentPerfJobId) {
    alert("请先选择一个性能分析任务。");
    return;
  }
  const url = `/api/renderdoc-perf/jobs/${currentPerfJobId}/export?format=zip`;
  downloadPerfArtifactViaBlob(url, `${currentPerfJobId}_perf_export.zip`, "报告+CSV (.zip)");
}

function updatePerfExportButtonsState() {
  const hasData = Boolean(currentPerfAnalysis && (currentPerfAnalysis.rows || []).length);
  const hasJob = Boolean(currentPerfJobId);
  const copyBtn = document.getElementById("perf-copy-tsv-btn");
  const downloadBtn = document.getElementById("perf-download-pack-btn");
  if (copyBtn) copyBtn.disabled = !hasData;
  if (downloadBtn) downloadBtn.disabled = !hasJob;
}

function highlightPerfRow(eid) {
  if (!eid) return;
  const safeEid = String(eid);
  const row = document.getElementById(`perf-row-${safeEid}`);
  if (!row) {
    return;
  }
  const wrap = document.getElementById("perf-table-wrap");
  if (wrap) {
    const wrapRect = wrap.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    const offset = rowRect.top - wrapRect.top - (wrapRect.height / 2) + (rowRect.height / 2);
    wrap.scrollBy({ top: offset, behavior: "smooth" });
  }
  row.classList.remove("perf-row-highlight");
  // Force reflow so the animation restarts even on repeated clicks.
  void row.offsetWidth;
  row.classList.add("perf-row-highlight");
  window.setTimeout(() => {
    row.classList.remove("perf-row-highlight");
  }, 2200);
}

// Maps a file extension within the perf exports folder to the export endpoint
// format token + the label used in download status flashes.
const PERF_EXPORT_FORMAT_FOR_FILE = {
  "draws.csv": { format: "csv", label: "draws.csv" },
  "draws.tsv": { format: "tsv", label: "draws.tsv" },
  "overview.csv": { format: "zip", label: "overview.csv (in zip)", useArtifact: true },
  "passes.csv": { format: "zip", label: "passes.csv (in zip)", useArtifact: true },
  "textures.csv": { format: "zip", label: "textures.csv (in zip)", useArtifact: true },
  "shaders.csv": { format: "zip", label: "shaders.csv (in zip)", useArtifact: true },
  "findings.csv": { format: "zip", label: "findings.csv (in zip)", useArtifact: true },
};

function rewritePerfReportLinks(container, jobId) {
  const links = container.querySelectorAll("a[href]");
  links.forEach((link) => {
    const href = link.getAttribute("href") || "";
    if (!href) return;
    if (href.startsWith("#perf-row-")) {
      link.dataset.perfRowAnchor = href.substring(1);
      link.setAttribute("href", "javascript:void(0)");
      return;
    }
    if (href.startsWith("#finding-")) {
      link.dataset.perfFindingAnchor = href.substring(1);
      link.setAttribute("href", "javascript:void(0)");
      return;
    }
    if (href.startsWith("exports/") || href.startsWith("./exports/")) {
      const file = href.replace(/^\.\//, "").substring("exports/".length);
      const meta = PERF_EXPORT_FORMAT_FOR_FILE[file];
      // The draws.tsv link in section 4 doubles as "copy to clipboard" per
      // user feedback - dedicated action gets its own intercept marker.
      if (file === "draws.tsv") {
        link.dataset.perfCopyTsv = "1";
        link.dataset.perfArtifactPath = `artifacts/exports/${file}`;
        link.setAttribute("href", "javascript:void(0)");
        link.setAttribute("title", "点击直接复制 TSV 内容到剪贴板");
        return;
      }
      if (meta && meta.format === "csv") {
        link.dataset.perfDownloadFormat = "csv";
        link.dataset.perfDownloadName = `${jobId}_${file}`;
        link.dataset.perfDownloadLabel = meta.label;
        link.setAttribute("href", "javascript:void(0)");
        return;
      }
      // The 4 supplementary CSVs (overview/passes/textures/shaders/findings)
      // only live inside the zip pack. Rewrite their links to /artifact so
      // a direct fetch + blob-download works in pywebview.
      link.dataset.perfArtifactPath = `artifacts/exports/${file}`;
      link.dataset.perfDownloadName = `${jobId}_${file}`;
      link.dataset.perfDownloadLabel = file;
      link.setAttribute("href", "javascript:void(0)");
      return;
    }
  });
}

async function renderPerfReportPanel(jobId) {
  const panel = document.getElementById("perf-report-panel");
  const status = document.getElementById("perf-report-status");
  const linkMd = document.getElementById("perf-report-download-md");
  const linkHtml = document.getElementById("perf-report-download-html");
  const linkZip = document.getElementById("perf-report-download-zip");
  const linkEnhancedView = document.getElementById("perf-report-view-enhanced");
  const linkEnhancedMd = document.getElementById("perf-report-download-enhanced-md");
  if (!panel) return;
  panel.innerHTML = '<div class="empty-state">报告加载中...</div>';
  if (status) status.textContent = "报告加载中...";
  [linkMd, linkHtml, linkZip, linkEnhancedView, linkEnhancedMd].forEach((el) => { if (el) el.classList.add("hidden"); });
  if (!jobId) {
    panel.innerHTML = '<div class="empty-state">执行性能分析后将自动生成报告</div>';
    if (status) status.textContent = "尚未生成报告";
    return;
  }
  try {
    const response = await fetch(`/api/renderdoc-perf/jobs/${jobId}/report?format=html`);
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    const html = await response.text();
    // Extract <body> so the wrapper styles in the report HTML don't clash
    // with the host page styles.
    const bodyMatch = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
    const innerHtml = bodyMatch ? bodyMatch[1] : html;
    panel.innerHTML = innerHtml;
    // The standalone report embeds a full perf-results table so the
    // downloaded file is self-contained.  The SPA already shows that data
    // in #perf-table-wrap above, so we strip the embedded copy to avoid
    // duplication and keep the inline panel compact.
    const embedded = panel.querySelector("#perf-results-section");
    if (embedded) {
      embedded.remove();
    }
    rewritePerfReportLinks(panel, jobId);
    if (status) status.textContent = "报告已生成";
    if (linkMd) {
      linkMd.href = `/api/renderdoc-perf/jobs/${jobId}/export?format=md`;
      linkMd.classList.remove("hidden");
    }
    if (linkHtml) {
      linkHtml.href = `/api/renderdoc-perf/jobs/${jobId}/export?format=html`;
      linkHtml.classList.remove("hidden");
    }
    if (linkZip) {
      linkZip.href = `/api/renderdoc-perf/jobs/${jobId}/export?format=zip`;
      linkZip.classList.remove("hidden");
    }
    // Enhanced report links are best-effort: probe the endpoint and only
    // reveal them when the artifact exists for this job.
    try {
      const enhancedResp = await fetch(`/api/renderdoc-perf/jobs/${jobId}/report?format=enhanced`, { method: "HEAD" });
      if (enhancedResp.ok) {
        if (linkEnhancedView) {
          linkEnhancedView.href = `/api/renderdoc-perf/jobs/${jobId}/report?format=enhanced`;
          linkEnhancedView.classList.remove("hidden");
        }
        if (linkEnhancedMd) {
          linkEnhancedMd.href = `/api/renderdoc-perf/jobs/${jobId}/report?format=enhanced_md`;
          linkEnhancedMd.classList.remove("hidden");
        }
      }
    } catch (e) { /* enhanced report optional */ }
  } catch (error) {
    const msg = error && error.message ? error.message : String(error);
    panel.innerHTML = `<div class="empty-state">报告未生成或加载失败：${escapeHtml(msg)}</div>`;
    if (status) status.textContent = "报告未生成";
  }
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

  if (exportScope === "single" && !singlePass.id && !singleManualEid) {
    throw new Error("请先读取 Pass 列表并选择一个 Pass，或手动填写单个 EID。");
  }
  if (exportScope === "range" && (!(startPass.id || startManualEid) || !(endPass.id || endManualEid))) {
    throw new Error("请先读取 Pass 列表并选择起始/结束 Pass，或手动填写起始/结束 EID。");
  }
  if (!capturePath) {
    throw new Error("请先选择 .rdc 文件。");
  }

  return {
    capturePath,
    exportScope,
    passId: singleManualEid || singlePass.id,
    passName: singleManualEid || singlePass.label || singlePass.displayName || singlePass.name,
    passStartId: startManualEid || startPass.id,
    passStart: startManualEid || startPass.label || startPass.displayName || startPass.name,
    passEndId: endManualEid || endPass.id,
    passEnd: endManualEid || endPass.label || endPass.displayName || endPass.name,
    exportFbx: document.getElementById("asset-export-fbx").checked,
    exportObj: document.getElementById("asset-export-obj").checked,
    flipTextureY: document.getElementById("asset-export-flip-texture-y").checked,
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
  formData.append("capture_path", draft.capturePath);
  response = await fetch("/api/asset-export/export-mapping-preview/by-path", {
    method: "POST",
    body: formData,
  });
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
    <div><strong>贴图:</strong> ${input.texture_format || "-"}${input.flip_texture_y ? " (上下翻转)" : ""}</div>
    <div><strong>导出目录:</strong> ${outputRoot || "未设置"}</div>
    <div><strong>阶段:</strong> ${progress.stage || "-"}</div>
    <div><strong>说明:</strong> ${progress.message || "-"}</div>
    <div><strong>CSV:</strong> ${(result.csv_files || []).length}</div>
    <div><strong>模型:</strong> ${(result.model_files || []).length}</div>
    <div><strong>Shader:</strong> ${(result.shader_files || []).length}</div>
    <div><strong>贴图:</strong> ${(result.texture_files || []).length}</div>
    <div><strong>失败:</strong> ${(result.failed_items || []).length}</div>
    ${outputRoot ? `<div><button id="asset-export-open-output-btn" type="button" class="secondary-btn">复制输出目录路径</button></div>` : ""}
  `;
  const openButton = document.getElementById("asset-export-open-output-btn");
  if (openButton) {
    openButton.addEventListener("click", () => {
      copyServerPathToClipboard(outputRoot);
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
    `Base: ${basePath || "-"}`,
    `New: ${newPath || "-"}`,
    "说明: 正在执行性能 Diff，请稍候...",
  ]);
  setLogBusy("cmp-run-log", "正在执行 renderdoc_cmp，请稍候...");

  try {
    if (!basePath || !newPath) {
      throw new Error("请先选择 base/new 两个 .rdc 文件。");
    }
    const formData = new FormData();
    formData.append("base_path", basePath);
    formData.append("new_path", newPath);
    formData.append("strict_mode", strictMode);
    formData.append("verbose", verbose);
    formData.append("renderdoc_dir", renderdocDir);
    formData.append("malioc_path", maliocPath);
    const response = await fetch("/api/renderdoc-cmp/compare/by-path", {
      method: "POST",
      body: formData,
    });
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

// Stage -> human-readable Chinese label.  Used by the perf progress
// polling UI.  Keep these aligned with the stages emitted by
// ``RenderdocPerfService._emit_progress`` in renderdoc_perf_service.py.
const PERF_STAGE_LABELS = {
  init: "初始化",
  load_draws: "加载 draw 列表",
  replay_open: "打开 RenderDoc capture",
  fetch_counters: "采集 GPU counter",
  build_rows: "分析每个 draw",
  previews: "生成线框预览",
  report: "生成性能诊断报告",
  completed: "已完成",
  failed: "失败",
  // XML fallback path
  convert: "转换 capture 为 XML",
  xml_parse: "解析 XML",
  qr_replay: "qrenderdoc 升级预览",
};

let _perfPollIntervalId = null;
let _perfPollStartedAt = 0;

function _showPerfProgress(stage, message, current, total) {
  const wrap = document.getElementById("perf-progress");
  const stageEl = document.getElementById("perf-progress-stage");
  const msgEl = document.getElementById("perf-progress-message");
  const elapsedEl = document.getElementById("perf-progress-elapsed");
  if (!wrap) return;
  wrap.classList.remove("hidden");
  const label = PERF_STAGE_LABELS[stage] || stage || "运行中";
  let stageText = label;
  if (typeof current === "number" && typeof total === "number" && total > 0) {
    stageText = `${label} (${current}/${total})`;
  }
  if (stageEl) stageEl.textContent = stageText;
  if (msgEl) msgEl.textContent = message || "";
  if (elapsedEl) {
    const sec = Math.max(0, Math.round((Date.now() - _perfPollStartedAt) / 1000));
    elapsedEl.textContent = `已用时 ${sec}s`;
  }
}

function _hidePerfProgress() {
  const wrap = document.getElementById("perf-progress");
  if (wrap) wrap.classList.add("hidden");
}

function _stopPerfPoll() {
  if (_perfPollIntervalId !== null) {
    clearInterval(_perfPollIntervalId);
    _perfPollIntervalId = null;
  }
}

async function _pollPerfJobOnce(jobId, onDone) {
  try {
    const resp = await fetch(`/api/renderdoc-perf/jobs/${jobId}`);
    if (!resp.ok) return;
    const detail = await resp.json();
    const metadata = detail.metadata || {};
    const progress = metadata.progress || {};
    const status = metadata.status || "running";
    _showPerfProgress(progress.stage, progress.message, progress.current, progress.total);
    if (status === "completed" || status === "failed") {
      _stopPerfPoll();
      onDone(status, detail);
    }
  } catch (_e) {
    // transient network error - keep polling.
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
    `Capture: ${capturePath || "-"}`,
    renderdocDir ? `RenderDoc: ${renderdocDir}` : "",
    "说明: 正在执行单帧性能分析，请稍候...",
  ].filter(Boolean));
  setLogBusy("perf-run-log", "正在读取 draw/counter 并分析性能，请稍候...");
  _perfPollStartedAt = Date.now();
  _showPerfProgress("init", "已提交任务，等待 worker 启动…", 0, 0);

  let submitResp;
  try {
    if (!capturePath) {
      throw new Error("请先选择一个 .rdc 文件。");
    }
    const formData = new FormData();
    formData.append("capture_path", capturePath);
    formData.append("renderdoc_dir", renderdocDir);
    submitResp = await fetch("/api/renderdoc-perf/analyze/by-path", {
      method: "POST",
      body: formData,
    });
  } catch (error) {
    _hidePerfProgress();
    button.disabled = false;
    button.textContent = "执行性能分析";
    alert(error.message);
    return;
  }

  let initialPayload;
  try {
    initialPayload = await submitResp.json();
  } catch (_e) {
    initialPayload = null;
  }
  if (!submitResp.ok || !initialPayload || !initialPayload.job_id) {
    _hidePerfProgress();
    button.disabled = false;
    button.textContent = "执行性能分析";
    alert((initialPayload && initialPayload.detail) || "性能分析提交失败");
    return;
  }

  const jobId = initialPayload.job_id;
  currentPerfJobId = jobId;

  // Hard upper bound: 1 hour of consecutive polling without success.
  // Mirrors the qrenderdoc backend timeout (900s) plus headroom.
  const pollDeadline = Date.now() + 60 * 60 * 1000;

  _stopPerfPoll();
  _perfPollIntervalId = setInterval(async () => {
    if (Date.now() > pollDeadline) {
      _stopPerfPoll();
      _hidePerfProgress();
      button.disabled = false;
      button.textContent = "执行性能分析";
      alert("性能分析超过 1 小时仍未完成，已停止轮询。请查看 server 日志。");
      return;
    }
    await _pollPerfJobOnce(jobId, async (status, detail) => {
      _hidePerfProgress();
      button.disabled = false;
      button.textContent = "执行性能分析";
      if (status === "failed") {
        const err = (detail.metadata && detail.metadata.progress && detail.metadata.progress.message)
          || "性能分析失败";
        alert(err);
        return;
      }
      try {
        renderPerfSummary(detail);
        await loadPerfJobs();
        switchTab("perf");
      } catch (renderErr) {
        alert(`渲染性能结果失败: ${renderErr.message}`);
      }
    });
  }, 1000);
  // Fire one immediate poll so the user doesn't wait a full second to see the first stage.
  _pollPerfJobOnce(jobId, () => {});
}

async function handleAssetPassScan(event) {
  event.preventDefault();
  const capturePath = document.getElementById("asset-capture-source-path").value.trim();
  const button = document.getElementById("asset-pass-scan-btn");
  button.disabled = true;
  button.textContent = "读取中...";
  setLogBusy("asset-pass-scan-output", "正在读取 Pass 列表，请稍候...");
  try {
    if (!capturePath) {
      throw new Error("请先选择 .rdc 文件。");
    }
    const formData = new FormData();
    formData.append("capture_path", capturePath);
    const response = await fetch("/api/asset-export/scan-passes/by-path", {
      method: "POST",
      body: formData,
    });
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
    if (!csvPath) {
      throw new Error("请先选择 CSV 文件、多个 CSV 路径，或填写目录路径。");
    }
    const formData = new FormData();
    formData.append("csv_path", csvPath);
    const response = await fetch("/api/asset-export/csv-inspect/by-path", {
      method: "POST",
      body: formData,
    });
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
  commonForm.append("flip_texture_y", draft.flipTextureY ? "true" : "false");
  commonForm.append("texture_format", draft.textureFormat);
  commonForm.append("notes", draft.notes);
  Object.entries(mapping || {}).forEach(([key, value]) => {
    commonForm.append(key, value || "");
  });

  commonForm.append("capture_path", draft.capturePath);
  response = await fetch("/api/asset-export/jobs/by-path", {
    method: "POST",
    body: commonForm,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "保存资产导出任务失败");
  }
  currentExportJobId = data.metadata.job_id;
  renderAssetExportSummary(data);
  await loadAssetExportJobs();
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
    formData.append("flip_texture_y", document.getElementById("asset-csv-flip-texture-y").checked ? "true" : "false");
    if (!csvPath) {
      throw new Error("请先选择 CSV 文件、多个 CSV 路径，或填写目录路径。");
    }
    formData.append("csv_path", csvPath);
    const targetUrl = currentExportJobId
      ? `/api/asset-export/jobs/${currentExportJobId}/convert-csv/by-path`
      : "/api/asset-export/convert-csv/by-path";
    response = await fetch(targetUrl, {
      method: "POST",
      body: formData,
    });
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
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-fs-pick]");
  if (btn) {
    event.preventDefault();
    openFsPicker(btn);
  }
});
{
  const goBtn = document.getElementById("fs-picker-go");
  const upBtn = document.getElementById("fs-picker-up");
  const chooseBtn = document.getElementById("fs-picker-choose");
  const cancelBtn = document.getElementById("fs-picker-cancel");
  const pathInput = document.getElementById("fs-picker-path");
  const modal = document.getElementById("fs-picker-modal");
  if (goBtn && pathInput) goBtn.addEventListener("click", () => loadFsPath(pathInput.value.trim()));
  if (pathInput) pathInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadFsPath(pathInput.value.trim()); } });
  if (upBtn) upBtn.addEventListener("click", async () => {
    try {
      const data = await fsList(fsPicker.current);
      if (data.parent === null) return;
      loadFsPath(data.parent || "");
    } catch (_e) { /* ignore */ }
  });
  if (chooseBtn) chooseBtn.addEventListener("click", () => applyFsPick(fsPicker.current));
  if (cancelBtn) cancelBtn.addEventListener("click", closeFsPicker);
  if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeFsPicker(); });
}
document.getElementById("perf-sort-field").addEventListener("change", renderPerfTable);
document.getElementById("perf-sort-direction").addEventListener("change", renderPerfTable);

// ---- RenderDoc runtime detection status ----
async function refreshRenderdocStatus(statusId, inputId) {
  const el = document.getElementById(statusId);
  if (!el) return;
  const inputEl = document.getElementById(inputId);
  const dir = ((inputEl && inputEl.value) || "").split(/\r?\n/)[0].trim();
  try {
    const params = new URLSearchParams();
    if (dir) params.set("renderdoc_dir", dir);
    const resp = await fetch(`/api/renderdoc-runtime/status?${params.toString()}`);
    const data = await resp.json();
    if (data.available) {
      const srcLabel = {
        task_override: "自定义路径",
        global_settings: "全局设置",
        bundled: "内置",
        path: "系统安装",
      }[data.source] || data.source || "";
      const where = data.renderdoc_cmd_path || data.renderdoc_python_path || data.renderdoc_dir || "";
      el.className = "renderdoc-status ok";
      el.textContent = `✓ 已检测到 RenderDoc（来源：${srcLabel}）${where ? " · " + where : ""}`;
    } else {
      el.className = "renderdoc-status warn";
      el.innerHTML = `⚠ ${escapeHtml(data.guidance || "未检测到 RenderDoc，请安装官方 RenderDoc 或填写自定义路径。")} ` +
        `<a href="https://renderdoc.org/builds" target="_blank" rel="noopener">下载官方 RenderDoc</a>`;
    }
  } catch (_e) {
    el.className = "renderdoc-status";
    el.textContent = "RenderDoc 检测失败（不影响手动填写路径）";
  }
}
["perf-renderdoc-dir", "cmp-renderdoc-dir"].forEach((inputId) => {
  const statusId = inputId === "perf-renderdoc-dir" ? "perf-renderdoc-status" : "cmp-renderdoc-status";
  const inputEl = document.getElementById(inputId);
  if (inputEl) inputEl.addEventListener("change", () => refreshRenderdocStatus(statusId, inputId));
  refreshRenderdocStatus(statusId, inputId);
});
{
  // "上下翻转贴图" toggle in the perf toolbar.  Pure SPA-side CSS
  // flip via a body-scoped class - this never touches the PNG files
  // on disk, and the downloaded HTML/ZIP reports continue to use the
  // original (un-flipped) base64 images on purpose.  State is
  // persisted in localStorage so the user doesn't have to re-tick
  // after every reload.
  const FLIP_KEY = "perf-flip-texture-y";
  const flipInput = document.getElementById("perf-flip-texture-y");
  function applyPerfFlipState() {
    const on = !!(flipInput && flipInput.checked);
    document.body.classList.toggle("perf-preview-flipped", on);
    try {
      localStorage.setItem(FLIP_KEY, on ? "1" : "0");
    } catch (_e) { /* ignore quota / privacy errors */ }
  }
  if (flipInput) {
    try {
      flipInput.checked = localStorage.getItem(FLIP_KEY) === "1";
    } catch (_e) { /* ignore */ }
    applyPerfFlipState();
    flipInput.addEventListener("change", applyPerfFlipState);
  }
}
{
  const copyBtn = document.getElementById("perf-copy-tsv-btn");
  if (copyBtn) copyBtn.addEventListener("click", handleCopyPerfTsv);
  const zipBtn = document.getElementById("perf-download-pack-btn");
  if (zipBtn) zipBtn.addEventListener("click", handleDownloadPerfZip);
  const panel = document.getElementById("perf-report-panel");
  if (panel) {
    panel.addEventListener("click", (event) => {
      const link = event.target.closest("a");
      if (!link) return;
      if (link.dataset.perfRowAnchor) {
        event.preventDefault();
        const id = link.dataset.perfRowAnchor;
        const eid = id.startsWith("perf-row-") ? id.substring("perf-row-".length) : id;
        highlightPerfRow(eid);
        return;
      }
      if (link.dataset.perfFindingAnchor) {
        event.preventDefault();
        const target = document.getElementById(link.dataset.perfFindingAnchor);
        if (target && target.scrollIntoView) {
          target.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        return;
      }
      if (link.dataset.perfCopyTsv) {
        event.preventDefault();
        if (!currentPerfJobId) {
          alert("请先选择一个性能分析任务。");
          return;
        }
        const url = `/api/renderdoc-perf/jobs/${currentPerfJobId}/export?format=tsv`;
        copyPerfArtifactToClipboard(url, "draws.tsv");
        return;
      }
      if (link.dataset.perfDownloadFormat) {
        event.preventDefault();
        if (!currentPerfJobId) {
          alert("请先选择一个性能分析任务。");
          return;
        }
        const fmt = link.dataset.perfDownloadFormat;
        const name = link.dataset.perfDownloadName || `perf_export.${fmt}`;
        const label = link.dataset.perfDownloadLabel || name;
        const url = `/api/renderdoc-perf/jobs/${currentPerfJobId}/export?format=${encodeURIComponent(fmt)}`;
        downloadPerfArtifactViaBlob(url, name, label);
        return;
      }
      if (link.dataset.perfArtifactPath) {
        event.preventDefault();
        if (!currentPerfJobId) {
          alert("请先选择一个性能分析任务。");
          return;
        }
        const path = link.dataset.perfArtifactPath;
        const name = link.dataset.perfDownloadName || path.split("/").pop();
        const label = link.dataset.perfDownloadLabel || name;
        const url = `/api/renderdoc-perf/jobs/${currentPerfJobId}/artifact?path=${encodeURIComponent(path)}`;
        downloadPerfArtifactViaBlob(url, name, label);
        return;
      }
    });
  }

  // Hook the 3 toolbar download links (md/html/zip) so they go through the
  // blob downloader path too — keeps pywebview consistent with browsers.
  ["perf-report-download-md", "perf-report-download-html", "perf-report-download-zip"].forEach((id) => {
    const link = document.getElementById(id);
    if (!link) return;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (!currentPerfJobId) {
        alert("请先选择一个性能分析任务。");
        return;
      }
      const fmt = id.endsWith("-md") ? "md" : (id.endsWith("-html") ? "html" : "zip");
      const ext = fmt === "zip" ? "zip" : (fmt === "html" ? "html" : "md");
      const name = `${currentPerfJobId}_perf_${fmt === "zip" ? "export.zip" : (fmt === "html" ? "report.html" : "report.md")}`;
      const label = fmt === "zip" ? "报告+CSV (.zip)" : (fmt === "html" ? "perf_report.html" : "perf_report.md");
      void ext;
      const url = `/api/renderdoc-perf/jobs/${currentPerfJobId}/export?format=${fmt}`;
      downloadPerfArtifactViaBlob(url, name, label);
    });
  });
}
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
    overlaySrc: trigger.dataset.previewOverlaySrc || "",
    overlayKind: trigger.dataset.previewOverlayKind || "",
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
      overlaySrc: trigger.dataset.previewOverlaySrc || "",
      overlayKind: trigger.dataset.previewOverlayKind || "",
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
loadCmpJobs();
loadPerfJobs();
loadAssetExportJobs();
