from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SETTINGS_FILE, SETTINGS_EXAMPLE_FILE = DATA_DIR / "settings.json", DATA_DIR / "settings.example.json"
POOLS_FILE, POOLS_EXAMPLE_FILE = DATA_DIR / "pools.json", DATA_DIR / "pools.example.json"
ENV_FILE = ROOT / ".env"
if ENV_FILE.is_file():
    load_dotenv(ENV_FILE, override=False)


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


def _env(name: str, *aliases: str, default: str) -> str:
    for candidate in (name, *aliases):
        if value := os.environ.get(candidate, "").strip():
            return value
    return default


def _env_int(name: str, *aliases: str, default: int) -> int:
    value = _env(name, *aliases, default="")
    if not value:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer, got {value!r}.") from error


def _env_float(name: str, *aliases: str, default: float, minimum: float = 0.0,
               maximum: float | None = None) -> float:
    raw = _env(name, *aliases, default="")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number, got {raw!r}.") from error
    if value < minimum or (maximum is not None and value > maximum):
        max_text = f" and at most {maximum:g}" if maximum is not None else ""
        raise RuntimeError(f"{name} must be at least {minimum:g}{max_text}, got {raw!r}.")
    return value


PROXY_EXE = _env_path("CX_CLIPROXY_EXE", Path("cli-proxy-api.exe"))
PROXY_CONFIG = _env_path("CX_CLIPROXY_CONFIG", Path("config.yaml"))
PROXY_LOG, PROXY_PID = DATA_DIR / "cli-proxy-api.log", DATA_DIR / "cli-proxy-api.pid"
PROXY_HOST, PROXY_PORT = _env("CX_CLIPROXY_HOST", default="127.0.0.1"), _env_int("CX_CLIPROXY_PORT", default=8317)
PROXY_API_KEY = _env("CX_CLIPROXY_API_KEY", default="sk-dummy")
PROXY_START_TIMEOUT = _env_float("CX_CLIPROXY_START_TIMEOUT", default=15.0, minimum=0.1)

ROUTER_HOST = _env("CX_ROUTER_HOST", "CX_LITELLM_HOST", default="127.0.0.1")
ROUTER_PORT = _env_int("CX_ROUTER_PORT", "CX_LITELLM_PORT", default=4000)
ROUTER_API_KEY = _env("CX_ROUTER_API_KEY", "CX_LITELLM_API_KEY", default="sk-cx-local")
ROUTER_LOG, ROUTER_PID = DATA_DIR / "router.log", DATA_DIR / "router.pid"
# This must cover the router's upstream readiness wait as well as HTTP startup.
ROUTER_START_TIMEOUT = _env_float("CX_ROUTER_START_TIMEOUT", default=35.0, minimum=0.1)
ROUTER_COOLDOWN_429 = _env_float("CX_ROUTER_COOLDOWN_429", default=60.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_5XX = _env_float("CX_ROUTER_COOLDOWN_5XX", default=30.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_NETWORK = _env_float("CX_ROUTER_COOLDOWN_NETWORK", default=10.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_AUTH = _env_float("CX_ROUTER_COOLDOWN_AUTH", default=300.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_PACED_429 = _env_float("CX_ROUTER_COOLDOWN_PACED_429", default=10.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_EMPTY = _env_float("CX_ROUTER_COOLDOWN_EMPTY", default=20.0, minimum=1.0, maximum=1800.0)
# Whole-request budget for a pooled call: every failover attempt shares it.
ROUTER_POOL_TIMEOUT = _env_float("CX_ROUTER_POOL_TIMEOUT", default=180.0, minimum=1.0)

DEFAULT_GPT_FAST_MODEL = _env("CX_GPT_FAST_MODEL", default="")
DEFAULT_GPT_MEDIUM_MODEL = _env("CX_GPT_MEDIUM_MODEL", default="")
DEFAULT_GPT_SUBAGENT_MODEL = _env("CX_GPT_SUBAGENT_MODEL", default="")
DEFAULT_COMPACT_WINDOW = _env_int("CX_DEFAULT_COMPACT_WINDOW", default=170000)
