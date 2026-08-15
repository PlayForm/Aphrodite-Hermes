"""Regression test for feedback Point 3: unbounded `.hotreload` accumulation.

Simulates multiple terminated Hermes processes each leaving dylib copies
behind, plus a long-lived live process stacking generations, and verifies
`_reap_stale_hotreloads` keeps storage bounded:

  * copies belonging to PIDs that no longer exist are removed;
  * for a still-alive PID, only the newest generation is kept;
  * the relocated cache directory is what we clean (outside the plugin tree).

Stdlib-only (unittest) so it runs on the plugin's supported Python (>=3.10)
without a pytest dependency.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Load the plugin shim as a module without triggering Hermes registration.
_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("_aphrodite_plugin_under_test", _PLUGIN)
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)

_reap = _plugin._reap_stale_hotreloads
_pid_alive = _plugin._pid_alive
_hotreload_dir = _plugin._hotreload_dir


def _make_cache(self, tmp: Path, prefix: str) -> None:
    # Point the module's hotreload dir at a temp dir for the duration.
    self._saved = _plugin._hotreload_dir
    _plugin._hotreload_dir = lambda: str(tmp)  # type: ignore[assignment]


def _drop(tmp: Path, prefix: str, pid: int, gen: int) -> Path:
    p = tmp / f"{prefix}.{pid}.{gen}"
    p.write_bytes(b"\x00" * 1024)
    return p


class HotreloadCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aphrodite-hotreload-test-"))
        self._saved = _plugin._hotreload_dir
        _plugin._hotreload_dir = lambda: str(self.tmp)  # type: ignore[assignment]
        self.prefix = "libaphrodite_hermes.dylib"

    def tearDown(self):
        _plugin._hotreload_dir = self._saved  # type: ignore[assignment]
        for f in self.tmp.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def test_dead_pid_copies_are_reaped(self):
        dead_pids = (2**31 - 1, 2**31 - 2, 40000)
        for pid in dead_pids:
            _drop(self.tmp, self.prefix, pid, 1)
            _drop(self.tmp, self.prefix, pid, 2)  # several generations
        live = _drop(self.tmp, self.prefix, os.getpid(), 1)

        _reap()

        for pid in dead_pids:
            self.assertFalse(
                (self.tmp / f"{self.prefix}.{pid}.1").exists(),
                f"dead pid {pid} not reaped",
            )
            self.assertFalse(
                (self.tmp / f"{self.prefix}.{pid}.2").exists(),
                f"dead pid {pid} gen2 not reaped",
            )
        self.assertTrue(live.exists(), "live pid's copy must survive")

    def test_live_pid_keeps_only_newest_generation(self):
        pid = os.getpid()
        old = _drop(self.tmp, self.prefix, pid, 1)
        mid = _drop(self.tmp, self.prefix, pid, 2)
        new = _drop(self.tmp, self.prefix, pid, 3)

        _reap()

        self.assertFalse(old.exists(), "oldest generation of live pid should be trimmed")
        self.assertFalse(mid.exists(), "middle generation of live pid should be trimmed")
        self.assertTrue(new.exists(), "newest generation of live pid must be kept")

    def test_pid_alive_recognizes_self(self):
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(-1))
        self.assertFalse(_pid_alive(2**31 - 1))  # implausible PID

    def test_cache_dir_is_relocatable_outside_plugin_tree(self):
        d = Path(_hotreload_dir())
        self.assertEqual(d.parts[-3:], (".hermes", "aphrodite", "hotreload"), str(d))
        self.assertNotIn("plugins", d.parts, "hotreload cache must not live under the plugin tree")


if __name__ == "__main__":
    unittest.main(verbosity=2)
