from __future__ import annotations

import csv
import json
import multiprocessing
import re
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.asset_export_store import AssetExportStore
from app.services.csv_model_converter import CsvModelConverter
from app.services.renderdoc_direct_replay import RenderdocDirectReplay

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


def _normalize_json_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


class AssetExportService:
    def __init__(self, store: AssetExportStore, converter: CsvModelConverter) -> None:
        self.store = store
        self.converter = converter

    def scan_passes(self, capture_path: Path) -> List[Dict[str, Any]]:
        self._open_capture(capture_path)
        try:
            return self.scan_passes_in_current_capture()
        finally:
            self._close_capture()

    def run_export(
        self,
        *,
        job_id: str,
        capture_path: Path,
        output_root: Path,
        export_scope: str,
        pass_id: str,
        pass_name: str,
        pass_start_id: str,
        pass_start: str,
        pass_end_id: str,
        pass_end: str,
        export_fbx: bool,
        export_obj: bool,
        texture_format: str,
        mapping_override: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        job_dir = self.store.job_path(job_id)
        output_root.mkdir(parents=True, exist_ok=True)
        manifest: Dict[str, Any] = {
            "capture_file": str(capture_path.name),
            "output_root": str(output_root),
            "selected_passes": [],
            "texture_format_requested": texture_format,
            "texture_format_effective": "png" if texture_format.lower() in {"hdr", "exr"} else texture_format.lower(),
            "notes": [],
            "items": [],
            "export_mapping": dict(mapping_override or {}),
        }
        failed_items: List[Dict[str, Any]] = []
        model_files: List[str] = []
        csv_files: List[str] = []
        texture_files: List[str] = []

        self.store.write_text_artifact(job_id, "artifacts/job.log", "资产导出开始\n")
        self.store.update_metadata(
            job_id,
            {
                "status": "running",
                "progress": {
                    "stage": "opening_capture",
                    "message": "正在打开 RenderDoc capture。",
                    "current": 0,
                    "total": 0,
                },
            },
        )

        self._open_capture(capture_path)
        try:
            all_passes = self.scan_passes_in_current_capture()
            selected_passes = self._select_passes(
                all_passes,
                export_scope,
                pass_id,
                pass_name,
                pass_start_id,
                pass_start,
                pass_end_id,
                pass_end,
            )
            manifest["selected_passes"] = [item["display_name"] for item in selected_passes]
            self.store.update_metadata(
                job_id,
                {
                    "progress": {
                        "stage": "enumerating_passes",
                        "message": f"已选中 {len(selected_passes)} 个 Pass。",
                        "current": 0,
                        "total": len(selected_passes),
                    },
                    "result": {
                        "selected_passes": manifest["selected_passes"],
                    },
                },
            )

            resources = self._run_json(["rdc", "resources", "--json"])
            texture_resources = self._extract_texture_resources(resources)
            exported_texture_paths: Dict[Tuple[str, str], str] = {}
            all_draws = self._extract_draws(self._run_json(["rdc", "draws", "--json"]))

            direct_replay: Optional[RenderdocDirectReplay] = None
            direct_replay_error = ""
            try:
                direct_replay = RenderdocDirectReplay(capture_path)
                direct_replay.__enter__()
            except Exception as exc:
                direct_replay_error = str(exc)
                manifest["notes"].append(f"未能初始化直接 RenderDoc replay，纹理导出将回退到 CLI：{exc}")

            try:
                for pass_offset, pass_item in enumerate(selected_passes, start=1):
                    pass_slug = f"{pass_item['index']:03d}_{self._slugify(pass_item['name'])}"
                    pass_manifest: Dict[str, Any] = {
                        "pass_index": pass_item["index"],
                        "pass_id": pass_item["id"],
                        "pass_source": pass_item["source"],
                        "pass_name": pass_item["name"],
                        "pass_display_name": pass_item["display_name"],
                        "draws": [],
                    }
                    manifest["items"].append(pass_manifest)

                    self.store.update_metadata(
                        job_id,
                        {
                            "progress": {
                                "stage": "exporting_pass",
                                "message": f"正在导出 Pass: {pass_item['display_name']}",
                                "current": pass_offset,
                                "total": len(selected_passes),
                            }
                        },
                    )
                    self._append_log(job_id, f"[pass] {pass_item['display_name']}")

                    draws = self._resolve_draws_for_pass(pass_item, all_draws)
                    for draw_item in draws:
                        eid = self._extract_eid(draw_item)
                        if not eid:
                            continue
                        draw_label = self._extract_draw_label(draw_item)
                        draw_slug = f"eid_{eid}_{self._slugify(draw_label)[:48]}".rstrip("_")
                        draw_manifest: Dict[str, Any] = {
                            "eid": eid,
                            "label": draw_label,
                            "mesh_obj": "",
                            "mesh_fbx": "",
                            "mesh_csv": "",
                            "textures": [],
                        }
                        pass_manifest["draws"].append(draw_manifest)

                        model_dir = output_root / "models" / pass_slug
                        csv_dir = output_root / "csv" / pass_slug
                        texture_dir = output_root / "textures" / pass_slug
                        model_dir.mkdir(parents=True, exist_ok=True)
                        csv_dir.mkdir(parents=True, exist_ok=True)
                        texture_dir.mkdir(parents=True, exist_ok=True)

                        mesh_obj_path = model_dir / f"{draw_slug}.obj"
                        mesh_csv_path = csv_dir / f"{draw_slug}.csv"
                        mesh_fbx_path = model_dir / f"{draw_slug}.fbx"
                        if direct_replay is None:
                            failed_items.append(
                                {
                                    "type": "mesh-export",
                                    "pass_name": pass_item["name"],
                                    "eid": eid,
                                    "reason": direct_replay_error or "direct replay unavailable",
                                }
                            )
                            self._append_log(job_id, f"[mesh-failed] eid={eid} export={direct_replay_error or 'direct replay unavailable'}")
                        else:
                            try:
                                export_info = direct_replay.export_vsin_csv(eid=str(eid), output_path=mesh_csv_path)
                                draw_manifest["mesh_csv"] = self._artifact_ref(job_dir, mesh_csv_path)
                                draw_manifest["mesh_stage"] = str(export_info.get("stage") or "vsin")
                                suggested_mapping = self.converter.suggest_mapping(mesh_csv_path)
                                draw_manifest["mapping_suggested"] = suggested_mapping.to_dict()
                                csv_files.append(draw_manifest["mesh_csv"])

                                for skipped_attr in list(export_info.get("skipped_attributes") or []):
                                    manifest["notes"].append(f"EID {eid} 顶点属性已跳过：{skipped_attr}")

                                mapping, mapping_notes = self._resolve_export_mapping(
                                    mesh_csv_path=mesh_csv_path,
                                    suggested_mapping=suggested_mapping,
                                    override_mapping=mapping_override or {},
                                )
                                draw_manifest["mapping_applied"] = mapping.to_dict()
                                for note in mapping_notes:
                                    manifest["notes"].append(f"EID {eid} {note}")
                                if export_obj:
                                    self.converter.convert(
                                        csv_path=mesh_csv_path,
                                        output_path=mesh_obj_path,
                                        mapping=mapping,
                                        fmt="obj",
                                    )
                                    draw_manifest["mesh_obj"] = self._artifact_ref(job_dir, mesh_obj_path)
                                    model_files.append(draw_manifest["mesh_obj"])
                                if export_fbx:
                                    self.converter.convert(
                                        csv_path=mesh_csv_path,
                                        output_path=mesh_fbx_path,
                                        mapping=mapping,
                                        fmt="fbx",
                                    )
                                    draw_manifest["mesh_fbx"] = self._artifact_ref(job_dir, mesh_fbx_path)
                                    model_files.append(draw_manifest["mesh_fbx"])
                            except Exception as exc:
                                failed_items.append(
                                    {
                                        "type": "mesh-convert",
                                        "pass_name": pass_item["name"],
                                        "eid": eid,
                                        "reason": str(exc),
                                    }
                                )
                                self._append_log(job_id, f"[mesh-failed] eid={eid} convert={exc}")

                        try:
                            bindings_payload = self._run_json(["rdc", "bindings", str(eid), "--json"])
                            texture_bindings = self._extract_texture_bindings(str(eid), bindings_payload, texture_resources)
                            for binding in texture_bindings:
                                export_key = (pass_slug, binding["id"])
                                if export_key in exported_texture_paths:
                                    draw_manifest["textures"].append(exported_texture_paths[export_key])
                                    continue

                                texture_path = self._export_texture(
                                    replay=direct_replay,
                                    eid=str(eid),
                                    stage=binding["stage"],
                                    slot=binding["slot"],
                                    texture_id=binding["id"],
                                    texture_dir=texture_dir,
                                    base_name=f"{binding['slot_label']}_{binding['name']}",
                                    preferred_format=texture_format,
                                    notes=manifest["notes"],
                                )
                                relative_texture_path = self._artifact_ref(job_dir, texture_path)
                                exported_texture_paths[export_key] = relative_texture_path
                                binding["export_path"] = relative_texture_path
                                draw_manifest["textures"].append(relative_texture_path)
                                texture_files.append(relative_texture_path)
                        except Exception as exc:
                            if direct_replay_error:
                                reason = f"{exc} | replay init: {direct_replay_error}"
                            else:
                                reason = str(exc)
                            failed_items.append(
                                {
                                    "type": "texture-export",
                                    "pass_name": pass_item["name"],
                                    "eid": eid,
                                    "reason": reason,
                                }
                            )
                            self._append_log(job_id, f"[texture-failed] eid={eid} error={reason}")
            finally:
                if direct_replay is not None:
                    direct_replay.__exit__(None, None, None)

            self.store.write_json_artifact(job_id, "artifacts/manifest.json", manifest)
            metadata = self.store.update_metadata(
                job_id,
                {
                    "status": "completed",
                    "progress": {
                        "stage": "completed",
                        "message": f"资产导出完成，Pass={len(selected_passes)}。",
                        "current": len(selected_passes),
                        "total": len(selected_passes),
                    },
                    "result": {
                        "output_root": str(output_root),
                        "selected_passes": manifest["selected_passes"],
                        "csv_files": csv_files,
                        "model_files": model_files,
                        "texture_files": texture_files,
                        "failed_items": failed_items,
                    },
                },
            )
            return {
                "metadata": metadata,
                "manifest": manifest,
                "job_log": (job_dir / "artifacts" / "job.log").read_text(encoding="utf-8", errors="replace"),
                "mapping": {},
            }
        finally:
            self._close_capture()

    def preview_export_mapping(
        self,
        *,
        capture_path: Path,
        export_scope: str,
        pass_id: str,
        pass_name: str,
        pass_start_id: str,
        pass_start: str,
        pass_end_id: str,
        pass_end: str,
    ) -> Dict[str, Any]:
        self._open_capture(capture_path)
        try:
            all_passes = self.scan_passes_in_current_capture()
            selected_passes = self._select_passes(
                all_passes,
                export_scope,
                pass_id,
                pass_name,
                pass_start_id,
                pass_start,
                pass_end_id,
                pass_end,
            )
            all_draws = self._extract_draws(self._run_json(["rdc", "draws", "--json"]))
            sample_pass = None
            sample_draw = None
            for pass_item in selected_passes:
                draws = self._resolve_draws_for_pass(pass_item, all_draws)
                for draw_item in draws:
                    if self._extract_eid(draw_item):
                        sample_pass = pass_item
                        sample_draw = draw_item
                        break
                if sample_draw is not None:
                    break
            if sample_pass is None or sample_draw is None:
                raise RuntimeError("当前导出范围内未找到可用于映射确认的 draw")

            eid = self._extract_eid(sample_draw) or ""
            draw_label = self._extract_draw_label(sample_draw)
            with tempfile.NamedTemporaryFile("w+b", suffix=".csv", delete=False) as handle:
                temp_csv_path = Path(handle.name)
            try:
                with RenderdocDirectReplay(capture_path) as replay:
                    export_info = replay.export_vsin_csv(eid=eid, output_path=temp_csv_path)
                rows = self.converter._read_rows(temp_csv_path)
                headers = rows[0] if rows else []
                suggested_mapping = self.converter.suggest_mapping(temp_csv_path).to_dict()
            finally:
                temp_csv_path.unlink(missing_ok=True)

            return {
                "sample_pass": sample_pass.get("display_name") or sample_pass.get("selection_label") or sample_pass.get("name") or "",
                "sample_eid": eid,
                "sample_draw_label": draw_label,
                "sample_stage": str(export_info.get("stage") or "vsin"),
                "headers": headers,
                "suggested_mapping": suggested_mapping,
                "selected_passes": [item.get("display_name") or item.get("selection_label") or item.get("name") or "" for item in selected_passes],
                "skipped_attributes": list(export_info.get("skipped_attributes") or []),
            }
        finally:
            self._close_capture()

    def preview_export_mapping_isolated(
        self,
        *,
        capture_path: Path,
        export_scope: str,
        pass_id: str,
        pass_name: str,
        pass_start_id: str,
        pass_start: str,
        pass_end_id: str,
        pass_end: str,
    ) -> Dict[str, Any]:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_asset_mapping_preview_worker,
            args=(
                child_conn,
                str(capture_path),
                export_scope,
                pass_id,
                pass_name,
                pass_start_id,
                pass_start,
                pass_end_id,
                pass_end,
            ),
            daemon=False,
        )
        process.start()
        child_conn.close()
        try:
            message = parent_conn.recv()
        finally:
            parent_conn.close()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        if not isinstance(message, dict):
            raise RuntimeError("批量映射预览子进程返回了无效结果")
        if message.get("ok"):
            return message.get("result") or {}
        raise RuntimeError(str(message.get("error") or "批量映射预览失败"))

    def scan_passes_in_current_capture(self) -> List[Dict[str, Any]]:
        pass_payload = self._run_json(["rdc", "passes", "--json"])
        draw_payload = self._run_json(["rdc", "draws", "--json"])
        pass_items = pass_payload if isinstance(pass_payload, list) else (pass_payload or {}).get("passes", [])
        draws = self._extract_draws(draw_payload)

        merged: List[Dict[str, Any]] = []
        seen_names: set[str] = set()

        for item in self._build_marker_passes_from_draws(draws):
            seen_names.add(self._normalize_name(item["name"]))
            merged.append(item)

        render_passes = [
            {
                "index": -1,
                "id": f"render-pass:{index}:{self._slugify(self._extract_pass_name(item, index)) or index}",
                "name": self._extract_pass_name(item, index),
                "display_name": f"{self._extract_pass_name(item, index)} [RenderPass]",
                "selection_label": "",
                "source": "render-pass",
                "first_eid": self._stringify(item.get("begin_eid")) if isinstance(item, dict) else "",
                "last_eid": self._stringify(item.get("end_eid")) if isinstance(item, dict) else "",
                "raw": item,
            }
            for index, item in enumerate(pass_items)
        ]
        for item in render_passes:
            normalized = self._normalize_name(item["name"])
            if normalized in seen_names:
                continue
            seen_names.add(normalized)
            merged.append(item)

        for index, item in enumerate(merged):
            item["index"] = index
            if not item.get("selection_label"):
                item["selection_label"] = self._build_pass_selection_label(item)
        return merged

    def _run(self, args: List[str]) -> Tuple[int, str]:
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

    def _run_json(self, args: List[str]) -> Any:
        rc, output = self._run(args)
        if rc != 0:
            raise RuntimeError(output or "command failed")
        return _normalize_json_text(output)

    def _open_capture(self, capture_path: Path) -> None:
        self._run(["rdc", "close"])
        rc, output = self._run(["rdc", "open", str(capture_path)])
        if rc != 0:
            raise RuntimeError(output or f"无法打开 capture: {capture_path}")

    def _close_capture(self) -> None:
        self._run(["rdc", "close"])

    def _append_log(self, job_id: str, line: str) -> None:
        log_path = self.store.job_path(job_id) / "artifacts" / "job.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")

    @staticmethod
    def _extract_pass_name(item: Any, index: int) -> str:
        if isinstance(item, dict):
            return str(
                item.get("pass_name")
                or item.get("name")
                or item.get("label")
                or item.get("title")
                or f"pass_{index}"
            )
        return str(item)

    def _select_passes(
        self,
        all_passes: List[Dict[str, Any]],
        export_scope: str,
        pass_id: str,
        pass_name: str,
        pass_start_id: str,
        pass_start: str,
        pass_end_id: str,
        pass_end: str,
    ) -> List[Dict[str, Any]]:
        if not all_passes:
            raise RuntimeError("capture 中没有可用 Pass")
        if export_scope == "range":
            start_token = pass_start_id or pass_start
            end_token = pass_end_id or pass_end
            start_index = self._find_pass_index(all_passes, start_token)
            end_index = self._find_pass_index(all_passes, end_token)
            if start_index is not None and end_index is not None:
                if start_index > end_index:
                    start_index, end_index = end_index, start_index
                return all_passes[start_index : end_index + 1]

            start_eid = self._resolve_pass_endpoint_eid(
                all_passes,
                start_token,
                prefer_last=False,
            )
            end_eid = self._resolve_pass_endpoint_eid(
                all_passes,
                end_token,
                prefer_last=True,
            )
            if start_eid is None or end_eid is None:
                raise RuntimeError("Pass 区间选择无效")
            if start_eid > end_eid:
                start_eid, end_eid = end_eid, start_eid
            return [self._build_manual_range_item(start_eid, end_eid)]

        selected_index = self._find_pass_index(all_passes, pass_id or pass_name)
        if selected_index is None:
            manual_eid = self._parse_eid_value(pass_id or pass_name)
            if manual_eid is None:
                raise RuntimeError(f"未找到 Pass: {pass_name or pass_id}")
            return [self._build_manual_eid_item(manual_eid)]
        return [all_passes[selected_index]]

    @staticmethod
    def _find_pass_index(all_passes: List[Dict[str, Any]], target_name: str) -> Optional[int]:
        target_name = (target_name or "").strip()
        for index, item in enumerate(all_passes):
            if (
                item.get("id") == target_name
                or item["name"] == target_name
                or item.get("display_name") == target_name
                or item.get("selection_label") == target_name
            ):
                return index
        return None

    def _resolve_draws_for_pass(self, pass_item: Dict[str, Any], all_draws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if pass_item.get("source") == "manual-eid":
            target_eid = int(pass_item.get("raw", {}).get("eid") or 0)
            return [draw for draw in all_draws if self._parse_eid_value(self._extract_eid(draw)) == target_eid]
        if pass_item.get("source") == "manual-range":
            raw = pass_item.get("raw") if isinstance(pass_item.get("raw"), dict) else {}
            begin_eid = int(raw.get("begin_eid") or 0)
            end_eid = int(raw.get("end_eid") or 0)
            return self._filter_draws_by_eid_range(all_draws, begin_eid, end_eid)
        if pass_item.get("source") == "render-pass":
            raw = pass_item.get("raw") if isinstance(pass_item.get("raw"), dict) else {}
            begin_eid = int(raw.get("begin_eid") or 0)
            end_eid = int(raw.get("end_eid") or 0)
            if begin_eid and end_eid:
                ranged = self._filter_draws_by_eid_range(all_draws, begin_eid, end_eid)
                if ranged:
                    return ranged
            try:
                draws_payload = self._run_json(["rdc", "draws", "--pass", pass_item["name"], "--json"])
                draws = self._extract_draws(draws_payload)
                if draws:
                    return draws
            except Exception:
                pass
        target_name = pass_item.get("name", "")
        return [draw for draw in all_draws if self._extract_draw_pass_group(draw) == target_name]

    @staticmethod
    def _extract_draws(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("draws", "items", "events"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_eid(draw_item: Dict[str, Any]) -> Optional[str]:
        for key in ("eid", "Event", "event", "event_id"):
            value = draw_item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _extract_draw_label(draw_item: Dict[str, Any]) -> str:
        for key in ("Marker", "marker", "Name", "name", "Draw", "draw"):
            value = draw_item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return "draw"

    @staticmethod
    def _extract_draw_pass_group(draw_item: Dict[str, Any]) -> str:
        for key in ("Marker", "marker", "Pass", "pass", "pass_name", "PassName", "marker_name"):
            value = draw_item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _build_marker_passes_from_draws(self, draws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        marker_items: List[Dict[str, Any]] = []
        marker_map: Dict[str, Dict[str, Any]] = {}
        for draw in draws:
            marker_name = self._extract_draw_pass_group(draw)
            if not self._is_useful_marker_name(marker_name):
                continue
            marker_id = f"marker:{self._slugify(marker_name) or 'marker'}"
            eid = self._extract_eid(draw)
            if marker_id not in marker_map:
                item = {
                    "index": -1,
                    "id": marker_id,
                    "name": marker_name,
                    "display_name": f"{marker_name} [Marker]",
                    "selection_label": "",
                    "source": "marker",
                    "first_eid": eid or "",
                    "last_eid": eid or "",
                    "draw_count": 0,
                    "raw": {"marker": marker_name},
                }
                marker_map[marker_id] = item
                marker_items.append(item)
            item = marker_map[marker_id]
            item["draw_count"] += 1
            if eid:
                if not item["first_eid"]:
                    item["first_eid"] = eid
                item["last_eid"] = eid
        return marker_items

    def _resolve_export_mapping(
        self,
        *,
        mesh_csv_path: Path,
        suggested_mapping: Any,
        override_mapping: Dict[str, str],
    ) -> Tuple[Any, List[str]]:
        headers = self.converter.read_headers(mesh_csv_path)
        header_set = set(headers)
        merged = suggested_mapping.to_dict()
        notes: List[str] = []
        for field_name, raw_value in (override_mapping or {}).items():
            selected = str(raw_value or "").strip()
            if not selected:
                continue
            if selected in header_set:
                merged[field_name] = selected
            else:
                notes.append(f"批量映射字段 `{field_name}` 选择的列 `{selected}` 在当前 CSV 中不存在，已回退自动识别。")
        return type(suggested_mapping)(**merged), notes

    def _resolve_pass_endpoint_eid(
        self,
        all_passes: List[Dict[str, Any]],
        target_name: str,
        *,
        prefer_last: bool,
    ) -> Optional[int]:
        target_name = (target_name or "").strip()
        pass_index = self._find_pass_index(all_passes, target_name)
        if pass_index is not None:
            item = all_passes[pass_index]
            key = "last_eid" if prefer_last else "first_eid"
            eid = self._parse_eid_value(item.get(key))
            if eid is not None:
                return eid
        return self._parse_eid_value(target_name)

    @staticmethod
    def _parse_eid_value(value: Any) -> Optional[int]:
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"\b(\d+)\b", text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _filter_draws_by_eid_range(self, all_draws: List[Dict[str, Any]], begin_eid: int, end_eid: int) -> List[Dict[str, Any]]:
        ranged: List[Dict[str, Any]] = []
        for draw in all_draws:
            eid = self._extract_eid(draw)
            eid_value = self._parse_eid_value(eid)
            if eid_value is None:
                continue
            if begin_eid <= eid_value <= end_eid:
                ranged.append(draw)
        return ranged

    @staticmethod
    def _build_manual_eid_item(eid: int) -> Dict[str, Any]:
        return {
            "index": -1,
            "id": f"manual-eid:{eid}",
            "name": f"EID {eid}",
            "display_name": f"EID {eid} [Manual]",
            "selection_label": f"EID {eid} | Manual",
            "source": "manual-eid",
            "first_eid": str(eid),
            "last_eid": str(eid),
            "draw_count": 1,
            "raw": {"eid": eid},
        }

    @staticmethod
    def _build_manual_range_item(begin_eid: int, end_eid: int) -> Dict[str, Any]:
        return {
            "index": -1,
            "id": f"manual-range:{begin_eid}-{end_eid}",
            "name": f"EID {begin_eid}-{end_eid}",
            "display_name": f"EID {begin_eid}-{end_eid} [Manual]",
            "selection_label": f"EID {begin_eid}-{end_eid} | Manual Range",
            "source": "manual-range",
            "first_eid": str(begin_eid),
            "last_eid": str(end_eid),
            "draw_count": 0,
            "raw": {"begin_eid": begin_eid, "end_eid": end_eid},
        }

    @staticmethod
    def _build_pass_selection_label(item: Dict[str, Any]) -> str:
        name = str(item.get("name") or item.get("display_name") or "").strip()
        first_eid = str(item.get("first_eid") or "").strip()
        last_eid = str(item.get("last_eid") or "").strip()
        if item.get("source") == "render-pass" and first_eid and last_eid and first_eid != last_eid:
            return f"EID {first_eid}-{last_eid} | {name}"
        if first_eid:
            return f"EID {first_eid} | {name}"
        return name

    def _extract_texture_resources(self, payload: Any) -> Dict[str, Dict[str, Any]]:
        items: Iterable[Any] = payload if isinstance(payload, list) else (payload or {}).get("resources", [])
        result: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            resource_id = self._stringify(item.get("id") or item.get("ID") or item.get("resource_id"))
            resource_type = self._stringify(item.get("type") or item.get("Type"))
            if not resource_id:
                continue
            if "texture" not in resource_type.lower():
                continue
            result[resource_id] = {
                "id": resource_id,
                "name": self._stringify(item.get("name") or item.get("Name") or f"texture_{resource_id}") or f"texture_{resource_id}",
                "type": resource_type,
            }
        return result

    def _extract_texture_bindings(
        self,
        eid: str,
        payload: Any,
        texture_resources: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        slot_items = self._extract_binding_slots(payload)
        resolved_items = self._resolve_texture_binding_ids(eid)
        result: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in resolved_items:
            resource_id = self._stringify(item.get("id"))
            if not resource_id or resource_id in seen_ids:
                continue
            info = texture_resources.get(resource_id)
            if not info:
                continue
            seen_ids.add(resource_id)
            stage = self._stringify(item.get("stage")) or "stage"
            slot = self._stringify(item.get("slot")) or "slot"
            slot_info = slot_items.get((stage, slot), {})
            result.append(
                {
                    "id": resource_id,
                    "stage": stage,
                    "slot": slot,
                    "name": self._slugify(self._stringify(slot_info.get("name") or info["name"]) or f"texture_{resource_id}"),
                    "slot_label": self._slugify(f"{stage}_{slot}") or f"{stage}_{slot}",
                    "export_path": "",
                }
            )

        if result:
            return result

        items: Iterable[Any] = payload if isinstance(payload, list) else (payload or {}).get("bindings", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            resource_id = self._stringify(
                item.get("id")
                or item.get("ID")
                or item.get("resource_id")
                or item.get("resourceId")
                or item.get("resource")
            )
            if not resource_id or resource_id in seen_ids:
                continue
            info = texture_resources.get(resource_id)
            if not info:
                continue
            seen_ids.add(resource_id)
            slot = self._stringify(item.get("slot") or item.get("binding") or item.get("bind")) or "slot"
            stage = self._stringify(item.get("stage") or item.get("Stage")) or "stage"
            result.append(
                {
                    "id": resource_id,
                    "stage": stage,
                    "slot": slot,
                    "name": self._slugify(self._stringify(item.get("name") or info["name"]) or f"texture_{resource_id}"),
                    "slot_label": self._slugify(f"{stage}_{slot}") or f"{stage}_{slot}",
                    "export_path": "",
                }
            )
        return result

    def _extract_binding_slots(self, payload: Any) -> Dict[Tuple[str, str], Dict[str, str]]:
        items: Iterable[Any] = payload if isinstance(payload, list) else (payload or {}).get("bindings", [])
        result: Dict[Tuple[str, str], Dict[str, str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            stage = self._stringify(item.get("stage") or item.get("Stage")) or "stage"
            slot = self._stringify(item.get("slot") or item.get("binding") or item.get("bind")) or "slot"
            result[(stage, slot)] = {
                "stage": stage,
                "slot": slot,
                "name": self._stringify(item.get("name") or item.get("Name")),
                "kind": self._stringify(item.get("kind") or item.get("Kind")),
            }
        return result

    def _resolve_texture_binding_ids(self, eid: str) -> List[Dict[str, str]]:
        script = """
stage_defs = [
    ("vs", rd.ShaderStage.Vertex),
    ("hs", rd.ShaderStage.Hull),
    ("ds", rd.ShaderStage.Domain),
    ("gs", rd.ShaderStage.Geometry),
    ("ps", rd.ShaderStage.Pixel),
    ("cs", rd.ShaderStage.Compute),
]
pipe = adapter.get_pipeline_state()
items = []
for stage_name, stage_enum in stage_defs:
    try:
        descriptors = list(pipe.GetReadOnlyResources(stage_enum))
    except Exception:
        descriptors = []
    for used in descriptors:
        resid = str(used.descriptor.resource)
        if not resid or resid.endswith("Null"):
            continue
        items.append(
            {
                "stage": stage_name,
                "slot": str(used.access.index),
                "id": resid.split("::", 1)[-1],
            }
        )
result = items
""".strip()
        self._run(["rdc", "goto", str(eid)])
        with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            payload = self._run_json(["rdc", "script", str(script_path), "--json"])
        finally:
            script_path.unlink(missing_ok=True)
        items = (payload or {}).get("return_value") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        return [
            {
                "stage": self._stringify(item.get("stage")),
                "slot": self._stringify(item.get("slot")),
                "id": self._stringify(item.get("id")),
            }
            for item in items
            if isinstance(item, dict)
        ]

    def _export_texture(
        self,
        *,
        replay: Optional[RenderdocDirectReplay],
        eid: str,
        stage: str,
        slot: str,
        texture_id: str,
        texture_dir: Path,
        base_name: str,
        preferred_format: str,
        notes: List[str],
    ) -> Path:
        preferred = preferred_format.lower().strip() or "png"
        safe_name = self._slugify(base_name)[:80] or f"texture_{texture_id}"
        if replay is not None:
            direct_result = self._save_texture_via_direct_replay(
                replay=replay,
                eid=eid,
                stage=stage,
                slot=slot,
                texture_id=texture_id,
                texture_dir=texture_dir,
                safe_name=safe_name,
                preferred=preferred,
                notes=notes,
            )
            if direct_result is not None:
                return direct_result

        png_path = texture_dir / f"{texture_id}_{safe_name}.png"
        rc, output = self._run(["rdc", "texture", texture_id, "-o", str(png_path)])
        if rc != 0 or not png_path.exists():
            raise RuntimeError(output or f"rdc texture 失败: {texture_id}")
        return self._postprocess_saved_texture(png_path, preferred, notes)

    def _save_texture_via_direct_replay(
        self,
        *,
        replay: RenderdocDirectReplay,
        eid: str,
        stage: str,
        slot: str,
        texture_id: str,
        texture_dir: Path,
        safe_name: str,
        preferred: str,
        notes: List[str],
    ) -> Optional[Path]:
        preferred_exts = ["png", "dds"] if preferred == "png" else [preferred, "png", "dds"]
        output_root = texture_dir / f"{texture_id}_{safe_name}"
        for ext in preferred_exts:
            if ext not in {"png", "dds"}:
                continue
            output_path = output_root.with_suffix(f".{ext}")
            saved = replay.save_bound_texture(
                eid=eid,
                stage=stage,
                slot=slot,
                texture_id=texture_id,
                output_path=output_path,
                dest_ext=ext,
            )
            if saved is None:
                continue
            if ext == "dds":
                notes.append(f"纹理 `{texture_id}` 通过直接 replay 仅成功导出为 DDS，未能直接保存为 PNG。")
                return saved
            return self._postprocess_saved_texture(saved, preferred, notes)
        return None

    def _postprocess_saved_texture(self, source_path: Path, preferred: str, notes: List[str]) -> Path:
        if preferred == "png":
            return source_path
        if preferred == "tga" and Image is not None:
            target = source_path.with_suffix(".tga")
            with Image.open(source_path) as image:
                image.save(target)
            source_path.unlink(missing_ok=True)
            return target
        notes.append(f"纹理 `{source_path.name}` 请求格式 `{preferred}`，当前回退为 `{source_path.suffix}`。")
        return source_path

    @staticmethod
    def resolve_output_root(job_dir: Path, capture_source_path: str, capture_name: str) -> Path:
        source_text = (capture_source_path or "").strip()
        if source_text:
            source_path = Path(source_text).expanduser()
            if source_path.suffix.lower() != ".rdc":
                raise ValueError("RenderDoc 原始路径必须指向 .rdc 文件")
            folder_name = f"{source_path.stem}_RenderdocDiffExport"
            return source_path.parent / folder_name
        capture_stem = Path(capture_name).stem or "capture"
        return job_dir / "exports" / f"{capture_stem}_RenderdocDiffExport"

    @staticmethod
    def resolve_manual_output_dir(job_dir: Path, output_root: Path, csv_source_path: str) -> Path:
        source_text = (csv_source_path or "").strip()
        if source_text:
            source_path = Path(source_text).expanduser()
            if source_path.suffix.lower() != ".csv":
                raise ValueError("CSV 原始路径必须指向 .csv 文件")
            return source_path.parent
        return output_root / "manual_convert"

    @staticmethod
    def _artifact_ref(job_dir: Path, path: Path) -> str:
        try:
            return str(path.relative_to(job_dir)).replace("\\", "/")
        except ValueError:
            return str(path)

    @staticmethod
    def _slugify(text: str) -> str:
        text = re.sub(r"[^\w\-\.]+", "_", text.strip(), flags=re.ASCII)
        return re.sub(r"_+", "_", text).strip("._")

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _normalize_name(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).lower()

    @staticmethod
    def _is_useful_marker_name(value: str) -> bool:
        text = re.sub(r"\s+", " ", (value or "").strip())
        if not text:
            return False
        return text not in {"-", "--", "---"}


def _asset_mapping_preview_worker(
    conn: Any,
    capture_path: str,
    export_scope: str,
    pass_id: str,
    pass_name: str,
    pass_start_id: str,
    pass_start: str,
    pass_end_id: str,
    pass_end: str,
) -> None:
    try:
        service = AssetExportService(AssetExportStore(), CsvModelConverter())
        result = service.preview_export_mapping(
            capture_path=Path(capture_path),
            export_scope=export_scope,
            pass_id=pass_id,
            pass_name=pass_name,
            pass_start_id=pass_start_id,
            pass_start=pass_start,
            pass_end_id=pass_end_id,
            pass_end=pass_end,
        )
        conn.send({"ok": True, "result": result})
    except Exception as exc:
        conn.send({"ok": False, "error": f"{exc}\n{traceback.format_exc()}"})
    finally:
        conn.close()
