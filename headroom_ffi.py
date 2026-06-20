"""aphrodite FFI — Python ctypes adapter for libaphrodite.dylib.

Loads the Rust cdylib from crates/aphrodite/ and hot-reloads on mtime change.
Zero-friction dev: `cargo build -p aphrodite` → next hook call picks up new dylib.

Usage:
    from headroom_ffi import get_ffi
    ffi = get_ffi()                              # auto-loads + hot-reloads
    result = ffi.classify("pub fn main() {}")      # {"type":"source_code",...}
    ccr    = ffi.compress(content, "code")         # {"hash":"abc...","preview":"[code:...]"}
    orig   = ffi.retrieve(ccr["hash"])             # original content
"""

import ctypes
import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("aphrodite.ffi")

# ── Global hot-reload state ───────────────────────────
_FFI_INSTANCE: "HeadroomFFI | None" = None


class HeadroomFFI:
    """Thin ctypes wrapper around libaphrodite.dylib with mtime hot-reload."""

    def __init__(self, dylib_path: str | None = None):
        self._dylib_path = str(dylib_path) if dylib_path else None
        self._dylib_mtime: float = 0.0
        self._lib: ctypes.CDLL | None = None
        self._handle: bytes = b"0"  # default handle
        self._load()
        # Create a stateful handle for session-scoped operations
        if self._lib:
            try:
                ptr = self._lib.aphrodite_init(b"")
                self._handle = ctypes.cast(ptr, ctypes.c_char_p).value
                self._lib.aphrodite_free_string(ptr)
            except Exception:
                pass

    # ── Public API ────────────────────────────────────

    def classify(self, content: str) -> dict:
        self._maybe_reload()
        return self._call1(self._lib.aphrodite_classify, content)

    def compress(self, content: str, type_hint: str = "") -> dict:
        self._maybe_reload()
        return self._call3(self._lib.aphrodite_compress, content, type_hint)

    def retrieve(self, hash_val: str) -> str:
        self._maybe_reload()
        ptr = self._lib.aphrodite_retrieve(self._handle, hash_val.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        if result.startswith("{"):
            err = json.loads(result)
            if "error" in err:
                raise KeyError(err["error"])
        return result

    def call_hook(self, hook: str, args: dict) -> dict:
        self._maybe_reload()
        args_json = json.dumps(args)
        ptr = self._lib.aphrodite_call_hook(
            hook.encode("utf-8"), args_json.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def session_start(self) -> dict:
        self._maybe_reload()
        ptr = self._lib.aphrodite_session_start(self._handle)
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def stats(self) -> dict:
        self._maybe_reload()
        ptr = self._lib.aphrodite_stats(self._handle)
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def catalog(self, mode: str = "full") -> dict:
        self._maybe_reload()
        ptr = self._lib.aphrodite_catalog(self._handle, mode.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def transform(self, content: str, tool_name: str) -> dict:
        self._maybe_reload()
        ptr = self._lib.aphrodite_transform(
            self._handle, content.encode("utf-8"), tool_name.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def terminal(self, content: str) -> dict:
        self._maybe_reload()
        ptr = self._lib.aphrodite_terminal(self._handle, content.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def dispatch(self, hook_name: str, args: dict) -> dict:
        """Universal hook dispatcher — routes to Rust dylib."""
        self._maybe_reload()
        args_json = json.dumps(args)
        ptr = self._lib.aphrodite_dispatch(
            self._handle, hook_name.encode("utf-8"), args_json.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def stage2(self, content: str, ccr_type: str) -> str | None:
        """Semantic reduction via Rust stage2.rs."""
        self._maybe_reload()
        ptr = self._lib.aphrodite_stage2(
            content.encode("utf-8"), ccr_type.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        if result.startswith("{"):
            return None  # error response
        return result

    def struct_extract(self, content: str, language: str = "") -> dict:
        """Code structure extraction via Rust struct_extract.rs."""
        self._maybe_reload()
        ptr = self._lib.aphrodite_struct_extract(
            content.encode("utf-8"), language.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def filter_lines(self, content: str, query: str) -> str:
        """Filter content lines by query via Rust resolve.rs."""
        self._maybe_reload()
        ptr = self._lib.aphrodite_filter_lines(
            content.encode("utf-8"), query.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return result

    def resolve(self, hash_val: str) -> dict:
        """Full recursive CCR resolution via Rust resolve.rs."""
        self._maybe_reload()
        ptr = self._lib.aphrodite_resolve(
            self._handle, hash_val.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def preview(self, content: str, ccr_type: str = "text") -> str:
        """Generate preview via Rust build_preview()."""
        self._maybe_reload()
        ptr = self._lib.aphrodite_preview(
            content.encode("utf-8"), ccr_type.encode("utf-8")
        )
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return result

    @property
    def version(self) -> str:
        self._maybe_reload()
        ptr = self._lib.aphrodite_version()
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return result

    @property
    def dylib_path(self) -> str:
        return self._dylib_path or str(self._find_dylib())

    # ── Hot-reload ────────────────────────────────────

    def _maybe_reload(self):
        """Check dylib mtime; reload if changed since last load."""
        path = self.dylib_path
        try:
            current_mtime = os.path.getmtime(path)
        except OSError:
            return  # dylib not yet built — skip

        if self._dylib_mtime > 0 and current_mtime != self._dylib_mtime:
            _log.info("dylib mtime changed — hot-reloading %s", path)
            self._load()

    def _load(self):
        """Load (or reload) the dylib."""
        path = self.dylib_path
        if not os.path.exists(path):
            _log.warning("dylib not found at %s — build with: cargo build -p aphrodite", path)
            return

        self._lib = ctypes.CDLL(str(path))
        self._dylib_mtime = os.path.getmtime(path)
        self._register_signatures()
        _log.info("loaded dylib v%s from %s", self.version, path)

    def _register_signatures(self):
        """Register all C ABI function signatures."""
        lib = self._lib

        # classify: (content) -> JSON
        lib.aphrodite_classify.argtypes = [ctypes.c_char_p]
        lib.aphrodite_classify.restype = ctypes.c_void_p

        # compress: (handle, content, hint) -> JSON
        lib.aphrodite_compress.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_compress.restype = ctypes.c_void_p

        # retrieve: (handle, hash) -> raw string
        lib.aphrodite_retrieve.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_retrieve.restype = ctypes.c_void_p

        # call_hook: (hook_name, args_json) -> JSON
        lib.aphrodite_call_hook.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_call_hook.restype = ctypes.c_void_p

        # session_start: (handle) -> JSON
        lib.aphrodite_session_start.argtypes = [ctypes.c_char_p]
        lib.aphrodite_session_start.restype = ctypes.c_void_p

        # stats: (handle) -> JSON
        lib.aphrodite_stats.argtypes = [ctypes.c_char_p]
        lib.aphrodite_stats.restype = ctypes.c_void_p

        # catalog: (handle, mode) -> JSON
        lib.aphrodite_catalog.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_catalog.restype = ctypes.c_void_p

        # transform: (handle, content, tool_name) -> JSON
        lib.aphrodite_transform.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_transform.restype = ctypes.c_void_p

        # terminal: (handle, content) -> JSON
        lib.aphrodite_terminal.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_terminal.restype = ctypes.c_void_p

        # version: () -> string
        lib.aphrodite_version.argtypes = []
        lib.aphrodite_version.restype = ctypes.c_void_p

        # free_string: (ptr) -> ()
        lib.aphrodite_free_string.argtypes = [ctypes.c_void_p]
        lib.aphrodite_free_string.restype = None

        # hooks list: () -> JSON array
        lib.aphrodite_hooks.argtypes = []
        lib.aphrodite_hooks.restype = ctypes.c_void_p

        # init: (config_path) -> handle string
        lib.aphrodite_init.argtypes = [ctypes.c_char_p]
        lib.aphrodite_init.restype = ctypes.c_void_p

        # destroy: (handle) -> void
        lib.aphrodite_destroy.argtypes = [ctypes.c_char_p]
        lib.aphrodite_destroy.restype = None

        # config_get/set
        lib.aphrodite_config_get.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_config_get.restype = ctypes.c_void_p
        lib.aphrodite_config_set.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_config_set.restype = ctypes.c_void_p

        # search
        lib.aphrodite_search.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_search.restype = ctypes.c_void_p

        # reload
        lib.aphrodite_reload.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_reload.restype = ctypes.c_void_p

        # Universal dispatch
        lib.aphrodite_dispatch.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_dispatch.restype = ctypes.c_void_p

        # Stage 2 reduction
        lib.aphrodite_stage2.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_stage2.restype = ctypes.c_void_p

        # Code structure extraction
        lib.aphrodite_struct_extract.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_struct_extract.restype = ctypes.c_void_p

        # Filter lines
        lib.aphrodite_filter_lines.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_filter_lines.restype = ctypes.c_void_p

        # Full resolve
        lib.aphrodite_resolve.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_resolve.restype = ctypes.c_void_p

        # Preview generation
        lib.aphrodite_preview.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aphrodite_preview.restype = ctypes.c_void_p

    # ── Internals ─────────────────────────────────────

    def _call1(self, fn, arg: str) -> dict:
        ptr = fn(arg.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def _call2(self, fn, a: str, b: str) -> dict:
        ptr = fn(a.encode("utf-8"), b.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    def _call3(self, fn, a: str, b: str) -> dict:
        """Stateful call: (handle, a, b) → JSON."""
        ptr = fn(self._handle, a.encode("utf-8"), b.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return json.loads(result)

    @staticmethod
    def _read_string(ptr) -> str:
        if ptr:
            return ctypes.cast(ptr, ctypes.c_char_p).value.decode("utf-8")
        return ""

    @staticmethod
    def _find_dylib() -> Path:
        """Find the aphrodite dylib — prefer newer build (dev or release)."""
        candidates = [
            # Cargo debug build (dev / cargo watch)
            Path("target/debug/libaphrodite.dylib"),
            Path("target/debug/libaphrodite.so"),
            # Cargo release build
            Path("target/release/libaphrodite.dylib"),
            Path("target/release/libaphrodite.so"),
            # Installed location
            Path.home() / ".hermes" / "aphrodite" / "libaphrodite.dylib",
            Path.home() / ".hermes" / "aphrodite" / "libaphrodite.so",
        ]
        # Find newest existing dylib
        newest = None
        newest_mtime = 0.0
        for p in candidates:
            if p.exists():
                mtime = os.path.getmtime(p)
                if mtime > newest_mtime:
                    newest = p.resolve()
                    newest_mtime = mtime
        if newest:
            return newest
        raise FileNotFoundError(
            "libaphrodite.dylib not found. Build with: cargo build -p aphrodite"
        )


# ── Singleton accessor ──────────────────────────────────

def get_ffi() -> HeadroomFFI:
    """Get or create the singleton FFI instance with hot-reload support."""
    global _FFI_INSTANCE
    if _FFI_INSTANCE is None:
        _FFI_INSTANCE = HeadroomFFI()
    return _FFI_INSTANCE


def _load_dylib():
    """Force-load the dylib (for use in __init__.py). Returns the FFI instance."""
    return get_ffi()


def _DYLIB():  # noqa: N802
    """Access the underlying ctypes CDLL (for direct C ABI calls)."""
    ffi = get_ffi()
    ffi._maybe_reload()
    return ffi._lib


# ── Quick smoke test ───────────────────────────────────

if __name__ == "__main__":
    ffi = HeadroomFFI()
    print(f"version: {ffi.version}")
    print(f"dylib:   {ffi.dylib_path}")

    r = ffi.classify('pub fn main() {\n    println!("hello");\n}\n')
    print(f"classify: {r}")

    r = ffi.compress('fn hello() -> &\'static str { "world" }', "source_code")
    print(f"compress: {r}")

    orig = ffi.retrieve(r["hash"])
    print(f"retrieve: {repr(orig)}")
