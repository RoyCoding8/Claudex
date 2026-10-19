from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from modules import process
from modules import router_starter as rs
from tests.test_router import _running_router


@contextmanager
def _paths():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        for name, value in {"ROUTER_PID": root / "router.pid", "ROUTER_LOG": root / "router.log",
                            "ROUTER_BOOT_LOG": root / "router.boot.log"}.items():
            stack.enter_context(patch.object(rs, name, value))
        yield root


class RouterIdentityTests(unittest.TestCase):
    def test_identity_is_published_and_only_its_owner_can_clear_it(self):
        with _paths():
            first = rs.publish_router_identity(os.getpid(), "a" * 32)
            self.assertEqual(rs.read_router_pid(), os.getpid())
            second = rs.publish_router_identity(os.getpid(), "b" * 32)
            rs.clear_router_identity(first)
            self.assertEqual(rs._read_identity(), second)
            rs.clear_router_identity(second)
            self.assertIsNone(rs.read_router_pid())

    def test_invalid_or_legacy_claims_do_not_identify_a_router(self):
        with _paths():
            for raw in [b"123", b"{", b"x" * 20_000, b'{"pid":true,"token":"aaaaaaaaaaaaaaaa"}',
                        b'{"pid":-1,"token":"aaaaaaaaaaaaaaaa"}']:
                rs.ROUTER_PID.write_bytes(raw)
                self.assertIsNone(rs.read_router_pid())

    def test_health_requires_the_recorded_pid_and_token(self):
        with _paths(), _running_router() as server, \
                patch.object(rs, "ROUTER_HOST", "127.0.0.1"), patch.object(rs, "ROUTER_PORT", server.server_port):
            rs.publish_router_identity(os.getpid(), server.instance_token)
            self.assertTrue(rs.router_is_ready())
            rs.publish_router_identity(os.getpid(), "wrong-instance-token")
            self.assertFalse(rs.router_is_ready())
            rs.publish_router_identity(os.getpid() + 1, server.instance_token)
            self.assertFalse(rs.router_is_ready())

    def test_second_listener_cannot_bind_the_router_port(self):
        from modules.router import _RouterServer
        with _running_router() as server, self.assertRaises(OSError):
            _RouterServer(("127.0.0.1", server.server_port))

    def test_unresponsive_listener_is_left_running(self):
        with _paths(), socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with patch.object(rs, "ROUTER_PORT", listener.getsockname()[1]), \
                    patch.object(rs.subprocess, "Popen") as spawn:
                with self.assertRaises(rs.PortOccupiedError) as raised:
                    rs.ensure_router()
                self.assertEqual(raised.exception.port, listener.getsockname()[1])
                self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)
            spawn.assert_not_called()

    def test_absent_router_and_stale_dead_identity(self):
        with _paths(), patch.object(rs.process, "port_is_open", return_value=False):
            self.assertIs(rs.stop_router(), rs.StopOutcome.ABSENT)
            rs.publish_router_identity(123, "a" * 32)
            with patch.object(rs, "_health_check", return_value=False), \
                    patch.object(rs.process, "pid_is_alive", return_value=False):
                self.assertIs(rs.stop_router(), rs.StopOutcome.ABSENT)
            self.assertFalse(rs.ROUTER_PID.exists())

    def test_live_unresponsive_identity_is_not_killed(self):
        with _paths(), patch.object(rs.process, "port_is_open", return_value=False):
            identity = rs.publish_router_identity(os.getpid(), "a" * 32)
            with patch.object(rs, "_health_check", return_value=False), \
                    patch.object(rs, "_request_shutdown") as shutdown:
                self.assertIs(rs.stop_router(), rs.StopOutcome.REFUSED)
            shutdown.assert_not_called()
            self.assertEqual(rs._read_identity(), identity)

    def test_shutdown_requires_key_and_current_instance_token(self):
        with _running_router() as server, patch.object(rs, "ROUTER_HOST", "127.0.0.1"), \
                patch.object(rs, "ROUTER_PORT", server.server_port):
            identity = rs.RouterIdentity(os.getpid(), server.instance_token)
            with patch.object(rs, "ROUTER_API_KEY", "wrong"):
                self.assertFalse(rs._request_shutdown(identity))
            with patch.object(rs, "ROUTER_API_KEY", "test-router-key"):
                self.assertFalse(rs._request_shutdown(rs.RouterIdentity(os.getpid(), "wrong")))
                self.assertTrue(rs._request_shutdown(identity))


class RouterProcessTests(unittest.TestCase):
    def test_process_liveness_uses_the_actual_process(self):
        self.assertTrue(process.pid_is_alive(os.getpid(), 1))
        self.assertFalse(process.pid_is_alive(0, 1))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=5)
        self.assertFalse(process.pid_is_alive(child.pid, 1))

    def test_start_reuse_and_stop_with_environment_overrides(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            shutil.copytree(Path(rs.__file__).parent, root / "modules", ignore=shutil.ignore_patterns("__pycache__"))
            with socket.socket() as upstream, socket.socket() as reservation:
                upstream.bind(("127.0.0.1", 0))
                upstream.listen()
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
                reservation.close()
                data = root / "data"
                data.mkdir()
                overrides = {"CX_ROUTER_PORT": str(port), "CX_ROUTER_API_KEY": "test-router-key",
                             "CX_CLIPROXY_PORT": str(upstream.getsockname()[1]), "CX_ROUTER_START_TIMEOUT": "10"}
                stack.enter_context(patch.dict(os.environ, overrides))
                for name, value in {"ROUTER_PID": data / "router.pid", "ROUTER_LOG": data / "router.log",
                                    "ROUTER_BOOT_LOG": data / "router.boot.log", "ROUTER_HOST": "127.0.0.1",
                                    "ROUTER_PORT": port, "ROUTER_API_KEY": "test-router-key",
                                    "ROUTER_START_TIMEOUT": 10}.items():
                    stack.enter_context(patch.object(rs, name, value))
                try:
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        list(executor.map(lambda _: rs.ensure_router(), range(3)))
                    first = rs._read_identity()
                    self.assertIsNotNone(first)
                    self.assertTrue(rs.router_is_ready())
                    rs.ensure_router()
                    self.assertEqual(rs._read_identity(), first)
                    self.assertIs(rs.stop_router(), rs.StopOutcome.STOPPED)
                    self.assertFalse(rs.ROUTER_PID.exists())
                    self.assertFalse(process.port_is_open("127.0.0.1", port))
                finally:
                    identity = rs._read_identity()
                    if identity is not None:
                        rs._request_shutdown(identity)
                        process.wait_until(lambda: not process.pid_is_alive(identity.pid, 1), 5)

    def test_failed_start_terminates_only_the_owned_child(self):
        with _paths(), patch.object(rs, "router_is_ready", return_value=False), \
                patch.object(rs.process, "port_is_open", return_value=False), \
                patch.object(rs, "ROUTER_START_TIMEOUT", 0), patch.object(rs.subprocess, "Popen") as spawn, \
                patch.object(rs, "_terminate_process") as terminate:
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                rs.ensure_router()
            terminate.assert_called_once_with(spawn.return_value)


if __name__ == "__main__":
    unittest.main()
