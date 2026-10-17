from __future__ import annotations

import os
import subprocess
import time
from typing import TextIO

from . import process
from .config import DATA_DIR, PROXY_CONFIG, PROXY_EXE, PROXY_HOST, PROXY_LOG, PROXY_PID, PROXY_PORT, PROXY_START_TIMEOUT
from .models import fetch_upstream_models

_LOG_ROTATE_BYTES = 5_000_000
_STOP_TIMEOUT = 5.0


def _port_is_open() -> bool:
    return process.port_is_open(PROXY_HOST, PROXY_PORT)


def _read_pid() -> int | None:
    return process.read_pid(PROXY_PID)


def proxy_is_ready(deadline: float | None = None) -> bool:
    remaining = (deadline if deadline is not None else time.monotonic() + PROXY_START_TIMEOUT) - time.monotonic()
    if remaining <= 0:
        return False
    try:
        fetch_upstream_models(timeout=min(1.0, remaining))
        return True
    except RuntimeError:
        return False


def _startup_lock() -> TextIO | None:
    return process.lock_file(PROXY_LOG.with_suffix(".lock"))


def _reuse_listener(deadline: float) -> None:
    if not _wait_for_proxy_readiness(deadline):
        raise RuntimeError(
            f"Port {PROXY_PORT} did not answer the authenticated models endpoint. "
            f"It was left running; check {PROXY_LOG} or set CX_CLIPROXY_PORT."
        )


def _publish_pid(pid: int) -> None:
    PROXY_PID.write_text(str(pid), encoding="ascii")


def _clear_pid(pid: int) -> None:
    process.clear_pid(PROXY_PID, pid)


def _stop_process(child: subprocess.Popen[bytes], deadline: float) -> None:
    if child.poll() is not None:
        return
    grace = min(_STOP_TIMEOUT, max(0.0, deadline - time.monotonic()))
    try:
        child.terminate()
        child.wait(timeout=grace)
    except (OSError, subprocess.TimeoutExpired):
        try:
            child.kill()
            child.wait(timeout=_STOP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            if child.poll() is None:
                raise RuntimeError(
                    f"could not stop owned CLIProxyAPI process {child.pid}; PID ownership was preserved"
                ) from error


def _wait_for_proxy_readiness(deadline: float) -> bool:
    return process.wait_until(lambda: proxy_is_ready(deadline), max(0.0, deadline - time.monotonic()))


def ensure_proxy() -> None:
    deadline = time.monotonic() + PROXY_START_TIMEOUT
    if proxy_is_ready(deadline):
        return
    if _port_is_open():
        _reuse_listener(deadline)
        return
    if not PROXY_EXE.is_file():
        raise RuntimeError(f"CLIProxyAPI executable not found:\n{PROXY_EXE}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = _startup_lock()
    if lock is None:
        if not _wait_for_proxy_readiness(deadline):
            raise RuntimeError(f"Another launcher is starting CLIProxyAPI and it did not become ready. Check:\n{PROXY_LOG}")
        return
    child: subprocess.Popen[bytes] | None = None
    try:
        if proxy_is_ready(deadline):
            return
        if _port_is_open():
            _reuse_listener(deadline)
            return
        flags = process.spawn_flags()
        command = [str(PROXY_EXE)] + (["--config", str(PROXY_CONFIG)] if PROXY_CONFIG.is_file() else [])
        process.rotate_spawn_log(PROXY_LOG, _LOG_ROTATE_BYTES)
        environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("CX_")}
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        with PROXY_LOG.open("ab") as log, process.deferred_interrupts():
            child = subprocess.Popen(
                command,
                cwd=str(PROXY_EXE.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
            )
            _publish_pid(child.pid)
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"CLIProxyAPI exited during startup. Check:\n{PROXY_LOG}")
            if proxy_is_ready(deadline):
                return
            time.sleep(min(0.4, max(0.0, deadline - time.monotonic())))
        raise RuntimeError(f"CLIProxyAPI did not become ready. Check:\n{PROXY_LOG}")
    except BaseException:
        if child is not None:
            if child.poll() is None:
                try:
                    _stop_process(child, deadline)
                except RuntimeError:
                    if _read_pid() != child.pid:
                        try:
                            _publish_pid(child.pid)
                        except BaseException as error:
                            raise RuntimeError(
                                f"could not stop owned CLIProxyAPI process {child.pid} or preserve its PID"
                            ) from error
                    raise
            _clear_pid(child.pid)
        raise
    finally:
        lock.close()
