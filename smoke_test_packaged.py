from __future__ import annotations

import asyncio
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


PROJECT_ROOT = Path(r"G:\RenderdocSKillEvn")
EXE_PATH = Path(r"G:\RenderdocDiffTools\RenderdocDiffPortable\RenderdocDiffTools.exe")
USER_DATA = EXE_PATH.parent / "user_data"
LOG_PATH = USER_DATA / "logs" / "launcher.log"


def post_form(url: str, data: dict[str, str], timeout: int = 600) -> dict[str, Any]:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_port(timeout_seconds: int = 30) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if LOG_PATH.exists():
            text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
            for line in reversed(text.splitlines()):
                if "port=" in line:
                    return int(line.rsplit("port=", 1)[1].strip())
        time.sleep(0.3)
    raise RuntimeError("launcher.log 中未找到端口")


def wait_for_health(base_url: str, timeout_seconds: int = 30) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"健康检查超时: {last_error}")


def choose_rdc_files() -> tuple[Path, Path]:
    captures = sorted(PROJECT_ROOT.glob("*.rdc"))
    if len(captures) < 2:
        raise RuntimeError("未找到足够的 .rdc 测试文件")
    return captures[0], captures[1]


def choose_csv_file() -> Path:
    candidates = sorted(PROJECT_ROOT.glob("*.csv"))
    if not candidates:
        candidates = sorted((PROJECT_ROOT / "export_jobs").rglob("*.csv"))
    if not candidates:
        raise RuntimeError("未找到 CSV 测试文件")
    return candidates[0]


def cleanup_old_log() -> None:
    if LOG_PATH.exists():
        try:
            LOG_PATH.unlink()
        except PermissionError:
            pass


def stop_process_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)


def stop_existing_portable_processes() -> None:
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'RenderdocDiffTools.exe'\" | "
        f"Where-Object {{ $_.ExecutablePath -eq '{EXE_PATH}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False, capture_output=True, text=True)
    time.sleep(1)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def pick_export_pass(scan_payload: dict[str, Any]) -> dict[str, Any]:
    passes = scan_payload.get("passes") or []
    for item in passes:
        name = (item.get("display_name") or item.get("name") or "").lower()
        if "waterhair" in name or "glitter" in name:
            return item
    if passes:
        return passes[0]
    raise RuntimeError("未扫描到可导出的 pass")


def build_convert_mapping(inspect_result: dict[str, Any]) -> dict[str, str]:
    suggested = inspect_result.get("suggested_mapping") or {}
    mapping = {
        "output_format": "fbx",
        "position": str(suggested.get("position") or ""),
        "normal": str(suggested.get("normal") or ""),
        "uv0": str(suggested.get("uv0") or ""),
        "uv1": str(suggested.get("uv1") or ""),
        "uv2": str(suggested.get("uv2") or ""),
        "uv3": str(suggested.get("uv3") or ""),
        "color": str(suggested.get("color") or ""),
        "tangent": str(suggested.get("tangent") or ""),
    }
    ensure(bool(mapping["position"]), "CSV 自动识别没有给出 position 列")
    return mapping


def run_api_regression(base_url: str, before_rdc: Path, after_rdc: Path, csv_path: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}

    analyze = post_form(
        f"{base_url}/api/analyze/by-path",
        {
            "before_path": str(before_rdc),
            "after_path": str(after_rdc),
            "pass_name": "M_Matcap_Glitter_SSS_Trans SK_OG_069_Wai_001",
            "issue": "面部亮度异常",
            "eid_before": "575",
            "eid_after": "610",
        },
        timeout=900,
    )
    ensure(analyze.get("metadata", {}).get("status") == "completed", "问题诊断未完成")
    print("analyze_session:", analyze["metadata"]["session_id"], analyze["metadata"]["status"])
    results["analyze_session_id"] = analyze["metadata"]["session_id"]

    cmp_result = post_form(
        f"{base_url}/api/renderdoc-cmp/compare/by-path",
        {
            "base_path": str(before_rdc),
            "new_path": str(after_rdc),
            "strict_mode": "false",
            "verbose": "false",
            "renderdoc_dir": "",
            "malioc_path": "",
        },
        timeout=900,
    )
    ensure(cmp_result.get("metadata", {}).get("status") == "completed", "性能 Diff 未完成")
    ensure(bool(cmp_result.get("report_url")), "性能 Diff 缺少 HTML 报告地址")
    print("cmp_job:", cmp_result["metadata"]["job_id"], cmp_result["metadata"]["status"])
    results["cmp_job_id"] = cmp_result["metadata"]["job_id"]

    perf_first = post_form(
        f"{base_url}/api/renderdoc-perf/analyze/by-path",
        {"capture_path": str(before_rdc)},
        timeout=900,
    )
    rows_first = (perf_first.get("analysis") or {}).get("rows") or []
    ensure(perf_first.get("metadata", {}).get("status") == "completed", "第一次性能分析未完成")
    ensure(bool(rows_first), "第一次性能分析没有 rows")
    top_first = rows_first[0]
    preview_result = post_form(
        f"{base_url}/api/renderdoc-perf/jobs/{perf_first['metadata']['job_id']}/draw-preview",
        {"eid": str(top_first.get("eid") or "")},
        timeout=300,
    )
    ensure(bool(preview_result.get("url")), "第一次性能分析未生成线框预览")
    print("perf_job_first:", perf_first["metadata"]["job_id"], len(rows_first), top_first.get("eid"))
    results["perf_job_first"] = perf_first["metadata"]["job_id"]

    perf_second = post_form(
        f"{base_url}/api/renderdoc-perf/analyze/by-path",
        {"capture_path": str(after_rdc)},
        timeout=900,
    )
    rows_second = (perf_second.get("analysis") or {}).get("rows") or []
    ensure(perf_second.get("metadata", {}).get("status") == "completed", "第二次性能分析未完成")
    ensure(bool(rows_second), "第二次性能分析没有 rows")
    print("perf_job_second:", perf_second["metadata"]["job_id"], len(rows_second), rows_second[0].get("eid"))
    results["perf_job_second"] = perf_second["metadata"]["job_id"]

    scan = post_form(
        f"{base_url}/api/asset-export/scan-passes/by-path",
        {"capture_path": str(before_rdc)},
        timeout=300,
    )
    ensure(bool(scan.get("passes")), "资产导出未读取到 pass 列表")
    print("scan_passes:", len(scan["passes"]))
    chosen_pass = pick_export_pass(scan)
    results["scan_pass_count"] = len(scan["passes"])

    export_result = post_form(
        f"{base_url}/api/asset-export/jobs/by-path",
        {
            "capture_path": str(before_rdc),
            "export_scope": "single",
            "pass_id": str(chosen_pass.get("id") or ""),
            "pass_name": str(chosen_pass.get("display_name") or chosen_pass.get("name") or ""),
            "pass_start_id": "",
            "pass_start": "",
            "pass_end_id": "",
            "pass_end": "",
            "export_fbx": "true",
            "export_obj": "false",
            "texture_format": "png",
            "notes": "packaged smoke test",
        },
        timeout=900,
    )
    job_id = export_result["metadata"]["job_id"]
    ensure(export_result.get("metadata", {}).get("status") == "completed", "资产导出任务未完成")
    print("export_job:", job_id, export_result["metadata"]["status"])
    results["export_job_id"] = job_id

    inspect_result = post_form(
        f"{base_url}/api/asset-export/csv-inspect/by-path",
        {"csv_path": str(csv_path)},
        timeout=300,
    )
    ensure(bool(inspect_result.get("headers")), "CSV 列识别失败")
    print("csv_headers:", len(inspect_result["headers"]))

    convert_result = post_form(
        f"{base_url}/api/asset-export/jobs/{job_id}/convert-csv/by-path",
        {
            "csv_path": str(csv_path),
            **build_convert_mapping(inspect_result),
        },
        timeout=300,
    )
    ensure(convert_result.get("metadata", {}).get("status") == "completed", "CSV 转换未完成")
    print("convert_job:", convert_result["metadata"]["job_id"], convert_result["metadata"]["status"])
    results["convert_job_id"] = convert_result["metadata"]["job_id"]

    return results


async def close_setup_if_needed(page: Any) -> None:
    close_button = page.locator("#setup-close-btn")
    if await close_button.count():
        try:
            await close_button.click(timeout=1000)
            await page.wait_for_timeout(300)
        except Exception:
            pass


async def run_ui_regression(base_url: str, before_rdc: Path, after_rdc: Path, csv_path: Path) -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    dialogs: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 980}, device_scale_factor=1)

        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def handle_console(message: Any) -> None:
            if message.type == "error":
                text = message.text or ""
                if "favicon" not in text.lower():
                    console_errors.append(text)

        page.on("console", handle_console)

        async def handle_dialog(dialog: Any) -> None:
            dialogs.append(dialog.message)
            await dialog.accept()

        page.on("dialog", handle_dialog)

        await page.goto(base_url, wait_until="networkidle")
        await close_setup_if_needed(page)

        for tab_name in ("diagnose", "cmp", "perf", "asset-export"):
            await page.click(f'.tab-btn[data-tab="{tab_name}"]')
            workspace = page.locator(f"#workspace-{tab_name}")
            await workspace.wait_for(state="visible", timeout=5000)

        await page.click('.tab-btn[data-tab="cmp"]')
        await page.fill("#cmp-base-path", str(before_rdc))
        await page.fill("#cmp-new-path", str(after_rdc))
        await page.click("#cmp-run-btn")
        await page.locator("#cmp-summary").wait_for(state="visible", timeout=900000)
        await page.wait_for_function(
            "() => document.querySelector('#cmp-summary')?.textContent?.includes('completed')",
            timeout=900000,
        )
        cmp_frame_src = await page.locator("#cmp-report-frame").get_attribute("src")
        ensure(bool(cmp_frame_src), "UI 性能 Diff 运行后 iframe 没有报告地址")

        await page.click('.tab-btn[data-tab="perf"]')
        await page.fill("#perf-capture-path", str(before_rdc))
        await page.click("#perf-run-btn")
        await page.wait_for_function(
            "() => document.querySelectorAll('#perf-table-wrap tbody tr').length > 0",
            timeout=900000,
        )
        first_preview = page.locator(".perf-preview-trigger").first
        await first_preview.hover()
        await page.wait_for_function(
            "() => !document.querySelector('#perf-preview-panel')?.classList.contains('hidden')",
            timeout=10000,
        )
        preview_src = await page.locator("#perf-preview-panel-image").get_attribute("src")
        ensure(bool(preview_src), "UI 性能预览面板没有加载图片")
        await page.click("body", position={"x": 20, "y": 20})

        await page.click('.tab-btn[data-tab="asset-export"]')
        await page.fill("#asset-capture-source-path", str(before_rdc))
        await page.click("#asset-pass-scan-btn")
        await page.wait_for_function(
            "() => document.querySelector('#asset-pass-name')?.options.length > 0",
            timeout=300000,
        )
        await page.fill("#asset-csv-source-path", str(csv_path))
        await page.click("#asset-csv-inspect-btn")
        await page.wait_for_function(
            "() => document.querySelector('#mapping-position')?.value?.length > 0",
            timeout=300000,
        )

        await browser.close()

    ensure(not dialogs, f"UI 交互出现弹窗错误: {dialogs[:1]}")
    ensure(not page_errors, f"UI 出现未捕获异常: {page_errors[:1]}")
    actionable_console_errors = [item for item in console_errors if "TypeError" in item or "ReferenceError" in item or "Failed to fetch" in item]
    ensure(not actionable_console_errors, f"UI 控制台报错: {actionable_console_errors[:1]}")


def main() -> None:
    before_rdc, after_rdc = choose_rdc_files()
    csv_path = choose_csv_file()
    stop_existing_portable_processes()
    cleanup_old_log()

    proc = subprocess.Popen([str(EXE_PATH)])
    try:
        port = wait_for_port()
        base_url = f"http://127.0.0.1:{port}"
        health = wait_for_health(base_url)
        print(
            "health:",
            json.dumps({"rdc": health["rdc"]["ok"], "cmp": health["renderdoc_cmp"]["ok"]}, ensure_ascii=False),
        )

        results = run_api_regression(base_url, before_rdc, after_rdc, csv_path)
        asyncio.run(run_ui_regression(base_url, before_rdc, after_rdc, csv_path))
        print("ui_regression: passed")
        print("summary:", json.dumps(results, ensure_ascii=False))
    finally:
        stop_process_tree(proc.pid)
        time.sleep(1)


if __name__ == "__main__":
    main()
