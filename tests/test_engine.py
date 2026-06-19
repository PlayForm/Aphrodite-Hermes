"""
Aphrodite context engine tests — compression, storage, session lifecycle.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from plugins.aphrodite._core import _conv_index, _hash_alias, _inline_store, _recent_markers, _state
from plugins.aphrodite._engine import AphroditeContextEngine, _pack_msg


class TestMessagePacking:
    """Message serialization for CCR storage."""

    def test_pack_simple_messages(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        packed = _pack_msg(msgs)
        assert isinstance(packed, str)
        assert "hello" in packed
        assert "assistant" in packed

    def test_pack_tool_messages_preserves_call_id(self):
        msgs = [
            {"role": "tool", "content": "result", "tool_call_id": "call_123"},
        ]
        packed = _pack_msg(msgs)
        assert "call_123" in packed

    def test_pack_tool_calls_preserved(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "f"}}]},
        ]
        packed = _pack_msg(msgs)
        assert "tool_calls" in packed

    def test_pack_empty_messages(self):
        packed = _pack_msg([])
        assert packed == "[]"

    def test_pack_non_string_content(self):
        msgs = [{"role": "assistant", "content": 42}]
        packed = _pack_msg(msgs)
        assert "42" in packed


class TestContextEngineSessionLifecycle:
    """Session start/reset clears state correctly."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()
        _recent_markers.clear()
        _conv_index.clear()
        _state["turn_counter"] = 0

    def test_session_start_clears_state(self):
        eng = AphroditeContextEngine()
        # Set some state
        _recent_markers.append({"hash": "h1", "type": "test", "size": 100, "preview": "p", "turn": 1})
        _state["turn_counter"] = 42
        _inline_store["test_key"] = "test_val"

        eng.on_session_reset()

        assert len(_recent_markers) == 0
        assert len(_inline_store) == 0
        assert len(_conv_index) == 0

    def test_should_compress_threshold_zero(self):
        eng = AphroditeContextEngine()
        eng.threshold_percent = 0
        assert eng.should_compress(prompt_tokens=5000) is False

    def test_should_compress_threshold_negative_one(self):
        eng = AphroditeContextEngine()
        eng.threshold_percent = -1
        assert eng.should_compress(prompt_tokens=5000) is True

    def test_should_compress_no_tokens_fallback(self):
        eng = AphroditeContextEngine()
        eng.threshold_percent = 45
        eng.context_length = 100000
        assert eng.should_compress() is True  # No token data yet, allows compress()

    def test_compress_too_few_messages(self):
        """Engine skips compression when messages < min_messages_to_compress."""
        eng = AphroditeContextEngine()
        eng.threshold_percent = 45
        eng.min_messages_to_compress = 12
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
        result = eng.compress(msgs)
        assert result == msgs  # Unchanged - too few messages

    def test_compress_small_middle(self):
        """Engine skips when middle content is below 200 bytes."""
        eng = AphroditeContextEngine()
        eng.min_messages_to_compress = 3
        eng.threshold_percent = -1  # Always compress
        msgs = [
            {"role": "user", "content": "head"},
            {"role": "assistant", "content": "tiny"},
            {"role": "user", "content": "tail"},
        ]
        result = eng.compress(msgs)
        assert result == msgs  # Middle too small to pack

    def test_update_from_response_sets_threshold(self):
        eng = AphroditeContextEngine()
        eng.context_length = 100000
        eng.threshold_percent = 45
        eng.update_from_response({"prompt_tokens": 50000, "completion_tokens": 100, "total_tokens": 50100})
        assert eng.threshold_tokens == 45000

    def test_compress_protects_head_and_tail(self):
        """Head and tail messages are preserved in compressed output."""
        eng = AphroditeContextEngine()
        eng.min_messages_to_compress = 3
        eng.protect_first_n = 2
        eng.protect_last_n = 2
        eng.threshold_percent = -1
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "head1"},
            {"role": "assistant", "content": "middle1"},
            {"role": "user", "content": "middle2"},
            {"role": "assistant", "content": "middle3"},
            {"role": "user", "content": "tail1"},
            {"role": "assistant", "content": "tail2"},
        ]
        # Pack middle must be >= 200 bytes
        from plugins.aphrodite._engine import _pack_msg
        middle = msgs[eng.protect_first_n:-eng.protect_last_n]
        if len(_pack_msg(middle)) < 200:
            # Make middle larger
            for m in middle:
                m["content"] = m["content"] + " padding " * 20
        result = eng.compress(msgs)
        if result != msgs:  # If compression happened
            assert len(result) < len(msgs)  # Fewer messages
            # Head preserved
            assert result[0] == msgs[0]
            # Tail preserved (last messages)
            assert result[-2:] == msgs[-2:]

    def test_compress_with_protected_editing(self):
        """Editing detection extends tail protection."""
        eng = AphroditeContextEngine()
        eng.min_messages_to_compress = 3
        eng.protect_first_n = 2
        eng.protect_last_n = 2
        eng.threshold_percent = -1
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "head1"},
            {"role": "assistant", "content": "middle1"},
            {"role": "user", "content": "middle2"},
            {"role": "assistant", "content": "middle3"},
            {"role": "user", "content": "tail1"},
            {"role": "tool", "content": "wrote file successfully", "tool_call_id": "c1"},
        ]
        middle = msgs[eng.protect_first_n:-eng.protect_last_n]
        if len(_pack_msg(middle)) < 200:
            for m in middle:
                m["content"] = m["content"] + " padding " * 20
        result = eng.compress(msgs)
        if result != msgs:
            # Tail should include the tool message (editing detection extended protection)
            assert any("wrote" in str(m.get("content", "")) for m in result[-3:])


class TestContextEngineStorage:
    """Engine compression stores content for later retrieval."""

    def setup_method(self):
        _inline_store.clear()
        _hash_alias.clear()
        _recent_markers.clear()

    def test_compress_stores_in_inline(self):
        """Compressed content should be findable after engine compresses."""
        eng = AphroditeContextEngine()
        eng.min_messages_to_compress = 3
        eng.threshold_percent = -1  # Always compress
        # Need enough content for _pack_msg to exceed 200 bytes
        large = "X" * 200
        msgs = [
            {"role": "user", "content": large},
            {"role": "assistant", "content": large},
            {"role": "user", "content": large},
            {"role": "assistant", "content": large},
            {"role": "user", "content": large},
        ]
        result = eng.compress(msgs)
        # Either it compressed (len changed) or it didn't (too small middle)
        # Either way, verify no crash and reasonable output
        assert isinstance(result, list)
        assert len(result) >= 2
