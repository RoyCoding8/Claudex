"""Shared process probes, startup locks, and log rotation."""
from __future__ import annotations

import errno
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
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


@contextmanager
def deferred_interrupts() -> Iterator[None]:
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


def pid_is_alive(pid: int, timeout: float) -> bool:
    if not 0 < pid <= MAX_PID:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in {5, 87}:
                return error == 5
            raise ctypes.WinError(error)
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            kernel.CloseHandle(handle)
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
    if sys.platform == "darwin":
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                text=True, timeout=max(0.001, timeout), check=False)
        return result.stdout.strip()[:1] != "Z" and result.returncode == 0
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


def try_lock(handle: TextIO, *, blocking: bool = False) -> bool:
    """False only when another holder has it. Every other failure propagates, because
    reading a broken filesystem as a held lock invents a launcher that will never run."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    except OSError as error:
        if error.errno in _CONTENTION:
            return False
        raise
    return True


def lock_file(path: Path, *, blocking: bool = False) -> TextIO | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="ascii")
    try:
        if try_lock(handle, blocking=blocking):
            return handle
    except BaseException:
        handle.close()
        raise
    handle.close()
    return None


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
