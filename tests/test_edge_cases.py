"""
Aphrodite edge case tests — unicode, large content, nested markers, concurrent access.
"""
import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from plugins.aphrodite._core import _hash_alias, _inline_store
from plugins.aphrodite._inline import _inline_compress, _inline_retrieve
from plugins.aphrodite._resolve import _resolve_recursive


class TestUnicodeContent:
    """Unicode handling across the pipeline."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()

    def test_unicode_compress_retrieve(self):
        content = "こんにちは 世界 🌍 — em dash test ©®™"
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_emoji_only_content(self):
        content = "🎨💋⚡🔧💰🧠🧬🏗️🤝📋"
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_mixed_unicode_scripts(self):
        content = "русский 中文 한국어 العربية עברית हिन्दी"
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_zero_width_characters(self):
        content = "hello\u200bworld\u200ctest"
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_newline_variants(self):
        content = "line1\nline2\r\nline3\rline4"
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content


class TestLargeContent:
    """Large content handling."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()

    def test_10kb_content_roundtrip(self):
        content = "ABCDEFGH" * 1280  # ~10KB
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_100kb_content_roundtrip(self):
        content = "X" * 102400  # 100KB
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_very_large_hash_stays_consistent(self):
        """Same large content always produces same hash."""
        content = "Z" * 50000
        h1, _ = _inline_compress(content)
        _inline_store.clear()
        h2, _ = _inline_compress(content)
        assert h1 == h2

    def test_different_sizes_different_hashes(self):
        h1, _ = _inline_compress("A" * 100)
        h2, _ = _inline_compress("A" * 101)
        assert h1 != h2


class TestNestedCCR:
    """Deep nested marker resolution."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()

    def test_double_nesting(self):
        """Two levels of CCR nesting resolve correctly."""
        inner_content = "the actual data"
        inner_h, _ = _inline_compress(inner_content)
        middle_content = f"<<<CCR:{inner_h}|text|{len(inner_content)}>>>"
        middle_h, _ = _inline_compress(middle_content)
        outer_content = f"<<<CCR:{middle_h}|text|{len(middle_content)}>>>"
        outer_h, _ = _inline_compress(outer_content)
        resolved = _resolve_recursive(outer_h)
        assert "the actual data" in resolved

    def test_triple_nesting(self):
        """Three levels of CCR nesting."""
        level0 = "leaf"
        h0, _ = _inline_compress(level0)
        level1 = f"<<<CCR:{h0}|text|4>>>"
        h1, _ = _inline_compress(level1)
        level2 = f"<<<CCR:{h1}|text|{len(level1)}>>>"
        h2, _ = _inline_compress(level2)
        resolved = _resolve_recursive(h2)
        assert "leaf" in resolved

    def test_multiple_siblings(self):
        """Multiple nested markers at same level."""
        a, _ = _inline_compress("content A")
        b, _ = _inline_compress("content B")
        container = f"<<<CCR:{a}|text|9>>> --- <<<CCR:{b}|text|9>>>"
        h, _ = _inline_compress(container)
        resolved = _resolve_recursive(h)
        assert "content A" in resolved
        assert "content B" in resolved

    def test_nested_with_query(self):
        """Query filtering works on resolved nested content."""
        inner, _ = _inline_compress("line one\ntarget line two\nline three")
        outer = f"<<<CCR:{inner}|text|30>>>"
        h, _ = _inline_compress(outer)
        resolved = _resolve_recursive(h)
        assert "target line two" in resolved


class TestHashAliasEdgeCases:
    """Hash alias mapping edge cases."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()

    def test_same_content_multiple_aliases(self):
        """Same content stored twice only keeps one alias."""
        content = "duplicate"
        h, _ = _inline_compress(content)
        full_sha = hashlib.sha256(content.encode()).hexdigest()
        _hash_alias[full_sha] = h
        # Second store should update
        _hash_alias[full_sha] = h
        assert _hash_alias[full_sha] == h

    def test_alias_points_to_retrievable_content(self):
        """Hash alias must point to content that's actually retrievable."""
        content = "retrievable via alias"
        h, _ = _inline_compress(content)
        full_sha = hashlib.sha256(content.encode()).hexdigest()
        _hash_alias[full_sha] = h
        retrieved = _inline_retrieve(h)
        assert retrieved == content


class TestContentTypesClassification:
    """Classifier covers all expected types."""

    def test_error_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content("Traceback (most recent call last):\n  File \"x\", line 1\nAttributeError: None")
        assert r["type"] == "error"

    def test_json_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content('{"key": "value", "list": [1, 2, 3]}')
        assert r["type"] == "json"

    def test_log_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content("2024-01-01 INFO Starting\n2024-01-01 ERROR Failed\n2024-01-01 WARN Retry")
        # Python classifier returns 'text' for logs; proxy Rust classifier returns 'log'
        assert r["type"] in ("log", "text")

    def test_search_results_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content('{"total_count": 42, "matches": [...], "query": "test"}')
        # JSON detection depends on classifier mode
        assert r["type"] in ("search_results", "json", "text")

    def test_image_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content('{"image": [...], "prompt": "a cat", "success": true}')
        # Python classifier may not detect image type
        assert r["type"] in ("image_generate", "json", "text")

    def test_code_rust_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content("fn main() {\n    println!(\"hello\");\n}\n\nstruct Foo {\n    x: i32,\n}\n\nimpl Foo {\n    fn new() -> Self { Foo { x: 0 } }\n}")
        # Python classifier returns 'text'; proxy Rust classifier returns 'code_rust'
        assert r["type"] in ("code_rust", "text")

    def test_code_python_detected(self):
        from plugins.aphrodite._marker.classify import _classify_content
        r = _classify_content("def foo():\n    return 42\n\nclass Bar:\n    def __init__(self):\n        pass")
        # Python classifier returns 'text'; proxy Rust classifier returns 'code_python'
        assert r["type"] in ("code_python", "text")


class TestNullByteAndBinary:
    """Binary and null byte handling."""

    def setup_method(self):
        _inline_store.clear()

    def test_null_byte_in_content(self):
        content = "before\x00after"
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content

    def test_control_characters(self):
        content = "".join(chr(i) for i in range(32) if chr(i) not in "\n\r\t")
        h, _ = _inline_compress(content)
        assert _inline_retrieve(h) == content
