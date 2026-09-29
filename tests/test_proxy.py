from __future__ import annotations

import errno
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from modules import models, proxy
from modules.models import Model

_MODELS_SERVER = """
import http.server, socketserver, time

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"object": "list", "data": [{"id": "model-a", "owned_by": "vendor"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

class Server(http.server.HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind resolves the host name between bind() and
        # listen(), and a reverse-DNS lookup that stalls there leaves the socket
        # bound but not listening, so the parent waits on a port nothing serves.
        # Nothing here needs a name, so skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

for attempt in range(100):
    try:
        server = Server(("127.0.0.1", PORT), Handler)
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit(f"could not bind {PORT}")

server.serve_forever()
"""

_SOCKET_HOLDER = """
import socket, time

for attempt in range(100):
    try:
        holder = socket.socket()
        holder.bind(("127.0.0.1", PORT))
        holder.listen(8)
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit(f"could not bind {PORT}")

while True:
    time.sleep(0.05)
"""

_SLEEPER = "import time\nwhile True:\n    time.sleep(0.05)\n"



def _stub_executable(directory: Path, body: str) -> Path:
    path = directory / "cli-proxy-api"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="ascii")
    path.chmod(0o755)
    return path


def _executable_alias(directory: Path, name: str = "cli-proxy-api") -> Path:
    path = directory / name
    path.symlink_to(os.path.realpath(sys.executable))
    return path


def _unvenved() -> dict[str, str]:
    """An environment in which a copy of the interpreter, moved aside, starts.

    VIRTUAL_ENV names a pyvenv.cfg the copy cannot see, and a CPython that finds
    the variable without the file exits before running any code. PYTHONHOME names
    the tree to load the standard library from, which a copy outside that tree
    can no longer find by walking up from where it sits.
    """
    environment = {name: value for name, value in os.environ.items() if name.upper() != "VIRTUAL_ENV"}
    environment["PYTHONHOME"] = sys.base_prefix
    return environment


def _executable_copy(directory: Path, name: str) -> Path:
    """A runnable stand-in for this interpreter under another name.

    Identity is matched on the executable path, so the child must be spawned from
    this file. sys.executable is not the interpreter: inside a virtualenv it is a
    launcher that dispatches to the real one, carrying none of the runtime a copy
    needs. Which launcher it is varies by how the environment was built, and only
    one of them fails when relocated. A uv trampoline embeds the interpreter path
    and starts anywhere, so this test passed on a machine where uv built the
    environment. CPython's own launcher, which uv copies when the base install is
    not uv-managed, instead looks for a pyvenv.cfg beside or above itself and
    exits 106 without running any code. Copy the real interpreter and the DLLs it
    loads from its own directory and neither launcher is involved. A symlink would
    need a privilege this test does not have.
    """
    path = directory / (f"{name}.exe" if os.name == "nt" else name)
    if os.name == "nt":
        home = Path(sys.base_prefix)
        for sibling in home.glob("*.dll"):
            shutil.copy2(sibling, directory / sibling.name)
        shutil.copy2(home / "python.exe", path)
    else:
        path.write_text(
            f'#!{sys.executable}\n'
            'import os, sys\n'
            'os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n',
            encoding="utf-8",
        )
    path.chmod(0o755)
    return path


def _foreign_binary_alias(directory: Path, name: str) -> Path:
    other = shutil.which("dash") or shutil.which("sh")
    if other is None:
        raise AssertionError("no second system binary to build a decoy from")
    path = directory / name
    path.symlink_to(other)
    return path


def _foreign_binary(directory: Path, name: str) -> Path:
    """A decoy that is only ever compared against, never spawned.

    An empty file says as much as a symlink would, and needs no symlink privilege.
    """
    path = directory / name
    path.touch()
    return path


def _became_zombie(pid: int, timeout: float = 5.0) -> bool:
    """Wait for an unreaped child to reach the zombie state.

    Linux reports it in /proc; macOS has no /proc, so ps is the only place the
    state is visible.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sys.platform.startswith("linux"):
            try:
                state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[-1].split(maxsplit=1)[0]
            except (OSError, IndexError):
                pass
            else:
                if state == b"Z":
                    return True
        else:
            result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                    capture_output=True, text=True, check=False)
            if result.stdout.strip()[:1] == "Z":
                return True
        time.sleep(0.02)
    return False


def _signal_mask(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("SigBlk"):
            return line.split()[1]
    raise AssertionError(f"no SigBlk line for process {pid}")


def _unroutable_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _processes_running(path: Path) -> list[str]:
    found: list[str] = []
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
            except OSError:
                continue
            if str(path) in cmdline:
                found.append(f"{entry.name} {cmdline}")
    elif sys.platform == "darwin":
        result = subprocess.run(["ps", "-ax", "-o", "pid=,args="], capture_output=True, text=True, check=False)
        for line in result.stdout.splitlines():
            pid, _, args = line.strip().partition(" ")
            if pid.isdigit() and str(path) in args:
                found.append(f"{pid} {args}")
    return sorted(found)


_CLOCK_SLACK = 0.05


def _wait_for_port(
    port: int,
    timeout: float = 30.0,
    child: subprocess.Popen[bytes] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        if child is not None and child.poll() is not None:
            raise AssertionError(
                f"the child exited with {child.returncode} before port {port} was listening"
                + _child_complaint(child.stderr))
        time.sleep(0.05)
    raise AssertionError(f"port {port} never started listening within {timeout:.0f}s")


def _child_complaint(stderr: Any) -> str:
    """Whatever the child managed to say, which is the only clue to why it died."""
    if stderr is None:
        return ""
    try:
        text = stderr.read()
    except (OSError, ValueError):
        return ""
    return f"\nchild stderr:\n{text.decode(errors='replace')[-2000:]}" if text else ""


class FakeProcess:
    def __init__(self, pid: int = 43210) -> None:
        self.pid = pid
        self.alive = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float | None = None) -> int:
        if self.alive:
            raise subprocess.TimeoutExpired("fake", timeout)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.alive = False


class UnstoppableProcess(FakeProcess):
    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class ProxyLifecycleTests(unittest.TestCase):
    def test_concurrent_ensure_proxy_spawns_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "proxy.log"
            pid = root / "proxy.pid"
            exe = root / "cli-proxy-api"
            exe.touch()
            prechecks = threading.Barrier(2, timeout=1)
            spawned = threading.Event()
            probe_count = 0
            probe_lock = threading.Lock()
            process = FakeProcess()

            def probe(deadline: float | None = None) -> bool:
                nonlocal probe_count
                with probe_lock:
                    probe_count += 1
                    count = probe_count
                if count <= 2:
                    prechecks.wait()
                return spawned.is_set()

            def popen(*args, **kwargs):
                spawned.set()
                return process

            real_startup_lock = proxy._startup_lock

            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", log), \
                 patch.object(proxy, "PROXY_PID", pid), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 2.0), \
                 patch.object(proxy, "proxy_is_ready", side_effect=probe), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(proxy, "_startup_lock", side_effect=real_startup_lock), \
                 patch.object(proxy.subprocess, "Popen", side_effect=popen) as spawn:
                errors: list[BaseException] = []

                def run() -> None:
                    try:
                        proxy.ensure_proxy()
                    except BaseException as error:
                        errors.append(error)

                threads = [threading.Thread(target=run) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(3)
                self.assertFalse(any(thread.is_alive() for thread in threads))

            self.assertEqual(errors, [])
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(pid.read_text(encoding="ascii"), "43210")

    def test_pid_identity_uses_lsof_on_macos(self):
        result = SimpleNamespace(returncode=0, stdout="p123\nn/srv/cli proxy api\n")
        with patch.object(proxy, "_pid_is_alive", return_value=True), \
             patch.object(proxy, "PROXY_EXE", Path("/srv/cli proxy api")), \
             patch.object(proxy.sys, "platform", "darwin"), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("proc used")), \
             patch.object(proxy.subprocess, "run", return_value=result):
            self.assertTrue(proxy._pid_is_proxy(123))

    def test_ps_fallback_handles_paths_with_spaces(self):
        result = SimpleNamespace(returncode=0, stdout="/srv/cli proxy api --config config.yaml\n")
        with patch.object(proxy, "_pid_is_alive", return_value=True), \
             patch.object(proxy, "PROXY_EXE", Path("/srv/cli proxy api")), \
             patch.object(proxy.sys, "platform", "linux"), \
             patch.object(Path, "read_bytes", side_effect=OSError("proc unavailable")), \
             patch.object(proxy.subprocess, "run", return_value=result):
            self.assertTrue(proxy._pid_is_proxy(123))

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "a live pid is identified through /proc"
    )
    def test_live_pid_identity_handles_spaces_and_same_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured_dir = root / "configured dir"
            other_dir = root / "other dir"
            configured_dir.mkdir()
            other_dir.mkdir()
            configured = configured_dir / "python proxy"
            other = other_dir / "python proxy"
            configured.symlink_to(os.path.realpath(sys.executable))
            _executable_copy(other_dir, "python proxy")
            process = subprocess.Popen([str(configured), "-c", "import time; time.sleep(30)"])
            try:
                with patch.object(proxy.sys, "platform", "linux"), \
                     patch.object(proxy, "PROXY_EXE", configured):
                    self.assertTrue(proxy._pid_is_proxy(process.pid))
                    with patch.object(proxy, "PROXY_EXE", other):
                        self.assertFalse(proxy._pid_is_proxy(process.pid))
            finally:
                process.terminate()
                process.wait(timeout=3)

    def test_windows_pid_identity_checks_the_executable_field(self):
        result = SimpleNamespace(
            returncode=0,
            stdout='cli-proxy-api.exe\n"C:\\Program Files\\cli-proxy-api.exe" --config config.yaml\n',
        )
        with patch.object(proxy, "_pid_is_alive", return_value=True), \
             patch.object(proxy, "PROXY_EXE", Path(r"C:\Program Files\cli-proxy-api.exe")), \
             patch.object(proxy.sys, "platform", "win32"), \
             patch.object(proxy.subprocess, "run", return_value=result), \
             patch.object(proxy.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(proxy.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, create=True):
            self.assertTrue(proxy._pid_is_proxy(123))

    def test_identity_helper_timeouts_are_reported_separately(self):
        def timeout_run(result):
            def run(command, **kwargs):
                timeout = kwargs.get("timeout")
                if timeout is None:
                    return result
                raise subprocess.TimeoutExpired(command, timeout)

            return run

        cases = (
            (
                "tasklist",
                "win32",
                nullcontext(),
                SimpleNamespace(returncode=0, stdout='"123"\n'),
            ),
            (
                "PowerShell",
                "win32",
                patch.object(proxy, "_pid_is_alive", return_value=True),
                SimpleNamespace(
                    returncode=0,
                    stdout='cli-proxy-api.exe\n"C:\\cli-proxy-api.exe" --config config.yaml\n',
                ),
            ),
            (
                "ps",
                "freebsd",
                patch.object(proxy, "_pid_is_alive", return_value=True),
                SimpleNamespace(returncode=0, stdout="/srv/cli-proxy-api --config config.yaml\n"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "proxy.log"
            for helper, platform, alive_patch, result in cases:
                with self.subTest(helper=helper), \
                     patch.object(proxy, "PROXY_START_TIMEOUT", 0.01), \
                     patch.object(proxy, "PROXY_LOG", log), \
                     patch.object(proxy, "_read_pid", return_value=123), \
                     patch.object(proxy, "PROXY_EXE", Path("/srv/cli-proxy-api")), \
                     patch.object(proxy.sys, "platform", platform), \
                     alive_patch, \
                     patch.object(proxy.subprocess, "run", side_effect=timeout_run(result)), \
                     patch.object(proxy.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                     patch.object(proxy.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, create=True):
                    with self.assertRaises(RuntimeError) as caught:
                        proxy.proxy_is_ready()
                    self.assertRegex(str(caught.exception), f"{helper} timed out")
                    self.assertIn(str(log), str(caught.exception))

    def test_positive_readiness_accepts_an_empty_model_list(self):
        with patch.object(proxy, "_read_pid", return_value=123), \
             patch.object(proxy, "_pid_is_proxy", return_value=True), \
             patch.object(proxy, "fetch_upstream_models", return_value=[]):
            self.assertTrue(proxy.proxy_is_ready())

    def test_oversized_pid_is_invalid_without_os_error(self):
        with patch.object(proxy.os, "kill", side_effect=OverflowError("pid too large")) as kill:
            self.assertFalse(proxy._pid_is_alive(2**63))
            kill.assert_not_called()

    def test_a_zombie_that_never_exits_is_not_a_live_listener(self):
        if not sys.platform.startswith(("linux", "darwin")):
            self.skipTest("a zombie is observed through /proc on Linux and ps on macOS")
        child = os.fork()
        if child == 0:
            os._exit(0)
        self.addCleanup(os.waitpid, child, 0)
        self.assertTrue(_became_zombie(child), "the unreaped child never became a zombie")
        self.assertFalse(proxy._pid_is_alive(child, time.monotonic() + 2))

    def test_a_lock_failure_is_not_read_as_a_second_launcher(self):
        if sys.platform == "win32":
            self.skipTest("POSIX advisory-lock errno")
        import fcntl

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(proxy, "PROXY_LOG", Path(directory) / "proxy.log"):
            with patch.object(fcntl, "flock", side_effect=OSError(errno.EACCES, "held by another launcher")):
                self.assertIsNone(proxy._startup_lock(), "a contended lock is a launcher to wait for")
            opened = []
            real_open = Path.open

            def watched_open(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                opened.append(handle)
                return handle

            with patch.object(Path, "open", new=watched_open), \
                 patch.object(fcntl, "flock", side_effect=OSError(errno.ENOSPC, "no space")):
                with self.assertRaises(OSError):
                    proxy._startup_lock()
        for handle in opened:
            self.assertTrue(handle.closed, "the lock descriptor outlived the failed acquisition")

    def test_persistent_startup_lock_serializes_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "proxy.log"
            with patch.object(proxy, "PROXY_LOG", log):
                first = proxy._startup_lock()
                self.assertIsNotNone(first)
                self.assertIsNone(proxy._startup_lock())
                first.close()
                second = proxy._startup_lock()
                self.assertIsNotNone(second)
                second.close()
            self.assertTrue(log.with_suffix(".lock").exists())

    def test_verified_listener_waits_for_readiness(self):
        ready_results = iter((False, False, True))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(proxy, "PROXY_EXE", root / "cli-proxy-api"), \
                 patch.object(proxy, "PROXY_PID", root / "proxy.pid"), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 1.0), \
                 patch.object(proxy, "proxy_is_ready", side_effect=lambda deadline: next(ready_results)), \
                 patch.object(proxy, "_port_is_open", return_value=True), \
                 patch.object(proxy, "_read_pid", return_value=123), \
                 patch.object(proxy, "_pid_is_proxy", return_value=True), \
                 patch.object(proxy, "fetch_upstream_models", side_effect=AssertionError("single probe")), \
                 patch.object(proxy.time, "sleep", return_value=None):
                self.assertIsNone(proxy.ensure_proxy())

    def test_base_exception_after_spawn_stops_child_and_clears_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "proxy.log"
            pid = root / "proxy.pid"
            exe = _executable_copy(root, "cli proxy api")
            real_popen = subprocess.Popen
            children: list[subprocess.Popen[bytes]] = []
            ready_calls = 0

            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                self.addCleanup(self._stop_child, child)
                return child

            def ready(deadline: float | None = None) -> bool:
                nonlocal ready_calls
                ready_calls += 1
                if ready_calls == 3:
                    raise KeyboardInterrupt
                return False

            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", log), \
                 patch.object(proxy, "PROXY_PID", pid), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 1.0), \
                 patch.object(proxy, "proxy_is_ready", side_effect=ready), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(proxy.subprocess, "Popen", side_effect=spawn):
                with self.assertRaises(KeyboardInterrupt):
                    proxy.ensure_proxy()

            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(pid.exists())

    def test_base_exception_during_pid_publication_removes_partial_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid = root / "proxy.pid"
            exe = _executable_copy(root, "cli proxy api")
            real_popen = subprocess.Popen
            real_write_text = Path.write_text
            children: list[subprocess.Popen[bytes]] = []

            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                self.addCleanup(self._stop_child, child)
                return child

            def write_text(path, *args, **kwargs):
                if path == pid:
                    path.open("w").close()
                    raise KeyboardInterrupt
                return real_write_text(path, *args, **kwargs)

            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", pid), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 1.0), \
                 patch.object(proxy, "proxy_is_ready", return_value=False), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(Path, "write_text", new=write_text), \
                 patch.object(proxy.subprocess, "Popen", side_effect=spawn):
                with self.assertRaises(KeyboardInterrupt):
                    proxy.ensure_proxy()

            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(pid.exists())

    def test_failed_shutdown_preserves_live_pid_and_reports_ownership_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid = root / "proxy.pid"
            exe = root / "cli-proxy-api"
            exe.touch()
            process = UnstoppableProcess()
            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", pid), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 0.01), \
                 patch.object(proxy, "proxy_is_ready", return_value=False), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(proxy, "_STOP_TIMEOUT", 0.001), \
                 patch.object(proxy.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "could not stop owned CLIProxyAPI process 43210"):
                    proxy.ensure_proxy()

            self.assertEqual(pid.read_text(encoding="ascii"), "43210")

    def test_failed_shutdown_records_pid_when_publication_was_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid = root / "proxy.pid"
            exe = root / "cli-proxy-api"
            exe.touch()

            real_write_text = Path.write_text
            publication_interrupted = False

            def write_text(path, *args, **kwargs):
                nonlocal publication_interrupted
                if path == pid and not publication_interrupted:
                    publication_interrupted = True
                    path.open("w").close()
                    raise KeyboardInterrupt
                return real_write_text(path, *args, **kwargs)

            process = UnstoppableProcess()
            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", pid), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 0.01), \
                 patch.object(proxy, "proxy_is_ready", return_value=False), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(proxy, "_STOP_TIMEOUT", 0.001), \
                 patch.object(Path, "write_text", new=write_text), \
                 patch.object(proxy.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "could not stop owned CLIProxyAPI process 43210"):
                    proxy.ensure_proxy()

            self.assertEqual(pid.read_text(encoding="ascii"), "43210")

    def test_startup_timeout_terminates_and_clears_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "proxy.log"
            pid = root / "proxy.pid"
            exe = root / "cli-proxy-api"
            exe.touch()
            process = FakeProcess()
            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", log), \
                 patch.object(proxy, "PROXY_PID", pid), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 0.01), \
                 patch.object(proxy, "proxy_is_ready", return_value=False), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(proxy.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    proxy.ensure_proxy()

            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertFalse(pid.exists())

    def test_readiness_requires_verified_proxy_process(self):
        with patch.object(proxy, "_read_pid", return_value=123), \
             patch.object(proxy, "_pid_is_proxy", return_value=False), \
             patch.object(proxy, "fetch_upstream_models", return_value=[Model("m", "provider")]):
            self.assertFalse(proxy.proxy_is_ready())

    @unittest.skipUnless(sys.platform.startswith("linux"), "stray children are spotted through /proc")
    def test_sigint_inside_the_popen_window_leaves_no_running_child(self):
        strays: list[str] = []
        for trial in range(16):
            strays.extend(self._popen_window_trial(trial))
        self.assertEqual(strays, [])

    def _popen_window_trial(self, trial: int) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = _stub_executable(root, _SLEEPER)
            entered = threading.Event()
            real_popen = subprocess.Popen

            def spawn(*args, **kwargs):
                entered.set()
                return real_popen(*args, **kwargs)

            def interrupt() -> None:
                entered.wait(10.0)
                time.sleep(trial * 0.00002)
                os.kill(os.getpid(), signal.SIGINT)

            thread = threading.Thread(target=interrupt)
            thread.start()
            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", root / "proxy.pid"), \
                 patch.object(proxy, "PROXY_HOST", "127.0.0.1"), \
                 patch.object(proxy, "PROXY_PORT", _unroutable_port()), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 0.6), \
                 patch.object(proxy.subprocess, "Popen", side_effect=spawn):
                with self.assertRaises(KeyboardInterrupt):
                    proxy.ensure_proxy()
            thread.join(10.0)
            self.assertFalse(thread.is_alive())
            strays = _processes_running(exe)
            self.addCleanup(self._kill_strays, exe)
            return strays

    def _assert_listener_is_refused_and_left_running(self, exe: Path, port: int, pid_file: Path) -> None:
        """A listener we cannot attribute to our own process is reported and the
        port is left as found, so the next launch can still claim it."""
        with patch.object(proxy, "PROXY_EXE", exe), \
             patch.object(proxy, "PROXY_CONFIG", exe.parent / "missing.yaml"), \
             patch.object(proxy, "PROXY_LOG", exe.parent / "proxy.log"), \
             patch.object(proxy, "PROXY_PID", pid_file), \
             patch.object(proxy, "PROXY_HOST", "127.0.0.1"), \
             patch.object(proxy, "PROXY_PORT", port), \
             patch.object(proxy, "PROXY_START_TIMEOUT", 30.0), \
             patch.object(models, "PROXY_HOST", "127.0.0.1"), \
             patch.object(models, "PROXY_PORT", port):
            with self.assertRaisesRegex(RuntimeError, "not a verified CLIProxyAPI"):
                proxy.ensure_proxy()
            with socket.socket() as probe:
                probe.settimeout(1.0)
                self.assertEqual(probe.connect_ex(("127.0.0.1", port)), 0, "the port must be left as found")

    def test_verified_listener_without_a_pid_file_is_refused_not_adopted(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        exe = _executable_copy(root, "cli-proxy-api")
        port = _unroutable_port()
        child = subprocess.Popen(
            [str(exe), "-c", _MODELS_SERVER.replace("PORT", str(port))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=_unvenved(),
        )
        self.addCleanup(self._stop_child, child)
        _wait_for_port(port, child=child)
        pid_file = root / "proxy.pid"
        self._assert_listener_is_refused_and_left_running(exe, port, pid_file)
        self.assertFalse(pid_file.exists(), "no ownership record may be published for an unstarted process")
        self.assertIsNone(child.poll(), "a process we cannot attribute must be left running")

    @unittest.skipUnless(sys.platform.startswith("linux"), "liveness is probed through /proc")
    def test_pid_record_survives_a_probe_this_user_may_not_run(self):
        if shutil.which("sudo") is None:
            self.skipTest("needs sudo to create a process owned by another user")
        probe = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=15)
        if probe.returncode != 0:
            self.skipTest("sudo on this box is not passwordless")
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reported = root / "victim.pid"
        staging = root / "victim.pid.staging"
        release = root / "release"
        code = (
            "import os, pathlib, time\n"
            f"pathlib.Path({str(staging)!r}).write_text(str(os.getpid()))\n"
            f"os.replace({str(staging)!r}, {str(reported)!r})\n"
            f"while not pathlib.Path({str(release)!r}).exists():\n"
            "    time.sleep(0.05)\n"
        )
        victim = subprocess.Popen(
            ["sudo", "-n", sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._release_sudo_child, victim, release)
        for _ in range(200):
            if reported.exists():
                break
            time.sleep(0.05)
        self.assertTrue(reported.exists(), "the root-owned victim never reported its pid")
        pid = int(reported.read_text(encoding="ascii"))
        self.assertEqual(os.stat(f"/proc/{pid}").st_uid, 0)
        self.assertTrue(proxy._pid_is_alive(pid, time.monotonic() + 2))
        pid_file = root / "proxy.pid"
        pid_file.write_text(str(pid), encoding="ascii")
        with patch.object(proxy, "PROXY_PID", pid_file), \
             patch.object(proxy, "PROXY_EXE", root / "cli-proxy-api"):
            self.assertFalse(proxy.proxy_is_ready(time.monotonic() + 2))
        self.assertEqual(pid_file.read_text(encoding="ascii"), str(pid))

    def test_missing_identity_helper_is_named_instead_of_claiming_a_foreign_process(self):
        for helper, platform in (("ps", "freebsd"), ("lsof", "darwin")):
            with self.subTest(helper=helper), \
                 patch.object(proxy, "_pid_is_alive", return_value=True), \
                 patch.object(proxy, "PROXY_EXE", Path("/srv/cli-proxy-api")), \
                 patch.object(proxy.sys, "platform", platform), \
                 patch.object(Path, "read_bytes", side_effect=OSError("proc unavailable")), \
                 patch.object(proxy.subprocess, "run", side_effect=FileNotFoundError(2, "No such file")):
                with self.assertRaisesRegex(RuntimeError, helper):
                    proxy._pid_is_proxy(123, time.monotonic() + 2)

    def test_tasklist_failure_is_named_instead_of_read_as_dead(self):
        with patch.object(proxy.sys, "platform", "win32"), \
             patch.object(proxy.subprocess, "run", return_value=SimpleNamespace(returncode=128, stdout="")), \
             patch.object(proxy.subprocess, "CREATE_NO_WINDOW", 0, create=True):
            with self.assertRaisesRegex(RuntimeError, "tasklist exited with status 128"):
                proxy._pid_is_alive(123, time.monotonic() + 2)

    @unittest.skipUnless(not sys.platform.startswith("win"), "a self-signal is fatal only off Windows")
    def test_an_ignored_interrupt_is_not_turned_into_an_abort(self):
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                with proxy._deferred_interrupts():
                    os.kill(os.getpid(), signal.SIGINT)
            except BaseException as error:
                self.fail(f"an ignored interrupt must stay ignored, got {type(error).__name__}")
        finally:
            signal.signal(signal.SIGINT, previous)

    @unittest.skipUnless(sys.platform.startswith("linux"), "identity is read from /proc")
    def test_executable_identity_resolves_both_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = _executable_alias(root, "cli-proxy-api")
            link = root / "cliproxy-current"
            link.symlink_to(configured)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            other = _foreign_binary_alias(elsewhere, "cli-proxy-api")
            child = subprocess.Popen(
                [str(link), "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(self._stop_child, child)
            with patch.object(proxy.sys, "platform", "linux"), \
                 patch.object(proxy, "PROXY_EXE", configured):
                self.assertTrue(proxy._pid_is_proxy(child.pid))
            with patch.object(proxy.sys, "platform", "linux"), \
                 patch.object(proxy, "PROXY_EXE", other):
                self.assertFalse(proxy._pid_is_proxy(child.pid))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the listener is found through /proc")
    def test_verified_listener_refuses_a_foreign_process_on_the_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = _foreign_binary_alias(root, "cli-proxy-api")
            port = _unroutable_port()
            intruder = subprocess.Popen(
                [sys.executable, "-c", _SOCKET_HOLDER.replace("PORT", str(port))],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(self._stop_child, intruder)
            _wait_for_port(port, child=intruder)
            self._assert_listener_is_refused_and_left_running(exe, port, root / "proxy.pid")
            self.assertIsNone(intruder.poll())

    def test_a_record_naming_a_foreign_process_is_not_treated_as_ours(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = _foreign_binary(root, "cli-proxy-api")
            port = _unroutable_port()
            intruder = subprocess.Popen(
                [sys.executable, "-c", _SOCKET_HOLDER.replace("PORT", str(port))],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(self._stop_child, intruder)
            _wait_for_port(port, child=intruder)
            pid_file = root / "proxy.pid"
            pid_file.write_text(str(intruder.pid), encoding="ascii")
            self._assert_listener_is_refused_and_left_running(exe, port, pid_file)
            self.assertIsNone(intruder.poll())

    @unittest.skipUnless(not sys.platform.startswith("win"), "a self-signal is fatal only off Windows")
    def test_recorded_interrupt_outranks_a_failure_inside_the_spawn_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = _stub_executable(root, _SLEEPER)

            def failing_spawn(*args, **kwargs):
                os.kill(os.getpid(), signal.SIGINT)
                raise PermissionError(13, "Permission denied")

            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", root / "proxy.pid"), \
                 patch.object(proxy, "PROXY_HOST", "127.0.0.1"), \
                 patch.object(proxy, "PROXY_PORT", _unroutable_port()), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 0.6), \
                 patch.object(proxy.subprocess, "Popen", side_effect=failing_spawn):
                with self.assertRaises(KeyboardInterrupt):
                    proxy.ensure_proxy()
            self.assertEqual(_processes_running(exe), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "the child's signal mask is read from /proc")
    def test_spawned_child_keeps_an_unblocked_signal_mask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = _stub_executable(root, _SLEEPER)
            masks: list[str] = []
            real_publish = proxy._publish_pid

            def publish(value: int) -> None:
                masks.append(_signal_mask(value))
                real_publish(value)

            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", root / "proxy.pid"), \
                 patch.object(proxy, "PROXY_HOST", "127.0.0.1"), \
                 patch.object(proxy, "PROXY_PORT", _unroutable_port()), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 0.05), \
                 patch.object(proxy, "_publish_pid", side_effect=publish), \
                 patch.object(proxy.subprocess, "Popen", side_effect=subprocess.Popen):
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    proxy.ensure_proxy()
            self.assertEqual(masks, ["0000000000000000"])

    @unittest.skipUnless(not sys.platform.startswith("win"), "a self-signal is fatal only off Windows")
    def test_hangup_in_the_spawn_window_publishes_the_record_before_dying(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        exe = _stub_executable(root, _SLEEPER)
        (root / "launcher.py").write_text(
            "import os, pathlib, signal, subprocess, sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
            "from unittest.mock import patch\n"
            "from modules import proxy\n"
            f"exe = pathlib.Path({str(exe)!r})\n"
            "folder = exe.parent\n"
            "real = subprocess.Popen\n"
            "\n"
            "def spawn(*args, **kwargs):\n"
            "    child = real(*args, **kwargs)\n"
            "    os.kill(os.getpid(), signal.SIGHUP)\n"
            "    return child\n"
            "\n"
            "with patch.object(proxy, 'PROXY_EXE', exe), \\\n"
            "     patch.object(proxy, 'PROXY_CONFIG', folder / 'missing.yaml'), \\\n"
            "     patch.object(proxy, 'PROXY_LOG', folder / 'proxy.log'), \\\n"
            "     patch.object(proxy, 'PROXY_PID', folder / 'proxy.pid'), \\\n"
            "     patch.object(proxy, 'PROXY_HOST', '127.0.0.1'), \\\n"
            "     patch.object(proxy, 'PROXY_PORT', 1), \\\n"
            "     patch.object(proxy, 'PROXY_START_TIMEOUT', 0.6), \\\n"
            "     patch.object(proxy.subprocess, 'Popen', side_effect=spawn):\n"
            "    proxy.ensure_proxy()\n",
            encoding="ascii",
        )
        result = subprocess.run(
            [sys.executable, str(root / "launcher.py")],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.addCleanup(self._kill_strays, exe)
        self.assertEqual(result.returncode, -signal.SIGHUP, result.stderr)
        pid_file = root / "proxy.pid"
        self.assertTrue(pid_file.exists(), "the ownership record must be published before the hangup lands")
        recorded = int(pid_file.read_text(encoding="ascii"))
        self.assertTrue(
            any(entry.split(" ", 1)[0] == str(recorded) for entry in _processes_running(exe)),
            "the recorded child must still be running, so the next launch can reclaim it",
        )

    def test_unexpected_liveness_error_keeps_the_record_instead_of_being_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "proxy.pid"
            pid_file.write_text("4242", encoding="ascii")
            with patch.object(proxy, "PROXY_PID", pid_file), \
                 patch.object(proxy, "PROXY_EXE", root / "cli-proxy-api"), \
                 patch.object(proxy, "_pid_is_proxy", return_value=False), \
                 patch.object(proxy, "_pid_is_alive", side_effect=OSError("unexpected errno")):
                with self.assertRaises(OSError):
                    proxy.proxy_is_ready(time.monotonic() + 1)
            self.assertEqual(pid_file.read_text(encoding="ascii"), "4242")

    def test_listener_identity_timeout_still_names_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "proxy.log"
            calls: list[list[str]] = []

            def run(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    return SimpleNamespace(returncode=0, stdout="/srv/cli-proxy-api --config config.yaml\n")
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

            with patch.object(proxy, "PROXY_EXE", Path("/srv/cli-proxy-api")), \
                 patch.object(proxy, "PROXY_LOG", log), \
                 patch.object(proxy, "PROXY_PID", root / "proxy.pid"), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 2.0), \
                 patch.object(proxy, "_read_pid", return_value=123), \
                 patch.object(proxy, "_pid_is_alive", return_value=True), \
                 patch.object(proxy, "_port_is_open", return_value=True), \
                 patch.object(proxy, "fetch_upstream_models", side_effect=RuntimeError("no models yet")), \
                 patch.object(proxy.sys, "platform", "freebsd"), \
                 patch.object(proxy.subprocess, "run", side_effect=run):
                with self.assertRaises(RuntimeError) as caught:
                    proxy.ensure_proxy()
            self.assertIn(str(log), str(caught.exception))

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "escalation needs a child that ignores SIGTERM, and strays are spotted through /proc",
    )
    def test_startup_cleanup_uses_the_remaining_deadline_and_still_escalates(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        armed = root / "armed"
        exe = _stub_executable(
            root,
            "import pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path({str(armed)!r}).write_text('1')\n"
            "while True:\n    time.sleep(0.05)\n",
        )
        timeouts: list[float | None] = []
        real_wait = subprocess.Popen.wait

        def wait(child, timeout=None):
            timeouts.append(timeout)
            return real_wait(child, timeout)

        with patch.object(proxy, "PROXY_EXE", exe), \
             patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
             patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
             patch.object(proxy, "PROXY_PID", root / "proxy.pid"), \
             patch.object(proxy, "PROXY_PORT", _unroutable_port()), \
             patch.object(proxy, "PROXY_START_TIMEOUT", 0.01), \
             patch.object(proxy, "_port_is_open", return_value=False), \
             patch.object(proxy, "proxy_is_ready", return_value=False), \
             patch.object(proxy.subprocess.Popen, "wait", wait):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                proxy.ensure_proxy()
        self.assertGreaterEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0], 0.0, "an expired budget must not wait before escalating")
        self.assertEqual(_processes_running(exe), [])

    def test_secondary_launcher_budget_covers_the_probes_before_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "cli-proxy-api"
            exe.touch()
            pid_file = root / "proxy.pid"
            pid_file.write_text("4321", encoding="ascii")
            handed: list[float | None] = []

            def probe(deadline: float | None = None) -> bool:
                handed.append(deadline)
                if len(handed) == 1:
                    time.sleep(0.6)
                return False

            with patch.object(proxy, "PROXY_EXE", exe), \
                 patch.object(proxy, "PROXY_CONFIG", root / "missing.yaml"), \
                 patch.object(proxy, "PROXY_LOG", root / "proxy.log"), \
                 patch.object(proxy, "PROXY_PID", pid_file), \
                 patch.object(proxy, "PROXY_PORT", _unroutable_port()), \
                 patch.object(proxy, "PROXY_START_TIMEOUT", 30.0), \
                 patch.object(proxy, "_port_is_open", return_value=False), \
                 patch.object(proxy, "proxy_is_ready", side_effect=probe):
                other_launcher = proxy._startup_lock()
                try:
                    self.assertIsNotNone(other_launcher)
                    with self.assertRaisesRegex(RuntimeError, "Another launcher"):
                        proxy.ensure_proxy()
                    self.assertGreaterEqual(len(handed), 2)
                finally:
                    other_launcher.close()
                self.assertEqual(len(set(handed)), 1, "every probe must share one launch deadline")

    def test_the_readiness_probe_spends_only_the_budget_it_was_given(self):
        for label, budget, probe_allowed in (
            ("an expired budget issues no request", -1.0, False),
            ("a live budget is capped by what is left", 0.2, True),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                pid_file = Path(directory) / "proxy.pid"
                pid_file.write_text("4321", encoding="ascii")
                issued: list[float] = []

                def fetch(timeout: float = 5.0, _issued=issued) -> list[Model]:
                    _issued.append(timeout)
                    raise RuntimeError("no models")

                with patch.object(proxy, "PROXY_PID", pid_file), \
                     patch.object(proxy, "PROXY_START_TIMEOUT", 2.0), \
                     patch.object(proxy, "_pid_is_proxy", return_value=True), \
                     patch.object(proxy, "fetch_upstream_models", side_effect=fetch):
                    self.assertFalse(proxy.proxy_is_ready(time.monotonic() + budget))

                if not probe_allowed:
                    self.assertEqual(issued, [])
                else:
                    self.assertEqual(len(issued), 1)
                    self.assertGreater(issued[0], 0.0)
                    self.assertLessEqual(issued[0], budget + _CLOCK_SLACK)

    def _release_sudo_child(self, victim: subprocess.Popen[bytes], release: Path) -> None:
        release.touch()
        try:
            victim.wait(timeout=15)
        except subprocess.TimeoutExpired:
            victim.kill()
            victim.wait(timeout=5)

    def _kill_strays(self, path: Path) -> None:
        for stray in _processes_running(path):
            with suppress(OSError):
                os.kill(int(stray.split(" ", 1)[0]), signal.SIGKILL)

    def _stop_child(self, child: subprocess.Popen[bytes]) -> None:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


class ProxyChildEnvironmentTests(unittest.TestCase):
    @staticmethod
    def _executable(directory):
        path = Path(directory) / "cli-proxy-api"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        path.chmod(0o755)
        return path

    def test_the_proxy_child_inherits_no_cx_variables(self) -> None:
        captured = {}

        class _FakePopen:
            pid = 5150

            def __init__(self, command, **kwargs):
                captured.update(kwargs)

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"CX_CLIPROXY_API_KEY": "sk-proxy-secret",
                                     "CX_CLIPROXY_PORT": "8317",
                                     "PATH": "/usr/bin"}, clear=True), \
             patch.object(proxy, "PROXY_PID", Path(directory) / "proxy.pid"), \
             patch.object(proxy, "PROXY_LOG", Path(directory) / "proxy.log"), \
             patch.object(proxy, "PROXY_EXE", self._executable(directory)), \
             patch.object(proxy, "_port_is_open", return_value=False), \
             patch.object(proxy, "_read_pid", return_value=None), \
             patch.object(proxy, "_listener_pid", return_value=None), \
             patch.object(proxy, "_startup_lock", return_value=MagicMock()), \
             patch.object(proxy, "_publish_pid"), \
             patch.object(proxy.subprocess, "Popen", _FakePopen), \
             patch.object(proxy, "proxy_is_ready", side_effect=[False, False, True]):
            proxy.ensure_proxy()

        environment = captured["env"]
        self.assertTrue(all(not name.upper().startswith("CX_") for name in environment))
        self.assertNotIn("sk-proxy-secret", " ".join(environment.values()))
        self.assertEqual(environment["PATH"], "/usr/bin")


if __name__ == "__main__":
    unittest.main()
