"""Lifecycle for the cx router subprocess.

The router (``modules.router``) is spawned once and shared by every Claudex
session. This module owns the health check, spawn, and PID/log bookkeeping.

Kept intentionally small: the previous LiteLLM equivalent (``gateway.py``)
was ~260 lines because it had to generate a YAML config, patch upstream
code, and wait 30s for boot. Our router boots in <100ms and has no config
to write, so this file is short.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException

from .config import (
    ROUTER_API_KEY,
    ROUTER_HOST,
    ROUTER_LOG,
    ROUTER_PID,
    ROUTER_PORT,
    ROUTER_START_TIMEOUT,
)


def _port_is_open() -> bool:
    try:
        with socket.create_connection((ROUTER_HOST, ROUTER_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _health_check(timeout: float = 1.5) -> bool:
    conn = HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=timeout)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        return response.status == 200 and b"ok" in response.read()
    except (OSError, HTTPException):
        return False
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _read_pid() -> int | None:
    try:
        return int(ROUTER_PID.read_text(encoding="ascii").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows uses TerminateProcess. Use tasklist.
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        if result.returncode != 0:
            return False
        output = result.stdout.strip().lower()
        return bool(output) and "no tasks are running" not in output and f'"{pid}"' in output
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_router() -> bool:
    """Stop the router subprocess if one is running under our PID file."""
    pid = _read_pid()
    if pid is None or not _pid_is_alive(pid):
        ROUTER_PID.unlink(missing_ok=True)
        return False

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _port_is_open():
            break
        time.sleep(0.2)

    ROUTER_PID.unlink(missing_ok=True)
    return True


def router_is_ready() -> bool:
    """True iff a health-checking router responds on the configured port."""
    return _port_is_open() and _health_check()


def read_router_pid() -> int | None:
    return _read_pid()


def ensure_router() -> None:
    """Start the router subprocess if it isn't already responding."""
    if router_is_ready():
        return

    if _port_is_open():
        # Something else is bound to our port; refuse to fight over it.
        raise RuntimeError(
            f"Port {ROUTER_PORT} is already in use by another process. Stop "
            "the offending server or set CX_ROUTER_PORT to another port."
        )

    ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)

    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    environment = os.environ.copy()
    # Router reads its own config from these env vars; ensure defaults propagate.
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUNBUFFERED", "1")

    with ROUTER_LOG.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "modules.router"],
            cwd=str(ROUTER_LOG.parent.parent),   # project root, so `modules.` imports resolve
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
        )

    ROUTER_PID.write_text(str(process.pid), encoding="ascii")

    deadline = time.monotonic() + ROUTER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "cx router exited during startup. Check:\n"
                f"{ROUTER_LOG}"
            )
        if router_is_ready():
            return
        time.sleep(0.2)

    raise RuntimeError(
        f"cx router did not become ready within {ROUTER_START_TIMEOUT:.0f}s. Check:\n"
        f"{ROUTER_LOG}"
    )
