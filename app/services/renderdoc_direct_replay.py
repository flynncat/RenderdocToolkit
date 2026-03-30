from __future__ import annotations

import csv
import gc
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import app.config as app_config


_REPLAY_LOCK = threading.Lock()


class RenderdocDirectReplay:
    _STAGE_MAP = {
        "vs": "Vertex",
        "hs": "Hull",
        "ds": "Domain",
        "gs": "Geometry",
        "ps": "Pixel",
        "cs": "Compute",
    }

    def __init__(self, capture_path: str | Path) -> None:
        self.capture_path = Path(capture_path)
        self.rd = None
        self.cap = None
        self.controller = None
        self._current_eid: Optional[int] = None
        self._entered = False
        self._action_map: Dict[int, Any] = {}

    def __enter__(self) -> "RenderdocDirectReplay":
        _REPLAY_LOCK.acquire()
        try:
            gc.collect()
            self.rd = self._import_renderdoc()
            self.rd.InitialiseReplay(self.rd.GlobalEnvironment(), [])

            self.cap = self.rd.OpenCaptureFile()
            open_result = self.cap.OpenFile(str(self.capture_path), "", None)
            if open_result != self.rd.ResultCode.Succeeded:
                raise RuntimeError(f"OpenFile failed: {open_result}")
            if not self.cap.LocalReplaySupport():
                raise RuntimeError("Capture cannot be replayed locally")

            result, controller = self.cap.OpenCapture(self.rd.ReplayOptions(), None)
            if result != self.rd.ResultCode.Succeeded:
                raise RuntimeError(f"OpenCapture failed: {result}")
            self.controller = controller
            self._entered = True
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.controller is not None:
            try:
                self.controller.Shutdown()
            except Exception:
                pass
            self.controller = None
        if self.cap is not None:
            try:
                self.cap.Shutdown()
            except Exception:
                pass
            self.cap = None
        if self.rd is not None:
            try:
                self.rd.ShutdownReplay()
            except Exception:
                pass
            self.rd = None
        self._current_eid = None
        self._action_map = {}
        gc.collect()
        time.sleep(0.15)
        if self._entered:
            self._entered = False
            _REPLAY_LOCK.release()

    def save_bound_texture(
        self,
        *,
        eid: str | int,
        stage: str,
        slot: str | int,
        texture_id: str,
        output_path: str | Path,
        dest_ext: str = "png",
    ) -> Optional[Path]:
        if self.controller is None or self.rd is None:
            raise RuntimeError("RenderDoc replay is not opened")

        event_id = int(str(eid).strip())
        slot_index = int(str(slot).strip())
        self._set_frame_event(event_id)
        pipe = self.controller.GetPipelineState()
        stage_enum = getattr(self.rd.ShaderStage, self._STAGE_MAP.get(stage.lower(), "Pixel"))
        candidates = list(pipe.GetReadOnlyResources(stage_enum))
        target = None
        for used in candidates:
            if int(used.access.index) == slot_index and str(used.descriptor.resource).split("::", 1)[-1] == str(texture_id):
                target = used
                break
        if target is None:
            for used in candidates:
                if int(used.access.index) == slot_index:
                    target = used
                    break
        if target is None:
            return None

        output = Path(output_path)
        save = self.rd.TextureSave()
        save.resourceId = target.descriptor.resource
        save.alpha = self.rd.AlphaMapping.Preserve
        save.sample.sampleIndex = 0
        if dest_ext.lower() == "dds":
            save.destType = self.rd.FileType.DDS
            save.mip = -1
            save.slice.sliceIndex = -1
        else:
            save.destType = self.rd.FileType.PNG
            save.mip = 0
            save.slice.sliceIndex = 0

        result = self.controller.SaveTexture(save, str(output))
        if result == self.rd.ResultCode.Succeeded and output.exists():
            return output
        return None

    def get_bound_texture_data(
        self,
        *,
        eid: str | int,
        stage: str,
        slot: str | int,
        texture_id: str,
    ) -> bytes:
        if self.controller is None or self.rd is None:
            raise RuntimeError("RenderDoc replay is not opened")

        event_id = int(str(eid).strip())
        slot_index = int(str(slot).strip())
        self._set_frame_event(event_id)
        pipe = self.controller.GetPipelineState()
        stage_enum = getattr(self.rd.ShaderStage, self._STAGE_MAP.get(stage.lower(), "Pixel"))
        candidates = list(pipe.GetReadOnlyResources(stage_enum))
        target = None
        for used in candidates:
            if int(used.access.index) == slot_index and str(used.descriptor.resource).split("::", 1)[-1] == str(texture_id):
                target = used
                break
        if target is None:
            raise RuntimeError("binding texture not found")

        sub = self.rd.Subresource()
        sub.mip = 0
        sub.slice = 0
        sub.sample = 0
        return self.controller.GetTextureData(target.descriptor.resource, sub)

    def save_draw_preview(
        self,
        *,
        eid: str | int,
        output_path: str | Path,
        prefer_depth: bool = False,
    ) -> Optional[Path]:
        if self.controller is None or self.rd is None:
            raise RuntimeError("RenderDoc replay is not opened")

        event_id = int(str(eid).strip())
        self._set_frame_event(event_id)
        pipe = self.controller.GetPipelineState()

        resource_id = None
        if not prefer_depth:
            for target in list(pipe.GetOutputTargets()):
                if str(target.resource) != "ResourceId::0":
                    resource_id = target.resource
                    break
        if resource_id is None:
            depth_target = pipe.GetDepthTarget()
            if str(depth_target.resource) != "ResourceId::0":
                resource_id = depth_target.resource
        if resource_id is None:
            return None

        output = Path(output_path)
        save = self.rd.TextureSave()
        save.resourceId = resource_id
        save.destType = self.rd.FileType.PNG
        save.alpha = self.rd.AlphaMapping.BlendToCheckerboard
        save.mip = 0
        save.slice.sliceIndex = 0
        save.sample.sampleIndex = 0

        result = self.controller.SaveTexture(save, str(output))
        if result == self.rd.ResultCode.Succeeded and output.exists():
            return output
        return None

    def save_draw_wireframe_preview(
        self,
        *,
        eid: str | int,
        output_path: str | Path,
        size: int = 256,
    ) -> Optional[Path]:
        if self.controller is None or self.rd is None:
            raise RuntimeError("RenderDoc replay is not opened")

        event_id = int(str(eid).strip())
        self._set_frame_event(event_id)
        pipe = self.controller.GetPipelineState()

        target_resource = None
        for target in list(pipe.GetOutputTargets()):
            if str(target.resource) != "ResourceId::0":
                target_resource = target.resource
                break
        if target_resource is None:
            depth_target = pipe.GetDepthTarget()
            if str(depth_target.resource) != "ResourceId::0":
                target_resource = depth_target.resource
        if target_resource is None:
            return None

        replay_output = self.controller.CreateOutput(
            self.rd.CreateHeadlessWindowingData(int(size), int(size)),
            self.rd.ReplayOutputType.Texture,
        )
        try:
            display = self.rd.TextureDisplay()
            display.resourceId = target_resource
            display.typeCast = self.rd.CompType.Typeless
            display.overlay = self.rd.DebugOverlay.Wireframe
            display.subresource.mip = 0
            display.subresource.slice = 0
            display.subresource.sample = 0
            replay_output.SetTextureDisplay(display)
            replay_output.Display()

            overlay_id = replay_output.GetDebugOverlayTexID()
            if str(overlay_id) == "ResourceId::0":
                return None

            output = Path(output_path)
            save = self.rd.TextureSave()
            save.resourceId = overlay_id
            save.destType = self.rd.FileType.PNG
            save.alpha = self.rd.AlphaMapping.BlendToCheckerboard
            save.mip = 0
            save.slice.sliceIndex = 0
            save.sample.sampleIndex = 0

            result = self.controller.SaveTexture(save, str(output))
            if result == self.rd.ResultCode.Succeeded and output.exists():
                return output
            return None
        finally:
            replay_output.Shutdown()

    def get_capture_metadata(self) -> Dict[str, Any]:
        if self.cap is None:
            raise RuntimeError("RenderDoc replay capture is not opened")
        return {
            "driver_name": str(self.cap.DriverName() or "").strip(),
            "recorded_machine": str(self.cap.RecordedMachineIdent() or "").strip(),
            "timestamp_frequency": float(self.cap.TimestampFrequency() or 0.0),
            "timestamp_base": int(self.cap.TimestampBase() or 0),
        }

    def get_texture_description_map(self) -> Dict[str, Dict[str, Any]]:
        if self.controller is None:
            raise RuntimeError("RenderDoc replay is not opened")
        result: Dict[str, Dict[str, Any]] = {}
        for texture in list(self.controller.GetTextures()):
            texture_id = str(texture.resourceId)
            fmt = getattr(texture, "format", None)
            result[texture_id] = {
                "resource_id": texture_id,
                "width": int(getattr(texture, "width", 0) or 0),
                "height": int(getattr(texture, "height", 0) or 0),
                "depth": int(getattr(texture, "depth", 0) or 0),
                "arraysize": int(getattr(texture, "arraysize", 0) or 0),
                "mips": int(getattr(texture, "mips", 0) or 0),
                "samples": int(getattr(texture, "msSamp", 0) or 0),
                "byte_size": int(getattr(texture, "byteSize", 0) or 0),
                "format_name": str(fmt.Name()) if fmt is not None and hasattr(fmt, "Name") else "",
            }
        return result

    def fetch_counter_map(self, counter_names: list[str]) -> Dict[str, Dict[str, float]]:
        if self.controller is None:
            raise RuntimeError("RenderDoc replay is not opened")
        wanted = {str(name).strip() for name in counter_names if str(name).strip()}
        counter_ids: list[int] = []
        counter_id_to_name: Dict[int, str] = {}
        for counter_id in list(self.controller.EnumerateCounters()):
            description = self.controller.DescribeCounter(counter_id)
            name = str(description.name or "").strip()
            if name in wanted:
                counter_ids.append(counter_id)
                counter_id_to_name[counter_id] = name
        if not counter_ids:
            return {}

        result: Dict[str, Dict[str, float]] = {}
        for item in list(self.controller.FetchCounters(counter_ids)):
            eid = str(getattr(item, "eventId", "")).strip()
            counter_name = counter_id_to_name.get(int(getattr(item, "counter", 0) or 0), "")
            if not eid or not counter_name:
                continue
            result.setdefault(eid, {})[counter_name] = self._counter_value_to_float(getattr(item, "value", None))
        return result

    def export_vsin_csv(
        self,
        *,
        eid: str | int,
        output_path: str | Path,
    ) -> Dict[str, Any]:
        if self.controller is None or self.rd is None:
            raise RuntimeError("RenderDoc replay is not opened")

        event_id = int(str(eid).strip())
        self._set_frame_event(event_id)
        action = self._get_action(event_id)
        if action is None:
            raise RuntimeError(f"无法定位 EID {event_id} 的 drawcall")

        pipe = self.controller.GetPipelineState()
        ib = pipe.GetIBuffer()
        vbs = list(pipe.GetVBuffers())
        attrs = list(pipe.GetVertexInputs())
        if not attrs:
            raise RuntimeError(f"EID {event_id} 未找到 VSInput 顶点属性")

        mesh_inputs = []
        skipped_attributes = []
        name_counter: Dict[str, int] = {}
        for attr in attrs:
            attr_name = self._make_unique_attr_name(self._stringify(getattr(attr, "name", "")) or "ATTR", name_counter)
            if bool(getattr(attr, "perInstance", False)):
                skipped_attributes.append(f"{attr_name}: instanced input is not supported")
                continue

            fmt = getattr(attr, "format", None)
            if fmt is None or bool(fmt.Special()):
                skipped_attributes.append(f"{attr_name}: packed/special format is not supported")
                continue

            vb_index = int(getattr(attr, "vertexBuffer", 0) or 0)
            if vb_index < 0 or vb_index >= len(vbs):
                skipped_attributes.append(f"{attr_name}: invalid vertex buffer index {vb_index}")
                continue

            vb = vbs[vb_index]
            mesh_inputs.append(
                {
                    "name": attr_name,
                    "resource_id": getattr(vb, "resourceId", None),
                    "byte_offset": int(getattr(attr, "byteOffset", 0) or 0)
                    + int(getattr(vb, "byteOffset", 0) or 0)
                    + int(getattr(action, "vertexOffset", 0) or 0) * int(getattr(vb, "byteStride", 0) or 0),
                    "byte_stride": int(getattr(vb, "byteStride", 0) or 0),
                    "format": fmt,
                }
            )

        if not mesh_inputs:
            raise RuntimeError(f"EID {event_id} 的 VSInput 顶点属性不可导出")

        indices = self._decode_mesh_indices(action, ib)
        if not indices:
            raise RuntimeError(f"EID {event_id} 的网格顶点为空")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        headers = ["row_index", "vertex_id"]
        for attr in mesh_inputs:
            comp_count = max(int(getattr(attr["format"], "compCount", 0) or 0), 1)
            headers.extend(f"{attr['name']}.{self._component_suffix(comp_idx)}" for comp_idx in range(comp_count))

        buffer_cache: Dict[str, bytes] = {}
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row_index, vertex_id in enumerate(indices):
                row = [row_index, vertex_id]
                for attr in mesh_inputs:
                    resource_id = attr["resource_id"]
                    if resource_id is None or str(resource_id) == "ResourceId::0":
                        values = self._default_components(attr["format"])
                    else:
                        cache_key = str(resource_id)
                        if cache_key not in buffer_cache:
                            buffer_cache[cache_key] = self.controller.GetBufferData(resource_id, 0, 0)
                        values = self._read_vertex_attribute(
                            data=buffer_cache[cache_key],
                            fmt=attr["format"],
                            byte_offset=int(attr["byte_offset"]),
                            byte_stride=int(attr["byte_stride"]),
                            vertex_index=int(vertex_id),
                        )
                    row.extend(self._format_component(value) for value in values)
                writer.writerow(row)

        return {
            "path": str(output),
            "headers": headers,
            "row_count": len(indices),
            "skipped_attributes": skipped_attributes,
            "stage": "vsin",
        }

    def _set_frame_event(self, eid: int) -> None:
        if self._current_eid == eid:
            return
        self.controller.SetFrameEvent(eid, True)
        self._current_eid = eid

    def _get_action(self, eid: int) -> Any:
        if eid in self._action_map:
            return self._action_map[eid]
        if self.controller is None:
            return None
        for action in list(self.controller.GetRootActions()):
            self._collect_actions(action)
        return self._action_map.get(eid)

    def _collect_actions(self, action: Any) -> None:
        event_id = int(getattr(action, "eventId", 0) or 0)
        if event_id > 0:
            self._action_map[event_id] = action
        for child in list(getattr(action, "children", []) or []):
            self._collect_actions(child)

    def _decode_mesh_indices(self, action: Any, ib: Any) -> list[int]:
        num_indices = int(getattr(action, "numIndices", 0) or 0)
        if num_indices <= 0:
            return []

        flags = getattr(action, "flags", 0)
        indexed = bool(flags & self.rd.ActionFlags.Indexed)
        if indexed and str(getattr(ib, "resourceId", "ResourceId::0")) != "ResourceId::0":
            byte_stride = int(getattr(ib, "byteStride", 0) or 0)
            if byte_stride not in {1, 2, 4}:
                raise RuntimeError(f"不支持的 index stride: {byte_stride}")
            byte_offset = int(getattr(ib, "byteOffset", 0) or 0) + int(getattr(action, "indexOffset", 0) or 0) * byte_stride
            raw = self.controller.GetBufferData(getattr(ib, "resourceId"), byte_offset, byte_stride * num_indices)
            index_fmt = {1: "B", 2: "H", 4: "I"}[byte_stride]
            values = struct.unpack_from("<" + str(num_indices) + index_fmt, raw, 0)
            base_vertex = int(getattr(action, "baseVertex", 0) or 0)
            return [int(value) + base_vertex for value in values]

        vertex_offset = int(getattr(action, "vertexOffset", 0) or 0)
        return list(range(vertex_offset, vertex_offset + num_indices))

    def _read_vertex_attribute(
        self,
        *,
        data: bytes,
        fmt: Any,
        byte_offset: int,
        byte_stride: int,
        vertex_index: int,
    ) -> tuple[Any, ...]:
        absolute_offset = byte_offset + byte_stride * vertex_index
        if absolute_offset < 0 or absolute_offset >= len(data):
            return self._default_components(fmt)
        try:
            return self._unpack_format(fmt, data, absolute_offset)
        except Exception:
            return self._default_components(fmt)

    def _unpack_format(self, fmt: Any, data: bytes, offset: int) -> tuple[Any, ...]:
        if bool(fmt.Special()):
            raise RuntimeError("Packed formats are not supported")

        format_chars = {
            self.rd.CompType.UInt: "xBHxIxxxL",
            self.rd.CompType.SInt: "xbhxixxxl",
            self.rd.CompType.Float: "xxexfxxxd",
        }
        format_chars[self.rd.CompType.UNorm] = format_chars[self.rd.CompType.UInt]
        format_chars[self.rd.CompType.UScaled] = format_chars[self.rd.CompType.UInt]
        format_chars[self.rd.CompType.SNorm] = format_chars[self.rd.CompType.SInt]
        format_chars[self.rd.CompType.SScaled] = format_chars[self.rd.CompType.SInt]

        comp_count = max(int(getattr(fmt, "compCount", 0) or 0), 1)
        comp_width = int(getattr(fmt, "compByteWidth", 0) or 0)
        comp_type = getattr(fmt, "compType")
        format_char = format_chars[comp_type][comp_width]
        values = struct.unpack_from("<" + str(comp_count) + format_char, data, offset)

        if comp_type == self.rd.CompType.UNorm:
            divisor = float((2 ** (comp_width * 8)) - 1)
            values = tuple(float(item) / divisor for item in values)
        elif comp_type == self.rd.CompType.SNorm:
            max_neg = -float(2 ** (comp_width * 8)) / 2
            divisor = float(-(max_neg - 1))
            values = tuple((float(item) if item == max_neg else (float(item) / divisor)) for item in values)

        if bool(fmt.BGRAOrder()) and len(values) >= 4:
            values = tuple(values[idx] for idx in [2, 1, 0, 3])

        return tuple(values)

    @staticmethod
    def _default_components(fmt: Any) -> tuple[float, ...]:
        comp_count = max(int(getattr(fmt, "compCount", 0) or 0), 1)
        return tuple(0.0 for _ in range(comp_count))

    @staticmethod
    def _component_suffix(index: int) -> str:
        suffixes = ["x", "y", "z", "w"]
        if 0 <= index < len(suffixes):
            return suffixes[index]
        return str(index)

    @staticmethod
    def _make_unique_attr_name(name: str, counter: Dict[str, int]) -> str:
        base = RenderdocDirectReplay._stringify(name) or "ATTR"
        current = counter.get(base, 0)
        counter[base] = current + 1
        if current == 0:
            return base
        return f"{base}_{current}"

    @staticmethod
    def _format_component(value: Any) -> str:
        if isinstance(value, int):
            return str(value)
        try:
            return f"{float(value):.9g}"
        except (TypeError, ValueError):
            return "0"

    @staticmethod
    def _counter_value_to_float(value: Any) -> float:
        if value is None:
            return 0.0
        for attr in ("d", "f", "u64", "u32"):
            try:
                raw = getattr(value, attr)
            except Exception:
                continue
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _import_renderdoc():
        python_path = (app_config.RENDERDOC_PYTHON_PATH or "").strip()
        if not python_path:
            raise RuntimeError("未配置 RenderDoc Python 路径")
        if python_path not in sys.path:
            sys.path.insert(0, python_path)
        import renderdoc  # type: ignore

        return renderdoc

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()
