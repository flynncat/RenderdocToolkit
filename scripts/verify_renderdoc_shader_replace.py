"""Phase 1 Step 1.1 — RenderDoc BuildCustomShader API feasibility verification.

Usage:
    python scripts/verify_renderdoc_shader_replace.py ^
        --capture "D:/captures/test.rdc" ^
        --eid 42 ^
        --renderdoc-python "C:/RenderDoc/x64"

The script will:
  1. Open the capture and navigate to the target EID.
  2. Export the original pixel-shader source and take a baseline screenshot.
  3. Attempt BuildCustomShader with the *same* source to verify API availability.
  4. ReplaceResource → re-render → take a second screenshot.
  5. Compare the two screenshots byte-for-byte.
  6. Clean up (RemoveReplacement / FreeCustomShader).

Exit codes:
  0 — API is available and identity-replacement produces identical output.
  1 — API call failed (details printed to stderr).
  2 — Screenshots differ unexpectedly.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import os
import sys
import time
from pathlib import Path


def _import_renderdoc(python_path: str):
    if python_path not in sys.path:
        sys.path.insert(0, python_path)
    import renderdoc  # type: ignore
    return renderdoc


def _find_first_draw_eid(controller, rd) -> int | None:
    """Walk the action tree and return the first real draw-call EID."""
    def _walk(action):
        flags = getattr(action, "flags", 0)
        if flags & rd.ActionFlags.Drawcall:
            return int(getattr(action, "eventId", 0))
        for child in list(getattr(action, "children", []) or []):
            found = _walk(child)
            if found:
                return found
        return None

    for root in list(controller.GetRootActions()):
        found = _walk(root)
        if found:
            return found
    return None


def _get_render_target(pipe, rd):
    """Return the first non-null colour output target, or depth target."""
    for t in list(pipe.GetOutputTargets()):
        if str(t.resource) != "ResourceId::0":
            return t.resource
    dt = pipe.GetDepthTarget()
    if str(dt.resource) != "ResourceId::0":
        return dt.resource
    return None


def _save_texture(controller, rd, resource_id, path: Path) -> bool:
    save = rd.TextureSave()
    save.resourceId = resource_id
    save.destType = rd.FileType.PNG
    save.alpha = rd.AlphaMapping.BlendToCheckerboard
    save.mip = 0
    save.slice.sliceIndex = 0
    save.sample.sampleIndex = 0
    result = controller.SaveTexture(save, str(path))
    return result == rd.ResultCode.Succeeded and path.exists()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def run(capture_path: str, eid: int | None, renderdoc_python: str) -> int:
    rd = _import_renderdoc(renderdoc_python)
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])

    cap = rd.OpenCaptureFile()
    rc = cap.OpenFile(capture_path, "", None)
    if rc != rd.ResultCode.Succeeded:
        print(f"[FAIL] OpenFile returned {rc}", file=sys.stderr)
        return 1

    if not cap.LocalReplaySupport():
        print("[FAIL] Capture cannot be replayed locally", file=sys.stderr)
        return 1

    result, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if result != rd.ResultCode.Succeeded:
        print(f"[FAIL] OpenCapture returned {result}", file=sys.stderr)
        return 1

    try:
        if eid is None:
            eid = _find_first_draw_eid(controller, rd)
            if eid is None:
                print("[FAIL] No draw call found in capture", file=sys.stderr)
                return 1
            print(f"[INFO] Auto-selected first draw call EID = {eid}")

        controller.SetFrameEvent(eid, True)
        pipe = controller.GetPipelineState()
        pipeline_obj = pipe.GetGraphicsPipelineObject()

        stage_enum = rd.ShaderStage.Pixel
        reflection = pipe.GetShaderReflection(stage_enum)
        if reflection is None:
            print(f"[FAIL] EID {eid} has no pixel-shader reflection", file=sys.stderr)
            return 1

        original_shader_id = reflection.resourceId
        entry_point = pipe.GetShaderEntryPoint(stage_enum)
        print(f"[INFO] Original PS resourceId = {original_shader_id}")
        print(f"[INFO] Entry point = {entry_point}")

        # --- Get shader source ---
        targets = [str(t) for t in list(controller.GetDisassemblyTargets(True))]
        selected = ""
        for t in targets:
            if "glsl" in t.lower() or "opengl" in t.lower():
                selected = t
                break
        if not selected and targets:
            selected = targets[0]
        print(f"[INFO] Disassembly target = {selected or '(none)'}")
        print(f"[INFO] Available targets = {targets}")

        source = controller.DisassembleShader(pipeline_obj, reflection, selected or "")
        if not source or not source.strip():
            print("[FAIL] DisassembleShader returned empty source", file=sys.stderr)
            return 1
        print(f"[INFO] Shader source length = {len(source)} chars")

        # --- Baseline screenshot ---
        rt = _get_render_target(pipe, rd)
        if rt is None:
            print("[FAIL] No render target found", file=sys.stderr)
            return 1

        out_dir = Path("scripts/_verify_output")
        out_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = out_dir / "baseline.png"
        if not _save_texture(controller, rd, rt, baseline_path):
            print("[FAIL] SaveTexture (baseline) failed", file=sys.stderr)
            return 1
        print(f"[OK]   Baseline saved: {baseline_path}")

        # --- BuildCustomShader ---
        print("[INFO] Calling BuildCustomShader with identical source...")
        compile_flags = rd.ShaderCompileFlags()
        try:
            custom_id, errors = controller.BuildCustomShader(
                entry_point, source, compile_flags, stage_enum
            )
        except AttributeError:
            print(
                "[FAIL] controller.BuildCustomShader is not available in this "
                "RenderDoc version. Fallback to standalone renderer is required.",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:
            print(f"[FAIL] BuildCustomShader raised: {exc}", file=sys.stderr)
            return 1

        if str(custom_id) == "ResourceId::0":
            print(f"[FAIL] BuildCustomShader returned null id. Errors:\n{errors}", file=sys.stderr)
            return 1
        print(f"[OK]   BuildCustomShader succeeded, custom id = {custom_id}")
        if errors:
            print(f"[WARN] Compile warnings:\n{errors}")

        # --- ReplaceResource + re-render ---
        try:
            controller.ReplaceResource(original_shader_id, custom_id)
        except AttributeError:
            print("[FAIL] controller.ReplaceResource is not available", file=sys.stderr)
            controller.FreeCustomShader(custom_id)
            return 1
        except Exception as exc:
            print(f"[FAIL] ReplaceResource raised: {exc}", file=sys.stderr)
            controller.FreeCustomShader(custom_id)
            return 1

        controller.SetFrameEvent(eid, True)
        pipe = controller.GetPipelineState()
        rt = _get_render_target(pipe, rd)
        replaced_path = out_dir / "replaced.png"
        if rt is None or not _save_texture(controller, rd, rt, replaced_path):
            print("[FAIL] SaveTexture (replaced) failed", file=sys.stderr)
            controller.RemoveReplacement(original_shader_id)
            controller.FreeCustomShader(custom_id)
            return 1
        print(f"[OK]   Replaced saved: {replaced_path}")

        # --- Cleanup ---
        controller.RemoveReplacement(original_shader_id)
        controller.FreeCustomShader(custom_id)
        print("[OK]   Cleanup done (RemoveReplacement + FreeCustomShader)")

        # --- Compare ---
        hash_a = _file_sha256(baseline_path)
        hash_b = _file_sha256(replaced_path)
        if hash_a == hash_b:
            print("[OK]   Screenshots are byte-identical — API is fully functional!")
            return 0
        else:
            size_a = baseline_path.stat().st_size
            size_b = replaced_path.stat().st_size
            print(
                f"[WARN] Screenshots differ: baseline sha256={hash_a[:16]}… "
                f"({size_a} bytes), replaced sha256={hash_b[:16]}… ({size_b} bytes)"
            )
            print(
                "       Minor differences may be acceptable due to GPU non-determinism. "
                "Run PixelDiffService for SSIM analysis."
            )
            return 2

    finally:
        controller.Shutdown()
        cap.Shutdown()
        rd.ShutdownReplay()
        gc.collect()
        time.sleep(0.15)


def main():
    parser = argparse.ArgumentParser(
        description="Verify RenderDoc BuildCustomShader / ReplaceResource API availability"
    )
    parser.add_argument("--capture", required=True, help="Path to .rdc capture file")
    parser.add_argument("--eid", type=int, default=None, help="Event ID (auto-detect first draw if omitted)")
    parser.add_argument(
        "--renderdoc-python",
        default=os.getenv("RENDERDOC_PYTHON_PATH", ""),
        help="Path to RenderDoc Python module directory",
    )
    args = parser.parse_args()

    if not args.renderdoc_python:
        print(
            "[FAIL] --renderdoc-python not set and RENDERDOC_PYTHON_PATH env var is empty",
            file=sys.stderr,
        )
        sys.exit(1)

    if not Path(args.capture).exists():
        print(f"[FAIL] Capture file not found: {args.capture}", file=sys.stderr)
        sys.exit(1)

    sys.exit(run(args.capture, args.eid, args.renderdoc_python))


if __name__ == "__main__":
    main()
