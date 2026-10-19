from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import Enum
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from . import process
from .config import (
    ROUTER_API_KEY,
    ROUTER_BOOT_LOG,
    ROUTER_HOST,
    ROUTER_LOG,
    ROUTER_PID,
    ROUTER_PORT,
    ROUTER_START_TIMEOUT,
)
from .process import PortOccupiedError

_STOP_TIMEOUT = 5.0
_MAX_HEALTH_BYTES = 16 * 1024


class StopOutcome(Enum):
    STOPPED = "stopped"
    ABSENT = "absent"
    REFUSED = "refused"


@dataclass(frozen=True)
class RouterIdentity:
    pid: int
    token: str


def _read_identity() -> RouterIdentity | None:
    try:
        with ROUTER_PID.open("rb") as handle:
            raw = handle.read(_MAX_HEALTH_BYTES + 1)
        value = json.loads(raw) if len(raw) <= _MAX_HEALTH_BYTES else None
        if (isinstance(value, dict) and type(value.get("pid")) is int
                and 0 < value["pid"] <= process.MAX_PID
                and isinstance(value.get("token"), str) and 16 <= len(value["token"]) <= 256
                and value["token"].isascii() and value["token"].isprintable()):
            return RouterIdentity(value["pid"], value["token"])
    except (OSError, ValueError, RecursionError):
        pass
    return None


def publish_router_identity(pid: int, token: str) -> RouterIdentity:
    identity = RouterIdentity(pid, token)
    ROUTER_PID.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=ROUTER_PID.parent, delete=False, encoding="ascii") as handle:
        staging = Path(handle.name)
        json.dump(asdict(identity), handle)
    try:
        staging.replace(ROUTER_PID)
    finally:
        staging.unlink(missing_ok=True)
    return identity


def clear_router_identity(identity: RouterIdentity) -> None:
    if _read_identity() == identity:
        ROUTER_PID.unlink(missing_ok=True)


def _health_check(identity: RouterIdentity, timeout: float = 1.0) -> bool:
    connection = HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=timeout)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read(_MAX_HEALTH_BYTES + 1)
        return (response.status == 200 and len(body) <= _MAX_HEALTH_BYTES
                and json.loads(body) == {"status": "ok", **asdict(identity)})
    except (OSError, HTTPException, ValueError, RecursionError):
        return False
    finally:
        connection.close()


def router_is_ready() -> bool:
    identity = _read_identity()
    return identity is not None and _health_check(identity)


def read_router_pid() -> int | None:
    identity = _read_identity()
    return identity.pid if identity is not None else None


def _request_shutdown(identity: RouterIdentity) -> bool:
    connection = HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=_STOP_TIMEOUT)
    try:
        connection.request("POST", "/-/shutdown", body=b"", headers={
            "Authorization": f"Bearer {ROUTER_API_KEY}", "X-CX-Router-Token": identity.token,
        })
        response = connection.getresponse()
        response.read(_MAX_HEALTH_BYTES)
        return response.status == 202
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def stop_router() -> StopOutcome:
    lock = process.lock_file(ROUTER_LOG.with_suffix(".lock"))
    if lock is None:
        return StopOutcome.REFUSED
    try:
        identity = _read_identity()
        if identity is None:
            return StopOutcome.REFUSED if process.port_is_open(ROUTER_HOST, ROUTER_PORT) else StopOutcome.ABSENT
        if not _health_check(identity):
            if process.pid_is_alive(identity.pid, _STOP_TIMEOUT) or process.port_is_open(ROUTER_HOST, ROUTER_PORT):
                return StopOutcome.REFUSED
            clear_router_identity(identity)
            return StopOutcome.ABSENT
        if not _request_shutdown(identity):
            return StopOutcome.REFUSED
        if not process.wait_until(lambda: not process.pid_is_alive(identity.pid, _STOP_TIMEOUT), _STOP_TIMEOUT):
            return StopOutcome.REFUSED
        clear_router_identity(identity)
        return StopOutcome.STOPPED
    finally:
        lock.close()


def _terminate_process(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=process.spawn_flags(), timeout=_STOP_TIMEOUT, check=False)
    else:
        child.terminate()
    try:
        child.wait(timeout=_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=_STOP_TIMEOUT)


def ensure_router() -> None:
    if router_is_ready():
        return
    lock = process.lock_file(ROUTER_LOG.with_suffix(".lock"))
    if lock is None:
        if process.wait_until(router_is_ready, ROUTER_START_TIMEOUT):
            return
        raise RuntimeError("Another launcher is starting the router, but it did not become ready.")
    child: subprocess.Popen[bytes] | None = None
    try:
        if router_is_ready():
            return
        if process.port_is_open(ROUTER_HOST, ROUTER_PORT):
            raise PortOccupiedError(ROUTER_HOST, ROUTER_PORT)
        process.rotate_spawn_log(ROUTER_BOOT_LOG, 5_000_000)
        environment = os.environ.copy()
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        with ROUTER_BOOT_LOG.open("ab") as stdout, process.deferred_interrupts():
            child = subprocess.Popen(
                [sys.executable, "-m", "modules.router"], cwd=str(ROUTER_LOG.parent.parent), env=environment,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
                creationflags=process.spawn_flags(), close_fds=True,
            )
        deadline = time.monotonic() + ROUTER_START_TIMEOUT
        while time.monotonic() < deadline:
            if router_is_ready():
                return
            if child.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError(f"cx router did not become ready. Check:\n{ROUTER_BOOT_LOG}")
    except BaseException:
        if child is not None:
            _terminate_process(child)
        raise
    finally:
        lock.close()
