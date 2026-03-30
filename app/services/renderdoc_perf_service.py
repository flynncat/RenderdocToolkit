from __future__ import annotations

import json
import multiprocessing
import re
import subprocess
from collections import defaultdict
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.services.renderdoc_direct_replay import RenderdocDirectReplay
from app.services.renderdoc_perf_store import RenderdocPerfStore


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

    def create_job(self, title: str) -> Dict[str, Any]:
        return self.store.create_job(title)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return self.store.list_jobs()

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        return self.store.get_job_detail(job_id)

    def analyze_capture_isolated(self, job_id: str, capture_path: Path) -> Dict[str, Any]:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_perf_worker_entry,
            args=(str(self.store.session_root), job_id, str(capture_path), child_conn),
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
            self.store.update_metadata(job_id, {"status": "failed"})
            raise RuntimeError(f"性能分析子进程异常退出，exit_code={process.exitcode}")
        if result and not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "性能分析失败"))
        return self.store.get_job_detail(job_id)

    def analyze_capture(self, job_id: str, capture_path: Path) -> Dict[str, Any]:
        job_dir = self.store.job_path(job_id)
        preview_dir = job_dir / "artifacts" / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        run_log_lines = [f"[capture] {capture_path}"]

        draws_payload = self._load_draws_payload(capture_path)
        counters_payload = self._load_counters_payload(capture_path)
        draw_rows = self._extract_draw_rows(draws_payload)
        counter_map = self._extract_counter_map(counters_payload)

        with RenderdocDirectReplay(capture_path) as replay:
            capture_info = replay.get_capture_metadata()
            texture_desc_map = replay.get_texture_description_map()
            gpu_duration_map = replay.fetch_counter_map(["GPU Duration"])
            for eid, values in gpu_duration_map.items():
                counter_map.setdefault(eid, {}).update(values)
            action_map = self._collect_action_map(replay)
            rows = self._build_rows(
                replay=replay,
                draw_rows=draw_rows,
                counter_map=counter_map,
                action_map=action_map,
                texture_desc_map=texture_desc_map,
                run_log_lines=run_log_lines,
            )
            self._populate_initial_draw_previews(
                replay=replay,
                job_id=job_id,
                preview_dir=preview_dir,
                rows=rows,
                run_log_lines=run_log_lines,
            )

        overview = self._build_overview(rows)
        pass_chart = self._build_pass_chart(rows)
        hotspot_hints = self._build_hotspot_hints(pass_chart, rows)
        warnings = self._build_warnings(capture_info)
        analysis = {
            "capture_name": capture_path.name,
            "capture_path": str(capture_path),
            "capture_info": capture_info,
            "overview": overview,
            "warnings": warnings,
            "sort_fields": [
                {"id": "stable_sort_score", "label": "稳定得分(估算)"},
                {"id": "screen_coverage_percent", "label": "屏幕覆盖率(估算%)"},
                {"id": "gpu_duration_ms", "label": "GPU耗时"},
                {"id": "triangles", "label": "三角面数"},
                {"id": "vertices_read", "label": "顶点数量"},
                {"id": "input_primitives", "label": "输入图元"},
                {"id": "instruction_total", "label": "总指令数"},
                {"id": "ps_instruction_count", "label": "PS指令数"},
                {"id": "vs_instruction_count", "label": "VS指令数"},
                {"id": "ps_invocations", "label": "PS调用数"},
                {"id": "vs_invocations", "label": "VS调用数"},
                {"id": "texture_count", "label": "贴图数量"},
                {"id": "texture_total_mb", "label": "贴图总大小(MB)"},
                {"id": "texture_bandwidth_risk", "label": "纹理带宽风险(估算)"},
            ],
            "rows": rows,
            "pass_chart": pass_chart,
            "hotspot_hints": hotspot_hints,
        }

        self.store.write_json_artifact(job_id, "artifacts/perf_analysis.json", analysis)
        self.store.write_text_artifact(job_id, "artifacts/perf_run_log.txt", "\n".join(run_log_lines) + "\n")
        metadata = self.store.update_metadata(
            job_id,
            {
                "status": "completed",
                "inputs": {
                    "capture_file": str(capture_path),
                },
                "summary": {
                    "row_count": len(rows),
                    "total_gpu_duration_ms": overview["total_gpu_duration_ms"],
                    "hottest_pass": pass_chart[0]["name"] if pass_chart else "",
                },
            },
        )
        detail = self.store.get_job_detail(job_id)
        detail["metadata"] = metadata
        return detail

    def generate_draw_preview(self, job_id: str, eid: str) -> Dict[str, str]:
        detail = self.store.get_job_detail(job_id)
        capture_path = self._resolve_capture_path(job_id, detail)
        preview_dir = self.store.job_path(job_id) / "artifacts" / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        output_path = preview_dir / f"wireframe_{eid}.png"

        if not output_path.exists():
            with RenderdocDirectReplay(capture_path) as replay:
                saved = replay.save_draw_wireframe_preview(
                    eid=eid,
                    output_path=output_path,
                )
            if saved is None or not saved.exists():
                raise RuntimeError(f"无法生成 EID {eid} 的线框预览")

        rel = output_path.relative_to(self.store.job_path(job_id)).as_posix()
        url = f"/perf-session-files/{job_id}/{rel}"

        analysis = detail.get("analysis") or {}
        rows = analysis.get("rows") or []
        changed = False
        for row in rows:
            if self._stringify(row.get("eid")) == self._stringify(eid):
                row["draw_preview_url"] = url
                row["draw_preview_kind"] = "wireframe_overlay"
                changed = True
                break
        if changed:
            self.store.write_json_artifact(job_id, "artifacts/perf_analysis.json", analysis)

        return {
            "eid": self._stringify(eid),
            "url": url,
            "kind": "wireframe_overlay",
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
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        shader_cache: Dict[str, Dict[str, Any]] = {}

        for draw in draw_rows:
            eid = self._stringify(draw.get("eid"))
            if not eid:
                continue
            metadata = action_map.get(eid, {})
            counters = counter_map.get(eid, {})
            replay._set_frame_event(int(eid))
            pipe = replay.controller.GetPipelineState()

            shader_metrics = self._get_shader_metrics(replay, pipe, shader_cache)
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
            scene_pass = self._normalize_scene_pass_name(self._stringify(metadata.get("scene_pass")))
            triangle_count = int(draw.get("triangles") or 0)
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
            row = {
                "eid": eid,
                "scene_pass": scene_pass or "Other",
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

    def _populate_initial_draw_previews(
        self,
        *,
        replay: RenderdocDirectReplay,
        job_id: str,
        preview_dir: Path,
        rows: List[Dict[str, Any]],
        run_log_lines: List[str],
        limit: int = 8,
    ) -> None:
        for row in rows[:limit]:
            eid = self._stringify(row.get("eid"))
            if not eid:
                continue
            output_path = preview_dir / f"wireframe_{eid}.png"
            saved = replay.save_draw_wireframe_preview(
                eid=eid,
                output_path=output_path,
            )
            if saved is None or not saved.exists():
                continue
            rel = saved.relative_to(self.store.job_path(job_id)).as_posix()
            row["draw_preview_url"] = f"/perf-session-files/{job_id}/{rel}"
            row["draw_preview_kind"] = "wireframe_overlay"
            run_log_lines.append(f"[wireframe] eid={eid} saved={row['draw_preview_url']}")

    def _collect_action_map(self, replay: RenderdocDirectReplay) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}

        def walk(
            actions: Iterable[Any],
            ancestors: List[str],
            *,
            inside_mobile: bool,
            mobile_pass: str,
            parent_is_mobile_root: bool,
        ) -> None:
            for action in actions:
                name = self._stringify(getattr(action, "customName", ""))
                named_ancestors = ancestors + ([name] if name else [])
                current_inside_mobile = inside_mobile or name == "MobileSceneRender"
                current_mobile_pass = mobile_pass
                if parent_is_mobile_root and name:
                    current_mobile_pass = name
                event_id = self._stringify(getattr(action, "eventId", ""))
                if event_id:
                    nearest_name = named_ancestors[-1] if named_ancestors else ""
                    if current_inside_mobile and nearest_name == "MobileSceneRender":
                        nearest_name = ""
                    result[event_id] = {
                        "pass_name": nearest_name,
                        "scene_pass": current_mobile_pass,
                        "breadcrumbs": named_ancestors,
                    }
                walk(
                    getattr(action, "children", []),
                    named_ancestors,
                    inside_mobile=current_inside_mobile,
                    mobile_pass=current_mobile_pass,
                    parent_is_mobile_root=name == "MobileSceneRender",
                )

        walk(replay.controller.GetRootActions(), [], inside_mobile=False, mobile_pass="", parent_is_mobile_root=False)
        return result

    def _get_shader_metrics(self, replay: RenderdocDirectReplay, pipe: Any, shader_cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        metrics = {
            "vs_instruction_count": 0,
            "ps_instruction_count": 0,
            "instruction_total": 0,
            "shader_ids": {},
        }
        pipeline_object = pipe.GetGraphicsPipelineObject()
        for stage_name, stage_enum in (("vs", replay.rd.ShaderStage.Vertex), ("ps", replay.rd.ShaderStage.Pixel)):
            shader_id = str(pipe.GetShader(stage_enum))
            metrics["shader_ids"][stage_name] = shader_id
            if not shader_id or shader_id.endswith("::0"):
                continue
            if shader_id not in shader_cache:
                refl = pipe.GetShaderReflection(stage_enum)
                count = 0
                try:
                    disassembly = replay.controller.DisassembleShader(pipeline_object, refl, "DXBC")
                    count = self._count_shader_instructions(disassembly)
                except Exception:
                    count = 0
                shader_cache[shader_id] = {"instruction_count": count}
            metrics[f"{stage_name}_instruction_count"] = int(shader_cache[shader_id]["instruction_count"])
        metrics["instruction_total"] = metrics["vs_instruction_count"] + metrics["ps_instruction_count"]
        return metrics

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
    def _count_shader_instructions(disassembly: str) -> int:
        return sum(1 for line in (disassembly or "").splitlines() if re.match(r"^\s*\d+:", line))

    @staticmethod
    def _normalize_scene_pass_name(name: str) -> str:
        text = (name or "").strip()
        if not text:
            return ""
        lowers = text.lower()
        known = {
            "shadowdepths": "ShadowDepths",
            "mobilerenderprepass": "MobileRenderPrePass",
            "mobilebasepass": "MobileBasePass",
            "translucency": "Translucency",
            "postprocessing": "PostProcessing",
        }
        for key, value in known.items():
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
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return proc.returncode, output.strip()


def _perf_worker_entry(session_root: str, job_id: str, capture_path: str, conn: Connection) -> None:
    store = RenderdocPerfStore(Path(session_root))
    service = RenderdocPerfService(store)
    try:
        service.analyze_capture(job_id, Path(capture_path))
        conn.send({"ok": True})
    except Exception as exc:
        store.update_metadata(job_id, {"status": "failed"})
        conn.send({"ok": False, "error": str(exc)})
    finally:
        conn.close()
