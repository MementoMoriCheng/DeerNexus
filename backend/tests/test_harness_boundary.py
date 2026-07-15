"""Boundary check: harness layer must not import from app layer.

The deerflow-harness package (packages/harness/deerflow/) is a standalone,
publishable agent framework. It must never depend on the app layer (app/).

This test scans all Python files in the harness package and fails if any
static or literal dynamic import of the app layer is found.
"""

import ast
from pathlib import Path

import pytest

HARNESS_ROOT = Path(__file__).parent.parent / "packages" / "harness" / "deerflow"

BANNED_PREFIXES = ("app.",)


def _collect_imports(filepath: Path) -> list[tuple[int, str]]:
    """Return (line_number, module_path) for static and literal dynamic imports."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    importlib_aliases = {"importlib"}
    import_module_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.append((node.lineno, alias.name))
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                results.append((node.lineno, node.module))
                if node.module == "importlib":
                    import_module_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue

        module_arg = node.args[0]
        if not isinstance(module_arg, ast.Constant) or not isinstance(module_arg.value, str):
            continue

        function = node.func
        is_dynamic_import = (isinstance(function, ast.Name) and (function.id == "__import__" or function.id in import_module_aliases)) or (
            isinstance(function, ast.Attribute) and function.attr == "import_module" and isinstance(function.value, ast.Name) and function.value.id in importlib_aliases
        )
        if is_dynamic_import:
            results.append((node.lineno, module_arg.value))

    return results


def _is_banned(module: str) -> bool:
    return any(module == prefix.rstrip(".") or module.startswith(prefix) for prefix in BANNED_PREFIXES)


def test_harness_does_not_import_app():
    violations: list[str] = []

    for py_file in sorted(HARNESS_ROOT.rglob("*.py")):
        for lineno, module in _collect_imports(py_file):
            if _is_banned(module):
                rel = py_file.relative_to(HARNESS_ROOT.parent.parent.parent)
                violations.append(f"  {rel}:{lineno}  imports {module}")

    assert not violations, "Harness layer must not import from app layer:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import app.gateway\n", "app.gateway"),
        ("from app.control_plane import tenant\n", "app.control_plane"),
        ('import importlib\nimportlib.import_module("app.gateway.app")\n', "app.gateway.app"),
        ('import importlib as loader\nloader.import_module("app.channels")\n', "app.channels"),
        ('from importlib import import_module as load\nload("app.control_plane.audit")\n', "app.control_plane.audit"),
        ('__import__("app.gateway.routers")\n', "app.gateway.routers"),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import app.gateway\n", "app.gateway"),
    ],
)
def test_collect_imports_detects_app_imports(tmp_path: Path, source: str, module: str):
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")

    imports = _collect_imports(sample)

    assert any(imported == module and _is_banned(imported) for _, imported in imports)


@pytest.mark.parametrize(
    "source",
    [
        "from deerflow.config.app_config import AppConfig\n",
        '"""See app.gateway.routers for the adapter implementation."""\n',
        'import importlib\nmodule_name = "app.gateway"\nimportlib.import_module(module_name)\n',
    ],
)
def test_collect_imports_ignores_non_import_mentions(tmp_path: Path, source: str):
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")

    imports = _collect_imports(sample)

    assert not any(_is_banned(module) for _, module in imports)
