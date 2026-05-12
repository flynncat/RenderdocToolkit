"""Diagnose RenderDoc shader replacement: verify that replacing a shader
with its own source produces the same output."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.config as app_config

python_path = (app_config.RENDERDOC_PYTHON_PATH or "").strip()
if python_path and python_path not in sys.path:
    sys.path.insert(0, python_path)

import renderdoc as rd  # type: ignore

RDC_FILE = Path(r"G:\抓帧\蛋仔描边抓帧\DZ_ZMXT-frame5080.rdc")
EID = 203
OUTPUT_DIR = Path(r"G:\RenderdocSKillEvn\test_reports\diagnose_replace")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    cap = rd.OpenCaptureFile()
    res = cap.OpenFile(str(RDC_FILE), "", None)
    print(f"OpenFile: {res}")
    assert res == rd.ResultCode.Succeeded

    res2, ctrl = cap.OpenCapture(rd.ReplayOptions(), None)
    print(f"OpenCapture: {res2}")
    assert res2 == rd.ResultCode.Succeeded

    try:
        ctrl.SetFrameEvent(EID, True)
        pipe = ctrl.GetPipelineState()

        rt = None
        for t in list(pipe.GetOutputTargets()):
            if str(t.resource) != "ResourceId::0":
                rt = t.resource
                break
        print(f"Render target: {rt}")

        save = rd.TextureSave()
        save.resourceId = rt
        save.destType = rd.FileType.PNG
        save.alpha = rd.AlphaMapping.BlendToCheckerboard
        save.mip = 0
        save.slice.sliceIndex = 0
        save.sample.sampleIndex = 0

        bl_path = OUTPUT_DIR / "baseline.png"
        r = ctrl.SaveTexture(save, str(bl_path))
        print(f"Baseline save: {r}, exists: {bl_path.exists()}, size: {bl_path.stat().st_size if bl_path.exists() else 0}")

        stage = rd.ShaderStage.Pixel
        refl = pipe.GetShaderReflection(stage)
        print(f"Shader: {refl.resourceId}")

        orig_id = refl.resourceId
        entry = str(pipe.GetShaderEntryPoint(stage))
        print(f"Entry: {entry}")

        targets = [str(t) for t in list(ctrl.GetDisassemblyTargets(True))]
        print(f"Disassembly targets: {targets}")

        best_target = ""
        for t in targets:
            if "glsl" in t.lower():
                best_target = t
                break
        if not best_target and targets:
            best_target = targets[0]

        print(f"Selected target: {best_target}")

        exported_glsl = Path(r"G:\抓帧\蛋仔描边抓帧\DZ_ZMXT-frame5080_RenderdocDiffExport\shaders\-01_EID_203\eid_203_-_fs.glsl")
        source = exported_glsl.read_text(encoding="utf-8", errors="replace")
        print(f"Using exported GLSL: {exported_glsl}")
        print(f"Source length: {len(source)}")
        print(f"First 200 chars: {source[:200]}")

        (OUTPUT_DIR / "original_shader.glsl").write_text(source, encoding="utf-8")

        print("\n--- Testing BuildCustomShader with ORIGINAL source ---")
        flags = rd.ShaderCompileFlags()

        # Test: Simple red shader + explore replay/save mechanics
        simple_shader = """#version 420
layout(location = 0) out vec4 _entryPointOutput;
void main() {
    _entryPointOutput = vec4(1.0, 0.0, 0.0, 1.0);
}
"""
        print("Test: Simple red shader")
        source_enc = rd.ShaderEncoding.GLSL
        custom_id, errors = ctrl.BuildTargetShader(
            entry, source_enc, simple_shader.encode("utf-8"), flags, stage,
        )
        errors_str = str(errors or "")
        print(f"Custom ID: {custom_id}")
        print(f"Errors: {errors_str[:200]}")

        if str(custom_id) != "ResourceId::0":
            print("Compile succeeded! Replacing...")
            ctrl.ReplaceResource(orig_id, custom_id)
            ctrl.SetFrameEvent(EID, True)

            pipe2 = ctrl.GetPipelineState()
            rt2 = None
            for t in list(pipe2.GetOutputTargets()):
                if str(t.resource) != "ResourceId::0":
                    rt2 = t.resource
                    break
            print(f"RT after replace: {rt2}")

            # Try saving with Preserve alpha instead of checkerboard
            save2 = rd.TextureSave()
            save2.resourceId = rt2
            save2.destType = rd.FileType.PNG
            save2.alpha = rd.AlphaMapping.Preserve
            save2.mip = 0
            save2.slice.sliceIndex = 0
            save2.sample.sampleIndex = 0

            cd_path = OUTPUT_DIR / "candidate_preserve.png"
            r2 = ctrl.SaveTexture(save2, str(cd_path))
            print(f"Candidate (preserve alpha) save: {r2}, size: {cd_path.stat().st_size if cd_path.exists() else 0}")

            # Also try with BlendToCheckerboard
            save3 = rd.TextureSave()
            save3.resourceId = rt2
            save3.destType = rd.FileType.PNG
            save3.alpha = rd.AlphaMapping.BlendToCheckerboard
            save3.mip = 0
            save3.slice.sliceIndex = 0
            save3.sample.sampleIndex = 0

            cd_path2 = OUTPUT_DIR / "candidate_checkerboard.png"
            r3 = ctrl.SaveTexture(save3, str(cd_path2))
            print(f"Candidate (checkerboard) save: {r3}, size: {cd_path2.stat().st_size if cd_path2.exists() else 0}")

            # Try replaying to a later event and saving
            all_actions = list(ctrl.GetRootActions())
            last_eid = 0
            def walk(a):
                nonlocal last_eid
                eid_val = int(getattr(a, 'eventId', 0) or 0)
                if eid_val > last_eid:
                    last_eid = eid_val
                for c in list(getattr(a, 'children', []) or []):
                    walk(c)
            for a in all_actions:
                walk(a)
            print(f"\nLast event ID in capture: {last_eid}")

            ctrl.SetFrameEvent(last_eid, True)
            pipe_final = ctrl.GetPipelineState()
            rt_final = None
            for t in list(pipe_final.GetOutputTargets()):
                if str(t.resource) != "ResourceId::0":
                    rt_final = t.resource
                    break

            if rt_final is not None:
                save_final = rd.TextureSave()
                save_final.resourceId = rt  # Save ORIGINAL RT at end of frame
                save_final.destType = rd.FileType.PNG
                save_final.alpha = rd.AlphaMapping.Preserve
                save_final.mip = 0
                save_final.slice.sliceIndex = 0
                save_final.sample.sampleIndex = 0
                final_path = OUTPUT_DIR / "frame_end_with_replacement.png"
                r_final = ctrl.SaveTexture(save_final, str(final_path))
                print(f"Frame-end save (RT of EID 203): {r_final}, size: {final_path.stat().st_size if final_path.exists() else 0}")

            ctrl.RemoveReplacement(orig_id)
            ctrl.FreeCustomShader(custom_id)

            from PIL import Image
            import numpy as np
            for name, path in [("Baseline", bl_path), ("Candidate-preserve", cd_path), ("Candidate-checker", cd_path2), ("Frame-end", OUTPUT_DIR/"frame_end_with_replacement.png")]:
                if path.exists():
                    img = np.array(Image.open(str(path)))
                    print(f"{name}: shape={img.shape}, min={img.min()}, max={img.max()}, mean={img.mean():.2f}")
        else:
            print("Compile FAILED!")
            print(f"Full errors: {errors_str}")

    finally:
        ctrl.Shutdown()
        cap.Shutdown()
        rd.ShutdownReplay()


if __name__ == "__main__":
    main()
