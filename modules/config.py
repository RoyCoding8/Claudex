from __future__ import annotations

import ipaddress
import math
import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar

from dotenv import dotenv_values

from .urls import is_local_host

_T = TypeVar("_T", int, float)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class ConfigSource(Enum):
    DEFAULT = "default"
    PROCESS = "process"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class _EnvSetting:
    name: str
    value: str
    source: ConfigSource


class ConfigError(str):
    source: ConfigSource

    def __new__(cls, message: str, source: ConfigSource) -> ConfigError:
        instance = str.__new__(cls, message)
        instance.source = source
        return instance

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SETTINGS_FILE, SETTINGS_EXAMPLE_FILE = DATA_DIR / "settings.json", DATA_DIR / "settings.example.json"
POOLS_FILE, POOLS_EXAMPLE_FILE = DATA_DIR / "pools.json", DATA_DIR / "pools.example.json"
ENV_FILE = ROOT / ".env"
_FILE_VALUES = {key: value for key, value in dotenv_values(ENV_FILE).items() if value is not None} if ENV_FILE.is_file() else {}
_PROCESS_ENVIRONMENT = set(os.environ)


def _env_path(name: str, default: Path) -> Path:
    setting = _selected_env(name)
    value = setting.value.strip() if setting else ""
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _selected_env(name: str, *aliases: str) -> _EnvSetting | None:
    file_setting: _EnvSetting | None = None
    for candidate in (name, *aliases):
        if candidate in _PROCESS_ENVIRONMENT:
            return _EnvSetting(candidate, os.environ.get(candidate, ""), ConfigSource.PROCESS)
        if file_setting is None and candidate in _FILE_VALUES and _FILE_VALUES[candidate] != "":
            file_setting = _EnvSetting(candidate, _FILE_VALUES[candidate], ConfigSource.FILE)
    return file_setting


def _source_label(source: ConfigSource) -> str:
    if source is ConfigSource.PROCESS:
        return "process environment"
    if source is ConfigSource.FILE:
        return f"file {ENV_FILE}"
    return "default"


def _env(name: str, *aliases: str, default: str) -> str:
    setting = _selected_env(name, *aliases)
    if setting is None:
        return default
    return setting.value.strip() or default


def _env_source(name: str, *aliases: str) -> tuple[ConfigSource, str]:
    setting = _selected_env(name, *aliases)
    source = setting.source if setting else ConfigSource.DEFAULT
    return source, _source_label(source)


def _parse_dns_name(name: str) -> None:
    if not name.isascii():
        raise ValueError(f"{name!r} is not ASCII; write international host names in punycode")
    if len(name) > 253:
        raise ValueError(f"{name!r} is longer than 253 characters")
    for label in name.split("."):
        if _HOST_LABEL.fullmatch(label) is None:
            raise ValueError(f"{label!r} is not a valid DNS label")
    try:
        socket.inet_aton(name)
    except OSError:
        pass
    else:
        raise ValueError(f"{name!r} reads as a numeric address, not a DNS name")


def _parse_configured_host(value: str) -> str:
    if value.startswith("[") or value.endswith("]"):
        if not (value.startswith("[") and value.endswith("]")):
            raise ValueError(f"{value!r} has unbalanced IPv6 brackets")
        literal = value[1:-1]
        if "%" in literal:
            raise ValueError(f"{literal!r} carries an IPv6 zone identifier")
        try:
            address = ipaddress.ip_address(literal)
        except ValueError as error:
            raise ValueError(f"{literal!r} is not an IP address literal") from error
        if not isinstance(address, ipaddress.IPv6Address):
            raise ValueError(f"{literal!r} must be an IPv6 address inside brackets")
        return str(address)
    if "%" in value:
        raise ValueError(f"{value!r} carries an IPv6 zone identifier")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as error:
        if ":" in value:
            raise ValueError(f"{value!r} is not a valid IP address literal") from error
    trailing_dot = value.endswith(".")
    name = value[:-1] if trailing_dot else value
    _parse_dns_name(name)
    return f"{name}." if trailing_dot else name


def _env_host(name: str, *aliases: str, default: str) -> str:
    setting = _selected_env(name, *aliases)
    if setting is None or not setting.value:
        return default
    try:
        return _parse_configured_host(setting.value)
    except ValueError as error:
        CONFIG_ERRORS.append(ConfigError(
            f"{setting.name} from {_source_label(setting.source)} must be a valid IP address or DNS "
            f"hostname, got {setting.value!r}: {error}",
            setting.source,
        ))
    return default


def _env_number(name: str, *aliases: str, cast: Callable[[str], _T], article: str,
                default: _T, minimum: _T | None = None, maximum: _T | None = None) -> _T:
    setting = _selected_env(name, *aliases)
    if setting is None:
        return default
    raw = setting.value.strip()
    if not raw:
        return default
    variable, source = setting.name, setting.source
    source_label = _source_label(source)
    try:
        value = cast(raw)
    except ValueError:
        CONFIG_ERRORS.append(ConfigError(f"{variable} from {source_label} must be {article}, got {raw!r}.", source))
        return default
    if isinstance(value, float) and not math.isfinite(value):
        CONFIG_ERRORS.append(ConfigError(f"{variable} from {source_label} must be finite, got {raw!r}.", source))
        return default
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        limits = " and ".join(filter(None, [
            f"at least {minimum}" if minimum is not None else "",
            f"at most {maximum}" if maximum is not None else "",
        ]))
        CONFIG_ERRORS.append(ConfigError(f"{variable} from {source_label} must be {limits}, got {raw!r}.", source))
        return default
    return value


def _env_int(name: str, *aliases: str, default: int, minimum: int | None = None,
             maximum: int | None = None) -> int:
    return _env_number(name, *aliases, cast=int, article="an integer",
                       default=default, minimum=minimum, maximum=maximum)


def _env_float(name: str, *aliases: str, default: float, minimum: float = 0.0,
               maximum: float | None = None) -> float:
    return _env_number(name, *aliases, cast=float, article="a number", default=default,
                       minimum=minimum, maximum=maximum)


CONFIG_ERRORS: list[ConfigError] = []

_DEFAULT_PROXY_API_KEY = "sk-dummy"
_DEFAULT_ROUTER_API_KEY = "sk-cx-local"

PROXY_EXE = _env_path("CX_CLIPROXY_EXE", ROOT / ("cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api"))
PROXY_CONFIG = _env_path("CX_CLIPROXY_CONFIG", ROOT / "config.yaml")
PROXY_LOG, PROXY_PID = DATA_DIR / "cli-proxy-api.log", DATA_DIR / "cli-proxy-api.pid"
PROXY_HOST, PROXY_PORT = _env_host("CX_CLIPROXY_HOST", default="127.0.0.1"), _env_int("CX_CLIPROXY_PORT", default=8317, minimum=1, maximum=65535)
PROXY_API_KEY = _env("CX_CLIPROXY_API_KEY", default=_DEFAULT_PROXY_API_KEY)
PROXY_START_TIMEOUT = _env_float("CX_CLIPROXY_START_TIMEOUT", default=15.0, minimum=0.1)

ROUTER_IDENTITY = "cx-router/1.1"
ROUTER_HOST = _env_host("CX_ROUTER_HOST", "CX_LITELLM_HOST", default="127.0.0.1")
ROUTER_PORT = _env_int("CX_ROUTER_PORT", "CX_LITELLM_PORT", default=4000, minimum=1, maximum=65535)
ROUTER_API_KEY = _env("CX_ROUTER_API_KEY", "CX_LITELLM_API_KEY", default=_DEFAULT_ROUTER_API_KEY)
ROUTER_LOG, ROUTER_PID = DATA_DIR / "router.log", DATA_DIR / "router.pid"
ROUTER_BOOT_LOG = DATA_DIR / "router.boot.log"
ROUTER_START_TIMEOUT = _env_float("CX_ROUTER_START_TIMEOUT", default=35.0, minimum=0.1)
ROUTER_COOLDOWN_429 = _env_float("CX_ROUTER_COOLDOWN_429", default=60.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_5XX = _env_float("CX_ROUTER_COOLDOWN_5XX", default=30.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_NETWORK = _env_float("CX_ROUTER_COOLDOWN_NETWORK", default=10.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_AUTH = _env_float("CX_ROUTER_COOLDOWN_AUTH", default=300.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_PACED_429 = _env_float("CX_ROUTER_COOLDOWN_PACED_429", default=10.0, minimum=1.0, maximum=1800.0)
ROUTER_COOLDOWN_EMPTY = _env_float("CX_ROUTER_COOLDOWN_EMPTY", default=20.0, minimum=1.0, maximum=1800.0)
ROUTER_POOL_TIMEOUT = _env_float("CX_ROUTER_POOL_TIMEOUT", default=180.0, minimum=1.0, maximum=1800.0)
ROUTER_POOL_PASSES = _env_int("CX_ROUTER_POOL_PASSES", default=2, minimum=1, maximum=10)
ROUTER_DIRECT_ATTEMPTS = _env_int("CX_ROUTER_DIRECT_ATTEMPTS", default=3, minimum=1, maximum=10)

DEFAULT_GPT_FAST_MODEL = _env("CX_GPT_FAST_MODEL", default="")
DEFAULT_GPT_MEDIUM_MODEL = _env("CX_GPT_MEDIUM_MODEL", default="")
DEFAULT_GPT_SUBAGENT_MODEL = _env("CX_GPT_SUBAGENT_MODEL", default="")
DEFAULT_COMPACT_WINDOW = _env_int("CX_DEFAULT_COMPACT_WINDOW", default=170000, minimum=1)

if not is_local_host(PROXY_HOST) and PROXY_API_KEY == _DEFAULT_PROXY_API_KEY:
    source, source_label = _env_source("CX_CLIPROXY_API_KEY")
    CONFIG_ERRORS.append(ConfigError(
        f"PROXY_API_KEY from {source_label} must be changed when PROXY_HOST is not local.",
        source,
    ))

if not is_local_host(ROUTER_HOST) and ROUTER_API_KEY == _DEFAULT_ROUTER_API_KEY:
    source, source_label = _env_source("CX_ROUTER_API_KEY", "CX_LITELLM_API_KEY")
    CONFIG_ERRORS.append(ConfigError(
        f"ROUTER_API_KEY from {source_label} must be changed when ROUTER_HOST is not local.",
        source,
    ))
