from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import DATA_DIR, PROXY_CONFIG, PROXY_EXE, PROXY_HOST, PROXY_LOG, PROXY_PID, PROXY_PORT, PROXY_START_TIMEOUT
from .models import fetch_upstream_models

_LOG_ROTATE_BYTES = 5_000_000


def _rotate_spawn_log(path: Path) -> None:
    """A child process holds its log open, so rotation must happen before spawn."""
    try:
        if path.exists() and path.stat().st_size > _LOG_ROTATE_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _port_is_open() -> bool:
    try:
        with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def proxy_is_ready() -> bool:
    try:
        fetch_upstream_models(timeout=1.0)
        return True
    except RuntimeError:
        return False


def ensure_proxy() -> None:
    if proxy_is_ready():
        return
    if _port_is_open():
        fetch_upstream_models(timeout=2.0)
        return
    if not PROXY_EXE.is_file():
        raise RuntimeError(f"CLIProxyAPI executable not found:\n{PROXY_EXE}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags = 0
    command = [str(PROXY_EXE)] + (["--config", str(PROXY_CONFIG)] if PROXY_CONFIG.is_file() else [])
    _rotate_spawn_log(PROXY_LOG)
    with PROXY_LOG.open("ab") as log:
        process = subprocess.Popen(command, cwd=str(PROXY_EXE.parent), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    PROXY_PID.write_text(str(process.pid), encoding="ascii")
    deadline = time.monotonic() + PROXY_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            PROXY_PID.unlink(missing_ok=True)
            raise RuntimeError(f"CLIProxyAPI exited during startup. Check:\n{PROXY_LOG}")
        if proxy_is_ready():
            return
        time.sleep(0.4)
    raise RuntimeError(f"CLIProxyAPI did not become ready. Check:\n{PROXY_LOG}")
