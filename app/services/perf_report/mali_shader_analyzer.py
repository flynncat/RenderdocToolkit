"""Mali Offline Compiler (``malioc``) wrapper for the enhanced perf report.

Produces per-shader :class:`ShaderMaliMetrics` (work registers, ALU / LS /
varying / texture cycles, bound unit, register-spill flag) - the data behind
the reference report's "四、Shader Mali 编译器分析" table.

The parsing logic mirrors the proven implementation already shipping in
``external_tools/renderdoccmp/rdc_compare_ultimate.py`` (the ``malioc`` regexes
around its ``analyze_shader_with_mali`` method).  We re-implement it here
instead of importing so the perf-report package stays decoupled from the
external comparison script and can evolve independently.

SKELETON STATUS
---------------
``analyze(...)`` is wired end-to-end *given shader source text*, but the
producer that supplies GLSL source per unique shader is NOT part of this
package yet.

TODO(integration): the existing perf pipeline only stores instruction *counts*
(``app/services/renderdoc_perf_service.py`` -> ``_get_shader_metrics``), not
shader source.  To feed real data here we need a shader-source provider that
calls ``controller.DisassembleShader(pipe, refl, "<GLSL target>")`` during the
direct-replay session and stores the GLSL alongside ``shader_ids`` in
``perf_analysis.json``.  That touches the existing replay service, so it is
deferred to the follow-up phase.  Until then callers should treat
``ShaderMaliMetrics.available == False`` as "no data".
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from app.services.perf_report.models import (
    MALI_WORK_REGISTER_SPILL_THRESHOLD,
    ShaderMaliMetrics,
)

try:
    from app.services.subprocess_utils import hidden_subprocess_kwargs
except Exception:  # pragma: no cover - defensive: utils always present in app
    def hidden_subprocess_kwargs() -> Dict[str, object]:  # type: ignore
        return {}


# Where the bundled Mali Offline Compiler lives relative to the external
# renderdoccmd tools dir (same layout ``rdc_compare_ultimate.py`` searches).
_EXTERNAL_TOOLS_ROOT = (
    Path(__file__).resolve().parents[3]
    / "external_tools"
    / "renderdoccmp"
    / "tools"
    / "mali_offline_compiler"
)


class MaliShaderAnalyzer:
    """Run ``malioc`` on shader source and parse its metrics."""

    def __init__(self, malioc_path: str = "", *, timeout_seconds: float = 10.0) -> None:
        self._malioc_override = Path(malioc_path) if malioc_path.strip() else None
        self._timeout_seconds = timeout_seconds
        self._resolved_malioc: Optional[Path] = None
        # Cache by shader_id so a shader bound to many draws is only compiled
        # once per analysis run.
        self._cache: Dict[str, ShaderMaliMetrics] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        return self._find_malioc() is not None

    def analyze(
        self,
        shader_source: str,
        stage: str,
        *,
        shader_id: str = "",
    ) -> ShaderMaliMetrics:
        """Compile ``shader_source`` and return parsed Mali metrics.

        ``stage`` is ``"vs"``/``"vertex"`` or ``"ps"``/``"fs"``/``"fragment"``.
        Never raises: on any failure returns a metrics object with
        ``available=False`` and a human-readable ``note``.
        """
        if shader_id and shader_id in self._cache:
            return self._cache[shader_id]

        stage_norm = self._normalize_stage(stage)
        metrics = ShaderMaliMetrics(shader_id=shader_id, stage=stage_norm)

        if not (shader_source or "").strip():
            metrics.note = "无 shader 源（待接入 DisassembleShader GLSL 导出）"
            return self._remember(shader_id, metrics)

        malioc = self._find_malioc()
        if malioc is None:
            metrics.note = "未找到 malioc（Mali Offline Compiler）"
            return self._remember(shader_id, metrics)

        try:
            output = self._run_malioc(malioc, shader_source, stage_norm)
        except Exception as exc:  # noqa: BLE001 - never break the report
            metrics.note = f"malioc 执行失败: {exc}"
            return self._remember(shader_id, metrics)

        self._parse_into(output, metrics)
        metrics.available = True
        metrics.register_spill = (
            metrics.work_registers >= MALI_WORK_REGISTER_SPILL_THRESHOLD
        )
        return self._remember(shader_id, metrics)

    def analyze_many(self, shaders: List[Dict[str, str]]) -> List[ShaderMaliMetrics]:
        """Convenience batch helper.

        ``shaders`` is a list of ``{"shader_id", "stage", "source"}`` dicts.
        """
        results: List[ShaderMaliMetrics] = []
        for entry in shaders:
            results.append(
                self.analyze(
                    entry.get("source", ""),
                    entry.get("stage", ""),
                    shader_id=entry.get("shader_id", ""),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Internals (parsing regexes mirror rdc_compare_ultimate.py)
    # ------------------------------------------------------------------
    def _parse_into(self, output: str, metrics: ShaderMaliMetrics) -> None:
        work_reg = re.search(r"Work registers:\s*(\d+)", output)
        if work_reg:
            metrics.work_registers = int(work_reg.group(1))
        uni_reg = re.search(r"Uniform registers:\s*(\d+)", output)
        if uni_reg:
            metrics.uniform_registers = int(uni_reg.group(1))
        arith16 = re.search(r"16-bit arithmetic:\s*(\d+)%", output)
        if arith16:
            metrics.arithmetic_16bit = float(arith16.group(1))
        # "Total instruction cycles:   6.36   9.00   0.69   2.00   LS"
        cycles = re.search(
            r"Total instruction cycles:\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\w+)",
            output,
        )
        if cycles:
            metrics.alu_cycles = float(cycles.group(1))
            metrics.ls_cycles = float(cycles.group(2))
            metrics.varying_cycles = float(cycles.group(3))
            metrics.texture_cycles = float(cycles.group(4))
            metrics.bound_unit = cycles.group(5)

    def _run_malioc(self, malioc: Path, source: str, stage_norm: str) -> str:
        suffix = ".vert" if stage_norm == "vs" else ".frag"
        shader_code = source
        if "#version" not in shader_code:
            shader_code = "#version 320 es\n" + shader_code
        if stage_norm != "vs" and "precision" not in shader_code.lower():
            shader_code = shader_code.replace(
                "#version 320 es\n",
                "#version 320 es\nprecision highp float;\n",
            )

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, delete=False, encoding="utf-8"
            ) as handle:
                handle.write(shader_code)
                tmp_path = handle.name

            shader_flag = "--vertex" if stage_norm == "vs" else "--fragment"
            env = os.environ.copy()
            if platform.system() != "Windows":
                mali_dir = malioc.parent
                graphics_dir = mali_dir / "graphics"
                extra = (
                    f"{mali_dir}:{graphics_dir}"
                    if graphics_dir.exists()
                    else str(mali_dir)
                )
                prev = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = f"{extra}:{prev}" if prev else extra

            proc = subprocess.run(
                [str(malioc), shader_flag, tmp_path],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                env=env,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            )
            return (proc.stdout or "") + (proc.stderr or "")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _find_malioc(self) -> Optional[Path]:
        if self._resolved_malioc is not None:
            return self._resolved_malioc
        if self._malioc_override and self._malioc_override.exists():
            self._resolved_malioc = self._malioc_override
            return self._resolved_malioc

        is_windows = platform.system() == "Windows"
        platform_dir = "windows" if is_windows else "linux"
        exe_name = "malioc.exe" if is_windows else "malioc"

        candidates = [
            _EXTERNAL_TOOLS_ROOT / platform_dir / exe_name,
            _EXTERNAL_TOOLS_ROOT / exe_name,
        ]
        for candidate in candidates:
            if candidate.exists():
                self._resolved_malioc = candidate
                return candidate

        found = shutil.which("malioc") or shutil.which(exe_name)
        if found:
            self._resolved_malioc = Path(found)
            return self._resolved_malioc
        return None

    def _remember(self, shader_id: str, metrics: ShaderMaliMetrics) -> ShaderMaliMetrics:
        if shader_id:
            self._cache[shader_id] = metrics
        return metrics

    @staticmethod
    def _normalize_stage(stage: str) -> str:
        text = (stage or "").strip().lower()
        if text in {"vs", "vertex", "vert"}:
            return "vs"
        return "fs"
