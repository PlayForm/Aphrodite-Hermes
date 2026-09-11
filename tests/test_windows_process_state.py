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
and exercise the pre-launch health probe with urllib/Popen stubbed.
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import stat
import subprocess
import sys
import urllib.request
from contextlib import suppress
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"
_STATE_MODULE_NAME = "aphrodite_hermes._process_state"


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
    mod = _exec_shim("_aphrodite_shim_proxy")
    fake_binary = tmp_path / "aphrodite-fake-binary"
    fake_binary.write_bytes(b"")
    monkeypatch.setattr(mod, "_BINARY_PATH", str(fake_binary))
    yield mod, tmp_path
    sys.modules.pop("_aphrodite_shim_proxy", None)


def test_start_proxy_skips_launch_when_both_proxies_healthy(proxy_shim, monkeypatch, caplog):
    mod, tmp_path = proxy_shim
    probed: list[str] = []
    popen_calls: list[list[str]] = []

    def fake_urlopen(req, timeout=None):
        probed.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
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

    def fake_urlopen(req, timeout=None):
        # Nobody listening until *we* launch; healthy right after.
        if not popen_calls:
            raise ConnectionRefusedError
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
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
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append("probe"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append("popen"))

    mod._start_proxy()

    assert calls == [], "NO_AUTO_LAUNCH must short-circuit before probing or launching"
