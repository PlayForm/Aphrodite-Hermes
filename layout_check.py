"""Self-healing layout checker for the Aphrodite Hermes plugin.

Detects and reports deviations from the canonical layout described
in layout_schema.json. The plugin calls ``check_and_heal()`` at startup.
Everything the plugin manages lives in the runtime home: the single
decision shared with the plugin shim and the Rust half (issue 40) -
``$APHRODITE_HOME`` when set (the shim exports its own resolution there),
else ``$HERMES_HOME`` + ``/aphrodite``, else ``~/.hermes/aphrodite``.
Stray plugin-source copies there are quarantined, dangling links are
reported. The <hermes-home>/plugins/aphrodite install path is Hermes-owned
and REPORT-ONLY - the plugin never creates, converts, or modifies it (it may
be a symlink or a real directory; Hermes decides).

The module is standalone: it imports only the standard library, so tests can
run it directly without Hermes or the rest of the plugin package.  It never
raises: every failure degrades to a logged warning and a report-only result.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

__all__ = ["check_and_heal"]

logger = logging.getLogger("aphrodite")

_SCHEMA_NAME = "layout_schema.json"
# The <hermes-home>/plugins/aphrodite path is Hermes-owned: the plugin never
# scans it for forbidden contents (it may be a symlink OR a real directory
# copy; Hermes decides). Everything the plugin manages lives in the runtime
# home below.
_DEFAULT_RUNTIME_FORBIDDEN = (
    "__init__.py",
    "plugin.yaml",
    "README.md",
    "download.sh",
    "download.ps1",
    "BINARY_VERSION",
    "layout_check.py",
    "layout_schema.json",
)
_PLATFORM_LIB = {
    "darwin": "libaphrodite_hermes.dylib",
    "linux": "libaphrodite_hermes.so",
    "win32": "libaphrodite_hermes.dll",
}


# --------------------------------------------------------------------------- #
# low-level helpers (all defensive: never raise)
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _same_content(a: Path, b: Path) -> bool:
    """True when a and b hold identical data (files, symlinks, or trees)."""
    try:
        if a.is_symlink() or b.is_symlink():
            if not (a.is_symlink() and b.is_symlink()):
                return False
            return os.readlink(a) == os.readlink(b)
        if a.is_dir() and b.is_dir():
            a_names = {p.name for p in a.iterdir()}
            b_names = {p.name for p in b.iterdir()}
            if a_names != b_names:
                return False
            return all(_same_content(a / name, b / name) for name in a_names)
        if a.is_file() and b.is_file():
            if a.stat().st_size != b.stat().st_size:
                return False
            return _sha256(a) == _sha256(b)
        return False
    except OSError:
        return False


def _safe_copy(src: Path, dst: Path) -> bool:
    """Copy src -> dst (file or tree, symlinks preserved); True on success."""
    try:
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _move_out(src: Path, dst: Path, dry_run: bool, action, warn) -> None:
    """Move src to dst: copy, verify byte-identical, then remove src.

    Never overwrites an existing destination.  When the destination already
    holds identical content only the stale source copy is removed; when it
    differs the source is left in place with a warning.
    """
    if dst.exists() or dst.is_symlink():
        if _same_content(src, dst):
            if dry_run:
                action(f"would remove duplicate {src} (identical copy at {dst})")
            else:
                _remove(src)
                action(f"removed duplicate {src} (identical copy at {dst})")
        else:
            warn(f"destination {dst} exists and differs from {src}; leaving {src} in place")
        return
    if dry_run:
        action(f"would move {src} -> {dst}")
        return
    if not _safe_copy(src, dst):
        warn(f"failed to copy {src} -> {dst}; source left in place")
        return
    if not _same_content(src, dst):
        _remove(dst)
        warn(f"copy of {src} failed verification; rolled back, source left in place")
        return
    _remove(src)
    action(f"moved {src} -> {dst} (copy verified)")


def _replace_symlink_with_copy(path: Path, dry_run: bool, action, warn) -> bool:
    """Replace symlink ``path`` with a real copy of its target when safe."""
    try:
        target = path.resolve()
    except OSError:
        warn(f"could not resolve symlink {path}")
        return False
    if not target.exists():
        warn(f"symlink {path} -> {target} is dangling; left as-is")
        return False
    if dry_run:
        action(f"would replace symlink {path} (-> {target}) with a verified copy")
        return True
    tmp = path.parent / f".{path.name}.layout-heal-tmp"
    try:
        _remove(tmp)
        if target.is_dir():
            shutil.copytree(target, tmp, symlinks=True)
        else:
            shutil.copy2(target, tmp)
        if not _same_content(tmp, target):
            raise OSError("copy verification failed")
        os.replace(tmp, path)
    except OSError as exc:
        _remove(tmp)
        warn(f"could not replace symlink {path}: {exc}")
        return False
    action(f"replaced symlink {path} (-> {target}) with a verified copy")
    return True


def _load_schema() -> dict | None:
    """Load layout_schema.json next to this module; None on any failure."""
    schema_path = Path(__file__).resolve().parent / _SCHEMA_NAME
    try:
        with open(schema_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
            raise ValueError("schema must be an object with a 'paths' mapping")
        return data
    except (OSError, ValueError) as exc:
        logger.warning("aphrodite layout: cannot load %s: %s", schema_path, exc)
        return None


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def check_and_heal(home_dir=None, dry_run=False, plugin_dir=None) -> dict:
    """Compare ~/.hermes against layout_schema.json and repair deviations.

    Parameters
    ----------
    home_dir : path-like or None
        The user home root (defaults to ``Path.home()``).  ``~/.hermes`` is
        resolved under it, which lets tests point at a fake HOME tree.
    dry_run : bool
        When True, report mismatches and planned actions without touching
        the filesystem.
    plugin_dir : path-like or None
        The plugin's real location (defaults to the directory containing this
        module).  Used to build the plugin symlink; the plugin may itself be
        a symlink, so the resolved path is authoritative.

    Returns
    -------
    dict
        A report with keys: ``schema_version``, ``dry_run``, ``home_dir``,
        ``checks`` (list of {"name", "status", "detail"}), ``mismatches``,
        ``actions_taken``, ``warnings``.  Never raises; failures are logged
        via ``logging.getLogger("aphrodite")`` and reported as warnings.
    """
    report = {
        "schema_version": None,
        "dry_run": bool(dry_run),
        "home_dir": str(Path(home_dir).expanduser() if home_dir else Path.home()),
        "checks": [],
        "mismatches": [],
        "actions_taken": [],
        "warnings": [],
    }

    def _check(name, status, detail=""):
        report["checks"].append({"name": name, "status": status, "detail": detail})
        if status == "mismatch":
            report["mismatches"].append(detail or name)

    def _warn(message):
        report["warnings"].append(message)
        logger.warning("aphrodite layout: %s", message)

    def _action(message):
        report["actions_taken"].append(message)
        logger.warning("aphrodite layout action: %s", message)

    schema = _load_schema()
    if schema is None:
        _check("schema", "skipped", "layout_schema.json missing or malformed")
        _warn("layout schema unavailable; self-heal disabled (report-only)")
        return report
    report["schema_version"] = schema.get("schema_version")

    try:
        # Hermes home: $HERMES_HOME when set (expanded), else ~/.hermes -
        # never hardcode Path.home()/".hermes" (catalog validate runs the
        # probe against a scratch HERMES_HOME; a heal that resolves the real
        # ~/.hermes from there rewrites the user's actual install layout).
        if home_dir is not None:
            hermes_root = Path(home_dir).expanduser() / ".hermes"
        else:
            env_home = os.environ.get("HERMES_HOME", "").strip()
            hermes_root = Path(env_home).expanduser() if env_home else Path.home() / ".hermes"
        # Runtime home: the SAME single decision the plugin shim makes (issue
        # 40 F3). The shim exports its resolution through APHRODITE_HOME
        # (env setdefault at import), so honoring it here keeps the heal
        # consistent with what the plugin actually uses - a user who works
        # around the home mismatch with APHRODITE_HOME never gets required
        # dirs (re)created under a second, shadow home.
        env_runtime = os.environ.get("APHRODITE_HOME", "").strip()
        if env_runtime and not Path(env_runtime).expanduser().is_absolute():
            # Both halves resolve a relative override the same way
            # (cwd-relative), so it still agrees - but it is almost always a
            # mistake; surface it so the heal never silently operates on an
            # unexpected location.
            _warn(
                f"APHRODITE_HOME={env_runtime!r} is not absolute; resolving it "
                "relative to the current directory"
            )
        runtime_home = Path(env_runtime).expanduser() if env_runtime else hermes_root / "aphrodite"
        plugin_link = hermes_root / "plugins" / "aphrodite"
        backup_dir = runtime_home / ".stale-backup"

        # --- plugin real location (plugin may itself be a symlink) ----------- #
        plugin_real = None
        if plugin_dir is not None:
            plugin_real = Path(plugin_dir).expanduser().resolve()
        elif plugin_link.is_symlink():
            resolved_link = plugin_link.resolve()
            plugin_real = (
                resolved_link if resolved_link.is_dir() else Path(__file__).resolve().parent
            )
        else:
            plugin_real = Path(__file__).resolve().parent

        if plugin_real is not None and plugin_real.is_dir():
            _check("plugin_location", "ok", str(plugin_real))
        else:
            _check("plugin_location", "mismatch", f"plugin real path unavailable: {plugin_real}")
            _warn(f"cannot resolve plugin real path ({plugin_real}); layout heal limited")

        # --- repo root & dev-checkout detection ------------------------------ #
        repo_root = None
        if plugin_real is not None:
            for candidate in (plugin_real.parent.parent, plugin_real.parent):
                if (candidate / "crates").is_dir():
                    repo_root = candidate
                    break
        checkout = bool(
            repo_root is not None
            and ((repo_root / ".git").exists() or (repo_root / "crates").is_dir())
        )
        if checkout:
            _warn(
                "plugin resolves into the repo checkout; misplaced files are "
                "reported but NOT moved (developers manage the checkout)"
            )

        # --- env overrides --------------------------------------------------- #
        env_config = os.environ.get("APHRODITE_CONFIG_PATH")
        env_binary = os.environ.get("APHRODITE_BINARY_PATH")
        env_dylib = os.environ.get("APHRODITE_HERMES_DYLIB_PATH")
        platform_lib = _PLATFORM_LIB.get(sys.platform)
        config_canonical = (
            Path(env_config).expanduser() if env_config else runtime_home / "aphrodite.toml"
        )

        # --- plugin scan dir (what the runtime plugin path actually is) ------ #
        if plugin_link.is_symlink():
            resolved_link = plugin_link.resolve()
            plugin_scan_dir = resolved_link if resolved_link.is_dir() else plugin_link
        elif plugin_link.is_dir():
            plugin_scan_dir = plugin_link
        else:
            plugin_scan_dir = plugin_real if plugin_real is not None else hermes_root

        # --- required directories ------------------------------------------- #
        for path_key, spec in (schema.get("paths") or {}).items():
            rel = path_key[len("~/.hermes/") :] if path_key.startswith("~/.hermes/") else path_key
            if rel.endswith("aphrodite-*") or spec.get("kind") != "dir":
                continue
            # Schema keys use ~/.hermes/... notation; entries under
            # ~/.hermes/aphrodite/ are runtime-home entries and follow the
            # single runtime-home decision (APHRODITE_HOME when set), so the
            # heal never creates required dirs under a shadow home (issue 40
            # F3).
            if rel == "aphrodite" or rel.startswith("aphrodite/"):
                actual = runtime_home / rel[len("aphrodite") :].lstrip("/")
            else:
                actual = hermes_root / rel if rel else hermes_root
            if actual.is_dir():
                _check(f"dir:{rel}", "ok", str(actual))
            elif actual.exists():
                _check(f"dir:{rel}", "mismatch", f"expected a directory but found {actual}")
                _warn(f"{actual} is not a directory; left as-is")
            elif spec.get("required"):
                _check(f"dir:{rel}", "mismatch", f"required directory missing: {actual}")
                if dry_run:
                    _action(f"would create directory {actual}")
                else:
                    try:
                        actual.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        _warn(f"failed to create directory {actual}")
                    else:
                        _action(f"created directory {actual}")
            else:
                _check(f"dir:{rel}", "ok", f"optional directory absent: {actual}")

        # --- stale runtime symlinks resolving into the plugin dir ------------ #
        binary_paths = []
        if env_binary:
            binary_paths.append(("binary", Path(env_binary).expanduser()))
        else:
            binary_paths.append(("binary", runtime_home / "binaries" / "aphrodite"))
        if env_dylib:
            binary_paths.append(("dylib", Path(env_dylib).expanduser()))
        elif platform_lib:
            binary_paths.append(("dylib", runtime_home / "binaries" / platform_lib))

        for label, bpath in binary_paths:
            if not bpath.is_symlink():
                _check(f"binary:{label}", "ok", str(bpath))
                continue
            try:
                target = bpath.resolve()
            except OSError:
                target = None
            if target is None or not _inside(target, plugin_scan_dir):
                _check(f"binary:{label}", "ok", str(bpath))
                continue
            _check(
                f"binary:{label}",
                "mismatch",
                f"{bpath} resolves into the plugin dir ({target})",
            )
            if not _replace_symlink_with_copy(bpath, dry_run, _action, _warn):
                _warn(f"could not replace stale symlink {bpath} with a real copy; left as-is")

        # --- stray plugin-source files inside the runtime home --------------- #
        for name in schema.get("runtime_home_forbidden") or _DEFAULT_RUNTIME_FORBIDDEN:
            stray = runtime_home / name
            if not (stray.exists() or stray.is_symlink()):
                _check(f"runtime_home_forbidden:{name}", "ok", f"no stray '{name}' in runtime home")
                continue
            _check(
                f"runtime_home_forbidden:{name}",
                "mismatch",
                f"stray plugin-source file in runtime home: {stray}",
            )
            if stray.is_symlink() or stray.is_dir():
                _warn(f"{stray} is not a regular file; left as-is (ambiguous)")
                continue
            if plugin_real is None:
                _warn(f"cannot compare {stray} to a tracked plugin file; left as-is (ambiguous)")
                continue
            tracked = plugin_real / name
            if not (tracked.is_file() and not tracked.is_symlink()):
                _warn(
                    f"cannot compare {stray} to tracked plugin file {tracked}; left as-is (ambiguous)"
                )
                continue
            # Content verifies as an old or current plugin-source copy: the
            # only safe reads are quarantine (never hard-delete user data).
            _move_out(stray, backup_dir / name, dry_run, _action, _warn)

        # --- <hermes-home>/plugins/aphrodite (REPORT-ONLY) ----------------- #
        # The plugin must never rewrite the install layout under
        # <hermes-home>/plugins/ at register time - that directory is owned
        # by Hermes (plugin install/remove/enable). `hermes plugins validate`
        # runs the probe against a scratch HERMES_HOME and a heal that
        # creates/converts a symlink there rewrites the real install layout
        # (teknium review, PR 118488). Report state only; never create,
        # convert, or delete anything under plugins/.
        if plugin_link.is_symlink():
            resolved_link = plugin_link.resolve()
            if resolved_link.is_dir():
                if plugin_real is not None and resolved_link != plugin_real:
                    _warn(
                        f"plugin link {plugin_link} -> {resolved_link} differs from "
                        f"expected {plugin_real}; left as-is"
                    )
                _check("plugin_link", "ok", f"{plugin_link} -> {resolved_link}")
            else:
                _check(
                    "plugin_link",
                    "mismatch",
                    f"plugin link {plugin_link} is dangling (-> {resolved_link})",
                )
                _warn(f"dangling plugin link {plugin_link} left as-is (ambiguous)")
        elif plugin_link.exists():
            _check(
                "plugin_link",
                "ok",
                f"plugin path {plugin_link} is a real directory (installed layout; "
                "report-only, never converted)",
            )
        else:
            _check(
                "plugin_link",
                "ok",
                f"no plugin path at {plugin_link} (report-only; install layout is "
                "managed by Hermes, not the plugin)",
            )

        # --- config presence ------------------------------------------------- #
        config_present = config_canonical.is_file() or config_canonical.is_symlink()
        if env_config:
            _check(
                "config_present",
                "ok" if config_present else "mismatch",
                f"config at APHRODITE_CONFIG_PATH={config_canonical}"
                if config_present
                else f"config missing at APHRODITE_CONFIG_PATH={config_canonical}",
            )
        elif config_present:
            _check("config_present", "ok", f"config at {config_canonical}")
        else:
            _check(
                "config_present",
                "mismatch",
                f"aphrodite.toml missing from runtime home {runtime_home}",
            )
            _warn(
                "aphrodite.toml not found in runtime home and APHRODITE_CONFIG_PATH "
                "unset; plugin may need a fresh download or a user copy"
            )

        if dry_run:
            _warn(
                f"dry run: no changes made; {len(report['mismatches'])} mismatch(es) reported above"
            )
    except Exception as exc:  # defensive: layout heal must never crash the plugin
        logger.warning("aphrodite layout: check_and_heal aborted: %r", exc)
        _warn(f"check_and_heal aborted unexpectedly: {exc!r}; report only")

    return report
