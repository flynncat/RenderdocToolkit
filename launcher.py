from __future__ import annotations

import json
import logging
import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser
from urllib.request import urlopen
from pathlib import Path


def _portable_home() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "user_data"
    return Path(__file__).resolve().parent / "user_data"


def _settings_path() -> Path:
    return _portable_home() / "config" / "settings.json"


def _setup_logger() -> logging.Logger:
    home = _portable_home()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("RenderdocDiffToolsLauncher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    file_handler = logging.FileHandler(log_dir / "launcher.log", encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def _preferred_port() -> int:
    settings_file = _settings_path()
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8", errors="replace"))
            value = settings.get("port")
            if value:
                return int(value)
        except Exception:
            pass
    return 8010


def _auto_open_browser_enabled() -> bool:
    """Whether to pop the default browser once the server is ready.

    Enabled by default for the friendly double-click experience.  Set
    ``RENDERDOC_WEBUI_NO_BROWSER=1`` for unattended / server deployments
    where opening a browser on the host makes no sense.
    """
    return os.getenv("RENDERDOC_WEBUI_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes", "on"}


def _open_browser_when_ready(app_url: str, logger: logging.Logger) -> None:
    """Poll ``/api/ping`` in a background thread, then open the browser.

    Runs as a daemon thread so it never blocks ``uvicorn.run`` shutdown.
    """

    def _worker() -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urlopen(f"{app_url}/api/ping", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.3)
        else:
            logger.warning("Service not ready within 30s; skipping browser auto-open")
            return
        try:
            webbrowser.open(app_url)
            logger.info("Opened default browser at %s", app_url)
        except Exception as exc:  # pragma: no cover (platform-specific)
            logger.warning("Failed to auto-open browser: %s", exc)

    threading.Thread(target=_worker, name="open-browser", daemon=True).start()


def _resolve_host() -> str:
    """Bind address for the web service.

    Defaults to ``127.0.0.1`` so a portable double-click stays local-only.
    Operators preparing a network deployment can set
    ``RENDERDOC_WEBUI_HOST=0.0.0.0`` (or a specific NIC address) to expose
    the service to other machines.
    """
    host = os.getenv("RENDERDOC_WEBUI_HOST", "").strip()
    return host or "127.0.0.1"


def _pick_port(preferred: int, host: str) -> int:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    for candidate in (preferred, 8010, 8011, 8012, 8013):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex((probe_host, candidate)) != 0:
                return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((probe_host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    logger = _setup_logger()
    home = _portable_home()
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("RENDERDOC_WEBUI_HOME", str(home))
    host = _resolve_host()
    chosen_port = _pick_port(_preferred_port(), host)
    os.environ["RENDERDOC_WEBUI_PORT"] = str(chosen_port)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    app_url = f"http://{display_host}:{chosen_port}"
    logger.info("Launcher starting with home=%s host=%s port=%s", home, host, chosen_port)

    try:
        from app.main import app
        import uvicorn

        banner = (
            "\n"
            "============================================================\n"
            "  RenderDoc 工具集 — Web 服务已启动\n"
            f"  本机访问:   {app_url}\n"
            + ("  局域网访问: http://<本机IP>:%d\n" % chosen_port if host == "0.0.0.0" else "")
            + "  按 Ctrl+C 停止服务\n"
            "============================================================\n"
        )
        print(banner, flush=True)
        logger.info("Serving web UI at %s (bind host=%s)", app_url, host)

        if _auto_open_browser_enabled():
            _open_browser_when_ready(app_url, logger)

        uvicorn.run(
            app,
            host=host,
            port=chosen_port,
            log_level="info",
            log_config=None,
            access_log=False,
        )
    except Exception as exc:
        logger.exception("Launcher failed: %s", exc)
        raise


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
