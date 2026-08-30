"""Lifecycle management for the shared cx router subprocess."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from .config import ROUTER_HOST, ROUTER_LOG, ROUTER_PID, ROUTER_PORT, ROUTER_START_TIMEOUT

_STOP_TIMEOUT = 5.0
_LOG_ROTATE_BYTES = 5_000_000


def _wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _rotate_spawn_log(path) -> None:
    """A child process holds its log open, so rotation must happen before spawn."""
    try:
        if path.exists() and path.stat().st_size > _LOG_ROTATE_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


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
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
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
    if sys.platform == "win32":
        command = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(pid) + "';"
            "if($p){$p.Name+'`n'+$p.CommandLine}"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        details = result.stdout.lower()
        return result.returncode == 0 and "python" in details and "modules.router" in details
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").replace("\x00", " ")
    except OSError:
        return False
    return "modules.router" in command_line


def _terminate_router(pid: int) -> bool:
    if not _pid_is_router(pid):
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _listener_pids() -> set[int]:
    if sys.platform == "win32":
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
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{ROUTER_PORT}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, check=False)
    except OSError:
        return set()
    return {int(field) for field in result.stdout.split() if field.isdigit()}


def _sweep_router_listeners() -> None:
    """Remove only verified stale router listeners, never an unrelated service."""
    for pid in _listener_pids():
        _terminate_router(pid)


def stop_router() -> bool:
    """Kill the PID-file router; if that path fails, sweep verified listeners."""
    pid = _read_pid()
    if pid is None or not _terminate_router(pid):
        ROUTER_PID.unlink(missing_ok=True)
        _sweep_router_listeners()
    _wait_until(lambda: not _port_is_open(), _STOP_TIMEOUT)
    ROUTER_PID.unlink(missing_ok=True)
    return not _port_is_open()


def router_is_ready() -> bool:
    return _health_check()


def read_router_pid() -> int | None:
    return _read_pid()


def _startup_lock():
    """Exclusive-create lock; None means another launcher is already starting the router."""
    lock_path = ROUTER_LOG.with_suffix(".lock")
    ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lock_path.exists() and time.time() - lock_path.stat().st_mtime > _LOCK_STALE_SECONDS:
            lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        handle = lock_path.open("x")
    except FileExistsError:
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


_LOCK_STALE_SECONDS = 60.0


def ensure_router() -> None:
    if router_is_ready():
        return
    if _port_is_open():
        if _wait_until(lambda: _health_check(timeout=1.5), 5.0):
            return
        _sweep_router_listeners()
        time.sleep(0.2)
        if _port_is_open():
            raise RuntimeError(
                f"Port {ROUTER_PORT} is occupied by an unresponsive process — possibly a previous "
                "cx router that is not answering /health. Stop it or set CX_ROUTER_PORT.")

    lock = _startup_lock()
    if lock is None:
        if not _wait_until(router_is_ready, 10.0):
            raise RuntimeError(f"Another cx launcher is starting the router and it did not become ready. Check:\n{ROUTER_LOG}")
        return

    lock_path = ROUTER_LOG.with_suffix(".lock")
    try:
        ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            flags = 0
        environment = os.environ.copy()
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        _rotate_spawn_log(ROUTER_LOG)
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
    finally:
        lock.close()
        lock_path.unlink(missing_ok=True)
