"""aphrodite — CCR compression plugin for Hermes Agent (Rust-powered).

All logic in libaphrodite_hermes.dylib. This file is a thin registration shim.
Architecture: __init__.py → ctypes → libaphrodite_hermes.dylib → aphrodite crate (rlib)
"""
import ctypes
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("aphrodite")

# ── Dylib path resolution ──
_PLUGIN_DIR = Path(__file__).resolve().parent
_DYLIB_NAME = "libaphrodite_hermes.dylib" if sys.platform == "darwin" else \
              "libaphrodite_hermes.so" if sys.platform == "linux" else "aphrodite_hermes.dll"
_DYLIB_PATH = os.environ.get("APHRODITE_HERMES_DYLIB_PATH",
    str(_PLUGIN_DIR / "binaries" / _DYLIB_NAME))
_BINARY_NAME = "aphrodite.exe" if sys.platform == "win32" else "aphrodite"
_BINARY_PATH = os.environ.get("APHRODITE_BINARY_PATH",
    str(_PLUGIN_DIR / "binaries" / _BINARY_NAME))

_dylib: ctypes.CDLL | None = None


def _load_dylib() -> ctypes.CDLL:
    """Load libaphrodite_hermes.dylib with ctypes. Uses c_void_p for Python 3.14 compat."""
    global _dylib
    if _dylib is not None:
        return _dylib

    path = _DYLIB_PATH
    candidates = [
        path,
        str(_PLUGIN_DIR / "binaries" / _DYLIB_NAME),
        str(_PLUGIN_DIR.parent / "binaries" / _DYLIB_NAME),
    ]
    if sys.platform == "darwin":
        candidates.append(
            str(Path(__file__).resolve().parents[3] / "target" / "release" / _DYLIB_NAME)
        )
    for p in candidates:
        if os.path.exists(p):
            path = p
            break
    assert os.path.exists(path), f"Dylib not found. Tried: {candidates}"

    dylib = ctypes.CDLL(path)

    # c_void_p avoids Python 3.14 c_char_p malloc mismatch → SIGABRT
    dylib.aphrodite_hermes_get_schemas.restype = ctypes.c_void_p
    dylib.aphrodite_hermes_get_hooks.restype = ctypes.c_void_p
    dylib.aphrodite_hermes_list_skills.restype = ctypes.c_void_p
    dylib.aphrodite_hermes_dispatch_tool.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    dylib.aphrodite_hermes_dispatch_tool.restype = ctypes.c_void_p
    dylib.aphrodite_hermes_call_hook.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    dylib.aphrodite_hermes_call_hook.restype = ctypes.c_void_p
    dylib.aphrodite_hermes_proxy_health.restype = ctypes.c_void_p
    dylib.aphrodite_hermes_free_string.argtypes = [ctypes.c_void_p]

    _dylib = dylib
    return dylib


def _read_str(ptr: int) -> str | None:
    """Read a null-terminated C string from a void pointer."""
    if ptr is None or ptr == 0:
        return None
    return ctypes.cast(ptr, ctypes.c_char_p).value.decode("utf-8")


def _call_json(fn, *args):
    """Call C function, decode JSON, free C string."""
    ptr = fn(*args)
    result = _read_str(ptr)
    if ptr:
        _load_dylib().aphrodite_hermes_free_string(ptr)
    return json.loads(result) if result else None


def _make_handler(tool_name: str):
    """Create tool handler that dispatches via dylib."""
    def handler(args=None, **kwargs):
        args_json = json.dumps(args or {})
        return json.dumps(_call_json(
            _load_dylib().aphrodite_hermes_dispatch_tool,
            tool_name.encode("utf-8"),
            args_json.encode("utf-8"),
        ))
    return handler


def _start_proxy():
    """Start the aphrodite proxy binary."""
    binary = _BINARY_PATH
    if not os.path.exists(binary):
        _log.warning("aphrodite binary not found at %s", binary)
        return
    if not os.access(binary, os.X_OK):
        os.chmod(binary, 0o755)
    env = os.environ.copy()
    env.setdefault("APHRODITE_NO_AUTO_LAUNCH", "0")
    try:
        subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.getcwd())
        _log.info("aphrodite proxy started (%s)", binary)
    except Exception as e:
        _log.warning("failed to start aphrodite proxy: %s", e)


def _proxy_health():
    """Probe proxy health and format for stats display."""
    try:
        return _call_json(_load_dylib().aphrodite_hermes_proxy_health)
    except Exception:
        return {}


# ── Plugin registration ──
def register(ctx):
    """Register hooks, tools, and context engine with Hermes."""
    dylib = _load_dylib()
    _log.info("aphrodite-hermes dylib loaded: %s", _DYLIB_PATH)

    # Register hooks — dispatch to Rust dylib via aphrodite_hermes_call_hook
    hooks = _call_json(dylib.aphrodite_hermes_get_hooks)
    if hooks:
        def _hook_dispatch(hook_name, **kwargs):
            """Dispatch hook to Rust dylib and return parsed result."""
            # Hermes passes hook args as kwargs (content, tool_name, etc.)
            args_json = json.dumps(kwargs)
            return _call_json(
                dylib.aphrodite_hermes_call_hook,
                hook_name.encode("utf-8"),
                args_json.encode("utf-8"),
            )

        for hook_name in hooks:
            ctx.register_hook(
                hook_name,
                lambda *a, name=hook_name, **kw: _hook_dispatch(name, **kw),
            )
        _log.info("registered %d hooks", len(hooks))

    # Register tools
    schemas = _call_json(dylib.aphrodite_hermes_get_schemas)
    if schemas:
        for schema in schemas:
            name = schema["name"]
            ctx.register_tool(schema, _make_handler(name))
        _log.info("registered %d tools: %s", len(schemas), [s["name"] for s in schemas])

    # Register skills — from monorepo skills/ directory
    _skills_dir = Path(__file__).resolve().parent.parent.parent / "skills"
    skills = _call_json(dylib.aphrodite_hermes_list_skills)
    if skills:
        for skill in skills:
            name = skill["name"]
            desc = skill.get("description", "")
            skill_path = _skills_dir / name / "SKILL.md"
            if skill_path.exists():
                ctx.register_skill(name, str(skill_path), desc)
        _log.info("registered %d skills from %s", len(skills), _skills_dir)

    # Register context engine
    ctx.register_context_engine(
        name="aphrodite",
        pre_llm_call=lambda ctx, **kw: _call_json(
            dylib.aphrodite_hermes_dispatch_tool, b"context_engine_pre_llm", b"{}"
        ),
    )

    _start_proxy()
