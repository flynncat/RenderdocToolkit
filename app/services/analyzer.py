from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from app.config import ANALYZER_SCRIPT
from app.services.script_runner import run_python_script_inproc


class AnalyzerService:
    def __init__(self, analyzer_script: Path | None = None) -> None:
        self.analyzer_script = analyzer_script or ANALYZER_SCRIPT

    def run_initial_analysis(
        self,
        before_file: Path,
        after_file: Path,
        pass_name: str,
        issue: str,
        out_dir: Path,
        diff_report: Optional[Path] = None,
    ) -> Dict[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        run_log = out_dir / "run_log.txt"

        script_args = [
            "--before",
            str(before_file),
            "--after",
            str(after_file),
            "--pass",
            pass_name,
            "--issue",
            issue,
            "--out-dir",
            str(out_dir),
        ]
        if diff_report:
            script_args.extend(["--diff-report", str(diff_report)])

        if getattr(sys, "frozen", False):
            returncode, combined_output = run_python_script_inproc(self.analyzer_script, script_args)
        else:
            cmd = [sys.executable, str(self.analyzer_script), *script_args]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            returncode = proc.returncode
            combined_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        run_log.write_text(combined_output, encoding="utf-8", errors="replace")

        if returncode != 0:
            raise RuntimeError(combined_output.strip() or "分析脚本执行失败")

        return {
            "analysis_md": str(out_dir / "analysis.md"),
            "analysis_json": str(out_dir / "analysis.json"),
            "raw_diff": str(out_dir / "raw_diff.txt"),
            "run_log": str(run_log),
        }
