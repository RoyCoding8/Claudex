from __future__ import annotations

import ast
import errno
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import router_starter as rs
from modules.router import _RouterServer


@contextmanager
def _health_server(body: bytes, identity: str = "foreign/1"):
    class Handler(BaseHTTPRequestHandler):
        requests: list[dict[str, str]] = []

        def do_GET(self):
            type(self).requests.append({key.lower(): value for key, value in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def version_string(self):
            return identity

        def log_message(self, format, *args):
            pass

    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _child_complaint(stderr) -> str:
    """Whatever the child managed to say, which is the only clue to why it failed."""
    if stderr is None:
        return ""
    try:
        text = stderr.read()
    except (OSError, ValueError):
        return ""
    return f"\nchild stderr:\n{text.decode(errors='replace')[-2000:]}" if text else ""


@contextmanager
def _bound_child(ignore_sigterm: bool = False):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with _listening_child(port, ignore_sigterm=ignore_sigterm) as process:
        yield process, port


@contextmanager
def _listening_child(port: int, ignore_sigterm: bool = False):
    child_code = """
import signal
import socket
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
""" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_sigterm else "") + """

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def version_string(self):
        return "cx-router/1.1"

class Bound(HTTPServer):
    # HTTPServer sets allow_reuse_address, and on a BSD socket that lets a second
    # bind succeed on a port another process already holds, so this child would
    # quietly take a port it was only meant to be sharing. allow_reuse_port is
    # the BSD spelling of the same idea and is on by default on macOS.
    allow_reuse_address = False
    if hasattr(socket, "SO_REUSEPORT"):
        allow_reuse_port = False

    def server_bind(self):
        # HTTPServer.server_bind resolves the host name between bind() and
        # listen(), and a reverse-DNS lookup that stalls there leaves the socket
        # bound but not listening: the parent waits, the child is alive, and
        # nothing is written anywhere. Nothing here needs a name, so skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

try:
    Bound(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
except OSError as error:
    sys.stderr.write("bind failed: %r\\n" % (error,))
    raise
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(port)],
        cwd=str(rs._ROUTER_ROOT),
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            if process.poll() is not None:
                raise AssertionError(
                    f"the child listener exited with {process.returncode}"
                    + _child_complaint(process.stderr)) from None
            time.sleep(0.02)
    else:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("child listener did not bind within 30s" + _child_complaint(process.stderr))
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@contextmanager
def _spawned_router_on(port: int):
    real_popen = subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = real_popen([sys.executable, "-c", _ROUTER_CHILD, str(port)], cwd=str(rs._ROUTER_ROOT))
        children.append(child)
        return child

    def pid():
        return children[0].pid if children else None

    try:
        yield spawn, pid
    finally:
        _reap(children)


_ROUTER_CHILD = """
import socket
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def version_string(self):
        return "cx-router/1.1"

class Bound(HTTPServer):
    # HTTPServer sets allow_reuse_address, and on a BSD socket that lets a second
    # bind succeed on a port another process already holds, so this child would
    # quietly take a port it was only meant to be sharing. allow_reuse_port is
    # the BSD spelling of the same idea and is on by default on macOS.
    allow_reuse_address = False
    if hasattr(socket, "SO_REUSEPORT"):
        allow_reuse_port = False

    def server_bind(self):
        # HTTPServer.server_bind resolves the host name between bind() and
        # listen(), and a reverse-DNS lookup that stalls there leaves the socket
        # bound but not listening: the parent waits, the child is alive, and
        # nothing is written anywhere. Nothing here needs a name, so skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

try:
    Bound(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
except OSError as error:
    sys.stderr.write("bind failed: %r\\n" % (error,))
    raise
"""


class _FaultyProcess:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid

    def poll(self):
        return self.process.poll()

    def terminate(self):
        raise OSError("terminate failed")

    def wait(self, timeout):
        raise subprocess.TimeoutExpired("router", timeout)

    def kill(self):
        raise OSError("kill failed")


def _short_write_text(path, data, **kwargs):
    with open(path, "wb") as handle:
        handle.write(data.encode("utf-8")[: len(data) // 2])
    raise OSError(errno.ENOSPC, "No space left on device")


def _refuse_claim_publication():
    real_write = rs._write_atomic

    def write_atomic(path, text):
        if Path(path) == rs._router_owner_path():
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(path, text)

    return write_atomic


def _write_claim(pid: int, root: Path | None = None):
    path = rs._router_owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{root or rs._ROUTER_ROOT}\n", encoding="utf-8")
    return path


def _unvouched_pid_publications():
    real_write = rs._write_atomic
    unvouched = []

    def write_atomic(path, text):
        if Path(path) == rs.ROUTER_PID:
            try:
                vouching = rs._router_owner_path().read_text(encoding="utf-8").splitlines()[0]
            except (OSError, IndexError):
                vouching = None
            if vouching != text.strip():
                unvouched.append(text)
        return real_write(path, text)

    return unvouched, write_atomic


def _reap(processes) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


_STUBBORN_CHILD = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').close()\n"
    "time.sleep(30)\n"
)


@contextmanager
def _stubborn_child(directory: Path):
    ready = Path(directory) / "ready"
    process = subprocess.Popen([sys.executable, "-c", _STUBBORN_CHILD, str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            raise AssertionError("the child never installed its SIGTERM handler")
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@contextmanager
def _idle_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


@contextmanager
def _idle_children(faulty: bool = False):
    real_popen = subprocess.Popen
    spawned = []
    reaped = []

    def spawn(*args: object, **kwargs: object):
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"),
        )
        reaped.append(process)
        spawned.append(process)
        return _FaultyProcess(process) if faulty else process

    try:
        yield spawn, spawned
    finally:
        # _reap takes the real Popen objects; _FaultyProcess raises on kill and wait.
        _reap(reaped)
        _await_release(rs.ROUTER_BOOT_LOG, allow_held=any(p.poll() is None for p in reaped))


def _await_release(path: Path, timeout: float = 5.0, allow_held: bool = False) -> None:
    """Wait until a reaped child no longer holds `path` open.

    wait() returning only means the process has exited; the kernel can take a few
    more milliseconds to drop the descriptors it inherited. Windows refuses to
    unlink a file another process still holds, so a temporary directory holding a
    spawn log has to outlive that delay.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if allow_held:
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f"{path} was still held after {timeout:.0f}s") from None
            time.sleep(0.01)


def _spawned_tempdir() -> tempfile.TemporaryDirectory:
    """A temporary directory that tolerates a child still holding a handle.

    A child spawned with its working directory here keeps a handle on it after it
    is reaped, and wait() returns before the kernel drops that handle, so Windows
    refuses the removal for a few milliseconds afterwards. That is a teardown
    artefact rather than a test outcome, so it is not allowed to fail the test.
    """
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _sandbox_data_paths(case: unittest.TestCase) -> None:
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    for name, filename in (("ROUTER_LOG", "router.log"), ("ROUTER_PID", "router.pid"),
                           ("ROUTER_BOOT_LOG", "router.boot.log")):
        patcher = patch.object(rs, name, Path(directory.name) / filename)
        patcher.start()
        case.addCleanup(patcher.stop)


class HealthContractTests(unittest.TestCase):
    OK = b'{"status":"ok"}'

    def test_only_the_routers_own_exact_health_body_is_ready(self) -> None:
        padded = self.OK + b" " * (rs._MAX_HEALTH_BYTES - len(self.OK))
        cases = (
            ("not_ok_body", b"not ok", "foreign/1", False),
            ("foreign_service", self.OK, "foreign/1", False),
            ("extra_field", b'{"status":"ok","extra":true}', rs.ROUTER_IDENTITY, False),
            ("read_is_bounded", b" " * (16 * 1024) + self.OK, rs.ROUTER_IDENTITY, False),
            ("extra_byte_at_cap", padded + b"x", rs.ROUTER_IDENTITY, False),
            ("exact_cap", padded, rs.ROUTER_IDENTITY, True),
        )
        for label, body, identity, expected in cases:
            with self.subTest(body=label), _health_server(body, identity) as server, \
                 patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
                 patch.object(rs, "ROUTER_PORT", server.server_port):
                self.assertIs(rs._health_check(), expected)

    def test_router_health_is_ready(self):
        router = _RouterServer(("127.0.0.1", 0))
        thread = threading.Thread(target=router.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
                 patch.object(rs, "ROUTER_PORT", router.server_port):
                self.assertTrue(rs._health_check())
        finally:
            router.shutdown()
            router.server_close()
            thread.join(timeout=2)

    def test_foreign_listener_is_rejected_before_auth_is_sent(self):
        with _health_server(b'{"status":"ok"}', "foreign/1") as server, \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", server.server_port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value=set()), \
             patch.object(rs.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                rs.ensure_router()
        self.assertEqual(server.RequestHandlerClass.requests, [])
        popen.assert_not_called()


class PidIdentityTests(unittest.TestCase):
    POWERSHELL = MagicMock(
        returncode=0, stdout='python.exe\n"C:\\Python\\python.exe" -m modules.router')
    # ps on darwin reports a posix argv, so quote sys.executable the way ps quotes it.
    PROCESS_LISTING = MagicMock(
        returncode=0, stdout=f'"{sys.executable}" -m modules.router\n')

    def setUp(self):
        _sandbox_data_paths(self)

    def _macos_probe(self, cwd: str):
        return self.PROCESS_LISTING, MagicMock(
            returncode=0, stdout=f"p123\nfcwd\nn{cwd}\n")

    def _windows_identity(self):
        return (
            patch.object(rs.sys, "platform", "win32"),
            patch.object(rs.subprocess, "CREATE_NO_WINDOW", 0, create=True),
            patch.object(rs, "_pid_is_alive", return_value=True),
            patch.object(rs.subprocess, "run", return_value=self.POWERSHELL),
        )

    def _macos_identity(self, cwd: str):
        return (
            patch.object(rs.sys, "platform", "darwin"),
            patch.object(rs, "_pid_is_alive", return_value=True),
            patch.object(rs.subprocess, "run", side_effect=self._macos_probe(cwd)),
        )

    def test_unverifiable_pid_is_never_killed(self):
        with patch.object(rs, "_pid_is_alive", return_value=False):
            self.assertFalse(rs._pid_is_router(123))

    def test_a_process_we_cannot_signal_is_still_a_live_process(self):
        # win32 answers from tasklist and never signals; only posix reaches os.kill.
        with patch.object(rs.sys, "platform", "linux"), \
             patch("os.getpid", return_value=1234), \
             patch.object(rs.os, "kill", side_effect=PermissionError("owned by another user")) as kill:
            self.assertTrue(rs._pid_is_alive(1234))
        kill.assert_called_once_with(1234, 0)

    def test_an_impossible_pid_is_rejected_without_signalling(self):
        with patch.object(rs.sys, "platform", "linux"), \
             patch.object(rs.os, "kill", side_effect=OverflowError("pid too large")) as kill:
            self.assertFalse(rs._pid_is_alive(2**63))
        kill.assert_not_called()

    def test_module_argument_in_unrelated_python_process_is_not_router(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m", "modules.router"])
        try:
            self.assertFalse(rs._pid_is_router(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_same_argv_process_from_another_checkout_is_not_router(self):
        with _spawned_tempdir() as directory:
            module_dir = Path(directory) / "modules"
            module_dir.mkdir()
            # Without __init__.py, `modules` is a namespace package and python merges
            # every `modules` on sys.path, including a second checkout.
            (module_dir / "__init__.py").write_text("", encoding="utf-8")
            (module_dir / "router.py").write_text("import time; time.sleep(30)", encoding="utf-8")
            process = subprocess.Popen([sys.executable, "-m", "modules.router"], cwd=directory)
            try:
                self.assertFalse(rs._pid_is_router(process.pid))
                self.assertFalse(rs._terminate_router(process.pid))
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_windows_identity_requires_exact_python_module_invocation(self):
        with ExitStack() as stack:
            for guard in self._windows_identity():
                stack.enter_context(guard)
            _write_claim(123)
            self.assertTrue(rs._pid_is_router(123))

    def test_windows_identity_refuses_a_pid_no_live_claim_vouches_for(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        cases = (
            ("no_claim_at_all", None),
            ("claim_from_another_checkout", "123\nC:\\other-checkout\n"),
            ("claim_naming_another_live_pid", f"999\n{rs._ROUTER_ROOT}\n"),
            ("claim_naming_a_dead_pid", f"{exited.pid}\n{rs._ROUTER_ROOT}\n"),
        )
        for label, claim in cases:
            with self.subTest(claim=label), ExitStack() as stack:
                for guard in self._windows_identity():
                    stack.enter_context(guard)
                _write_claim(123)
                if claim is None:
                    rs._router_owner_path().unlink()
                else:
                    rs._router_owner_path().write_text(claim, encoding="utf-8")
                self.assertFalse(rs._pid_is_router(123))

    def test_macos_identity_reads_the_process_working_directory(self):
        cases = (("this_checkout", str(rs._ROUTER_ROOT), True),
                 ("a_different_checkout", "/other-checkout", False))
        for label, cwd, expected in cases:
            with self.subTest(cwd=label), ExitStack() as stack:
                for guard in self._macos_identity(cwd):
                    stack.enter_context(guard)
                self.assertIs(rs._pid_is_router(123), expected)

    def test_macos_identity_ignores_a_live_rival_owner_claim(self):
        with ExitStack() as stack:
            for guard in self._macos_identity(str(rs._ROUTER_ROOT)):
                stack.enter_context(guard)
            _write_claim(999)
            self.assertTrue(rs._pid_is_router(123))

    def test_macos_identity_names_the_probe_it_could_not_run(self):
        cases = (("ps", [FileNotFoundError(2, "No such file or directory", "ps")]),
                 ("lsof", [self.PROCESS_LISTING,
                           FileNotFoundError(2, "No such file or directory", "lsof")]))
        for label, probes in cases:
            with self.subTest(missing=label), \
                 patch.object(rs.sys, "platform", "darwin"), \
                 patch.object(rs, "_pid_is_alive", return_value=True), \
                 patch.object(rs.subprocess, "run", side_effect=probes):
                with self.assertRaises(RuntimeError) as caught:
                    rs._pid_is_router(4321)
            self.assertIn(label, str(caught.exception))
            self.assertIn("4321", str(caught.exception))


class CrashRecoveryTests(unittest.TestCase):
    def test_next_launcher_recovers_a_bound_router_without_published_state(self):
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs.subprocess, "Popen") as popen:
            rs.ensure_router()
            self.assertEqual(rs.ROUTER_PID.read_text(encoding="ascii"), str(child.pid))
            self.assertEqual(rs._router_owner_path().read_text(encoding="utf-8").splitlines()[0], str(child.pid))
            self.assertIsNone(child.poll())
            popen.assert_not_called()

    def test_next_launcher_replaces_a_dead_recorded_pid_with_the_verified_listener(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_alive", side_effect=lambda pid: pid == child.pid), \
             patch.object(rs, "_pid_is_router", side_effect=lambda pid: pid == child.pid), \
             patch.object(rs.subprocess, "Popen") as popen:
            rs.ROUTER_PID.write_text(f"{exited.pid}\n", encoding="ascii")
            rs.ensure_router()
            self.assertEqual(rs.ROUTER_PID.read_text(encoding="ascii"), str(child.pid))
            popen.assert_not_called()

    def test_an_adopted_pid_never_appears_before_its_claim(self):
        unvouched, write_atomic = _unvouched_pid_publications()
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs, "_write_atomic", new=write_atomic), \
             patch.object(rs.subprocess, "Popen") as popen:
            rs.ensure_router()
            self.assertIsNone(child.poll())
            popen.assert_not_called()
        self.assertEqual(unvouched, [], "the adopted pid file was published before a record vouched for it")

    def test_adoption_publishes_no_pid_when_its_claim_cannot_be_written(self):
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs, "_write_atomic", new=_refuse_claim_publication()), \
             patch.object(rs.subprocess, "Popen") as popen:
            with self.assertRaises(OSError):
                rs.ensure_router()
            self.assertFalse(rs.ROUTER_PID.exists(), "a pid file was published with no record vouching for it")
            self.assertIsNone(child.poll())
            popen.assert_not_called()

    def test_adoption_retires_its_claim_when_the_pid_file_cannot_be_published(self):
        real_replace = os.replace
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs.subprocess, "Popen") as popen:

            def replace(source, destination):
                if Path(destination) == rs.ROUTER_PID:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_replace(source, destination)

            with patch.object(rs.os, "replace", new=replace):
                with self.assertRaises(OSError):
                    rs.ensure_router()
            self.assertFalse(rs.ROUTER_PID.exists())
            self.assertFalse(rs._router_owner_path().exists(),
                             "a claim outlived the pid file that pointed at it")
            self.assertIsNone(child.poll())
            popen.assert_not_called()

    def test_a_pair_of_claims_naming_a_dead_pid_converges_to_a_fresh_router(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with _spawned_router_on(port) as (spawn, child_pid), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 20.0), \
             patch.object(rs, "_pid_is_router", side_effect=lambda pid: pid == child_pid()), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn) as popen:
            _write_claim(exited.pid)
            (Path(directory) / "router.pid").write_text(f"{exited.pid}\n", encoding="ascii")
            rs.ensure_router()
            self.assertEqual(popen.call_count, 1, "the stale claims did not converge on a fresh router")
            self.assertEqual(rs.ROUTER_PID.read_text(encoding="ascii"), str(child_pid()))
            self.assertEqual(
                rs._router_owner_path().read_text(encoding="utf-8").splitlines()[0],
                str(child_pid()),
            )
            self.assertTrue(rs.router_is_ready())

    def test_refusing_an_unverified_listener_retires_the_dead_pid_it_saw(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_alive", side_effect=lambda pid: pid == child.pid), \
             patch.object(rs, "_pid_is_router", return_value=False), \
             patch.object(rs.subprocess, "Popen") as popen:
            _write_claim(exited.pid)
            (Path(directory) / "router.pid").write_text(f"{exited.pid}\n", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "unverified process"):
                rs.ensure_router()
            self.assertIsNone(child.poll())
            self.assertFalse((Path(directory) / "router.pid").exists(),
                             "the refusal left a pid file naming a process that is gone")
            self.assertFalse(rs._router_owner_path().exists(),
                             "the refusal left a claim naming a process that is gone")
            popen.assert_not_called()

    def test_a_claim_naming_another_process_does_not_survive_adoption(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid}), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs.subprocess, "Popen") as popen:
            _write_claim(exited.pid)
            self.assertNotEqual(exited.pid, child.pid)
            rs.ensure_router()
            self.assertEqual(rs.ROUTER_PID.read_text(encoding="ascii"), str(child.pid))
            self.assertEqual(
                rs._router_owner_path().read_text(encoding="utf-8"),
                f"{child.pid}\n{rs._ROUTER_ROOT}\n",
                "adoption kept a claim that named some other process",
            )
            self.assertIsNone(child.poll())
            popen.assert_not_called()

    def test_next_launcher_refuses_to_choose_between_two_router_candidates(self):
        with _bound_child() as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_listener_pids", return_value={child.pid, child.pid + 1}), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "unverified process"):
                rs.ensure_router()
            self.assertIsNone(child.poll())
            self.assertFalse(rs.ROUTER_PID.exists())
            self.assertFalse(rs._router_owner_path().exists())
            popen.assert_not_called()


class OwnerFileTests(unittest.TestCase):
    def setUp(self):
        _sandbox_data_paths(self)

    def test_owner_deletion_requires_matching_checkout(self):
        owner_path = rs._router_owner_path()
        owner_path.write_text(f"{os.getpid()}\n/other-checkout\n", encoding="utf-8")
        rs._clear_router_owner(os.getpid())
        self.assertTrue(owner_path.exists())

    def test_owner_deletion_reports_matching_cleanup(self):
        owner_path = rs._router_owner_path()
        owner_path.write_text(f"{os.getpid()}\n{rs._ROUTER_ROOT}\n", encoding="utf-8")
        self.assertTrue(rs._clear_router_owner(os.getpid()))
        self.assertFalse(owner_path.exists())

    def test_owner_deletion_reports_unlink_failure(self):
        owner_path = rs._router_owner_path()
        owner_path.write_text(f"{os.getpid()}\n{rs._ROUTER_ROOT}\n", encoding="utf-8")
        with patch.object(Path, "unlink", side_effect=PermissionError("owner locked")):
            self.assertFalse(rs._clear_router_owner(os.getpid()))
        self.assertTrue(owner_path.exists())

    def test_owner_publication_survives_an_interrupted_write(self):
        rs._publish_router_claims(111)
        with patch.object(Path, "write_text", new=_short_write_text):
            with self.assertRaises(OSError):
                rs._publish_router_claims(222)
        self.assertEqual(rs._router_owner_path().read_text(encoding="utf-8"),
                         f"111\n{rs._ROUTER_ROOT}\n")

    def test_owner_content_that_cannot_be_parsed_is_left_alone(self):
        owner_path = rs._router_owner_path()
        owner_path.write_text("half-written claim\n", encoding="utf-8")
        self.assertFalse(rs._clear_router_owner(None))
        self.assertTrue(owner_path.exists())

    def test_owner_content_that_cannot_be_parsed_is_replaced_by_the_next_launch(self):
        owner_path = rs._router_owner_path()
        owner_path.write_text("half-written claim\n", encoding="utf-8")
        rs._publish_router_claims(4321)
        self.assertEqual(owner_path.read_text(encoding="utf-8"), f"4321\n{rs._ROUTER_ROOT}\n")

    def test_owner_whose_read_fails_is_left_alone(self):
        owner_path = rs._router_owner_path()
        owner_path.write_text(f"{os.getpid()}\n{rs._ROUTER_ROOT}\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(rs._clear_router_owner(os.getpid()))
        self.assertTrue(owner_path.exists())

    def test_windows_identity_defers_to_a_claim_it_cannot_read(self):
        with patch.object(rs.sys, "platform", "win32"), \
             patch.object(rs.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(rs, "_pid_is_alive", return_value=True), \
             patch.object(rs.subprocess, "run", return_value=PidIdentityTests.POWERSHELL), \
             patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            rs._router_owner_path().write_text("123\nC:\\other-checkout\n", encoding="utf-8")
            self.assertFalse(rs._pid_is_router(123))


class StartupLockTests(unittest.TestCase):
    def setUp(self):
        _sandbox_data_paths(self)

    def test_readiness_is_rechecked_rather_than_spawning(self):
        cases = (
            ("lock_was_acquired", False, False, 2),
            ("port_already_bound", True, False, 2),
            ("lock_is_held_elsewhere", False, True, 3),
        )
        for label, port_open, lock_held, expected in cases:
            with self.subTest(launcher=label):
                ready_calls = 0

                def ready(becomes_ready_at=expected):
                    nonlocal ready_calls
                    ready_calls += 1
                    return ready_calls == becomes_ready_at

                with ExitStack() as stack:
                    stack.enter_context(patch.object(rs, "_port_is_open", return_value=port_open))
                    stack.enter_context(patch.object(rs, "router_is_ready", side_effect=ready))
                    if lock_held:
                        stack.enter_context(patch.object(rs, "_startup_lock", return_value=None))
                    popen = stack.enter_context(patch.object(rs.subprocess, "Popen"))
                    rs.ensure_router()
                self.assertEqual(ready_calls, expected)
                popen.assert_not_called()

    def test_returned_lock_handle_holds_advisory_lock(self):
        if sys.platform == "win32":
            self.skipTest("POSIX advisory-lock probe")
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "router.log"
            lock_path = log_path.with_suffix(".lock")
            with patch.object(rs, "ROUTER_LOG", log_path):
                handle = rs._startup_lock()
                self.assertIsNotNone(handle)
                contender = lock_path.open("r+")
                try:
                    import fcntl

                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    contender.close()
                    rs._release_startup_lock(handle)

    def test_contention_keeps_the_same_lock_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "router.log"
            lock_path = log_path.with_suffix(".lock")
            with patch.object(rs, "ROUTER_LOG", log_path):
                first = rs._startup_lock()
                self.assertIsNotNone(first)
                sentinel = lock_path.with_suffix(".sentinel")
                os.link(lock_path, sentinel)
                first_inode = lock_path.stat().st_ino
                self.assertIsNone(rs._startup_lock())
                rs._release_startup_lock(first)
                second = rs._startup_lock()
                self.assertIsNotNone(second)
                self.assertEqual(lock_path.stat().st_ino, first_inode)
                self.assertEqual(lock_path.stat().st_ino, sentinel.stat().st_ino)
                rs._release_startup_lock(second)

    def test_a_failed_lock_file_call_does_not_leak_the_lock_descriptor(self):
        if sys.platform == "win32":
            self.skipTest("POSIX advisory-lock holder")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_try_lock_file", side_effect=OSError(errno.ENOSPC, "no space")):
            opened = []
            real_open = Path.open

            def watched_open(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                opened.append(handle)
                return handle

            with patch.object(Path, "open", new=watched_open):
                with self.assertRaises(OSError):
                    rs._startup_lock()
        for handle in opened:
            self.assertTrue(handle.closed, "the startup lock descriptor outlived the failed acquisition")

    def test_startup_lock_propagates_open_errors(self):
        error = PermissionError("lock path unavailable")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(Path, "open", side_effect=error):
            with self.assertRaisesRegex(PermissionError, "lock path unavailable"):
                rs._startup_lock()

    def test_persistent_advisory_lock_allows_one_owner_under_contention(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"):
            first = rs._startup_lock()
            self.assertIsNotNone(first)
            barrier = threading.Barrier(8)
            handles = []
            failures = []

            def claim():
                try:
                    barrier.wait()
                    handle = rs._startup_lock()
                    if handle is not None:
                        handles.append(handle)
                except BaseException as error:
                    failures.append(error)

            threads = [threading.Thread(target=claim) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(failures, [])
            self.assertEqual(handles, [])
            rs._release_startup_lock(first)
            reacquired = rs._startup_lock()
            self.assertIsNotNone(reacquired)
            rs._release_startup_lock(reacquired)

    def test_contending_launcher_retries_the_lock_after_readiness_timeout(self):
        attempts = 0

        def startup_lock():
            nonlocal attempts
            attempts += 1
            return None if attempts == 1 else MagicMock()

        process = MagicMock(pid=4321, poll=MagicMock(return_value=None))
        with patch.object(rs, "ROUTER_START_TIMEOUT", 0.2), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "_startup_lock", side_effect=startup_lock), \
             patch.object(rs, "_release_startup_lock"), \
             patch.object(rs.subprocess, "Popen", return_value=process) as popen:
            with patch.object(rs, "router_is_ready", side_effect=lambda: popen.call_count > 0):
                rs.ensure_router()
        self.assertEqual(attempts, 2)
        self.assertEqual(popen.call_count, 1)

    def test_second_launcher_uses_configured_startup_timeout(self):
        clock = 0.0

        def monotonic():
            return clock

        def sleep(_interval):
            nonlocal clock
            clock += 0.2

        with patch.object(rs, "ROUTER_START_TIMEOUT", 0.5), \
             patch.object(rs, "router_is_ready", return_value=False), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "_startup_lock", return_value=None), \
             patch.object(rs.time, "monotonic", side_effect=monotonic), \
             patch.object(rs.time, "sleep", side_effect=sleep):
            with self.assertRaises(RuntimeError):
                rs.ensure_router()
        self.assertLess(clock, 0.7)

    def test_lock_holder_spawns_router_with_pid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "data" / "router.log"
            pid_path = Path(directory) / "data" / "router.pid"
            process = MagicMock(pid=4321, poll=MagicMock(return_value=None))
            with patch.object(rs, "ROUTER_LOG", log_path), \
                 patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready", side_effect=[False, False, True]), \
                 patch.object(rs.subprocess, "Popen", return_value=process) as popen:
                rs.ensure_router()
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [sys.executable, "-m", "modules.router"])
            self.assertEqual(kwargs["cwd"], str(log_path.parent.parent))
            self.assertEqual(pid_path.read_text(encoding="ascii"), "4321")
            self.assertTrue(log_path.with_suffix(".lock").exists())


class RouterShutdownTests(unittest.TestCase):
    def setUp(self):
        _sandbox_data_paths(self)
        with socket.socket() as probe:
            probe.bind((rs.ROUTER_HOST, 0))
            closed_port = probe.getsockname()[1]
        patcher = patch.object(rs, "ROUTER_PORT", closed_port)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stop_lock_obeys_deadline_when_contention_persists(self):
        clock = 0.0

        def monotonic():
            return clock

        def sleep(interval):
            nonlocal clock
            clock += interval

        with patch.object(rs, "_startup_lock", return_value=None), \
             patch.object(rs, "_STOP_TIMEOUT", 0.4), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.7), \
             patch.object(rs.time, "monotonic", side_effect=monotonic), \
             patch.object(rs.time, "sleep", side_effect=sleep):
            self.assertIsNone(rs._stop_lock())
        self.assertAlmostEqual(clock, 0.7, msg="the stop lock gave up before its startup budget ran out")

    def test_stop_refuses_when_the_lock_is_held_past_the_deadline(self):
        clock = 0.0

        def monotonic():
            return clock

        def contended_lock():
            nonlocal clock
            clock = 0.5
            return None

        with patch.object(rs, "_startup_lock", side_effect=contended_lock), \
             patch.object(rs, "_STOP_TIMEOUT", 0.0), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.01), \
             patch.object(rs.time, "monotonic", side_effect=monotonic):
            self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)

    def test_a_killed_lock_holder_does_not_wedge_the_next_launch(self):
        if sys.platform == "win32":
            self.skipTest("POSIX advisory-lock holder")
        rs._startup_lock_path().touch()
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys, time\n"
             "handle = open(sys.argv[1], 'r+')\n"
             "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "time.sleep(30)\n",
             str(rs._startup_lock_path())],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertIsNone(rs._startup_lock())
            holder.kill()
            holder.wait(timeout=5)
            recovered = rs._startup_lock()
            self.assertIsNotNone(recovered, "the killed holder left the startup lock wedged")
            rs._release_startup_lock(recovered)
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait(timeout=5)
            holder.stdout.close()

    def test_a_refused_launch_leaves_the_startup_lock_free(self):
        if sys.platform == "win32":
            self.skipTest("POSIX advisory-lock holder")
        import fcntl

        rs._startup_lock_path().touch()
        with rs._startup_lock_path().open("r+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            with patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
                 patch.object(rs, "router_is_ready", return_value=False), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs.subprocess, "Popen"):
                with self.assertRaises(RuntimeError):
                    rs.ensure_router()
            self.assertIsNone(rs._startup_lock(), "the refused launch took the lock its holder was holding")
        recovered = rs._startup_lock()
        self.assertIsNotNone(recovered, "the refused launch left the startup lock held")
        rs._release_startup_lock(recovered)

    def test_stop_lock_propagates_acquisition_errors(self):
        error = OSError("lock acquisition failed")
        with patch.object(rs, "_startup_lock", side_effect=error):
            with self.assertRaisesRegex(OSError, "lock acquisition failed"):
                rs._stop_lock()

    def test_closed_port_does_not_report_live_process_stopped(self):
        with _idle_process() as process, \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "_terminate_router", return_value=True), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "_STOP_TIMEOUT", 0.0):
            rs.ROUTER_PID.write_text(str(process.pid), encoding="ascii")
            self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)

    def test_startup_failure_names_a_stale_owner_file(self):
        with _idle_children() as (spawn, _), \
             _spawned_tempdir() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.05), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "router_is_ready", return_value=False), \
             patch.object(rs, "_clear_router_owner", return_value=False), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(RuntimeError) as caught:
                rs.ensure_router()
        self.assertIn("router.log.owner", "\n".join(getattr(caught.exception, "__notes__", [])))

    def test_stop_against_an_already_absent_router_reports_absence(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_port_is_open", return_value=False):
            self.assertIs(rs.stop_router(), rs.StopOutcome.ABSENT)

    def test_stop_without_pid_preserves_owner_from_another_checkout(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_startup_lock", return_value=MagicMock()), \
             patch.object(rs, "_release_startup_lock"), \
             patch.object(rs, "_sweep_router_listeners", return_value=False), \
             patch.object(rs, "_port_is_open", return_value=False):
            owner_path = rs._router_owner_path()
            owner_path.write_text("4321\n/other-checkout\n", encoding="utf-8")
            self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)
            self.assertTrue(owner_path.exists())

    def test_stop_router_terminates_verified_pid_and_removes_pid_file(self):
        with _idle_process() as process, tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "router.pid"
            pid_path.write_text(str(process.pid), encoding="ascii")
            with patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
                 patch.object(rs, "_pid_is_router", return_value=True), \
                 patch.object(rs, "_port_is_open", return_value=False):
                self.assertIs(rs.stop_router(), rs.StopOutcome.STOPPED)
            self.assertIsNotNone(process.poll())
            self.assertFalse(pid_path.exists())

    def test_failed_stop_preserves_verified_live_pid(self):
        with _idle_process() as process, tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "router.pid"
            log_path = Path(directory) / "router.log"
            pid_path.write_text(str(process.pid), encoding="ascii")
            with patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "ROUTER_LOG", log_path), \
                 patch.object(rs, "_pid_is_router", return_value=True), \
                 patch.object(rs, "_terminate_router", return_value=False), \
                 patch.object(rs, "_kill_router", return_value=False), \
                 patch.object(rs, "_sweep_router_listeners", return_value=False), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "_STOP_TIMEOUT", 0.0):
                self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)
            self.assertEqual(pid_path.read_text(encoding="ascii"), str(process.pid))
            self.assertIsNone(process.poll())

    def test_stop_router_escalates_to_sigkill(self):
        with tempfile.TemporaryDirectory() as directory, _stubborn_child(directory) as process:
            pid_path = Path(directory) / "router.pid"
            pid_path.write_text(f"{process.pid}\n", encoding="ascii")
            with patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
                 patch.object(rs, "_pid_is_router", return_value=True), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "_STOP_TIMEOUT", 0.3):
                self.assertIs(rs.stop_router(), rs.StopOutcome.STOPPED)
            self.assertIsNotNone(process.poll())
            self.assertFalse(pid_path.exists())

    def test_stop_router_keeps_claims_when_escalation_is_denied(self):
        # One escalation per platform: os.kill(SIGKILL) on posix, taskkill /F on
        # win32. Deny only the one this platform actually makes.
        if sys.platform == "win32":
            real_run = subprocess.run

            def deny_escalation(command, **kwargs):
                if command[:1] == ["taskkill"]:
                    raise OSError("escalation denied")
                return real_run(command, **kwargs)

            guard = patch.object(rs.subprocess, "run", side_effect=deny_escalation)
        else:
            real_kill = os.kill

            def deny_sigkill(pid, sig):
                if sig == signal.SIGKILL:
                    raise PermissionError("denied")
                return real_kill(pid, sig)

            guard = patch.object(rs.os, "kill", new=deny_sigkill)

        with tempfile.TemporaryDirectory() as directory, _stubborn_child(directory) as process:
            pid_path = Path(directory) / "router.pid"
            pid_path.write_text(f"{process.pid}\n", encoding="ascii")
            with patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
                 patch.object(rs, "_pid_is_router", return_value=True), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "_STOP_TIMEOUT", 0.0), \
                 guard:
                _write_claim(process.pid)
                self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)
                self.assertTrue(rs._router_owner_path().exists())
            self.assertIsNone(process.poll())
            self.assertEqual(pid_path.read_text(encoding="ascii"), f"{process.pid}\n")

    def test_stop_kills_a_listener_that_ignores_sigterm(self):
        with _bound_child(ignore_sigterm=True) as (child, port), \
             tempfile.TemporaryDirectory() as directory, \
             patch.object(rs, "ROUTER_PORT", port), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs, "_STOP_TIMEOUT", 0.3):
            self.assertIs(rs.stop_router(), rs.StopOutcome.STOPPED)
            self.assertIsNotNone(child.poll())

    def test_windows_stop_runs_taskkill_once_and_never_signals_a_pid(self):
        commands = []
        signs = []

        def record(command, **kwargs):
            commands.append(list(command))
            return MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "router.pid"
            pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
            with patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
                 patch.object(rs, "_startup_lock", return_value=MagicMock()), \
                 patch.object(rs, "_release_startup_lock"), \
                 patch.object(rs, "_pid_is_router", return_value=True), \
                 patch.object(rs, "_pid_is_alive", return_value=True), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "_sweep_router_listeners", return_value=False), \
                 patch.object(rs, "_STOP_TIMEOUT", 0.0), \
                 patch.object(rs.sys, "platform", "win32"), \
                 patch.object(rs.subprocess, "run", new=record), \
                 patch.object(rs.os, "kill", new=lambda pid, sig: signs.append(sig)):
                rs.stop_router()
        self.assertEqual([c for c in commands if c[0] == "taskkill"],
                         [["taskkill", "/PID", str(os.getpid()), "/T", "/F"]])
        self.assertEqual(signs, [])

    def test_stop_waits_for_active_startup_before_reporting_success(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
                 patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"):
                pid_path = rs.ROUTER_PID
                startup_lock = rs._startup_lock()
                self.assertIsNotNone(startup_lock)
                published = threading.Event()

                def publish():
                    time.sleep(0.1)
                    pid_path.write_text(str(process.pid), encoding="ascii")
                    rs._release_startup_lock(startup_lock)
                    published.set()

                publisher = threading.Thread(target=publish)
                publisher.start()

                def terminate(_pid):
                    process.terminate()
                    return True

                try:
                    with patch.object(rs, "_pid_is_router", return_value=True), \
                         patch.object(rs, "_terminate_router", side_effect=terminate), \
                         patch.object(rs, "_port_is_open", return_value=False):
                        self.assertIs(rs.stop_router(), rs.StopOutcome.STOPPED)
                    self.assertTrue(published.is_set())
                    self.assertIsNotNone(process.poll())
                finally:
                    publisher.join(timeout=5)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


class ListenerScanTests(unittest.TestCase):
    def test_windows_scan_ignores_a_port_that_only_contains_the_router_port(self):
        listing = MagicMock(returncode=0, stdout="\n".join((
            "  TCP    127.0.0.1:8080    0.0.0.0:0    LISTENING       4242",
            "  TCP    127.0.0.1:80    0.0.0.0:0    LISTENING       4343",
            "  TCP    127.0.0.1:80    127.0.0.1:51234    ESTABLISHED     4444",
            "",
        )))
        with patch.object(rs.sys, "platform", "win32"), \
             patch.object(rs, "ROUTER_PORT", 80), \
             patch.object(rs.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(rs.subprocess, "run", return_value=listing):
            self.assertEqual(rs._listener_pids(), {4343})


class SuiteSafetyTests(unittest.TestCase):
    PORT_DRIVEN = ("stop_router", "_sweep_router_listeners")
    PORT_GUARDS = ("ROUTER_PORT", "_sweep_router_listeners", "_listener_pids", "_terminate_router", "_kill_router")
    DATA_DRIVEN = ("ensure_router", "stop_router", "_startup_lock", "_stop_lock", "_startup_lock_uncontended")
    DATA_GUARDS = ("ROUTER_LOG", "ROUTER_BOOT_LOG", "ROUTER_PID", "_sandbox_data_paths", "_startup_lock", "_release_startup_lock")

    def _offenders(self, driven, guards, only_this_file=False):
        paths = [Path(__file__)] if only_this_file else sorted(Path(__file__).parent.glob("test_*.py"))
        offenders = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                setup = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "setUp"]
                fixture_guard = any(f"'{name}'" in ast.dump(n) for n in setup for name in guards)
                for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]:
                    body = ast.dump(fn)
                    reached = [name for name in driven if f"'{name}'" in body]
                    guarded = fixture_guard or any(f"'{name}'" in body for name in guards)
                    if reached and not guarded:
                        offenders.append(f"{path.name}::{cls.name}::{fn.name} reaches {', '.join(reached)}")
        return offenders

    def test_no_test_signals_a_listener_on_the_configured_router_port(self):
        offenders = self._offenders(self.PORT_DRIVEN, self.PORT_GUARDS)
        self.assertEqual(
            offenders, [],
            "these tests reach the router port sweep and could signal a live router:\n  "
            + "\n  ".join(offenders)
            + "\npatch ROUTER_PORT, or stub the sweep, so a suite run cannot reach a live router.",
        )

    def test_no_test_in_this_file_writes_to_the_configured_data_directory(self):
        offenders = self._offenders(self.DATA_DRIVEN, self.DATA_GUARDS, only_this_file=True)
        self.assertEqual(
            offenders, [],
            "these tests reach the real data directory and would overwrite a developer's router.pid "
            "or router.log.owner:\n  " + "\n  ".join(offenders)
            + "\ncall _sandbox_data_paths in the class setUp so every path lands in a temporary directory.",
        )



class RouterChildEnvironmentTests(unittest.TestCase):
    def test_the_router_child_inherits_no_cx_variables(self) -> None:
        captured = {}

        class _FakePopen:
            pid = 4242

            def __init__(self, command, **kwargs):
                captured.update(kwargs)
                captured["command"] = command

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"CX_ROUTER_API_KEY": "sk-router-secret",
                                     "CX_ROUTER_PORT": "4000",
                                     "PATH": "/usr/bin"}, clear=True), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "router_is_ready", side_effect=[False, False, True]), \
             patch.object(rs, "_adopt_running_router", return_value=False), \
             patch.object(rs, "_pid_is_router", return_value=True), \
             patch.object(rs, "_pid_is_alive", return_value=True), \
             patch.object(rs, "_clear_router_claims", return_value=[]), \
             patch.object(rs.subprocess, "Popen", _FakePopen), \
             patch.object(rs, "_startup_lock", return_value=MagicMock()), \
             patch.object(rs, "_release_startup_lock"):
            rs.ensure_router()

        environment = captured["env"]
        self.assertTrue(all(not name.upper().startswith("CX_") for name in environment))
        self.assertNotIn("sk-router-secret", " ".join(environment.values()))
        self.assertEqual(environment["PATH"], "/usr/bin")

class PidFileTests(unittest.TestCase):
    def test_read_router_pid_returns_the_file_value(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "router.pid"
            with patch.object(rs, "ROUTER_PID", pid_path):
                pid_path.write_text("4321\n", encoding="ascii")
                self.assertEqual(rs.read_router_pid(), 4321)
                pid_path.write_text("not-a-pid", encoding="ascii")
                self.assertIsNone(rs.read_router_pid())
                pid_path.unlink()
                self.assertIsNone(rs.read_router_pid())

    def test_a_pid_no_platform_could_hold_reads_as_no_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "router.pid"
            with patch.object(rs, "ROUTER_PID", pid_path):
                for label, written in (("zero", "0"), ("negative", "-1"), ("beyond_the_pid_space", str(1 << 40))):
                    with self.subTest(value=label):
                        pid_path.write_text(written, encoding="ascii")
                        self.assertIsNone(rs.read_router_pid())


class StartupTimeoutTests(unittest.TestCase):
    def test_startup_failure_points_at_a_file_that_holds_the_failure(self):
        real_popen = subprocess.Popen

        def spawn_dying_child(*args, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom during import\\n'); raise SystemExit(2)"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            )

        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), \
                 tempfile.TemporaryDirectory() as directory, \
                 patch.object(rs, "ROUTER_LOG", Path(directory) / "data" / "router.log"), \
                 patch.object(rs, "ROUTER_PID", Path(directory) / "data" / "router.pid"), \
                 patch.object(rs, "ROUTER_BOOT_LOG", Path(directory) / "data" / "router.boot.log"), \
                 patch.object(rs.sys, "platform", platform), \
                 patch.object(rs.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                 patch.object(rs.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, create=True), \
                 patch.object(rs, "_startup_lock", return_value=MagicMock()), \
                 patch.object(rs, "_release_startup_lock"), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready", return_value=False), \
                 patch.object(rs.subprocess, "Popen", side_effect=spawn_dying_child):
                with self.assertRaises(RuntimeError) as caught:
                    rs.ensure_router()
                named = Path(str(caught.exception).rsplit("Check:\n", 1)[1])
                self.assertTrue(named.exists(), f"{named} named by the error does not exist")
                self.assertIn("boom during import", named.read_text(encoding="utf-8"))

    def test_readiness_timeout_points_at_the_log_the_child_writes(self):
        real_popen = subprocess.Popen
        spawned = []
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-started"

            def spawn_sleeping_child(*args, **kwargs):
                process = real_popen(
                    [sys.executable, "-c",
                     "import pathlib, sys, time\n"
                     "sys.stderr.write('router started\\n')\n"
                     "sys.stderr.flush()\n"
                     "pathlib.Path(sys.argv[1]).touch()\n"
                     "time.sleep(30)\n",
                     str(marker)],
                    stdout=kwargs["stdout"],
                    stderr=kwargs["stderr"],
                )
                spawned.append(process)
                return process

            def ready():
                if not spawned:
                    return False
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return False

            try:
                with patch.object(rs, "ROUTER_LOG", Path(directory) / "data" / "router.log"), \
                     patch.object(rs, "ROUTER_PID", Path(directory) / "data" / "router.pid"), \
                     patch.object(rs, "ROUTER_BOOT_LOG", Path(directory) / "data" / "router.boot.log"), \
                     patch.object(rs, "ROUTER_START_TIMEOUT", 0.5), \
                     patch.object(rs, "_startup_lock", return_value=MagicMock()), \
                     patch.object(rs, "_release_startup_lock"), \
                     patch.object(rs, "_port_is_open", return_value=False), \
                     patch.object(rs, "router_is_ready", side_effect=ready), \
                     patch.object(rs.subprocess, "Popen", side_effect=spawn_sleeping_child):
                    with self.assertRaises(RuntimeError) as caught:
                        rs.ensure_router()
                named = Path(str(caught.exception).rsplit("Check:\n", 1)[1])
                self.assertEqual(named.read_text(encoding="utf-8"), "router started\n")
            finally:
                for process in spawned:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)

    def test_contention_names_only_files_that_exist(self):
        if sys.platform == "win32":
            self.skipTest("POSIX advisory-lock holder")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            log_path = data / "router.log"
            boot_log = data / "router.boot.log"
            lock_path = log_path.with_suffix(".lock")
            lock_path.touch()
            with patch.object(rs, "ROUTER_LOG", log_path), \
                 patch.object(rs, "ROUTER_PID", data / "router.pid"), \
                 patch.object(rs, "ROUTER_BOOT_LOG", boot_log), \
                 patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
                 patch.object(rs, "router_is_ready", return_value=False), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "_release_startup_lock"), \
                 patch.object(rs.subprocess, "Popen") as popen, \
                 lock_path.open("r+") as holder:
                import fcntl

                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(RuntimeError) as contended:
                    rs.ensure_router()
            message = str(contended.exception)
            self.assertIn(str(lock_path), message)
            named = [token for token in message.split() if token.startswith("/")]
            self.assertNotEqual(named, [])
            for token in named:
                self.assertTrue(Path(token).exists(), f"the error names {token}, which does not exist")
            self.assertFalse(boot_log.exists())
            popen.assert_not_called()

    def test_a_published_pid_never_appears_before_its_claim(self):
        unvouched, write_atomic = _unvouched_pid_publications()
        real_popen = subprocess.Popen

        def spawn_dying_child(*args, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom during import\\n'); raise SystemExit(2)"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            )

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            with patch.object(rs, "ROUTER_LOG", data / "router.log"), \
                 patch.object(rs, "ROUTER_PID", data / "router.pid"), \
                 patch.object(rs, "ROUTER_BOOT_LOG", data / "router.boot.log"), \
                 patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
                 patch.object(rs, "_startup_lock", return_value=MagicMock()), \
                 patch.object(rs, "_release_startup_lock"), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready", return_value=False), \
                 patch.object(rs, "_write_atomic", new=write_atomic), \
                 patch.object(rs.subprocess, "Popen", side_effect=spawn_dying_child):
                with self.assertRaises(RuntimeError):
                    rs.ensure_router()
        self.assertEqual(unvouched, [], "the pid file was published before a record vouched for it")

    def test_failed_startup_kills_a_child_that_ignores_sigterm(self):
        real_popen = subprocess.Popen
        spawned = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = Path(directory.name) / "data"
        marker = data / "stubborn-child-installed-sigign"

        def spawn_stubborn_child(*args, **kwargs):
            process = real_popen(
                [sys.executable, "-c",
                 "import pathlib, signal, time\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "pathlib.Path('data/stubborn-child-installed-sigign').touch()\n"
                 "time.sleep(30)\n"],
                cwd=str(kwargs["cwd"]),
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            )
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "the child never installed its SIGTERM handler")
            spawned.append(process)
            self.addCleanup(_reap, spawned)
            return process

        with patch.object(rs, "ROUTER_LOG", data / "router.log"), \
             patch.object(rs, "ROUTER_PID", data / "router.pid"), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
             patch.object(rs, "_STOP_TIMEOUT", 0.3), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "router_is_ready", return_value=False), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn_stubborn_child):
            with self.assertRaises(RuntimeError):
                rs.ensure_router()
        self.assertEqual(len(spawned), 1)
        # win32's terminate and kill are the same TerminateProcess, so the child is
        # reaped but no returncode encodes a signal.
        self.assertIsNotNone(spawned[0].poll(), "a failed startup left its child running")
        if sys.platform != "win32":
            self.assertEqual(spawned[0].returncode, -signal.SIGKILL)

    def test_a_crowded_boot_log_is_rotated_away_before_the_next_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            boot_log = data / "router.boot.log"
            crowded = b"traceback from the last crash\n" * 3
            boot_log.write_bytes(crowded)
            with patch.object(rs, "ROUTER_LOG", data / "router.log"), \
                 patch.object(rs, "ROUTER_PID", data / "router.pid"), \
                 patch.object(rs, "ROUTER_BOOT_LOG", boot_log), \
                 patch.object(rs, "_LOG_ROTATE_BYTES", 8), \
                 patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
                 patch.object(rs, "_startup_lock", return_value=MagicMock()), \
                 patch.object(rs, "_release_startup_lock"), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready", return_value=False), \
                 patch.object(rs.subprocess, "Popen") as popen:
                with self.assertRaises(RuntimeError):
                    rs.ensure_router()
            popen.assert_called_once()
            self.assertEqual(boot_log.stat().st_size, 0, "the crowded boot log was appended to instead of rotated")
            self.assertEqual(boot_log.with_suffix(".log.1").read_bytes(), crowded)

    def test_child_output_reaches_a_boot_log_on_every_platform(self):
        real_popen = subprocess.Popen
        spawned = []
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-spoke"

            def spawn_child(*args, **kwargs):
                process = real_popen(
                    [sys.executable, "-c",
                     "import pathlib, sys, time\n"
                     "print('boom during import')\n"
                     "sys.stdout.flush()\n"
                     "pathlib.Path(sys.argv[1]).touch()\n"
                     "time.sleep(30)\n",
                     str(marker)],
                    stdout=kwargs["stdout"],
                    stderr=kwargs["stderr"],
                )
                spawned.append(process)
                return process

            for platform in ("linux", "darwin", "win32"):
                marker.unlink(missing_ok=True)
                with self.subTest(platform=platform), \
                     patch.object(rs, "ROUTER_LOG", Path(directory) / "data" / "router.log"), \
                     patch.object(rs, "ROUTER_PID", Path(directory) / "data" / "router.pid"), \
                     patch.object(rs, "ROUTER_BOOT_LOG", Path(directory) / "data" / "router.boot.log"), \
                     patch.object(rs.sys, "platform", platform), \
                     patch.object(rs.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                     patch.object(rs.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, create=True), \
                     patch.object(rs, "_startup_lock", return_value=MagicMock()), \
                     patch.object(rs, "_release_startup_lock"), \
                     patch.object(rs, "_port_is_open", return_value=False), \
                     patch.object(rs, "router_is_ready", side_effect=lambda: marker.exists()), \
                     patch.object(rs.subprocess, "Popen", side_effect=spawn_child):
                    data = Path(directory) / "data"
                    data.mkdir(exist_ok=True)
                    (data / "router.boot.log").write_text("earlier run\n", encoding="utf-8")
                    try:
                        rs.ensure_router()
                        self.assertEqual(
                            (data / "router.boot.log").read_text(encoding="utf-8"),
                            "earlier run\nboom during import\n",
                        )
                    finally:
                        for process in spawned:
                            if process.poll() is None:
                                process.kill()
                            process.wait(timeout=5)
                        spawned.clear()
                        _await_release(data / "router.boot.log")

    def test_failed_child_cleanup_names_a_live_orphan_and_keeps_its_claims(self):
        with _idle_children(faulty=True) as (spawn, spawned), \
             _spawned_tempdir() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "router_is_ready", return_value=False), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(RuntimeError) as caught:
                rs.ensure_router()
            self.assertEqual(len(spawned), 1)
            self.assertIsNone(spawned[0].poll())
            self.assertEqual(rs.ROUTER_PID.read_text(encoding="ascii"), str(spawned[0].pid))
            self.assertEqual(rs._router_owner_path().read_text(encoding="utf-8").splitlines()[0], str(spawned[0].pid))
            notes = "\n".join(getattr(caught.exception, "__notes__", []))
            self.assertIn(str(spawned[0].pid), notes)
            self.assertIn(str(rs.ROUTER_PID), notes)
            self.assertIn(str(rs._router_owner_path()), notes)

    def test_pid_publication_failure_cleans_child_and_startup_lock(self):
        real_replace = os.replace
        with _idle_children() as (spawn, spawned), _spawned_tempdir() as directory:
            pid_path = Path(directory) / "router.pid"
            log_path = Path(directory) / "router.log"

            def replace(source, destination):
                if Path(destination) == pid_path:
                    raise OSError("pid publication failed")
                return real_replace(source, destination)

            with patch.object(rs, "ROUTER_LOG", log_path), \
                 patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready", side_effect=[False, False]), \
                 patch.object(rs.subprocess, "Popen", side_effect=spawn), \
                 patch.object(rs.os, "replace", new=replace):
                with self.assertRaises(OSError):
                    rs.ensure_router()
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll())
            self.assertFalse(pid_path.exists())
            self.assertFalse((Path(directory) / "router.log.owner").exists())
            self.assertTrue(log_path.with_suffix(".lock").exists())

    def test_pid_publication_survives_an_interrupted_write(self):
        with _idle_children() as (spawn, spawned), \
             _spawned_tempdir() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "router_is_ready", side_effect=[False, False]), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn), \
             patch.object(Path, "write_text", new=_short_write_text):
            with self.assertRaises(OSError):
                rs.ensure_router()
            self.assertFalse(rs.ROUTER_PID.exists())
            self.assertEqual(len(spawned), 1)

    def test_an_interrupt_during_readiness_leaves_no_child_and_no_claim(self):
        with _idle_children() as (spawn, spawned), _spawned_tempdir() as directory:
            data = Path(directory) / "data"
            with patch.object(rs, "ROUTER_LOG", data / "router.log"), \
                 patch.object(rs, "ROUTER_PID", data / "router.pid"), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready",
                              side_effect=[False, False, KeyboardInterrupt]), \
                 patch.object(rs.subprocess, "Popen", side_effect=spawn):
                with self.assertRaises(KeyboardInterrupt):
                    rs.ensure_router()
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll())
            self.assertFalse((data / "router.pid").exists())
            self.assertFalse((data / "router.log.owner").exists())

    def test_an_unpublished_child_orphan_names_no_claim_file(self):
        with _idle_children(faulty=True) as (spawn, spawned), \
             _spawned_tempdir() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.0), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "router_is_ready", return_value=False), \
             patch.object(rs, "_write_atomic", new=_refuse_claim_publication()), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(OSError) as caught:
                rs.ensure_router()
            self.assertEqual(len(spawned), 1)
            self.assertIsNone(spawned[0].poll())
            notes = "\n".join(getattr(caught.exception, "__notes__", []))
            self.assertIn("may still be running", notes)
            self.assertNotIn(str(rs.ROUTER_PID), notes)
            self.assertNotIn(str(rs._router_owner_path()), notes)

    def test_startup_failure_names_a_pid_file_it_could_not_remove(self):
        real_unlink = Path.unlink
        with _idle_children() as (spawn, spawned), _spawned_tempdir() as directory:
            pid_path = Path(directory) / "router.pid"

            def deny_pid_unlink(path, missing_ok=False):
                if Path(path) == pid_path:
                    raise PermissionError(13, "Permission denied")
                return real_unlink(path, missing_ok=missing_ok)

            with patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
                 patch.object(rs, "ROUTER_PID", pid_path), \
                 patch.object(rs, "ROUTER_START_TIMEOUT", 0.05), \
                 patch.object(rs, "_port_is_open", return_value=False), \
                 patch.object(rs, "router_is_ready", return_value=False), \
                 patch.object(rs.subprocess, "Popen", side_effect=spawn), \
                 patch.object(Path, "unlink", new=deny_pid_unlink):
                with self.assertRaises(RuntimeError) as caught:
                    rs.ensure_router()
            self.assertIn(str(pid_path), "\n".join(getattr(caught.exception, "__notes__", [])))

    def test_timeout_terminates_child_and_removes_pid(self):
        with _idle_children() as (spawn, spawned), \
             _spawned_tempdir() as directory, \
             patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"), \
             patch.object(rs, "ROUTER_PID", Path(directory) / "router.pid"), \
             patch.object(rs, "ROUTER_START_TIMEOUT", 0.05), \
             patch.object(rs, "router_is_ready", return_value=False), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(RuntimeError):
                rs.ensure_router()
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll())
            self.assertFalse((Path(directory) / "router.pid").exists())
            self.assertTrue((Path(directory) / "router.lock").exists())


if __name__ == "__main__":
    unittest.main()
