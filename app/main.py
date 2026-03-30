from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import app.config as app_config
from app.services.analyzer import AnalyzerService
from app.services.asset_export_service import AssetExportService
from app.services.asset_export_store import AssetExportStore
from app.services.chat_engine import ChatEngine
from app.services.csv_model_converter import ColumnMapping, CsvModelConverter
from app.services.eid_deep_dive import EidDeepDiveService
from app.services.renderdoc_cmp_service import RenderdocCmpService
from app.services.renderdoc_perf_service import RenderdocPerfService
from app.services.renderdoc_perf_store import RenderdocPerfStore
from app.services.session_store import SessionStore
from app.services.ue_source_scanner import UESourceScannerService


app = FastAPI(title="RenderDoc Compare Diagnose UI", version="0.1.0")
app.mount("/static", StaticFiles(directory=app_config.STATIC_DIR), name="static")
app.mount("/cmp-session-files", StaticFiles(directory=app_config.CMP_SESSION_ROOT), name="cmp-session-files")
app.mount("/export-job-files", StaticFiles(directory=app_config.EXPORT_JOB_ROOT), name="export-job-files")
app.mount("/perf-session-files", StaticFiles(directory=app_config.PERF_SESSION_ROOT), name="perf-session-files")
templates = Jinja2Templates(directory=str(app_config.TEMPLATE_DIR))

store = SessionStore()
asset_export_store = AssetExportStore()
perf_store = RenderdocPerfStore()
analyzer = AnalyzerService()
chat_engine = ChatEngine()
eid_deep_dive_service = EidDeepDiveService()
ue_source_scanner = UESourceScannerService()
cmp_service = RenderdocCmpService()
perf_service = RenderdocPerfService(perf_store)
csv_model_converter = CsvModelConverter()
asset_export_service = AssetExportService(asset_export_store, csv_model_converter)


def _run_shell_command(command: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except FileNotFoundError as exc:
        return False, str(exc)
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


def _run_csv_conversion_for_job(
    *,
    job_id: str,
    csv_sources: list[Path],
    csv_files: list[Path],
    output_format: str,
    mapping: dict,
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
            csv_model_converter.convert(
                csv_path=csv_file,
                output_path=output_path,
                mapping=ColumnMapping(**mapping),
                fmt=output_format,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"CSV 转换失败: {csv_file.name}: {exc}") from exc

        output_ref = asset_export_service._artifact_ref(job_dir, output_path)
        manual_conversions.append(
            {
                "csv_name": csv_file.name,
                "csv_source_path": str(csv_file),
                "output_format": output_format.upper(),
                "output_path": output_ref,
            }
        )
        if output_ref not in model_files:
            model_files.append(output_ref)

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


def _session_capture_paths(session_id: str) -> tuple[Path, Path]:
    metadata = store.load_metadata(session_id)
    inputs = metadata.get("inputs", {})
    before_source = (inputs.get("before_source_path") or "").strip()
    after_source = (inputs.get("after_source_path") or "").strip()
    if before_source and after_source:
        before_path = Path(before_source)
        after_path = Path(after_source)
        if before_path.exists() and after_path.exists():
            return before_path, after_path

    session_dir = Path(store.session_root) / session_id
    before_path = session_dir / "inputs" / "before.rdc"
    after_path = session_dir / "inputs" / "after.rdc"
    return before_path, after_path


def _refresh_runtime_services() -> None:
    global cmp_service
    global perf_service
    global chat_engine
    cmp_service = RenderdocCmpService()
    perf_service = RenderdocPerfService(perf_store)
    chat_engine = ChatEngine()


def _health_payload() -> dict:
    python_ok = True
    python_version = platform.python_version()
    rdc_ok, rdc_output = _run_shell_command(["rdc", "--version"])
    doctor_ok, doctor_output = _run_shell_command(["rdc", "doctor"])
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
        "analysis_script": {
            "ok": app_config.ANALYZER_SCRIPT.exists(),
            "path": str(app_config.ANALYZER_SCRIPT),
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


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    return store.list_sessions()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    try:
        return store.get_session_detail(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session 不存在")


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


@app.get("/api/asset-export/jobs")
async def list_asset_export_jobs() -> list[dict]:
    return asset_export_store.list_jobs()


@app.get("/api/asset-export/jobs/{job_id}")
async def get_asset_export_job(job_id: str) -> dict:
    try:
        return asset_export_store.get_job_detail(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="资产导出任务不存在")


@app.post("/api/analyze/by-path")
async def analyze_by_path(
    before_path: str = Form(...),
    after_path: str = Form(...),
    pass_name: str = Form(...),
    issue: str = Form(...),
    eid_before: str = Form(""),
    eid_after: str = Form(""),
) -> dict:
    if not pass_name.strip():
        raise HTTPException(status_code=400, detail="pass 为必填项")
    if not issue.strip():
        raise HTTPException(status_code=400, detail="问题描述为必填项")

    before_capture = _require_existing_file(before_path, ".rdc", "before_path")
    after_capture = _require_existing_file(after_path, ".rdc", "after_path")

    metadata = store.create_session(
        pass_name=pass_name.strip(),
        issue=issue.strip(),
        eid_before=eid_before.strip(),
        eid_after=eid_after.strip(),
    )
    session_id = metadata["session_id"]
    session_dir = Path(store.session_root) / session_id
    analysis_dir = session_dir / "analysis"

    store.update_metadata(
        session_id,
        {
            "status": "running",
            "inputs": {
                "before_file": str(before_capture),
                "after_file": str(after_capture),
                "before_source_path": str(before_capture),
                "after_source_path": str(after_capture),
            },
        },
    )

    try:
        analyzer.run_initial_analysis(
            before_file=before_capture,
            after_file=after_capture,
            pass_name=pass_name.strip(),
            issue=issue.strip(),
            out_dir=analysis_dir,
        )
        analysis_json = store.load_analysis_json(session_id) or {}
        ranked = analysis_json.get("ranked_causes", [])
        top = ranked[0] if ranked else {}
        store.update_metadata(
            session_id,
            {
                "status": "completed",
                "summary": {
                    "title": issue.strip()[:40] or "RenderDoc 分析",
                    "top_cause": top.get("title", ""),
                    "confidence": top.get("confidence", ""),
                },
            },
        )
    except Exception as exc:
        store.update_metadata(session_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"分析失败: {exc}") from exc

    return store.get_session_detail(session_id)


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

    try:
        cmp_service.run_compare(
            job_id=job_id,
            base_file=base_file,
            new_file=new_file,
            strict_mode=str(strict_mode).lower() in {"true", "1", "yes", "on"},
            renderdoc_dir=renderdoc_dir,
            malioc_path=malioc_path,
            verbose=str(verbose).lower() in {"true", "1", "yes", "on"},
        )
    except Exception as exc:
        cmp_service.update_metadata(job_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"renderdoc_cmp 执行失败: {exc}") from exc
    return cmp_service.get_job_detail(job_id)


@app.post("/api/renderdoc-perf/analyze/by-path")
async def run_renderdoc_perf_by_path(capture_path: str = Form(...)) -> dict:
    capture_file = _require_existing_file(capture_path, ".rdc", "capture_path")
    title = f"renderdoc_perf: {capture_file.name}"
    metadata = perf_service.create_job(title=title)
    job_id = metadata["job_id"]
    perf_service.store.update_metadata(
        job_id,
        {
            "status": "running",
            "inputs": {
                "capture_file": str(capture_file),
            },
        },
    )
    try:
        return perf_service.analyze_capture_isolated(job_id, capture_file)
    except Exception as exc:
        perf_service.store.update_metadata(job_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"性能分析失败: {exc}") from exc


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


@app.post("/api/analyze")
async def analyze(
    before_file: UploadFile = File(...),
    after_file: UploadFile = File(...),
    pass_name: str = Form(...),
    issue: str = Form(...),
    eid_before: str = Form(""),
    eid_after: str = Form(""),
) -> dict:
    if not pass_name.strip():
        raise HTTPException(status_code=400, detail="pass 为必填项")
    if not issue.strip():
        raise HTTPException(status_code=400, detail="问题描述为必填项")

    _ensure_rdc_file(before_file.filename)
    _ensure_rdc_file(after_file.filename)

    metadata = store.create_session(
        pass_name=pass_name.strip(),
        issue=issue.strip(),
        eid_before=eid_before.strip(),
        eid_after=eid_after.strip(),
    )
    session_id = metadata["session_id"]

    before_path = Path(store.save_input_file(session_id, "before.rdc", await before_file.read()))
    after_path = Path(store.save_input_file(session_id, "after.rdc", await after_file.read()))

    store.update_metadata(
        session_id,
        {
            "status": "running",
            "inputs": {
                "before_file": str(before_path.relative_to(before_path.parents[1])),
                "after_file": str(after_path.relative_to(after_path.parents[1])),
            },
        },
    )

    analysis_dir = Path(before_path.parents[1]) / "analysis"
    try:
        artifacts = analyzer.run_initial_analysis(
            before_file=before_path,
            after_file=after_path,
            pass_name=pass_name.strip(),
            issue=issue.strip(),
            out_dir=analysis_dir,
        )
        analysis_json = store.load_analysis_json(session_id) or {}
        ranked = analysis_json.get("ranked_causes", [])
        top = ranked[0] if ranked else {}
        store.update_metadata(
            session_id,
            {
                "status": "completed",
                "artifacts": {
                    "analysis_md": "analysis/analysis.md",
                    "analysis_json": "analysis/analysis.json",
                    "raw_diff": "analysis/raw_diff.txt",
                    "run_log": "analysis/run_log.txt",
                },
                "summary": {
                    "title": issue.strip()[:40] or "RenderDoc 分析",
                    "top_cause": top.get("title", ""),
                    "confidence": top.get("confidence", ""),
                },
            },
        )
    except Exception as exc:
        store.update_metadata(session_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"分析失败: {exc}") from exc

    return store.get_session_detail(session_id)


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
        result = cmp_service.run_compare(
            job_id=job_id,
            base_file=base_path,
            new_file=new_path,
            strict_mode=str(strict_mode).lower() in {"true", "1", "yes", "on"},
            renderdoc_dir=renderdoc_dir,
            malioc_path=malioc_path,
            verbose=str(verbose).lower() in {"true", "1", "yes", "on"},
        )
    except Exception as exc:
        cmp_service.update_metadata(job_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"renderdoc_cmp 执行失败: {exc}") from exc

    return cmp_service.get_job_detail(job_id)


@app.post("/api/renderdoc-perf/analyze")
async def run_renderdoc_perf(capture_file: UploadFile = File(...)) -> dict:
    _ensure_rdc_file(capture_file.filename)
    title = f"renderdoc_perf: {capture_file.filename}"
    metadata = perf_service.create_job(title=title)
    job_id = metadata["job_id"]
    saved_capture = perf_service.store.save_input_file(job_id, "capture.rdc", await capture_file.read())
    perf_service.store.update_metadata(
        job_id,
        {
            "status": "running",
            "inputs": {
                "capture_file": str(Path(saved_capture).relative_to(Path(saved_capture).parents[1])),
            },
        },
    )
    try:
        return perf_service.analyze_capture_isolated(job_id, saved_capture)
    except Exception as exc:
        perf_service.store.update_metadata(job_id, {"status": "failed"})
        raise HTTPException(status_code=500, detail=f"性能分析失败: {exc}") from exc


@app.post("/api/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    question: str = Form(...),
) -> dict:
    if not question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        session_detail = store.get_session_detail(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session 不存在")

    store.append_chat(session_id, "user", question.strip())
    result = chat_engine.answer(question.strip(), session_detail)
    store.append_chat(session_id, "assistant", result["answer"], result.get("sources"))
    return {
        "session_id": session_id,
        **result,
        "chat_history": store.load_chat(session_id),
    }


@app.post("/api/sessions/{session_id}/eid-deep-dive")
async def eid_deep_dive(
    session_id: str,
    eid_before: str = Form(...),
    eid_after: str = Form(...),
) -> dict:
    if not eid_before.strip() or not eid_after.strip():
        raise HTTPException(status_code=400, detail="before/after EID 都不能为空")

    try:
        metadata = store.load_metadata(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session 不存在")

    before_path, after_path = _session_capture_paths(session_id)
    if not before_path.exists() or not after_path.exists():
        raise HTTPException(status_code=400, detail="session 输入文件缺失")

    session_dir = Path(store.session_root) / session_id
    analysis_dir = session_dir / "analysis"
    try:
        eid_deep_dive_service.run(
            before_capture=before_path,
            after_capture=after_path,
            eid_before=eid_before.strip(),
            eid_after=eid_after.strip(),
            out_dir=analysis_dir,
        )
        deep_json = store.load_eid_deep_dive_json(session_id) or {}
        summary = deep_json.get("summary") or {}
        top_hypothesis = summary.get("top_hypothesis") or {}
        store.update_metadata(
            session_id,
            {
                "inputs": {
                    "eid_before": eid_before.strip(),
                    "eid_after": eid_after.strip(),
                },
                "artifacts": {
                    "eid_deep_dive_md": "analysis/eid_deep_dive.md",
                    "eid_deep_dive_json": "analysis/eid_deep_dive.json",
                },
                "summary": {
                    "top_cause": top_hypothesis.get("title", summary.get("conclusion", metadata.get("summary", {}).get("top_cause", ""))),
                    "confidence": top_hypothesis.get("confidence", summary.get("confidence", metadata.get("summary", {}).get("confidence", ""))),
                },
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"EID 深挖失败: {exc}") from exc

    return store.get_session_detail(session_id)


@app.post("/api/sessions/{session_id}/ue-source-scan")
async def ue_source_scan(
    session_id: str,
    project_root: str = Form(...),
) -> dict:
    if not project_root.strip():
        raise HTTPException(status_code=400, detail="project_root 不能为空")
    try:
        session_detail = store.get_session_detail(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session 不存在")

    session_dir = Path(store.session_root) / session_id
    analysis_dir = session_dir / "analysis"
    try:
        ue_source_scanner.run(Path(project_root.strip()), session_detail, analysis_dir)
        ue_scan_json = store.load_ue_scan_json(session_id) or {}
        summary = ue_scan_json.get("summary") or {}
        store.update_metadata(
            session_id,
            {
                "artifacts": {
                    "ue_scan_md": "analysis/ue_scan.md",
                    "ue_scan_json": "analysis/ue_scan.json",
                },
                "summary": {
                    "top_cause": session_detail["metadata"].get("summary", {}).get("top_cause", ""),
                    "confidence": session_detail["metadata"].get("summary", {}).get("confidence", ""),
                },
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"UE 源码扫描失败: {exc}") from exc

    return store.get_session_detail(session_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=app_config.DEFAULT_HOST, port=app_config.DEFAULT_PORT, reload=False)
