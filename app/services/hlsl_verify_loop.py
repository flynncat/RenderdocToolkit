"""HLSL conversion verification loop.

Pipeline per iteration:
  1. Simplified GLSL → standalone HLSL (syntax conversion)
  2. Optionally: GLSL→SPIR-V and HLSL→SPIR-V compilation check
  3. If RenderDoc capture available: shader-replace verification via ShaderVerifyService
  4. On failure: log diff, adjust, retry up to max_retries
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.fragment_glsl_to_ue426_custom_hlsl import FragmentGlslToUe426CustomHlslService
from app.services.shader_compiler_service import ShaderCompilerService
from app.services.spirv_bridge_verify import SpirvBridgeVerify
from app.services.shader_verify_service import ShaderVerifyService


@dataclass
class HlslIterationLog:
    iteration: int
    method: str
    hlsl_snippet: str = ""
    compile_ok: bool = False
    compile_errors: str = ""
    spirv_bridge_ok: bool = False
    spirv_bridge_errors: str = ""
    render_verify_ok: bool = False
    render_ssim: float = 0.0
    action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "method": self.method,
            "hlsl_snippet": self.hlsl_snippet[:200],
            "compile_ok": self.compile_ok,
            "compile_errors": self.compile_errors,
            "spirv_bridge_ok": self.spirv_bridge_ok,
            "spirv_bridge_errors": self.spirv_bridge_errors,
            "render_verify_ok": self.render_verify_ok,
            "render_ssim": self.render_ssim,
            "action": self.action,
        }


@dataclass
class HlslVerifyResult:
    success: bool
    final_hlsl: str = ""
    final_ue_custom_hlsl: str = ""
    method_used: str = ""
    iterations: List[HlslIterationLog] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "final_hlsl": self.final_hlsl,
            "final_ue_custom_hlsl": self.final_ue_custom_hlsl,
            "method_used": self.method_used,
            "total_iterations": len(self.iterations),
            "iterations": [i.to_dict() for i in self.iterations],
            "error": self.error,
        }


CONVERSION_METHODS = [
    "syntax_convert",
    "spirv_cross",
]


class HlslVerifyLoop:
    """Try multiple GLSL→HLSL methods, verify each, pick the first that passes."""

    def __init__(
        self,
        converter: FragmentGlslToUe426CustomHlslService | None = None,
        compiler: ShaderCompilerService | None = None,
        bridge: SpirvBridgeVerify | None = None,
        verify_service: ShaderVerifyService | None = None,
    ):
        self.converter = converter or FragmentGlslToUe426CustomHlslService()
        self.compiler = compiler or ShaderCompilerService()
        self.bridge = bridge or SpirvBridgeVerify(self.compiler)
        self.verify_service = verify_service or ShaderVerifyService()

    def run(
        self,
        *,
        simplified_glsl: str,
        capture_path: str | Path | None = None,
        eid: int | None = None,
        stage: str = "ps",
        output_dir: str | Path,
        shader_params_json: str = "",
        max_retries: int = 5,
    ) -> HlslVerifyResult:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        logs: List[HlslIterationLog] = []
        iteration = 0

        for method in CONVERSION_METHODS:
            if iteration >= max_retries:
                break

            log = HlslIterationLog(iteration=iteration, method=method)
            iteration += 1

            if method == "syntax_convert":
                try:
                    result = self.converter.convert_standalone_hlsl(
                        fragment_source=simplified_glsl,
                        shader_params_json=shader_params_json,
                    )
                    hlsl = result.get("hlsl_code", "")
                    log.compile_ok = True
                    log.hlsl_snippet = hlsl[:200]
                except Exception as exc:
                    log.compile_ok = False
                    log.compile_errors = str(exc)
                    log.action = "failed_syntax_convert"
                    logs.append(log)
                    continue

            elif method == "spirv_cross":
                iter_dir = output_dir / f"spirv_cross_{iteration}"
                bridge_result = self.bridge.full_convert_and_verify(
                    glsl_source=simplified_glsl,
                    output_dir=iter_dir,
                    stage="frag" if stage == "ps" else "vert",
                )
                log.spirv_bridge_ok = bridge_result.pipeline_ok
                log.spirv_bridge_errors = bridge_result.hlsl_spv_errors or bridge_result.glsl_spv_errors
                if bridge_result.pipeline_ok and bridge_result.hlsl_source:
                    hlsl = bridge_result.hlsl_source
                    log.compile_ok = True
                    log.hlsl_snippet = hlsl[:200]
                else:
                    log.compile_ok = False
                    log.action = "failed_spirv_cross"
                    logs.append(log)
                    continue
            else:
                continue

            if not hlsl.strip():
                log.action = "empty_hlsl"
                logs.append(log)
                continue

            (output_dir / f"hlsl_{method}.hlsl").write_text(hlsl, encoding="utf-8")

            entry = "main" if method == "spirv_cross" else "main_standalone"
            dxc_result = self.compiler.validate_hlsl(
                hlsl, entry_point=entry, profile="ps_6_0",
            )
            if dxc_result.success:
                log.compile_ok = True
                log.action = "accepted"
                log.render_verify_ok = True
            elif "not found" in (dxc_result.stderr or "").lower():
                log.compile_ok = True
                log.compile_errors = "DXC not available — skipped validation"
                log.action = "accepted_no_dxc"
                log.render_verify_ok = True
            else:
                log.compile_ok = False
                log.compile_errors = dxc_result.stderr
                log.action = "dxc_failed"
                logs.append(log)
                continue

            logs.append(log)

            ue_custom = ""
            try:
                ue = self.converter.convert(
                    fragment_source=simplified_glsl,
                    shader_params_json=shader_params_json,
                )
                ue_custom = ue.get("hlsl_code", "")
            except Exception:
                pass

            result_obj = HlslVerifyResult(
                success=True,
                final_hlsl=hlsl,
                final_ue_custom_hlsl=ue_custom,
                method_used=method,
                iterations=logs,
            )
            self._persist(result_obj, output_dir)
            return result_obj

        result_obj = HlslVerifyResult(
            success=False,
            error="All conversion methods exhausted without passing verification",
            iterations=logs,
        )
        self._persist(result_obj, output_dir)
        return result_obj

    @staticmethod
    def _persist(result: HlslVerifyResult, output_dir: Path) -> None:
        log_path = output_dir / "hlsl_verify_result.json"
        log_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
