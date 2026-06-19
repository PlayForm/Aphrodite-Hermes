"""headroom_ffi — Python ctypes adapter for headroom-ffi cdylib.

Usage:
    from headroom_ffi import HeadroomFFI
    ffi = HeadroomFFI()                        # auto-finds dylib
    result = ffi.classify("pub fn main() {}")  # {"type":"source_code",...}
    ccr    = ffi.compress(content, "code")     # {"hash":"abc...","preview":"[code:...]"}
    orig   = ffi.retrieve(ccr["hash"])         # original content
"""

import ctypes
import json
import os
import sys
from pathlib import Path


class HeadroomFFI:
    """Thin ctypes wrapper around headroom-ffi cdylib."""

    def __init__(self, dylib_path: str | None = None):
        if dylib_path is None:
            dylib_path = self._find_dylib()
        self._lib = ctypes.CDLL(str(dylib_path))

        # ── Function signatures ──────────────────────────
        # classify: (*const c_char) -> *mut c_char
        self._lib.aphrodite_classify.argtypes = [ctypes.c_char_p]
        self._lib.aphrodite_classify.restype = ctypes.c_void_p

        # compress: (*const c_char, *const c_char) -> *mut c_char
        self._lib.aphrodite_compress.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self._lib.aphrodite_compress.restype = ctypes.c_void_p

        # retrieve: (*const c_char) -> *mut c_char
        self._lib.aphrodite_retrieve.argtypes = [ctypes.c_char_p]
        self._lib.aphrodite_retrieve.restype = ctypes.c_void_p

        # version: () -> *mut c_char
        self._lib.aphrodite_version.argtypes = []
        self._lib.aphrodite_version.restype = ctypes.c_void_p

        # free_string: (*mut c_char) -> ()
        self._lib.aphrodite_free_string.argtypes = [ctypes.c_void_p]
        self._lib.aphrodite_free_string.restype = None

    # ── Public API ──────────────────────────────────────

    def classify(self, content: str) -> dict:
        """Classify content type. Returns dict with 'type', 'lines', 'bytes'."""
        return self._call1(self._lib.aphrodite_classify, content)

    def compress(self, content: str, type_hint: str = "") -> dict:
        """Compress and store in CCR. Returns dict with 'hash', 'type', 'size', 'preview'."""
        return self._call2(self._lib.aphrodite_compress, content, type_hint)

    def retrieve(self, hash_val: str) -> str:
        """Retrieve original content from CCR. Returns raw string."""
        ptr = self._lib.aphrodite_retrieve(hash_val.encode("utf-8"))
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        # May be JSON error
        if result.startswith("{"):
            err = json.loads(result)
            if "error" in err:
                raise KeyError(err["error"])
        return result

    @property
    def version(self) -> str:
        """Dylib version string."""
        ptr = self._lib.aphrodite_version()
        result = self._read_string(ptr)
        self._lib.aphrodite_free_string(ptr)
        return result

    # ── Internals ───────────────────────────────────────

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

    @staticmethod
    def _read_string(ptr) -> str:
        if ptr:
            return ctypes.cast(ptr, ctypes.c_char_p).value.decode("utf-8")
        return ""

    @staticmethod
    def _find_dylib() -> Path:
        """Find the headroom-ffi dylib in common locations."""
        candidates = [
            # Cargo build output
            Path("target/debug/libheadroom_ffi.dylib"),
            Path("target/debug/libheadroom_ffi.so"),
            Path("target/debug/headroom_ffi.dll"),
            Path("target/release/libheadroom_ffi.dylib"),
            Path("target/release/libheadroom_ffi.so"),
            Path("target/release/headroom_ffi.dll"),
            # Installed location
            Path.home() / ".hermes" / "aphrodite" / "libheadroom_ffi.dylib",
            Path.home() / ".hermes" / "aphrodite" / "libheadroom_ffi.so",
        ]
        for p in candidates:
            if p.exists():
                return p
        raise FileNotFoundError(
            "headroom-ffi dylib not found. Build with: "
            "cargo build -p headroom-ffi"
        )


# ── Quick smoke test ────────────────────────────────────

if __name__ == "__main__":
    ffi = HeadroomFFI()
    print(f"version: {ffi.version}")

    r = ffi.classify("pub fn main() {\n    println!(\"hello\");\n}\n")
    print(f"classify: {r}")

    r = ffi.compress("fn hello() -> &'static str { \"world\" }", "source_code")
    print(f"compress: {r}")

    orig = ffi.retrieve(r["hash"])
    print(f"retrieve: {repr(orig)}")
