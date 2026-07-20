from __future__ import annotations

import os
import socket
import subprocess
import time

from .config import (
    DATA_DIR,
    PROXY_CONFIG,
    PROXY_EXE,
    PROXY_LOG,
    PROXY_START_TIMEOUT,
    PROXY_HOST,
    PROXY_PORT,
)
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


def ensure_proxy() -> None:
    if proxy_is_ready():
        return

    if _port_is_open():
        # Surface the real error, usually a mismatched API key or config file.
        fetch_upstream_models(timeout=2.0)
        return

    if not PROXY_EXE.is_file():
        raise RuntimeError(f"CLIProxyAPI executable not found:\n{PROXY_EXE}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    # Build the command — only pass --config if the config file actually exists.
    command = [str(PROXY_EXE)]
    if PROXY_CONFIG.is_file():
        command.extend(["--config", str(PROXY_CONFIG)])

    with PROXY_LOG.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=str(PROXY_EXE.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
        )

    deadline = time.monotonic() + PROXY_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "CLIProxyAPI exited during startup. Check:\n"
                f"{PROXY_LOG}"
            )
        if proxy_is_ready():
            return
        time.sleep(0.4)

    raise RuntimeError(
        "CLIProxyAPI did not become ready. Check:\n"
        f"{PROXY_LOG}"
    )
