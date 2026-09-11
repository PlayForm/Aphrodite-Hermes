"""Platform-agnostic prefix contract for `_reap_stale_hotreloads` (issue 4).

Issue 4 regression: the hotreload cleanup tests hardcoded the tombstone
prefix as `libaphrodite_hermes.dylib` while the implementation derives it at
runtime from `os.path.basename(_DYLIB_PATH)` and filters candidates with
`name.startswith(prefix + ".")`. On Linux (`.so`) and Windows (`.dll`) that
mismatch made the reaper no-op, so the cleanup regression tests failed
cross-platform.

This file pins the platform-agnostic contract directly:

  * dead-PID tombstones must be reaped for ALL THREE platform dylib name
    forms (`.dylib` / `.so` / `.dll`), driven solely by whatever
    `os.path.basename(_DYLIB_PATH)` resolves to at reap time;
  * defensively, a tombstone with an UNRELATED prefix (a file this plugin
    does not own) must NEVER be reaped - the reaper only deletes files whose
    names start with its own dylib basename.

Stdlib-only (unittest) so it runs on the plugin's supported Python (>=3.10)
without a pytest dependency.
"""

import importlib.util
import os
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

# Load the plugin shim as a module without triggering Hermes registration.
_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "_aphrodite_reaper_prefix_under_test", _PLUGIN
)
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)

_reap = _plugin._reap_stale_hotreloads

# Implausible PID - dead on every platform, never accidentally a live process.
_DEAD_PID = 2**31 - 1

# The three platform dylib name forms the reaper must handle.
_DYLIB_NAME_FORMS = (
    "libaphrodite_hermes.dylib",  # macOS
    "libaphrodite_hermes.so",  # Linux
    "aphrodite_hermes.dll",  # Windows
)


class ReaperPrefixContractTest(unittest.TestCase):
    """Issue 4: reaping must work for every platform's dylib name form."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aphrodite-prefix-contract-"))
        self._saved_dir = _plugin._hotreload_dir
        self._saved_dylib = _plugin._DYLIB_PATH
        _plugin._hotreload_dir = lambda: str(self.tmp)  # type: ignore[assignment]

    def tearDown(self):
        _plugin._hotreload_dir = self._saved_dir  # type: ignore[assignment]
        _plugin._DYLIB_PATH = self._saved_dylib
        for f in self.tmp.glob("*"):
            with suppress(OSError):
                f.unlink()
        with suppress(OSError):
            self.tmp.rmdir()

    def _tombstone(self, prefix: str, pid: int, gen: int = 1) -> Path:
        p = self.tmp / f"{prefix}.{pid}.{gen}"
        p.write_bytes(b"\x00" * 1024)
        return p

    def test_dead_pid_tombstones_reaped_for_every_dylib_name_form(self):
        for name in _DYLIB_NAME_FORMS:
            with self.subTest(dylib_name=name):
                # Simulate the plugin running on that platform: the reaper
                # derives its prefix from _DYLIB_PATH at call time.
                _plugin._DYLIB_PATH = f"/some/install/binaries/{name}"
                prefix = os.path.basename(_plugin._DYLIB_PATH)
                self.assertEqual(prefix, name)
                tombstone = self._tombstone(prefix, _DEAD_PID, gen=1)

                _reap()

                self.assertFalse(
                    tombstone.exists(),
                    f"dead-pid tombstone {tombstone.name} not reaped",
                )

    def test_unrelated_prefix_is_never_reaped(self):
        """Defensive: the reaper must not delete files it does not own."""
        _plugin._DYLIB_PATH = "/some/install/binaries/libaphrodite_hermes.dylib"
        prefix = os.path.basename(_plugin._DYLIB_PATH)

        foreign = self._tombstone("some_other_file.so", 123, gen=1)
        owned = self._tombstone(prefix, _DEAD_PID, gen=1)

        _reap()

        self.assertTrue(
            foreign.exists(),
            "reaper deleted a file with an unrelated prefix - it must only "
            "reap tombstones starting with its own dylib basename",
        )
        self.assertFalse(
            owned.exists(),
            "control tombstone not reaped - the reaper did not run",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)