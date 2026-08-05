from __future__ import annotations

import os
import signal
import socket
import subprocess
import time

from .config import DATA_DIR, PROXY_CONFIG, PROXY_EXE, PROXY_LOG, PROXY_PID, PROXY_START_TIMEOUT, PROXY_HOST, PROXY_PORT
from .models import fetch_upstream_models


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


def _read_pid() -> int | None:
    try:
        return int(PROXY_PID.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def stop_proxy() -> bool:
    """Stop only the instance launched by cx, if its PID is still live."""
    pid = _read_pid()
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    PROXY_PID.unlink(missing_ok=True)
    return True


def ensure_proxy() -> None:
    if proxy_is_ready():
        return
    if _port_is_open():
        fetch_upstream_models(timeout=2.0)
        return
    if not PROXY_EXE.is_file():
        raise RuntimeError(f"CLIProxyAPI executable not found:\n{PROXY_EXE}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    command = [str(PROXY_EXE)] + (["--config", str(PROXY_CONFIG)] if PROXY_CONFIG.is_file() else [])
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
