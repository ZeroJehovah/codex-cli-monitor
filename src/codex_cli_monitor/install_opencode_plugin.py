"""Install, check, or uninstall the optional OpenCode decision plugin.

OpenCode auto-discovers plugins from ``<config dir>/plugin/*.js``.  This
installer copies exactly one file there,
``assets/opencode/codex-monitor-decisions.js``, which subscribes to OpenCode's
observational ``event`` stream and appends bounded JSONL markers when a
permission or question prompt opens and when it is answered.  Without it the
monitor cannot tell a working OpenCode session apart from one that has stopped
and is waiting for the user to choose, because OpenCode keeps pending prompts
in memory only.

The installer is as conservative as the hook installer:

* nothing outside its own single plugin file is ever created, modified, or
  removed, and the OpenCode binary and its installed packages stay untouched;
* an existing file that is not the monitor plugin is never overwritten;
* an unchanged plugin is not rewritten;
* updates are written atomically through a temporary file in the same
  directory;
* uninstalling removes only the monitor's own plugin file.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


PLUGIN_MARKER = "codex-cli-monitor OpenCode plugin"
PLUGIN_FILE_NAME = "codex-monitor-decisions.js"
PLUGIN_DIR_NAME = "plugin"
PLUGIN_ASSET = Path("assets") / "opencode" / PLUGIN_FILE_NAME

DEFAULT_OPENCODE_CONFIG = Path.home() / ".config" / "opencode"


class OpenCodePluginError(ValueError):
    pass


@dataclass(frozen=True)
class OpenCodePluginResult:
    changed: bool
    action: str
    path: Path


@dataclass(frozen=True)
class OpenCodePluginCheck:
    installed: bool
    current: bool
    foreign_file: bool
    source_valid: bool
    path: Path
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "current": self.current,
            "foreign_file": self.foreign_file,
            "source_valid": self.source_valid,
            "path": str(self.path),
            "detail": self.detail,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opencode-monitor-install-plugin",
        description=(
            "Install, check, or uninstall the codex-cli-monitor OpenCode "
            "decision plugin."
        ),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_OPENCODE_CONFIG,
        help=f"OpenCode config directory, defaults to {DEFAULT_OPENCODE_CONFIG}",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="codex-cli-monitor repository root",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true", help="check plugin installation")
    operation.add_argument(
        "--uninstall",
        action="store_true",
        help="remove only the codex-cli-monitor OpenCode plugin",
    )
    args = parser.parse_args(argv)
    config_dir = args.config_dir.expanduser()
    repo_root = args.repo_root.expanduser().resolve()

    try:
        if args.check:
            result = check_plugin(config_dir, repo_root)
            _print_check(result)
            return 0 if result.current else 1
        outcome = (
            uninstall_plugin(config_dir)
            if args.uninstall
            else install_plugin(config_dir, repo_root)
        )
    except OpenCodePluginError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: could not update the OpenCode plugin: {error}", file=sys.stderr)
        return 2

    if outcome.changed:
        print(f"{outcome.action.capitalize()} OpenCode decision plugin at {outcome.path}")
        print("Restart OpenCode to load the plugin.")
    else:
        print(f"OpenCode decision plugin is already {outcome.action} at {outcome.path}")
    return 0


def plugin_path(config_dir: Path) -> Path:
    return config_dir / PLUGIN_DIR_NAME / PLUGIN_FILE_NAME


def plugin_source(repo_root: Path) -> Path:
    return repo_root / PLUGIN_ASSET


def install_plugin(config_dir: Path, repo_root: Path) -> OpenCodePluginResult:
    source = plugin_source(repo_root)
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise OpenCodePluginError(f"cannot read plugin source {source}: {error}") from error
    if PLUGIN_MARKER.encode() not in payload:
        raise OpenCodePluginError(f"{source} is not the codex-cli-monitor OpenCode plugin")

    destination = plugin_path(config_dir)
    existing = _read_existing(destination)
    if existing is not None and not _is_monitor_plugin(existing):
        raise OpenCodePluginError(
            f"refusing to overwrite {destination}: it was not written by this monitor"
        )
    if existing == payload:
        return OpenCodePluginResult(False, "installed", destination)
    _write_atomic(destination, payload)
    return OpenCodePluginResult(True, "installed", destination)


def uninstall_plugin(config_dir: Path) -> OpenCodePluginResult:
    destination = plugin_path(config_dir)
    existing = _read_existing(destination)
    if existing is None:
        return OpenCodePluginResult(False, "uninstalled", destination)
    if not _is_monitor_plugin(existing):
        raise OpenCodePluginError(
            f"refusing to remove {destination}: it was not written by this monitor"
        )
    destination.unlink()
    _fsync_directory(destination.parent)
    return OpenCodePluginResult(True, "uninstalled", destination)


def check_plugin(config_dir: Path, repo_root: Path) -> OpenCodePluginCheck:
    destination = plugin_path(config_dir)
    source = plugin_source(repo_root)
    try:
        expected = source.read_bytes()
        source_valid = PLUGIN_MARKER.encode() in expected
    except OSError:
        expected = None
        source_valid = False
    existing = _read_existing(destination)
    if existing is None:
        return OpenCodePluginCheck(
            False, False, False, source_valid, destination, "not installed"
        )
    if not _is_monitor_plugin(existing):
        return OpenCodePluginCheck(
            False, False, True, source_valid, destination, "foreign file present"
        )
    if not source_valid or expected is None:
        return OpenCodePluginCheck(
            True, False, False, False, destination, "plugin source missing"
        )
    if existing != expected:
        return OpenCodePluginCheck(
            True, False, False, True, destination, "out of date"
        )
    return OpenCodePluginCheck(True, True, False, True, destination, "current")


def _read_existing(path: Path) -> bytes | None:
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _is_monitor_plugin(payload: bytes) -> bool:
    return PLUGIN_MARKER.encode() in payload


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _print_check(result: OpenCodePluginCheck) -> None:
    print(f"OpenCode decision plugin: {result.path}")
    print(f"Status: {result.detail}")
    if result.foreign_file:
        print("A file with that name exists but was not written by this monitor.")
    if not result.source_valid:
        print("Monitor OpenCode plugin asset is missing or unrecognized.")
    if result.current:
        print("Restart OpenCode if it was running before the plugin was installed.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
