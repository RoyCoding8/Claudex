from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from enum import Enum
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import TextIO

from . import process
from .config import (
    ROUTER_BOOT_LOG,
    ROUTER_HOST,
    ROUTER_IDENTITY,
    ROUTER_LOG,
    ROUTER_PID,
    ROUTER_PORT,
    ROUTER_START_TIMEOUT,
)

_STOP_TIMEOUT = 5.0
_LOG_ROTATE_BYTES = 5_000_000
_MAX_HEALTH_BYTES = 16 * 1024
_IDENTITY_TIMEOUT = 5.0
_ROUTER_ROOT = Path(__file__).resolve().parents[1]
_STARTUP_LOCK_GUARD = threading.Lock()
_WINDOWS_ROUTER_COMMAND = re.compile(
    r'^\s*(?:"[^"]*python[^"]*"|[^\s"]*python[^\s"]*)\s+-m\s+modules\.router\s*$',
    re.IGNORECASE,
)


class StopOutcome(Enum):
    STOPPED = "stopped"
    ABSENT = "absent"
    REFUSED = "refused"


def _port_is_open() -> bool:
    return process.port_is_open(ROUTER_HOST, ROUTER_PORT)


def _health_check(timeout: float = 1.5) -> bool:
    connection = HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=timeout)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        if (response.getheader("Server") or "").split(" ", 1)[0] != ROUTER_IDENTITY:
            return False
        body = response.read(_MAX_HEALTH_BYTES + 1)
        return len(body) <= _MAX_HEALTH_BYTES and json.loads(body) == {"status": "ok"}
    except (OSError, HTTPException, ValueError, UnicodeDecodeError):
        return False
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _read_pid() -> int | None:
    return process.read_pid(ROUTER_PID)


def _pid_is_alive(pid: int) -> bool:
    return process.pid_is_alive(pid, _IDENTITY_TIMEOUT)


def _router_argv(argv: list[str]) -> bool:
    return len(argv) == 3 and Path(argv[0]).name.lower().startswith("python") and argv[1:] == ["-m", "modules.router"]


def _macos_process_argv(pid: int, deadline: float) -> list[str] | None:
    result = process.run_tool(
        ["ps", "-p", str(pid), "-o", "command="],
        pid=pid,
        timeout=deadline - time.monotonic(),
        label="ps",
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return shlex.split(result.stdout.strip())
    except ValueError:
        return None


def _macos_process_cwd(pid: int, deadline: float) -> Path | None:
    result = process.run_tool(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        pid=pid,
        timeout=deadline - time.monotonic(),
        label="lsof",
    )
    if result.returncode != 0:
        return None
    paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n") and len(line) > 1]
    if len(paths) != 1:
        return None
    try:
        return Path(paths[0]).resolve()
    except OSError:
        return None


def _pid_is_router_windows(pid: int) -> bool:
    command = (
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(pid) + "';"
        "if($p){$p.Name+'`n'+$p.CommandLine}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) < 2 or not lines[0].lower().startswith("python"):
        return False
    return _WINDOWS_ROUTER_COMMAND.fullmatch(lines[1]) is not None and _router_owner_matches(pid)


def _matches_router_process(command_line: list[str] | None, working_directory: Path | None) -> bool:
    if command_line is None or not _router_argv(command_line):
        return False
    return working_directory == _ROUTER_ROOT


def _pid_is_router_posix(pid: int) -> bool:
    if sys.platform == "darwin":
        deadline = time.monotonic() + _IDENTITY_TIMEOUT
        return _matches_router_process(_macos_process_argv(pid, deadline), _macos_process_cwd(pid, deadline))
    if sys.platform.startswith("linux"):
        try:
            return _matches_router_process(
                [part.decode(errors="replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part],
                Path(f"/proc/{pid}/cwd").resolve(),
            )
        except OSError:
            return False
    return False


def _pid_is_router(pid: int) -> bool:
    if not _pid_is_alive(pid):
        return False
    if sys.platform == "win32":
        return _pid_is_router_windows(pid)
    return _pid_is_router_posix(pid)


def _signal_router(pid: int, *, force: bool) -> bool:
    if sys.platform == "win32":
        if force:
            return False
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return True
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        return False
    return True


def _terminate_router(pid: int) -> bool:
    return _pid_is_router(pid) and _signal_router(pid, force=False)


def _kill_router(pid: int) -> bool:
    return _pid_is_router(pid) and _signal_router(pid, force=True)


def _terminate_process(child: subprocess.Popen[bytes]) -> bool:
    if child.poll() is not None:
        return True
    try:
        child.terminate()
    except OSError:
        pass
    try:
        child.wait(timeout=_STOP_TIMEOUT)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        child.kill()
    except OSError:
        return child.poll() is not None
    try:
        child.wait(timeout=_STOP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return child.poll() is not None
    return True


def _listener_pids() -> set[int]:
    if sys.platform == "win32":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
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
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    return {int(field) for field in result.stdout.split() if field.isdigit()}


def _stop_router_process(pid: int) -> bool:
    """Signal a router pid, escalating, and wait for it to die.

    Returns whether a signal was sent, so a caller that reports a stop never
    claims one it did not perform. Waiting matters: returning on the first
    successful signal left the listener holding the port when the caller checked
    straight afterwards.
    """
    signalled = False
    if _terminate_router(pid):
        signalled = True
        if process.wait_until(lambda: not _pid_is_alive(pid), _STOP_TIMEOUT):
            return signalled
    if _kill_router(pid):
        signalled = True
        process.wait_until(lambda: not _pid_is_alive(pid), _STOP_TIMEOUT)
    return signalled


def _sweep_router_listeners() -> bool:
    # Signalled means a signal went out, never that a listener was seen:
    # stop_router reports what it stopped.
    signalled = False
    for pid in _listener_pids():
        if _stop_router_process(pid):
            signalled = True
    return signalled


def _router_stopped(pid: int | None) -> bool:
    return (pid is None or not _pid_is_alive(pid)) and not _port_is_open()


def _clear_pid(pid: int) -> bool:
    return process.clear_pid(ROUTER_PID, pid)


def _write_atomic(path: Path, text: str) -> None:
    staging = path.with_name(path.name + ".staging")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


def _router_owner_path() -> Path:
    return ROUTER_LOG.with_name(ROUTER_LOG.name + ".owner")


def _startup_failure_hint() -> str:
    if ROUTER_BOOT_LOG.exists():
        return f"Check:\n{ROUTER_BOOT_LOG}"
    return (
        f"This launcher started no router, so nothing wrote a boot log. The startup lock at "
        f"{_startup_lock_path()} is held by a cx process that never reported a ready router."
    )


def _parse_router_owner() -> tuple[int, Path]:
    owner_pid, root = _router_owner_path().read_text(encoding="utf-8").splitlines()
    return int(owner_pid), Path(root).resolve()


def _router_owner_matches(pid: int) -> bool:
    try:
        return _parse_router_owner() == (pid, _ROUTER_ROOT)
    except (OSError, ValueError):
        return False


def _publish_router_claims(pid: int) -> None:
    path = _router_owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, f"{pid}\n{_ROUTER_ROOT}\n")
    try:
        _write_atomic(ROUTER_PID, str(pid))
    except BaseException:
        _clear_router_claims(pid)
        raise


def _clear_router_owner(pid: int | None) -> bool:
    path = _router_owner_path()
    try:
        owner_pid, root = _parse_router_owner()
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return False
    else:
        if root != _ROUTER_ROOT or (pid is not None and owner_pid != pid):
            return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _clear_router_claims(pid: int | None) -> list[Path]:
    stuck: list[Path] = []
    if pid is not None and not _clear_pid(pid):
        stuck.append(ROUTER_PID)
    if not _clear_router_owner(pid):
        stuck.append(_router_owner_path())
    return stuck


def _stop_lock() -> TextIO | None:
    deadline = time.monotonic() + max(_STOP_TIMEOUT, ROUTER_START_TIMEOUT)
    while (remaining := deadline - time.monotonic()) > 0:
        lock = _startup_lock()
        if lock is not None:
            return lock
        time.sleep(min(0.05, remaining))
    return None


def stop_router() -> StopOutcome:
    lock = _stop_lock()
    if lock is None:
        return StopOutcome.REFUSED
    try:
        pid = _read_pid()
        was_running = pid is not None and _pid_is_alive(pid)
        if pid is None:
            was_running = _sweep_router_listeners()
        else:
            _stop_router_process(pid)
            if _pid_is_alive(pid):
                _sweep_router_listeners()
        if not process.wait_until(lambda: _router_stopped(pid), _STOP_TIMEOUT):
            return StopOutcome.REFUSED
        if _clear_router_claims(pid):
            return StopOutcome.REFUSED
        return StopOutcome.STOPPED if was_running else StopOutcome.ABSENT
    finally:
        _release_startup_lock(lock)


def router_is_ready() -> bool:
    pid = _read_pid()
    return pid is not None and _pid_is_router(pid) and _health_check()


def read_router_pid() -> int | None:
    return _read_pid()


def _try_lock_file(handle: TextIO) -> bool:
    return process.try_lock(handle)


def _startup_lock_path() -> Path:
    return ROUTER_LOG.with_suffix(".lock")


def _startup_lock_uncontended() -> TextIO | None:
    lock_path = _startup_lock_path()
    ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("r+")
    except FileNotFoundError:
        try:
            handle = lock_path.open("x")
        except FileExistsError:
            return None
    try:
        locked = _try_lock_file(handle)
        if locked:
            handle.truncate(0)
            handle.flush()
    except BaseException:
        handle.close()
        raise
    if not locked:
        handle.close()
        return None
    return handle


def _release_startup_lock(handle: TextIO) -> None:
    handle.close()


def _startup_lock() -> TextIO | None:
    with _STARTUP_LOCK_GUARD:
        return _startup_lock_uncontended()


def _adopt_running_router() -> bool:
    if not _port_is_open():
        return False
    pid = _read_pid()
    if pid is not None and not _pid_is_alive(pid):
        _clear_router_claims(pid)
        pid = None
    if pid is None:
        listener_pids = _listener_pids()
        if len(listener_pids) == 1:
            candidate = next(iter(listener_pids))
            if _pid_is_router(candidate):
                _publish_router_claims(candidate)
                pid = candidate
    pid_verified = pid is not None and _pid_is_router(pid)
    if pid_verified and process.wait_until(router_is_ready, 5.0):
        return True
    if not pid_verified:
        raise RuntimeError(
            f"Port {ROUTER_PORT} is occupied by an unverified process — stop it or set CX_ROUTER_PORT."
        )
    _sweep_router_listeners()
    time.sleep(0.2)
    if _port_is_open():
        raise RuntimeError(
            f"Port {ROUTER_PORT} is occupied by an unresponsive process — possibly a previous "
            "cx router that is not answering /health. Stop it or set CX_ROUTER_PORT."
        )
    return False


def ensure_router() -> None:
    if router_is_ready():
        return

    lock = _startup_lock()
    if lock is None:
        if process.wait_until(router_is_ready, ROUTER_START_TIMEOUT):
            return
        lock = _startup_lock()
        if lock is None:
            raise RuntimeError(
                f"Another cx launcher is starting the router and it did not become ready. "
                f"{_startup_failure_hint()}"
            )

    child: subprocess.Popen[bytes] | None = None
    ready = False
    claims_published = False
    try:
        if router_is_ready():
            return
        if _adopt_running_router():
            return
        ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        flags = process.spawn_flags()
        environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("CX_")}
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        command = [sys.executable, "-m", "modules.router"]
        # Windows refuses to rename a file that still has an open handle, and
        # rotate_spawn_log swallows that OSError, so rotation must precede the open.
        process.rotate_spawn_log(ROUTER_BOOT_LOG, _LOG_ROTATE_BYTES)
        with ROUTER_BOOT_LOG.open("ab") as stdout:
            child = subprocess.Popen(
                command,
                cwd=str(ROUTER_LOG.parent.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
            )
        _publish_router_claims(child.pid)
        claims_published = True
        deadline = time.monotonic() + ROUTER_START_TIMEOUT
        while time.monotonic() < deadline:
            if child.poll() is not None:
                if _adopt_running_router():
                    ready = True
                    return
                raise RuntimeError(f"cx router exited during startup. {_startup_failure_hint()}")
            if router_is_ready():
                ready = True
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"cx router did not become ready within {ROUTER_START_TIMEOUT:.0f}s. {_startup_failure_hint()}"
        )
    except BaseException as failure:
        if child is not None and not ready:
            if not _terminate_process(child):
                kept = (
                    f" Its claims at {ROUTER_PID} and {_router_owner_path()} are kept."
                    if claims_published
                    else ""
                )
                failure.add_note(
                    f"the cx router process {child.pid} could not be terminated or killed and "
                    f"may still be running.{kept}"
                )
            elif claims_published:
                stuck = _clear_router_claims(child.pid)
                if stuck:
                    failure.add_note("stale router claims left at " + ", ".join(str(path) for path in stuck))
        raise
    finally:
        _release_startup_lock(lock)
