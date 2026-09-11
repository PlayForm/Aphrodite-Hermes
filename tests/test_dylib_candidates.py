"""Regression test for issue 5: shallow install paths must not raise IndexError.

The old dylib candidate search indexed the plugin directory's parents chain
unconditionally at depths 2 and 3; for shallow installs such as
/opt/Aphrodite-Hermes (fewer than 4 parents) that indexing raised IndexError
and aborted plugin registration. The fix extracts the candidate list into a
pure `_dylib_candidates(plugin_dir)` helper whose parent-depth fallbacks are
length-guarded, and routes `_load_dylib` through it.

These tests pin that contract:

  * shallow paths build the binaries candidates without IndexError and
    without any monorepo target/release fallback;
  * deep (monorepo-style) paths keep BOTH depth-guarded target/release
    fallbacks;
  * the filesystem root (zero parents) must not raise;
  * the env override (APHRODITE_HERMES_DYLIB_PATH, or the binaries default
    captured in _DYLIB_PATH) is always candidates[0];
  * an end-to-end `_load_dylib` call on a shallow dir with a missing dylib
    raises the deliberate AssertionError ("Dylib not found."), never an
    IndexError.

Stdlib-only (unittest) so it runs on the plugin's supported Python (>=3.10)
without a pytest dependency.
"""

import importlib.util
import os
import unittest
from pathlib import Path

# Load the plugin shim as a module without triggering Hermes registration.
_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "_aphrodite_dylib_candidates_under_test", _PLUGIN
)
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)

# The issue-5 refactor adds the pure helper. Access it lazily so this file is
# runnable before the partner's edit lands (tests skip with a reason instead
# of AttributeError-ing at import time).
_dylib_candidates = getattr(_plugin, "_dylib_candidates", None)


def _require_helper(test_case: unittest.TestCase) -> None:
    if _dylib_candidates is None:
        test_case.skipTest(
            "partner's _dylib_candidates refactor has not landed yet - "
            "_load_dylib still uses the pre-refactor guarded loop"
        )


class DylibCandidatesTest(unittest.TestCase):
    """Issue 5 regression: shallow paths must not raise IndexError."""

    def test_shallow_path_builds_candidates_without_index_error(self):
        # /opt/Aphrodite-Hermes has 2 parents; old code indexed [2]/[3] and crashed.
        _require_helper(self)
        shallow = _dylib_candidates(Path("/opt/Aphrodite-Hermes"))
        self.assertIn(
            str(Path("/opt/Aphrodite-Hermes") / "binaries" / _plugin._DYLIB_NAME),
            shallow,
        )
        self.assertNotIn(
            str(
                Path("/opt/Aphrodite-Hermes")
                / "target"
                / "release"
                / _plugin._DYLIB_NAME
            ),
            shallow,
        )

    def test_deep_path_keeps_monorepo_fallbacks(self):
        _require_helper(self)
        # 5 path components -> 4 parents, so BOTH depth-guarded fallbacks
        # (parents[2] and parents[3]) are inside the chain and must survive.
        # A 4-component path like /repo/plugins/aphrodite only has 3 parents
        # (parents[3] out of range), so it would exercise just one fallback.
        deep = _dylib_candidates(Path("/repo/monorepo/plugins/aphrodite"))
        suffixes = [
            c
            for c in deep
            if c.endswith(f"target{os.sep}release{os.sep}{_plugin._DYLIB_NAME}")
        ]
        self.assertEqual(len(suffixes), 2)  # parents[2] and parents[3] fallbacks

    def test_root_path_no_index_error(self):
        _require_helper(self)
        _dylib_candidates(Path("/"))  # 0 parents - must not raise

    def test_env_override_is_first_candidate(self):
        _require_helper(self)
        # APHRODITE_HERMES_DYLIB_PATH (or the binaries default) must be candidates[0]
        self.assertEqual(_dylib_candidates(Path("/tmp/x"))[0], _plugin._DYLIB_PATH)

    def test_load_dylib_shallow_raises_assertion_not_indexerror(self):
        # End-to-end: env path missing + shallow dir -> must hit the deliberate
        # AssertionError "Dylib not found." not IndexError. Only meaningful once
        # _load_dylib routes through the helper; skip with a reason otherwise.
        _require_helper(self)
        saved = (_plugin._PLUGIN_DIR, _plugin._DYLIB_PATH)
        try:
            _plugin._PLUGIN_DIR = Path("/opt/Aphrodite-Hermes")
            _plugin._DYLIB_PATH = "/nonexistent/libaphrodite_hermes.dylib"
            with self.assertRaises(AssertionError):
                _plugin._load_dylib()
        finally:
            _plugin._PLUGIN_DIR, _plugin._DYLIB_PATH = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)