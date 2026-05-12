"""Batch compile-check worker: open the .rdc once, compile multiple GLSL variants.

Usage:
  python rdc_batch_compile_worker.py <rdc_path> <eid> <stage> <manifest.json> <output_dir>

manifest.json is a list of { "index": N, "glsl_path": "..." } entries.

Output: <output_dir>/batch_results.json
"""
import json
import re
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 6:
        print("Usage: rdc_batch_compile_worker.py <rdc> <eid> <stage> <manifest.json> <outdir>")
        sys.exit(1)

    rdc_path = Path(sys.argv[1])
    eid = int(sys.argv[2])
    stage = sys.argv[3]
    manifest_path = Path(sys.argv[4])
    output_dir = Path(sys.argv[5])
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result_file = output_dir / "batch_results.json"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app.config as app_config

    python_path = (app_config.RENDERDOC_PYTHON_PATH or "").strip()
    if python_path and python_path not in sys.path:
        sys.path.insert(0, python_path)

    try:
        import renderdoc as rd
    except ImportError as exc:
        results = [{"index": e["index"], "compile_ok": False, "error": f"Cannot import renderdoc: {exc}"} for e in manifest]
        result_file.write_text(json.dumps(results))
        sys.exit(1)

    results = []

    try:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        cap = rd.OpenCaptureFile()
        open_result = cap.OpenFile(str(rdc_path), "", None)
        if open_result != rd.ResultCode.Succeeded:
            results = [{"index": e["index"], "compile_ok": False, "error": f"OpenFile failed: {open_result}"} for e in manifest]
            result_file.write_text(json.dumps(results))
            sys.exit(0)

        if not cap.LocalReplaySupport():
            results = [{"index": e["index"], "compile_ok": False, "error": "No local replay"} for e in manifest]
            result_file.write_text(json.dumps(results))
            cap.Shutdown()
            sys.exit(0)

        res, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if res != rd.ResultCode.Succeeded:
            results = [{"index": e["index"], "compile_ok": False, "error": f"OpenCapture failed: {res}"} for e in manifest]
            result_file.write_text(json.dumps(results))
            cap.Shutdown()
            sys.exit(0)

        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()

            _STAGE_MAP = {"vs": "Vertex", "ps": "Pixel", "gs": "Geometry", "hs": "Hull", "ds": "Domain", "cs": "Compute"}
            stage_enum = getattr(rd.ShaderStage, _STAGE_MAP.get(stage.lower(), "Pixel"))
            reflection = pipe.GetShaderReflection(stage_enum)
            if reflection is None:
                results = [{"index": e["index"], "compile_ok": False, "error": f"No shader at {stage}"} for e in manifest]
                result_file.write_text(json.dumps(results))
                return

            entry_point = str(pipe.GetShaderEntryPoint(stage_enum))
            compile_flags = rd.ShaderCompileFlags()
            source_encoding = rd.ShaderEncoding.GLSL

            for entry in manifest:
                idx = entry["index"]
                glsl_path = Path(entry["glsl_path"])

                if not glsl_path.exists():
                    results.append({"index": idx, "compile_ok": False, "error": "GLSL file not found"})
                    continue

                source = glsl_path.read_text(encoding="utf-8", errors="replace")
                if not re.search(r"^\s*#version\s+", source, re.MULTILINE):
                    source = "#version 420\n#extension GL_ARB_shading_language_packing : enable\n" + source

                try:
                    custom_id, errors = controller.BuildTargetShader(
                        entry_point, source_encoding, source.encode("utf-8"), compile_flags, stage_enum,
                    )
                    errors_str = str(errors or "")
                    ok = str(custom_id) != "ResourceId::0"
                    results.append({
                        "index": idx,
                        "compile_ok": ok,
                        "compile_errors": errors_str,
                    })
                except Exception as exc:
                    results.append({"index": idx, "compile_ok": False, "error": str(exc)})

        finally:
            controller.Shutdown()
            cap.Shutdown()
            rd.ShutdownReplay()

    except Exception as exc:
        remaining = [e["index"] for e in manifest if not any(r["index"] == e["index"] for r in results)]
        for idx in remaining:
            results.append({"index": idx, "compile_ok": False, "error": str(exc)})

    result_file.write_text(json.dumps(results, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
