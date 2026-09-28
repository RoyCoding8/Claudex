"""Process lifecycle shared by the proxy and the router launcher.

The two launchers ask the operating system the same questions -- is this pid
alive, what does the pid file claim, is the port taken, can I hold the startup
lock, may I run this probe without hanging -- and for five days each answered
them with its own copy of the logic, several of the copies disagreeing. This
module is the one authority for those answers.

What counts as *our* process is not here. The proxy matches an executable path
and the router matches an argv vector plus a working directory; identity is not
the same question, so it stays with the launcher that owns it.
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

MAX_PID = (1 << 32) - 1 if os.name == "nt" else (1 << 31) - 1

_POLL_INTERVAL = 0.2
_CONTENTION = frozenset({errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", errno.EAGAIN)})


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def spawn_flags() -> int:
    if sys.platform != "win32":
        return 0
    return _no_window() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def run_tool(command: list[str], *, pid: int, timeout: float, label: str) -> subprocess.CompletedProcess[str]:
    """Run an identity probe, or raise. A probe that cannot answer never answers "no"."""
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_no_window(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{label} timed out while checking whether pid {pid} is running") from error
    except OSError as error:
        raise RuntimeError(f"{label} could not be run to check whether pid {pid} is running ({error})") from error


def pid_is_alive(pid: int, timeout: float) -> bool:
    if not 0 < pid <= MAX_PID:
        return False
    if sys.platform == "win32":
        result = run_tool(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            pid=pid,
            timeout=timeout,
            label="tasklist",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"tasklist exited with status {result.returncode} while checking whether pid {pid} is running"
            )
        return f'"{pid}"' in result.stdout and "no tasks are running" not in result.stdout.lower()
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[-1].split(maxsplit=1)[0]
        except (OSError, IndexError):
            pass
        else:
            if state == b"Z":
                return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if 0 < pid <= MAX_PID else None


def clear_pid(path: Path, pid: int) -> bool:
    """Drop the claim for pid, or one no run can interpret. False if it survived."""
    recorded = read_pid(path)
    if recorded == pid or (recorded is None and path.exists()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
    return True


def try_lock(handle: TextIO) -> bool:
    """False only when another holder has it. Every other failure propagates, because
    reading a broken filesystem as a held lock invents a launcher that will never run."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as error:
        if error.errno in _CONTENTION:
            return False
        raise
    return True


def wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        if predicate():
            return True
        time.sleep(min(_POLL_INTERVAL, remaining))
    return False


def rotate_spawn_log(path: Path, limit: int) -> None:
    try:
        if path.exists() and path.stat().st_size > limit:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass
