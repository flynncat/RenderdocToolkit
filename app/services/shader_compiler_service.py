"""Wrapper for external shader compilation toolchain.

Supports:
  - glslangValidator (GLSL → SPIR-V)
  - spirv-cross      (SPIR-V → HLSL)
  - DXC / dxc        (HLSL → SPIR-V, also HLSL validation)

The service looks for executables in:
  1. external_tools/shader_compiler/
  2. System PATH
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import app.config as app_config
from app.services.subprocess_utils import hidden_subprocess_kwargs


_TOOL_DIR = app_config.RESOURCE_ROOT / "external_tools" / "shader_compiler"


@dataclass
class CompileResult:
    success: bool
    output_path: Optional[str] = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self):
        return {
            "success": self.success,
            "output_path": self.output_path,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _find_tool(name: str) -> Optional[str]:
    """Find a tool in the shader_compiler dir or PATH."""
    for ext in ("", ".exe"):
        candidate = _TOOL_DIR / f"{name}{ext}"
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    return found


def _run(cmd: list[str], timeout: float = 60) -> CompileResult:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
        return CompileResult(
            success=proc.returncode == 0,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
        )
    except FileNotFoundError as exc:
        return CompileResult(success=False, stderr=f"Tool not found: {exc}")
    except subprocess.TimeoutExpired:
        return CompileResult(success=False, stderr="Compilation timed out")
    except Exception as exc:
        return CompileResult(success=False, stderr=str(exc))


class ShaderCompilerService:

    @staticmethod
    def check_tools() -> dict:
        """Return availability of each tool."""
        return {
            "glslangValidator": _find_tool("glslangValidator") or "",
            "spirv-cross": _find_tool("spirv-cross") or "",
            "dxc": _find_tool("dxc") or "",
        }

    def glsl_to_spirv(
        self,
        glsl_source: str,
        stage: str = "frag",
        output_path: str | Path | None = None,
        target_env: str = "opengl",
    ) -> CompileResult:
        """Compile GLSL to SPIR-V binary using glslangValidator.

        *target_env*: ``"opengl"`` (default, for RenderDoc GLSL) or ``"vulkan"``.
        """
        tool = _find_tool("glslangValidator")
        if not tool:
            return CompileResult(success=False, stderr="glslangValidator not found")

        if "#version" not in glsl_source[:200]:
            glsl_source = "#version 420\n" + glsl_source

        env_flag = "-G" if target_env == "opengl" else "-V"

        with tempfile.TemporaryDirectory(prefix="glsl2spv_") as tmpdir:
            src = Path(tmpdir) / f"input.{stage}"
            src.write_text(glsl_source, encoding="utf-8")
            if output_path is None:
                output_path = Path(tmpdir) / "output.spv"
            else:
                output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            result = _run([
                tool, env_flag,
                "-S", stage,
                "-o", str(output_path),
                str(src),
            ])
            if result.success and output_path.exists():
                result.output_path = str(output_path)
            return result

    def spirv_to_hlsl(
        self,
        spirv_path: str | Path,
        output_path: str | Path | None = None,
        shader_model: str = "50",
    ) -> CompileResult:
        """Decompile SPIR-V to HLSL using spirv-cross."""
        tool = _find_tool("spirv-cross")
        if not tool:
            return CompileResult(success=False, stderr="spirv-cross not found")

        spirv_path = Path(spirv_path)
        if not spirv_path.exists():
            return CompileResult(success=False, stderr=f"SPIR-V file not found: {spirv_path}")

        if output_path is None:
            output_path = spirv_path.with_suffix(".hlsl")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = _run([
            tool,
            str(spirv_path),
            "--hlsl",
            "--hlsl-enable-compat",
            f"--shader-model", shader_model,
            "--output", str(output_path),
        ])
        if result.success and output_path.exists():
            result.output_path = str(output_path)
        return result

    def hlsl_to_spirv(
        self,
        hlsl_source: str,
        entry_point: str = "main",
        profile: str = "ps_6_0",
        output_path: str | Path | None = None,
    ) -> CompileResult:
        """Compile HLSL to SPIR-V using DXC."""
        tool = _find_tool("dxc")
        if not tool:
            return CompileResult(success=False, stderr="dxc not found")

        with tempfile.TemporaryDirectory(prefix="hlsl2spv_") as tmpdir:
            src = Path(tmpdir) / "input.hlsl"
            src.write_text(hlsl_source, encoding="utf-8")
            if output_path is None:
                output_path = Path(tmpdir) / "output.spv"
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            result = _run([
                tool,
                "-spirv",
                "-T", profile,
                "-E", entry_point,
                "-Fo", str(output_path),
                str(src),
            ])
            if result.success and output_path.exists():
                result.output_path = str(output_path)
            return result

    def validate_hlsl(
        self,
        hlsl_source: str,
        entry_point: str = "main",
        profile: str = "ps_6_0",
    ) -> CompileResult:
        """Validate HLSL via DXC without generating output."""
        tool = _find_tool("dxc")
        if not tool:
            return CompileResult(success=False, stderr="dxc not found")

        with tempfile.TemporaryDirectory(prefix="hlsl_validate_") as tmpdir:
            src = Path(tmpdir) / "input.hlsl"
            src.write_text(hlsl_source, encoding="utf-8")
            return _run([tool, "-T", profile, "-E", entry_point, str(src)])

    def glsl_to_hlsl(
        self,
        glsl_source: str,
        stage: str = "frag",
        output_dir: str | Path | None = None,
        shader_model: str = "50",
    ) -> CompileResult:
        """Full pipeline: GLSL → SPIR-V → HLSL."""
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="glsl2hlsl_"))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        spv_path = output_dir / "intermediate.spv"
        step1 = self.glsl_to_spirv(glsl_source, stage=stage, output_path=spv_path)
        if not step1.success:
            return CompileResult(
                success=False,
                stderr=f"GLSL→SPIR-V failed:\n{step1.stderr}\n{step1.stdout}",
            )

        hlsl_path = output_dir / "output.hlsl"
        step2 = self.spirv_to_hlsl(spv_path, output_path=hlsl_path, shader_model=shader_model)
        if not step2.success:
            return CompileResult(
                success=False,
                stderr=f"SPIR-V→HLSL failed:\n{step2.stderr}\n{step2.stdout}",
            )

        return CompileResult(
            success=True,
            output_path=str(hlsl_path),
            stdout=f"GLSL→SPIR-V OK, SPIR-V→HLSL OK\n{step2.stdout}",
        )
