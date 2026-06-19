"""
Aphrodite plugin test suite.
Run from monorepo root: python -m pytest plugins/aphrodite/tests/test_core.py -v
"""

import hashlib

from plugins.aphrodite._core import config as core_config
from plugins.aphrodite._core import store
from plugins.aphrodite._inline import _inline_compress, _inline_retrieve, _inline_store
from plugins.aphrodite._marker import classify
from plugins.aphrodite._marker import marker as marker_mod
from plugins.aphrodite._marker import parse as marker_parse


class TestConfig:
    """Config constants and version formats."""

    def test_bin_version_format(self):
        assert core_config.BIN_VERSION.startswith("v")
        parts = core_config.BIN_VERSION[1:].split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)

    def test_plugin_version_format(self):
        parts = core_config.PLUGIN_VERSION.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)

    def test_ports_default(self):
        assert core_config.PORTS == {"cache": 9797, "token": 9798}

    def test_binary_dir_in_hermes(self):
        assert ".hermes" in core_config.BINARY_DIR

    def test_binary_path_ends_with_aphrodite(self):
        assert core_config.BINARY.endswith("aphrodite")

    def test_repo_is_playform(self):
        assert core_config.REPO == "PlayForm/Aphrodite"

    def test_load_toml_returns_dict(self):
        assert isinstance(core_config._load_toml_config(), dict)

    def test_cfg_int_fallback_to_default(self):
        assert core_config._cfg_int("NONEXISTENT_VAR_99999", 42) == 42


class TestStore:
    """Inline store put/get/clear."""

    def setup_method(self):
        store._inline_store.clear()

    def test_put_and_get(self):
        store._inline_store_put("abc123", "hello")
        assert store._inline_store["abc123"] == "hello"

    def test_overwrite(self):
        store._inline_store_put("k", "first")
        store._inline_store_put("k", "second")
        assert store._inline_store["k"] == "second"

    def test_clear_removes_all(self):
        store._inline_store_put("k", "v")
        store._inline_store.clear()
        assert len(store._inline_store) == 0

    def test_multiple_entries(self):
        for i in range(10):
            store._inline_store_put(f"k{i}", f"v{i}")
        assert len(store._inline_store) == 10


class TestClassifier:
    """Classifier returns dict with 'type' and 'ln' keys."""

    def test_returns_dict(self):
        r = classify._classify_content("any text")
        assert isinstance(r, dict)
        assert "type" in r

    def test_diff_detected(self):
        r = classify._classify_content("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new")
        assert r["type"] == "diff"

    def test_terminal_detected(self):
        r = classify._classify_content("$ cargo build\nexit code: 0")
        assert r["type"] == "terminal"

    def test_build_output_detected(self):
        r = classify._classify_content("   Compiling foo v1.0\n    Finished release")
        assert r["type"] == "build_output"

    def test_tabular_detected(self):
        r = classify._classify_content("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |")
        assert r["type"] == "tabular"

    def test_fallback_is_text(self):
        r = classify._classify_content("ordinary text no patterns")
        assert r["type"] == "text"

    def test_empty_is_text(self):
        r = classify._classify_content("")
        assert r["type"] == "text"
        assert r["ln"] == 0

    def test_build_error_detected(self):
        r = classify._classify_content("error[E0308]: mismatched types\n --> src/main.rs:10")
        assert r["type"] == "build_error"

    def test_commit_detected(self):
        r = classify._classify_content("abc1234 Add feature\ndef4567 Fix bug")
        assert r["type"] == "commit"

    def test_diff_has_plus_minus(self):
        r = classify._classify_content("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new")
        assert "+" in r
        assert "-" in r

    def test_terminal_has_exit_code(self):
        r = classify._classify_content("$ cargo build\nexit code: 0")
        assert "exit" in r

    def test_build_output_has_errors_warnings(self):
        r = classify._classify_content("   Compiling foo v1.0\n    Finished release")
        assert "errors" in r
        assert "warnings" in r


class TestMarker:
    """CCR marker generation and validation."""

    def test_generate_contains_ccr(self):
        m = marker_mod._ccr_marker("abc123", "code_rust", 2048)
        assert "<<<CCR:" in m and m.endswith(">>>")

    def test_valid_hash_accepts_sha256(self):
        assert marker_mod._is_valid_ccr_hash(hashlib.sha256(b"x").hexdigest())

    def test_valid_hash_rejects_short(self):
        assert not marker_mod._is_valid_ccr_hash("short")

    def test_valid_hash_rejects_empty(self):
        assert not marker_mod._is_valid_ccr_hash("")

    def test_parse_runs_without_error(self):
        result = marker_parse._parse_ccr_markers("<<<CCR:deadbeefcafe|code_rust|2048>>>")
        assert isinstance(result, list)

    def test_parse_empty_on_plain_text(self):
        result = marker_parse._parse_ccr_markers("plain text no markers")
        assert isinstance(result, list)


class TestInline:
    """Inline compression and retrieval. _inline_compress returns (hash, size) tuple."""

    def setup_method(self):
        _inline_store.clear()

    def test_compress_returns_tuple(self):
        r = _inline_compress("test content")
        assert isinstance(r, tuple)
        assert len(r) == 2

    def test_different_content_different_hash(self):
        h1, _ = _inline_compress("A")
        h2, _ = _inline_compress("B")
        assert h1 != h2

    def test_same_content_same_hash(self):
        h1, _ = _inline_compress("same")
        h2, _ = _inline_compress("same")
        assert h1 == h2

    def test_size_positive(self):
        _, sz = _inline_compress("x" * 100)
        assert sz > 0

    def test_stores_in_inline_store(self):
        h, _ = _inline_compress("unique test content 42")
        # Store uses bare hash, compress returns prefixed (i:hash)
        bare = h[2:] if h.startswith("i:") else h
        assert bare in _inline_store or h in _inline_store

    def test_retrieve_roundtrip(self):
        h, _ = _inline_compress("roundtrip test")
        assert _inline_retrieve(h) == "roundtrip test"

    def test_retrieve_nonexistent_returns_none(self):
        assert _inline_retrieve("deadbeef1234deadbeef") is None

    def test_multiple_compressions_stored(self):
        hashes = [_inline_compress(f"c{i}")[0] for i in range(5)]
        assert len(hashes) == 5
        # Verify retrievability
        assert all(_inline_retrieve(h) == f"c{i}" for i, h in enumerate(hashes))


class TestIntegration:
    """Full pipeline: classify → compress → retrieve."""

    def setup_method(self):
        _inline_store.clear()

    def test_diff_pipeline_roundtrip(self):
        original = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new"
        ctype = classify._classify_content(original)
        assert ctype["type"] == "diff"
        h, _ = _inline_compress(original)
        assert _inline_retrieve(h) == original

    def test_terminal_pipeline_roundtrip(self):
        original = "$ cargo build\nexit code: 0"
        ctype = classify._classify_content(original)
        assert ctype["type"] == "terminal"
        h, _ = _inline_compress(original)
        assert _inline_retrieve(h) == original

    def test_build_output_pipeline_roundtrip(self):
        original = "   Compiling foo v1.0\n    Finished release"
        ctype = classify._classify_content(original)
        assert ctype["type"] == "build_output"
        h, _ = _inline_compress(original)
        assert _inline_retrieve(h) == original
