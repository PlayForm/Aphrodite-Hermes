"""aphrodite - CCR compression plugin for Hermes Agent (Rust-powered).

All logic in libaphrodite_hermes.dylib. This file is a thin registration shim.
Architecture: __init__.py → ctypes → libaphrodite_hermes.dylib → aphrodite crate (rlib)
"""
import ctypes
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
_dylib_mtime: float = 0.0


def _load_dylib() -> ctypes.CDLL:
    """Load libaphrodite_hermes.dylib with ctypes. Hot-reloads on mtime change."""
    global _dylib, _dylib_mtime

    # Find current dylib path
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

    # Hot-reload: check mtime, reload if changed
    current_mtime = os.path.getmtime(path)
    if _dylib is not None and current_mtime == _dylib_mtime:
        return _dylib

    if _dylib is not None:
        _log.info("dylib mtime changed (%.2f → %.2f) - hot-reloading %s",
            _dylib_mtime, current_mtime, path)

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
    _dylib_mtime = current_mtime
    return dylib


def _read_str(ptr: int | None) -> str | None:
    """Read a null-terminated C string from a void pointer."""
    if ptr is None or ptr == 0:
        return None
    value = ctypes.cast(ptr, ctypes.c_char_p).value
    return value.decode("utf-8") if value else None


def _call_json(fn: Callable[..., int | None], *args: bytes) -> Any:
    """Call C function, decode JSON, free C string."""
    ptr = fn(*args)
    result = _read_str(ptr)
    if ptr:
        _load_dylib().aphrodite_hermes_free_string(ptr)
    return json.loads(result) if result else None


def _make_handler(tool_name: str) -> Callable[..., str]:
    """Create tool handler that dispatches via dylib."""
    def handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        args_json = json.dumps(args or {})
        return json.dumps(_call_json(
            _load_dylib().aphrodite_hermes_dispatch_tool,
            tool_name.encode("utf-8"),
            args_json.encode("utf-8"),
        ))
    return handler


def _start_proxy():
    """Start the aphrodite proxy binary and verify both proxies are healthy.

    Pipes stderr to a log file (not DEVNULL) so startup errors are
    diagnosable.  After launch, polls both proxy health endpoints for up
    to 5 seconds and logs a warning for each one that doesn't come up.
    """
    import time
    import urllib.request

    binary = _BINARY_PATH
    if not os.path.exists(binary):
        _log.warning("aphrodite binary not found at %s", binary)
        return
    if not os.access(binary, os.X_OK):
        os.chmod(binary, 0o755)
    env = os.environ.copy()
    env.setdefault("APHRODITE_NO_AUTO_LAUNCH", "0")

    # Write stderr to a log file so startup errors (e.g. SQLite "unable
    # to open database file") are visible.  Previously stderr was piped
    # to DEVNULL, making every startup failure silent.
    log_dir = Path.home() / ".hermes" / "aphrodite"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_log = open(log_dir / "proxy-stderr.log", "a")

    try:
        subprocess.Popen(
            [binary],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            cwd=os.getcwd(),
        )
        _log.info("aphrodite proxy started (%s)", binary)
    except Exception as e:
        _log.warning("failed to start aphrodite proxy: %s", e)
        stderr_log.close()
        return

    # ── Health check: poll both proxies for up to 5 seconds ──────
    # Read custom ports from env vars (matching the Rust dylib's
    # configured_ports() in aphrodite-hermes/src/lib.rs).  Falls back
    # to the historical 9797/9798 defaults when unset.
    _cache_port = int(os.environ.get("APHRODITE_CACHE_PORT", "9797"))
    _token_port = int(os.environ.get("APHRODITE_TOKEN_PORT", "9798"))
    proxies = [
        ("cache", _cache_port),
        ("token", _token_port),
    ]
    deadline = time.monotonic() + 5.0
    up: set[str] = set()
    while time.monotonic() < deadline:
        for name, port in proxies:
            if name in up:
                continue
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        up.add(name)
                        _log.info("aphrodite %s proxy healthy on :%d", name, port)
            except Exception:
                pass
        if len(up) == len(proxies):
            break
        time.sleep(0.5)

    for name, port in proxies:
        if name not in up:
            _log.warning(
                "aphrodite %s proxy on :%d did not become healthy within 5s "
                "- check ~/.hermes/aphrodite/proxy-stderr.log for errors",
                name,
                port,
            )


# ── Plugin registration ──
def register(ctx: Any) -> None:
    """Register hooks, tools, and (optionally) a context engine with Hermes.

    Targets the Hermes v0.17.0 PluginContext API:
      register_hook(hook_name, callback)
      register_tool(name, toolset, schema, handler, ...)
      register_skill(name, path: Path, description="")
      register_context_engine(engine)   # engine must subclass ContextEngine
    Each registration is isolated so one failure never aborts the whole plugin.
    """
    dylib = _load_dylib()
    _log.info("aphrodite-hermes dylib loaded: %s", _DYLIB_PATH)

    # Register hooks - dispatch to Rust dylib via aphrodite_hermes_call_hook
    hooks = _call_json(dylib.aphrodite_hermes_get_hooks)
    if hooks:
        def _hook_dispatch(hook_name: str, **kwargs: Any) -> Any:
            """Dispatch hook to Rust dylib and return parsed result."""
            # Hermes passes hook args as kwargs (result, output, tool_name, ...).
            # default=str keeps any non-JSON-serializable extras (e.g. message
            # objects on pre/post_llm_call) from crashing the hook.
            args_json = json.dumps(kwargs, default=str)
            return _call_json(
                dylib.aphrodite_hermes_call_hook,
                hook_name.encode("utf-8"),
                args_json.encode("utf-8"),
            )

        for hook_name in hooks:
            def _dispatch(*a: Any, name: str = hook_name, **kw: Any) -> Any:
                return _hook_dispatch(name, **kw)
            ctx.register_hook(hook_name, _dispatch)
        _log.info("registered %d hooks", len(hooks))

    # Register tools. Hermes API: register_tool(name, toolset, schema, handler).
    schemas = _call_json(dylib.aphrodite_hermes_get_schemas)
    if schemas:
        registered: list[str] = []
        for schema in schemas:
            name = schema["name"]
            try:
                ctx.register_tool(name, "aphrodite", schema, _make_handler(name))
                registered.append(name)
            except Exception as e:
                _log.warning("failed to register tool %s: %s", name, e)
        _log.info("registered %d tools: %s", len(registered), registered)

    # Register skills - from monorepo skills/ directory (Hermes wants a Path).
    _skills_dir = Path(__file__).resolve().parent.parent.parent / "skills"
    skills = _call_json(dylib.aphrodite_hermes_list_skills)
    if skills:
        count = 0
        for skill in skills:
            name = skill["name"]
            desc = skill.get("description", "")
            skill_path = _skills_dir / name / "SKILL.md"
            # Hermes skill identifiers must match [a-zA-Z0-9_-]+ (no dots), so
            # sanitize names like "aphrodite-v0.8.6-patterns" for registration
            # while still loading from the real on-disk directory.
            reg_name = "".join(c if (c.isalnum() or c in "_-") else "-" for c in name)
            if skill_path.exists():
                try:
                    ctx.register_skill(reg_name, skill_path, desc)
                    count += 1
                except Exception as e:
                    _log.warning("failed to register skill %s: %s", name, e)
        _log.info("registered %d skills from %s", count, _skills_dir)

    # Context engine is opt-in (APHRODITE_CONTEXT_ENGINE=1). Hermes expects a
    # ContextEngine subclass instance here; the per-turn catalog summary is
    # already injected via the pre_llm_call hook above, so the default path
    # needs no engine. Registering anything other than a ContextEngine instance
    # is silently rejected by Hermes, so we only attempt it when asked.
    if os.environ.get("APHRODITE_CONTEXT_ENGINE", "") == "1":
        try:
            _register_context_engine(ctx, dylib)
        except Exception as e:
            _log.warning(
                "context engine opt-in requested but not registered (%s); "
                "falling back to hooks + proxy", e,
            )

    _start_proxy()


def _register_context_engine(ctx: Any, dylib: ctypes.CDLL) -> None:
    """Best-effort context-engine registration (opt-in).

    Builds a thin ContextEngine subclass whose pre-flight summary comes from the
    dylib catalog. Raises if the host Hermes does not expose ContextEngine, so
    the caller can fall back to the hook + proxy path.
    """
    # Resolved dynamically: `agent.context_engine` only exists inside the Hermes
    # runtime, so a static import would break standalone lint/type checks.
    import importlib

    context_engine_cls = importlib.import_module("agent.context_engine").ContextEngine

    class AphroditeContextEngine(context_engine_cls):
        @property
        def name(self) -> str:
            return "aphrodite"

        def update_from_response(self, usage: dict[str, Any]) -> None:
            self.last_prompt_tokens = usage.get("prompt_tokens", 0)
            self.last_completion_tokens = usage.get("completion_tokens", 0)
            self.last_total_tokens = usage.get("total_tokens", 0)

        def should_compress(self, prompt_tokens: int | None = None) -> bool:
            # Defer to Hermes' own threshold accounting; the proxy + hooks do the
            # heavy lifting, so the engine itself never forces a compaction.
            return False

        def compress(
            self,
            messages: list[Any],
            current_tokens: int | None = None,
            focus_topic: str | None = None,
        ) -> list[Any]:
            # Non-destructive: the proxy and transform hooks already shrink tool
            # output, so the engine returns the transcript unchanged.
            return messages

    ctx.register_context_engine(AphroditeContextEngine())
