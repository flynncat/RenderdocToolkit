"""Test the visual probe simplification pipeline on real exported shaders.

Two modes:
  --offline   Only run static analysis + candidate generation (no RenderDoc)
  --live      Run full visual probing using the .rdc file (requires RenderDoc)

Generates an HTML report showing:
  - Static simplification results
  - All probe candidates per shader
  - If live: accepted/rejected probes with SSIM scores
  - Comparison of original → static-simplified → visually-simplified
"""
import argparse
import html as html_mod
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.glsl_simplifier import GlslSimplifier
from app.services.glsl_code_analyzer import GlslCodeAnalyzer
from app.services.visual_probe_simplifier import VisualProbeSimplifier
from app.services.deterministic_rule_engine import DeterministicRuleEngine

EXPORT_ROOT = Path(r"G:\抓帧\蛋仔描边抓帧\DZ_ZMXT-frame5080_RenderdocDiffExport")
RDC_FILE = Path(r"G:\抓帧\蛋仔描边抓帧\DZ_ZMXT-frame5080.rdc")
REPORT_DIR = Path(__file__).resolve().parent.parent / "test_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT = REPORT_DIR / "visual_probe_sessions"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

simplifier = GlslSimplifier()
analyzer = GlslCodeAnalyzer()
engine = DeterministicRuleEngine()


def discover_shaders(root: Path):
    shaders = []
    shader_dir = root / "shaders"
    if not shader_dir.exists():
        return shaders
    for eid_dir in sorted(shader_dir.iterdir()):
        if not eid_dir.is_dir():
            continue
        for fs_file in sorted(eid_dir.glob("*_fs.glsl")):
            eid_name = eid_dir.name
            eid_num = None
            import re
            m = re.search(r"EID_(\d+)", eid_name)
            if m:
                eid_num = int(m.group(1))
            params_file = list(eid_dir.glob("*_shader_params.json"))
            shaders.append({
                "eid_name": eid_name,
                "eid_num": eid_num,
                "fs_path": fs_file,
                "params_path": params_file[0] if params_file else None,
            })
    return shaders


def run_offline_test(shader_info: dict) -> dict:
    """Run static simplification + candidate analysis without RenderDoc."""
    fs_path = shader_info["fs_path"]
    glsl = fs_path.read_text(encoding="utf-8", errors="replace")
    params_json = ""
    if shader_info["params_path"]:
        params_json = shader_info["params_path"].read_text(encoding="utf-8", errors="replace")

    result = {
        "eid": shader_info["eid_name"],
        "eid_num": shader_info["eid_num"],
        "original_lines": len(glsl.splitlines()),
        "original_source": glsl,
    }

    t0 = time.time()
    try:
        simp = simplifier.simplify(glsl, shader_params_json=params_json, levels="L0,L1,L2,L3,L4")
        result["static_simplified_lines"] = simp.simplified_line_count
        result["static_simplified_source"] = simp.simplified_source
        result["simplify_ok"] = True

        candidates = analyzer.analyze(simp.simplified_source)
        result["candidates"] = []
        for c in candidates:
            result["candidates"].append({
                "kind": c.kind,
                "label": c.label,
                "description": c.description,
                "line_range": c.line_range,
                "snippet": c.original_snippet[:150],
            })

        result["uniform_probes"] = sum(1 for c in candidates if c.kind == "uniform")
        result["if_block_probes"] = sum(1 for c in candidates if c.kind in ("if_block", "if_branch_keep_if", "if_branch_keep_else"))
        result["preprocessor_probes"] = sum(1 for c in candidates if c.kind.startswith("preprocessor_"))
        result["statement_probes"] = sum(1 for c in candidates if c.kind == "statement")
        result["statement_default_probes"] = sum(1 for c in candidates if c.kind == "statement_default")
        result["total_probes"] = len(candidates)

        conv = engine.convert(simp.simplified_source, shader_params_json=params_json, mode="both")
        result["hlsl_lines"] = len(conv.standalone_hlsl.splitlines())
        result["convert_ok"] = conv.success
    except Exception as exc:
        result["simplify_ok"] = False
        result["error"] = str(exc)
        result["candidates"] = []
        result["total_probes"] = 0

    result["elapsed_ms"] = round((time.time() - t0) * 1000)
    return result


def run_live_test(shader_info: dict, rdc_path: Path) -> dict:
    """Run full visual probe with RenderDoc."""
    from app.services.pixel_diff_service import PixelDiffService
    from app.services.shader_verify_service import ShaderVerifyService

    pixel_diff = PixelDiffService()
    verify_svc = ShaderVerifyService(pixel_diff)
    probe = VisualProbeSimplifier(verify_svc, simplifier, analyzer)
    compile_only = not getattr(run_live_test, '_full_render', False)

    fs_path = shader_info["fs_path"]
    glsl = fs_path.read_text(encoding="utf-8", errors="replace")
    params_json = ""
    if shader_info["params_path"]:
        params_json = shader_info["params_path"].read_text(encoding="utf-8", errors="replace")

    eid_num = shader_info["eid_num"]
    if eid_num is None:
        return {
            "eid": shader_info["eid_name"],
            "error": "无法解析 EID 数字",
            "mode": "live",
        }

    out_dir = OUTPUT_ROOT / shader_info["eid_name"]

    result = {
        "eid": shader_info["eid_name"],
        "eid_num": eid_num,
        "original_lines": len(glsl.splitlines()),
        "original_source": glsl,
        "mode": "live",
    }

    t0 = time.time()
    try:
        probe_result = probe.run(
            capture_path=rdc_path,
            eid=eid_num,
            original_glsl=glsl,
            shader_params_json=params_json,
            output_dir=out_dir,
            ssim_threshold=0.995,
            max_probes=100,
            compile_only=compile_only,
        )
        result["static_simplified_lines"] = probe_result.static_simplified_lines
        result["static_simplified_source"] = probe_result.static_simplified_source
        result["final_lines"] = probe_result.final_lines
        result["final_source"] = probe_result.final_source
        result["total_probes"] = probe_result.total_probes
        result["accepted_probes"] = probe_result.accepted_probes
        result["rejected_probes"] = probe_result.rejected_probes
        result["compile_failed"] = probe_result.compile_failed_probes
        result["probe_steps"] = [s.to_dict() for s in probe_result.probe_steps]
        result["simplify_ok"] = True

        conv = engine.convert(probe_result.final_source, shader_params_json=params_json, mode="both")
        result["hlsl_lines"] = len(conv.standalone_hlsl.splitlines())
        result["convert_ok"] = conv.success
    except Exception as exc:
        result["simplify_ok"] = False
        result["error"] = str(exc)

    result["elapsed_ms"] = round((time.time() - t0) * 1000)
    return result


def esc(text):
    return html_mod.escape(str(text))


def generate_html_report(results: list, mode: str, output_path: Path):
    total = len(results)
    ok = sum(1 for r in results if r.get("simplify_ok"))

    rows = []
    detail_sections = []

    for idx, r in enumerate(results):
        eid = r["eid"]
        orig = r.get("original_lines", 0)
        simp = r.get("static_simplified_lines", orig)
        static_reduction = round((1 - simp / max(orig, 1)) * 100) if orig else 0

        if mode == "live":
            final = r.get("final_lines", simp)
            visual_reduction = round((1 - final / max(simp, 1)) * 100) if simp else 0
            total_reduction = round((1 - final / max(orig, 1)) * 100) if orig else 0
            accepted = r.get("accepted_probes", 0)
            total_p = r.get("total_probes", 0)
            rows.append(f"""
            <tr onclick="showDetail('detail-{idx}')">
              <td>{esc(eid)}</td>
              <td>{orig}</td>
              <td>{simp} <span class="pct">(-{static_reduction}%)</span></td>
              <td>{final} <span class="pct">(-{total_reduction}%)</span></td>
              <td>{accepted}/{total_p}</td>
              <td><span class="pct">-{visual_reduction}%</span></td>
              <td>{r.get('elapsed_ms', 0)}ms</td>
            </tr>""")
        else:
            total_p = r.get("total_probes", 0)
            uni_p = r.get("uniform_probes", 0)
            if_p = r.get("if_block_probes", 0)
            pp_p = r.get("preprocessor_probes", 0)
            stmt_p = r.get("statement_probes", 0)
            stmtd_p = r.get("statement_default_probes", 0)
            rows.append(f"""
            <tr onclick="showDetail('detail-{idx}')">
              <td>{esc(eid)}</td>
              <td>{orig}</td>
              <td>{simp} <span class="pct">(-{static_reduction}%)</span></td>
              <td>{total_p}</td>
              <td>{pp_p}</td>
              <td>{uni_p}</td>
              <td>{if_p}</td>
              <td>{stmt_p + stmtd_p}</td>
              <td>{r.get('elapsed_ms', 0)}ms</td>
            </tr>""")

        cand_html = ""
        if mode == "offline":
            for ci, c in enumerate(r.get("candidates", [])):
                kind_class = c["kind"]
                cand_html += f"""
                <div class="probe-item {kind_class}">
                  <span class="probe-kind">{c['kind']}</span>
                  <span class="probe-label">{esc(c['label'])}</span>
                  <div class="probe-desc">{esc(c['description'])}</div>
                  <pre class="probe-snippet">{esc(c['snippet'])}</pre>
                </div>"""
        elif mode == "live":
            for step in r.get("probe_steps", []):
                accepted_cls = "accepted" if step.get("accepted") else "rejected"
                ssim_str = f"SSIM={step.get('ssim', 0):.4f}" if step.get("ssim") else ""
                err_str = f" | {esc(step.get('error', ''))}" if step.get("error") else ""
                cand_html += f"""
                <div class="probe-item {accepted_cls}">
                  <span class="probe-kind">{step['kind']}</span>
                  <span class="probe-status {'badge-pass' if step['accepted'] else 'badge-fail'}">
                    {'ACCEPTED' if step['accepted'] else 'REJECTED'}
                  </span>
                  <span>{ssim_str}{err_str}</span>
                  <div class="probe-desc">{esc(step.get('description', ''))}</div>
                </div>"""

        detail_sections.append(f"""
    <div id="detail-{idx}" class="detail-panel" style="display:none;">
      <h2>{esc(eid)}</h2>
      <div class="meta-row">
        <span>原始: {orig} 行</span>
        <span>静态简化: {simp} 行</span>
        {'<span>视觉简化后: ' + str(r.get("final_lines", "N/A")) + ' 行</span>' if mode == 'live' else ''}
        <span>探针数: {r.get("total_probes", 0)}</span>
      </div>
      <h3>探针候选列表</h3>
      <div class="probe-list">{cand_html}</div>
      <div class="code-compare">
        <div class="code-col">
          <h3>原始 GLSL ({orig} 行)</h3>
          <pre class="code">{esc(r.get('original_source', '')[:6000])}</pre>
        </div>
        <div class="code-col">
          <h3>{'最终简化' if mode == 'live' else '静态简化'} ({r.get('final_lines', simp)} 行)</h3>
          <pre class="code">{esc(r.get('final_source', r.get('static_simplified_source', ''))[:6000])}</pre>
        </div>
      </div>
      <button class="close-btn" onclick="hideDetail('detail-{idx}')">关闭</button>
    </div>""")

    if mode == "live":
        table_header = "<th>EID</th><th>原始</th><th>静态简化</th><th>最终</th><th>接受/总探针</th><th>视觉优化</th><th>耗时</th>"
    else:
        table_header = "<th>EID</th><th>原始</th><th>静态简化</th><th>总探针</th><th>预处理</th><th>Uniform</th><th>If块</th><th>语句</th><th>耗时</th>"

    total_probes = sum(r.get("total_probes", 0) for r in results)
    total_accepted = sum(r.get("accepted_probes", 0) for r in results) if mode == "live" else 0
    mode_label = "完整模式 (RenderDoc 验证)" if mode == "live" else "离线模式 (候选分析)"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>视觉探针简化测试报告</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background:#0d1117; color:#c9d1d9; padding:24px; }}
  h1 {{ color:#58a6ff; margin-bottom:8px; }}
  .subtitle {{ color:#8b949e; margin-bottom:24px; }}
  .summary {{ display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
  .summary-card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px 24px; text-align:center; }}
  .summary-card .num {{ font-size:2em; font-weight:700; }}
  .summary-card .label {{ color:#8b949e; font-size:0.9em; }}
  .pass .num {{ color:#3fb950; }}
  .fail .num {{ color:#f85149; }}
  .total .num {{ color:#58a6ff; }}
  .rate .num {{ color:#d2a8ff; }}
  table {{ width:100%; border-collapse:collapse; background:#161b22; border-radius:8px; overflow:hidden; margin-bottom:24px; }}
  th {{ background:#21262d; padding:12px 16px; text-align:left; color:#8b949e; font-weight:600; font-size:0.85em; }}
  td {{ padding:10px 16px; border-top:1px solid #21262d; }}
  tr {{ cursor:pointer; transition:background 0.15s; }}
  tr:hover {{ background:#1c2128; }}
  .pct {{ color:#8b949e; font-size:0.85em; }}
  .detail-panel {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:24px; margin-bottom:24px; }}
  .detail-panel h2 {{ color:#58a6ff; margin-bottom:12px; }}
  .detail-panel h3 {{ color:#8b949e; font-size:0.9em; margin:16px 0 8px; }}
  .meta-row {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:12px; }}
  .meta-row span {{ background:#21262d; padding:4px 12px; border-radius:6px; font-size:0.85em; }}
  .probe-list {{ max-height:400px; overflow-y:auto; margin-bottom:16px; }}
  .probe-item {{ background:#0d1117; border:1px solid #21262d; border-radius:6px; padding:8px 12px; margin:4px 0; font-size:0.85em; }}
  .probe-item.uniform {{ border-left:3px solid #58a6ff; }}
   .probe-item.if_block {{ border-left:3px solid #d2a8ff; }}
   .probe-item.if_branch_keep_if, .probe-item.if_branch_keep_else {{ border-left:3px solid #bc8cff; }}
   .probe-item.preprocessor_define, .probe-item.preprocessor_define_inline, .probe-item.preprocessor_extension {{ border-left:3px solid #79c0ff; }}
   .probe-item.preprocessor_keep_if, .probe-item.preprocessor_keep_else, .probe-item.preprocessor_remove_block {{ border-left:3px solid #56d364; }}
   .probe-item.statement, .probe-item.statement_default {{ border-left:3px solid #e3b341; }}
  .probe-item.accepted {{ border-left:3px solid #3fb950; }}
  .probe-item.rejected {{ border-left:3px solid #f85149; }}
  .probe-kind {{ display:inline-block; background:#21262d; padding:2px 8px; border-radius:4px; font-family:monospace; margin-right:8px; }}
  .probe-label {{ font-weight:600; }}
  .probe-desc {{ color:#8b949e; margin-top:4px; }}
  .probe-snippet {{ background:#161b22; padding:4px 8px; margin-top:4px; font-size:0.8em; max-height:80px; overflow:auto; white-space:pre; }}
  .probe-status {{ display:inline-block; padding:1px 8px; border-radius:4px; font-size:0.8em; font-weight:700; margin-left:8px; }}
  .badge-pass {{ background:#238636; color:#fff; }}
  .badge-fail {{ background:#da3633; color:#fff; }}
  .code-compare {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:16px 0; }}
  .code-col h3 {{ color:#8b949e; font-size:0.85em; margin-bottom:6px; }}
  .code {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; font-family:'Cascadia Code','Fira Code',monospace; font-size:0.75em; line-height:1.5; overflow-x:auto; max-height:500px; overflow-y:auto; white-space:pre; }}
  .close-btn {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; padding:8px 20px; border-radius:6px; cursor:pointer; margin-top:12px; }}
  .close-btn:hover {{ background:#30363d; }}
  .pipeline-diagram {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px; margin-bottom:24px; text-align:center; }}
  .pipeline-diagram .step {{ display:inline-block; background:#21262d; border:1px solid #30363d; border-radius:8px; padding:8px 16px; margin:4px; font-size:0.85em; }}
  .pipeline-diagram .arrow {{ color:#58a6ff; margin:0 4px; font-weight:700; }}
  .pipeline-diagram .step.visual {{ border-color:#3fb950; color:#3fb950; }}
</style>
</head>
<body>
<h1>视觉探针简化测试报告</h1>
<p class="subtitle">{mode_label} | 数据源: DZ_ZMXT-frame5080 | {time.strftime('%Y-%m-%d %H:%M:%S')}</p>

<div class="pipeline-diagram">
  <span class="step">原始 GLSL</span>
  <span class="arrow">→</span>
  <span class="step">L0-L4 静态简化</span>
  <span class="arrow">→</span>
  <span class="step visual">预处理指令探针</span>
  <span class="arrow">→</span>
  <span class="step visual">Uniform 探针</span>
  <span class="arrow">→</span>
  <span class="step visual">If/Else 分支探针</span>
  <span class="arrow">→</span>
  <span class="step visual">语句探针</span>
  <span class="arrow">→</span>
  <span class="step">最终简化 GLSL</span>
  <span class="arrow">→</span>
  <span class="step">规则引擎 HLSL</span>
</div>

<div class="summary">
  <div class="summary-card total"><div class="num">{total}</div><div class="label">Shader 数</div></div>
  <div class="summary-card pass"><div class="num">{ok}</div><div class="label">分析成功</div></div>
  <div class="summary-card rate"><div class="num">{total_probes}</div><div class="label">总探针数</div></div>
  {'<div class="summary-card pass"><div class="num">' + str(total_accepted) + '</div><div class="label">已接受探针</div></div>' if mode == 'live' else ''}
</div>

<table>
<thead><tr>{table_header}</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>

{''.join(detail_sections)}

<script>
function showDetail(id) {{
  document.querySelectorAll('.detail-panel').forEach(el => el.style.display='none');
  document.getElementById(id).style.display='block';
  document.getElementById(id).scrollIntoView({{behavior:'smooth',block:'start'}});
}}
function hideDetail(id) {{
  document.getElementById(id).style.display='none';
}}
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Visual probe simplification test")
    parser.add_argument("--live", action="store_true", help="Run live RenderDoc verification")
    parser.add_argument("--compile-only", action="store_true", default=True, help="Only check compilation (default)")
    parser.add_argument("--full-render", action="store_true", help="Full render comparison (requires matching GPU)")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of shaders to test")
    args = parser.parse_args()

    if args.live:
        mode = "live"
        if args.full_render:
            run_live_test._full_render = True
        else:
            run_live_test._full_render = False
    else:
        mode = "offline"
    print(f"Mode: {mode}" + (" (compile-only)" if mode == "live" and not args.full_render else ""))
    print(f"Discovering shaders in: {EXPORT_ROOT}")

    shaders = discover_shaders(EXPORT_ROOT)
    if args.limit > 0:
        shaders = shaders[:args.limit]
    print(f"Found {len(shaders)} fragment shaders\n")

    results = []
    for shader in shaders:
        eid = shader["eid_name"]
        print(f"  Testing {eid}...", end=" ", flush=True)

        if mode == "live":
            r = run_live_test(shader, RDC_FILE)
        else:
            r = run_offline_test(shader)

        orig = r.get("original_lines", 0)
        simp = r.get("static_simplified_lines", orig)
        probes = r.get("total_probes", 0)

        if mode == "live":
            final = r.get("final_lines", simp)
            accepted = r.get("accepted_probes", 0)
            print(f"{orig}→{simp}→{final} 行 | 探针: {accepted}/{probes} | {r.get('elapsed_ms', 0)}ms")
        else:
            print(f"{orig}→{simp} 行 | 探针候选: {probes} | {r.get('elapsed_ms', 0)}ms")

        results.append(r)

    report_name = f"visual_probe_{'live' if mode == 'live' else 'offline'}_report.html"
    report_path = REPORT_DIR / report_name
    generate_html_report(results, mode, report_path)

    total_probes = sum(r.get("total_probes", 0) for r in results)
    total_accepted = sum(r.get("accepted_probes", 0) for r in results) if mode == "live" else 0

    print(f"\n{'='*70}")
    print(f"  Shader 总数: {len(results)}")
    print(f"  探针总数: {total_probes}")
    if mode == "live":
        print(f"  已接受探针: {total_accepted}")
    print(f"  报告: {report_path}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
