"""Batch real-shader test: run the full simplify+convert pipeline on
RenderDoc-exported shaders and generate an HTML comparison report.
"""
import html as html_mod
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.glsl_simplifier import GlslSimplifier
from app.services.deterministic_rule_engine import DeterministicRuleEngine
from app.services.shader_compiler_service import ShaderCompilerService
from app.services.spirv_bridge_verify import SpirvBridgeVerify

EXPORT_ROOT = Path(r"G:\抓帧\蛋仔描边抓帧\DZ_ZMXT-frame5080_RenderdocDiffExport")
REPORT_DIR = Path(__file__).resolve().parent.parent / "test_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

simplifier = GlslSimplifier()
engine = DeterministicRuleEngine()
compiler = ShaderCompilerService()
bridge = SpirvBridgeVerify(compiler)


def discover_shaders(root: Path):
    """Find all fragment shaders grouped by EID."""
    shaders = []
    shader_dir = root / "shaders"
    if not shader_dir.exists():
        return shaders
    for eid_dir in sorted(shader_dir.iterdir()):
        if not eid_dir.is_dir():
            continue
        for fs_file in sorted(eid_dir.glob("*_fs.glsl")):
            eid_name = eid_dir.name
            params_file = list(eid_dir.glob("*_shader_params.json"))
            vs_file = list(eid_dir.glob("*_vs.glsl"))
            texture_dir = root / "textures" / eid_name
            textures = sorted(texture_dir.glob("*.png")) if texture_dir.exists() else []
            shaders.append({
                "eid_name": eid_name,
                "fs_path": fs_file,
                "vs_path": vs_file[0] if vs_file else None,
                "params_path": params_file[0] if params_file else None,
                "textures": textures,
            })
    return shaders


def run_test(shader_info: dict) -> dict:
    """Run simplify + convert on a single shader."""
    fs_path = shader_info["fs_path"]
    glsl = fs_path.read_text(encoding="utf-8", errors="replace")
    params_json = ""
    if shader_info["params_path"]:
        params_json = shader_info["params_path"].read_text(encoding="utf-8", errors="replace")

    result = {
        "eid": shader_info["eid_name"],
        "fs_path": str(fs_path),
        "original_lines": len(glsl.splitlines()),
        "original_source": glsl,
        "texture_count": len(shader_info["textures"]),
        "has_params": bool(params_json),
        "has_vertex": shader_info["vs_path"] is not None,
    }

    t0 = time.time()
    try:
        simp = simplifier.simplify(glsl, shader_params_json=params_json, levels="L0,L1,L2,L3,L4")
        result["simplified_lines"] = simp.simplified_line_count
        result["simplified_source"] = simp.simplified_source
        result["simplify_transforms"] = [
            {"level": t.level, "name": t.name, "before": t.lines_before, "after": t.lines_after}
            for t in simp.transforms
        ]
        result["simplify_ok"] = True
    except Exception as exc:
        result["simplify_ok"] = False
        result["simplify_error"] = str(exc)
        result["simplified_source"] = glsl
        result["simplified_lines"] = result["original_lines"]
        result["simplify_transforms"] = []

    try:
        conv = engine.convert(result["simplified_source"], shader_params_json=params_json, mode="both")
        result["convert_ok"] = conv.success
        result["standalone_hlsl"] = conv.standalone_hlsl
        result["ue_custom_hlsl"] = conv.ue_custom_hlsl
        result["warnings"] = conv.warnings
        result["unsupported"] = conv.unsupported
        result["rules_applied"] = [r.to_dict() for r in conv.rules_applied]
        result["convert_error"] = conv.error
    except Exception as exc:
        result["convert_ok"] = False
        result["standalone_hlsl"] = ""
        result["ue_custom_hlsl"] = ""
        result["warnings"] = []
        result["unsupported"] = []
        result["rules_applied"] = []
        result["convert_error"] = str(exc)

    hlsl = result.get("standalone_hlsl", "")
    if hlsl.strip():
        dxc_res = compiler.validate_hlsl(hlsl, entry_point="main_standalone", profile="ps_6_0")
        result["dxc_ok"] = dxc_res.success
        result["dxc_errors"] = dxc_res.stderr
        if "not found" in (dxc_res.stderr or "").lower():
            result["dxc_available"] = False
        else:
            result["dxc_available"] = True
    else:
        result["dxc_ok"] = False
        result["dxc_errors"] = "No HLSL to validate"
        result["dxc_available"] = True

    simplified_src = result.get("simplified_source", "")
    if simplified_src.strip():
        bridge_dir = REPORT_DIR / "bridge" / shader_info["eid_name"]
        bridge_dir.mkdir(parents=True, exist_ok=True)
        br = bridge.full_convert_and_verify(
            glsl_source=simplified_src, output_dir=bridge_dir, stage="frag",
        )
        result["bridge_pipeline_ok"] = br.pipeline_ok
        result["bridge_glsl_spv_ok"] = br.glsl_spv_ok
        result["bridge_hlsl_spv_ok"] = br.hlsl_spv_ok
        result["bridge_errors"] = br.error
        result["bridge_hlsl_source"] = br.hlsl_source
    else:
        result["bridge_pipeline_ok"] = False
        result["bridge_glsl_spv_ok"] = False
        result["bridge_hlsl_spv_ok"] = False
        result["bridge_errors"] = "No simplified GLSL"
        result["bridge_hlsl_source"] = ""

    result["elapsed_ms"] = round((time.time() - t0) * 1000)

    glsl_tokens_remaining = []
    for token in ["sampler2D ", " vec2 ", " vec3 ", " vec4 ", "mat4 ", "fract(", "dFdx(", "dFdy("]:
        if token in hlsl:
            glsl_tokens_remaining.append(token.strip())
    result["residual_glsl_tokens"] = glsl_tokens_remaining

    result["overall_pass"] = (
        result.get("simplify_ok", False) and
        result.get("convert_ok", False) and
        len(glsl_tokens_remaining) == 0
    )

    return result


def esc(text):
    return html_mod.escape(str(text))


def generate_html_report(results: list, output_path: Path):
    """Generate a self-contained HTML report."""
    total = len(results)
    passed = sum(1 for r in results if r["overall_pass"])
    failed = total - passed
    rate = (passed * 100 // total) if total else 0

    dxc_pass = sum(1 for r in results if r.get("dxc_ok"))
    bridge_pass = sum(1 for r in results if r.get("bridge_pipeline_ok"))

    rows = []
    detail_sections = []

    for idx, r in enumerate(results):
        eid = r["eid"]
        status_class = "pass" if r["overall_pass"] else "fail"
        status_text = "PASS" if r["overall_pass"] else "FAIL"
        reduction = 0
        if r["original_lines"] > 0:
            reduction = round((1 - r["simplified_lines"] / r["original_lines"]) * 100)

        warns = len(r.get("warnings", []))
        unsup = len(r.get("unsupported", []))
        dxc_class = "pass" if r.get("dxc_ok") else "fail"
        dxc_text = "OK" if r.get("dxc_ok") else ("N/A" if not r.get("dxc_available", True) else "FAIL")
        bridge_class = "pass" if r.get("bridge_pipeline_ok") else "fail"
        bridge_text = "OK" if r.get("bridge_pipeline_ok") else "FAIL"

        rows.append(f"""
        <tr class="{status_class}" onclick="showDetail('detail-{idx}')">
          <td><span class="badge {status_class}">{status_text}</span></td>
          <td>{esc(eid)}</td>
          <td>{r['original_lines']}</td>
          <td>{r['simplified_lines']}</td>
          <td>{reduction}%</td>
          <td>{len(r.get('standalone_hlsl','').splitlines())}</td>
          <td><span class="badge {dxc_class}">{dxc_text}</span></td>
          <td><span class="badge {bridge_class}">{bridge_text}</span></td>
          <td>{warns}</td>
          <td>{r['elapsed_ms']}ms</td>
        </tr>""")

        transforms_html = ""
        for t in r.get("simplify_transforms", []):
            transforms_html += f"<span class='tag'>{t['level']}: {t['before']}→{t['after']}</span> "

        warnings_html = ""
        for w in r.get("warnings", []):
            warnings_html += f"<div class='warn-item'>{esc(w)}</div>"
        for u in r.get("unsupported", []):
            warnings_html += f"<div class='unsup-item'>{esc(u)}</div>"

        residual_html = ""
        if r.get("residual_glsl_tokens"):
            residual_html = f"<div class='residual'>残留 GLSL token: {', '.join(r['residual_glsl_tokens'])}</div>"

        dxc_detail = ""
        if r.get("dxc_ok"):
            dxc_detail = "<div class='verify-ok'>DXC 编译验证: 通过</div>"
        elif not r.get("dxc_available", True):
            dxc_detail = "<div class='verify-na'>DXC 未安装 — 跳过验证</div>"
        else:
            dxc_detail = f"<div class='verify-fail'>DXC 编译验证: 失败<pre class='code error-code'>{esc(r.get('dxc_errors','')[:3000])}</pre></div>"

        bridge_detail = ""
        if r.get("bridge_pipeline_ok"):
            bridge_detail = "<div class='verify-ok'>SPIR-V 桥接验证: 通过 (GLSL→SPIR-V→HLSL→SPIR-V)</div>"
        else:
            bridge_steps = f"GLSL→SPV: {'OK' if r.get('bridge_glsl_spv_ok') else 'FAIL'} | HLSL→SPV: {'OK' if r.get('bridge_hlsl_spv_ok') else 'FAIL'}"
            bridge_detail = f"<div class='verify-fail'>SPIR-V 桥接验证: 失败 ({bridge_steps})<pre class='code error-code'>{esc(r.get('bridge_errors','')[:3000])}</pre></div>"

        detail_sections.append(f"""
    <div id="detail-{idx}" class="detail-panel" style="display:none;">
      <h2>{esc(eid)} <span class="badge {status_class}">{status_text}</span></h2>
      <div class="meta-row">
        <span>原始: {r['original_lines']} 行</span>
        <span>简化: {r['simplified_lines']} 行 (减少 {reduction}%)</span>
        <span>耗时: {r['elapsed_ms']}ms</span>
        <span>纹理: {r['texture_count']}</span>
        <span>Params: {'有' if r['has_params'] else '无'}</span>
      </div>
      <div class="transforms">{transforms_html}</div>
      {residual_html}
      {dxc_detail}
      {bridge_detail}
      {warnings_html}
      <div class="code-compare">
        <div class="code-col">
          <h3>原始 GLSL ({r['original_lines']} 行)</h3>
          <pre class="code">{esc(r['original_source'][:8000])}</pre>
        </div>
        <div class="code-col">
          <h3>简化后 GLSL ({r['simplified_lines']} 行)</h3>
          <pre class="code">{esc(r.get('simplified_source','')[:8000])}</pre>
        </div>
      </div>
      <div class="code-compare">
        <div class="code-col">
          <h3>Standalone HLSL ({len(r.get('standalone_hlsl','').splitlines())} 行)</h3>
          <pre class="code">{esc(r.get('standalone_hlsl','')[:8000])}</pre>
        </div>
        <div class="code-col">
          <h3>UE Custom Node HLSL</h3>
          <pre class="code">{esc(r.get('ue_custom_hlsl','')[:8000])}</pre>
        </div>
      </div>
      <button class="close-btn" onclick="hideDetail('detail-{idx}')">关闭详情</button>
    </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>GLSL→HLSL 一键转换测试报告 — 蛋仔描边抓帧</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background:#0d1117; color:#c9d1d9; padding:24px; }}
  h1 {{ color:#58a6ff; margin-bottom:8px; }}
  .subtitle {{ color:#8b949e; margin-bottom:24px; }}
  .summary {{ display:flex; gap:16px; margin-bottom:24px; }}
  .summary-card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px 24px; text-align:center; }}
  .summary-card .num {{ font-size:2em; font-weight:700; }}
  .summary-card .label {{ color:#8b949e; font-size:0.9em; }}
  .pass .num {{ color:#3fb950; }}
  .fail .num {{ color:#f85149; }}
  .total .num {{ color:#58a6ff; }}
  .rate .num {{ color:#d2a8ff; }}
  table {{ width:100%; border-collapse:collapse; background:#161b22; border-radius:8px; overflow:hidden; margin-bottom:24px; }}
  th {{ background:#21262d; padding:12px 16px; text-align:left; color:#8b949e; font-weight:600; font-size:0.85em; text-transform:uppercase; }}
  td {{ padding:10px 16px; border-top:1px solid #21262d; }}
  tr {{ cursor:pointer; transition:background 0.15s; }}
  tr:hover {{ background:#1c2128; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.8em; font-weight:700; }}
  .badge.pass {{ background:#238636; color:#fff; }}
  .badge.fail {{ background:#da3633; color:#fff; }}
  .detail-panel {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:24px; margin-bottom:24px; }}
  .detail-panel h2 {{ color:#58a6ff; margin-bottom:12px; }}
  .meta-row {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:12px; }}
  .meta-row span {{ background:#21262d; padding:4px 12px; border-radius:6px; font-size:0.85em; }}
  .transforms {{ margin-bottom:12px; }}
  .tag {{ display:inline-block; background:#1f6feb33; color:#58a6ff; padding:2px 8px; border-radius:4px; font-size:0.8em; margin:2px; }}
  .warn-item {{ color:#d29922; font-size:0.85em; margin:2px 0; }}
  .warn-item::before {{ content:'⚠ '; }}
  .unsup-item {{ color:#f85149; font-size:0.85em; margin:2px 0; }}
  .unsup-item::before {{ content:'✗ '; }}
  .residual {{ color:#f85149; font-weight:600; margin:8px 0; padding:8px; background:#da363322; border-radius:6px; }}
  .code-compare {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:16px 0; }}
  .code-col h3 {{ color:#8b949e; font-size:0.85em; margin-bottom:6px; }}
  .code {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; font-family:'Cascadia Code','Fira Code',monospace; font-size:0.75em; line-height:1.5; overflow-x:auto; max-height:500px; overflow-y:auto; white-space:pre; }}
  .close-btn {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; padding:8px 20px; border-radius:6px; cursor:pointer; margin-top:12px; }}
  .close-btn:hover {{ background:#30363d; }}
  .verify-ok {{ color:#3fb950; margin:8px 0; padding:8px; background:#23863622; border-radius:6px; font-weight:600; }}
  .verify-ok::before {{ content:'✓ '; }}
  .verify-fail {{ color:#f85149; margin:8px 0; padding:8px; background:#da363322; border-radius:6px; }}
  .verify-fail::before {{ content:'✗ '; }}
  .verify-na {{ color:#8b949e; margin:8px 0; padding:8px; background:#21262d; border-radius:6px; }}
  .verify-na::before {{ content:'— '; }}
  .error-code {{ color:#f85149; font-size:0.75em; margin-top:6px; max-height:200px; }}
  .pipeline-diagram {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px; margin-bottom:24px; text-align:center; }}
  .pipeline-diagram .step {{ display:inline-block; background:#21262d; border:1px solid #30363d; border-radius:8px; padding:8px 16px; margin:4px; font-size:0.85em; }}
  .pipeline-diagram .arrow {{ color:#58a6ff; margin:0 4px; font-weight:700; }}
</style>
</head>
<body>
<h1>GLSL → HLSL 一键转换测试报告</h1>
<p class="subtitle">数据源: 蛋仔描边抓帧 DZ_ZMXT-frame5080 | 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>

<div class="pipeline-diagram">
  <span class="step">RenderDoc GLSL</span>
  <span class="arrow">→</span>
  <span class="step">L0 清理</span>
  <span class="arrow">→</span>
  <span class="step">L1 死代码</span>
  <span class="arrow">→</span>
  <span class="step">L2 常量折叠</span>
  <span class="arrow">→</span>
  <span class="step">L3 分支消除</span>
  <span class="arrow">→</span>
  <span class="step">L4 代数简化</span>
  <span class="arrow">→</span>
  <span class="step">规则引擎转换</span>
  <span class="arrow">→</span>
  <span class="step">Standalone HLSL</span>
  <span class="arrow">+</span>
  <span class="step">UE Custom HLSL</span>
  <span class="arrow">→</span>
  <span class="step">DXC 编译验证</span>
  <span class="arrow">+</span>
  <span class="step">SPIR-V 桥接闭环</span>
</div>

<div class="summary">
  <div class="summary-card total"><div class="num">{total}</div><div class="label">总 Shader 数</div></div>
  <div class="summary-card pass"><div class="num">{passed}</div><div class="label">规则转换通过</div></div>
  <div class="summary-card fail"><div class="num">{failed}</div><div class="label">规则转换失败</div></div>
  <div class="summary-card rate"><div class="num">{rate}%</div><div class="label">规则通过率</div></div>
  <div class="summary-card pass"><div class="num">{dxc_pass}</div><div class="label">DXC 编译通过</div></div>
  <div class="summary-card pass"><div class="num">{bridge_pass}</div><div class="label">SPIR-V 桥接通过</div></div>
</div>

<table>
<thead><tr>
  <th>状态</th><th>EID</th><th>原始行</th><th>简化行</th><th>减少</th><th>HLSL行</th><th>DXC</th><th>SPIR-V桥接</th><th>警告</th><th>耗时</th>
</tr></thead>
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
    print(f"Discovering shaders in: {EXPORT_ROOT}")
    shaders = discover_shaders(EXPORT_ROOT)
    print(f"Found {len(shaders)} fragment shaders\n")

    results = []
    for shader in shaders:
        print(f"  Testing {shader['eid_name']}...", end=" ", flush=True)
        r = run_test(shader)
        status = "PASS" if r["overall_pass"] else "FAIL"
        reduction = 0
        if r["original_lines"] > 0:
            reduction = round((1 - r["simplified_lines"] / r["original_lines"]) * 100)
        dxc_s = "OK" if r.get("dxc_ok") else ("N/A" if not r.get("dxc_available", True) else "FAIL")
        bridge_s = "OK" if r.get("bridge_pipeline_ok") else "FAIL"
        print(f"[{status}] {r['original_lines']}→{r['simplified_lines']} 行 (-{reduction}%) | "
              f"HLSL: {len(r.get('standalone_hlsl','').splitlines())} 行 | "
              f"DXC: {dxc_s} | 桥接: {bridge_s} | {r['elapsed_ms']}ms")
        results.append(r)

    report_path = REPORT_DIR / "real_shader_test_report.html"
    generate_html_report(results, report_path)

    total = len(results)
    passed = sum(1 for r in results if r["overall_pass"])
    dxc_ok = sum(1 for r in results if r.get("dxc_ok"))
    bridge_ok = sum(1 for r in results if r.get("bridge_pipeline_ok"))
    print(f"\n{'='*70}")
    print(f"  总计: {total} | 规则通过: {passed} | DXC编译通过: {dxc_ok} | SPIR-V桥接通过: {bridge_ok}")
    print(f"  成功率: 规则 {passed*100//total if total else 0}% | DXC {dxc_ok*100//total if total else 0}% | 桥接 {bridge_ok*100//total if total else 0}%")
    print(f"  报告已生成: {report_path}")
    print(f"{'='*70}")

    json_path = REPORT_DIR / "real_shader_test_results.json"
    json_data = []
    for r in results:
        entry = {k: v for k, v in r.items() if k not in ("original_source", "simplified_source", "standalone_hlsl", "ue_custom_hlsl")}
        entry["standalone_hlsl_lines"] = len(r.get("standalone_hlsl", "").splitlines())
        entry["ue_custom_hlsl_lines"] = len(r.get("ue_custom_hlsl", "").splitlines())
        json_data.append(entry)
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
