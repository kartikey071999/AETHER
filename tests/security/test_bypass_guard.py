"""Static guard: agent and model code must reach privileged effects only via the tool registry."""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "aether"
GUARDED_PACKAGES = ("agents", "models")
FORBIDDEN_MODULES = (
    "subprocess",
    "socket",
    "httpx",
    "requests",
    "urllib.request",
    "aiohttp",
    "asyncssh",
    "paramiko",
    "aether.tools.adapters",
    "aether.tools.registry",
    "aether.security.permissions",
    "aether.security.approvals",
)
# Privileged services agents may only reach through their injected context.
FORBIDDEN_IMPORTED_NAMES = frozenset(
    {"ToolRegistry", "PermissionService", "ApprovalService", "build_default_registry"}
)
FORBIDDEN_CALLS = {
    "os": {"system", "popen", "execv", "execve", "execvp", "startfile", "fork", "forkpty"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}
FORBIDDEN_CALL_PREFIXES = {"os": ("spawn", "exec", "posix_spawn")}


def _forbidden_module(name: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in FORBIDDEN_MODULES)


def _is_forbidden_call(module: str, name: str) -> bool:
    return name in FORBIDDEN_CALLS.get(module, ()) or any(
        name.startswith(prefix) for prefix in FORBIDDEN_CALL_PREFIXES.get(module, ())
    )


def _forbidden_uses(code: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _forbidden_module(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _forbidden_module(node.module):
                found.append(node.module)
            found += [
                f"{node.module}.{alias.name}"
                for alias in node.names
                if _is_forbidden_call(node.module, alias.name)
                or (node.module.startswith("aether") and alias.name in FORBIDDEN_IMPORTED_NAMES)
            ]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if _is_forbidden_call(node.value.id, node.attr):
                found.append(f"{node.value.id}.{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            found.append("__import__")
    return found


def test_agents_and_models_do_not_bypass_the_tool_registry() -> None:
    files = [
        path
        for package in GUARDED_PACKAGES
        if (SOURCE_ROOT / package).is_dir()
        for path in sorted((SOURCE_ROOT / package).rglob("*.py"))
    ]
    assert files, "expected agent sources to scan"
    violations = [
        f"{path.relative_to(SOURCE_ROOT)}: {name}"
        for path in files
        for name in _forbidden_uses(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def test_guard_detects_direct_privileged_calls() -> None:
    code = (
        "import os, subprocess\n"
        "import urllib.request\n"
        "from asyncio import create_subprocess_shell\n"
        "from aether.tools.adapters import SafeSubprocess\n"
        "from aether.security.permissions import Authorization\n"
        "from aether.security import PermissionService\n"
        "from aether.tools import ToolRegistry\n"
        "import httpx\n"
        "os.system('x')\n"
        "os.spawnl(0, 'x')\n"
        "__import__('socket')\n"
    )
    assert set(_forbidden_uses(code)) == {
        "subprocess",
        "urllib.request",
        "asyncio.create_subprocess_shell",
        "aether.tools.adapters",
        "aether.security.permissions",
        "aether.security.PermissionService",
        "aether.tools.ToolRegistry",
        "httpx",
        "os.system",
        "os.spawnl",
        "__import__",
    }
    assert (
        _forbidden_uses("import os\nfrom urllib.parse import urlsplit\nos.path.join('a')\n") == []
    )
