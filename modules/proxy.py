from __future__ import annotations

import ntpath
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
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


def _with_log_pointer(error: RuntimeError) -> RuntimeError:
    return RuntimeError(f"{error}. Check:\n{PROXY_LOG}")


@contextmanager
def _deferred_interrupts() -> Iterator[None]:
    if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        yield
        return
    guarded = (signal.SIGINT, signal.SIGHUP)
    previous = {int(sig): signal.getsignal(sig) for sig in guarded}
    recorded: list[int] = []

    def record(signum: int, frame: FrameType | None) -> None:
        recorded.append(signum)

    for sig in guarded:
        signal.signal(sig, record)
    try:
        yield
    finally:
        for sig in guarded:
            signal.signal(sig, previous[int(sig)])
        if recorded:
            for signum in recorded:
                os.kill(os.getpid(), signum)


def _pid_is_alive(pid: int, deadline: float | None = None) -> bool:
    budget = deadline if deadline is not None else time.monotonic() + PROXY_START_TIMEOUT
    return process.pid_is_alive(pid, budget - time.monotonic())


def _first_invocation(command_line: str) -> str:
    value = command_line.strip()
    if not value:
        return ""
    if value.startswith('"'):
        end = value.find('"', 1)
        return value[1:end] if end > 0 else ""
    return value.split(maxsplit=1)[0]


def _normalized_executable_path(value: str) -> str:
    expanded = os.path.expanduser(value.strip().strip('"'))
    if sys.platform == "win32":
        return ntpath.normcase(ntpath.normpath(ntpath.abspath(expanded)))
    return os.path.normcase(os.path.realpath(expanded))


def _windows_name_matches(process_name: str) -> bool:
    name = ntpath.basename(process_name.strip().strip('"')).casefold()
    configured_name = ntpath.basename(str(PROXY_EXE).strip().strip('"')).casefold()
    return bool(name) and name == configured_name


def _posix_command_matches(command_line: str) -> bool:
    value = command_line.strip()
    configured = _normalized_executable_path(str(PROXY_EXE))
    for index in range(len(value) + 1):
        if index < len(value) and not value[index].isspace():
            continue
        candidate = value[:index].strip().strip('"')
        if candidate and _normalized_executable_path(candidate) == configured:
            return True
    return False


def _invocation_matches(command_line: str) -> bool:
    invocation = _first_invocation(command_line) if sys.platform == "win32" else command_line.strip()
    return bool(invocation) and _normalized_executable_path(invocation) == _normalized_executable_path(str(PROXY_EXE))


def _pid_is_proxy_via_ps(pid: int, deadline: float) -> bool:
    result = process.run_tool(
        ["ps", "-p", str(pid), "-o", "args="],
        pid=pid,
        timeout=deadline - time.monotonic(),
        label="ps",
    )
    return result.returncode == 0 and _posix_command_matches(result.stdout.strip())


def _pid_is_proxy_via_lsof(pid: int, deadline: float) -> bool:
    result = process.run_tool(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
        pid=pid,
        timeout=deadline - time.monotonic(),
        label="lsof",
    )
    if result.returncode != 0:
        return False
    configured = _normalized_executable_path(str(PROXY_EXE))
    return any(
        _normalized_executable_path(line[1:]) == configured
        for line in result.stdout.splitlines()
        if line.startswith("n")
    )


def _pid_is_proxy(pid: int, deadline: float | None = None) -> bool:
    identity_deadline = deadline if deadline is not None else time.monotonic() + PROXY_START_TIMEOUT
    if not _pid_is_alive(pid, identity_deadline):
        return False
    if sys.platform == "win32":
        command = (
            f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={pid}";'
            'if($p){$p.Name+"`n"+$p.CommandLine}'
        )
        result = process.run_tool(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            pid=pid,
            timeout=identity_deadline - time.monotonic(),
            label="PowerShell",
        )
        if result.returncode != 0:
            return False
        fields = result.stdout.splitlines()
        return len(fields) >= 2 and _windows_name_matches(fields[0]) and _invocation_matches(fields[1])
    if sys.platform.startswith("linux"):
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00", 1)[0]
        except OSError:
            command_line = b""
        if command_line:
            return _invocation_matches(command_line.decode(errors="replace"))
    if sys.platform == "darwin":
        return _pid_is_proxy_via_lsof(pid, identity_deadline)
    return _pid_is_proxy_via_ps(pid, identity_deadline)


def _listener_pid(deadline: float) -> int | None:
    pid = _read_pid()
    if pid is None:
        return None
    try:
        return pid if _pid_is_proxy(pid, deadline) else None
    except RuntimeError as error:
        raise _with_log_pointer(error) from error


def _reuse_verified_listener(deadline: float) -> None:
    pid = _listener_pid(deadline)
    if pid is None:
        raise RuntimeError(
            f"Port {PROXY_PORT} is held by a process that is not a verified CLIProxyAPI. "
            f"It was left running; stop it yourself or set CX_CLIPROXY_PORT."
        )
    if _wait_for_proxy_readiness(deadline):
        return
    raise RuntimeError(f"Verified CLIProxyAPI process {pid} is listening but did not become ready. Check:\n{PROXY_LOG}")


def proxy_is_ready(deadline: float | None = None) -> bool:
    readiness_deadline = deadline if deadline is not None else time.monotonic() + PROXY_START_TIMEOUT
    try:
        pid = _read_pid()
        if pid is None or not _pid_is_proxy(pid, readiness_deadline):
            if pid is not None and not _pid_is_alive(pid, readiness_deadline):
                process.clear_pid(PROXY_PID, pid)
            return False
    except RuntimeError as error:
        raise _with_log_pointer(error) from error
    remaining = readiness_deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        fetch_upstream_models(timeout=min(1.0, remaining))
        return True
    except RuntimeError:
        return False


def _startup_lock() -> TextIO | None:
    lock_path = PROXY_LOG.with_suffix(".lock")
    PROXY_LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="ascii")
    try:
        if not process.try_lock(handle):
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.flush()
    except Exception:
        try:
            handle.close()
        except OSError:
            pass
        raise
    return handle


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
        _reuse_verified_listener(deadline)
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
            _reuse_verified_listener(deadline)
            return
        flags = process.spawn_flags()
        command = [str(PROXY_EXE)] + (["--config", str(PROXY_CONFIG)] if PROXY_CONFIG.is_file() else [])
        process.rotate_spawn_log(PROXY_LOG, _LOG_ROTATE_BYTES)
        environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("CX_")}
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        with PROXY_LOG.open("ab") as log, _deferred_interrupts():
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
