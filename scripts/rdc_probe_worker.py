"""Isolated subprocess worker for RenderDoc shader probe.

Usage:
  python rdc_probe_worker.py <rdc_path> <eid> <stage> <original_glsl_path> <modified_glsl_path> <output_dir> [--compile-only]

Outputs:
  <output_dir>/baseline.png  (not in compile-only mode)
  <output_dir>/candidate.png (not in compile-only mode)
  <output_dir>/result.json   { compile_ok, success, compile_errors }

Exit codes:
  0  = success (check result.json for details)
  1  = error (result.json has error field)
  -1 = crash (native abort — result.json may not exist)
"""
import json
import os
import signal
import sys
from pathlib import Path


def _write_result(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def main():
    if len(sys.argv) < 7:
        print("Usage: rdc_probe_worker.py <rdc> <eid> <stage> <orig.glsl> <mod.glsl> <outdir> [--compile-only]")
        sys.exit(1)

    rdc_path = Path(sys.argv[1])
    eid = int(sys.argv[2])
    stage = sys.argv[3]
    orig_path = Path(sys.argv[4])
    mod_path = Path(sys.argv[5])
    output_dir = Path(sys.argv[6])
    compile_only = "--compile-only" in sys.argv
    output_dir.mkdir(parents=True, exist_ok=True)

    result_file = output_dir / "result.json"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app.config as app_config

    python_path = (app_config.RENDERDOC_PYTHON_PATH or "").strip()
    if python_path and python_path not in sys.path:
        sys.path.insert(0, python_path)

    try:
        import renderdoc as rd
    except ImportError as exc:
        _write_result(result_file, {"error": f"Cannot import renderdoc: {exc}"})
        sys.exit(1)

    modified_source = mod_path.read_text(encoding="utf-8", errors="replace")

    result = {"compile_ok": False, "success": False, "compile_errors": "", "error": ""}
    baseline_path = output_dir / "baseline.png"
    candidate_path = output_dir / "candidate.png"

    controller = None
    cap = None
    custom_id = None
    original_shader_id = None

    try:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])

        cap = rd.OpenCaptureFile()
        open_result = cap.OpenFile(str(rdc_path), "", None)
        if open_result != rd.ResultCode.Succeeded:
            result["error"] = f"OpenFile failed: {open_result}"
            _write_result(result_file, result)
            sys.exit(0)

        if not cap.LocalReplaySupport():
            result["error"] = "No local replay support"
            _write_result(result_file, result)
            _safe_shutdown(cap=cap, rd_mod=rd)
            sys.exit(0)

        res, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if res != rd.ResultCode.Succeeded:
            result["error"] = f"OpenCapture failed: {res}"
            _write_result(result_file, result)
            _safe_shutdown(cap=cap, rd_mod=rd)
            sys.exit(0)

        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()

            resource_id = _find_output_target(pipe)

            if not compile_only and resource_id is not None:
                save = rd.TextureSave()
                save.resourceId = resource_id
                save.destType = rd.FileType.PNG
                save.alpha = rd.AlphaMapping.BlendToCheckerboard
                save.mip = 0
                save.slice.sliceIndex = 0
                save.sample.sampleIndex = 0
                controller.SaveTexture(save, str(baseline_path))

            _STAGE_MAP = {"vs": "Vertex", "ps": "Pixel", "gs": "Geometry", "hs": "Hull", "ds": "Domain", "cs": "Compute"}
            stage_enum = getattr(rd.ShaderStage, _STAGE_MAP.get(stage.lower(), "Pixel"))
            reflection = pipe.GetShaderReflection(stage_enum)
            if reflection is None:
                result["error"] = f"No shader reflection for {stage} at EID {eid}"
                _write_result(result_file, result)
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

            import re
            if shader_encoding == rd.ShaderEncoding.GLSL and not re.search(r"^\s*#version\s+", modified_source, re.MULTILINE):
                modified_source = "#version 420\n#extension GL_ARB_shading_language_packing : enable\n" + modified_source

            try:
                custom_id, errors = controller.BuildTargetShader(
                    entry_point, shader_encoding,
                    modified_source.encode("utf-8"), compile_flags, stage_enum,
                )
            except Exception as exc:
                result["compile_ok"] = False
                result["compile_errors"] = f"BuildTargetShader raised: {exc}"
                _write_result(result_file, result)
                return

            errors_str = str(errors or "")
            result["compile_errors"] = errors_str

            if str(custom_id) == "ResourceId::0":
                result["compile_ok"] = False
                custom_id = None
                _write_result(result_file, result)
                return

            result["compile_ok"] = True

            if compile_only:
                result["success"] = True
                result["mode"] = "compile_only"
            else:
                try:
                    controller.ReplaceResource(original_shader_id, custom_id)
                    controller.SetFrameEvent(eid, True)

                    pipe = controller.GetPipelineState()
                    resource_id = _find_output_target(pipe)

                    if resource_id is not None:
                        save = rd.TextureSave()
                        save.resourceId = resource_id
                        save.destType = rd.FileType.PNG
                        save.alpha = rd.AlphaMapping.BlendToCheckerboard
                        save.mip = 0
                        save.slice.sliceIndex = 0
                        save.sample.sampleIndex = 0
                        res = controller.SaveTexture(save, str(candidate_path))
                        result["success"] = (res == rd.ResultCode.Succeeded)
                    else:
                        result["error"] = "No render target after shader replacement"
                finally:
                    try:
                        controller.RemoveReplacement(original_shader_id)
                    except Exception:
                        pass

        finally:
            if custom_id is not None:
                try:
                    controller.FreeCustomShader(custom_id)
                except Exception:
                    pass
            _safe_shutdown(controller=controller, cap=cap, rd_mod=rd)

    except Exception as exc:
        result["error"] = str(exc)

    _write_result(result_file, result)
    sys.exit(0)


def _find_output_target(pipe):
    """Find a non-null render target or depth target."""
    for target in list(pipe.GetOutputTargets()):
        if str(target.resource) != "ResourceId::0":
            return target.resource
    depth = pipe.GetDepthTarget()
    if str(depth.resource) != "ResourceId::0":
        return depth.resource
    return None


def _safe_shutdown(*, controller=None, cap=None, rd_mod=None):
    """Shutdown RenderDoc resources, swallowing any exceptions."""
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


if __name__ == "__main__":
    main()
