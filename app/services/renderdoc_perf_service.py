from __future__ import annotations

import json
import multiprocessing
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from app.services.perf_report_builder import PerfReportBuilder
from app.services.perf_rule_engine import PerfRuleEngine
from app.services.renderdoc_direct_replay import RenderdocDirectReplay
from app.services.renderdoc_perf_exporter import PerfExporter
from app.services.renderdoc_perf_store import RenderdocPerfStore
from app.services.subprocess_utils import hidden_subprocess_kwargs


def _select_preview_texture(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick the largest bound texture from a draw row to use as its preview.

    Prefers textures that have a known ``data_buffer_id`` (compressed formats
    upload the actual bytes to a buffer separate from the texture descriptor)
    and the largest estimated size among them.
    """
    items = row.get("texture_summary_items") or []
    if not items:
        return None
    # Two-pass: prefer items with a data_buffer_id (decodable); fall back to
    # any item with a res_id if none have a buffer.
    candidates = [it for it in items if it.get("data_buffer_id")]
    if not candidates:
        candidates = items
    best = None
    best_bytes = -1
    for it in candidates:
        if not it.get("width") or not it.get("height"):
            continue
        if not (it.get("data_buffer_id") or it.get("res_id")):
            continue
        b = int(it.get("estimated_bytes", 0))
        if b > best_bytes:
            best = it
            best_bytes = b
    return best


class RenderdocPerfService:
    COUNTER_NAMES = [
        "GPU Duration",
        "Input Vertices Read",
        "Input Primitives",
        "VS Invocations",
        "PS Invocations",
        "Samples Passed",
    ]

    def __init__(self, store: RenderdocPerfStore) -> None:
        self.store = store

    def _emit_progress(
        self,
        job_id: str,
        stage: str,
        message: str,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        """Best-effort progress write into the job's metadata.

        The SPA polls ``GET /api/renderdoc-perf/jobs/{job_id}`` once per
        second; that endpoint returns the full metadata, so writing
        ``progress`` here is the only thing the frontend needs.  Failures
        (e.g. metadata.json briefly locked) are swallowed - progress
        reporting must never break the analysis.
        """
        try:
            payload: Dict[str, Any] = {
                "stage": stage,
                "message": message,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if current is not None:
                payload["current"] = int(current)
            if total is not None:
                payload["total"] = int(total)
            self.store.update_metadata(job_id, {"progress": payload})
        except Exception:
            pass

    def create_job(self, title: str) -> Dict[str, Any]:
        return self.store.create_job(title)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return self.store.list_jobs()

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        return self.store.get_job_detail(job_id)

    def analyze_capture_isolated(self, job_id: str, capture_path: Path, renderdoc_dir: str = "") -> Dict[str, Any]:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_perf_worker_entry,
            args=(str(self.store.session_root), job_id, str(capture_path), child_conn, renderdoc_dir),
            daemon=False,
        )
        process.start()
        child_conn.close()
        process.join()
        result: Dict[str, Any] | None = None
        if parent_conn.poll():
            result = parent_conn.recv()
        parent_conn.close()

        if process.exitcode not in (0, None):
            self._emit_progress(
                job_id,
                "failed",
                f"性能分析子进程异常退出，exit_code={process.exitcode}",
            )
            self.store.update_metadata(job_id, {"status": "failed"})
            raise RuntimeError(f"性能分析子进程异常退出，exit_code={process.exitcode}")
        if result and not result.get("ok"):
            err_text = str(result.get("error") or "性能分析失败")
            self._emit_progress(job_id, "failed", err_text)
            raise RuntimeError(err_text)
        return self.store.get_job_detail(job_id)

    def analyze_capture(self, job_id: str, capture_path: Path, renderdoc_dir: str = "") -> Dict[str, Any]:
        from app.services.renderdoc_runtime_resolver import (
            resolve_renderdoc_runtime,
            capture_needs_foreign_renderdoc,
        )
        self._emit_progress(job_id, "init", "正在解析 RenderDoc 运行时…")
        rd_ctx = resolve_renderdoc_runtime(renderdoc_dir)

        if capture_needs_foreign_renderdoc(capture_path):
            if rd_ctx.renderdoc_cmd_path:
                return self._analyze_capture_via_xml(job_id, capture_path, rd_ctx)
            raise RuntimeError(
                "当前 capture 文件格式与已安装的 RenderDoc 不兼容，"
                "且未指定包含 renderdoccmd 的自定义 RenderDoc 目录。"
            )

        job_dir = self.store.job_path(job_id)
        preview_dir = job_dir / "artifacts" / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        run_log_lines = [
            f"[capture] {capture_path}",
            f"[renderdoc] dir={rd_ctx.renderdoc_dir} python={rd_ctx.renderdoc_python_path} source={rd_ctx.source}",
        ]

        # Draw enumeration and counters both come straight from the RenderDoc
        # Python API inside the replay session below.  This removes the old
        # dependency on the external ``rdc draws --json`` CLI (a fixed, often
        # stale RenderDoc build) so the whole perf path honours the
        # user-selected RenderDoc install and supports the same capture
        # versions that install can open.
        counter_map: Dict[str, Dict[str, float]] = {}

        shader_cache: Dict[str, Dict[str, Any]] = {}
        self._emit_progress(job_id, "replay_open", "正在打开 RenderDoc capture（直连回放）…")
        with RenderdocDirectReplay(capture_path, renderdoc_python_path=rd_ctx.renderdoc_python_path) as replay:
            capture_info = replay.get_capture_metadata()
            texture_desc_map = replay.get_texture_description_map()
            self._emit_progress(job_id, "load_draws", "正在枚举 draw 列表（直连 RenderDoc API）…")
            draw_rows = replay.list_draws()
            run_log_lines.append(f"[draws] enumerated={len(draw_rows)} via direct RenderDoc API")
            self._emit_progress(job_id, "fetch_counters", f"正在批量采集 GPU counter ({len(self.COUNTER_NAMES)} 个)…")
            counter_map = replay.fetch_counter_map(self.COUNTER_NAMES)
            missing_counters = [
                name for name in self.COUNTER_NAMES
                if not any(name in values for values in counter_map.values())
            ]
            if missing_counters:
                run_log_lines.append(
                    f"[counters] missing from backend: {missing_counters} (相关字段会回退为 0)"
                )
            # Opt-2: pre-fetch ``GetDisassemblyTargets`` once per analysis
            # instead of once per draw inside ``_get_shader_metrics``.
            try:
                disassembly_targets_raw = list(replay.controller.GetDisassemblyTargets(True))
            except Exception:
                disassembly_targets_raw = []
            available_disassembly_targets = [
                self._stringify(item) for item in disassembly_targets_raw if self._stringify(item)
            ]
            action_map = self._collect_action_map(replay)
            total_draws_to_build = len(draw_rows)
            self._emit_progress(
                job_id,
                "build_rows",
                f"正在分析 draw 0/{total_draws_to_build}…",
                current=0,
                total=total_draws_to_build,
            )

            def _build_progress(current: int, total: int, last_eid: str) -> None:
                self._emit_progress(
                    job_id,
                    "build_rows",
                    f"正在分析 draw {current}/{total}（当前 EID {last_eid}）",
                    current=current,
                    total=total,
                )

            rows = self._build_rows(
                replay=replay,
                draw_rows=draw_rows,
                counter_map=counter_map,
                action_map=action_map,
                texture_desc_map=texture_desc_map,
                run_log_lines=run_log_lines,
                shader_cache=shader_cache,
                available_targets=available_disassembly_targets,
                progress_callback=_build_progress,
            )
            preview_total = min(len(rows), self._DIRECT_PREVIEW_HARD_CAP) if rows else 0
            self._emit_progress(
                job_id,
                "previews",
                f"正在生成线框预览 0/{preview_total}…",
                current=0,
                total=preview_total,
            )

            def _preview_progress(current: int, total: int, last_eid: str) -> None:
                self._emit_progress(
                    job_id,
                    "previews",
                    f"正在生成线框预览 {current}/{total}（当前 EID {last_eid}）",
                    current=current,
                    total=total,
                )

            self._populate_initial_draw_previews(
                replay=replay,
                job_id=job_id,
                preview_dir=preview_dir,
                rows=rows,
                run_log_lines=run_log_lines,
                progress_callback=_preview_progress,
            )

        self._emit_progress(job_id, "report", "正在生成性能诊断报告…")
        overview = self._build_overview(rows)
        pass_chart = self._build_pass_chart(rows)
        hotspot_hints = self._build_hotspot_hints(pass_chart, rows)
        warnings = self._build_warnings(capture_info)

        # Direct replay used to leave ``analysis_features`` empty which made
        # the exporter fall back to ``analysis_mode="xml_fallback"``.
        # Surface the disassembly targets we actually ended up using so
        # downstream tools (and humans) can tell at a glance why a given
        # capture's instruction counts came back the way they did.
        disassembly_targets_seen: List[str] = []
        any_estimated = False
        for entry in shader_cache.values():
            tgt = self._stringify(entry.get("disassembly_target"))
            if tgt and tgt not in disassembly_targets_seen:
                disassembly_targets_seen.append(tgt)
            if entry.get("estimated"):
                any_estimated = True
        # Which GPU pipeline-statistics counters the replay backend actually
        # provided.  Desktop replay of a mobile GLES capture typically only
        # exposes ``GPU Duration`` (everything else comes back missing), so
        # downstream display/sorting must avoid the all-zero columns.
        missing_counter_names = list(missing_counters)
        counter_dependent = {
            "ps_invocations": "PS Invocations",
            "vs_invocations": "VS Invocations",
            "vertices_read": "Input Vertices Read",
            "input_primitives": "Input Primitives",
            "samples_passed": "Samples Passed",
        }
        # A field is "valid" only if its backing counter was provided.
        unavailable_fields = [
            field_id
            for field_id, counter_name in counter_dependent.items()
            if counter_name in missing_counter_names
        ]
        # Coverage + stable-sort are derived from PS Invocations, so they are
        # only meaningful when that counter exists.
        if "PS Invocations" in missing_counter_names:
            unavailable_fields.extend(
                ["screen_coverage_percent", "stable_sort_score", "instruction_coverage_score"]
            )
        counters_available = not unavailable_fields
        analysis_features = {
            "analysis_mode": "direct_replay",
            "instruction_count_estimated": any_estimated,
            "instruction_count_disassembly_targets": disassembly_targets_seen,
            "missing_counters": missing_counter_names,
            "counters_available": counters_available,
            "unavailable_fields": sorted(set(unavailable_fields)),
        }
        run_log_lines.append(
            f"[shader] disassembly_targets={disassembly_targets_seen or '[]'} "
            f"estimated={any_estimated}"
        )

        # Mali Offline Compiler per-shader analysis (Work Reg / ALU / LS /
        # bound / register-spill).  Runs on the GLSL source we extracted into
        # the shader cache; best-effort and skipped silently if malioc or the
        # source is unavailable.  This is the data behind the enhanced
        # report's "Shader Mali 编译器分析" section.
        self._emit_progress(job_id, "mali", "正在用 Mali 编译器分析 shader…")
        shader_mali_metrics = self._run_mali_shader_analysis(shader_cache, run_log_lines)

        analysis = {
            "capture_name": capture_path.name,
            "capture_path": str(capture_path),
            "capture_info": capture_info,
            "overview": overview,
            "warnings": warnings,
            "analysis_mode": "direct_replay",
            "analysis_features": analysis_features,
            "sort_fields": self._build_sort_fields(unavailable_fields),
            "rows": rows,
            "pass_chart": pass_chart,
            "hotspot_hints": hotspot_hints,
            "shader_mali_metrics": shader_mali_metrics,
        }

        self.store.write_json_artifact(job_id, "artifacts/perf_analysis.json", analysis)
        report_summary = self._generate_report_artifacts(
            job_id=job_id,
            analysis=analysis,
            run_log_lines=run_log_lines,
        )
        self.store.write_text_artifact(job_id, "artifacts/perf_run_log.txt", "\n".join(run_log_lines) + "\n")
        self._emit_progress(
            job_id,
            "completed",
            f"分析完成，共处理 {len(rows)} 个 draw。",
            current=len(rows),
            total=len(rows),
        )
        metadata = self.store.update_metadata(
            job_id,
            {
                "status": "completed",
                "inputs": {
                    "capture_file": str(capture_path),
                    "renderdoc_dir_requested": renderdoc_dir,
                    "renderdoc_dir_resolved": rd_ctx.renderdoc_dir,
                    "renderdoc_python_path": rd_ctx.renderdoc_python_path,
                    "renderdoc_source": rd_ctx.source,
                },
                "summary": {
                    "row_count": len(rows),
                    "total_gpu_duration_ms": overview["total_gpu_duration_ms"],
                    "hottest_pass": pass_chart[0]["name"] if pass_chart else "",
                    **report_summary,
                },
            },
        )
        detail = self.store.get_job_detail(job_id)
        detail["metadata"] = metadata
        return detail

    def _generate_report_artifacts(
        self,
        *,
        job_id: str,
        analysis: Dict[str, Any],
        run_log_lines: List[str],
    ) -> Dict[str, Any]:
        """Run rule engine + exporter + report builder and persist artifacts.

        Failures here are non-fatal: the perf row table is the primary
        deliverable, and we don't want a bug in the rule engine to cancel a
        completed analysis.  Any failure is logged into the per-job run log
        and surfaced in metadata.summary as ``report_generation_error``.
        """
        try:
            capture_name = str(analysis.get("capture_name") or "")
            capture_id = Path(capture_name).stem or capture_name or job_id
            rows = list(analysis.get("rows") or [])
            overview = dict(analysis.get("overview") or {})
            pass_chart = list(analysis.get("pass_chart") or [])
            capture_info = dict(analysis.get("capture_info") or {})

            engine = PerfRuleEngine()
            findings = engine.analyze(
                rows=rows,
                overview=overview,
                pass_chart=pass_chart,
                capture_info=capture_info,
                capture_name=capture_name,
            )
            findings_serialised = [f.to_dict() for f in findings]
            self.store.write_json_artifact(
                job_id, "artifacts/findings.json", findings_serialised,
            )

            builder = PerfReportBuilder()
            report = builder.build(
                analysis=analysis,
                findings=findings,
                capture_name=capture_name,
                job_id=job_id,
                exports_relpath="exports",
            )
            self.store.write_text_artifact(
                job_id, "artifacts/perf_report.md", report.md,
            )
            self.store.write_text_artifact(
                job_id, "artifacts/perf_report.html", report.html,
            )

            exporter = PerfExporter()
            out_dir = self.store.job_path(job_id) / "artifacts" / "exports"
            exporter.write_all(
                analysis=analysis,
                out_dir=out_dir,
                findings=findings_serialised,
                capture_id=capture_id,
                report_md_relpath="artifacts/perf_report.md",
                report_html_relpath="artifacts/perf_report.html",
            )

            # Enhanced report (reference-report layout: business category
            # breakdown, Mali shader table, texture audit, optimisation
            # priorities).  Best-effort: a failure here must not invalidate the
            # primary report above.
            try:
                from app.services.perf_report import EnhancedReportBuilder

                enhanced_builder = EnhancedReportBuilder()
                enhanced_md = enhanced_builder.build_from_analysis(analysis, findings_serialised)
                enhanced_html = enhanced_builder.render_html(
                    enhanced_builder.assemble_from_analysis(analysis, findings_serialised)
                )
                self.store.write_text_artifact(
                    job_id, "artifacts/perf_report_enhanced.md", enhanced_md,
                )
                self.store.write_text_artifact(
                    job_id, "artifacts/perf_report_enhanced.html", enhanced_html,
                )
                run_log_lines.append(
                    "[report] enhanced=artifacts/perf_report_enhanced.md/html"
                )
            except Exception as enh_exc:
                run_log_lines.append(f"[report] enhanced FAILED: {enh_exc}")

            severity_counts = {"high": 0, "med": 0, "low": 0}
            for finding in findings_serialised:
                sev = str(finding.get("severity") or "").lower()
                if sev in severity_counts:
                    severity_counts[sev] += 1
            run_log_lines.append(
                f"[report] findings={len(findings)} "
                f"high={severity_counts['high']} med={severity_counts['med']} low={severity_counts['low']} "
                f"md=artifacts/perf_report.md html=artifacts/perf_report.html exports=artifacts/exports/"
            )
            return {
                "finding_count_total": len(findings),
                "finding_count_by_severity": severity_counts,
                "report_md_path": "artifacts/perf_report.md",
                "report_html_path": "artifacts/perf_report.html",
                "report_enhanced_md_path": "artifacts/perf_report_enhanced.md",
                "report_enhanced_html_path": "artifacts/perf_report_enhanced.html",
                "exports_dir": "artifacts/exports",
            }
        except Exception as exc:
            run_log_lines.append(f"[report] FAILED: {exc}")
            return {
                "finding_count_total": 0,
                "finding_count_by_severity": {"high": 0, "med": 0, "low": 0},
                "report_generation_error": str(exc),
            }

    def generate_draw_preview(self, job_id: str, eid: str) -> Dict[str, str]:
        detail = self.store.get_job_detail(job_id)
        capture_path = self._resolve_capture_path(job_id, detail)
        preview_dir = self.store.job_path(job_id) / "artifacts" / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        rt_output_path = preview_dir / f"rt_{eid}.png"
        wf_output_path = preview_dir / f"wireframe_{eid}.png"

        # Re-render iff at least one of the two PNGs is missing - this way
        # an on-demand request always tops up whatever the eager pass might
        # have skipped (e.g. RT was OK but overlay came back blank).
        if not (rt_output_path.exists() and wf_output_path.exists()):
            inputs = (detail.get("metadata") or {}).get("inputs", {})
            renderdoc_dir = self._stringify(inputs.get("renderdoc_dir_requested"))
            ctx = multiprocessing.get_context("spawn")
            parent_conn, child_conn = ctx.Pipe(duplex=False)
            process = ctx.Process(
                target=_preview_worker_entry,
                args=(
                    str(capture_path),
                    eid,
                    str(rt_output_path),
                    str(wf_output_path),
                    renderdoc_dir,
                    child_conn,
                ),
                daemon=False,
            )
            process.start()
            child_conn.close()
            process.join()
            result: Dict[str, Any] | None = None
            if parent_conn.poll():
                result = parent_conn.recv()
            parent_conn.close()
            if process.exitcode not in (0, None):
                raise RuntimeError(f"预览子进程异常退出，exit_code={process.exitcode}")
            if result and not result.get("ok"):
                raise RuntimeError(str(result.get("error") or f"无法生成 EID {eid} 的预览"))
            if not (rt_output_path.exists() or wf_output_path.exists()):
                raise RuntimeError(f"无法生成 EID {eid} 的预览")

        rt_url = ""
        if rt_output_path.exists():
            rt_rel = rt_output_path.relative_to(self.store.job_path(job_id)).as_posix()
            rt_url = f"/perf-session-files/{job_id}/{rt_rel}"
        wf_url = ""
        if wf_output_path.exists():
            wf_rel = wf_output_path.relative_to(self.store.job_path(job_id)).as_posix()
            wf_url = f"/perf-session-files/{job_id}/{wf_rel}"

        primary_url = rt_url or wf_url
        primary_kind = "rt_replay" if rt_url else "wireframe_overlay"

        analysis = detail.get("analysis") or {}
        rows = analysis.get("rows") or []
        changed = False
        for row in rows:
            if self._stringify(row.get("eid")) == self._stringify(eid):
                row["draw_preview_url"] = primary_url
                row["draw_preview_kind"] = primary_kind
                if wf_url:
                    row["draw_preview_overlay_url"] = wf_url
                    row["draw_preview_overlay_kind"] = "wireframe"
                changed = True
                break
        if changed:
            self.store.write_json_artifact(job_id, "artifacts/perf_analysis.json", analysis)

        return {
            "eid": self._stringify(eid),
            "url": primary_url,
            "kind": primary_kind,
            "overlay_url": wf_url,
            "overlay_kind": "wireframe" if wf_url else "",
        }

    def _analyze_capture_via_xml(
        self,
        job_id: str,
        capture_path: Path,
        rd_ctx: Any,
    ) -> Dict[str, Any]:
        """Fallback path: convert the capture to XML (and optionally Chrome
        JSON for timing) with the task-specific ``renderdoccmd`` and extract
        analysis from the structured data.

        After XML analysis we *also* try to drive a real GPU replay through
        the user's ``qrenderdoc.exe`` (the ``QRenderdocScriptBackend``).
        When that succeeds, we upgrade per-draw previews from
        "biggest bound texture decoded from XML" to "actual replayed RT
        PNG", which is what the user really wants.
        """
        from app.services.renderdoc_runtime_resolver import (
            convert_capture_to_xml,
            convert_capture_to_zip_xml,
            convert_capture_to_chrome_json,
            extract_capture_thumbnail,
        )
        from app.services.renderdoc_xml_analyzer import analyze_capture_xml

        job_dir = self.store.job_path(job_id)
        work_dir = job_dir / "workdir"
        work_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = job_dir / "artifacts" / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)

        self._emit_progress(
            job_id,
            "convert",
            "正在用 renderdoccmd 把 capture 转成 XML（capture 较大时可能 1-3 分钟）…",
        )
        # Prefer zip.xml (XML + companion .zip with raw resource buffers) so
        # we can lazily generate per-draw texture thumbnails later.  Fall back
        # to pure xml if the conversion fails (e.g. very old renderdoccmd).
        zip_xml_path = convert_capture_to_zip_xml(
            capture_path, rd_ctx.renderdoc_cmd_path, work_dir,
        )
        if zip_xml_path is not None:
            xml_path = zip_xml_path
            zip_companion = work_dir / "capture.zip"
        else:
            xml_path = convert_capture_to_xml(capture_path, rd_ctx.renderdoc_cmd_path, work_dir)
            zip_companion = None
        if xml_path is None:
            raise RuntimeError(
                f"无法用自定义 renderdoccmd 转换 capture 为 XML。"
                f"renderdoccmd={rd_ctx.renderdoc_cmd_path}"
            )

        # Best-effort: extract chrome.json for CPU-side timings and thumbnail
        # for capture preview.  Either failing is non-fatal.
        chrome_path = convert_capture_to_chrome_json(
            capture_path, rd_ctx.renderdoc_cmd_path, work_dir,
        )
        thumbnail_path = extract_capture_thumbnail(
            capture_path, rd_ctx.renderdoc_cmd_path, preview_dir,
        )

        self._emit_progress(job_id, "xml_parse", "正在解析 XML（提取 draw / counter / texture 信息）…")
        analysis = analyze_capture_xml(xml_path, chrome_json_path=chrome_path)
        analysis["capture_path"] = str(capture_path)
        analysis["capture_name"] = capture_path.name

        if thumbnail_path and thumbnail_path.exists():
            try:
                rel = thumbnail_path.relative_to(job_dir)
                analysis["capture_thumbnail_url"] = (
                    f"/api/renderdoc-perf/jobs/{job_id}/artifact?path={rel.as_posix()}"
                )
            except ValueError:
                analysis["capture_thumbnail_url"] = ""

        # Annotate each draw row with a lazy texture-thumbnail URL so the
        # frontend can populate the "preview" cell.  Per-draw GPU wireframe
        # is impossible in fallback mode (no replay API) — we substitute the
        # draw's dominant bound texture as a visual hint.
        if zip_companion and zip_companion.exists():
            features = analysis.setdefault("analysis_features", {})
            features["draw_texture_thumbnail_supported"] = True
            for row in analysis.get("rows", []):
                top_tex = _select_preview_texture(row)
                if top_tex is None:
                    continue
                # Prefer data_buffer_id (actual pixel storage in capture.zip)
                # over the texture's own resource id (which is the descriptor).
                lookup_id = top_tex.get("data_buffer_id") or top_tex.get("res_id", "")
                if not lookup_id:
                    continue
                # Use upload's actual dimensions (data_width/height) when
                # available — they tell the decoder how to interpret the
                # compressed block grid.  For partial uploads this avoids
                # mis-sized ASTC headers.
                decode_w = top_tex.get("data_width") or top_tex.get("width", 0)
                decode_h = top_tex.get("data_height") or top_tex.get("height", 0)
                row["draw_preview_url"] = (
                    f"/api/renderdoc-perf/jobs/{job_id}/draw-texture-thumbnail"
                    f"?res_id={lookup_id}"
                    f"&width={decode_w}&height={decode_h}"
                    f"&fmt={top_tex.get('format_full', top_tex.get('format', ''))}"
                )
                row["draw_preview_kind"] = "texture"

        # ------------------------------------------------------------------
        # Upgrade path: GPU replay via QRenderdocScriptBackend
        # ------------------------------------------------------------------
        self._emit_progress(
            job_id,
            "qr_replay",
            "正在用 qrenderdoc 升级线框预览（可能 2-15 分钟，最多 60 个最热 draw）…",
        )
        replay_info = self._run_qrenderdoc_replay(
            job_id=job_id,
            capture_path=capture_path,
            rd_ctx=rd_ctx,
            analysis=analysis,
        )

        self.store.write_json_artifact(job_id, "artifacts/perf_analysis.json", analysis)

        self._emit_progress(job_id, "report", "正在生成性能诊断报告…")
        report_log_lines: list[str] = []
        report_summary = self._generate_report_artifacts(
            job_id=job_id,
            analysis=analysis,
            run_log_lines=report_log_lines,
        )

        features = analysis.get("analysis_features", {})
        run_log = (
            f"[capture] {capture_path}\n"
            f"[renderdoc] dir={rd_ctx.renderdoc_dir} cmd={rd_ctx.renderdoc_cmd_path} source={rd_ctx.source}\n"
            f"[mode] xml_fallback\n"
            f"[xml] {xml_path}\n"
            f"[chrome_json] {chrome_path or 'unavailable'}\n"
            f"[thumbnail] {thumbnail_path or 'unavailable'}\n"
            f"[qr_replay] backend={replay_info.get('backend','none')} "
            f"ok={replay_info.get('ok', False)} "
            f"draws={replay_info.get('draws_upgraded', 0)} "
            f"err={replay_info.get('error') or 'n/a'}\n"
            f"[features] api_duration_chrome={features.get('api_duration_from_chrome_json', False)}, "
            f"instructions_estimated={features.get('instruction_count_estimated', False)}, "
            f"coverage_estimated={features.get('coverage_estimated_from_viewport', False)}, "
            f"qr_replay={features.get('qr_replay_used', False)}\n"
            f"[draws] {analysis['overview']['draw_count']}\n"
            f"[triangles] {analysis['overview']['total_triangles']}\n"
            f"[total_api_ms] {analysis['overview']['total_gpu_duration_ms']}\n"
            f"[total_instructions_est] {analysis['overview']['total_instruction_count']}\n"
            f"[total_texture_mb] {analysis['overview']['total_texture_mb']}\n"
        )
        if report_log_lines:
            run_log = run_log + "\n".join(report_log_lines) + "\n"
        self.store.write_text_artifact(job_id, "artifacts/perf_run_log.txt", run_log)

        overview = analysis.get("overview", {})
        pass_chart = analysis.get("pass_chart", [])
        rows = analysis.get("rows", [])
        self._emit_progress(
            job_id,
            "completed",
            f"分析完成，共处理 {len(rows)} 个 draw（XML 回退）。",
            current=len(rows),
            total=len(rows),
        )
        metadata = self.store.update_metadata(
            job_id,
            {
                "status": "completed",
                "inputs": {
                    "capture_file": str(capture_path),
                    "renderdoc_dir_requested": rd_ctx.renderdoc_dir,
                    "renderdoc_dir_resolved": rd_ctx.renderdoc_dir,
                    "renderdoc_python_path": rd_ctx.renderdoc_python_path,
                    "renderdoc_source": rd_ctx.source,
                    "analysis_mode": "xml_fallback",
                    "replay_backend": replay_info.get("backend", "xml_only"),
                },
                "summary": {
                    "row_count": len(rows),
                    "total_gpu_duration_ms": overview.get("total_gpu_duration_ms", 0.0),
                    "hottest_pass": pass_chart[0]["name"] if pass_chart else "",
                    "total_triangles": overview.get("total_triangles", 0),
                    "total_instruction_count": overview.get("total_instruction_count", 0),
                    "total_texture_mb": overview.get("total_texture_mb", 0.0),
                    "analysis_mode": "xml_fallback",
                    "analysis_features": features,
                    "qr_replay_draws_upgraded": replay_info.get("draws_upgraded", 0),
                    **report_summary,
                },
            },
        )
        detail = self.store.get_job_detail(job_id)
        detail["metadata"] = metadata
        return detail

    def _run_qrenderdoc_replay(
        self,
        *,
        job_id: str,
        capture_path: Path,
        rd_ctx: Any,
        analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Attempt to upgrade XML-only previews with real GPU replay PNGs.

        Always returns a status dict — never raises.  When it succeeds, the
        ``analysis`` dict is mutated in-place to point ``draw_preview_url``
        at the freshly dumped RT/texture PNGs for the hottest draws.
        """
        from app.services.replay_backend import select_replay_backend

        backend = select_replay_backend(rd_ctx.renderdoc_dir, timeout_seconds=900)
        if backend is None:
            return {"backend": "none", "ok": False,
                    "error": "no qrenderdoc.exe in selected renderdoc dir"}

        job_dir = self.store.job_path(job_id)
        qr_out = job_dir / "artifacts" / "qr_replay"
        qr_out.mkdir(parents=True, exist_ok=True)

        rows = analysis.get("rows", []) or []
        # The XML analyzer's "eid" is the chunkIndex of the draw chunk,
        # which is a different ID space from RenderDoc's API ``eventId``
        # used by qrenderdoc.  We can't reliably translate one to the
        # other from the XML side, so let the worker pick its own top-N
        # draws by primitive count.  The manifest emits both ``eid`` and
        # ``chunk_index`` so we can still correlate the results back.
        max_draws = min(60, max(len(rows), 20))

        result = backend.run(
            capture_path=capture_path,
            output_dir=qr_out,
            mode="perf",
            max_draws=max_draws,
            event_ids=None,
        )

        features = analysis.setdefault("analysis_features", {})
        features["qr_replay_backend"] = backend.name
        features["qr_replay_used"] = bool(result.ok)
        features["qr_replay_duration_s"] = round(result.duration_seconds, 2)

        if not result.ok:
            return {
                "backend": backend.name, "ok": False,
                "error": result.error or "unknown",
                "stderr_tail": result.stderr_tail[-500:],
            }

        # The XML-fallback analyzer keys draws by ``chunkIndex`` (position
        # in the structured chunk stream) while RenderDoc's replay API uses
        # ``Action.eventId`` (a different ID space).  We accept either form
        # so the upgrade works regardless of which one matches.
        row_by_id: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            try:
                row_by_id[int(r.get("eid"))] = r
            except (TypeError, ValueError):
                continue

        upgraded = 0
        for d in (result.manifest or {}).get("draws", []):
            row = None
            for key in ("chunk_index", "eid"):
                try:
                    val = int(d.get(key, -1))
                except (TypeError, ValueError):
                    continue
                if val < 0:
                    continue
                row = row_by_id.get(val)
                if row is not None:
                    break
            if row is None:
                continue
            rt_png = d.get("rt_png")
            overlay_png = d.get("overlay_png")
            overlay_kind = d.get("overlay_kind") or ""
            tex_pngs = [t.get("png") for t in (d.get("textures") or []) if t.get("png")]

            base_url = f"/perf-session-files/{job_id}/artifacts/qr_replay"
            if rt_png:
                row["draw_preview_url"] = f"{base_url}/{rt_png}"
                row["draw_preview_kind"] = "rt_replay"
                upgraded += 1
            elif tex_pngs:
                # No RT (e.g. shadow-map only draw) but we still have bound
                # texture PNGs we can show.
                row["draw_preview_url"] = f"{base_url}/{tex_pngs[0]}"
                row["draw_preview_kind"] = "tex_replay"
                upgraded += 1

            # Wireframe overlay (only meaningful when we also have an RT —
            # the overlay is transparent outside the highlighted geometry so
            # it only "reads" when composited over something).  Pass the URL
            # and overlay kind ("wireframe" or "drawcall") through so the
            # frontend can stack & label it.
            if overlay_png and rt_png:
                # PIL composite: RT colour + transparent wireframe overlay
                # → write back to the overlay PNG so every downstream
                # consumer (SPA, embedded HTML report table, base64
                # inlined HTML download, ZIP) picks up the "RT + 线框"
                # look without any further URL/field changes.  Mirrors
                # the post-processing in
                # ``RenderdocDirectReplay.save_draw_rt_and_overlay_preview``.
                self._composite_qr_replay_preview(qr_out / rt_png, qr_out / overlay_png)
                row["draw_preview_overlay_url"] = f"{base_url}/{overlay_png}"
                row["draw_preview_overlay_kind"] = overlay_kind or "wireframe"

            # Always expose the full texture-png list so the UI can pop them
            # in a side-panel when the user clicks a draw row.
            if tex_pngs:
                row["draw_replay_texture_urls"] = [
                    f"{base_url}/{p}" for p in tex_pngs
                ]

        features["qr_replay_draws_upgraded"] = upgraded
        manifest_dict = result.manifest or {}
        features["qr_replay_overlay_kind"] = manifest_dict.get("overlay_kind") or ""
        features["qr_replay_overlay_count"] = int(manifest_dict.get("overlay_count") or 0)
        return {
            "backend": backend.name, "ok": True,
            "draws_upgraded": upgraded,
            "overlay_count": features["qr_replay_overlay_count"],
            "overlay_kind": features["qr_replay_overlay_kind"],
            "duration_s": result.duration_seconds,
        }

    def _load_draws_payload(self, capture_path: Path) -> Any:
        return self._run_session_json(capture_path, ["rdc", "draws", "--json"])

    def _load_counters_payload(self, capture_path: Path) -> Any:
        return self._run_session_json(capture_path, ["rdc", "counters", "--json"])

    def _run_session_json(self, capture_path: Path, command: List[str]) -> Any:
        self._run(["rdc", "close"])
        open_rc, open_output = self._run(["rdc", "open", str(capture_path)])
        if open_rc != 0:
            raise RuntimeError(open_output or f"无法打开 capture: {capture_path}")
        try:
            rc, output = self._run(command)
        finally:
            self._run(["rdc", "close"])
        if rc != 0:
            raise RuntimeError(output or f"命令失败: {' '.join(command)}")
        return self._normalize_json_text(output)

    def _build_rows(
        self,
        *,
        replay: RenderdocDirectReplay,
        draw_rows: List[Dict[str, Any]],
        counter_map: Dict[str, Dict[str, float]],
        action_map: Dict[str, Dict[str, Any]],
        texture_desc_map: Dict[str, Dict[str, Any]],
        run_log_lines: List[str],
        shader_cache: Optional[Dict[str, Dict[str, Any]]] = None,
        available_targets: Optional[List[str]] = None,
        progress_callback: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if shader_cache is None:
            shader_cache = {}

        total_draws = len(draw_rows)
        for draw_index, draw in enumerate(draw_rows, start=1):
            eid = self._stringify(draw.get("eid"))
            if not eid:
                continue
            metadata = action_map.get(eid, {})
            counters = counter_map.get(eid, {})
            replay._set_frame_event(int(eid))
            pipe = replay.controller.GetPipelineState()

            shader_metrics = self._get_shader_metrics(replay, pipe, shader_cache, available_targets)
            texture_summary = self._get_texture_summary(
                replay=replay,
                pipe=pipe,
                texture_desc_map=texture_desc_map,
            )
            target_metrics = self._get_draw_target_metrics(
                pipe=pipe,
                texture_desc_map=texture_desc_map,
            )

            pass_name = self._stringify(metadata.get("pass_name")) or self._stringify(draw.get("marker")) or f"EID {eid}"
            scene_pass_from_marker = self._normalize_scene_pass_name(self._stringify(metadata.get("scene_pass")))
            scene_pass_source = self._stringify(metadata.get("scene_pass_source"))
            if draw.get("triangles") is not None:
                triangle_count = int(draw.get("triangles") or 0)
            else:
                triangle_count = self._compute_triangle_count(
                    replay.rd, pipe, int(draw.get("num_indices") or 0)
                )
            instances = int(draw.get("instances") or 0)
            ps_invocations = int(counters.get("PS Invocations", 0))
            instruction_total = int(shader_metrics.get("instruction_total", 0))
            samples_passed = int(counters.get("Samples Passed", 0))
            target_total_samples = int(target_metrics.get("target_total_samples", 0))
            coverage_ratio = 0.0
            if target_total_samples > 0:
                coverage_ratio = min(max(float(samples_passed) / float(target_total_samples), 0.0), 1.0)
            coverage_percent = round(coverage_ratio * 100.0, 4)
            coverage_pixels_estimate = int(round(float(samples_passed) / max(int(target_metrics.get("target_samples", 1)), 1)))
            instruction_coverage_score = round(float(instruction_total) * coverage_ratio, 6)
            stable_sort_basis = "instruction_x_coverage" if instruction_total > 0 else "ps_invocations_x_coverage"
            stable_sort_score = round(
                (float(instruction_total) if instruction_total > 0 else float(ps_invocations)) * coverage_ratio,
                6,
            )

            # Render-state heuristic: classify every draw so captures
            # without debug markers (Cocos / Unity / in-house) still get a
            # meaningful ``scene_pass`` instead of "Other".
            try:
                state_info = self._classify_pass_from_state(
                    replay=replay,
                    pipe=pipe,
                    draw=draw,
                    target_metrics=target_metrics,
                    screen_coverage_percent=coverage_percent,
                    triangle_count=triangle_count,
                )
            except Exception as exc:
                run_log_lines.append(f"[pass-state] eid={eid} FAILED: {exc}")
                state_info = {"pass_kind": "Other"}
            inferred_pass_kind = self._stringify(state_info.get("pass_kind")) or "Other"

            # Pick the final scene_pass.  Order:
            #   1. marker-derived (only if it matched a known keyword)
            #   2. render-state heuristic
            #   3. outermost breadcrumb (raw engine name, last-resort fallback)
            #   4. "Other"
            marker_was_known = bool(
                scene_pass_from_marker
                and scene_pass_from_marker
                in {label for _, label in self._SCENE_PASS_KEYWORDS}
            )
            if marker_was_known:
                scene_pass = scene_pass_from_marker
                scene_pass_decided_by = "marker"
            elif inferred_pass_kind and inferred_pass_kind != "Other":
                scene_pass = inferred_pass_kind
                scene_pass_decided_by = "render_state"
            elif scene_pass_from_marker:
                scene_pass = scene_pass_from_marker
                scene_pass_decided_by = "marker_raw"
            else:
                scene_pass = "Other"
                scene_pass_decided_by = "fallback"

            row = {
                "eid": eid,
                "scene_pass": scene_pass or "Other",
                "scene_pass_decided_by": scene_pass_decided_by,
                "inferred_pass_kind": inferred_pass_kind,
                "marker_scene_pass": scene_pass_from_marker,
                "marker_scene_pass_source": scene_pass_source,
                "render_state": {
                    "blend_enable": bool(state_info.get("blend_enable")),
                    "color_write_mask": int(state_info.get("color_write_mask") or 0),
                    "depth_test": bool(state_info.get("depth_test")),
                    "depth_write": bool(state_info.get("depth_write")),
                    "cull_mode": self._stringify(state_info.get("cull_mode")),
                    "blend_summary": self._stringify(state_info.get("blend_summary")),
                },
                "pass_name": pass_name,
                "selection_label": f"EID {eid} | {pass_name}",
                "breadcrumbs": metadata.get("breadcrumbs") or [],
                "draw_type": self._stringify(draw.get("type")) or "Draw",
                "instances": instances,
                "triangles": triangle_count,
                "vertices_read": int(counters.get("Input Vertices Read", 0)),
                "input_primitives": int(counters.get("Input Primitives", 0)),
                "gpu_duration_ms": round(float(counters.get("GPU Duration", 0.0)) * 1000.0, 6),
                "vs_invocations": int(counters.get("VS Invocations", 0)),
                "ps_invocations": ps_invocations,
                "samples_passed": samples_passed,
                "vs_instruction_count": int(shader_metrics.get("vs_instruction_count", 0)),
                "ps_instruction_count": int(shader_metrics.get("ps_instruction_count", 0)),
                "instruction_total": instruction_total,
                "target_width": int(target_metrics.get("target_width", 0)),
                "target_height": int(target_metrics.get("target_height", 0)),
                "target_samples": int(target_metrics.get("target_samples", 1)),
                "screen_coverage_percent": coverage_percent,
                "coverage_pixels_estimate": coverage_pixels_estimate,
                "instruction_coverage_score": instruction_coverage_score,
                "stable_sort_score": stable_sort_score,
                "stable_sort_basis": stable_sort_basis,
                "draw_preview_url": "",
                "draw_preview_kind": "wireframe_overlay_pending",
                "draw_preview_overlay_url": "",
                "draw_preview_overlay_kind": "",
                "texture_count": int(texture_summary.get("texture_count", 0)),
                "texture_total_bytes": int(texture_summary.get("total_bytes", 0)),
                "texture_total_mb": round(float(texture_summary.get("total_bytes", 0)) / (1024.0 * 1024.0), 3),
                "texture_bandwidth_risk": round(
                    float(texture_summary.get("total_bytes", 0)) / (1024.0 * 1024.0) * max(ps_invocations, 1),
                    3,
                ),
                "texture_summary_items": texture_summary.get("items", []),
                "texture_summary_text": self._build_texture_summary_text(texture_summary.get("items", [])),
                "texture_previews": [],
                "shader_ids": shader_metrics.get("shader_ids", {}),
            }
            rows.append(row)
            run_log_lines.append(
                f"[row] eid={eid} scene_pass={row['scene_pass']} gpu_ms={row['gpu_duration_ms']:.4f} tris={triangle_count} instr={row['instruction_total']} cover={row['screen_coverage_percent']:.4f}% stable={row['stable_sort_score']:.6f} basis={row['stable_sort_basis']} tex_mb={row['texture_total_mb']:.3f} tex_risk={row['texture_bandwidth_risk']:.3f}"
            )
            if progress_callback is not None and (draw_index % 50 == 0 or draw_index == total_draws):
                try:
                    progress_callback(draw_index, total_draws, eid)
                except Exception:
                    # Progress reporting must never break the analysis.
                    pass

        rows.sort(
            key=lambda item: (
                item.get("stable_sort_score") or 0,
                item.get("instruction_total") or 0,
                item.get("ps_invocations") or 0,
                item.get("screen_coverage_percent") or 0,
                item.get("triangles") or 0,
            ),
            reverse=True,
        )
        return rows

    # Cap for the eager direct-replay preview pass.  Captures with thousands
    # of draws (e.g. UE mobile open-world) would otherwise generate
    # thousands of PNGs synchronously and bloat analysis time.  Any draw
    # beyond this cap keeps its ``wireframe_overlay_pending`` placeholder
    # and is generated on-demand via :meth:`generate_draw_preview`.
    _DIRECT_PREVIEW_HARD_CAP = 500

    # Maximum long-edge (px) for the "RT + wireframe" composite produced
    # by ``_composite_qr_replay_preview``.  Kept in sync with
    # ``renderdoc_direct_replay._COMPOSITE_MAX_EDGE`` so SPA, direct
    # replay and xml_fallback paths all emit similarly-sized PNGs.
    _COMPOSITE_MAX_EDGE = 640

    @classmethod
    def _composite_qr_replay_preview(
        cls, rt_path: Path, overlay_path: Path
    ) -> None:
        """In-place: take the RT screenshot and the transparent wireframe
        overlay produced by ``qr_replay_worker.py`` and overwrite the
        wireframe PNG with a baked "RT + wireframe" composite.  Best
        effort - any PIL error leaves the original PNGs untouched so
        the row never goes blank.
        """
        if not (rt_path.exists() and overlay_path.exists()):
            return
        try:
            from PIL import Image
            rt_img = Image.open(rt_path).convert("RGBA")
            ov_img = Image.open(overlay_path).convert("RGBA")
            if ov_img.size != rt_img.size:
                ov_img = ov_img.resize(rt_img.size, Image.BILINEAR)
            rt_img.alpha_composite(ov_img)
            max_edge = cls._COMPOSITE_MAX_EDGE
            w, h = rt_img.size
            long_edge = max(w, h)
            if max_edge and long_edge > max_edge:
                scale = max_edge / float(long_edge)
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                rt_img = rt_img.resize(new_size, Image.LANCZOS)
            rt_img.save(overlay_path, format="PNG", optimize=True)
        except Exception:
            return

    def _populate_initial_draw_previews(
        self,
        *,
        replay: RenderdocDirectReplay,
        job_id: str,
        preview_dir: Path,
        rows: List[Dict[str, Any]],
        run_log_lines: List[str],
        limit: Optional[int] = None,
        progress_callback: Optional[Any] = None,
    ) -> None:
        # "应有尽有": match the qrenderdoc worker behaviour and emit both an
        # RT screenshot and a wireframe overlay PNG for every draw, up to a
        # safety cap so very large captures don't grind the analysis to a
        # halt.
        if limit is None:
            effective_limit = min(len(rows), self._DIRECT_PREVIEW_HARD_CAP)
        else:
            effective_limit = max(0, min(int(limit), len(rows)))
        saved_count = 0
        skipped_count = 0
        for preview_index, row in enumerate(rows[:effective_limit], start=1):
            eid = self._stringify(row.get("eid"))
            if not eid:
                continue
            rt_path = preview_dir / f"rt_{eid}.png"
            wf_path = preview_dir / f"wireframe_{eid}.png"
            try:
                result = replay.save_draw_rt_and_overlay_preview(
                    eid=eid,
                    rt_output_path=rt_path,
                    overlay_output_path=wf_path,
                )
            except Exception as exc:
                run_log_lines.append(f"[preview] eid={eid} FAILED: {exc}")
                skipped_count += 1
                if progress_callback is not None and (preview_index % 20 == 0 or preview_index == effective_limit):
                    try:
                        progress_callback(preview_index, effective_limit, eid)
                    except Exception:
                        pass
                continue
            rt_saved = result.get("rt_path")
            overlay_saved = result.get("overlay_path")
            if rt_saved is not None and rt_saved.exists():
                rt_rel = rt_saved.relative_to(self.store.job_path(job_id)).as_posix()
                row["draw_preview_url"] = f"/perf-session-files/{job_id}/{rt_rel}"
                row["draw_preview_kind"] = "rt_replay"
            if overlay_saved is not None and overlay_saved.exists():
                wf_rel = overlay_saved.relative_to(self.store.job_path(job_id)).as_posix()
                row["draw_preview_overlay_url"] = f"/perf-session-files/{job_id}/{wf_rel}"
                row["draw_preview_overlay_kind"] = "wireframe"
                # Fallback: if RT capture failed for some reason we still
                # surface the overlay so the row is not blank.
                if not row.get("draw_preview_url"):
                    row["draw_preview_url"] = row["draw_preview_overlay_url"]
                    row["draw_preview_kind"] = "wireframe_overlay"
            if rt_saved is None and overlay_saved is None:
                skipped_count += 1
            else:
                saved_count += 1
            if progress_callback is not None and (preview_index % 20 == 0 or preview_index == effective_limit):
                try:
                    progress_callback(preview_index, effective_limit, eid)
                except Exception:
                    pass
        run_log_lines.append(
            f"[preview] direct_replay saved={saved_count} skipped={skipped_count} "
            f"cap={effective_limit}/{len(rows)} (hard_cap={self._DIRECT_PREVIEW_HARD_CAP})"
        )

    def _collect_action_map(self, replay: RenderdocDirectReplay) -> Dict[str, Dict[str, Any]]:
        """Walk the RenderDoc action tree and collect breadcrumbs + best-guess
        pass labels for every event.

        Earlier this code hard-required a ``customName == "MobileSceneRender"``
        node at the top of the tree to populate ``scene_pass``; this only
        worked for Unreal Engine 4/5 Mobile captures.  Captures from other
        engines (Unity, Cocos, in-house engines) usually never have that
        marker - or any debug markers at all - so every draw came back as
        ``scene_pass=Other`` and ``pass_name=EID xxx``.

        The new logic is engine-agnostic:

        * ``breadcrumbs`` = full ancestor ``customName`` chain (most outer
          first).  Empty names are skipped.
        * ``pass_name`` = nearest non-empty ancestor name, or the outermost
          one if the nearest is the same as a known UE root.  This is what
          we display in the row's ``pass_name`` column.
        * ``scene_pass`` = the **outermost recognisable group name** in the
          breadcrumbs (e.g. an Unreal ``MobileBasePass`` at depth 1, or any
          group name returned by `_normalize_scene_pass_name`).  This is
          what aggregations (pass chart, R005/R006 rules) key on.
        * ``scene_pass_source`` = ``"marker"`` when we found a name in
          breadcrumbs, otherwise ``""`` so the caller can fall back to the
          render-state heuristic.
        """
        result: Dict[str, Dict[str, Any]] = {}

        def walk(actions: Iterable[Any], ancestors: List[str]) -> None:
            for action in actions:
                name = self._stringify(getattr(action, "customName", ""))
                named_ancestors = ancestors + ([name] if name else [])
                event_id = self._stringify(getattr(action, "eventId", ""))
                if event_id:
                    nearest_name = named_ancestors[-1] if named_ancestors else ""
                    # Walk from outer to inner and pick the first crumb that
                    # _normalize_scene_pass_name() recognises as a pass.  If
                    # nothing matches, leave scene_pass empty so the state
                    # heuristic can take over.
                    scene_pass = ""
                    for crumb in named_ancestors:
                        normalised = self._normalize_scene_pass_name(crumb)
                        if normalised and normalised != crumb:
                            # Real match against a known keyword.
                            scene_pass = normalised
                            break
                    if not scene_pass and named_ancestors:
                        # No known-keyword hit, but the engine *did* leave a
                        # marker.  Use the outermost as a fallback so users
                        # see *something* meaningful instead of "Other".
                        scene_pass = self._normalize_scene_pass_name(named_ancestors[0]) or named_ancestors[0]
                    result[event_id] = {
                        "pass_name": nearest_name,
                        "scene_pass": scene_pass,
                        "scene_pass_source": "marker" if scene_pass else "",
                        "breadcrumbs": named_ancestors,
                    }
                walk(getattr(action, "children", []), named_ancestors)

        walk(replay.controller.GetRootActions(), [])
        return result

    def _get_shader_metrics(
        self,
        replay: RenderdocDirectReplay,
        pipe: Any,
        shader_cache: Dict[str, Dict[str, Any]],
        available_targets: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {
            "vs_instruction_count": 0,
            "ps_instruction_count": 0,
            "instruction_total": 0,
            "shader_ids": {},
            "disassembly_targets": {},
            "instruction_count_estimated": False,
        }
        # Opt-2: ``available_targets`` is pre-fetched once per analysis
        # by ``analyze_capture`` and passed in.  The legacy per-draw
        # ``GetDisassemblyTargets`` call is kept as a defensive fallback
        # so this method still works if a caller forgets to pass it in.
        if available_targets is None:
            try:
                available_targets_raw = list(replay.controller.GetDisassemblyTargets(True))
            except Exception:
                available_targets_raw = []
            available_targets = [self._stringify(item) for item in available_targets_raw if self._stringify(item)]

        pipeline_object = pipe.GetGraphicsPipelineObject()
        for stage_name, stage_enum in (("vs", replay.rd.ShaderStage.Vertex), ("ps", replay.rd.ShaderStage.Pixel)):
            shader_id = str(pipe.GetShader(stage_enum))
            metrics["shader_ids"][stage_name] = shader_id
            if not shader_id or shader_id.endswith("::0"):
                continue
            if shader_id not in shader_cache:
                refl = pipe.GetShaderReflection(stage_enum)
                count, used_target, estimated = self._disassemble_and_count_instructions(
                    replay=replay,
                    pipeline_object=pipeline_object,
                    refl=refl,
                    available_targets=available_targets,
                )
                # Prefer a GLSL/SPIR-V-based instruction estimate over the
                # host-GPU ISA line count.  Mobile GLES captures replayed on
                # desktop only expose the host ISA (e.g. "AMD GCN ISA"), which
                # our line-count heuristic could not parse, so every shader
                # collapsed to a near-constant value.  The original GLSL source
                # lives in ``reflection.debugInfo`` and is far more meaningful,
                # and is also what ``malioc`` needs downstream.
                glsl_source = ""
                source_kind = ""
                try:
                    glsl_source, source_kind = replay.get_shader_glsl_source(pipe, stage_enum)
                except Exception:
                    glsl_source, source_kind = "", ""
                if glsl_source and source_kind in {"glsl_source", "glsl_disasm", "spirv_disasm"}:
                    glsl_count = self._estimate_source_instructions(glsl_source, source_kind)
                    if glsl_count > 0:
                        count = glsl_count
                        used_target = source_kind
                        # A real source counts as solid; a disassembly-derived
                        # estimate stays flagged as estimated.
                        estimated = source_kind != "glsl_source"
                shader_cache[shader_id] = {
                    "instruction_count": count,
                    "disassembly_target": used_target,
                    "estimated": estimated,
                    "glsl_source": glsl_source,
                    "source_kind": source_kind,
                    "stage": stage_name,
                }
            cache_entry = shader_cache[shader_id]
            metrics[f"{stage_name}_instruction_count"] = int(cache_entry["instruction_count"])
            metrics["disassembly_targets"][stage_name] = cache_entry.get("disassembly_target", "")
            if cache_entry.get("estimated"):
                metrics["instruction_count_estimated"] = True
        metrics["instruction_total"] = metrics["vs_instruction_count"] + metrics["ps_instruction_count"]
        return metrics

    @staticmethod
    def _estimate_source_instructions(source: str, source_kind: str) -> int:
        """Estimate an instruction count from GLSL source / SPIR-V disassembly.

        Used as the preferred instruction metric when the host-GPU ISA line
        count is meaningless (mobile GLES capture replayed on desktop).
        Never raises - returns 0 on any failure so the caller falls back to
        the disassembly count.
        """
        text = source or ""
        if not text.strip():
            return 0
        if source_kind == "spirv_disasm":
            try:
                return RenderdocPerfService._count_shader_instructions(text, family="spirv")
            except Exception:
                return 0
        try:
            from app.services.renderdoc_xml_analyzer import _estimate_glsl_instructions
            return int(_estimate_glsl_instructions(text))
        except Exception:
            return 0

    def _run_mali_shader_analysis(
        self,
        shader_cache: Dict[str, Dict[str, Any]],
        run_log_lines: List[str],
    ) -> List[Dict[str, Any]]:
        """Run the Mali Offline Compiler over every unique shader that has
        real GLSL source in the cache, returning a list of metric dicts.

        Best-effort: any failure (malioc missing, source unavailable, parse
        error) leaves that shader out and never aborts the analysis.
        """
        try:
            from app.services.perf_report import MaliShaderAnalyzer
        except Exception as exc:  # pragma: no cover - import guard
            run_log_lines.append(f"[mali] analyzer import failed: {exc}")
            return []

        analyzer = MaliShaderAnalyzer()
        if not analyzer.is_available():
            run_log_lines.append("[mali] malioc 不可用，跳过 Mali shader 分析")
            return []

        results: List[Dict[str, Any]] = []
        analyzed = 0
        for shader_id, entry in shader_cache.items():
            source = self._stringify(entry.get("glsl_source"))
            source_kind = self._stringify(entry.get("source_kind"))
            # malioc compiles GLSL source; disassembly text won't compile.
            if not source or source_kind != "glsl_source":
                continue
            stage = self._stringify(entry.get("stage")) or "fs"
            try:
                metrics = analyzer.analyze(source, stage, shader_id=shader_id)
            except Exception as exc:
                run_log_lines.append(f"[mali] {shader_id} FAILED: {exc}")
                continue
            results.append(metrics.to_dict())
            if metrics.available:
                analyzed += 1
        run_log_lines.append(
            f"[mali] analyzed={analyzed} of {len(shader_cache)} unique shaders "
            f"(only shaders with GLSL source are compiled)"
        )
        return results

    def _disassemble_and_count_instructions(
        self,
        *,
        replay: RenderdocDirectReplay,
        pipeline_object: Any,
        refl: Any,
        available_targets: List[str],
    ) -> Tuple[int, str, bool]:
        """Try several disassembly targets in order of fidelity for instruction
        counting and return ``(count, used_target, estimated)``.

        Earlier the perf path hard-coded ``"DXBC"`` which threw on every
        GLES/Vulkan capture (e.g. the user's ``DZ_ZMXT-frame71704.rdc``),
        making the three instruction-count fields silently zero.  We now
        walk a prioritised list of targets and apply a format-appropriate
        counting heuristic to each, falling back to a conservative
        line-count estimate so the result is rarely zero in practice.
        """
        if not available_targets:
            return 0, "", False

        priority_order = ("dxbc", "dxil", "hlsl", "glsl", "opengl", "gles", "spir", "msl", "metal")
        seen: set[str] = set()
        ordered: List[str] = []
        for keyword in priority_order:
            for target in available_targets:
                if keyword in target.lower() and target not in seen:
                    ordered.append(target)
                    seen.add(target)
        # Then anything left over so we always try every published target.
        for target in available_targets:
            if target not in seen:
                ordered.append(target)
                seen.add(target)

        last_disassembly = ""
        last_target = ""
        for target in ordered:
            try:
                disassembly = replay.controller.DisassembleShader(pipeline_object, refl, target)
            except Exception:
                continue
            if not disassembly:
                continue
            last_disassembly = disassembly
            last_target = target
            family = replay._classify_shader_target(target)  # type: ignore[attr-defined]
            count = self._count_shader_instructions(disassembly, family=family)
            if count > 0:
                return count, target, False

        # Final fallback: use the most recent disassembly that came back
        # but apply a conservative 0.6 multiplier to raw line count.  This
        # at least keeps the metric non-zero so downstream rules can fire,
        # and we flag it as estimated.
        if last_disassembly:
            raw_lines = sum(1 for ln in last_disassembly.splitlines() if ln.strip())
            est = int(raw_lines * 0.6)
            return max(est, 0), last_target, True

        return 0, "", False

    def _get_texture_summary(
        self,
        *,
        replay: RenderdocDirectReplay,
        pipe: Any,
        texture_desc_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            bindings = list(pipe.GetReadOnlyResources(replay.rd.ShaderStage.Pixel))
        except Exception:
            bindings = []
        items: List[Dict[str, Any]] = []
        total_bytes = 0
        for binding in bindings:
            slot = int(getattr(binding.access, "index", 0) or 0)
            resource_id = str(getattr(binding.descriptor, "resource", ""))
            desc = texture_desc_map.get(resource_id, {})
            byte_size = int(desc.get("byte_size", 0) or 0)
            total_bytes += byte_size
            items.append(
                {
                    "slot": slot,
                    "resource_id": resource_id,
                    "width": int(desc.get("width", 0) or 0),
                    "height": int(desc.get("height", 0) or 0),
                    "format": self._stringify(desc.get("format_name")) or "Unknown",
                    "byte_size": byte_size,
                    "byte_size_mb": round(byte_size / (1024.0 * 1024.0), 3),
                }
            )
        items.sort(key=lambda item: (item["byte_size"], -item["slot"]), reverse=True)
        return {
            "texture_count": len(bindings),
            "total_bytes": total_bytes,
            "items": items[:6],
        }

    @staticmethod
    def _render_state_enum_name(value: Any, rd_module: Any, enum_name: str) -> str:
        """Best-effort conversion of a RenderDoc pipeline-state enum to a
        lowercase symbolic name.

        The Python bindings expose enums as classes on the ``rd`` module
        (``rd.BlendMultiplier``, ``rd.BlendOperation``, ...).  Calling
        ``str(enum_value)`` typically returns the **integer** form, which
        is useless for substring matching.  We try, in order:

          1. ``value.name`` (works for some bindings)
          2. ``rd.<EnumClass>(value).name`` (works on most pybind11 builds)
          3. ``str(value)`` (fallback)
        """
        if value is None:
            return ""
        name = getattr(value, "name", None)
        if isinstance(name, str) and name:
            return name.lower()
        enum_cls = getattr(rd_module, enum_name, None)
        if enum_cls is not None:
            try:
                wrapped = enum_cls(int(value))
                wrapped_name = getattr(wrapped, "name", None)
                if isinstance(wrapped_name, str) and wrapped_name:
                    return wrapped_name.lower()
            except Exception:
                pass
        return str(value).lower()

    def _classify_pass_from_state(
        self,
        *,
        replay: RenderdocDirectReplay,
        pipe: Any,
        draw: Mapping[str, Any],
        target_metrics: Mapping[str, Any],
        screen_coverage_percent: float,
        triangle_count: int,
    ) -> Dict[str, Any]:
        """Infer a ``pass_kind`` from pipeline render state.

        This is the fallback when the capture has no useful debug markers
        (e.g. Cocos / Unity / in-house engines that don't call
        ``glPushDebugGroup``).  We classify each draw into one of:

          DepthOnly | Shadow | Translucent | Additive | PostProcess |
          Sky | UI | Opaque | Other

        The decision is driven by colour write mask, blend equation,
        depth state, viewport coverage, and draw topology.  All three
        ingredients - state + coverage + triangle count - are also
        surfaced verbatim on the row so a human can sanity-check the
        classifier or filter the perf table by hand.
        """
        rd = replay.rd
        info: Dict[str, Any] = {
            "blend_enable": False,
            "color_write_mask": 0xF,
            "depth_test": False,
            "depth_write": False,
            "cull_mode": "",
            "blend_summary": "",
            "pass_kind": "Other",
        }

        # Colour blend / write mask (one entry per render target).
        try:
            blends = list(pipe.GetColorBlends() or [])
        except Exception:
            try:
                blends = list(pipe.GetColorBlendStates() or [])  # older API
            except Exception:
                blends = []
        first_blend = blends[0] if blends else None
        if first_blend is not None:
            info["blend_enable"] = bool(getattr(first_blend, "enabled", False))
            mask = int(getattr(first_blend, "writeMask", 0xF) or 0)
            info["color_write_mask"] = mask
            color_blend = getattr(first_blend, "colorBlend", None)
            src = self._render_state_enum_name(getattr(color_blend, "source", None), rd, "BlendMultiplier")
            dst = self._render_state_enum_name(getattr(color_blend, "destination", None), rd, "BlendMultiplier")
            op = self._render_state_enum_name(getattr(color_blend, "operation", None), rd, "BlendOperation")
            # Only surface the blend equation when the stage is actually
            # enabled.  When blend is off the API still returns the cached
            # state-machine defaults, which would otherwise mislead users
            # looking at the tooltip.
            if info["blend_enable"] and (src or dst or op):
                info["blend_summary"] = f"{src}|{dst}|{op}"

        # Depth state.
        try:
            depth = pipe.GetDepthState() if hasattr(pipe, "GetDepthState") else None
        except Exception:
            depth = None
        if depth is None:
            try:
                depth = pipe.GetDepthStencilState()
            except Exception:
                depth = None
        if depth is not None:
            info["depth_test"] = bool(getattr(depth, "depthEnable", False) or getattr(depth, "enabled", False))
            info["depth_write"] = bool(getattr(depth, "depthWrites", False) or getattr(depth, "writeEnable", False))

        # Rasterizer / cull mode.
        try:
            raster = pipe.GetRasterizer() if hasattr(pipe, "GetRasterizer") else None
            if raster is None and hasattr(pipe, "GetRasterizerState"):
                raster = pipe.GetRasterizerState()
            if raster is not None:
                info["cull_mode"] = self._stringify(getattr(raster, "cullMode", ""))
        except Exception:
            pass

        # Heuristic decision tree (most specific → most general).
        blend_summary = info["blend_summary"]
        color_off = (info["color_write_mask"] & 0xF) == 0
        depth_only = color_off and info["depth_write"]
        target_width = int(target_metrics.get("target_width") or 0)
        target_height = int(target_metrics.get("target_height") or 0)
        target_area = target_width * target_height
        is_small_rt = target_area > 0 and target_area < 1280 * 720
        is_fullscreen_geometry = triangle_count <= 2 and screen_coverage_percent >= 70.0

        # Treat blend names like ``srcalpha``, ``src_alpha``, ``invsrcalpha``,
        # ``inv_src_alpha``, ``oneminussrcalpha`` etc. as the canonical alpha
        # blend.  Strip the divider characters so we can use simple substring
        # checks instead of cataloguing every variant.
        bs_clean = blend_summary.replace("_", "").replace(" ", "")
        is_alpha_blend = (
            ("srcalpha" in bs_clean and ("invsrcalpha" in bs_clean or "oneminussrcalpha" in bs_clean))
            or "premultiplied" in bs_clean
        )
        is_additive = "one|one" in blend_summary or "one|one|add" in bs_clean
        is_multiplicative = "dstcolor" in bs_clean or "destcolor" in bs_clean

        pass_kind = "Other"
        if depth_only:
            pass_kind = "Shadow" if is_small_rt else "DepthOnly"
        elif info["blend_enable"] and is_additive:
            pass_kind = "Additive"
        elif info["blend_enable"] and is_alpha_blend:
            if is_fullscreen_geometry:
                pass_kind = "PostProcess"
            elif is_small_rt:
                pass_kind = "UI"
            else:
                pass_kind = "Translucent"
        elif info["blend_enable"] and is_multiplicative:
            pass_kind = "Translucent"
        elif info["blend_enable"]:
            # Some other blend - still treat as translucent-ish but flag.
            pass_kind = "Translucent" if not is_fullscreen_geometry else "PostProcess"
        elif is_fullscreen_geometry and not info["depth_test"]:
            pass_kind = "Sky" if info["depth_write"] is False else "PostProcess"
        elif is_fullscreen_geometry:
            pass_kind = "PostProcess"
        elif info["depth_write"] and not color_off:
            pass_kind = "Opaque"
        elif not info["depth_write"] and not color_off and not info["blend_enable"]:
            # Opaque-style draw that doesn't write depth (e.g. some emissive
            # or decal passes).  Still much more informative than "Other".
            pass_kind = "Opaque"

        info["pass_kind"] = pass_kind
        return info

    @staticmethod
    def _get_draw_target_metrics(
        *,
        pipe: Any,
        texture_desc_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, int]:
        target_resource = None
        for target in list(pipe.GetOutputTargets()):
            resource = str(getattr(target, "resource", ""))
            if resource and resource != "ResourceId::0":
                target_resource = resource
                break
        if target_resource is None:
            depth_target = pipe.GetDepthTarget()
            resource = str(getattr(depth_target, "resource", ""))
            if resource and resource != "ResourceId::0":
                target_resource = resource
        if target_resource is None:
            return {
                "target_width": 0,
                "target_height": 0,
                "target_samples": 1,
                "target_total_samples": 0,
            }

        desc = texture_desc_map.get(target_resource, {})
        width = int(desc.get("width", 0) or 0)
        height = int(desc.get("height", 0) or 0)
        samples = int(desc.get("samples", 0) or 0)
        if samples <= 0:
            samples = 1
        return {
            "target_width": width,
            "target_height": height,
            "target_samples": samples,
            "target_total_samples": width * height * samples,
        }

    def _resolve_capture_path(self, job_id: str, detail: Dict[str, Any]) -> Path:
        capture_text = self._stringify((detail.get("metadata") or {}).get("inputs", {}).get("capture_file"))
        capture_path = Path(capture_text)
        if capture_path.is_absolute() and capture_path.exists():
            return capture_path
        fallback = self.store.job_path(job_id) / capture_text
        if fallback.exists():
            return fallback
        fallback = self.store.job_path(job_id) / "inputs" / "capture.rdc"
        if fallback.exists():
            return fallback
        raise FileNotFoundError(f"performance capture not found for job: {job_id}")

    @staticmethod
    def _build_sort_fields(unavailable_fields: List[str]) -> List[Dict[str, str]]:
        """Build the SPA sort dropdown, dropping fields whose backing GPU
        counter the replay backend did not provide.

        When pipeline-statistics counters are missing (typical for desktop
        replay of a mobile GLES capture), sorting/displaying by PS 调用 /
        覆盖率 / 稳定得分 / 顶点 / 图元 is meaningless (all zero), so we hide
        them and lead with the metrics that ARE real: GPU 耗时, 三角面,
        指令数, 贴图.
        """
        unavailable = set(unavailable_fields or [])
        # Ordered so the first valid entry becomes the SPA's default sort.
        all_fields = [
            {"id": "gpu_duration_ms", "label": "GPU耗时"},
            {"id": "instruction_total", "label": "总指令数"},
            {"id": "ps_instruction_count", "label": "PS指令数"},
            {"id": "vs_instruction_count", "label": "VS指令数"},
            {"id": "triangles", "label": "三角面数"},
            {"id": "texture_total_mb", "label": "贴图总大小(MB)"},
            {"id": "texture_count", "label": "贴图数量"},
            {"id": "texture_bandwidth_risk", "label": "纹理带宽风险(估算)"},
            {"id": "stable_sort_score", "label": "稳定得分(估算)"},
            {"id": "screen_coverage_percent", "label": "屏幕覆盖率(估算%)"},
            {"id": "vertices_read", "label": "顶点数量"},
            {"id": "input_primitives", "label": "输入图元"},
            {"id": "ps_invocations", "label": "PS调用数"},
            {"id": "vs_invocations", "label": "VS调用数"},
        ]
        return [field for field in all_fields if field["id"] not in unavailable]

    def _build_overview(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        total_gpu_duration_ms = round(sum(float(item.get("gpu_duration_ms") or 0.0) for item in rows), 6)
        return {
            "draw_count": len(rows),
            "total_gpu_duration_ms": total_gpu_duration_ms,
            "total_triangles": int(sum(int(item.get("triangles") or 0) for item in rows)),
            "total_vertices_read": int(sum(int(item.get("vertices_read") or 0) for item in rows)),
            "total_instruction_count": int(sum(int(item.get("instruction_total") or 0) for item in rows)),
            "total_stable_sort_score": round(sum(float(item.get("stable_sort_score") or 0.0) for item in rows), 6),
            "total_instruction_coverage_score": round(
                sum(float(item.get("instruction_coverage_score") or 0.0) for item in rows),
                6,
            ),
            "total_texture_mb": round(sum(float(item.get("texture_total_mb") or 0.0) for item in rows), 3),
        }

    def _build_pass_chart(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        expected_order = [
            "ShadowDepths",
            "MobileRenderPrePass",
            "MobileBasePass",
            "Translucency",
            "PostProcessing",
        ]
        bucket: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "name": "Other",
                "gpu_duration_ms": 0.0,
                "triangles": 0,
                "draw_count": 0,
            }
        )
        for row in rows:
            name = self._normalize_scene_pass_name(self._stringify(row.get("scene_pass"))) or "Other"
            if name == "Other":
                continue
            item = bucket[name]
            item["name"] = name
            item["gpu_duration_ms"] += float(row.get("gpu_duration_ms") or 0.0)
            item["triangles"] += int(row.get("triangles") or 0)
            item["draw_count"] += 1

        if not bucket:
            return []

        total_gpu = sum(item["gpu_duration_ms"] for item in bucket.values()) or 1.0
        result = []
        seen_names: set[str] = set()
        for name in expected_order + sorted(bucket.keys()):
            if name not in bucket:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            item = bucket[name]
            result.append(
                {
                    "name": item["name"],
                    "gpu_duration_ms": round(item["gpu_duration_ms"], 6),
                    "triangles": item["triangles"],
                    "draw_count": item["draw_count"],
                    "percent": round(item["gpu_duration_ms"] / total_gpu * 100.0, 2),
                }
            )
        result.sort(key=lambda item: item["gpu_duration_ms"], reverse=True)
        return result

    def _build_hotspot_hints(self, pass_chart: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> List[str]:
        hints: List[str] = []
        if pass_chart:
            top_pass = pass_chart[0]
            hints.append(
                f"优先关注 `{top_pass['name']}`，当前约占总 GPU 开销 {top_pass['percent']}%，累计 {top_pass['gpu_duration_ms']:.3f} ms。"
            )
        for item in pass_chart[1:3]:
            if item["percent"] >= 15:
                hints.append(
                    f"`{item['name']}` 也有较高占比，约 {item['percent']}%，建议和主热点一起检查。"
                )
        if rows:
            stable_hotspot = max(rows, key=lambda item: float(item.get("stable_sort_score") or 0.0))
            basis_text = "指令x面积" if stable_hotspot.get("stable_sort_basis") == "instruction_x_coverage" else "PS调用x面积"
            hints.append(
                f"稳定排序最重项为 `EID {stable_hotspot['eid']} | {stable_hotspot['pass_name']}`，依据 `{basis_text}`，屏幕覆盖约 {float(stable_hotspot.get('screen_coverage_percent') or 0.0):.4f}%。"
            )
            texture_hotspot = max(rows, key=lambda item: float(item.get("texture_bandwidth_risk") or 0.0))
            if float(texture_hotspot.get("texture_bandwidth_risk") or 0.0) > 0:
                hints.append(
                    f"纹理带宽风险最高的是 `EID {texture_hotspot['eid']} | {texture_hotspot['pass_name']}`，绑定贴图约 {float(texture_hotspot.get('texture_total_mb') or 0.0):.3f} MB。"
                )
        return hints

    @staticmethod
    def _build_warnings(capture_info: Dict[str, Any]) -> List[str]:
        driver_name = str(capture_info.get("driver_name") or "").strip()
        warnings: List[str] = []
        if driver_name in {"OpenGL", "Vulkan"}:
            warnings.append(
                "当前 capture 的单 draw GPU Duration 在移动/模拟器/TBDR 场景下可能波动较大；当前结果页已改为优先使用“稳定得分(估算)”排序，有指令数时按“指令x面积”，否则退化为“PS调用x面积”。"
            )
        return warnings

    @staticmethod
    def _build_texture_summary_text(items: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for item in items:
            width = int(item.get("width", 0) or 0)
            height = int(item.get("height", 0) or 0)
            fmt = str(item.get("format") or "Unknown")
            slot = int(item.get("slot", 0) or 0)
            mb = float(item.get("byte_size_mb") or 0.0)
            parts.append(f"T{slot} {width}x{height} {fmt} {mb:.3f}MB")
        return " | ".join(parts)

    @staticmethod
    def _compute_triangle_count(rd_mod: Any, pipe: Any, num_indices: int) -> int:
        """Derive a triangle count from the draw's primitive topology.

        Replaces the value the old ``rdc draws --json`` CLI used to provide.
        Only triangle topologies contribute; lines / points / patches return
        0 (their geometry isn't measured in triangles).
        """
        if num_indices <= 0:
            return 0
        topology_enum = getattr(rd_mod, "Topology", None)
        if topology_enum is None:
            return 0
        try:
            topo = int(pipe.GetPrimitiveTopology())
        except Exception:
            return 0
        try:
            tri_list = int(topology_enum.TriangleList)
            tri_strip = int(topology_enum.TriangleStrip)
            tri_fan = int(topology_enum.TriangleFan)
            tri_list_adj = int(topology_enum.TriangleList_Adj)
            tri_strip_adj = int(topology_enum.TriangleStrip_Adj)
        except Exception:
            return 0
        if topo == tri_list:
            return num_indices // 3
        if topo in (tri_strip, tri_fan):
            return max(0, num_indices - 2)
        if topo == tri_list_adj:
            return num_indices // 6
        if topo == tri_strip_adj:
            return max(0, (num_indices // 2) - 2)
        return 0

    @staticmethod
    def _extract_draw_rows(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("draws", "items", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_counter_map(payload: Any) -> Dict[str, Dict[str, float]]:
        rows = []
        if isinstance(payload, dict):
            rows = payload.get("rows") or []
        elif isinstance(payload, list):
            rows = payload
        result: Dict[str, Dict[str, float]] = defaultdict(dict)
        for item in rows:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("eid") or "").strip()
            counter_name = str(item.get("counter") or "").strip()
            if not eid or not counter_name:
                continue
            result[eid][counter_name] = float(item.get("value") or 0.0)
        return dict(result)

    @staticmethod
    def _count_shader_instructions(disassembly: str, *, family: str = "dxbc") -> int:
        """Count shader instructions in ``disassembly`` using a heuristic
        tailored to the target ``family``.

        The intent is not to match a specific compiler's instruction count
        byte-for-byte but to give a stable proxy that scales with shader
        complexity so the perf rules can rank draws sensibly across very
        different graphics APIs.
        """
        text = disassembly or ""
        if not text:
            return 0
        lines = text.splitlines()
        family_norm = (family or "").lower()
        if family_norm in ("dxbc", "dxil"):
            return sum(1 for line in lines if re.match(r"^\s*\d+:", line))
        if family_norm == "spirv":
            return sum(
                1
                for line in lines
                if re.match(r"^\s*%\w+\s*=", line) or re.match(r"^\s*Op[A-Z]", line)
            )
        if family_norm in ("glsl", "hlsl", "msl"):
            count = 0
            in_block_comment = False
            for raw in lines:
                line = raw.strip()
                if not line:
                    continue
                if in_block_comment:
                    if "*/" in line:
                        in_block_comment = False
                    continue
                if line.startswith("/*"):
                    if "*/" not in line:
                        in_block_comment = True
                    continue
                if line.startswith("//") or line.startswith("#"):
                    continue
                if line in ("{", "}", "};"):
                    continue
                # Skip pure declarations / qualifiers / function signatures
                # but keep statements that look like work (assignment, call,
                # control flow).
                if line.endswith("{") and "(" not in line:
                    continue
                count += 1
            return count
        # Generic fallback: any non-empty, non-comment line.
        count = 0
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("//", "#", ";")):
                continue
            count += 1
        return count

    # Known pass-name keywords mapped to a canonical label.  The mapping is
    # intentionally broad so we recognise debug markers across UE, Unity,
    # Cocos, and custom engines.  Order matters: more specific keywords
    # (e.g. "mobilebasepass") must come before more generic ones
    # (e.g. "basepass") to win the substring match.
    _SCENE_PASS_KEYWORDS: List[Tuple[str, str]] = [
        # Unreal Engine 4/5 Mobile
        ("shadowdepths", "ShadowDepths"),
        ("mobilerenderprepass", "MobileRenderPrePass"),
        ("mobilebasepass", "MobileBasePass"),
        ("postprocessing", "PostProcessing"),
        ("translucency", "Translucency"),
        # Generic / Unity / Cocos / in-house
        ("prepass", "PrePass"),
        ("gbuffer", "GBuffer"),
        ("basepass", "BasePass"),
        ("depthonly", "DepthOnly"),
        ("shadow", "Shadow"),
        ("velocity", "Velocity"),
        ("decal", "Decal"),
        ("lighting", "Lighting"),
        ("ssao", "SSAO"),
        ("ssr", "SSR"),
        ("reflection", "Reflection"),
        ("refraction", "Refraction"),
        ("transparent", "Translucent"),
        ("translucent", "Translucent"),
        ("additive", "Translucent"),
        ("alphablend", "Translucent"),
        ("alpha-blend", "Translucent"),
        ("alpha_blend", "Translucent"),
        ("sky", "Sky"),
        ("skybox", "Sky"),
        ("tonemap", "PostProcess"),
        ("bloom", "PostProcess"),
        ("fxaa", "PostProcess"),
        ("taa", "PostProcess"),
        ("smaa", "PostProcess"),
        ("dof", "PostProcess"),
        ("postprocess", "PostProcess"),
        ("post-process", "PostProcess"),
        ("post_process", "PostProcess"),
        ("blit", "Blit"),
        ("copy", "Copy"),
        ("resolve", "Resolve"),
        ("clear", "Clear"),
        ("ui", "UI"),
        ("hud", "UI"),
        ("widget", "UI"),
        ("imgui", "UI"),
        ("particle", "Particle"),
        ("fluid", "Particle"),
        ("water", "Water"),
        ("foliage", "Foliage"),
        ("opaque", "Opaque"),
        ("emissive", "Emissive"),
        ("outline", "Outline"),
        ("edge", "Outline"),
        ("stencil", "Stencil"),
    ]

    @classmethod
    def _normalize_scene_pass_name(cls, name: str) -> str:
        text = (name or "").strip()
        if not text:
            return ""
        lowers = text.lower()
        for key, value in cls._SCENE_PASS_KEYWORDS:
            if key in lowers:
                return value
        return text

    @staticmethod
    def _normalize_json_text(text: str) -> Any:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _run(args: List[str]) -> tuple[int, str]:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **hidden_subprocess_kwargs(),
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return proc.returncode, output.strip()


def _perf_worker_entry(session_root: str, job_id: str, capture_path: str, conn: Connection, renderdoc_dir: str = "") -> None:
    store = RenderdocPerfStore(Path(session_root))
    service = RenderdocPerfService(store)
    try:
        service.analyze_capture(job_id, Path(capture_path), renderdoc_dir=renderdoc_dir)
        conn.send({"ok": True})
    except Exception as exc:
        service._emit_progress(job_id, "failed", f"性能分析失败：{exc}")
        store.update_metadata(job_id, {"status": "failed"})
        conn.send({"ok": False, "error": str(exc)})
    finally:
        conn.close()


def _preview_worker_entry(
    capture_path: str,
    eid: str,
    rt_output_path: str,
    wf_output_path: str,
    renderdoc_dir: str,
    conn: Connection,
) -> None:
    try:
        from app.services.renderdoc_runtime_resolver import resolve_renderdoc_runtime
        rd_ctx = resolve_renderdoc_runtime(renderdoc_dir)
        with RenderdocDirectReplay(capture_path, renderdoc_python_path=rd_ctx.renderdoc_python_path) as replay:
            result = replay.save_draw_rt_and_overlay_preview(
                eid=eid,
                rt_output_path=rt_output_path,
                overlay_output_path=wf_output_path,
            )
        rt_ok = bool(result.get("rt_path") and Path(result["rt_path"]).exists())
        overlay_ok = bool(result.get("overlay_path") and Path(result["overlay_path"]).exists())
        if not rt_ok and not overlay_ok:
            conn.send({"ok": False, "error": f"无法生成 EID {eid} 的预览"})
        else:
            conn.send({"ok": True, "rt": rt_ok, "overlay": overlay_ok})
    except Exception as exc:
        conn.send({"ok": False, "error": str(exc)})
    finally:
        conn.close()
