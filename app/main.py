from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import platform
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from PIL import Image as _PIL_Image  # type: ignore
except Exception:
    _PIL_Image = None  # type: ignore

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import app.config as app_config
from app.services.asset_export_service import AssetExportService
from app.services.asset_export_store import AssetExportStore
from app.services.csv_model_converter import CsvModelConverter
from app.services.renderdoc_cmp_service import RenderdocCmpService
from app.services.renderdoc_perf_service import RenderdocPerfService
from app.services.renderdoc_perf_store import RenderdocPerfStore
from app.services.subprocess_utils import hidden_subprocess_kwargs


app = FastAPI(title="RenderDoc Tools UI", version="0.1.0")
app.mount("/static", StaticFiles(directory=app_config.STATIC_DIR), name="static")
app.mount("/cmp-session-files", StaticFiles(directory=app_config.CMP_SESSION_ROOT), name="cmp-session-files")
app.mount("/export-job-files", StaticFiles(directory=app_config.EXPORT_JOB_ROOT), name="export-job-files")
app.mount("/perf-session-files", StaticFiles(directory=app_config.PERF_SESSION_ROOT), name="perf-session-files")
templates = Jinja2Templates(directory=str(app_config.TEMPLATE_DIR))

asset_export_store = AssetExportStore()
perf_store = RenderdocPerfStore()
cmp_service = RenderdocCmpService()
perf_service = RenderdocPerfService(perf_store)
csv_model_converter = CsvModelConverter()
asset_export_service = AssetExportService(asset_export_store, csv_model_converter)


def _run_shell_command(command: list[str], timeout_seconds: float | None = None) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            **hidden_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired as exc:
        partial_output = ""
        if exc.stdout:
            partial_output += str(exc.stdout)
        if exc.stderr:
            partial_output += ("\n" if partial_output else "") + str(exc.stderr)
        detail = partial_output.strip() or "command timed out"
        return False, f"{detail}\n[timeout after {timeout_seconds}s]".strip()
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, output.strip()


def _ensure_rdc_file(filename: str) -> None:
    if not filename.lower().endswith(".rdc"):
        raise HTTPException(status_code=400, detail=f"文件 `{filename}` 不是 .rdc")


def _ensure_csv_file(filename: str) -> None:
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail=f"文件 `{filename}` 不是 .csv")


def _require_existing_file(path_text: str, suffix: str, label: str) -> Path:
    path_text = (path_text or "").strip()
    if not path_text:
        raise HTTPException(status_code=400, detail=f"{label} 不能为空")
    path = Path(path_text).expanduser()
    if path.suffix.lower() != suffix.lower():
        raise HTTPException(status_code=400, detail=f"{label} 必须是 `{suffix}` 文件")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=400, detail=f"{label} 不存在: {path}")
    return path


def _require_existing_text_file(path_text: str, label: str, allowed_suffixes: tuple[str, ...]) -> Path:
    path_text = (path_text or "").strip()
    if not path_text:
        raise HTTPException(status_code=400, detail=f"{label} 不能为空")
    path = Path(path_text).expanduser()
    if allowed_suffixes and path.suffix.lower() not in {item.lower() for item in allowed_suffixes}:
        allowed_text = " / ".join(allowed_suffixes)
        raise HTTPException(status_code=400, detail=f"{label} 必须是 {allowed_text} 文件")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=400, detail=f"{label} 不存在: {path}")
    return path


def _load_optional_text_file(
    path_text: str,
    label: str,
    allowed_suffixes: tuple[str, ...],
) -> tuple[str, str]:
    raw = (path_text or "").strip()
    if not raw:
        return "", "未提供，已按空值处理。"
    try:
        file_path = _require_existing_text_file(raw, label, allowed_suffixes)
    except HTTPException:
        return "", f"未找到，已忽略: {raw}"
    return file_path.read_text(encoding="utf-8", errors="replace"), f"已读取: {file_path}"


def _split_path_entries(path_text: str) -> list[str]:
    raw = (path_text or "").strip()
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _collect_csv_targets(path_text: str, label: str) -> tuple[list[Path], list[Path]]:
    path_text = (path_text or "").strip()
    if not path_text:
        raise HTTPException(status_code=400, detail=f"{label} 不能为空")
    sources: list[Path] = []
    csv_files: list[Path] = []
    seen_csv_files: set[str] = set()
    for entry in _split_path_entries(path_text):
        source = Path(entry).expanduser()
        if not source.exists():
            raise HTTPException(status_code=400, detail=f"{label} 不存在: {source}")
        sources.append(source)
        if source.is_file():
            if source.suffix.lower() != ".csv":
                raise HTTPException(status_code=400, detail=f"{label} 必须是 `.csv` 文件、多个 CSV 文件路径，或包含 CSV 的目录")
            resolved = str(source.resolve())
            if resolved not in seen_csv_files:
                csv_files.append(source)
                seen_csv_files.add(resolved)
            continue
        if source.is_dir():
            dir_csv_files = sorted(path for path in source.rglob("*.csv") if path.is_file())
            if not dir_csv_files:
                raise HTTPException(status_code=400, detail=f"{label} 目录下未找到 CSV: {source}")
            for csv_file in dir_csv_files:
                resolved = str(csv_file.resolve())
                if resolved not in seen_csv_files:
                    csv_files.append(csv_file)
                    seen_csv_files.add(resolved)
            continue
        raise HTTPException(status_code=400, detail=f"{label} 既不是 CSV 文件也不是目录: {source}")
    if not sources or not csv_files:
        raise HTTPException(status_code=400, detail=f"{label} 未找到有效的 CSV 输入")
    return sources, csv_files


def _safe_rel(base: Path, path: Path) -> str:
    try:
        return str(path.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(path)


def _extract_mapping_form(
    *,
    position: str = "",
    normal: str = "",
    uv0: str = "",
    uv1: str = "",
    uv2: str = "",
    uv3: str = "",
    color: str = "",
    tangent: str = "",
) -> dict:
    return {
        "position": position.strip(),
        "normal": normal.strip(),
        "uv0": uv0.strip(),
        "uv1": uv1.strip(),
        "uv2": uv2.strip(),
        "uv3": uv3.strip(),
        "color": color.strip(),
        "tangent": tangent.strip(),
    }


def _common_output_root(paths: list[Path]) -> str:
    if not paths:
        return ""
    try:
        common_text = os.path.commonpath([str(path) for path in paths])
        return str(Path(common_text))
    except Exception:
        return str(paths[0])


def _create_manual_csv_conversion_job(
    *,
    csv_source_text: str,
    output_format: str,
    mapping: dict,
    output_root: str,
) -> str:
    metadata = asset_export_store.create_job(
        {
            "capture_name": "",
            "capture_source_path": "",
            "export_scope": "manual_csv_convert",
            "pass_id": "",
            "pass_name": "",
            "pass_start_id": "",
            "pass_start": "",
            "pass_end_id": "",
            "pass_end": "",
            "export_fbx": output_format == "fbx",
            "export_obj": output_format == "obj",
            "texture_format": "",
            "notes": "手工 CSV 转换",
            "csv_source_path": csv_source_text.strip(),
            "export_mapping": dict(mapping),
        }
    )
    job_id = metadata["job_id"]
    asset_export_store.update_metadata(
        job_id,
        {
            "status": "completed",
            "progress": {
                "stage": "manual_csv_convert",
                "message": "已完成手工 CSV 转换。",
                "current": 0,
                "total": 0,
            },
            "artifacts": {
                "output_root": output_root,
            },
            "result": {
                "output_root": output_root,
            },
        },
    )
    return job_id


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Keep a strong reference to background perf-analysis tasks so the asyncio
# event loop doesn't garbage-collect them mid-flight.  Cleared in the task's
# own done-callback to avoid an unbounded leak.
_BACKGROUND_PERF_TASKS: set[asyncio.Task] = set()


def _launch_perf_analysis_background(
    *,
    job_id: str,
    capture_file: Path,
    renderdoc_dir: str,
) -> None:
    """Schedule ``analyze_capture_isolated`` as a background task and return
    immediately.  All exceptions are funnelled into the job's metadata so
    the SPA poller surfaces them without us having to ``await`` here.
    """

    async def _runner() -> None:
        try:
            await run_in_threadpool(
                perf_service.analyze_capture_isolated,
                job_id,
                capture_file,
                renderdoc_dir,
            )
        except Exception as exc:
            try:
                perf_service._emit_progress(job_id, "failed", f"性能分析失败：{exc}")
            except Exception:
                pass
            try:
                perf_service.store.update_metadata(job_id, {"status": "failed"})
            except Exception:
                pass

    task = asyncio.create_task(_runner())
    _BACKGROUND_PERF_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_PERF_TASKS.discard)


def _flip_job_textures_in_place(job_dir: Path) -> dict:
    """Scan typical texture locations under ``job_dir`` and flip PNG/TGA
    files vertically in-place (used by the manual-convert "上下翻转
    贴图" toggle).

    Returns a summary so the caller can record what happened in the
    manifest's ``conversion_history``.
    """
    summary: dict = {"performed": True, "flipped": [], "skipped": [], "notes": []}
    if _PIL_Image is None:
        summary["notes"].append("当前环境未安装 PIL，跳过贴图二次翻转")
        return summary

    candidate_roots: list[Path] = []
    candidate_roots.append(job_dir / "textures")
    exports_root = job_dir / "exports"
    if exports_root.is_dir():
        candidate_roots.append(exports_root)

    seen: set[Path] = set()
    for root in candidate_roots:
        if not root or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in {".png", ".tga"}:
                if suffix == ".dds":
                    rel = str(path.relative_to(job_dir)).replace("\\", "/")
                    summary["skipped"].append({"path": rel, "reason": "dds 格式 PIL 无法原地翻转"})
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                with _PIL_Image.open(path) as image:
                    flipped = image.transpose(_PIL_Image.FLIP_TOP_BOTTOM)
                    flipped.save(path)
                summary["flipped"].append(str(path.relative_to(job_dir)).replace("\\", "/"))
            except Exception as exc:
                summary["skipped"].append(
                    {
                        "path": str(path.relative_to(job_dir)).replace("\\", "/"),
                        "reason": f"PIL 翻转失败: {exc}",
                    }
                )
    return summary


def _run_csv_conversion_for_job(
    *,
    job_id: str,
    csv_sources: list[Path],
    csv_files: list[Path],
    output_format: str,
    mapping: dict,
    flip_texture_y: bool = False,
) -> dict:
    try:
        job_dir = asset_export_store.job_path(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="资产导出任务不存在")

    detail = asset_export_store.get_job_detail(job_id)
    metadata = detail["metadata"]
    output_root = Path(
        metadata.get("result", {}).get("output_root")
        or metadata.get("artifacts", {}).get("output_root")
        or (job_dir / "exports")
    )

    manifest = detail.get("manifest") or {"items": []}
    manual_conversions = manifest.get("manual_conversions") or []
    result = metadata.get("result") or {}
    model_files = list(result.get("model_files") or [])
    batch_mode = len(csv_sources) > 1 or any(path.is_dir() for path in csv_sources)

    for csv_file in csv_files:
        if batch_mode:
            output_dir = csv_file.parent
        else:
            output_dir = asset_export_service.resolve_manual_output_dir(job_dir, output_root, str(csv_file))
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{csv_file.stem}.{output_format}"
        try:
            headers = csv_model_converter.read_headers(csv_file)
            suggested_mapping = csv_model_converter.suggest_mapping(csv_file)
            applied_mapping, mapping_notes = csv_model_converter.merge_override_mapping(
                headers,
                suggested_mapping,
                mapping,
            )
            csv_model_converter.convert(
                csv_path=csv_file,
                output_path=output_path,
                mapping=applied_mapping,
                fmt=output_format,
                flip_texture_y=bool(flip_texture_y),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"CSV 转换失败: {csv_file.name}: {exc}") from exc

        output_ref = asset_export_service._artifact_ref(job_dir, output_path)
        conversion_entry = {
            "csv_name": csv_file.name,
            "csv_source_path": str(csv_file),
            "output_format": output_format.upper(),
            "output_path": output_ref,
            "mapping_suggested": suggested_mapping.to_dict(),
            "mapping_applied": applied_mapping.to_dict(),
            "mapping_notes": mapping_notes,
        }
        if flip_texture_y:
            conversion_entry["flip_texture_y"] = True
        manual_conversions.append(conversion_entry)
        if output_ref not in model_files:
            model_files.append(output_ref)

    texture_flip_summary: dict = {"performed": False, "flipped": [], "skipped": [], "notes": []}
    if flip_texture_y:
        texture_flip_summary = _flip_job_textures_in_place(job_dir)
        history_entry = {
            "action": "flip_texture_y",
            "timestamp": _utc_now_iso(),
            "flipped": list(texture_flip_summary.get("flipped") or []),
            "skipped": list(texture_flip_summary.get("skipped") or []),
            "notes": list(texture_flip_summary.get("notes") or []),
        }
        conversion_history = list(manifest.get("conversion_history") or [])
        conversion_history.append(history_entry)
        manifest["conversion_history"] = conversion_history

    manifest["manual_conversions"] = manual_conversions
    asset_export_store.write_json_artifact(job_id, "artifacts/manifest.json", manifest)
    asset_export_store.write_json_artifact(job_id, "artifacts/mapping.json", mapping)
    asset_export_store.update_metadata(
        job_id,
        {
            "status": "completed",
            "progress": {
                "stage": "manual_csv_convert",
                "message": f"已转换 {len(csv_files)} 个 CSV。",
                "current": len(csv_files),
                "total": len(csv_files),
            },
            "result": {
                "model_files": model_files,
            },
        },
    )
    return asset_export_store.get_job_detail(job_id)


def _refresh_runtime_services() -> None:
    global cmp_service
    global perf_service
    cmp_service = RenderdocCmpService()
    perf_service = RenderdocPerfService(perf_store)


def _health_payload() -> dict:
    python_ok = True
    python_version = platform.python_version()
    rdc_ok, rdc_output = _run_shell_command(["rdc", "--version"], timeout_seconds=5)
    # `rdc doctor` can hang in some packaged environments, so keep health checks responsive.
    doctor_ok, doctor_output = _run_shell_command(["rdc", "doctor"], timeout_seconds=5)
    settings = app_config.current_settings()
    return {
        "python": {
            "ok": python_ok,
            "version": python_version,
        },
        "rdc": {
            "ok": rdc_ok,
            "output": rdc_output,
        },
        "doctor": {
            "ok": doctor_ok,
            "output": doctor_output,
        },
        "renderdoc_cmp": {
            "ok": app_config.RENDERDOC_CMP_SCRIPT.exists(),
            "path": str(app_config.RENDERDOC_CMP_SCRIPT),
        },
        "llm_provider": {
            "provider": app_config.LLM_PROVIDER,
            "configured": bool(app_config.OPENAI_BASE_URL and app_config.OPENAI_API_KEY and app_config.OPENAI_MODEL),
            "base_url": app_config.OPENAI_BASE_URL,
            "model": app_config.OPENAI_MODEL,
        },
        "settings": settings,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "default_host": app_config.DEFAULT_HOST,
            "default_port": app_config.DEFAULT_PORT,
        },
    )


@app.get("/api/health")
async def health() -> dict:
    return _health_payload()


@app.get("/api/ping")
async def ping() -> dict:
    return {"ok": True}


@app.get("/api/setup-status")
async def setup_status() -> dict:
    payload = _health_payload()
    settings = payload["settings"]
    renderdoc_ready = payload["doctor"]["ok"]
    cmp_ready = payload["renderdoc_cmp"]["ok"]
    llm_ready = settings.get("llm_provider") == "local" or payload["llm_provider"]["configured"]
    setup_completed = bool(settings.get("setup_completed"))
    payload["wizard"] = {
        "setup_completed": setup_completed,
        # Only auto-block on true first-run setup. Later health issues should not lock the whole UI.
        "needs_setup": not setup_completed,
        "checks": {
            "renderdoc_ready": renderdoc_ready,
            "cmp_ready": cmp_ready,
            "llm_ready": llm_ready,
        },
    }
    return payload


@app.post("/api/settings")
async def save_settings(
    renderdoc_python_path: str = Form(""),
    llm_provider: str = Form("local"),
    openai_base_url: str = Form(""),
    openai_api_key: str = Form(""),
    openai_model: str = Form(""),
    renderdoc_cmp_root: str = Form(""),
    setup_completed: str = Form("true"),
) -> dict:
    app_config.persist_settings(
        {
            "renderdoc_python_path": renderdoc_python_path.strip(),
            "llm_provider": llm_provider.strip() or "local",
            "openai_base_url": openai_base_url.strip(),
            "openai_api_key": openai_api_key.strip(),
            "openai_model": openai_model.strip(),
            "renderdoc_cmp_root": renderdoc_cmp_root.strip(),
            "setup_completed": str(setup_completed).lower() in {"true", "1", "yes", "on"},
        }
    )
    _refresh_runtime_services()
    return _health_payload()


@app.get("/api/renderdoc-cmp/jobs")
async def list_cmp_jobs() -> list[dict]:
    return cmp_service.list_jobs()


@app.get("/api/renderdoc-cmp/jobs/{job_id}")
async def get_cmp_job(job_id: str) -> dict:
    try:
        return cmp_service.get_job_detail(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="cmp job 不存在")


@app.get("/api/renderdoc-perf/jobs")
async def list_perf_jobs() -> list[dict]:
    return perf_service.list_jobs()


@app.get("/api/renderdoc-perf/jobs/{job_id}")
async def get_perf_job(job_id: str) -> dict:
    try:
        return perf_service.get_job_detail(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="性能分析任务不存在")


@app.post("/api/renderdoc-perf/jobs/{job_id}/draw-preview")
async def generate_perf_draw_preview(job_id: str, eid: str = Form(...)) -> dict:
    if not eid.strip():
        raise HTTPException(status_code=400, detail="eid 不能为空")
    try:
        return perf_service.generate_draw_preview(job_id, eid.strip())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="性能分析任务不存在")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成线框预览失败: {exc}") from exc


@app.get("/api/renderdoc-perf/jobs/{job_id}/artifact")
async def get_perf_artifact(job_id: str, path: str) -> FileResponse:
    """Serve files under the perf job directory (e.g. capture thumbnail)."""
    try:
        job_dir = perf_service.store.job_path(job_id).resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="性能分析任务不存在")

    target = Path(path)
    candidate = target if target.is_absolute() else (job_dir / target)
    candidate = candidate.resolve()
    if not candidate.is_relative_to(job_dir):
        raise HTTPException(status_code=403, detail="不允许访问该文件")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=str(candidate), filename=candidate.name)


@app.get("/api/renderdoc-perf/jobs/{job_id}/draw-texture-thumbnail")
async def get_perf_draw_texture_thumbnail(
    job_id: str, res_id: str, width: int, height: int, fmt: str = ""
) -> FileResponse:
    """Lazily decode the dominant bound texture for an XML-fallback draw.

    Used as the per-draw preview surrogate when GPU wireframe replay is
    unavailable (custom/older RenderDoc builds without Python API).
    Thumbnails are cached on disk so repeat requests are instant.
    """
    try:
        job_dir = perf_service.store.job_path(job_id).resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="性能分析任务不存在")

    if not res_id or width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="无效的纹理参数")

    cache_dir = job_dir / "artifacts" / "draw_thumbs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_fmt = re.sub(r"[^A-Za-z0-9_]", "_", fmt or "raw")
    cache_path = cache_dir / f"tex_{res_id}_{width}x{height}_{safe_fmt}.png"

    if not cache_path.exists():
        from app.services import renderdoc_xml_thumbnailer
        zip_path = job_dir / "workdir" / "capture.zip"
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail="capture.zip 不存在（未启用 zip.xml 回退路径）")
        astcenc = renderdoc_xml_thumbnailer.find_astcenc()
        ok = renderdoc_xml_thumbnailer.generate_thumbnail(
            zip_path=zip_path,
            resource_id=res_id,
            width=width,
            height=height,
            fmt=fmt,
            output_png=cache_path,
            astcenc_path=astcenc,
        )
        if not ok:
            raise HTTPException(status_code=404, detail=f"无法解码纹理 res={res_id} fmt={fmt}")
    return FileResponse(path=str(cache_path), filename=cache_path.name, media_type="image/png")


_PERF_EXPORT_FORMATS = {"csv", "tsv", "json", "md", "html", "zip"}
_PERF_EXPORT_FILENAMES = {
    "csv": "draws.csv",
    "tsv": "draws.tsv",
    "md": "perf_report.md",
    "html": "perf_report.html",
    "json": "perf_analysis.json",
}
_PERF_EXPORT_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


def _resolve_perf_job_dir(job_id: str) -> Path:
    try:
        return perf_service.store.job_path(job_id).resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="性能分析任务不存在")


def _perf_artifact_path(job_dir: Path, relative: str) -> Path:
    candidate = (job_dir / relative).resolve()
    if not candidate.is_relative_to(job_dir):
        raise HTTPException(status_code=403, detail="不允许访问该文件")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {relative}")
    return candidate


# Match preview URLs the perf service emits.  Both the direct-replay
# path and the xml_fallback path produce ``<img>`` references like
#   /perf-session-files/<job>/artifacts/previews/wireframe_528.png   (direct_replay)
#   /perf-session-files/<job>/artifacts/qr_replay/wf_wireframe_eid528.png  (xml_fallback)
# We need to catch BOTH directories or the xml_fallback download ends
# up with absolute, service-only URLs that break offline.
_PERF_PREVIEW_URL_PATTERN = re.compile(
    r"/perf-session-files/(?P<job>[^/\"' >]+)/artifacts/(?P<dir>previews|qr_replay)/(?P<file>[A-Za-z0-9_.\-]+\.(?:png|jpg|jpeg|webp))"
)

# Whitelist of artifact sub-directories that are allowed to host
# preview images.  Anything outside is rejected by the rewriter to
# avoid reaching into unrelated job files.
_PERF_PREVIEW_ALLOWED_DIRS = ("previews", "qr_replay")


def _rewrite_perf_html_previews(
    html_text: str,
    job_dir: Path,
    job_id: str,
    *,
    mode: str,
) -> tuple[str, list[Path]]:
    """Rewrite preview ``<img src=...>`` URLs in the saved HTML report so
    that downloaded copies still resolve when the local FastAPI service is
    not running.

    ``mode='base64'``  → replace each preview URL with an inline
                         ``data:image/...;base64,...`` URI.  Use this for
                         standalone HTML downloads.

    ``mode='relative'`` → replace each preview URL with the relative
                          path ``<dir>/<file>`` (preserving whichever
                          artifact sub-directory the original URL used)
                          so it resolves against the report sitting
                          next to a matching sibling folder.  Use this
                          when bundling into a ZIP.

    Returns ``(rewritten_html, referenced_image_paths)`` so callers can
    bundle the referenced preview files alongside the report.  Image
    files that cannot be read are left as-is (the original URL is kept
    so users will still see a broken-image placeholder rather than a
    silent removal).
    """
    if mode not in {"base64", "relative"}:
        raise ValueError(f"unsupported rewrite mode: {mode}")

    artifacts_root = (job_dir / "artifacts").resolve()
    referenced: list[Path] = []
    seen_paths: set[Path] = set()

    def repl(match: re.Match[str]) -> str:
        ref_job = match.group("job")
        # Only rewrite URLs belonging to *this* job - never reach into
        # another session's preview directory even by accident.
        if ref_job != job_id:
            return match.group(0)
        subdir = match.group("dir")
        if subdir not in _PERF_PREVIEW_ALLOWED_DIRS:
            return match.group(0)
        filename = match.group("file")
        dir_root = (artifacts_root / subdir).resolve()
        candidate = (dir_root / filename).resolve()
        try:
            if not candidate.is_relative_to(dir_root) or not candidate.exists():
                return match.group(0)
        except Exception:
            return match.group(0)

        if mode == "relative":
            if candidate not in seen_paths:
                seen_paths.add(candidate)
                referenced.append(candidate)
            return f"{subdir}/{filename}"

        # mode == "base64" - inline.
        try:
            data = candidate.read_bytes()
        except Exception:
            return match.group(0)
        mime, _ = mimetypes.guess_type(filename)
        if not mime:
            mime = "image/png"
        b64 = base64.b64encode(data).decode("ascii")
        if candidate not in seen_paths:
            seen_paths.add(candidate)
            referenced.append(candidate)
        return f"data:{mime};base64,{b64}"

    return _PERF_PREVIEW_URL_PATTERN.sub(repl, html_text), referenced


def _build_perf_zip_bytes(job_dir: Path, job_id: str) -> bytes:
    buffer = io.BytesIO()
    candidate_paths: list[tuple[Path, str]] = []
    for relative in (
        "artifacts/perf_report.md",
        "artifacts/perf_analysis.json",
        "artifacts/findings.json",
        "artifacts/perf_run_log.txt",
    ):
        path = (job_dir / relative).resolve()
        if path.exists() and path.is_relative_to(job_dir):
            candidate_paths.append((path, relative))
    exports_dir = (job_dir / "artifacts" / "exports").resolve()
    if exports_dir.exists() and exports_dir.is_dir() and exports_dir.is_relative_to(job_dir):
        for path in sorted(exports_dir.iterdir()):
            if path.is_file():
                candidate_paths.append((path, f"artifacts/exports/{path.name}"))

    # The HTML report is handled specially: every preview image gets
    # inlined as a base64 ``data:`` URI so the resulting file is fully
    # self-contained.  This avoids a Windows-shell footgun where users
    # double-click ``perf_report.html`` from *inside* the ZIP (Explorer
    # extracts only the HTML to a temp directory, so a sibling
    # ``previews/`` folder isn't present and every <img> would 404).
    # Now the report works regardless of how the user opens it.
    html_path = (job_dir / "artifacts" / "perf_report.html").resolve()
    rewritten_html: Optional[str] = None
    referenced_previews: list[Path] = []
    if html_path.exists() and html_path.is_relative_to(job_dir):
        original_html = html_path.read_text(encoding="utf-8", errors="replace")
        rewritten_html, referenced_previews = _rewrite_perf_html_previews(
            original_html, job_dir, job_id, mode="base64"
        )

    if not candidate_paths and rewritten_html is None:
        raise HTTPException(status_code=404, detail="该任务还没有可导出的报告/CSV")

    # Note: ``referenced_previews`` is populated by the base64 rewrite
    # for diagnostics but the PNG files themselves are NOT bundled as
    # a separate folder.  All previews are already inlined into the
    # HTML, and shipping a redundant ``previews/`` folder would roughly
    # double the ZIP size (~65 MB for a 100-draw analysis) without
    # giving the typical reader anything more than what the HTML
    # already provides.
    _ = referenced_previews

    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in candidate_paths:
            zf.write(path, arcname=f"{job_id}/{arcname}")
        if rewritten_html is not None:
            zf.writestr(f"{job_id}/artifacts/perf_report.html", rewritten_html)
    return buffer.getvalue()


@app.get("/api/renderdoc-perf/jobs/{job_id}/export")
async def export_perf_job(job_id: str, format: str = "zip") -> Response:
    """Download perf job artifacts in the requested format.

    - ``csv`` / ``tsv``: returns the wide ``draws`` table only
    - ``json``: returns the original ``perf_analysis.json``
    - ``md`` / ``html``: returns the generated report
    - ``zip`` (default): bundles report + all CSV/TSV + run log + findings.json
    """
    fmt = (format or "zip").lower().strip()
    if fmt not in _PERF_EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的导出格式: {format} (可选 {sorted(_PERF_EXPORT_FORMATS)})",
        )
    job_dir = _resolve_perf_job_dir(job_id)
    if fmt == "zip":
        data = _build_perf_zip_bytes(job_dir, job_id)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job_id}_perf_export.zip"'},
        )

    if fmt in {"csv", "tsv"}:
        relative = f"artifacts/exports/{_PERF_EXPORT_FILENAMES[fmt]}"
    elif fmt == "json":
        relative = "artifacts/perf_analysis.json"
    else:
        relative = f"artifacts/{_PERF_EXPORT_FILENAMES[fmt]}"
    target = _perf_artifact_path(job_dir, relative)

    # For standalone HTML downloads, inline every wireframe preview as a
    # base64 ``data:`` URI so the file works offline without any sibling
    # ``previews/`` folder and without the local FastAPI service.
    if fmt == "html":
        original_html = target.read_text(encoding="utf-8", errors="replace")
        rewritten_html, _ = _rewrite_perf_html_previews(
            original_html, job_dir, job_id, mode="base64"
        )
        return Response(
            content=rewritten_html.encode("utf-8"),
            media_type=_PERF_EXPORT_MEDIA_TYPES.get(fmt, "text/html; charset=utf-8"),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{job_id}_{target.name}"'
                ),
            },
        )

    return FileResponse(
        path=str(target),
        filename=f"{job_id}_{target.name}",
        media_type=_PERF_EXPORT_MEDIA_TYPES.get(fmt, "application/octet-stream"),
    )


@app.get("/api/renderdoc-perf/jobs/{job_id}/report")
async def get_perf_job_report(job_id: str, format: str = "html") -> Response:
    """Return the rendered perf report for inline display in the frontend.

    Defaults to ``html`` for the inline preview panel; ``md`` is provided so
    callers can also fetch the raw Markdown without going through the zip
    download route.
    """
    fmt = (format or "html").lower().strip()
    if fmt not in {"html", "md"}:
        raise HTTPException(status_code=400, detail="format 仅支持 html 或 md")
    job_dir = _resolve_perf_job_dir(job_id)
    relative = "artifacts/perf_report.html" if fmt == "html" else "artifacts/perf_report.md"
    target = _perf_artifact_path(job_dir, relative)
    text = target.read_text(encoding="utf-8", errors="replace")
    if fmt == "html":
        return HTMLResponse(content=text)
    return PlainTextResponse(content=text, media_type="text/markdown; charset=utf-8")


@app.get("/api/asset-export/jobs")
async def list_asset_export_jobs() -> list[dict]:
    return asset_export_store.list_jobs()


@app.get("/api/asset-export/jobs/{job_id}")
async def get_asset_export_job(job_id: str) -> dict:
    try:
        return asset_export_store.get_job_detail(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="资产导出任务不存在")


@app.post("/api/renderdoc-cmp/compare/by-path")
async def run_renderdoc_cmp_by_path(
    base_path: str = Form(...),
    new_path: str = Form(...),
    strict_mode: str = Form("false"),
    renderdoc_dir: str = Form(""),
    malioc_path: str = Form(""),
    verbose: str = Form("false"),
) -> dict:
    base_file = _require_existing_file(base_path, ".rdc", "base_path")
    new_file = _require_existing_file(new_path, ".rdc", "new_path")

    title = f"renderdoc_cmp: {base_file.name} vs {new_file.name}"
    metadata = cmp_service.create_job(title=title)
    job_id = metadata["job_id"]
    cmp_service.update_metadata(
        job_id,
        {
            "status": "running",
            "inputs": {
                "base_file": str(base_file),
                "new_file": str(new_file),
                "strict_mode": str(strict_mode).lower() in {"true", "1", "yes", "on"},
                "renderdoc_dir": renderdoc_dir.strip(),
                "malioc_path": malioc_path.strip(),
            },
        },
    )

    # cmp_service.run_compare blocks for several minutes while the cmp child
    # process decodes textures / analyses shaders.  Run it in a worker thread
    # so the FastAPI event loop keeps serving health checks, job listings,
    # and other UI requests.
    try:
        await run_in_threadpool(
            cmp_service.run_compare,
            job_id,
            base_file,
            new_file,
            str(strict_mode).lower() in {"true", "1", "yes", "on"},
            renderdoc_dir,
            malioc_path,
            str(verbose).lower() in {"true", "1", "yes", "on"},
        )
    except Exception as exc:
        cmp_service.update_metadata(job_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"renderdoc_cmp 执行失败: {exc}") from exc
    return cmp_service.get_job_detail(job_id)


@app.post("/api/renderdoc-perf/analyze/by-path")
async def run_renderdoc_perf_by_path(
    capture_path: str = Form(...),
    renderdoc_dir: str = Form(""),
) -> dict:
    capture_file = _require_existing_file(capture_path, ".rdc", "capture_path")
    title = f"renderdoc_perf: {capture_file.name}"
    metadata = perf_service.create_job(title=title)
    job_id = metadata["job_id"]
    metadata = perf_service.store.update_metadata(
        job_id,
        {
            "status": "running",
            "inputs": {
                "capture_file": str(capture_file),
                "renderdoc_dir_requested": renderdoc_dir.strip(),
            },
            "progress": {
                "stage": "init",
                "message": "已创建任务，等待 worker 启动…",
                "updated_at": _utc_now_iso(),
            },
        },
    )
    _launch_perf_analysis_background(
        job_id=job_id,
        capture_file=capture_file,
        renderdoc_dir=renderdoc_dir,
    )
    return {"job_id": job_id, "status": "running", "metadata": metadata}


@app.post("/api/asset-export/scan-passes/by-path")
async def asset_export_scan_passes_by_path(capture_path: str = Form(...)) -> dict:
    capture_file = _require_existing_file(capture_path, ".rdc", "capture_path")
    try:
        passes = asset_export_service.scan_passes(capture_file)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 Pass 列表失败: {exc}") from exc
    return {"capture_name": capture_file.name, "capture_path": str(capture_file), "passes": passes}


@app.post("/api/asset-export/csv-inspect/by-path")
async def asset_export_csv_inspect_by_path(csv_path: str = Form(...)) -> dict:
    csv_sources, csv_files = _collect_csv_targets(csv_path, "csv_path")
    csv_file = csv_files[0]
    batch_mode = len(csv_sources) > 1 or any(path.is_dir() for path in csv_sources)
    try:
        headers = csv_model_converter.read_headers(csv_file)
        mapping = csv_model_converter.suggest_mapping(csv_file).to_dict()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"解析 CSV 失败: {exc}") from exc
    return {
        "csv_name": csv_file.name,
        "csv_path": "\n".join(str(path) for path in csv_sources),
        "inspect_csv_path": str(csv_file),
        "headers": headers,
        "suggested_mapping": mapping,
        "batch_mode": batch_mode,
        "source_count": len(csv_sources),
        "source_preview_paths": [str(path) for path in csv_sources[:20]],
        "csv_count": len(csv_files),
        "csv_preview_paths": [str(path) for path in csv_files[:20]],
    }

@app.post("/api/asset-export/scan-passes")
async def asset_export_scan_passes(capture_file: UploadFile = File(...)) -> dict:
    _ensure_rdc_file(capture_file.filename)
    content = await capture_file.read()
    with tempfile.TemporaryDirectory(prefix="renderdoc_pass_scan_") as temp_dir:
        capture_path = Path(temp_dir) / "capture.rdc"
        capture_path.write_bytes(content)
        try:
            passes = asset_export_service.scan_passes(capture_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取 Pass 列表失败: {exc}") from exc
    return {"capture_name": capture_file.filename, "passes": passes}


@app.post("/api/asset-export/csv-inspect")
async def asset_export_csv_inspect(csv_file: UploadFile = File(...)) -> dict:
    _ensure_csv_file(csv_file.filename)
    content = await csv_file.read()
    with tempfile.TemporaryDirectory(prefix="renderdoc_csv_inspect_") as temp_dir:
        csv_path = Path(temp_dir) / "mesh.csv"
        csv_path.write_bytes(content)
        try:
            headers = csv_model_converter.read_headers(csv_path)
            mapping = csv_model_converter.suggest_mapping(csv_path).to_dict()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"解析 CSV 失败: {exc}") from exc
    return {
        "csv_name": csv_file.filename,
        "headers": headers,
        "suggested_mapping": mapping,
    }


@app.post("/api/asset-export/export-mapping-preview/by-path")
async def asset_export_mapping_preview_by_path(
    capture_path: str = Form(...),
    export_scope: str = Form("single"),
    pass_id: str = Form(""),
    pass_name: str = Form(""),
    pass_start_id: str = Form(""),
    pass_start: str = Form(""),
    pass_end_id: str = Form(""),
    pass_end: str = Form(""),
) -> dict:
    capture_file = _require_existing_file(capture_path, ".rdc", "capture_path")
    try:
        return asset_export_service.preview_export_mapping_isolated(
            capture_path=capture_file,
            export_scope=export_scope.strip() or "single",
            pass_id=pass_id.strip(),
            pass_name=pass_name.strip(),
            pass_start_id=pass_start_id.strip(),
            pass_start=pass_start.strip(),
            pass_end_id=pass_end_id.strip(),
            pass_end=pass_end.strip(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"批量映射预览失败: {exc}") from exc


@app.post("/api/asset-export/export-mapping-preview")
async def asset_export_mapping_preview(
    capture_file: UploadFile = File(...),
    export_scope: str = Form("single"),
    pass_id: str = Form(""),
    pass_name: str = Form(""),
    pass_start_id: str = Form(""),
    pass_start: str = Form(""),
    pass_end_id: str = Form(""),
    pass_end: str = Form(""),
) -> dict:
    _ensure_rdc_file(capture_file.filename)
    content = await capture_file.read()
    with tempfile.TemporaryDirectory(prefix="renderdoc_mapping_preview_") as temp_dir:
        temp_capture = Path(temp_dir) / "capture.rdc"
        temp_capture.write_bytes(content)
        try:
            return asset_export_service.preview_export_mapping_isolated(
                capture_path=temp_capture,
                export_scope=export_scope.strip() or "single",
                pass_id=pass_id.strip(),
                pass_name=pass_name.strip(),
                pass_start_id=pass_start_id.strip(),
                pass_start=pass_start.strip(),
                pass_end_id=pass_end_id.strip(),
                pass_end=pass_end.strip(),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"批量映射预览失败: {exc}") from exc


@app.post("/api/asset-export/jobs")
async def create_asset_export_job(
    capture_file: UploadFile = File(...),
    capture_source_path: str = Form(""),
    export_scope: str = Form("single"),
    pass_id: str = Form(""),
    pass_name: str = Form(""),
    pass_start_id: str = Form(""),
    pass_start: str = Form(""),
    pass_end_id: str = Form(""),
    pass_end: str = Form(""),
    export_fbx: str = Form("true"),
    export_obj: str = Form("false"),
    flip_texture_y: str = Form("false"),
    texture_format: str = Form("png"),
    notes: str = Form(""),
    position: str = Form(""),
    normal: str = Form(""),
    uv0: str = Form(""),
    uv1: str = Form(""),
    uv2: str = Form(""),
    uv3: str = Form(""),
    color: str = Form(""),
    tangent: str = Form(""),
) -> dict:
    _ensure_rdc_file(capture_file.filename)
    requested_scope = export_scope.strip() or "single"
    requested_texture_format = texture_format.strip().lower() or "png"
    requested_flip_texture_y = str(flip_texture_y).lower() in {"true", "1", "yes", "on"}
    if requested_scope == "single" and not (pass_id.strip() or pass_name.strip()):
        raise HTTPException(status_code=400, detail="单个 Pass 模式下必须选择 pass_name")
    if requested_scope == "range" and not ((pass_start_id.strip() or pass_start.strip()) and (pass_end_id.strip() or pass_end.strip())):
        raise HTTPException(status_code=400, detail="Pass 区间模式下必须同时选择起始和结束 Pass")

    metadata = asset_export_store.create_job(
        {
            "capture_name": capture_file.filename,
            "capture_source_path": capture_source_path.strip(),
            "export_scope": requested_scope,
            "pass_id": pass_id.strip(),
            "pass_name": pass_name.strip(),
            "pass_start_id": pass_start_id.strip(),
            "pass_start": pass_start.strip(),
            "pass_end_id": pass_end_id.strip(),
            "pass_end": pass_end.strip(),
            "export_fbx": str(export_fbx).lower() in {"true", "1", "yes", "on"},
            "export_obj": str(export_obj).lower() in {"true", "1", "yes", "on"},
            "flip_texture_y": requested_flip_texture_y,
            "texture_format": requested_texture_format,
            "notes": notes.strip(),
            "export_mapping": _extract_mapping_form(
                position=position,
                normal=normal,
                uv0=uv0,
                uv1=uv1,
                uv2=uv2,
                uv3=uv3,
                color=color,
                tangent=tangent,
            ),
        }
    )
    job_id = metadata["job_id"]
    capture_path = asset_export_store.save_input_file(job_id, "capture.rdc", await capture_file.read())
    job_dir = asset_export_store.job_path(job_id)
    try:
        output_root = asset_export_service.resolve_output_root(job_dir, capture_source_path.strip(), capture_file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    asset_export_store.update_metadata(
        job_id,
        {
            "artifacts": {
                "capture_file": str(Path(capture_path).relative_to(Path(capture_path).parents[1])),
                "output_root": str(output_root),
            },
            "result": {
                "output_root": str(output_root),
            },
        },
    )
    try:
        asset_export_service.run_export(
            job_id=job_id,
            capture_path=Path(capture_path),
            output_root=output_root,
            export_scope=requested_scope,
            pass_id=pass_id.strip(),
            pass_name=pass_name.strip(),
            pass_start_id=pass_start_id.strip(),
            pass_start=pass_start.strip(),
            pass_end_id=pass_end_id.strip(),
            pass_end=pass_end.strip(),
            export_fbx=str(export_fbx).lower() in {"true", "1", "yes", "on"},
            export_obj=str(export_obj).lower() in {"true", "1", "yes", "on"},
            texture_format=requested_texture_format,
            flip_texture_y=requested_flip_texture_y,
            mapping_override=_extract_mapping_form(
                position=position,
                normal=normal,
                uv0=uv0,
                uv1=uv1,
                uv2=uv2,
                uv3=uv3,
                color=color,
                tangent=tangent,
            ),
            isolated=True,
        )
    except Exception as exc:
        asset_export_store.update_metadata(
            job_id,
            {
                "status": "failed",
                "progress": {
                    "stage": "failed",
                    "message": str(exc),
                    "current": 0,
                    "total": 0,
                },
            },
        )
        raise HTTPException(status_code=500, detail=f"资产导出失败: {exc}") from exc
    return asset_export_store.get_job_detail(job_id)


@app.post("/api/asset-export/jobs/by-path")
async def create_asset_export_job_by_path(
    capture_path: str = Form(...),
    export_scope: str = Form("single"),
    pass_id: str = Form(""),
    pass_name: str = Form(""),
    pass_start_id: str = Form(""),
    pass_start: str = Form(""),
    pass_end_id: str = Form(""),
    pass_end: str = Form(""),
    export_fbx: str = Form("true"),
    export_obj: str = Form("false"),
    flip_texture_y: str = Form("false"),
    texture_format: str = Form("png"),
    notes: str = Form(""),
    position: str = Form(""),
    normal: str = Form(""),
    uv0: str = Form(""),
    uv1: str = Form(""),
    uv2: str = Form(""),
    uv3: str = Form(""),
    color: str = Form(""),
    tangent: str = Form(""),
) -> dict:
    capture_file = _require_existing_file(capture_path, ".rdc", "capture_path")
    requested_scope = export_scope.strip() or "single"
    requested_texture_format = texture_format.strip().lower() or "png"
    requested_flip_texture_y = str(flip_texture_y).lower() in {"true", "1", "yes", "on"}
    if requested_scope == "single" and not (pass_id.strip() or pass_name.strip()):
        raise HTTPException(status_code=400, detail="单个 Pass 模式下必须选择 pass_name")
    if requested_scope == "range" and not ((pass_start_id.strip() or pass_start.strip()) and (pass_end_id.strip() or pass_end.strip())):
        raise HTTPException(status_code=400, detail="Pass 区间模式下必须同时选择起始和结束 Pass")

    metadata = asset_export_store.create_job(
        {
            "capture_name": capture_file.name,
            "capture_source_path": str(capture_file),
            "export_scope": requested_scope,
            "pass_id": pass_id.strip(),
            "pass_name": pass_name.strip(),
            "pass_start_id": pass_start_id.strip(),
            "pass_start": pass_start.strip(),
            "pass_end_id": pass_end_id.strip(),
            "pass_end": pass_end.strip(),
            "export_fbx": str(export_fbx).lower() in {"true", "1", "yes", "on"},
            "export_obj": str(export_obj).lower() in {"true", "1", "yes", "on"},
            "flip_texture_y": requested_flip_texture_y,
            "texture_format": requested_texture_format,
            "notes": notes.strip(),
            "export_mapping": _extract_mapping_form(
                position=position,
                normal=normal,
                uv0=uv0,
                uv1=uv1,
                uv2=uv2,
                uv3=uv3,
                color=color,
                tangent=tangent,
            ),
        }
    )
    job_id = metadata["job_id"]
    job_dir = asset_export_store.job_path(job_id)
    output_root = asset_export_service.resolve_output_root(job_dir, str(capture_file), capture_file.name)
    asset_export_store.update_metadata(
        job_id,
        {
            "artifacts": {
                "capture_file": str(capture_file),
                "output_root": str(output_root),
            },
            "result": {
                "output_root": str(output_root),
            },
        },
    )
    try:
        asset_export_service.run_export(
            job_id=job_id,
            capture_path=capture_file,
            output_root=output_root,
            export_scope=requested_scope,
            pass_id=pass_id.strip(),
            pass_name=pass_name.strip(),
            pass_start_id=pass_start_id.strip(),
            pass_start=pass_start.strip(),
            pass_end_id=pass_end_id.strip(),
            pass_end=pass_end.strip(),
            export_fbx=str(export_fbx).lower() in {"true", "1", "yes", "on"},
            export_obj=str(export_obj).lower() in {"true", "1", "yes", "on"},
            texture_format=requested_texture_format,
            flip_texture_y=requested_flip_texture_y,
            mapping_override=_extract_mapping_form(
                position=position,
                normal=normal,
                uv0=uv0,
                uv1=uv1,
                uv2=uv2,
                uv3=uv3,
                color=color,
                tangent=tangent,
            ),
            isolated=True,
        )
    except Exception as exc:
        asset_export_store.update_metadata(
            job_id,
            {
                "status": "failed",
                "progress": {
                    "stage": "failed",
                    "message": str(exc),
                    "current": 0,
                    "total": 0,
                },
            },
        )
        raise HTTPException(status_code=500, detail=f"资产导出失败: {exc}") from exc
    return asset_export_store.get_job_detail(job_id)


@app.post("/api/asset-export/jobs/{job_id}/convert-csv")
async def convert_asset_export_csv(
    job_id: str,
    csv_file: UploadFile = File(...),
    csv_source_path: str = Form(""),
    output_format: str = Form("fbx"),
    position: str = Form(""),
    normal: str = Form(""),
    uv0: str = Form(""),
    uv1: str = Form(""),
    uv2: str = Form(""),
    uv3: str = Form(""),
    color: str = Form(""),
    tangent: str = Form(""),
    flip_texture_y: str = Form("false"),
) -> dict:
    _ensure_csv_file(csv_file.filename)
    content = await csv_file.read()
    try:
        job_dir = asset_export_store.job_path(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="资产导出任务不存在")
    upload_name = Path(csv_file.filename).name
    csv_input_dir = job_dir / "inputs" / "manual_csv"
    csv_input_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_input_dir / upload_name
    csv_path.write_bytes(content)

    mapping = {
        "position": position.strip(),
        "normal": normal.strip(),
        "uv0": uv0.strip(),
        "uv1": uv1.strip(),
        "uv2": uv2.strip(),
        "uv3": uv3.strip(),
        "color": color.strip(),
        "tangent": tangent.strip(),
    }
    if not mapping["position"]:
        raise HTTPException(status_code=400, detail="Position 列映射不能为空")

    output_format = output_format.strip().lower() or "fbx"
    if output_format not in {"fbx", "obj"}:
        raise HTTPException(status_code=400, detail="output_format 只支持 fbx 或 obj")
    return _run_csv_conversion_for_job(
        job_id=job_id,
        csv_sources=[csv_path],
        csv_files=[csv_path],
        output_format=output_format,
        mapping=mapping,
        flip_texture_y=str(flip_texture_y).lower() in {"true", "1", "yes", "on"},
    )


@app.post("/api/asset-export/jobs/{job_id}/convert-csv/by-path")
async def convert_asset_export_csv_by_path(
    job_id: str,
    csv_path: str = Form(...),
    output_format: str = Form("fbx"),
    position: str = Form(""),
    normal: str = Form(""),
    uv0: str = Form(""),
    uv1: str = Form(""),
    uv2: str = Form(""),
    uv3: str = Form(""),
    color: str = Form(""),
    tangent: str = Form(""),
    flip_texture_y: str = Form("false"),
) -> dict:
    csv_sources, csv_files = _collect_csv_targets(csv_path, "csv_path")
    mapping = {
        "position": position.strip(),
        "normal": normal.strip(),
        "uv0": uv0.strip(),
        "uv1": uv1.strip(),
        "uv2": uv2.strip(),
        "uv3": uv3.strip(),
        "color": color.strip(),
        "tangent": tangent.strip(),
    }
    if not mapping["position"]:
        raise HTTPException(status_code=400, detail="Position 列映射不能为空")

    output_format = output_format.strip().lower() or "fbx"
    if output_format not in {"fbx", "obj"}:
        raise HTTPException(status_code=400, detail="output_format 只支持 fbx 或 obj")
    return _run_csv_conversion_for_job(
        job_id=job_id,
        csv_sources=csv_sources,
        csv_files=csv_files,
        output_format=output_format,
        mapping=mapping,
        flip_texture_y=str(flip_texture_y).lower() in {"true", "1", "yes", "on"},
    )


@app.post("/api/asset-export/convert-csv/by-path")
async def convert_asset_export_csv_by_path_standalone(
    csv_path: str = Form(...),
    output_format: str = Form("fbx"),
    position: str = Form(""),
    normal: str = Form(""),
    uv0: str = Form(""),
    uv1: str = Form(""),
    uv2: str = Form(""),
    uv3: str = Form(""),
    color: str = Form(""),
    tangent: str = Form(""),
    flip_texture_y: str = Form("false"),
) -> dict:
    csv_sources, csv_files = _collect_csv_targets(csv_path, "csv_path")
    mapping = _extract_mapping_form(
        position=position,
        normal=normal,
        uv0=uv0,
        uv1=uv1,
        uv2=uv2,
        uv3=uv3,
        color=color,
        tangent=tangent,
    )
    if not mapping["position"]:
        raise HTTPException(status_code=400, detail="Position 列映射不能为空")
    requested_format = output_format.strip().lower() or "fbx"
    if requested_format not in {"fbx", "obj"}:
        raise HTTPException(status_code=400, detail="output_format 只支持 fbx 或 obj")
    output_root = _common_output_root([path.parent if path.is_file() else path for path in csv_sources])
    job_id = _create_manual_csv_conversion_job(
        csv_source_text=csv_path,
        output_format=requested_format,
        mapping=mapping,
        output_root=output_root,
    )
    return _run_csv_conversion_for_job(
        job_id=job_id,
        csv_sources=csv_sources,
        csv_files=csv_files,
        output_format=requested_format,
        mapping=mapping,
        flip_texture_y=str(flip_texture_y).lower() in {"true", "1", "yes", "on"},
    )


@app.post("/api/asset-export/convert-csv")
async def convert_asset_export_csv_standalone(
    csv_file: UploadFile = File(...),
    csv_source_path: str = Form(""),
    output_format: str = Form("fbx"),
    position: str = Form(""),
    normal: str = Form(""),
    uv0: str = Form(""),
    uv1: str = Form(""),
    uv2: str = Form(""),
    uv3: str = Form(""),
    color: str = Form(""),
    tangent: str = Form(""),
    flip_texture_y: str = Form("false"),
) -> dict:
    _ensure_csv_file(csv_file.filename)
    mapping = _extract_mapping_form(
        position=position,
        normal=normal,
        uv0=uv0,
        uv1=uv1,
        uv2=uv2,
        uv3=uv3,
        color=color,
        tangent=tangent,
    )
    if not mapping["position"]:
        raise HTTPException(status_code=400, detail="Position 列映射不能为空")
    requested_format = output_format.strip().lower() or "fbx"
    if requested_format not in {"fbx", "obj"}:
        raise HTTPException(status_code=400, detail="output_format 只支持 fbx 或 obj")

    output_root = ""
    source_text = csv_source_path.strip()
    if source_text:
        source_candidate = Path(source_text).expanduser()
        output_root = str(source_candidate.parent if source_candidate.suffix.lower() == ".csv" else source_candidate)

    job_id = _create_manual_csv_conversion_job(
        csv_source_text=source_text,
        output_format=requested_format,
        mapping=mapping,
        output_root=output_root,
    )
    job_dir = asset_export_store.job_path(job_id)
    content = await csv_file.read()
    upload_name = Path(csv_file.filename).name
    csv_input_dir = job_dir / "inputs" / "manual_csv"
    csv_input_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_input_dir / upload_name
    csv_path.write_bytes(content)
    return _run_csv_conversion_for_job(
        job_id=job_id,
        csv_sources=[csv_path],
        csv_files=[csv_path],
        output_format=requested_format,
        mapping=mapping,
        flip_texture_y=str(flip_texture_y).lower() in {"true", "1", "yes", "on"},
    )


@app.get("/api/asset-export/jobs/{job_id}/artifact")
async def get_asset_export_artifact(job_id: str, path: str) -> FileResponse:
    try:
        job_dir = asset_export_store.job_path(job_id).resolve()
        metadata = asset_export_store.load_metadata(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="资产导出任务不存在")

    target = Path(path)
    candidate = target if target.is_absolute() else (job_dir / target)
    candidate = candidate.resolve()
    allowed_roots = [job_dir]

    output_root_text = metadata.get("result", {}).get("output_root") or metadata.get("artifacts", {}).get("output_root")
    if output_root_text:
        allowed_roots.append(Path(output_root_text).resolve())

    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="不允许访问该文件")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=str(candidate), filename=candidate.name)


@app.post("/api/renderdoc-cmp/compare")
async def run_renderdoc_cmp(
    base_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    strict_mode: str = Form("false"),
    renderdoc_dir: str = Form(""),
    malioc_path: str = Form(""),
    verbose: str = Form("false"),
) -> dict:
    _ensure_rdc_file(base_file.filename)
    _ensure_rdc_file(new_file.filename)

    title = f"renderdoc_cmp: {base_file.filename} vs {new_file.filename}"
    metadata = cmp_service.create_job(title=title)
    job_id = metadata["job_id"]
    base_path = cmp_service.save_input_file(job_id, "base.rdc", await base_file.read())
    new_path = cmp_service.save_input_file(job_id, "new.rdc", await new_file.read())

    cmp_service.update_metadata(
        job_id,
        {
            "status": "running",
            "inputs": {
                "base_file": str(base_path.relative_to(base_path.parents[1])),
                "new_file": str(new_path.relative_to(new_path.parents[1])),
            },
        },
    )

    try:
        # Same as /compare/by-path: run in worker thread so the FastAPI event
        # loop keeps serving other requests during the multi-minute cmp.
        await run_in_threadpool(
            cmp_service.run_compare,
            job_id,
            base_path,
            new_path,
            str(strict_mode).lower() in {"true", "1", "yes", "on"},
            renderdoc_dir,
            malioc_path,
            str(verbose).lower() in {"true", "1", "yes", "on"},
        )
    except Exception as exc:
        cmp_service.update_metadata(job_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"renderdoc_cmp 执行失败: {exc}") from exc

    return cmp_service.get_job_detail(job_id)


@app.post("/api/renderdoc-perf/analyze")
async def run_renderdoc_perf(
    capture_file: UploadFile = File(...),
    renderdoc_dir: str = Form(""),
) -> dict:
    _ensure_rdc_file(capture_file.filename)
    title = f"renderdoc_perf: {capture_file.filename}"
    metadata = perf_service.create_job(title=title)
    job_id = metadata["job_id"]
    saved_capture = perf_service.store.save_input_file(job_id, "capture.rdc", await capture_file.read())
    metadata = perf_service.store.update_metadata(
        job_id,
        {
            "status": "running",
            "inputs": {
                "capture_file": str(Path(saved_capture).relative_to(Path(saved_capture).parents[1])),
                "renderdoc_dir_requested": renderdoc_dir.strip(),
            },
            "progress": {
                "stage": "init",
                "message": "已创建任务，等待 worker 启动…",
                "updated_at": _utc_now_iso(),
            },
        },
    )
    _launch_perf_analysis_background(
        job_id=job_id,
        capture_file=Path(saved_capture),
        renderdoc_dir=renderdoc_dir,
    )
    return {"job_id": job_id, "status": "running", "metadata": metadata}




if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=app_config.DEFAULT_HOST, port=app_config.DEFAULT_PORT, reload=False)
