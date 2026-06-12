"""Frame-wide unique-texture audit for the enhanced perf report.

The existing perf rows carry a per-draw ``texture_summary_items`` list (slot /
resource_id / width / height / format / byte_size_mb - produced by
``RenderdocPerfService._get_texture_summary``).  This auditor aggregates those
across every draw into a de-duplicated list of unique textures, sorted by
size, flagging the large ones - the data behind the reference report's
"五、纹理/带宽问题 -> 5.1 大尺寸纹理" table.

SKELETON STATUS
---------------
Aggregation + large-texture flagging are implemented from the data already in
``perf_analysis.json``.  Texture *names* and *attribution* ("需排查归属") are
best-effort: the current rows only expose ``resource_id`` / ``format``, so
``name`` falls back to the resource id.

TODO(integration): to surface friendly texture names (e.g.
``T_FC_Skybox_Day``) we need the replay's resource-name map
(``controller.GetResources()`` -> ``ResourceDescription.name``) persisted into
``perf_analysis.json``.  That touches the existing replay service and is
deferred to the follow-up phase.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from app.services.perf_report.models import LARGE_TEXTURE_EDGE, TextureAuditEntry


class TextureAuditor:
    """Aggregate per-draw bound textures into a unique-texture audit."""

    def __init__(self, large_edge: int = LARGE_TEXTURE_EDGE) -> None:
        self._large_edge = large_edge

    def audit(self, analysis: Mapping[str, Any]) -> List[TextureAuditEntry]:
        rows = analysis.get("rows") or []
        # resource_id -> aggregated entry (+ draw count)
        agg: Dict[str, TextureAuditEntry] = {}

        for row in rows:
            for item in row.get("texture_summary_items") or []:
                res_id = str(item.get("resource_id") or "").strip()
                if not res_id:
                    continue
                width = int(item.get("width", 0) or 0)
                height = int(item.get("height", 0) or 0)
                fmt = str(item.get("format") or "Unknown")
                mb = float(item.get("byte_size_mb", 0.0) or 0.0)

                entry = agg.get(res_id)
                if entry is None:
                    entry = TextureAuditEntry(
                        resource_id=res_id,
                        name=res_id,  # TODO(integration): map to friendly name
                        width=width,
                        height=height,
                        format=fmt,
                        byte_size_mb=mb,
                        used_by_draw_count=0,
                    )
                    agg[res_id] = entry
                # Keep the largest observed dimensions / size for the texture.
                entry.width = max(entry.width, width)
                entry.height = max(entry.height, height)
                entry.byte_size_mb = max(entry.byte_size_mb, mb)
                entry.used_by_draw_count += 1

        entries = list(agg.values())
        for entry in entries:
            entry.is_large = (
                entry.width >= self._large_edge or entry.height >= self._large_edge
            )
            if entry.is_large:
                # TODO(tuning): refine per-format downscale advice.
                entry.suggestion = "大尺寸纹理，建议评估是否可降分辨率/换压缩格式"

        entries.sort(
            key=lambda e: (e.byte_size_mb, e.width * e.height),
            reverse=True,
        )
        return entries

    @staticmethod
    def unique_texture_count(entries: List[TextureAuditEntry]) -> int:
        return len(entries)

    @staticmethod
    def large_textures(entries: List[TextureAuditEntry]) -> List[TextureAuditEntry]:
        return [e for e in entries if e.is_large]
