"""Self-healing layout checker for the Aphrodite Hermes plugin.

Detects and repairs deviations from the canonical ~/.hermes layout described
in layout_schema.json. The plugin calls ``check_and_heal()`` at startup so a
broken install (wrong symlinks, misplaced config/binaries, stray plugin-source
copies, dangling links, stale copies) is fixed automatically.

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
_DEFAULT_FORBIDDEN = ("binaries", "aphrodite.toml", "ccr.db")
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


def _ensure_symlink(link: Path, target: Path, dry_run: bool, action, warn, what: str) -> str:
    """Make ``link`` a symlink to ``target`` where safe.

    Returns "ok" | "created" | "skipped".  Never clobbers non-empty real
    directories or replaces unrelated symlinks.
    """
    if link.is_symlink():
        try:
            resolved = link.resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved.is_dir():
            if resolved != target.resolve():
                warn(f"{what} {link} -> {resolved} differs from expected {target}; left as-is")
            return "ok"
        warn(f"{what} {link} is a dangling symlink (-> {resolved}); left as-is")
        return "skipped"
    if link.exists():
        if link.is_dir():
            try:
                has_children = any(True for _ in link.iterdir())
            except OSError:
                has_children = True
            if has_children:
                warn(f"{what} {link} is a non-empty directory; left as-is (user data)")
                return "skipped"
            if dry_run:
                action(f"would remove empty directory {link}")
            else:
                try:
                    link.rmdir()
                except OSError:
                    warn(f"could not remove empty directory {link}; left as-is")
                    return "skipped"
                action(f"removed empty directory {link}")
        else:
            warn(f"{what} {link} exists and is not a directory; left as-is")
            return "skipped"
    if dry_run:
        action(f"would create symlink {link} -> {target}")
        return "created"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    except OSError:
        warn(f"failed to create symlink {link} -> {target}")
        return "skipped"
    action(f"created symlink {link} -> {target}")
    return "created"


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


def _binary_override_moves(forbidden: Path, env_binary, env_dylib, platform_lib):
    """Return (src, dst) pairs to move out of the plugin binaries dir."""
    moves = []
    if env_binary:
        src = forbidden / "aphrodite"
        if src.exists() and not src.is_symlink():
            moves.append((src, Path(env_binary).expanduser()))
    if env_dylib and platform_lib:
        src = forbidden / platform_lib
        if src.exists() and not src.is_symlink():
            moves.append((src, Path(env_dylib).expanduser()))
    return moves


def _has_newer_than_canonical(src_dir: Path, canonical_dir: Path) -> bool:
    """True when a regular file under src_dir is newer than its canonical copy.

    Used to protect mid-refactor binaries: a NEWER-than-canonical binary in
    the plugin dir is warn-and-skip, never moved or deleted.
    """
    if not canonical_dir.is_dir():
        return False
    try:
        for child in src_dir.iterdir():
            if child.is_dir():
                if _has_newer_than_canonical(child, canonical_dir / child.name):
                    return True
            elif child.is_file() and not child.is_symlink():
                canon = canonical_dir / child.name
                if (
                    canon.is_file()
                    and not canon.is_symlink()
                    and child.stat().st_mtime > canon.stat().st_mtime
                ):
                    return True
    except OSError:
        pass
    return False


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
        hermes_root = (Path(home_dir).expanduser() if home_dir else Path.home()) / ".hermes"
        runtime_home = hermes_root / "aphrodite"
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

        # --- forbidden contents inside the plugin dir ------------------------ #
        for name in schema.get("plugin_dir_forbidden") or _DEFAULT_FORBIDDEN:
            forbidden = plugin_scan_dir / name
            if not (forbidden.exists() or forbidden.is_symlink()):
                _check(f"plugin_forbidden:{name}", "ok", f"no '{name}' inside plugin dir")
                continue
            _check(
                f"plugin_forbidden:{name}",
                "mismatch",
                f"forbidden '{name}' present inside plugin dir: {forbidden}",
            )
            if checkout:
                _warn(f"{forbidden} not moved: plugin dir is a git checkout")
                continue
            if forbidden.is_symlink():
                _warn(f"{forbidden} is a symlink; not moved (ambiguous)")
                continue
            if name == "binaries":
                if _has_newer_than_canonical(forbidden, runtime_home / "binaries"):
                    _warn(
                        f"{forbidden} contains a binary newer than the canonical runtime "
                        "copy; not moved (warn+skip, mid-refactor protection)"
                    )
                    continue
                if env_binary or env_dylib:
                    moves = _binary_override_moves(forbidden, env_binary, env_dylib, platform_lib)
                    if not moves:
                        _warn(f"{forbidden} holds no env-override binaries; left in place")
                    for src, dst in moves:
                        _move_out(src, dst, dry_run, _action, _warn)
                    if not dry_run:
                        leftovers = [
                            c.name for c in forbidden.iterdir() if c.exists() or c.is_symlink()
                        ]
                        if leftovers:
                            _warn(
                                f"binaries left in plugin dir after env-override moves: {leftovers}"
                            )
                        else:
                            _remove(forbidden)
                            _action(f"removed emptied binaries dir {forbidden}")
                else:
                    _move_out(forbidden, runtime_home / "binaries", dry_run, _action, _warn)
            elif name == "aphrodite.toml":
                _move_out(forbidden, config_canonical, dry_run, _action, _warn)
            else:
                _move_out(forbidden, runtime_home / name, dry_run, _action, _warn)

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

        # --- ~/.hermes/plugins/aphrodite ------------------------------------ #
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
                "mismatch",
                f"plugin path {plugin_link} is a real directory, not a symlink",
            )
            if plugin_real is None or not plugin_real.is_dir():
                _warn(f"cannot convert {plugin_link} to a symlink: plugin real path unavailable")
            elif (
                _ensure_symlink(plugin_link, plugin_real, dry_run, _action, _warn, "plugin path")
                == "skipped"
            ):
                _warn(f"plugin path {plugin_link} left as-is (non-empty or in use)")
        else:
            _check("plugin_link", "mismatch", f"missing plugin symlink {plugin_link}")
            if plugin_real is not None and plugin_real.is_dir():
                _ensure_symlink(plugin_link, plugin_real, dry_run, _action, _warn, "plugin path")
            else:
                _warn("plugin symlink not created: plugin real path unavailable")

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
