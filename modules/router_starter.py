"""Lifecycle management for the shared cx router subprocess."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException

from .config import ROUTER_HOST, ROUTER_LOG, ROUTER_PID, ROUTER_PORT, ROUTER_START_TIMEOUT


def _port_is_open() -> bool:
    try:
        with socket.create_connection((ROUTER_HOST, ROUTER_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _health_check(timeout: float = 1.5) -> bool:
    connection = HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=timeout)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        return response.status == 200 and b"ok" in response.read()
    except (OSError, HTTPException):
        return False
    finally:
        try:
            connection.close()
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
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        return result.returncode == 0 and f'"{pid}"' in result.stdout and "no tasks are running" not in result.stdout.lower()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_is_router(pid: int) -> bool:
    """Verify identity before a PID-file cleanup can terminate anything."""
    if not _pid_is_alive(pid):
        return False
    if os.name == "nt":
        command = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(pid) + "';"
            "if($p){$p.Name+'`n'+$p.CommandLine}"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        details = result.stdout.lower()
        return result.returncode == 0 and "python" in details and "modules.router" in details
    try:
        command_line = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
    except OSError:
        return False
    return "modules.router" in command_line and ("python" in command_line or "py" in command_line)


def _terminate_router(pid: int) -> bool:
    if not _pid_is_router(pid):
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _listener_pids() -> set[int]:
    if os.name != "nt":
        return set()
    result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    suffix = f":{ROUTER_PORT}"
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0].upper() == "TCP" and fields[1].endswith(suffix) and fields[3].upper() == "LISTENING":
            try:
                pids.add(int(fields[-1]))
            except ValueError:
                pass
    return pids


def _sweep_router_listeners() -> None:
    """Remove only verified stale router listeners, never an unrelated service."""
    for pid in _listener_pids():
        _terminate_router(pid)


def stop_router() -> bool:
    pid = _read_pid()
    if pid is None or not _terminate_router(pid):
        if pid is None or not _pid_is_alive(pid):
            ROUTER_PID.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _port_is_open():
        time.sleep(0.2)
    ROUTER_PID.unlink(missing_ok=True)
    return True


def router_is_ready() -> bool:
    return _health_check()


def read_router_pid() -> int | None:
    return _read_pid()


def ensure_router() -> None:
    if router_is_ready():
        return
    # A 1.5s probe can flake while the service is busy. Re-probe for five
    # seconds before treating an occupied port as foreign or stale.
    if _port_is_open():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _health_check(timeout=1.5):
                return
            time.sleep(0.2)
        _sweep_router_listeners()
        time.sleep(0.2)
        if _port_is_open():
            raise RuntimeError(f"Port {ROUTER_PORT} is already in use by another process. Stop it or set CX_ROUTER_PORT.")

    ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    environment = os.environ.copy()
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUNBUFFERED", "1")
    with ROUTER_LOG.open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "modules.router"], cwd=str(ROUTER_LOG.parent.parent), env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    ROUTER_PID.write_text(str(process.pid), encoding="ascii")
    deadline = time.monotonic() + ROUTER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            ROUTER_PID.unlink(missing_ok=True)
            raise RuntimeError(f"cx router exited during startup. Check:\n{ROUTER_LOG}")
        if router_is_ready():
            return
        time.sleep(0.2)
    raise RuntimeError(f"cx router did not become ready within {ROUTER_START_TIMEOUT:.0f}s. Check:\n{ROUTER_LOG}")
