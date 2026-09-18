"""Performance probe: end-to-end compression + retrieval latency over the REAL dylib.

This is a benchmark test, not a unit test: it loads the actual
libaphrodite_hermes dylib (never a stub) and measures the wall-clock cost
of a full compress -> retrieve round trip across N=100 samples, reporting
the p50 (median) and p95 latencies.

Design rules (repo convention: defensive coding):

  * Skip, never fail, when no real dylib is present. CI and fresh checkouts
    may not have binaries/ populated or target/release built yet; a missing
    dylib is an environment condition, not a regression. The skip reason
    says exactly which candidate paths were tried.
  * If a dylib IS present but fails to load or round-trips incorrectly,
    that is a genuine regression and the test FAILS (loud) - a present-but-
    broken binary must not masquerade as "skipped".
  * The latency bound is deliberately pathological (p95 < 5s), not a
    benchmark target: it only catches an entirely broken engine (e.g. a
    synchronous proxy round-trip where none should exist), so the probe
    stays non-flaky on loaded CI machines while still being meaningful.
  * Round-trip integrity is asserted on every sample - the probe measures
    correct compression, not just speed.
  * Test hygiene mirrors test_windows_process_state.py: APHRODITE_HOME is
    pointed at a pytest tmp_path and APHRODITE_NO_AUTO_DOWNLOAD=1 keeps the
    run hermetic/offline, and an atexit guard redirects the plugin's
    exit-time hotreload sweep away from the real ~/.hermes/aphrodite.

Requires pytest (this file is excluded from the stdlib-unittest trio by
design - the unittest files run on the plugin's supported Python >=3.10
with zero dependencies, while a latency probe needs pytest.skip).
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_STATE_MODULE_NAME = "aphrodite_hermes._process_state"

# Sample payload: ~2 KB of mixed code-like + prose text, enough to exercise
# the classifier (type detection) on every sample, varied per sample so each
# compress hashes distinct content.
_SAMPLE_HEAD = (
    "def handle_request(req):\n"
    "    # route: token (:9798, SQLite, >1KB) | cache (:9797, in-memory, >8KB)\n"
    "    body = req.read(4096).decode('utf-8', errors='replace')\n"
    "    if '<<<CCR:' in body:\n"
    "        return retrieve_marker(body)\n"
    "    return json.dumps({'status': 'ok', 'bytes': len(body)})\n"
)
_SAMPLE_TAIL = (
    "The aphrodite plugin proxies large tool output into a content-addressable "
    "store and hands the transcript a compact marker instead of the raw blob. "
    "Retrieval expands the marker back to the exact original bytes, so the "
    "round trip must be byte-identical for the compression to be lossless.\n"
)

# Pathological bound only - real p50/p95 on a warm dylib are sub-millisecond.
_P95_BOUND_SECONDS = 5.0

_SCRATCH_HOME = Path(tempfile.mkdtemp(prefix="aphrodite-perf-atexit-"))
_guard_registered = False


def _redirect_exit_reap() -> None:
    """Point the plugin's exit-time hotreload sweep at a scratch dir.

    Same rationale as test_windows_process_state.py: the plugin registers a
    real atexit handler that reaps stale hotreload copies by resolving
    APHRODITE_HOME at exit time, so a LIFO guard registered after it keeps
    the sweep inside scratch and off the live tree.
    """
    global _guard_registered
    if _guard_registered:
        return
    _guard_registered = True

    def _guard() -> None:
        os.environ["APHRODITE_HOME"] = str(_SCRATCH_HOME)

    atexit.register(_guard)


def _exec_shim(name: str):
    """exec_module() the plugin __init__.py under `name` (Hermes-style)."""
    spec = importlib.util.spec_from_file_location(name, _PLUGIN)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def real_dylib(tmp_path, monkeypatch):
    """Load the plugin shim against the REAL dylib, or skip if none exists."""
    monkeypatch.delitem(sys.modules, _STATE_MODULE_NAME, raising=False)
    monkeypatch.setenv("APHRODITE_HOME", str(tmp_path))
    monkeypatch.delenv("APHRODITE_HERMES_DYLIB_PATH", raising=False)
    # Defensive: never let the probe auto-download binaries or launch proxies.
    monkeypatch.setenv("APHRODITE_NO_AUTO_DOWNLOAD", "1")
    monkeypatch.setenv("APHRODITE_NO_AUTO_LAUNCH", "1")

    mod = _exec_shim("_aphrodite_perf_probe")
    candidates = mod._dylib_candidates(mod._PLUGIN_DIR)
    real = next((p for p in candidates if os.path.exists(p)), None)
    if real is None:
        pytest.skip(
            "no real dylib present - perf probe skipped (tried: %s); "
            "build crates/aphrodite-hermes (target/release) or run "
            "download.sh to enable it" % (candidates,)
        )
    yield mod, real
    sys.modules.pop("_aphrodite_perf_probe", None)
    _redirect_exit_reap()


def test_compress_retrieve_roundtrip_latency_over_real_dylib(real_dylib, capsys):
    """N=100 compress+retrieve round trips over the real dylib; report p50/p95.

    Every sample must round-trip byte-identical (correctness is measured,
    not assumed), and p95 must stay under the pathological bound.
    """
    mod, real_path = real_dylib
    dylib = mod._load_dylib()

    def _compress(content: str) -> dict:
        out = mod._call_json(
            dylib,
            "aphrodite_hermes_dispatch_tool",
            b"aphrodite_compress",
            __import__("json").dumps({"content": content}).encode("utf-8"),
        )
        assert isinstance(out, dict) and out.get("hash"), f"compress failed: {out!r}"
        return out

    def _retrieve(hash_: str) -> str:
        out = mod._call_json(
            dylib,
            "aphrodite_hermes_dispatch_tool",
            b"aphrodite_retrieve",
            __import__("json").dumps({"hash": hash_}).encode("utf-8"),
        )
        assert isinstance(out, dict) and out.get("found"), f"retrieve failed: {out!r}"
        return out["content"]

    n = 100
    latencies: list[float] = []

    # Warm-up: first call pays one-time costs (mtime check, copy, classifier
    # table init) that are not representative of steady-state latency.
    _retrieve(_compress(_SAMPLE_HEAD + f"[warmup {0:04d}]\n" + _SAMPLE_TAIL)["hash"])

    for i in range(n):
        content = _SAMPLE_HEAD + f"[sample {i:04d}]\n" + _SAMPLE_TAIL
        start = time.perf_counter()
        marker = _compress(content)
        roundtrip = _retrieve(marker["hash"])
        elapsed = time.perf_counter() - start
        latencies.append(elapsed)
        assert roundtrip == content, (
            f"round-trip mismatch on sample {i}: got {len(roundtrip)}B, expected {len(content)}B"
        )

    p50 = statistics.quantiles(latencies, n=100)[49]
    p95 = statistics.quantiles(latencies, n=100)[94]
    print(
        f"\nperf probe: {n} round trips over {real_path}\n"
        f"  p50 (median): {p50 * 1000:.2f} ms\n"
        f"  p95:          {p95 * 1000:.2f} ms\n"
        f"  min:          {min(latencies) * 1000:.2f} ms\n"
        f"  max:          {max(latencies) * 1000:.2f} ms"
    )
    # capsys keeps the report visible in verbose pytest output.
    _ = capsys

    assert p95 < _P95_BOUND_SECONDS, (
        f"p95 latency {p95:.3f}s exceeds pathological bound "
        f"{_P95_BOUND_SECONDS}s - the engine is not operating normally"
    )
