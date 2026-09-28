"""Config problems are collected and reported, never raised at import."""
from __future__ import annotations

import importlib
import ipaddress
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import dotenv

from modules.urls import is_local_host

_SNAPSHOT_KEYS = (
    "ROUTER_PORT", "ROUTER_POOL_PASSES", "ROUTER_COOLDOWN_429", "ROUTER_START_TIMEOUT", "DEFAULT_COMPACT_WINDOW",
    "PROXY_EXE", "PROXY_CONFIG", "PROXY_HOST", "PROXY_API_KEY", "ROUTER_HOST", "ROUTER_API_KEY", "CONFIG_ERRORS",
)


@contextmanager
def _isolated_config(file_values: dict[str, str] | None = None, **process_values: str):
    import modules.config as config
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / ".env"
        env_file.write_text(
            "\n".join(f"{key}={value}" for key, value in (file_values or {}).items()) + "\n",
            encoding="utf-8",
        )
        file_snapshot = dotenv.dotenv_values(env_file)
        real_is_file = Path.is_file

        def isolated_is_file(path: Path) -> bool:
            return path == config.ROOT / ".env" or real_is_file(path)

        try:
            with patch.dict(os.environ, process_values, clear=True), \
                 patch.object(dotenv, "dotenv_values", return_value=file_snapshot), \
                 patch.object(Path, "is_file", isolated_is_file):
                importlib.reload(config)
                yield {key: getattr(config, key) for key in _SNAPSHOT_KEYS}
        finally:
            importlib.reload(config)


class ConfigErrorTests(unittest.TestCase):
    def test_negative_port_is_reported(self):
        with _isolated_config(CX_ROUTER_PORT="-5") as snapshot:
            self.assertTrue(any("CX_ROUTER_PORT" in e for e in snapshot["CONFIG_ERRORS"]))
            self.assertEqual(snapshot["ROUTER_PORT"], 4000)

    def test_bracketed_ipv6_hosts_normalize_to_raw_socket_values(self):
        with _isolated_config(
            CX_ROUTER_HOST="[2001:db8::1]",
            CX_ROUTER_API_KEY="configured",
            CX_CLIPROXY_HOST="[::1]",
        ) as snapshot:
            self.assertEqual(snapshot["ROUTER_HOST"], "2001:db8::1")
            self.assertEqual(snapshot["PROXY_HOST"], "::1")
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_invalid_router_hosts_are_rejected_at_config_boundary(self):
        invalid_hosts = (
            "user@router.test",
            "router.test/path",
            "router.test?query",
            "router.test#fragment",
            "router.test:5000",
            "127.0.0.1:5000",
            "router.test ",
            "\trouter.test",
            "bad..example",
            "-router.example",
            "router-.example",
            "router_test",
            "x" * 64 + ".example",
            ".".join(("a" * 63,) * 4 + ("example",)),
            "01.2.3.4",
            "1.2.3",
            "1.2.3.4.",
            "0x7f000001",
            "0x7f.0.0.1",
            "0X7F.0X0.0X1",
            "0177.0.0.1",
            "127.1",
            "0300.0250.1",
            "2130706433",
            "[::1",
            "::1]",
            "[router.test]",
            "[::1]:5000",
            "[fe80::1%eth0]",
            "fe80::1%eth0",
            "faß.de",
            "例え.jp",
        )
        for host in invalid_hosts:
            with self.subTest(host=host), _isolated_config(CX_ROUTER_HOST=host) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], "127.0.0.1")
                self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
                error = snapshot["CONFIG_ERRORS"][0]
                self.assertIn("CX_ROUTER_HOST from process environment", error)
                self.assertEqual(error.source.value, "process")

    def test_invalid_proxy_host_is_rejected_at_config_boundary(self):
        with _isolated_config(CX_CLIPROXY_HOST="proxy.test/path") as snapshot:
            self.assertEqual(snapshot["PROXY_HOST"], "127.0.0.1")
            self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
            self.assertIn("CX_CLIPROXY_HOST from process environment", snapshot["CONFIG_ERRORS"][0])

    def test_file_host_whitespace_and_control_bytes_are_refused_without_being_trimmed(self):
        for raw in ('" router.test "', '"router.\x01test"'):
            with self.subTest(raw=raw), _isolated_config({"CX_ROUTER_HOST": raw}) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], "127.0.0.1")
                self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
                self.assertEqual(snapshot["CONFIG_ERRORS"][0].source.value, "file")

    def test_host_diagnostic_names_the_supplied_alias(self):
        with _isolated_config({"CX_LITELLM_HOST": "router.test/path"}) as snapshot:
            self.assertEqual(snapshot["ROUTER_HOST"], "127.0.0.1")
            self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
            self.assertIn("CX_LITELLM_HOST from file", snapshot["CONFIG_ERRORS"][0])

    def test_raw_ipv6_and_dns_hosts_remain_valid(self):
        cases = (
            ("::1", "::1"),
            ("2001:db8::1", "2001:db8::1"),
            ("router.test", "router.test"),
            ("router-1.example", "router-1.example"),
            ("a" * 63 + ".example", "a" * 63 + ".example"),
            (".".join(("a" * 63,) * 3 + ("example",)), ".".join(("a" * 63,) * 3 + ("example",))),
            ("localhost.", "localhost."),
            ("xn--fa-hia.de", "xn--fa-hia.de"),
            ("xn--fa-hia.de.", "xn--fa-hia.de."),
            ("1.1.1.1.sslip.io", "1.1.1.1.sslip.io"),
        )
        for host, expected in cases:
            with self.subTest(host=host), _isolated_config(
                CX_ROUTER_HOST=host,
                CX_ROUTER_API_KEY="configured",
            ) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], expected)
                self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_numeric_ipv4_disguises_name_the_reason_they_are_refused(self):
        for host in ("0x7f000001", "0x7f.0.0.1", "0177.0.0.1", "127.1", "0300.0250.1", "2130706433"):
            with self.subTest(host=host), _isolated_config(CX_ROUTER_HOST=host) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], "127.0.0.1")
                self.assertIn("numeric", snapshot["CONFIG_ERRORS"][0])

    def test_non_ascii_hosts_are_refused_instead_of_becoming_another_domain(self):
        for host, punycoded in (
            ("faß.de", "fass.de"),
            ("ẞ.de", "ss.de"),
            ("例え.jp", "xn--r8jz45g.jp"),
            ("🏠.example", None),
        ):
            with self.subTest(host=host), _isolated_config(CX_ROUTER_HOST=host) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], "127.0.0.1")
                self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
                self.assertIn("punycode", snapshot["CONFIG_ERRORS"][0])
            if punycoded is None:
                continue
            with self.subTest(host=punycoded), _isolated_config(
                CX_ROUTER_HOST=punycoded,
                CX_ROUTER_API_KEY="configured",
            ) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], punycoded)
                self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_dns_names_including_numeric_labels_are_accepted_and_never_treated_as_local(self):
        for host in (
            "nas.7", "printer.10", "a1.2", "example.123", "host.0",
            "999.1.1.1", "1.2.3.4.5", "256.1.1.1",
        ):
            with self.subTest(host=host), _isolated_config(
                CX_ROUTER_HOST=host,
                CX_ROUTER_API_KEY="configured",
            ) as snapshot:
                self.assertEqual(snapshot["ROUTER_HOST"], host)
                self.assertEqual(snapshot["CONFIG_ERRORS"], [])
                self.assertFalse(is_local_host(host))

    def test_ipv4_mapped_loopback_is_local_for_the_router_key_rule(self):
        with _isolated_config(
            CX_ROUTER_HOST="::ffff:127.0.0.1",
            CX_CLIPROXY_HOST="::ffff:127.0.0.1",
        ) as snapshot:
            self.assertEqual(ipaddress.ip_address(snapshot["ROUTER_HOST"]).ipv4_mapped, ipaddress.IPv4Address("127.0.0.1"))
            self.assertEqual(ipaddress.ip_address(snapshot["PROXY_HOST"]).ipv4_mapped, ipaddress.IPv4Address("127.0.0.1"))
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_out_of_bounds_values_are_collected(self):
        with _isolated_config(CX_ROUTER_POOL_PASSES="999", CX_ROUTER_COOLDOWN_429="99999") as snapshot:
            self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 2)
            self.assertEqual(snapshot["ROUTER_POOL_PASSES"], 2)
            self.assertEqual(snapshot["ROUTER_COOLDOWN_429"], 60.0)

    def test_non_finite_float_is_rejected(self):
        with _isolated_config(CX_ROUTER_START_TIMEOUT="nan", CX_DEFAULT_COMPACT_WINDOW="inf") as snapshot:
            self.assertEqual(snapshot["ROUTER_START_TIMEOUT"], 35.0)
            self.assertEqual(snapshot["DEFAULT_COMPACT_WINDOW"], 170000)
            self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 2)
            self.assertTrue(all("from process environment" in error for error in snapshot["CONFIG_ERRORS"]))

    def test_compact_window_must_be_positive(self):
        with _isolated_config(CX_DEFAULT_COMPACT_WINDOW="0") as snapshot:
            self.assertEqual(snapshot["DEFAULT_COMPACT_WINDOW"], 170000)
            self.assertTrue(any("at least 1" in error for error in snapshot["CONFIG_ERRORS"]))

    def test_relative_cli_paths_resolve_from_project_root(self):
        with _isolated_config(CX_CLIPROXY_EXE="bin/cli", CX_CLIPROXY_CONFIG="etc/config.yaml") as snapshot:
            import modules.config as config
            self.assertEqual(snapshot["PROXY_EXE"], config.ROOT / "bin/cli")
            self.assertEqual(snapshot["PROXY_CONFIG"], config.ROOT / "etc/config.yaml")

    def test_non_local_router_requires_explicit_key(self):
        with _isolated_config(CX_ROUTER_HOST="192.0.2.10") as snapshot:
            self.assertTrue(any("ROUTER_API_KEY" in error for error in snapshot["CONFIG_ERRORS"]))
        with _isolated_config(CX_ROUTER_HOST="192.0.2.10", CX_ROUTER_API_KEY="configured") as snapshot:
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_explicit_empty_process_value_uses_default_over_file_alias(self):
        with _isolated_config(
            {"CX_LITELLM_HOST": "0.0.0.0"},
            CX_ROUTER_HOST="",
        ) as snapshot:
            self.assertEqual(snapshot["ROUTER_HOST"], "127.0.0.1")
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_nonempty_file_alias_is_used_without_process_primary(self):
        with _isolated_config(
            {"CX_LITELLM_HOST": "192.0.2.10", "CX_LITELLM_API_KEY": "file-key"},
        ) as snapshot:
            self.assertEqual(snapshot["ROUTER_HOST"], "192.0.2.10")
            self.assertEqual(snapshot["ROUTER_API_KEY"], "file-key")
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_equal_process_and_file_values_keep_process_source(self):
        with _isolated_config(
            {"CX_ROUTER_PORT": "-5"},
            CX_ROUTER_PORT="-5",
        ) as snapshot:
            self.assertEqual(snapshot["ROUTER_PORT"], 4000)
            self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
            self.assertEqual(snapshot["CONFIG_ERRORS"][0].source.value, "process")
            self.assertIn("from process environment", snapshot["CONFIG_ERRORS"][0])

    def test_process_legacy_alias_wins_over_file_primary(self):
        with _isolated_config(
            {"CX_ROUTER_HOST": "0.0.0.0", "CX_ROUTER_API_KEY": "file-key"},
            CX_LITELLM_HOST="192.0.2.10",
            CX_LITELLM_API_KEY="process-key",
        ) as snapshot:
            self.assertEqual(snapshot["ROUTER_HOST"], "192.0.2.10")
            self.assertEqual(snapshot["ROUTER_API_KEY"], "process-key")
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_non_local_proxy_rejects_default_dummy_key(self):
        with _isolated_config(CX_CLIPROXY_HOST="192.0.2.10") as snapshot:
            self.assertEqual(snapshot["PROXY_API_KEY"], "sk-dummy")
            self.assertIn(
                "PROXY_API_KEY from default must be changed when PROXY_HOST is not local.",
                snapshot["CONFIG_ERRORS"],
            )
        with _isolated_config(CX_CLIPROXY_HOST="192.0.2.10", CX_CLIPROXY_API_KEY="configured") as snapshot:
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_wildcard_bind_hosts_need_no_key_under_the_single_locality_rule(self):
        for host in ("0.0.0.0", "::"):
            with self.subTest(host=host), _isolated_config(
                CX_CLIPROXY_HOST=host, CX_ROUTER_HOST=host
            ) as snapshot:
                self.assertEqual(snapshot["PROXY_HOST"], host)
                self.assertEqual(snapshot["ROUTER_HOST"], host)
                self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_valid_env_still_applies(self):
        with _isolated_config(CX_ROUTER_PORT="5050") as snapshot:
            self.assertEqual(snapshot["ROUTER_PORT"], 5050)
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])

    def test_number_diagnostics_name_the_alias_the_user_wrote(self):
        for raw in ("notanumber", "99999", "nan"):
            for alias in ("CX_LITELLM_PORT", "CX_ROUTER_PORT"):
                with self.subTest(alias=alias, raw=raw), _isolated_config(
                    {alias: raw}
                ) as snapshot:
                    self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 1)
                    self.assertIn(f"{alias} from file", snapshot["CONFIG_ERRORS"][0])
                    self.assertNotIn(
                        "CX_ROUTER_PORT" if alias == "CX_LITELLM_PORT" else "CX_LITELLM_PORT",
                        snapshot["CONFIG_ERRORS"][0],
                    )
        with _isolated_config(CX_LITELLM_PORT="99999") as snapshot:
            self.assertIn("CX_LITELLM_PORT from process environment", snapshot["CONFIG_ERRORS"][0])

    def test_a_file_path_configures_the_proxy_through_the_same_authority_as_every_other_setting(self):
        # "/opt/..." is rooted but driveless on Windows, where is_absolute() is
        # False, so ROOT is correctly prepended.
        anchor = Path.cwd().anchor or "/"
        from_dotenv = Path(anchor) / "opt" / "from-dotenv" / "cli-proxy-api"
        from_process = Path(anchor) / "opt" / "from-process"
        with _isolated_config(
            {"CX_CLIPROXY_EXE": str(from_dotenv), "CX_CLIPROXY_CONFIG": "etc/proxy.yaml"}
        ) as snapshot:
            import modules.config as config
            self.assertEqual(snapshot["PROXY_EXE"], from_dotenv)
            self.assertEqual(snapshot["PROXY_CONFIG"], config.ROOT / "etc/proxy.yaml")
            self.assertEqual(snapshot["CONFIG_ERRORS"], [])
        with _isolated_config(CX_CLIPROXY_EXE=str(from_process)) as snapshot:
            self.assertEqual(snapshot["PROXY_EXE"], from_process)


if __name__ == "__main__":
    unittest.main()
