"""Regression test for feedback Point 3: unbounded `.hotreload` accumulation.

Simulates multiple terminated Hermes processes each leaving dylib copies
behind, plus a long-lived live process stacking generations, and verifies
`_reap_stale_hotreloads` keeps storage bounded:

  * copies belonging to PIDs that no longer exist are removed;
  * for a still-alive PID, only the newest generation is kept;
  * the relocated cache directory is what we clean (outside the plugin tree).
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Load the plugin shim as a module without triggering Hermes registration.
_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("_aphrodite_plugin_under_test", _PLUGIN)
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)

_reap = _plugin._reap_stale_hotreloads
_pid_alive = _plugin._pid_alive
_hotreload_dir = _plugin._hotreload_dir


def _make_cache(tmp: Path, prefix: str) -> None:
    """Point the module's hotreload dir at a temp dir for the duration."""
    _plugin._hotreload_dir = lambda: str(tmp)  # type: ignore[assignment]


def _drop(tmp: Path, prefix: str, pid: int, gen: int) -> Path:
    p = tmp / f"{prefix}.{pid}.{gen}"
    p.write_bytes(b"\x00" * 1024)
    return p


def test_dead_pid_copies_are_reaped(tmp_path: Path):
    prefix = "libaphrodite_hermes.dylib"
    _make_cache(tmp_path, prefix)
    # Use PIDs that are virtually never alive on a test machine.
    dead_pids = (2**31 - 1, 2**31 - 2, 40000)
    for pid in dead_pids:
        dead = _drop(tmp_path, prefix, pid, 1)
        dead = _drop(tmp_path, prefix, pid, 2)  # several generations
    # A live PID (ours) with one generation - must survive.
    live = _drop(tmp_path, prefix, os.getpid(), 1)

    _reap()

    for pid in dead_pids:
        assert not (tmp_path / f"{prefix}.{pid}.1").exists(), f"dead pid {pid} not reaped"
        assert not (tmp_path / f"{prefix}.{pid}.2").exists(), f"dead pid {pid} gen2 not reaped"
    assert live.exists(), "live pid's copy must survive"


def test_live_pid_keeps_only_newest_generation(tmp_path: Path):
    prefix = "libaphrodite_hermes.dylib"
    _make_cache(tmp_path, prefix)
    pid = os.getpid()
    old = _drop(tmp_path, prefix, pid, 1)
    mid = _drop(tmp_path, prefix, pid, 2)
    new = _drop(tmp_path, prefix, pid, 3)

    _reap()

    assert old.exists() is False, "oldest generation of live pid should be trimmed"
    assert mid.exists() is False, "middle generation of live pid should be trimmed"
    assert new.exists(), "newest generation of live pid must be kept"


def test_pid_alive_recognizes_self():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(-1) is False
    assert _pid_alive(2**31 - 1) is False  # implausible PID


def test_cache_dir_is_relocatable_outside_plugin_tree():
    # The cache must live under ~/.hermes/aphrodite/hotreload, NOT inside the
    # plugin source tree (so plugin-doctor never stages/copies it, and it
    # can't accumulate in version control or released artifacts).
    d = Path(_hotreload_dir())
    assert d.parts[-3:] == (".hermes", "aphrodite", "hotreload"), d
    assert "plugins" not in d.parts, "hotreload cache must not live under the plugin tree"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
