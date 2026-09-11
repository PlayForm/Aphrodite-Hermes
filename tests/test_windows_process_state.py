"""Regression tests for the Windows multi-home load path.

Hermes builds one PluginManager per Hermes home (root + every profile) and
exec_module()s this shim once per home under a distinct module name, all in
one process. Two things went wrong on Windows:

  * dylib state lived in module globals, so the second exec restarted the
    generation counter and tried to overwrite `<name>.<pid>.0` - the copy the
    first load had already mapped - which fails with PermissionError [Errno 13]
    (a mapped DLL is locked on Windows);
  * `_start_proxy` Popen'd the binary unconditionally, so every extra process
    spawned a proxy that died on `failed to bind listener` (os error 10048),
    growing proxy-stderr.log on every start.

These tests exec the shim twice under different names against a fake dylib
(ctypes.CDLL stubbed) with the data dir pointed at tmp_path via APHRODITE_HOME,
and exercise the pre-launch health probe with the health opener/Popen stubbed
(against the current tree the probe is `_health_opener.open()` with a JSON
`status == "healthy"` body check, not a bare status-code read).

Test hygiene: the dylib tests exercise `_load_dylib`, which registers a REAL
atexit handler (`_register_atexit_cleanup`) that reaps stale hotreload copies
at interpreter exit. By exit time the env monkeypatches have been restored, so
that handler would sweep the DEFAULT ~/.hermes/aphrodite hotreload dir - on a
live machine that could trim a real proxy's generations. `_redirect_exit_reap`
registers a guard handler AFTER the plugin's (atexit is LIFO, so ours fires
first) that points APHRODITE_HOME at a scratch dir, so the exit-time reap
sweeps only scratch and never touches the real tree.
"""

from __future__ import annotations

import atexit
import ctypes
import importlib.util
import logging
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_STATE_MODULE_NAME = "aphrodite_hermes._process_state"

# ── Test hygiene: keep the plugin's real atexit handler off the live tree ──
# The exit-time reap resolves its data dir dynamically (it reads the
# APHRODITE_HOME env var via _data_dir at exit time), so a guard handler that
# runs BEFORE the plugin's cleanup can redirect it to scratch. atexit is LIFO:
# the guard must be registered after the plugin's handler, which we do from
# fixture teardown (always runs after the test body that called _load_dylib).
_SCRATCH_HOME = Path(tempfile.mkdtemp(prefix="aphrodite-test-atexit-"))
_guard_registered = False


def _redirect_exit_reap() -> None:
    """Point the plugin's exit-time hotreload sweep at a scratch dir.

    Idempotent; call from fixture teardown so the guard is registered after
    any real atexit handler the plugin installed mid-test.
    """
    global _guard_registered
    if _guard_registered:
        return
    _guard_registered = True

    def _guard() -> None:
        # The plugin's _cleanup -> _reap_stale_hotreloads -> _data_dir reads
        # this env var at exit time, so pointing it at scratch makes the sweep
        # a no-op on the real ~/.hermes/aphrodite.
        os.environ["APHRODITE_HOME"] = str(_SCRATCH_HOME)

    atexit.register(_guard)


class _FakeFn:
    restype = None
    argtypes = None


class _FakeCDLL:
    """Stand-in for ctypes.CDLL: records every load, exposes any symbol."""

    instances: list["_FakeCDLL"] = []

    def __init__(self, path: str) -> None:
        self.path = path
        _FakeCDLL.instances.append(self)

    def __getattr__(self, name: str) -> _FakeFn:
        return _FakeFn()


class _FakeProc:
    """Stand-in for the Popen return value: alive until the test ends.

    The current tree's `_start_proxy` polls/wait()s the child right after
    Popen to detect immediate death, so a stub must expose poll()/wait().
    """

    def poll(self):
        return None  # still running

    def wait(self, timeout=None):
        return None


class _FakeOpener:
    """Stand-in for the module-global `_health_opener` urllib opener.

    `_proxy_healthy` calls `_health_opener.open(req, timeout=...)` (a direct
    opener built with an empty ProxyHandler map), not urllib.request.urlopen,
    so stubbing the module global is what actually intercepts the probe.
    """

    def __init__(self, fn):
        self._fn = fn

    def open(self, req, timeout=None):
        return self._fn(req, timeout=timeout)


def _exec_shim(name: str):
    """exec_module() the plugin __init__.py under `name`, the way Hermes'
    _load_directory_module does for each Hermes home."""
    spec = importlib.util.spec_from_file_location(name, _PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Fresh process-global state + data dir under tmp_path + stubbed CDLL."""
    monkeypatch.delitem(sys.modules, _STATE_MODULE_NAME, raising=False)
    monkeypatch.setenv("APHRODITE_HOME", str(tmp_path))
    monkeypatch.delenv("APHRODITE_HERMES_DYLIB_PATH", raising=False)
    # Defensive: the plugin auto-fetches missing binaries via download.sh on
    # first load and this repo ships no binaries/ dir; opt out so the suite
    # stays offline and hermetic whichever _load_dylib variant lands.
    monkeypatch.setenv("APHRODITE_NO_AUTO_DOWNLOAD", "1")
    monkeypatch.setattr(ctypes, "CDLL", _FakeCDLL)
    _FakeCDLL.instances.clear()
    fake_dylib = tmp_path / "fake_aphrodite_hermes.dll"
    fake_dylib.write_bytes(b"MZ" + b"\x00" * 64)
    yield tmp_path, fake_dylib
    # Make sure a read-only copy left behind by a test doesn't break cleanup.
    for f in (tmp_path / "hotreload").glob("*"):
        with suppress(OSError):
            os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    for name in [n for n in sys.modules if n.startswith("_aphrodite_shim_")]:
        del sys.modules[name]
    # Neutralize the plugin's exit-time reap (see module docstring).
    _redirect_exit_reap()


def _load_twice(tmp_path: Path, fake_dylib: Path, monkeypatch, lock_first_copy: bool):
    first = _exec_shim("_aphrodite_shim_home_a")
    monkeypatch.setattr(first, "_DYLIB_PATH", str(fake_dylib))
    h1 = first._load_dylib()

    copies = sorted((tmp_path / "hotreload").glob("*"))
    assert len(copies) == 1, copies
    assert copies[0].name == f"{fake_dylib.name}.{os.getpid()}.0"
    if lock_first_copy:
        # Emulate the mapped-DLL lock: on Windows a read-only file cannot be
        # opened for writing or removed, which is exactly the PermissionError
        # the second load used to raise when it re-targeted this path.
        os.chmod(copies[0], stat.S_IREAD)

    second = _exec_shim("_aphrodite_shim_home_b")
    assert second is not first
    monkeypatch.setattr(second, "_DYLIB_PATH", str(fake_dylib))
    h2 = second._load_dylib()
    return first, second, h1, h2, copies[0]


def test_second_exec_reuses_mapped_handle_without_recopying(isolated_home, monkeypatch):
    tmp_path, fake_dylib = isolated_home
    first, second, h1, h2, copy = _load_twice(tmp_path, fake_dylib, monkeypatch, lock_first_copy=False)

    assert h2 is h1, "second shim copy must return the already-loaded handle"
    assert len(_FakeCDLL.instances) == 1, "dylib must be mapped exactly once per process"
    assert sorted((tmp_path / "hotreload").glob("*")) == [copy], "no second copy may be created"
    # Both shim copies see the same holder module, not their own globals.
    assert first._state is second._state
    assert first._state is sys.modules[_STATE_MODULE_NAME]
    assert first._state.dylib_copy_path == str(copy)
    assert first._state.atexit_registered is True


def test_second_exec_survives_locked_first_copy(isolated_home, monkeypatch):
    """With the old module-global state this raised PermissionError from
    shutil.copy2 (second load re-targets `<name>.<pid>.0`, which is locked)."""
    tmp_path, fake_dylib = isolated_home
    _, _, h1, h2, copy = _load_twice(tmp_path, fake_dylib, monkeypatch, lock_first_copy=True)

    assert h2 is h1
    assert len(_FakeCDLL.instances) == 1
    assert sorted((tmp_path / "hotreload").glob("*")) == [copy]


def test_mtime_change_still_hot_reloads_across_shim_copies(isolated_home, monkeypatch):
    """The shared holder must not disable hot-reload: a genuinely rebuilt dylib
    (new mtime) still gets a fresh generation, and the old copy is removed."""
    tmp_path, fake_dylib = isolated_home
    first, second, h1, h2, copy = _load_twice(tmp_path, fake_dylib, monkeypatch, lock_first_copy=False)

    os.utime(fake_dylib, (fake_dylib.stat().st_atime, fake_dylib.stat().st_mtime + 10))
    h3 = second._load_dylib()

    assert h3 is not h1
    assert len(_FakeCDLL.instances) == 2
    remaining = sorted((tmp_path / "hotreload").glob("*"))
    assert remaining == [tmp_path / "hotreload" / f"{fake_dylib.name}.{os.getpid()}.1"]
    assert first._load_dylib() is h3, "first shim copy sees the reloaded handle too"


def test_data_dir_honours_aphrodite_home(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, _STATE_MODULE_NAME, raising=False)
    monkeypatch.setenv("APHRODITE_HOME", str(tmp_path / "custom"))
    mod = _exec_shim("_aphrodite_shim_home_env")
    try:
        assert mod._data_dir() == tmp_path / "custom"
        assert Path(mod._hotreload_dir()) == tmp_path / "custom" / "hotreload"

        monkeypatch.delenv("APHRODITE_HOME")
        assert mod._data_dir() == Path.home() / ".hermes" / "aphrodite"
    finally:
        sys.modules.pop("_aphrodite_shim_home_env", None)


# ── _start_proxy pre-launch probe ────────────────────────────────────────


class _Resp:
    status = 200

    def read(self, n: int = -1) -> bytes:
        # The current tree's _proxy_healthy requires the body to be JSON with
        # status == "healthy" (what the Rust /health endpoint actually
        # returns) - a bare 200 is no longer enough to count as healthy.
        return b'{"status":"healthy","version":"test"}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def proxy_shim(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, _STATE_MODULE_NAME, raising=False)
    monkeypatch.setenv("APHRODITE_HOME", str(tmp_path))
    monkeypatch.delenv("APHRODITE_NO_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("APHRODITE_CACHE_PORT", raising=False)
    monkeypatch.delenv("APHRODITE_TOKEN_PORT", raising=False)
    # Defensive: keep the suite offline/hermetic (see isolated_home).
    monkeypatch.setenv("APHRODITE_NO_AUTO_DOWNLOAD", "1")
    mod = _exec_shim("_aphrodite_shim_proxy")
    fake_binary = tmp_path / "aphrodite-fake-binary"
    fake_binary.write_bytes(b"")
    monkeypatch.setattr(mod, "_BINARY_PATH", str(fake_binary))
    yield mod, tmp_path
    sys.modules.pop("_aphrodite_shim_proxy", None)
    # Neutralize the plugin's exit-time reap (see module docstring).
    _redirect_exit_reap()


def test_start_proxy_skips_launch_when_both_proxies_healthy(proxy_shim, monkeypatch, caplog):
    mod, tmp_path = proxy_shim
    probed: list[str] = []
    popen_calls: list[list[str]] = []

    def fake_urlopen(req, timeout=None):
        probed.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(mod, "_health_opener", _FakeOpener(fake_urlopen))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popen_calls.append(a[0]))

    with caplog.at_level(logging.INFO, logger="aphrodite"):
        mod._start_proxy()

    assert popen_calls == [], "must not launch a second proxy when one already owns the ports"
    assert sorted(probed) == [
        "http://127.0.0.1:9797/health",
        "http://127.0.0.1:9798/health",
    ]
    assert not (tmp_path / "proxy-stderr.log").exists(), "no stderr log line may be appended"
    skip_records = [r for r in caplog.records if "already healthy" in r.getMessage()]
    assert skip_records and skip_records[0].levelno == logging.INFO
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_start_proxy_launches_when_ports_are_free(proxy_shim, monkeypatch, caplog):
    mod, tmp_path = proxy_shim
    popen_calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append(argv)
        assert kwargs["stderr"].name.endswith("proxy-stderr.log")
        return _FakeProc()

    def fake_urlopen(req, timeout=None):
        # Nobody listening until *we* launch; healthy right after.
        if not popen_calls:
            raise ConnectionRefusedError
        return _Resp()

    monkeypatch.setattr(mod, "_health_opener", _FakeOpener(fake_urlopen))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with caplog.at_level(logging.INFO, logger="aphrodite"):
        mod._start_proxy()

    assert popen_calls == [[mod._BINARY_PATH]]
    assert (tmp_path / "proxy-stderr.log").exists(), "stderr log lives under APHRODITE_HOME"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_start_proxy_honours_no_auto_launch(proxy_shim, monkeypatch):
    mod, _ = proxy_shim
    monkeypatch.setenv("APHRODITE_NO_AUTO_LAUNCH", "1")
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_health_opener",
        _FakeOpener(lambda req, timeout=None: calls.append("probe")),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append("popen"))

    mod._start_proxy()

    assert calls == [], "NO_AUTO_LAUNCH must short-circuit before probing or launching"


def test_start_proxy_still_launches_when_only_one_port_healthy(proxy_shim, monkeypatch, caplog):
    """Half-up state must NOT skip the launch.

    The pre-launch probe only skips when BOTH ports answer healthy. A single
    healthy port is a partial signal (stale or half-dead instance, foreign
    listener) - the plugin must still launch its own proxy pair.
    """
    mod, tmp_path = proxy_shim
    popen_calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append(argv)
        return _FakeProc()

    def fake_urlopen(req, timeout=None):
        # cache (:9797) is already healthy; token (:9798) is dead until the
        # launch brings the second proxy up.
        if req.full_url.endswith(":9798/health") and not popen_calls:
            raise ConnectionRefusedError
        return _Resp()

    monkeypatch.setattr(mod, "_health_opener", _FakeOpener(fake_urlopen))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with caplog.at_level(logging.INFO, logger="aphrodite"):
        mod._start_proxy()

    assert popen_calls == [[mod._BINARY_PATH]], (
        "half-up (one port healthy, one dead) must still launch, not skip"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_foreign_200_responder_does_not_skip_launch(proxy_shim, monkeypatch):
    """A 200 from a NON-aphrodite listener must not count as proxy health.

    Regression: a status-only probe mistakes any 200 (a dev server, an app
    squatting on the port) for a healthy proxy and skips the launch, leaving
    the plugin without a real proxy. The current tree's _proxy_healthy
    validates the body (JSON with status == "healthy"); if that validation is
    ever reverted, this skips with a reason instead of silently asserting a
    false positive.
    """
    mod, tmp_path = proxy_shim
    popen_calls: list[list[str]] = []

    class _ForeignResp(_Resp):
        def read(self, n: int = -1) -> bytes:
            return b"<html>not the aphrodite proxy</html>"

    def fake_popen(argv, **kwargs):
        popen_calls.append(argv)
        return _FakeProc()

    def fake_urlopen(req, timeout=None):
        # A foreign 200 on both ports until *we* launch, then the real proxy.
        if not popen_calls:
            return _ForeignResp()
        return _Resp()

    monkeypatch.setattr(mod, "_health_opener", _FakeOpener(fake_urlopen))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    if mod._proxy_healthy(9797):
        pytest.skip(
            "body validation in _proxy_healthy has been reverted - "
            "foreign 200 responders are currently treated as healthy"
        )

    mod._start_proxy()

    assert popen_calls == [[mod._BINARY_PATH]], (
        "a foreign 200 responder must not cause a launch skip"
    )