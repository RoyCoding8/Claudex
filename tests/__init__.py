"""Repoint the app's data paths at one sandbox for the whole test session.

Importing this module activates that sandbox whenever the process is a test
run, so the suite cannot write to a developer's checkout. The gate that decides
this is the ``is_test_process`` check at the end of this file.
"""
from __future__ import annotations

import atexit
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import FunctionType, ModuleType

ROOT = Path(__file__).resolve().parents[1]

SANDBOX_PREFIX = "cx-test-sandbox-"
WRITABLE_PATHS = (
    "DATA_DIR",
    "POOLS_FILE",
    "SETTINGS_FILE",
    "PROXY_PID",
    "ROUTER_PID",
    "PROXY_LOG",
    "ROUTER_LOG",
    "ROUTER_BOOT_LOG",
)
TEMPLATE_PATHS = ("POOLS_EXAMPLE_FILE", "SETTINGS_EXAMPLE_FILE")
TEST_MODULES = ("unittest", "pytest")
TEST_EXECUTABLES = ("pytest", "py.test")
PYTEST_MARKER = "PYTEST_CURRENT_TEST"
CONFIG_MODULE = "modules.config"
DATA_DIR = "DATA_DIR"

SANDBOX: Path | None = None
_RELOAD = importlib.reload
_ORIGINALS: dict[str, Path] = {}
_REPLACEMENTS: dict[Path, Path] = {}


def is_test_process(argv: list[str] | None = None) -> bool:
    """argv cannot carry ``-m unittest``: runpy rewrites argv[0] to the module
    path, then to a single ``python -m unittest`` token, before any test module
    is imported.
    """
    arguments = list(sys.argv if argv is None else argv)
    if PYTEST_MARKER in os.environ:
        return True
    if any(name in sys.modules for name in TEST_MODULES):
        return True
    return any(Path(argument).name in TEST_EXECUTABLES for argument in arguments)


def sandbox_dir() -> Path:
    if SANDBOX is not None:
        return SANDBOX
    return Path(tempfile.gettempdir()) / f"{SANDBOX_PREFIX}{os.getpid()}"


def _sandbox_path(name: str) -> Path:
    original = _ORIGINALS[name]
    return SANDBOX if name == DATA_DIR else SANDBOX / original.name


def _replacement(value: object) -> object:
    if isinstance(value, Path) and value in _REPLACEMENTS:
        return _REPLACEMENTS[value]
    return value


def _rebind_defaults(function: FunctionType) -> None:
    defaults = function.__defaults__
    if defaults is not None:
        replaced = tuple(_replacement(item) for item in defaults)
        if replaced != defaults:
            function.__defaults__ = replaced
    keyword_defaults = function.__kwdefaults__
    if keyword_defaults is not None:
        for key, item in list(keyword_defaults.items()):
            replacement = _replacement(item)
            if replacement is not item:
                keyword_defaults[key] = replacement


def _rebind_attributes(owner: object, seen: set[int]) -> None:
    if id(owner) in seen:
        return
    seen.add(id(owner))
    for attribute, value in list(vars(owner).items()):
        replacement = _replacement(value)
        if replacement is not value:
            setattr(owner, attribute, replacement)
        elif isinstance(value, type):
            _rebind_attributes(value, seen)
        elif isinstance(value, FunctionType):
            _rebind_defaults(value)


def _rebind_imported_modules() -> None:
    seen: set[int] = set()
    for module in list(sys.modules.values()):
        if module is not None:
            _rebind_attributes(module, seen)


def install_guard() -> Path:
    global SANDBOX
    from modules import config

    if SANDBOX is None:
        SANDBOX = Path(tempfile.mkdtemp(prefix=SANDBOX_PREFIX))
        atexit.register(shutil.rmtree, SANDBOX, True)
        for name in WRITABLE_PATHS:
            original = getattr(config, name)
            _ORIGINALS[name] = original
            _REPLACEMENTS[original] = SANDBOX if name == DATA_DIR else SANDBOX / original.name
    for name in WRITABLE_PATHS:
        setattr(config, name, _sandbox_path(name))
    _rebind_imported_modules()
    return SANDBOX


def guard_is_active() -> bool:
    from modules import config

    return config.DATA_DIR == sandbox_dir()


def _reload_keeping_sandbox(module: ModuleType) -> ModuleType:
    was_active = guard_is_active()
    reloaded = _RELOAD(module)
    if was_active and getattr(reloaded, "__name__", "") == CONFIG_MODULE:
        install_guard()
    return reloaded


if is_test_process():
    importlib.reload = _reload_keeping_sandbox
    install_guard()
