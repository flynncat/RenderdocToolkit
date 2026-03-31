from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[1]


def _app_home() -> Path:
    explicit = os.getenv("RENDERDOC_WEBUI_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "user_data"
    return Path(__file__).resolve().parents[1]


RESOURCE_ROOT = _resource_root()
APP_HOME = _app_home()
APP_DIR = RESOURCE_ROOT / "app"
DOCS_DIR = RESOURCE_ROOT / "docs"
STATIC_DIR = APP_DIR / "static"
TEMPLATE_DIR = APP_DIR / "templates"
SESSION_ROOT = APP_HOME / "sessions"
CMP_SESSION_ROOT = APP_HOME / "cmp_sessions"
EXPORT_JOB_ROOT = APP_HOME / "export_jobs"
PERF_SESSION_ROOT = APP_HOME / "perf_sessions"
LOG_ROOT = APP_HOME / "logs"
CONFIG_ROOT = APP_HOME / "config"
SETTINGS_FILE = CONFIG_ROOT / "settings.json"


def _read_settings_file() -> dict:
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig", errors="replace"))


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return _read_settings_file()
        except Exception:
            return {}
    return {}


SETTINGS = _load_settings()

DEFAULT_SETTINGS = {
    "setup_completed": False,
    "host": "127.0.0.1",
    "port": 8010,
    "llm_provider": "local",
    "openai_base_url": "",
    "openai_api_key": "",
    "openai_model": "",
    "openai_timeout_seconds": 60,
    "llm_max_context_chars": 24000,
    "renderdoc_python_path": "",
    "renderdoc_cmp_root": "",
}


def _get_setting(name: str, default: str = "") -> str:
    env = os.getenv(name)
    if env is not None and env != "":
        return env

    key_map = {
        "RENDERDOC_WEBUI_HOST": "host",
        "RENDERDOC_WEBUI_PORT": "port",
        "RENDERDOC_WEBUI_LLM_PROVIDER": "llm_provider",
        "RENDERDOC_WEBUI_OPENAI_BASE_URL": "openai_base_url",
        "RENDERDOC_WEBUI_OPENAI_API_KEY": "openai_api_key",
        "RENDERDOC_WEBUI_OPENAI_MODEL": "openai_model",
        "RENDERDOC_WEBUI_OPENAI_TIMEOUT_SECONDS": "openai_timeout_seconds",
        "RENDERDOC_WEBUI_LLM_MAX_CONTEXT_CHARS": "llm_max_context_chars",
        "RENDERDOC_PYTHON_PATH": "renderdoc_python_path",
        "RENDERDOC_WEBUI_CMP_ROOT": "renderdoc_cmp_root",
    }
    mapped = key_map.get(name)
    if mapped and mapped in SETTINGS:
        value = str(SETTINGS[mapped]).strip()
        if value != "":
            return value
    return default


ANALYZER_SCRIPT = (
    RESOURCE_ROOT
    / ".cursor"
    / "skills"
    / "renderdoc-compare-diagnose"
    / "scripts"
    / "compare_pass_issue.py"
)

RENDERDOC_CMP_ROOT = Path(
    _get_setting(
        "RENDERDOC_WEBUI_CMP_ROOT",
        str(RESOURCE_ROOT / "external_tools" / "renderdoccmp"),
    )
).expanduser().resolve()
RENDERDOC_CMP_SCRIPT = RENDERDOC_CMP_ROOT / "rdc_compare_ultimate.py"

DEFAULT_HOST = _get_setting("RENDERDOC_WEBUI_HOST", "127.0.0.1")
DEFAULT_PORT = int(_get_setting("RENDERDOC_WEBUI_PORT", "8010"))

LLM_PROVIDER = _get_setting("RENDERDOC_WEBUI_LLM_PROVIDER", "local")
OPENAI_BASE_URL = _get_setting("RENDERDOC_WEBUI_OPENAI_BASE_URL", "").strip()
OPENAI_API_KEY = _get_setting("RENDERDOC_WEBUI_OPENAI_API_KEY", "").strip()
OPENAI_MODEL = _get_setting("RENDERDOC_WEBUI_OPENAI_MODEL", "").strip()
OPENAI_TIMEOUT_SECONDS = float(_get_setting("RENDERDOC_WEBUI_OPENAI_TIMEOUT_SECONDS", "60"))
LLM_MAX_CONTEXT_CHARS = int(_get_setting("RENDERDOC_WEBUI_LLM_MAX_CONTEXT_CHARS", "24000"))
RENDERDOC_PYTHON_PATH = _get_setting("RENDERDOC_PYTHON_PATH", "").strip()

if RENDERDOC_PYTHON_PATH:
    os.environ["RENDERDOC_PYTHON_PATH"] = RENDERDOC_PYTHON_PATH

for path in (SESSION_ROOT, CMP_SESSION_ROOT, EXPORT_JOB_ROOT, PERF_SESSION_ROOT, LOG_ROOT, CONFIG_ROOT, STATIC_DIR, TEMPLATE_DIR):
    path.mkdir(parents=True, exist_ok=True)

def current_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(_read_settings_file())
        except Exception:
            pass
    settings["host"] = DEFAULT_HOST
    settings["port"] = DEFAULT_PORT
    settings["llm_provider"] = LLM_PROVIDER
    settings["openai_base_url"] = OPENAI_BASE_URL
    settings["openai_api_key"] = OPENAI_API_KEY
    settings["openai_model"] = OPENAI_MODEL
    settings["openai_timeout_seconds"] = OPENAI_TIMEOUT_SECONDS
    settings["llm_max_context_chars"] = LLM_MAX_CONTEXT_CHARS
    settings["renderdoc_python_path"] = RENDERDOC_PYTHON_PATH or str(settings.get("renderdoc_python_path") or "")
    settings["renderdoc_cmp_root"] = str(RENDERDOC_CMP_ROOT) if str(RENDERDOC_CMP_ROOT) else str(settings.get("renderdoc_cmp_root") or "")
    return settings


def persist_settings(patch: dict) -> dict:
    settings = current_settings()
    settings.update({k: v for k, v in patch.items() if v is not None})
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_runtime_settings(settings)
    return settings


def apply_runtime_settings(settings: dict) -> None:
    global SETTINGS
    global DEFAULT_HOST
    global DEFAULT_PORT
    global LLM_PROVIDER
    global OPENAI_BASE_URL
    global OPENAI_API_KEY
    global OPENAI_MODEL
    global OPENAI_TIMEOUT_SECONDS
    global LLM_MAX_CONTEXT_CHARS
    global RENDERDOC_PYTHON_PATH
    global RENDERDOC_CMP_ROOT
    global RENDERDOC_CMP_SCRIPT

    SETTINGS = dict(settings)
    DEFAULT_HOST = str(settings.get("host") or "127.0.0.1")
    DEFAULT_PORT = int(settings.get("port") or 8010)
    LLM_PROVIDER = str(settings.get("llm_provider") or "local")
    OPENAI_BASE_URL = str(settings.get("openai_base_url") or "").strip()
    OPENAI_API_KEY = str(settings.get("openai_api_key") or "").strip()
    OPENAI_MODEL = str(settings.get("openai_model") or "").strip()
    OPENAI_TIMEOUT_SECONDS = float(settings.get("openai_timeout_seconds") or 60)
    LLM_MAX_CONTEXT_CHARS = int(settings.get("llm_max_context_chars") or 24000)
    RENDERDOC_PYTHON_PATH = str(settings.get("renderdoc_python_path") or "").strip()
    renderdoc_cmp_root = str(settings.get("renderdoc_cmp_root") or "").strip()
    if renderdoc_cmp_root:
        RENDERDOC_CMP_ROOT = Path(renderdoc_cmp_root).expanduser().resolve()
        RENDERDOC_CMP_SCRIPT = RENDERDOC_CMP_ROOT / "rdc_compare_ultimate.py"

    os.environ["RENDERDOC_WEBUI_HOST"] = DEFAULT_HOST
    os.environ["RENDERDOC_WEBUI_PORT"] = str(DEFAULT_PORT)
    os.environ["RENDERDOC_WEBUI_LLM_PROVIDER"] = LLM_PROVIDER
    os.environ["RENDERDOC_WEBUI_OPENAI_BASE_URL"] = OPENAI_BASE_URL
    os.environ["RENDERDOC_WEBUI_OPENAI_API_KEY"] = OPENAI_API_KEY
    os.environ["RENDERDOC_WEBUI_OPENAI_MODEL"] = OPENAI_MODEL
    os.environ["RENDERDOC_WEBUI_OPENAI_TIMEOUT_SECONDS"] = str(OPENAI_TIMEOUT_SECONDS)
    os.environ["RENDERDOC_WEBUI_LLM_MAX_CONTEXT_CHARS"] = str(LLM_MAX_CONTEXT_CHARS)
    os.environ["RENDERDOC_WEBUI_CMP_ROOT"] = str(RENDERDOC_CMP_ROOT)
    if RENDERDOC_PYTHON_PATH:
        os.environ["RENDERDOC_PYTHON_PATH"] = RENDERDOC_PYTHON_PATH


if not SETTINGS_FILE.exists():
    SETTINGS_FILE.write_text(
        json.dumps(
            {
                **DEFAULT_SETTINGS,
                "host": DEFAULT_HOST,
                "port": DEFAULT_PORT,
                "llm_provider": LLM_PROVIDER,
                "openai_base_url": OPENAI_BASE_URL,
                "openai_api_key": OPENAI_API_KEY,
                "openai_model": OPENAI_MODEL,
                "openai_timeout_seconds": OPENAI_TIMEOUT_SECONDS,
                "llm_max_context_chars": LLM_MAX_CONTEXT_CHARS,
                "renderdoc_python_path": RENDERDOC_PYTHON_PATH,
                "renderdoc_cmp_root": str(RENDERDOC_CMP_ROOT),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

apply_runtime_settings(current_settings())
