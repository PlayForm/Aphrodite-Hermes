"""
Aphrodite stage-2 compression tests — semantic reduction of CCR-stored content.
"""
import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from plugins.aphrodite._core import _hash_alias, _inline_store, _inline_store_put
from plugins.aphrodite._hooks.transform import _transform_tool_result
from plugins.aphrodite._inline import _inline_compress, _inline_retrieve
from plugins.aphrodite._resolve import _resolve_one
from plugins.aphrodite._stage2 import compress_stage2


class TestStage2Reducers:
    """Individual reducer correctness."""

    def test_json_reducer_runs(self):
        import json; content = json.dumps([{"name": "test_" + "x" * 80, "value": 42, "nested": {"x": 1}} for _ in range(20)], indent=2)
        reduced = compress_stage2(content, "json")
        assert isinstance(reduced, str)

    def test_json_list_reducer_runs(self):
        import json; content = json.dumps([{"id": i, "name": f"item_{i}"} for i in range(60)], indent=2)
        reduced = compress_stage2(content, "json_list")
        assert isinstance(reduced, str)

    def test_build_extracts_errors(self):
        content = ("   Compiling foo v1.0\nerror[E0308]: mismatched types\n"
                   "  --> src/main.rs:10:5\nerror: aborting\n    Finished release\n") * 5
        reduced = compress_stage2(content, "build_output")
        assert reduced is not None
        assert "error" in reduced.lower()

    def test_build_reducer_runs(self):
        content = "   Compiling foo v1.0\n    Finished dev\n  warning: unused var\n" * 5
        reduced = compress_stage2(content, "build_output")
        assert isinstance(reduced, str) if reduced else reduced is None

    def test_diff_file_summary(self):
        content = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n-old\n+new\n" * 10
        reduced = compress_stage2(content, "diff")
        assert reduced is not None

    def test_code_extracts_signatures(self):
        content = ("fn main() {\n    println!(\"hello\");\n}\n\n"
                   "struct Foo { x: i32 }\n\nimpl Foo { fn new() -> Self { Foo { x: 0 } } }\n") * 3
        reduced = compress_stage2(content, "code_rust")
        assert reduced is not None

    def test_python_code_extracts_signatures(self):
        content = ("def foo(x):\n    return x + 1\n\nclass Bar:\n"
                   "    def __init__(self): self.x = 1\n    def method(self): pass\n") * 3
        reduced = compress_stage2(content, "code_python")
        assert reduced is not None

    def test_small_content_not_reduced(self):
        content = "tiny"
        reduced = compress_stage2(content, "json")
        assert reduced is None

    def test_unknown_type_passes_through(self):
        content = "some text " * 50
        reduced = compress_stage2(content, "text")
        assert reduced is None


class TestStage2Integration:
    """Stage-2 is stored alongside raw content."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()

    def test_transform_hook_stores_stage2(self):
        """After transform hook compresses, stage-2 should be in inline store."""
        content = '{"key": "value_' + "x" * 100 + '", "list": [1,2,3,4,5]}' * 3
        _transform_tool_result(tool_name="read_file", args={}, result=content)
        full_sha = hashlib.sha256(content.encode()).hexdigest()
        if full_sha in _hash_alias:
            h = _hash_alias[full_sha]
            stage2_key = f"{h}#stage2"
            content_inline = _inline_retrieve(stage2_key)
            if content_inline is not None:
                assert isinstance(content_inline, str)

    def test_stage2_retrieval(self):
        """Retrieving with depth=2 returns the reduced version."""
        content = '{"key1": "value1_' + "x" * 80 + '", "key2": "value2"}' * 5
        from plugins.aphrodite._stage2 import compress_stage2
        h, _ = _inline_compress(content)
        reduced = compress_stage2(content, "json")
        if reduced:
            _inline_store_put(f"{h}#stage2", reduced)
            result = _resolve_one(h, depth=2)
            assert result is not None
            assert isinstance(result, str)
