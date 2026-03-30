from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(r"G:\RenderdocSKillEvn")
EXE_PATH = Path(r"G:\RenderdocDiffTools\RenderdocDiffPortable\RenderdocDiffTools.exe")
USER_DATA = EXE_PATH.parent / "user_data"
LOG_PATH = USER_DATA / "logs" / "launcher.log"


def post_form(url: str, data: dict[str, str], timeout: int = 600) -> dict:
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


def wait_for_health(base_url: str, timeout_seconds: int = 30) -> dict:
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
    candidates = sorted((PROJECT_ROOT / "export_jobs").rglob("*.csv"))
    if not candidates:
        raise RuntimeError("未找到 CSV 测试文件")
    return candidates[0]


def cleanup_old_log() -> None:
    if LOG_PATH.exists():
        LOG_PATH.unlink()


def stop_process_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)


def main() -> None:
    before_rdc, after_rdc = choose_rdc_files()
    csv_path = choose_csv_file()
    cleanup_old_log()

    proc = subprocess.Popen([str(EXE_PATH)])
    try:
        port = wait_for_port()
        base_url = f"http://127.0.0.1:{port}"
        health = wait_for_health(base_url)
        print("health:", json.dumps({"rdc": health["rdc"]["ok"], "cmp": health["renderdoc_cmp"]["ok"]}, ensure_ascii=False))

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
        print("analyze_session:", analyze["metadata"]["session_id"], analyze["metadata"]["status"])

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
        print("cmp_job:", cmp_result["metadata"]["job_id"], cmp_result["metadata"]["status"])

        scan = post_form(
            f"{base_url}/api/asset-export/scan-passes/by-path",
            {
                "capture_path": str(before_rdc),
            },
            timeout=300,
        )
        print("scan_passes:", len(scan["passes"]))

        chosen_pass = None
        for item in scan["passes"]:
            name = (item.get("display_name") or item.get("name") or "").lower()
            if "waterhair" in name or "glitter" in name:
                chosen_pass = item
                break
        if not chosen_pass and scan["passes"]:
            chosen_pass = scan["passes"][0]
        if not chosen_pass:
            raise RuntimeError("未扫描到可导出的 pass")

        export_result = post_form(
            f"{base_url}/api/asset-export/jobs/by-path",
            {
                "capture_path": str(before_rdc),
                "export_scope": "single",
                "pass_id": chosen_pass.get("id", ""),
                "pass_name": chosen_pass.get("display_name") or chosen_pass.get("name") or "",
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
        print("export_job:", job_id, export_result["metadata"]["status"])

        inspect_result = post_form(
            f"{base_url}/api/asset-export/csv-inspect/by-path",
            {
                "csv_path": str(csv_path),
            },
            timeout=300,
        )
        print("csv_headers:", len(inspect_result["headers"]))

        convert_result = post_form(
            f"{base_url}/api/asset-export/jobs/{job_id}/convert-csv/by-path",
            {
                "csv_path": str(csv_path),
                "output_format": "fbx",
                "position": " in_POSITION0.x",
                "normal": " in_NORMAL0.x",
                "uv0": " in_TEXCOORD0.x",
                "uv1": "",
                "uv2": "",
                "uv3": "",
                "color": "",
                "tangent": "",
            },
            timeout=300,
        )
        print("convert_job:", convert_result["metadata"]["job_id"], convert_result["metadata"]["status"])
    finally:
        stop_process_tree(proc.pid)
        time.sleep(1)


if __name__ == "__main__":
    main()
