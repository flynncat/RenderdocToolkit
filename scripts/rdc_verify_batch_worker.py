"""Batch shader verification worker: open the .rdc ONCE and verify multiple
GLSL variants (baseline screenshot + shader replace + candidate screenshot).

Usage:
  python rdc_verify_batch_worker.py <rdc_path> <eid> <stage> <manifest.json> <output_dir>

manifest.json:
  [
    { "index": 0, "glsl_path": "variant_0.glsl" },
    { "index": 1, "glsl_path": "variant_1.glsl" },
    ...
  ]

Output: <output_dir>/batch_verify_results.json
  [
    {
      "index": 0,
      "compile_ok": true/false,
      "compile_errors": "...",
      "success": true/false,
      "baseline_path": "...",
      "candidate_path": "...",
      "error": ""
    },
    ...
  ]

Per-variant screenshots are saved as:
  <output_dir>/baseline.png          (shared across all variants)
  <output_dir>/candidate_000.png
  <output_dir>/candidate_001.png
  ...
"""
import json
import re
import sys
import time
from pathlib import Path


def _write_result(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _find_output_target(pipe):
    for target in list(pipe.GetOutputTargets()):
        if str(target.resource) != "ResourceId::0":
            return target.resource
    depth = pipe.GetDepthTarget()
    if str(depth.resource) != "ResourceId::0":
        return depth.resource
    return None


def _safe_shutdown(*, controller=None, cap=None, rd_mod=None):
    if controller is not None:
        try:
            controller.Shutdown()
        except Exception:
            pass
    if cap is not None:
        try:
            cap.Shutdown()
        except Exception:
            pass
    if rd_mod is not None:
        try:
            rd_mod.ShutdownReplay()
        except Exception:
            pass


def main():
    if len(sys.argv) < 6:
        print("Usage: rdc_verify_batch_worker.py <rdc> <eid> <stage> <manifest.json> <outdir>")
        sys.exit(1)

    rdc_path = Path(sys.argv[1])
    eid = int(sys.argv[2])
    stage = sys.argv[3]
    manifest_path = Path(sys.argv[4])
    output_dir = Path(sys.argv[5])
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result_file = output_dir / "batch_verify_results.json"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app.config as app_config

    python_path = (app_config.RENDERDOC_PYTHON_PATH or "").strip()
    if python_path and python_path not in sys.path:
        sys.path.insert(0, python_path)

    try:
        import renderdoc as rd
    except ImportError as exc:
        results = [{"index": e["index"], "compile_ok": False, "success": False,
                     "error": f"Cannot import renderdoc: {exc}"} for e in manifest]
        _write_result(result_file, results)
        sys.exit(1)

    results = []

    try:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        cap = rd.OpenCaptureFile()
        open_result = cap.OpenFile(str(rdc_path), "", None)
        if open_result != rd.ResultCode.Succeeded:
            results = [{"index": e["index"], "compile_ok": False, "success": False,
                         "error": f"OpenFile failed: {open_result}"} for e in manifest]
            _write_result(result_file, results)
            sys.exit(0)

        if not cap.LocalReplaySupport():
            results = [{"index": e["index"], "compile_ok": False, "success": False,
                         "error": "No local replay"} for e in manifest]
            _write_result(result_file, results)
            _safe_shutdown(cap=cap, rd_mod=rd)
            sys.exit(0)

        res, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if res != rd.ResultCode.Succeeded:
            results = [{"index": e["index"], "compile_ok": False, "success": False,
                         "error": f"OpenCapture failed: {res}"} for e in manifest]
            _write_result(result_file, results)
            _safe_shutdown(cap=cap, rd_mod=rd)
            sys.exit(0)

        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()

            baseline_path = output_dir / "baseline.png"
            resource_id = _find_output_target(pipe)
            if resource_id is not None:
                save = rd.TextureSave()
                save.resourceId = resource_id
                save.destType = rd.FileType.PNG
                save.alpha = rd.AlphaMapping.BlendToCheckerboard
                save.mip = 0
                save.slice.sliceIndex = 0
                save.sample.sampleIndex = 0
                controller.SaveTexture(save, str(baseline_path))

            _STAGE_MAP = {"vs": "Vertex", "ps": "Pixel", "gs": "Geometry",
                          "hs": "Hull", "ds": "Domain", "cs": "Compute"}
            stage_enum = getattr(rd.ShaderStage, _STAGE_MAP.get(stage.lower(), "Pixel"))
            reflection = pipe.GetShaderReflection(stage_enum)
            if reflection is None:
                results = [{"index": e["index"], "compile_ok": False, "success": False,
                             "error": f"No shader at {stage}"} for e in manifest]
                _write_result(result_file, results)
                return

            original_shader_id = reflection.resourceId
            entry_point = str(pipe.GetShaderEntryPoint(stage_enum))
            compile_flags = rd.ShaderCompileFlags()

            supported_enc = [int(e) for e in controller.GetTargetShaderEncodings()]
            if int(rd.ShaderEncoding.GLSL) in supported_enc:
                shader_encoding = rd.ShaderEncoding.GLSL
            elif int(rd.ShaderEncoding.HLSL) in supported_enc:
                shader_encoding = rd.ShaderEncoding.HLSL
            elif int(rd.ShaderEncoding.SPIRV) in supported_enc:
                shader_encoding = rd.ShaderEncoding.SPIRV
            else:
                shader_encoding = rd.ShaderEncoding.GLSL

            for entry in manifest:
                idx = entry["index"]
                glsl_path = Path(entry["glsl_path"])
                candidate_path = output_dir / f"candidate_{idx:03d}.png"
                item_result = {
                    "index": idx,
                    "compile_ok": False,
                    "success": False,
                    "compile_errors": "",
                    "baseline_path": str(baseline_path),
                    "candidate_path": str(candidate_path),
                    "error": "",
                }

                if not glsl_path.exists():
                    item_result["error"] = "GLSL file not found"
                    results.append(item_result)
                    continue

                source = glsl_path.read_text(encoding="utf-8", errors="replace")
                if shader_encoding == rd.ShaderEncoding.GLSL and not re.search(r"^\s*#version\s+", source, re.MULTILINE):
                    source = "#version 420\n#extension GL_ARB_shading_language_packing : enable\n" + source

                custom_id = None
                try:
                    custom_id, errors = controller.BuildTargetShader(
                        entry_point, shader_encoding,
                        source.encode("utf-8"), compile_flags, stage_enum,
                    )
                    errors_str = str(errors or "")
                    item_result["compile_errors"] = errors_str

                    if str(custom_id) == "ResourceId::0":
                        item_result["compile_ok"] = False
                        custom_id = None
                        results.append(item_result)
                        continue

                    item_result["compile_ok"] = True

                    try:
                        controller.ReplaceResource(original_shader_id, custom_id)
                        controller.SetFrameEvent(eid, True)

                        pipe = controller.GetPipelineState()
                        out_target = _find_output_target(pipe)
                        if out_target is not None:
                            save = rd.TextureSave()
                            save.resourceId = out_target
                            save.destType = rd.FileType.PNG
                            save.alpha = rd.AlphaMapping.BlendToCheckerboard
                            save.mip = 0
                            save.slice.sliceIndex = 0
                            save.sample.sampleIndex = 0
                            save_res = controller.SaveTexture(save, str(candidate_path))
                            item_result["success"] = (save_res == rd.ResultCode.Succeeded)
                        else:
                            item_result["error"] = "No render target after replacement"
                    finally:
                        try:
                            controller.RemoveReplacement(original_shader_id)
                        except Exception:
                            pass
                        controller.SetFrameEvent(eid, True)

                except Exception as exc:
                    item_result["error"] = str(exc)
                finally:
                    if custom_id is not None:
                        try:
                            controller.FreeCustomShader(custom_id)
                        except Exception:
                            pass

                results.append(item_result)

        finally:
            _safe_shutdown(controller=controller, cap=cap, rd_mod=rd)

    except Exception as exc:
        remaining = [e["index"] for e in manifest
                     if not any(r["index"] == e["index"] for r in results)]
        for idx in remaining:
            results.append({"index": idx, "compile_ok": False, "success": False,
                            "error": str(exc)})

    _write_result(result_file, results)
    sys.exit(0)


if __name__ == "__main__":
    main()
