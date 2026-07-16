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


# ---------------------------------------------------------------------------
# deerflow.contracts boundary (testing-strategy.md §8)
#
# Contracts are the stable boundary between the deerflow runtime kernel and the
# DeerNexus control plane. They must depend only on the standard library and
# base types (pydantic is the allowed exception here, since it provides the
# base DTO mechanism). They must never import ORM models, FastAPI routers,
# LangGraph/LangChain, or any control-plane service. Keeping this guard in the
# boundary test means a forbidden import can never slip in via a future PR
# without turning CI red.
# ---------------------------------------------------------------------------

CONTRACTS_ROOT = HARNESS_ROOT / "contracts"

# Modules contracts may never import. ``pydantic`` is the sole allowed external
# dependency; everything else would leak a framework, persistence or
# control-plane concern into the stable boundary.
CONTRACTS_BANNED_PREFIXES = (
    "app.",
    "deerflow.agents",
    "deerflow.sandbox",
    "deerflow.mcp",
    "deerflow.subagents",
    "deerflow.models",
    "deerflow.skills",
    "deerflow.community",
    "deerflow.persistence",
    "deerflow.runtime",
    "deerflow.config",
    "fastapi",
    "langgraph",
    "langchain",
    "sqlalchemy",
    "alembic",
)

# stdlib / base modules contracts may use. The guard fails closed: anything not
# in this allow-list is a violation, so adding a dependency requires an
# explicit decision recorded against runtime-contracts.md §2.
CONTRACTS_ALLOWED_MODULES = frozenset(
    {
        "pydantic",
        "contextvars",
        "datetime",
        "enum",
        "typing",
        "typing_extensions",
        "__future__",
        "deerflow.contracts",
    }
)


def _top_level_module(module: str) -> str:
    return module.split(".", 1)[0]


def test_contracts_dir_exists():
    """The contracts package must live at ``deerflow/contracts`` (spec §2)."""
    assert CONTRACTS_ROOT.is_dir(), f"expected contracts package at {CONTRACTS_ROOT}"
    assert (CONTRACTS_ROOT / "__init__.py").is_file()


def test_contracts_only_imports_allowlisted_modules():
    """Every import in deerflow.contracts must be stdlib, pydantic, or internal.

    Uses an allow-list so the boundary fails closed: a new external dependency
    is only permitted after being explicitly reviewed and added to the allow-list.
    """
    assert CONTRACTS_ROOT.is_dir(), "contracts package missing"
    py_files = sorted(CONTRACTS_ROOT.rglob("*.py"))
    assert py_files, "contracts package has no python files"

    violations: list[str] = []
    for py_file in py_files:
        for lineno, module in _collect_imports(py_file):
            # Internal intra-contracts imports are always allowed.
            if module == "deerflow.contracts" or module.startswith("deerflow.contracts."):
                continue
            top = _top_level_module(module)
            if top in CONTRACTS_ALLOWED_MODULES:
                continue
            if module in CONTRACTS_BANNED_PREFIXES or any(module == prefix.rstrip(".") or module.startswith(prefix) for prefix in CONTRACTS_BANNED_PREFIXES):
                rel = py_file.relative_to(HARNESS_ROOT.parent.parent.parent)
                violations.append(f"  {rel}:{lineno}  imports {module}")
                continue
            # Unknown module: fail closed by reporting it for explicit review.
            rel = py_file.relative_to(HARNESS_ROOT.parent.parent.parent)
            violations.append(f"  {rel}:{lineno}  imports {module} (not in allow-list; add to CONTRACTS_ALLOWED_MODULES if intentional)")

    assert not violations, "deerflow.contracts must only depend on stdlib, pydantic and itself:\n" + "\n".join(violations)


def test_contracts_does_not_import_app_layer():
    """Contracts must never import the control plane (spec §2, ADR-0001)."""
    assert CONTRACTS_ROOT.is_dir(), "contracts package missing"
    violations: list[str] = []
    for py_file in sorted(CONTRACTS_ROOT.rglob("*.py")):
        for lineno, module in _collect_imports(py_file):
            if _is_banned(module):
                rel = py_file.relative_to(HARNESS_ROOT.parent.parent.parent)
                violations.append(f"  {rel}:{lineno}  imports {module}")

    assert not violations, "deerflow.contracts must not import the app layer:\n" + "\n".join(violations)
