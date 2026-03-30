from __future__ import annotations

import json
import logging
import multiprocessing
import os
import socket
import sys
import threading
import time
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


def _pick_port(preferred: int) -> int:
    for candidate in (preferred, 8010, 8011, 8012, 8013):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    logger = _setup_logger()
    home = _portable_home()
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("RENDERDOC_WEBUI_HOME", str(home))
    chosen_port = _pick_port(_preferred_port())
    os.environ["RENDERDOC_WEBUI_PORT"] = str(chosen_port)
    logger.info("Launcher starting with home=%s port=%s", home, chosen_port)

    try:
        import webview
        from app.main import app
        from app.desktop_bridge import DesktopBridge
        import uvicorn

        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=chosen_port,
            log_level="info",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info("Starting uvicorn server thread")
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        app_url = f"http://127.0.0.1:{chosen_port}"
        deadline = time.time() + 30
        last_error = ""
        while time.time() < deadline:
            try:
                with urlopen(f"{app_url}/api/ping", timeout=5) as response:
                    if response.status == 200:
                        break
            except Exception as exc:  # pragma: no cover
                last_error = str(exc)
                time.sleep(0.3)
        else:
            raise RuntimeError(f"本地服务启动超时: {last_error or 'unknown error'}")

        logger.info("Opening desktop window at %s", app_url)
        window = webview.create_window(
            "RenderDoc 工具集",
            app_url,
            js_api=DesktopBridge(),
            width=1600,
            height=980,
            min_size=(1280, 820),
        )
        webview.start()

        logger.info("Desktop window closed, stopping uvicorn")
        server.should_exit = True
        server_thread.join(timeout=5)
        logger.info("Uvicorn server exited cleanly")
    except Exception as exc:
        logger.exception("Launcher failed: %s", exc)
        raise


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
