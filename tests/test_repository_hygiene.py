from __future__ import annotations

import ast
import importlib
import re
import subprocess
import unittest
from pathlib import Path

import tests

ROOT = Path(__file__).resolve().parents[1]
DATA_PREFIX = "data/"
EXAMPLE_SUFFIX = "_EXAMPLE_FILE"


def module_assignments(tree: ast.Module) -> list[tuple[list[str], ast.expr]]:
    """Module-level assignments to plain names, with each name of a tuple target
    paired to its own element of a tuple value."""
    assignments: list[tuple[list[str], ast.expr]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = []
            for target in node.targets:
                if isinstance(target, ast.Name):
                    targets.append(target)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    targets.extend(element for element in target.elts if isinstance(element, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        if node.value is None or not targets:
            continue
        elements = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List)) else [node.value]
        if len(targets) == len(elements):
            assignments.extend(
                ([target.id], element) for target, element in zip(targets, elements, strict=True)
            )
        else:
            assignments.append(([target.id for target in targets], node.value))
    return assignments


def expression_path(node: ast.expr, known: dict[str, str]) -> str | None:
    """Checkout-relative path a path expression denotes, or None when it does
    not denote one. Covers the ``/`` chain, ``.joinpath(...)``, ``.parent`` and
    a transparent ``Path(...)`` wrap, so a plainly-shaped data path cannot hide.
    """
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = expression_path(node.left, known)
        right = node.right
        if left is None or not isinstance(right, ast.Constant) or not isinstance(right.value, str):
            return None
        return f"{left}/{right.value}" if left else right.value
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = expression_path(node.value, known)
        return base.rsplit("/", 1)[0] if base else None
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name) and node.func.id == "Path" and len(node.args) == 1:
        return expression_path(node.args[0], known)
    if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
        base = expression_path(node.func.value, known)
        parts = [argument.value for argument in node.args
                 if isinstance(argument, ast.Constant) and isinstance(argument.value, str)]
        if base is None or len(parts) != len(node.args):
            return None
        return "/".join([base, *parts]) if base else "/".join(parts)
    return None


def is_filesystem_root(value: ast.expr) -> bool:
    """True for ``Path(__file__).resolve().parents[1]`` and its like, which the
    config module uses to anchor every other path at the checkout root."""
    if any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) for node in ast.walk(value)):
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "Path"
        and any(isinstance(argument, ast.Name) and argument.id == "__file__" for argument in node.args)
        for node in ast.walk(value)
    )


def path_bases(node: ast.expr) -> set[str]:
    """Names an expression uses as a path base, by ``/`` or ``.joinpath``. A
    name passed as a call argument is not a base."""
    bases: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div) and isinstance(child.left, ast.Name):
            bases.add(child.left.id)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute) and child.func.attr == "joinpath"
            and isinstance(child.func.value, ast.Name)
        ):
            bases.add(child.func.value.id)
    return bases


def configured_data_names() -> dict[str, str]:
    """Every constant in modules/config.py that names a file under the data
    directory, mapped to its path relative to the checkout root."""
    tree = ast.parse((ROOT / "modules/config.py").read_text(encoding="utf-8"))
    assignments = module_assignments(tree)
    known: dict[str, str] = {
        name: ""
        for names, value in assignments
        if len(names) == 1 and is_filesystem_root(value)
        for name in names
    }
    pending = [(names, value) for names, value in assignments if any(name not in known for name in names)]
    while pending:
        remaining: list[tuple[list[str], ast.expr]] = []
        progressed = False
        for names, value in pending:
            relative = expression_path(value, known)
            if relative is None:
                remaining.append((names, value))
                continue
            for name in names:
                known[name] = relative
            progressed = True
        if not progressed:
            break
        pending = remaining
    referenced = set().union(*(path_bases(value) for _names, value in assignments)) if assignments else set()
    directories = sorted(known[name] for name in referenced if known.get(name))
    data_directory = max(directories, key=len, default="")
    prefix = f"{data_directory}/"
    return {
        name: relative
        for name, relative in known.items()
        if relative == data_directory or relative.startswith(prefix)
    }


def template_constants() -> set[str]:
    return {name for name in configured_data_names() if name.endswith(EXAMPLE_SUFFIX)}


def writable_data_constants() -> set[str]:
    return set(configured_data_names()) - template_constants()


def data_file_names(*constants: str) -> dict[str, str]:
    """The named constants mapped to their file name, without the directory."""
    names = configured_data_names()
    return {constant: Path(names[constant]).name for constant in constants}


def modules_binding_data_paths() -> dict[str, list[str]]:
    bound: dict[str, list[str]] = {}
    data_names = set(configured_data_names())
    for source in sorted((ROOT / "modules").glob("*.py")):
        constants: set[str] = set()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level < 1 or node.module != "config":
                continue
            constants.update(alias.name for alias in node.names if alias.name in data_names)
        if constants:
            bound[source.stem] = sorted(constants)
    return bound


def scoped_functions(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Module-level functions and methods, methods qualified by their class."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            found.extend(
                (f"{node.name}.{child.name}", child)
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return found


def default_argument_paths() -> dict[str, str]:
    """Checkout-relative data paths a default argument captured at import time,
    including methods, which the sweep has to reach as well as plain functions.
    """
    known = {"ROOT": "", **configured_data_names()}
    captured: dict[str, str] = {}
    for source in sorted((ROOT / "modules").glob("*.py")):
        for qualified, node in scoped_functions(ast.parse(source.read_text(encoding="utf-8"))):
            for default in [*(node.args.defaults or ()), *(node.args.kw_defaults or ())]:
                relative = expression_path(default, known)
                if relative is not None:
                    captured[f"{source.stem}.{qualified}"] = relative
    return captured


def resolve_dotted(dotted: str) -> object | None:
    module_name, _, attribute_path = dotted.partition(".")
    try:
        holder: object | None = importlib.import_module(f"modules.{module_name}")
    except ImportError:
        return None
    for part in attribute_path.split("."):
        if holder is None:
            return None
        holder = getattr(holder, part, None)
    return holder


def router_log_backup_count() -> int:
    router_source = (ROOT / "modules/router.py").read_text(encoding="utf-8")
    match = re.search(r"RotatingFileHandler\([^)]*backupCount=(\d+)", router_source, re.DOTALL)
    return int(match.group(1)) if match else 0


def derived_artifact_names() -> dict[str, str]:
    names = data_file_names("POOLS_FILE", "SETTINGS_FILE", "PROXY_LOG", "ROUTER_LOG", "ROUTER_BOOT_LOG")
    pools_file, settings_file = names["POOLS_FILE"], names["SETTINGS_FILE"]
    proxy_log, router_log = names["PROXY_LOG"], names["ROUTER_LOG"]
    return {
        "pools file lock": f".{pools_file}.lock",
        "settings file lock": f".{settings_file}.lock",
        "proxy startup lock": Path(proxy_log).with_suffix(".lock").name,
        "router startup lock": Path(router_log).with_suffix(".lock").name,
        "router owner record": f"{router_log}.owner",
        "pools conflict copy": f"{Path(pools_file).stem}.conflict*.json",
        "settings corruption backup": f"{Path(settings_file).stem}.broken_*.json",
        "pools atomic write temp": f".{pools_file}.*.tmp",
        "settings atomic write temp": f".{settings_file}.*.tmp",
        "proxy spawn log rotation": f"{proxy_log}.1",
        "router boot log rotation": f"{names['ROUTER_BOOT_LOG']}.1",
    }


def writable_data_names() -> set[str]:
    logs = data_file_names("PROXY_LOG", "ROUTER_LOG", "ROUTER_BOOT_LOG")
    rotated = {f"{logs['ROUTER_LOG']}.{index}" for index in range(1, router_log_backup_count() + 1)}
    return (
        set(data_file_names("POOLS_FILE", "SETTINGS_FILE", "PROXY_PID", "ROUTER_PID").values())
        | set(logs.values())
        | rotated
        | set(derived_artifact_names().values())
    )


def modules_spelling_an_inventory_name() -> dict[str, list[str]]:
    known = {name for name in writable_data_names() if not any(ch in name for ch in "*?")}
    offenders: dict[str, list[str]] = {}
    for source in sorted((ROOT / "modules").glob("*.py")):
        if source.name == "config.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        found = sorted(literals & known)
        if found:
            offenders[source.stem] = found
    return offenders


def readme_lines_between(opening: str, closing: str, *, after: int = 0) -> list[str]:
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(opening, after)
    return lines[start + 1:lines.index(closing, start)]


def check_ignored(paths: list[str]) -> set[str]:
    # Bytes, not text: text=True makes win32 emit \r\n and git reads the \r as
    # part of each filename.
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"], cwd=ROOT, input=b"\n".join(p.encode() for p in paths) + b"\n",
        capture_output=True, check=False,
    )
    return {line.decode().strip('"') for line in result.stdout.splitlines() if line.strip()}


class RepositoryHygieneTests(unittest.TestCase):
    def test_sandbox_lies_outside_the_checkout_and_guards_every_writable_data_path(self) -> None:
        config = importlib.import_module("modules.config")
        sandbox = tests.sandbox_dir().resolve()
        self.assertFalse(
            sandbox.is_relative_to(ROOT),
            f"test sandbox {sandbox} sits inside the checkout at {ROOT}",
        )
        self.assertEqual(Path(config.DATA_DIR).resolve(), sandbox)
        names = configured_data_names()
        templates = template_constants()
        template_paths = {names[constant] for constant in templates}
        self.assertTrue(templates, "no seed templates were derived from modules/config.py")
        self.assertTrue(writable_data_names(), "no writable artifacts were derived from modules/config.py")
        for module_name, constants in sorted(modules_binding_data_paths().items()):
            module = importlib.import_module(f"modules.{module_name}")
            for constant in constants:
                for holder in (config, module):
                    bound = getattr(holder, constant, None)
                    if bound is None:
                        continue
                    label = f"{holder.__name__}.{constant} = {bound}"
                    with self.subTest(path=label):
                        if names[constant] in template_paths:
                            self.assertEqual(
                                Path(bound).resolve(),
                                (ROOT / names[constant]).resolve(),
                                f"{label} is a read-only seed template and must stay in the checkout",
                            )
                        else:
                            self.assertTrue(
                                Path(bound).resolve().is_relative_to(sandbox),
                                f"{label} escapes the sandbox {sandbox}",
                            )
        for dotted, relative in sorted(default_argument_paths().items()):
            if relative in template_paths:
                continue
            function = resolve_dotted(dotted)
            captured: tuple[object, ...] = ()
            if isinstance(function, type(lambda: None)):
                captured = (*(function.__defaults__ or ()), *(function.__kwdefaults__ or {}).values())
            for value in captured:
                with self.subTest(default_argument=dotted, path=relative):
                    self.assertNotEqual(
                        Path(value).resolve() if isinstance(value, Path) else value,
                        (ROOT / relative).resolve(),
                        f"{dotted} captured {value} at import time, outside the sandbox",
                    )

    def test_guard_activates_and_declares_every_configured_data_path(self) -> None:
        self.assertTrue(tests.guard_is_active(), "the sandbox guard did not activate for this test run")
        self.assertEqual(set(tests.WRITABLE_PATHS), writable_data_constants())
        self.assertEqual(set(tests.TEMPLATE_PATHS), template_constants())

    def test_sandbox_survives_a_config_reload(self) -> None:
        config = importlib.import_module("modules.config")
        importlib.reload(config)
        sandbox = tests.sandbox_dir().resolve()
        self.assertEqual(Path(config.DATA_DIR).resolve(), sandbox)
        for name in tests.WRITABLE_PATHS:
            with self.subTest(name=name):
                self.assertTrue(
                    Path(getattr(config, name)).resolve().is_relative_to(sandbox),
                    f"modules.config.{name} escapes the sandbox after a reload",
                )

    def test_every_runtime_data_artifact_is_git_ignored(self) -> None:
        if subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
            capture_output=True, text=True, check=False,
        ).stdout.strip() != "true":
            self.skipTest(f"{ROOT} is not a git working tree, so ignore rules have no effect to check")
        artifacts = {f"{DATA_PREFIX}{name}" for name in writable_data_names()}
        unignored = artifacts - check_ignored(sorted(artifacts))
        self.assertEqual(unignored, set(), f"runtime artifacts git would commit: {sorted(unignored)}")

    def test_no_module_spells_a_data_file_name_that_config_owns(self) -> None:
        self.assertEqual(
            modules_spelling_an_inventory_name(), {},
            "these modules spell a runtime data file name as a literal, so the name is invisible to "
            "the sandbox, the README inventory and the ignore rules, all of which are derived from "
            "the constants in modules/config.py",
        )

    def test_readme_test_inventory_matches_test_files(self) -> None:
        documented = {
            line.split()[0]
            for line in readme_lines_between("tests/", "```")
            if line.strip().startswith("test_")
        }
        self.assertEqual(documented, {path.name for path in (ROOT / "tests").glob("test_*.py")})

    def test_readme_environment_inventory_matches_env_example(self) -> None:
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        template_names = set(re.findall(r"^# (CX_[A-Z0-9_]+)=", template, re.MULTILINE))
        configuration = (ROOT / "modules/config.py").read_text(encoding="utf-8")
        runtime_names = set(re.findall(r"\b(CX_[A-Z0-9_]+)\b", configuration))
        environment_section = "\n".join(readme_lines_between("## Environment overrides", "## Files", after=1))
        documented_names = set(re.findall(r"`(CX_[A-Z0-9_]+)`", environment_section))
        self.assertEqual(documented_names, template_names | (runtime_names - template_names))

    def test_readme_data_inventory_matches_runtime_files(self) -> None:
        names = data_file_names(
            "POOLS_FILE", "SETTINGS_FILE", "POOLS_EXAMPLE_FILE", "SETTINGS_EXAMPLE_FILE",
            "PROXY_PID", "ROUTER_PID", "PROXY_LOG", "ROUTER_LOG", "ROUTER_BOOT_LOG",
        )
        expected = set(names.values())
        expected.update(derived_artifact_names().values())
        expected.update(f"{names['ROUTER_LOG']}.{index}" for index in range(1, router_log_backup_count() + 1))
        documented = {line.split()[0] for line in readme_lines_between("data/", "tests/") if line.strip()}
        self.assertEqual(documented, expected)


if __name__ == "__main__":
    unittest.main()
