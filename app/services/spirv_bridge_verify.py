"""SPIR-V bridge verification: compare GLSL and HLSL via intermediate SPIR-V."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

from app.services.shader_compiler_service import ShaderCompilerService, CompileResult
from app.services.pixel_diff_service import PixelDiffService, DiffResult


@dataclass
class BridgeVerifyResult:
    glsl_spv_ok: bool
    hlsl_spv_ok: bool
    glsl_spv_errors: str = ""
    hlsl_spv_errors: str = ""
    hlsl_source: str = ""
    glsl_spv_path: str = ""
    hlsl_spv_path: str = ""
    pipeline_ok: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SpirvBridgeVerify:
    """Verify GLSL→SPIR-V and HLSL→SPIR-V compilation, enabling cross-API comparison."""

    def __init__(
        self,
        compiler: ShaderCompilerService | None = None,
        pixel_diff: PixelDiffService | None = None,
    ):
        self.compiler = compiler or ShaderCompilerService()
        self.pixel_diff = pixel_diff or PixelDiffService()

    def verify_pipeline(
        self,
        *,
        glsl_source: str,
        hlsl_source: str,
        output_dir: str | Path,
        stage: str = "frag",
        hlsl_entry: str = "main_standalone",
        hlsl_profile: str = "ps_6_0",
    ) -> BridgeVerifyResult:
        """Compile both GLSL→SPIR-V and HLSL→SPIR-V and verify both succeed."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        glsl_spv = output_dir / "glsl.spv"
        hlsl_spv = output_dir / "hlsl.spv"

        r1 = self.compiler.glsl_to_spirv(glsl_source, stage=stage, output_path=glsl_spv)
        r2 = self.compiler.hlsl_to_spirv(
            hlsl_source,
            entry_point=hlsl_entry,
            profile=hlsl_profile,
            output_path=hlsl_spv,
        )

        result = BridgeVerifyResult(
            glsl_spv_ok=r1.success,
            hlsl_spv_ok=r2.success,
            glsl_spv_errors=f"{r1.stderr}\n{r1.stdout}".strip(),
            hlsl_spv_errors=f"{r2.stderr}\n{r2.stdout}".strip(),
            hlsl_source=hlsl_source,
            glsl_spv_path=r1.output_path or "",
            hlsl_spv_path=r2.output_path or "",
            pipeline_ok=r1.success and r2.success,
        )

        log = output_dir / "bridge_verify_result.json"
        log.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def full_convert_and_verify(
        self,
        *,
        glsl_source: str,
        output_dir: str | Path,
        stage: str = "frag",
        shader_model: str = "50",
    ) -> BridgeVerifyResult:
        """GLSL → SPIR-V → HLSL (via spirv-cross) → SPIR-V (via DXC).

        Validates the full round-trip toolchain.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        step1 = self.compiler.glsl_to_spirv(
            glsl_source, stage=stage, output_path=output_dir / "step1_glsl.spv"
        )
        if not step1.success:
            return BridgeVerifyResult(
                glsl_spv_ok=False, hlsl_spv_ok=False,
                glsl_spv_errors=f"{step1.stderr}\n{step1.stdout}".strip(),
                error="GLSL → SPIR-V 失败",
            )

        step2 = self.compiler.spirv_to_hlsl(
            step1.output_path,
            output_path=output_dir / "step2_cross.hlsl",
            shader_model=shader_model,
        )
        if not step2.success:
            return BridgeVerifyResult(
                glsl_spv_ok=True, hlsl_spv_ok=False,
                glsl_spv_path=step1.output_path or "",
                hlsl_spv_errors=f"{step2.stderr}\n{step2.stdout}".strip(),
                error="SPIR-V → HLSL 失败 (spirv-cross)",
            )

        hlsl_code = Path(step2.output_path).read_text(encoding="utf-8", errors="replace")
        step3 = self.compiler.hlsl_to_spirv(
            hlsl_code,
            entry_point="main",
            profile="ps_6_0" if "frag" in stage else "vs_6_0",
            output_path=output_dir / "step3_hlsl.spv",
        )

        return BridgeVerifyResult(
            glsl_spv_ok=True,
            hlsl_spv_ok=step3.success,
            glsl_spv_path=step1.output_path or "",
            hlsl_spv_path=step3.output_path or "",
            hlsl_source=hlsl_code,
            glsl_spv_errors=f"{step1.stderr}\n{step1.stdout}".strip(),
            hlsl_spv_errors=f"{step3.stderr}\n{step3.stdout}".strip(),
            pipeline_ok=step3.success,
            error="" if step3.success else "HLSL → SPIR-V 失败 (DXC)",
        )
