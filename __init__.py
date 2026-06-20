"""aphrodite v1.62.62 — CCR compression plugin for Hermes Agent.

Thin Python loader — all compression logic lives in the Rust dylib
(libaphrodite.dylib). This file only handles:
  - Loading the dylib
  - Registering hooks and tools with Hermes
  - Proxy lifecycle orchestration
  - Context engine integration
"""

import logging
import os
import sys
from pathlib import Path

# ── Core (re-exports everything from config + state + store + struct + template) ──
from ._core import (
    _CCR_RE,
    _DEV,
    _FILE_TOOLS,
    BIN_VERSION,
    BINARY,
    BINARY_DIR,
    CATALOG_MODE,
    CONTEXT_ENGINE,
    DEBUG_LOGGING,
    ENGINE_MIN_MSGS,
    ENGINE_PROTECT_FIRST,
    ENGINE_PROTECT_LAST,
    ENGINE_THRESHOLD_PCT,
    ENV_FILE,
    INLINE_THRESHOLD,
    PLUGIN_VERSION,
    PORTS,
    RECURSIVE_DEPTH,
    REPO,
    TERMINAL_THRESHOLD,
    TOOL_THRESHOLD_CACHE,
    TOOL_THRESHOLD_TOKEN,
    _cfg_int,
    _conv_index,
    _fmt_size,
    _get_turn_counter,
    _git_cache,
    _increment_turn,
    _inline_clear,
    _inline_store,
    _recent_markers,
    _referenced_files,
    _reset_turn_counter,
)

# ── Code structure ─────────────────────────────────────
from ._core.struct import _CODE_PATTERNS, _extract_code_structure

# ── Engine ─────────────────────────────────────────────
from ._engine import AphroditeContextEngine, _fire_hook, _set_engine, get_engine

# ── Hooks ──────────────────────────────────────────────
from ._hooks import (
    CATALOG_SCHEMA,
    DIFF_SCHEMA,
    FILES_SCHEMA,
    PREFETCH_SCHEMA,
    PREFETCH_STATUS_SCHEMA,
    REBUILD_SCHEMA,
    RECLASSIFY_SCHEMA,
    SEARCH_SCHEMA,
    STATS_SCHEMA,
    TEST_SCHEMA,
    _aphrodite_reclassify_handler,
    _catalog_handler,
    _diff_handler,
    _extract_preview,
    _files_handler,
    _git_summary,
    _group_into_turns,
    _pre_llm_hook,
    _prefetch_handler,
    _prefetch_status_handler,
    _rebuild_handler,
    _search_handler,
    _stats_handler,
    _store_conversation_turn,
    _test_handler,
    _track_file_refs,
    _transform_terminal_hook,
    _transform_tool_result,
)

# ── Inline compression ─────────────────────────────────
from ._inline import _inline_compress, _inline_retrieve

# ── Marker utilities ───────────────────────────────────
from ._marker import _ccr_marker, _compress_via_proxy, _parse_ccr_markers
from ._marker.classify import _classify_content
from ._marker.preview import _make_ccr_preview

# ── Proxy lifecycle ────────────────────────────────────
from ._proxy import _alive, _alive_cache, _load_env, _start, _wait_alive, on_start
from ._proxy.health import _headroom_context

# ── Resolution ─────────────────────────────────────────
from ._resolve import _resolve_one, _resolve_recursive

# ── Stage 2 ────────────────────────────────────────────
from ._stage2 import compress_stage2

# ── Tool handlers ──────────────────────────────────────
from ._tools import COMPRESS_SCHEMA, RETRIEVE_SCHEMA, _compress_handler, _retrieve_handler

_log = logging.getLogger("aphrodite")

# Sync docstring version
__doc__ = (__doc__ or "").replace("v1.62.62", f"v{PLUGIN_VERSION}")


# ── Plugin registration ────────────────────────────────
def register(ctx):
    """Register hooks, tools, and context engine with Hermes."""
    ctx.register_hook("on_session_start", on_start)
    ctx.register_hook("pre_llm_call", _pre_llm_hook)
    ctx.register_hook("transform_terminal_output", _transform_terminal_hook)
    ctx.register_hook("post_llm_call", _store_conversation_turn)
    ctx.register_hook("transform_tool_result", _transform_tool_result)

    ctx.register_tool(name="aphrodite_rebuild", schema=REBUILD_SCHEMA, handler=_rebuild_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_compress", schema=COMPRESS_SCHEMA, handler=_compress_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_retrieve", schema=RETRIEVE_SCHEMA, handler=_retrieve_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_stats", schema=STATS_SCHEMA, handler=_stats_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_files", schema=FILES_SCHEMA, handler=_files_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_diff", schema=DIFF_SCHEMA, handler=_diff_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_search", schema=SEARCH_SCHEMA, handler=_search_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_test", schema=TEST_SCHEMA, handler=_test_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_catalog", schema=CATALOG_SCHEMA, handler=_catalog_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_reclassify", schema=RECLASSIFY_SCHEMA, handler=_aphrodite_reclassify_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_prefetch", schema=PREFETCH_SCHEMA, handler=_prefetch_handler, toolset="aphrodite")
    ctx.register_tool(name="aphrodite_prefetch_status", schema=PREFETCH_STATUS_SCHEMA, handler=_prefetch_status_handler, toolset="aphrodite")

    engine_configured = CONTEXT_ENGINE
    if engine_configured:
        try:
            engine = AphroditeContextEngine()
            ctx.register_context_engine(engine)
            ctx.register_hook("on_session_start", engine.on_session_start)
            _log.info("aphrodite context engine registered")
        except Exception as e:
            msg = f"aphrodite context engine registration failed [{type(e).__name__}]: {e}"
            _log.warning(msg)
            print(msg, file=sys.stderr)
    else:
        _log.info("context engine not registered — set APHRODITE_CONTEXT_ENGINE=1 to enable")

    # Bundle skills
    _skills_dir = Path(__file__).parent / "skills"
    _skills = [
        ("aphrodite-boundary-behaviors", "Edge cases and boundary conditions for compression pipeline"),
        ("aphrodite-coding-defaults", "Coding-optimized compression defaults, centers, and auto-expand"),
        ("aphrodite-compression-architecture", "Compression architecture reference — semantic layers, token savings"),
        ("aphrodite-context-efficiency", "Techniques for minimizing token usage with compressed content"),
        ("aphrodite-context-engine-defaults", "Context engine configuration defaults and tuning guide"),
        ("aphrodite-output-formatting", "LLM-native formatting rules for all output — previews, catalog, stats"),
        ("aphrodite-presentation", "How to present features in README, docs, and user-facing content"),
        ("aphrodite-proxy-lifecycle", "Proxy startup, health checks, auto-launch, and lifecycle management"),
        ("aphrodite-tool-guide", "Full reference for CCR tools: retrieve, compress, stats, search, catalog"),
    ]
    for _name, _desc in _skills:
        ctx.register_skill(_name, _skills_dir / _name / "SKILL.md", _desc)

    _log.info("aphrodite v%s registered — 12 tools + 9 skills + hooks", PLUGIN_VERSION)

    if DEBUG_LOGGING:
        lines = [
            "=" * 60,
            f"APHRODITE v{PLUGIN_VERSION} — DEBUG MODE",
            f"  Mode: {'proxy+hooks' if not engine_configured else 'proxy+hooks+engine'} | Engine: {'enabled' if engine_configured else 'off'} | Dev: {'on' if _DEV else 'off'}",
            f"  Thresholds: terminal={TERMINAL_THRESHOLD} inline={INLINE_THRESHOLD} tool_token={TOOL_THRESHOLD_TOKEN} tool_cache={TOOL_THRESHOLD_CACHE}",
            f"  Engine: threshold={ENGINE_THRESHOLD_PCT}% protect={ENGINE_PROTECT_FIRST}/{ENGINE_PROTECT_LAST} min_msgs={ENGINE_MIN_MSGS}",
            f"  CCR: depth={RECURSIVE_DEPTH}",
            "  Tools: retrieve, compress, stats, rebuild, files, diff, search, test, catalog, reclassify",
            f"  Catalog mode: {CATALOG_MODE}",
            "=" * 60,
        ]
        for line in lines:
            print(line)
            _log.info(line)
