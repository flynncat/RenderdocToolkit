from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = Path(os.getenv("RENDERDOC_PORTABLE_OUTPUT_ROOT", r"G:\RenderdocDiffTools"))
EXE_PATH = Path(
    os.getenv(
        "RENDERDOC_PORTABLE_EXE",
        str(DEFAULT_OUTPUT_ROOT / "RenderdocDiffPortable" / "RenderdocDiffTools.exe"),
    )
)
USER_DATA = EXE_PATH.parent / "user_data"
LOG_PATH = USER_DATA / "logs" / "launcher.log"


def post_form(url: str, data: dict[str, str], timeout: int = 600) -> dict[str, Any]:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_port(timeout_seconds: int = 30) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if LOG_PATH.exists():
            text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
            for line in reversed(text.splitlines()):
                if "port=" in line:
                    return int(line.rsplit("port=", 1)[1].strip())
        time.sleep(0.3)
    raise RuntimeError("launcher.log 中未找到端口")


def wait_for_health(base_url: str, timeout_seconds: int = 30) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"健康检查超时: {last_error}")


def wait_for_perf_job(
    base_url: str,
    job_id: str,
    *,
    timeout_seconds: int = 900,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Poll ``GET /api/renderdoc-perf/jobs/{job_id}`` until the job
    reaches ``completed`` or ``failed``.

    Mirrors the SPA's polling loop after the perf-analysis endpoint was
    converted to fire-and-forget.  Raises ``RuntimeError`` on timeout or
    when the job reports ``failed``.
    """
    deadline = time.time() + timeout_seconds
    last_stage = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{base_url}/api/renderdoc-perf/jobs/{job_id}", timeout=30
            ) as response:
                detail = json.loads(response.read().decode("utf-8"))
        except Exception:
            time.sleep(poll_interval)
            continue
        metadata = detail.get("metadata") or {}
        status = metadata.get("status") or "running"
        progress = metadata.get("progress") or {}
        stage = str(progress.get("stage") or "")
        if stage and stage != last_stage:
            print(f"  [perf {job_id}] stage={stage} msg={progress.get('message', '')}")
            last_stage = stage
        if status == "completed":
            return detail
        if status == "failed":
            raise RuntimeError(
                f"perf job {job_id} failed: stage={stage} message={progress.get('message', '')}"
            )
        time.sleep(poll_interval)
    raise RuntimeError(f"perf job {job_id} timed out after {timeout_seconds}s")


def choose_rdc_files() -> tuple[Path, Path] | None:
    captures = sorted(PROJECT_ROOT.glob("*.rdc"))
    if len(captures) < 2:
        return None
    return captures[0], captures[1]


def choose_csv_file() -> Path | None:
    candidates = sorted(PROJECT_ROOT.glob("*.csv"))
    if not candidates:
        candidates = sorted((PROJECT_ROOT / "export_jobs").rglob("*.csv"))
    if not candidates:
        return None
    return candidates[0]


def cleanup_old_log() -> None:
    if LOG_PATH.exists():
        try:
            LOG_PATH.unlink()
        except PermissionError:
            pass


def stop_process_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)


def stop_existing_portable_processes() -> None:
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'RenderdocDiffTools.exe'\" | "
        f"Where-Object {{ $_.ExecutablePath -eq '{EXE_PATH}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False, capture_output=True, text=True)
    time.sleep(1)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def pick_export_pass(scan_payload: dict[str, Any]) -> dict[str, Any]:
    passes = scan_payload.get("passes") or []
    for item in passes:
        name = (item.get("display_name") or item.get("name") or "").lower()
        if "waterhair" in name or "glitter" in name:
            return item
    if passes:
        return passes[0]
    raise RuntimeError("未扫描到可导出的 pass")


def build_convert_mapping(inspect_result: dict[str, Any]) -> dict[str, str]:
    suggested = inspect_result.get("suggested_mapping") or {}
    mapping = {
        "output_format": "fbx",
        "position": str(suggested.get("position") or ""),
        "normal": str(suggested.get("normal") or ""),
        "uv0": str(suggested.get("uv0") or ""),
        "uv1": str(suggested.get("uv1") or ""),
        "uv2": str(suggested.get("uv2") or ""),
        "uv3": str(suggested.get("uv3") or ""),
        "color": str(suggested.get("color") or ""),
        "tangent": str(suggested.get("tangent") or ""),
    }
    ensure(bool(mapping["position"]), "CSV 自动识别没有给出 position 列")
    return mapping


def build_csv_batch_fixture(csv_path: Path, inspect_result: dict[str, Any]) -> tuple[str, dict[str, str]]:
    mapping = build_convert_mapping(inspect_result)
    rows = csv_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    ensure(bool(rows), "CSV 测试文件为空")
    headers = rows[0].split(",")

    temp_dir = Path(tempfile.mkdtemp(prefix="renderdoc_csv_batch_fixture_"))
    first_csv = temp_dir / csv_path.name
    first_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    replacement_targets = ("normal", "uv0", "tangent", "color", "uv1")
    modified_headers = list(headers)
    changed_fields: dict[str, str] = {}
    for field_name in replacement_targets:
        selected = str(mapping.get(field_name) or "")
        if not selected or "." not in selected:
            continue
        original_prefix = selected.rsplit(".", 1)[0]
        replacement_prefix = f"{original_prefix}_ALT"
        touched = False
        for index, header in enumerate(modified_headers):
            if header.startswith(original_prefix + "."):
                modified_headers[index] = header.replace(original_prefix, replacement_prefix, 1)
                touched = True
        if touched:
            changed_fields[field_name] = replacement_prefix

    ensure(bool(changed_fields), "未能构造出用于批量回退测试的 CSV 表头变体")
    second_csv = temp_dir / f"{csv_path.stem}_variant.csv"
    second_csv.write_text("\n".join([",".join(modified_headers), *rows[1:]]) + "\n", encoding="utf-8")
    return str(temp_dir), changed_fields


def choose_optional_fixtures() -> tuple[tuple[Path, Path] | None, Path | None]:
    captures = choose_rdc_files()
    csv_path = choose_csv_file()
    return captures, csv_path


def build_shader_convert_fixture() -> tuple[Path, Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="renderdoc_shader_convert_fixture_"))
    fragment_path = temp_dir / "sample_fs.glsl"
    vertex_path = temp_dir / "sample_vs.glsl"
    params_path = temp_dir / "sample_shader_params.json"

    fragment_path.write_text(
        "\n".join(
            [
                "#version 330",
                "out vec4 rt0_;",
                "uniform float alpha_add;",
                "uniform vec4 DiffuseColor;",
                "uniform vec4 PackedValue;",
                "uniform sampler2D sam_diffuse_0;",
                "in vec4 vertexColor;",
                "in vec4 v_texture0;",
                "// BEGIN: Generated code for built-in function emulation",
                "float f16tof32(uint val)",
                "{",
                "    uint sign = (val & 0x8000u) << 16u;",
                "    uint exponent = (val & 0x7C00u) >> 10u;",
                "    uint mantissa = val & 0x03FFu;",
                "    if (exponent == 0u) {",
                "        if (mantissa == 0u) {",
                "            return uintBitsToFloat(sign);",
                "        }",
                "        return (sign != 0u ? -1.0 : 1.0) * exp2(-14.0) * (float(mantissa) / 1024.0);",
                "    }",
                "    if (exponent == 31u) {",
                "        return uintBitsToFloat(sign | 0x7F800000u | (mantissa << 13u));",
                "    }",
                "    uint expanded = sign | ((exponent + 112u) << 23u) | (mantissa << 13u);",
                "    return uintBitsToFloat(expanded);",
                "}",
                "vec2 unpackHalf2x16_emu(uint u)",
                "{",
                "    return vec2(f16tof32(u & 0xFFFFu), f16tof32((u >> 16u) & 0xFFFFu));",
                "}",
                "// END: Generated code for built-in function emulation",
                "void main(){",
                "  vec4 local_0 = texture(sam_diffuse_0, v_texture0.xy, -1.0);",
                "  vec4 local_1 = local_0 * vertexColor;",
                "  uvec2 packed = floatBitsToUint(PackedValue.zw);",
                "  vec2 unpacked = unpackHalf2x16_emu(packed.x);",
                "  local_1.xyz = mix(local_1.xyz, DiffuseColor.xyz, 0.5);",
                "  rt0_ = vec4(local_1.xyz + unpacked.xyy, clamp(local_1.w + alpha_add + unpacked.y, 0.0, 1.0));",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    vertex_path.write_text(
        "\n".join(
            [
                "#version 330",
                "uniform mat4 WorldViewProjection;",
                "layout(location = 0) in vec4 position;",
                "layout(location = 3) in vec4 diffuse;",
                "layout(location = 6) in vec4 texcoord0;",
                "out vec4 vertexColor;",
                "out vec4 v_texture0;",
                "void main(){",
                "  gl_Position = WorldViewProjection * position;",
                "  vertexColor = diffuse;",
                "  v_texture0 = vec4(texcoord0.xy, 1.0, 0.0);",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    params_path.write_text(
        json.dumps(
            {
                "stages": {
                    "fragment": {
                        "constant_blocks": [
                            {
                                "variables": [
                                    {"name": "alpha_add", "value": 0.25},
                                    {"name": "DiffuseColor", "value": [1.0, 0.5, 0.25, 1.0]},
                                    {"name": "PackedValue", "value": [0.0, 0.0, 15360.0, 16384.0]},
                                ]
                            }
                        ]
                    },
                    "vertex": {
                        "constant_blocks": [
                            {
                                "variables": [
                                    {
                                        "name": "WorldViewProjection",
                                        "value": [
                                            [1.0, 0.0, 0.0, 0.0],
                                            [0.0, 1.0, 0.0, 0.0],
                                            [0.0, 0.0, 1.0, 0.0],
                                            [0.0, 0.0, 0.0, 1.0],
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return fragment_path, vertex_path, params_path


def build_pure_graph_fixture() -> tuple[Path, Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="renderdoc_pure_graph_fixture_"))
    fragment_path = temp_dir / "pure_fs.glsl"
    vertex_path = temp_dir / "pure_vs.glsl"
    params_path = temp_dir / "pure_shader_params.json"

    fragment_path.write_text(
        "\n".join(
            [
                "#version 330",
                "out vec4 rt0_;",
                "uniform vec4 DiffuseColor;",
                "uniform sampler2D sam_diffuse_0;",
                "in vec4 vertexColor;",
                "in vec4 v_texture0;",
                "void main(){",
                "  vec4 local_0 = texture(sam_diffuse_0, v_texture0.xy);",
                "  vec4 local_1 = local_0 * vertexColor;",
                "  local_1.xyz = mix(local_1.xyz, DiffuseColor.xyz, 0.5);",
                "  rt0_ = vec4(local_1.xyz, clamp(local_1.w, 0.0, 1.0));",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    vertex_path.write_text(
        "\n".join(
            [
                "#version 330",
                "layout(location = 3) in vec4 diffuse;",
                "layout(location = 6) in vec4 texcoord0;",
                "out vec4 vertexColor;",
                "out vec4 v_texture0;",
                "void main(){",
                "  vertexColor = diffuse;",
                "  v_texture0 = vec4(texcoord0.xy, 1.0, 0.0);",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    params_path.write_text(
        json.dumps(
            {
                "stages": {
                    "fragment": {
                        "constant_blocks": [
                            {
                                "variables": [
                                    {"name": "DiffuseColor", "value": [1.0, 0.5, 0.25, 1.0]},
                                ]
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return fragment_path, vertex_path, params_path


def build_matrix_semantic_fixture() -> tuple[Path, Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="renderdoc_matrix_semantic_fixture_"))
    fragment_path = temp_dir / "matrix_fs.glsl"
    vertex_path = temp_dir / "matrix_vs.glsl"
    params_path = temp_dir / "matrix_shader_params.json"

    fragment_path.write_text(
        "\n".join(
            [
                "#version 330",
                "out vec4 rt0_;",
                "uniform mat4 World;",
                "uniform mat4 InverseWorld;",
                "uniform mat4 ViewProjection;",
                "in vec3 position_world;",
                "void main(){",
                "  vec4 clip_ref = ViewProjection * World * vec4(position_world.xyz, 1.0);",
                "  vec4 local_ref = InverseWorld * clip_ref;",
                "  rt0_ = vec4(local_ref.xyz, 1.0);",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    vertex_path.write_text(
        "\n".join(
            [
                "#version 330",
                "uniform mat4 World;",
                "uniform mat4 InverseWorld;",
                "uniform mat4 ViewProjection;",
                "layout(location = 0) in vec4 position;",
                "out vec4 clip_ref;",
                "void main(){",
                "  clip_ref = ViewProjection * World * position;",
                "  gl_Position = clip_ref;",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    params_path.write_text(
        json.dumps(
            {
                "stages": {
                    "fragment": {"constant_blocks": [{"variables": []}]},
                    "vertex": {"constant_blocks": [{"variables": []}]},
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return fragment_path, vertex_path, params_path


def run_api_regression(base_url: str, before_rdc: Path, after_rdc: Path, csv_path: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    cmp_result = post_form(
        f"{base_url}/api/renderdoc-cmp/compare/by-path",
        {
            "base_path": str(before_rdc),
            "new_path": str(after_rdc),
            "strict_mode": "false",
            "verbose": "false",
            "renderdoc_dir": "",
            "malioc_path": "",
        },
        timeout=900,
    )
    ensure(cmp_result.get("metadata", {}).get("status") == "completed", "性能 Diff 未完成")
    ensure(bool(cmp_result.get("report_url")), "性能 Diff 缺少 HTML 报告地址")
    print("cmp_job:", cmp_result["metadata"]["job_id"], cmp_result["metadata"]["status"])
    results["cmp_job_id"] = cmp_result["metadata"]["job_id"]

    # ``/analyze[/by-path]`` was converted to fire-and-forget: it now
    # returns ``{job_id, status:"running"}`` immediately and the analysis
    # runs in the background.  We mirror the SPA's polling loop here.
    perf_first_submit = post_form(
        f"{base_url}/api/renderdoc-perf/analyze/by-path",
        {"capture_path": str(before_rdc), "renderdoc_dir": ""},
        timeout=60,
    )
    ensure(bool(perf_first_submit.get("job_id")), "首次性能分析提交未返回 job_id")
    perf_first = wait_for_perf_job(base_url, perf_first_submit["job_id"])
    rows_first = (perf_first.get("analysis") or {}).get("rows") or []
    ensure(perf_first.get("metadata", {}).get("status") == "completed", "第一次性能分析未完成")
    ensure(bool(rows_first), "第一次性能分析没有 rows")
    perf_inputs = perf_first.get("metadata", {}).get("inputs", {})
    ensure("renderdoc_source" in perf_inputs, "性能分析 metadata 缺少 renderdoc_source 字段")
    perf_progress = perf_first.get("metadata", {}).get("progress", {})
    ensure(perf_progress.get("stage") == "completed", "性能分析未写入 progress.stage=completed")
    top_first = rows_first[0]
    preview_result = post_form(
        f"{base_url}/api/renderdoc-perf/jobs/{perf_first['metadata']['job_id']}/draw-preview",
        {"eid": str(top_first.get("eid") or "")},
        timeout=300,
    )
    ensure(bool(preview_result.get("url")), "第一次性能分析未生成线框预览")
    print("perf_job_first:", perf_first["metadata"]["job_id"], len(rows_first), top_first.get("eid"))
    results["perf_job_first"] = perf_first["metadata"]["job_id"]

    perf_second_submit = post_form(
        f"{base_url}/api/renderdoc-perf/analyze/by-path",
        {"capture_path": str(after_rdc)},
        timeout=60,
    )
    ensure(bool(perf_second_submit.get("job_id")), "第二次性能分析提交未返回 job_id")
    perf_second = wait_for_perf_job(base_url, perf_second_submit["job_id"])
    rows_second = (perf_second.get("analysis") or {}).get("rows") or []
    ensure(perf_second.get("metadata", {}).get("status") == "completed", "第二次性能分析未完成")
    ensure(bool(rows_second), "第二次性能分析没有 rows")
    print("perf_job_second:", perf_second["metadata"]["job_id"], len(rows_second), rows_second[0].get("eid"))
    results["perf_job_second"] = perf_second["metadata"]["job_id"]

    scan = post_form(
        f"{base_url}/api/asset-export/scan-passes/by-path",
        {"capture_path": str(before_rdc)},
        timeout=300,
    )
    ensure(bool(scan.get("passes")), "资产导出未读取到 pass 列表")
    print("scan_passes:", len(scan["passes"]))
    chosen_pass = pick_export_pass(scan)
    results["scan_pass_count"] = len(scan["passes"])

    export_result = post_form(
        f"{base_url}/api/asset-export/jobs/by-path",
        {
            "capture_path": str(before_rdc),
            "export_scope": "single",
            "pass_id": str(chosen_pass.get("id") or ""),
            "pass_name": str(chosen_pass.get("display_name") or chosen_pass.get("name") or ""),
            "pass_start_id": "",
            "pass_start": "",
            "pass_end_id": "",
            "pass_end": "",
            "export_fbx": "true",
            "export_obj": "false",
            "texture_format": "png",
            "notes": "packaged smoke test",
        },
        timeout=900,
    )
    job_id = export_result["metadata"]["job_id"]
    ensure(export_result.get("metadata", {}).get("status") == "completed", "资产导出任务未完成")
    exported_draws = [
        draw
        for pass_item in (export_result.get("manifest") or {}).get("items", [])
        for draw in (pass_item.get("draws") or [])
    ]
    shader_draw = next(
        (
            draw
            for draw in exported_draws
            if draw.get("shader_vertex") and draw.get("shader_fragment") and draw.get("shader_params")
        ),
        None,
    )
    ensure(shader_draw is not None, "资产导出未生成 VS/FS shader 及参数文件")
    params_url = (
        f"{base_url}/api/asset-export/jobs/{job_id}/artifact?path="
        f"{urllib.parse.quote(str(shader_draw.get('shader_params') or ''))}"
    )
    with urllib.request.urlopen(params_url, timeout=60) as response:
        shader_params = json.loads(response.read().decode("utf-8"))
    ensure(bool((shader_params.get("stages") or {}).get("vertex")), "shader 参数文件缺少 vertex 阶段")
    ensure(bool((shader_params.get("stages") or {}).get("fragment")), "shader 参数文件缺少 fragment 阶段")
    print("export_job:", job_id, export_result["metadata"]["status"])
    results["export_job_id"] = job_id

    inspect_result = post_form(
        f"{base_url}/api/asset-export/csv-inspect/by-path",
        {"csv_path": str(csv_path)},
        timeout=300,
    )
    ensure(bool(inspect_result.get("headers")), "CSV 列识别失败")
    print("csv_headers:", len(inspect_result["headers"]))
    batch_csv_path, changed_fields = build_csv_batch_fixture(csv_path, inspect_result)
    print("csv_batch_fixture:", batch_csv_path, changed_fields)

    convert_result = post_form(
        f"{base_url}/api/asset-export/jobs/{job_id}/convert-csv/by-path",
        {
            "csv_path": batch_csv_path,
            **build_convert_mapping(inspect_result),
        },
        timeout=300,
    )
    ensure(convert_result.get("metadata", {}).get("status") == "completed", "CSV 转换未完成")
    manual_conversions = (convert_result.get("manifest") or {}).get("manual_conversions") or []
    ensure(len(manual_conversions) >= 2, "批量 CSV 转换未生成多文件结果")
    ensure(any(item.get("mapping_notes") for item in manual_conversions), "批量 CSV 转换未出现按文件回退说明")
    print("convert_job:", convert_result["metadata"]["job_id"], convert_result["metadata"]["status"])
    results["convert_job_id"] = convert_result["metadata"]["job_id"]

    return results


def run_shader_convert_api_smoke(base_url: str) -> dict[str, Any]:
    fragment_path, vertex_path, params_path = build_shader_convert_fixture()
    result = post_form(
        f"{base_url}/api/shader-tools/fragment-to-ue-custom/by-path",
        {
            "fragment_path": str(fragment_path),
            "vertex_path": str(vertex_path),
            "shader_params_path": str(params_path),
        },
        timeout=120,
    )
    ensure((result.get("output") or {}).get("ue_output_type") == "CMOT Float4", "shader 转换输出类型异常")
    input_names = {str(item.get("name") or "") for item in (result.get("inputs") or [])}
    ensure("sam_diffuse_0" in input_names, "shader 转换结果缺少贴图输入")
    ensure("v_texture0" in input_names, "shader 转换结果缺少 UV 输入")
    ensure("return rt0_;" in str(result.get("hlsl_code") or ""), "shader 转换结果未生成 return 语句")
    ensure("float2 unpackHalf2x16_emu(uint u)" in str(result.get("hlsl_code") or ""), "fragment hlsl 缺少自包含 unpackHalf2x16_emu helper")
    ensure("_rdt_f32_to_u32(PackedValue.zw)" in str(result.get("hlsl_code") or ""), "fragment hlsl 未将 floatBitsToUint 改写为 HLSL 版本")
    copy_package = str(result.get("copy_package") or "")
    ensure("CMOT Float4" in copy_package and "[Output]" in copy_package, "shader 完整复制包缺少输出类型")
    ensure("MaterialGraphNode_" in str(result.get("t3d_text") or ""), "shader 转换结果未生成 T3D 节点")
    ensure("MaterialExpressionCustom" in str(result.get("t3d_text") or ""), "shader T3D 缺少 Custom 节点")
    ensure("MaterialExpressionAppendVector" in str(result.get("t3d_text") or ""), "shader T3D 缺少 vec4 适配用的 AppendVector 节点")
    ensure("AdditionalOutputs(" in str(result.get("t3d_text") or ""), "shader T3D 未保留 vertex 多输出")
    ensure("\\\\r\\\\n" not in str(result.get("t3d_text") or ""), "shader T3D 的 Code 仍包含双重换行转义")
    ensure('Code="' in str(result.get("t3d_text") or "") and "\\r\\n" in str(result.get("t3d_text") or ""), "shader T3D 的 Code 缺少 UE 需要的换行转义")
    t3d_text = str(result.get("t3d_text") or "")
    ensure("vs/fs 接口直连 2 处" in str(result.get("t3d_summary") or ""), "shader T3D 摘要缺少 varying 直连统计")
    ensure('InputName="vertexColor",Input=(Expression=MaterialExpressionCustom\'' in t3d_text, "fragment vertexColor 未连接到 vertex custom 输出")
    ensure('InputName="v_texture0",Input=(Expression=MaterialExpressionCustom\'' in t3d_text, "fragment v_texture0 未连接到 vertex custom 输出")
    ensure('InputName="v_texture0",Input=(Expression=MaterialExpressionCustom\'' in t3d_text and "OutputIndex=1" in t3d_text, "fragment v_texture0 未使用 vertex custom 的 AdditionalOutput")
    ensure('ParameterName="vertexColor"' not in t3d_text, "vertexColor 不应再生成独立参数节点")
    ensure('ParameterName="v_texture0"' not in t3d_text, "v_texture0 不应再生成独立参数节点")
    ensure("MaterialExpressionVertexColor" in t3d_text, "shader T3D 未将 diffuse 识别为 UE VertexColor 节点")
    ensure('ParameterName="diffuse"' not in t3d_text, "diffuse 不应再生成普通参数节点")
    ensure('ParameterName="WorldViewProjection"' not in t3d_text, "WorldViewProjection 不应再生成普通参数节点")
    ensure('ParameterName="WorldView"' not in t3d_text, "WorldView 不应再生成普通参数节点")
    ensure('ParameterName="Projection"' not in t3d_text, "Projection 不应再生成普通参数节点")
    ensure('ParameterName="TexTransform0"' not in t3d_text, "TexTransform0 不应再生成普通参数节点")
    ensure(bool(result.get("vertex_stage")), "shader 转换结果缺少 vertex stage 摘要")
    vertex_outputs = (result.get("vertex_stage") or {}).get("outputs") or []
    ensure(len(vertex_outputs) >= 2, "vertex stage 未生成多输出信息")
    vertex_hlsl = str((result.get("vertex_stage") or {}).get("hlsl_code") or "")
    ensure(str(result.get("vertex_hlsl_code") or "") == vertex_hlsl, "顶层 vertex_hlsl_code 与 vertex stage hlsl_code 不一致")
    ensure("float4 v_texture0;" not in vertex_hlsl, "vertex hlsl 不应重复声明 AdditionalOutput v_texture0")
    ensure("float4 gl_Position;" not in vertex_hlsl, "vertex hlsl 不应重复声明 AdditionalOutput gl_Position")
    root_mapping = (result.get("vertex_stage") or {}).get("root_mapping") or {}
    ensure(bool(root_mapping.get("summary")), "vertex stage 缺少 gl_Position 根语义说明")
    ensure(bool(root_mapping.get("recommended_root_slots")), "vertex stage 缺少推荐的 UE 根槽位")
    vertex_notes = "\n".join((result.get("vertex_stage") or {}).get("notes") or [])
    ensure("WorldViewProjection" in vertex_notes and "Projection" in vertex_notes, "vertex stage 缺少标准矩阵语义识别说明")

    optional_result = post_form(
        f"{base_url}/api/shader-tools/fragment-to-ue-custom/by-path",
        {
            "fragment_path": str(fragment_path),
            "vertex_path": "",
            "shader_params_path": "missing_params.json",
        },
        timeout=120,
    )
    path_status = optional_result.get("path_status") or {}
    ensure("未提供" in str(path_status.get("vertex_path") or ""), "未提供 vertex 路径时缺少提示")
    ensure("已忽略" in str(path_status.get("shader_params_path") or ""), "缺失 shader params 路径时缺少忽略提示")
    ensure(optional_result.get("vertex_stage") is None, "未提供 vertex shader 时不应生成 vertex stage")

    pure_fragment_path, pure_vertex_path, pure_params_path = build_pure_graph_fixture()
    pure_result = post_form(
        f"{base_url}/api/shader-tools/fragment-to-ue-custom/by-path",
        {
            "fragment_path": str(pure_fragment_path),
            "vertex_path": str(pure_vertex_path),
            "shader_params_path": str(pure_params_path),
        },
        timeout=120,
    )
    pure_t3d = str(pure_result.get("pure_graph_t3d_text") or "")
    ensure(bool(pure_t3d), "纯图模式未生成 T3D 文本")
    ensure("MaterialExpressionCustom" not in pure_t3d, "纯图 T3D 不应包含 Custom 节点")
    ensure("MaterialExpressionTextureSample" in pure_t3d, "纯图 T3D 缺少 TextureSample 节点")
    ensure("MaterialExpressionLinearInterpolate" in pure_t3d, "纯图 T3D 缺少 LinearInterpolate 节点")
    ensure("MaterialExpressionMultiply" in pure_t3d, "纯图 T3D 缺少 Multiply 节点")
    ensure("MaterialExpressionClamp" in pure_t3d, "纯图 T3D 缺少 Clamp 节点")
    ensure("MaterialExpressionTextureCoordinate" in pure_t3d, "纯图 T3D 缺少 TextureCoordinate 节点")
    ensure("CustomizedUV0" in str(pure_result.get("pure_graph_summary") or ""), "纯图摘要缺少 vertex CustomizedUV0 信息")
    ensure(not (pure_result.get("pure_graph_unsupported") or []), "纯图基线样例不应出现 unsupported")

    matrix_fragment_path, matrix_vertex_path, matrix_params_path = build_matrix_semantic_fixture()
    matrix_result = post_form(
        f"{base_url}/api/shader-tools/fragment-to-ue-custom/by-path",
        {
            "fragment_path": str(matrix_fragment_path),
            "vertex_path": str(matrix_vertex_path),
            "shader_params_path": str(matrix_params_path),
        },
        timeout=120,
    )
    matrix_input_names = {str(item.get("name") or "") for item in (matrix_result.get("inputs") or [])}
    ensure("World" not in matrix_input_names, "Custom 输入清单不应再暴露 World 变换矩阵")
    ensure("InverseWorld" not in matrix_input_names, "Custom 输入清单不应再暴露 InverseWorld 变换矩阵")
    ensure("ViewProjection" not in matrix_input_names, "Custom 输入清单不应再暴露 ViewProjection 变换矩阵")
    matrix_hlsl = str(matrix_result.get("hlsl_code") or "")
    ensure("float4x4 World = GetPrimitiveData(Parameters.PrimitiveId).LocalToWorld;" in matrix_hlsl, "World 应改写为 UE 内建矩阵表达")
    ensure("float4x4 InverseWorld = GetPrimitiveData(Parameters.PrimitiveId).WorldToLocal;" in matrix_hlsl, "InverseWorld 应改写为 UE 内建矩阵表达")
    ensure("float4x4 ViewProjection = ResolvedView.TranslatedWorldToClip;" in matrix_hlsl, "ViewProjection 应改写为 UE 内建矩阵表达")
    matrix_pure_t3d = str(matrix_result.get("pure_graph_t3d_text") or "")
    ensure('ParameterName="World"' not in matrix_pure_t3d, "纯图 T3D 不应把 World 生成普通参数节点")
    ensure('ParameterName="InverseWorld"' not in matrix_pure_t3d, "纯图 T3D 不应把 InverseWorld 生成普通参数节点")
    ensure('ParameterName="ViewProjection"' not in matrix_pure_t3d, "纯图 T3D 不应把 ViewProjection 生成普通参数节点")
    pure_unsupported_text = "\n".join(matrix_result.get("pure_graph_unsupported") or [])
    ensure(("World" in pure_unsupported_text) or ("ViewProjection" in pure_unsupported_text) or ("InverseWorld" in pure_unsupported_text), "纯图矩阵语义未命中时应明确列为 unsupported")

    print("shader_convert_api: ok", len(input_names), len(vertex_outputs))
    return {
        "output_type": (result.get("output") or {}).get("ue_output_type"),
        "input_count": len(input_names),
        "vertex_output_count": len(vertex_outputs),
    }


def run_basic_api_smoke(base_url: str) -> dict[str, Any]:
    health = wait_for_health(base_url)
    ensure(bool(health.get("rdc", {}).get("ok")), "RenderDoc CLI 健康检查失败")
    ensure(bool(health.get("renderdoc_cmp", {}).get("ok")), "内置 renderdoc_cmp 健康检查失败")
    return {
        "mode": "basic",
        "health": {
            "rdc": bool(health.get("rdc", {}).get("ok")),
            "cmp": bool(health.get("renderdoc_cmp", {}).get("ok")),
        },
    }


def run_csv_only_api_regression(base_url: str, csv_path: Path) -> dict[str, Any]:
    health = wait_for_health(base_url)
    ensure(bool(health.get("renderdoc_cmp", {}).get("ok")), "内置 renderdoc_cmp 健康检查失败")
    inspect_result = post_form(
        f"{base_url}/api/asset-export/csv-inspect/by-path",
        {"csv_path": str(csv_path)},
        timeout=300,
    )
    ensure(bool(inspect_result.get("headers")), "CSV 列识别失败")
    batch_csv_path, changed_fields = build_csv_batch_fixture(csv_path, inspect_result)

    convert_result = post_form(
        f"{base_url}/api/asset-export/convert-csv/by-path",
        {
            "csv_path": batch_csv_path,
            **build_convert_mapping(inspect_result),
        },
        timeout=300,
    )
    ensure(convert_result.get("metadata", {}).get("status") == "completed", "CSV 批量转换未完成")
    manual_conversions = (convert_result.get("manifest") or {}).get("manual_conversions") or []
    ensure(len(manual_conversions) >= 2, "CSV 批量转换未生成多文件结果")
    ensure(any(item.get("mapping_notes") for item in manual_conversions), "CSV 批量转换未出现按文件回退说明")
    return {
        "mode": "csv_only",
        "changed_fields": changed_fields,
        "convert_job_id": convert_result.get("metadata", {}).get("job_id"),
        "converted_files": len(manual_conversions),
    }


async def close_setup_if_needed(page: Any) -> None:
    close_button = page.locator("#setup-close-btn")
    if await close_button.count():
        try:
            await close_button.click(timeout=1000)
            await page.wait_for_timeout(300)
        except Exception:
            pass


async def run_shader_convert_ui_smoke(page: Any) -> None:
    fragment_path, vertex_path, params_path = build_pure_graph_fixture()
    await page.click('.tab-btn[data-tab="asset-export"]')
    await page.fill("#shader-fragment-path", str(fragment_path))
    await page.fill("#shader-vertex-path", str(vertex_path))
    await page.fill("#shader-params-path", str(params_path))
    await page.click("#shader-convert-btn")
    await page.wait_for_function(
        "() => document.querySelector('#shader-output-spec')?.value?.includes('CMOT Float4')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-input-spec')?.value?.includes('sam_diffuse_0')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-hlsl-code')?.value?.includes('return rt0_;')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-vertex-hlsl-code')?.value?.includes('return vertexColor;')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-vertex-summary')?.value?.includes('vertexColor')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-pure-graph-summary')?.value?.length > 0",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-pure-graph-t3d')?.value?.includes('MaterialExpressionTextureSample')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-pure-graph-unsupported')?.value === ''",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-vertex-summary')?.value?.includes('Suggested Root Slots')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-t3d-text')?.value?.includes('MaterialExpressionCustom')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelector('#shader-params-path-hint')?.textContent?.length > 0",
        timeout=120000,
    )
    print("ui_step: shader_convert_ready")


async def run_ui_regression(base_url: str, before_rdc: Path, after_rdc: Path, csv_path: Path) -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    dialogs: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 980}, device_scale_factor=1)

        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def handle_console(message: Any) -> None:
            if message.type == "error":
                text = message.text or ""
                if "favicon" not in text.lower():
                    console_errors.append(text)

        page.on("console", handle_console)

        async def handle_dialog(dialog: Any) -> None:
            dialogs.append(dialog.message)
            await dialog.accept()

        page.on("dialog", handle_dialog)

        await page.goto(base_url, wait_until="networkidle")
        await close_setup_if_needed(page)

        for tab_name in ("perf", "cmp", "asset-export"):
            await page.click(f'.tab-btn[data-tab="{tab_name}"]')
            workspace = page.locator(f"#workspace-{tab_name}")
            await workspace.wait_for(state="visible", timeout=5000)

        await page.click('.tab-btn[data-tab="perf"]')
        await page.locator("#perf-form").wait_for(state="visible", timeout=10000)
        ensure(await page.locator("#perf-renderdoc-dir").count() > 0, "性能 Tab 缺少 RenderDoc 目录输入")

        await page.click('.tab-btn[data-tab="cmp"]')
        await page.locator("#cmp-form").wait_for(state="visible", timeout=10000)
        ensure(await page.locator("#cmp-renderdoc-dir").count() > 0, "性能 Diff Tab 缺少 RenderDoc 目录输入")

        await page.click('.tab-btn[data-tab="asset-export"]')
        # ``#asset-capture-source-path`` is now a hidden input populated by the
        # native OS file dialog, so set its value via JS instead of ``fill``.
        await page.evaluate(
            """(value) => {
                const el = document.getElementById('asset-capture-source-path');
                el.value = value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            str(before_rdc),
        )
        await page.click("#asset-pass-scan-btn")
        await page.wait_for_function(
            "() => document.querySelector('#asset-pass-name')?.options.length > 1",
            timeout=300000,
        )
        print("ui_step: pass_scan_ready")
        await page.locator("#asset-pass-name").select_option(index=1)
        await page.click("#asset-export-create-btn")
        await page.wait_for_function(
            "() => !document.querySelector('#asset-export-mapping-modal')?.classList.contains('hidden')",
            timeout=300000,
        )
        print("ui_step: mapping_modal_ready")
        await page.click("#asset-export-mapping-cancel-btn")
        await page.fill("#asset-csv-source-path", str(csv_path))
        await page.click("#asset-csv-inspect-btn")
        await page.wait_for_function(
            "() => document.querySelector('#mapping-position')?.value?.length > 0",
            timeout=300000,
        )
        print("ui_step: csv_mapping_ready")
        await page.click("#asset-csv-convert-btn")
        await page.wait_for_function(
            "() => document.querySelector('#asset-export-files')?.textContent?.includes('手工 CSV 转换')",
            timeout=300000,
        )
        print("ui_step: csv_convert_ready")
        await page.wait_for_function(
            "() => !!document.querySelector('#asset-export-open-output-btn')",
            timeout=300000,
        )
        print("ui_step: open_output_button_ready")

        await browser.close()

    ensure(not dialogs, f"UI 交互出现弹窗错误: {dialogs[:1]}")
    ensure(not page_errors, f"UI 出现未捕获异常: {page_errors[:1]}")
    actionable_console_errors = [item for item in console_errors if "TypeError" in item or "ReferenceError" in item or "Failed to fetch" in item]
    ensure(not actionable_console_errors, f"UI 控制台报错: {actionable_console_errors[:1]}")


async def run_basic_ui_smoke(base_url: str) -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    dialogs: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 980}, device_scale_factor=1)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def handle_console(message: Any) -> None:
            if message.type == "error":
                text = message.text or ""
                if "favicon" not in text.lower():
                    console_errors.append(text)

        page.on("console", handle_console)

        async def handle_dialog(dialog: Any) -> None:
            dialogs.append(dialog.message)
            await dialog.accept()

        page.on("dialog", handle_dialog)

        await page.goto(base_url, wait_until="networkidle")
        await close_setup_if_needed(page)

        for tab_name in ("perf", "cmp", "asset-export"):
            await page.click(f'.tab-btn[data-tab="{tab_name}"]')
            workspace = page.locator(f"#workspace-{tab_name}")
            await workspace.wait_for(state="visible", timeout=5000)

        await browser.close()

    ensure(not dialogs, f"UI 交互出现弹窗错误: {dialogs[:1]}")
    ensure(not page_errors, f"UI 出现未捕获异常: {page_errors[:1]}")
    actionable_console_errors = [item for item in console_errors if "TypeError" in item or "ReferenceError" in item or "Failed to fetch" in item]
    ensure(not actionable_console_errors, f"UI 控制台报错: {actionable_console_errors[:1]}")


async def run_csv_only_ui_smoke(base_url: str, csv_path: Path) -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    dialogs: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 980}, device_scale_factor=1)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def handle_console(message: Any) -> None:
            if message.type == "error":
                text = message.text or ""
                if "favicon" not in text.lower():
                    console_errors.append(text)

        page.on("console", handle_console)

        async def handle_dialog(dialog: Any) -> None:
            dialogs.append(dialog.message)
            await dialog.accept()

        page.on("dialog", handle_dialog)

        await page.goto(base_url, wait_until="networkidle")
        await close_setup_if_needed(page)

        for tab_name in ("perf", "cmp", "asset-export"):
            await page.click(f'.tab-btn[data-tab="{tab_name}"]')
            workspace = page.locator(f"#workspace-{tab_name}")
            await workspace.wait_for(state="visible", timeout=5000)

        await page.click('.tab-btn[data-tab="asset-export"]')
        await page.fill("#asset-csv-source-path", str(csv_path))
        await page.click("#asset-csv-inspect-btn")
        await page.wait_for_function(
            "() => document.querySelector('#mapping-position')?.value?.length > 0",
            timeout=300000,
        )
        await page.click("#asset-csv-convert-btn")
        await page.wait_for_function(
            "() => document.querySelector('#asset-export-files')?.textContent?.includes('手工 CSV 转换')",
            timeout=300000,
        )
        await browser.close()

    ensure(not dialogs, f"UI 交互出现弹窗错误: {dialogs[:1]}")
    ensure(not page_errors, f"UI 出现未捕获异常: {page_errors[:1]}")
    actionable_console_errors = [item for item in console_errors if "TypeError" in item or "ReferenceError" in item or "Failed to fetch" in item]
    ensure(not actionable_console_errors, f"UI 控制台报错: {actionable_console_errors[:1]}")


def main() -> None:
    if not EXE_PATH.exists():
        raise RuntimeError(f"未找到绿色包可执行文件: {EXE_PATH}")

    capture_pair, csv_path = choose_optional_fixtures()
    stop_existing_portable_processes()
    cleanup_old_log()

    launch_env = os.environ.copy()
    launch_env["RENDERDOC_PORTABLE_HEADLESS"] = "1"
    # Don't pop a browser during automated regression runs.
    launch_env["RENDERDOC_WEBUI_NO_BROWSER"] = "1"
    proc = subprocess.Popen([str(EXE_PATH)], env=launch_env)
    try:
        port = wait_for_port()
        base_url = f"http://127.0.0.1:{port}"
        health = wait_for_health(base_url)
        print(
            "health:",
            json.dumps({"rdc": health["rdc"]["ok"], "cmp": health["renderdoc_cmp"]["ok"]}, ensure_ascii=False),
        )

        if capture_pair and csv_path:
            before_rdc, after_rdc = capture_pair
            results = run_api_regression(base_url, before_rdc, after_rdc, csv_path)
            asyncio.run(run_ui_regression(base_url, before_rdc, after_rdc, csv_path))
            print("ui_regression: passed")
        elif csv_path:
            results = run_csv_only_api_regression(base_url, csv_path)
            asyncio.run(run_csv_only_ui_smoke(base_url, csv_path))
            print("ui_smoke: csv-only")
            print("fixture_notice: 未找到本地 .rdc 测试数据，已退化为 CSV 批量识别/转换专项回归")
        else:
            results = run_basic_api_smoke(base_url)
            asyncio.run(run_basic_ui_smoke(base_url))
            print("ui_smoke: basic")
            print("fixture_notice: 未找到本地 .rdc/.csv 测试数据，已退化为启动与页面基础冒烟")
        print("summary:", json.dumps(results, ensure_ascii=False))
    finally:
        stop_process_tree(proc.pid)
        time.sleep(1)


if __name__ == "__main__":
    main()
