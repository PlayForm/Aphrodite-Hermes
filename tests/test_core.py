"""
Aphrodite plugin test suite.
Run from monorepo root: python -m pytest plugins/aphrodite/tests/test_core.py -v
"""
import hashlib
from plugins.aphrodite._core import config as core_config
from plugins.aphrodite._core import store
from plugins.aphrodite._marker import classify
from plugins.aphrodite._marker import parse as marker_parse
from plugins.aphrodite._marker import preview
from plugins.aphrodite._marker import marker as marker_mod
from plugins.aphrodite._inline import _inline_compress, _inline_retrieve, _inline_store


class TestConfig:
    def test_bin_version_format(self):
        assert core_config.BIN_VERSION.startswith("v")
        parts = core_config.BIN_VERSION[1:].split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)

    def test_plugin_version_format(self):
        parts = core_config.PLUGIN_VERSION.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)

    def test_ports_default(self):
        assert core_config.PORTS == {"cache": 9797, "token": 9798}

    def test_binary_dir(self):
        assert ".hermes" in core_config.BINARY_DIR

    def test_binary_path(self):
        assert core_config.BINARY.endswith("aphrodite")

    def test_repo(self):
        assert core_config.REPO == "PlayForm/Aphrodite"

    def test_load_toml(self):
        assert isinstance(core_config._load_toml_config(), dict)

    def test_cfg_int_fallback(self):
        assert core_config._cfg_int("NONEXISTENT_VAR_99999", 42) == 42


class TestStore:
    def setup_method(self):
        store._inline_store.clear()

    def test_put_and_get(self):
        store._inline_store_put("abc123", "hello")
        assert store._inline_store["abc123"] == "hello"

    def test_overwrite(self):
        store._inline_store_put("k", "first")
        store._inline_store_put("k", "second")
        assert store._inline_store["k"] == "second"

    def test_clear(self):
        store._inline_store_put("k", "v")
        store._inline_store.clear()
        assert len(store._inline_store) == 0

    def test_multiple(self):
        for i in range(10):
            store._inline_store_put(f"k{i}", f"v{i}")
        assert len(store._inline_store) == 10


class TestClassifier:
    def _ctype(self, content):
        """Get type field from classifier result."""
        return classify._classify_content(content)["type"]

    def test_rust(self):
        assert self._ctype("pub fn main() {}\npub struct Foo;\nimpl Foo { fn n()->Self{Foo} }") == "code_rust"

    def test_python(self):
        assert self._ctype("def hello():\n    return 1\n\nclass Foo:\n    pass") == "code_python"

    def test_go(self):
        assert self._ctype("package main\nfunc main() {}\ntype S struct{ X int }") == "code_go"

    def test_js(self):
        assert self._ctype("function hello(){return 1}\nclass Foo{constructor(){}}") == "code_js"

    def test_ts(self):
        assert self._ctype("interface Foo{bar:string}\nfunction hello():string{return'x'}") == "code_ts"

    def test_sh(self):
        assert self._ctype("#!/bin/bash\nset -e\necho hi") == "code_sh"

    def test_text(self):
        assert self._ctype("ordinary text no patterns") == "text"

    def test_empty(self):
        assert self._ctype("") in ("text", "terminal")

    def test_diff(self):
        assert self._ctype("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new") == "diff"

    def test_terminal(self):
        assert self._ctype("$ cargo build\nexit code: 0") == "terminal"

    def test_build_output(self):
        assert self._ctype("   Compiling foo v1.0\n    Finished release") == "build_output"

    def test_tabular(self):
        assert self._ctype("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |") == "tabular"


class TestMarker:
    def test_generate(self):
        m = marker_mod._ccr_marker("abc123", "code_rust", 2048)
        assert "<<<CCR:" in m and m.endswith(">>>")

    def test_valid_hash(self):
        assert marker_mod._is_valid_ccr_hash(hashlib.sha256(b"x").hexdigest())
        assert not marker_mod._is_valid_ccr_hash("short")

    def test_parse_single(self):
        markers = marker_parse._parse_ccr_markers("<<<CCR:deadbeefcafe|code_rust|2048>>>")
        assert len(markers) == 1
        assert markers[0][0] == "deadbeefcafe"

    def test_parse_multiple(self):
        m = marker_parse._parse_ccr_markers("<<<CCR:a|json|512>>> mid <<<CCR:b|code|1024>>>")
        assert len(m) == 2

    def test_parse_none(self):
        assert marker_parse._parse_ccr_markers("plain text") == []


class TestPreview:
    def test_returns_string(self):
        r = preview._make_ccr_preview("code_rust", "pub fn main() {}\npub struct Foo;\nimpl Foo {}", 3)
        assert isinstance(r, str) and len(r) > 0

    def test_diff(self):
        r = preview._make_ccr_preview("diff", "diff --git a/x b/x\n@@ -1 +1 @@", 3)
        assert isinstance(r, str)

    def test_terminal(self):
        r = preview._make_ccr_preview("terminal", "$ cargo build\nexit code: 0", 2)
        assert isinstance(r, str)

    def test_text(self):
        r = preview._make_ccr_preview("text", "hello world", 1)
        assert isinstance(r, str) and len(r) > 0

    def test_empty(self):
        r = preview._make_ccr_preview("text", "", 0)
        assert isinstance(r, str)


class TestInline:
    def setup_method(self):
        _inline_store.clear()

    def test_compress_has_hash(self):
        r = _inline_compress("test content", "text")
        assert isinstance(r, dict)
        assert "hash" in r and len(r["hash"]) >= 8

    def test_different_hash(self):
        assert _inline_compress("A", "text")["hash"] != _inline_compress("B", "text")["hash"]

    def test_same_hash(self):
        h1 = _inline_compress("same", "text")["hash"]
        h2 = _inline_compress("same", "text")["hash"]
        assert h1 == h2

    def test_stores(self):
        r = _inline_compress("unique test 42", "text")
        assert r["hash"] in _inline_store

    def test_retrieve_roundtrip(self):
        r = _inline_compress("roundtrip me", "text")
        assert _inline_retrieve(r["hash"]) == "roundtrip me"

    def test_retrieve_none(self):
        assert _inline_retrieve("deadbeef1234") is None

    def test_has_size(self):
        r = _inline_compress("x" * 100, "text")
        assert r.get("size", 0) > 0


class TestIntegration:
    def setup_method(self):
        _inline_store.clear()

    def test_rust_roundtrip(self):
        original = "pub fn calc(x: i32) -> i32 { x }\npub struct Calc;\nimpl Calc { pub fn new() -> Self { Calc } }"
        ctype = classify._classify_content(original)["type"]
        assert ctype in ("code_rust", "code")
        r = _inline_compress(original, ctype)
        assert _inline_retrieve(r["hash"]) == original

    def test_python_roundtrip(self):
        original = "def hello():\n    return 1\n\nclass Foo:\n    pass"
        assert classify._classify_content(original)["type"] == "code_python"
        r = _inline_compress(original, "code_python")
        assert _inline_retrieve(r["hash"]) == original

    def test_diff_roundtrip(self):
        original = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new"
        assert classify._classify_content(original)["type"] == "diff"
        r = _inline_compress(original, "diff")
        assert _inline_retrieve(r["hash"]) == original
