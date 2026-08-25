from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MONITOR_MARKER = "CODEX_CLI_MONITOR_HOOK=1"
DEFAULT_HOOK_EVENTS = {
    "UserPromptSubmit": {"command": "user_prompt_submit"},
    # PermissionRequest is the only signal that tells a resident observer Codex
    # has stopped and is waiting for the user to approve something; the rollout
    # file cannot distinguish an approval prompt from a running command.  The
    # hook writes nothing to stdout, which Codex reads as "no decision", so the
    # approval prompt behaves exactly as it would without the hook installed.
    "PermissionRequest": {"command": "permission_request"},
    "Stop": {"command": "stop"},
}
LEGACY_HOOK_EVENTS = {
    "SessionStart": {"matcher": "*", "command": "session_start"},
}
TOOL_HOOK_EVENTS = {
    "PreToolUse": {"matcher": "*", "command": "pre_tool_use"},
    "PostToolUse": {"matcher": "*", "command": "post_tool_use"},
}
ALL_HOOK_EVENTS = {
    **DEFAULT_HOOK_EVENTS,
    **LEGACY_HOOK_EVENTS,
    **TOOL_HOOK_EVENTS,
}


class HooksConfigError(ValueError):
    pass


@dataclass(frozen=True)
class InstallResult:
    changed: bool
    action: str
    installed_events: tuple[str, ...]
    removed_events: tuple[str, ...]
    backup_path: Path | None = None


@dataclass(frozen=True)
class CheckResult:
    valid: bool
    installed: bool
    current: bool
    hooks_disabled: bool
    installed_events: tuple[str, ...]
    missing_events: tuple[str, ...]
    unexpected_events: tuple[str, ...]
    stale_events: tuple[str, ...]
    command_path_valid: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "installed": self.installed,
            "current": self.current,
            "hooks_disabled": self.hooks_disabled,
            "installed_events": list(self.installed_events),
            "missing_events": list(self.missing_events),
            "unexpected_events": list(self.unexpected_events),
            "stale_events": list(self.stale_events),
            "command_path_valid": self.command_path_valid,
            "trust_state": "unknown",
            "trust_review_command": "/hooks",
            "detail": self.detail,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-monitor-install-hooks",
        description="Install, check, or uninstall codex-cli-monitor hooks.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
        help="Codex home directory, defaults to ~/.codex",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="codex-cli-monitor repository root",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true", help="check hook installation")
    operation.add_argument(
        "--uninstall", action="store_true", help="remove only codex-cli-monitor hooks"
    )
    parser.add_argument(
        "--include-tool-events",
        action="store_true",
        help="also install PreToolUse/PostToolUse diagnostic hooks",
    )
    args = parser.parse_args(argv)
    codex_home = args.codex_home.expanduser()
    hooks_path = codex_home / "hooks.json"
    repo_root = args.repo_root.expanduser().resolve()

    try:
        if args.check:
            result = check_hooks(
                hooks_path,
                repo_root,
                include_tool_events=args.include_tool_events,
                config_path=codex_home / "config.toml",
            )
            _print_check(result, hooks_path)
            return 0 if result.valid and result.current and not result.hooks_disabled else 1
        result = (
            uninstall_hooks(hooks_path)
            if args.uninstall
            else install_hooks(
                hooks_path,
                repo_root,
                include_tool_events=args.include_tool_events,
            )
        )
    except HooksConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: could not update {hooks_path}: {error}", file=sys.stderr)
        return 2

    if result.changed:
        print(f"{result.action.capitalize()} codex-cli-monitor hooks in {hooks_path}")
        if result.backup_path is not None:
            print(f"Backup: {result.backup_path}")
        print("Hook configuration changed; open /hooks to review and trust it.")
    else:
        print(f"Codex-cli-monitor hooks are already {result.action} in {hooks_path}")
    if result.installed_events:
        print("Installed events: " + ", ".join(result.installed_events))
    if result.removed_events:
        print("Removed events: " + ", ".join(result.removed_events))
    return 0


def install_hooks(
    hooks_path: Path,
    repo_root: Path,
    *,
    include_tool_events: bool = False,
) -> InstallResult:
    original, existed = _read_hooks_config(hooks_path)
    config = copy.deepcopy(original)
    hooks = config.setdefault("hooks", {})
    selected = dict(DEFAULT_HOOK_EVENTS)
    if include_tool_events:
        selected.update(TOOL_HOOK_EVENTS)

    removed = []
    for event_name in ALL_HOOK_EVENTS:
        entries, count = _without_existing_monitor_hooks(hooks.get(event_name, []))
        if count:
            removed.append(event_name)
        if entries:
            hooks[event_name] = entries
        else:
            hooks.pop(event_name, None)

    for event_name, spec in selected.items():
        entry: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command(repo_root, spec["command"]),
                    "timeout": 5,
                }
            ]
        }
        if spec.get("matcher"):
            entry["matcher"] = spec["matcher"]
        hooks.setdefault(event_name, []).append(entry)

    changed, backup = _write_if_changed(hooks_path, original, config, existed)
    removed_only = tuple(sorted(set(removed) - set(selected)))
    return InstallResult(
        changed=changed,
        action="installed",
        installed_events=tuple(selected),
        removed_events=removed_only,
        backup_path=backup,
    )


def uninstall_hooks(hooks_path: Path) -> InstallResult:
    original, existed = _read_hooks_config(hooks_path)
    if not existed:
        return InstallResult(False, "uninstalled", (), ())
    config = copy.deepcopy(original)
    hooks = config.get("hooks", {})
    removed = []
    for event_name in tuple(hooks):
        entries, count = _without_existing_monitor_hooks(hooks[event_name])
        if count:
            removed.append(event_name)
        if entries:
            hooks[event_name] = entries
        else:
            hooks.pop(event_name, None)
    if not hooks:
        config.pop("hooks", None)
    changed, backup = _write_if_changed(hooks_path, original, config, existed)
    return InstallResult(changed, "uninstalled", (), tuple(sorted(removed)), backup)


def check_hooks(
    hooks_path: Path,
    repo_root: Path,
    *,
    include_tool_events: bool = False,
    config_path: Path | None = None,
) -> CheckResult:
    try:
        config, existed = _read_hooks_config(hooks_path)
    except HooksConfigError as error:
        return CheckResult(False, False, False, False, (), (), (), (), False, str(error))
    expected = dict(DEFAULT_HOOK_EVENTS)
    if include_tool_events:
        expected.update(TOOL_HOOK_EVENTS)
    expected_commands = {
        name: _hook_command(repo_root, spec["command"]) for name, spec in expected.items()
    }
    installed: list[str] = []
    stale: list[str] = []
    unexpected: list[str] = []
    hooks = config.get("hooks", {})
    for event_name, entries in hooks.items():
        commands = _monitor_commands(entries)
        if not commands:
            continue
        installed.append(event_name)
        if event_name not in expected:
            unexpected.append(event_name)
        elif commands != [expected_commands[event_name]]:
            stale.append(event_name)
    missing = sorted(set(expected) - set(installed))
    disabled = _hooks_explicitly_disabled(config_path or hooks_path.with_name("config.toml"))
    command_path_valid = (repo_root / "src" / "codex_cli_monitor" / "hooks.py").is_file()
    current = existed and not missing and not unexpected and not stale and command_path_valid
    detail = (
        "current"
        if current
        else "command path missing"
        if installed and not command_path_valid
        else "not installed"
        if not installed
        else "out of date"
    )
    return CheckResult(
        True,
        bool(installed),
        current,
        disabled,
        tuple(sorted(installed)),
        tuple(missing),
        tuple(sorted(unexpected)),
        tuple(sorted(stale)),
        command_path_valid,
        detail,
    )


def _read_hooks_config(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {"hooks": {}}, False
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise HooksConfigError(f"cannot read {path}: {error}") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HooksConfigError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(payload, dict):
        raise HooksConfigError(f"{path} root must be a JSON object")
    if "hooks" not in payload:
        return payload, True
    hooks = payload["hooks"]
    if not isinstance(hooks, dict):
        raise HooksConfigError(f"{path} hooks must be a JSON object")
    for event_name, entries in hooks.items():
        if not isinstance(event_name, str) or not isinstance(entries, list):
            raise HooksConfigError(f"{path} contains an invalid hook event entry")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise HooksConfigError(f"{path} contains an invalid {event_name} group")
            if not all(isinstance(handler, dict) for handler in entry["hooks"]):
                raise HooksConfigError(f"{path} contains an invalid {event_name} handler")
    return payload, True


def _without_existing_monitor_hooks(entries: object) -> tuple[list[Any], int]:
    if not isinstance(entries, list):
        raise HooksConfigError("hook event entries must be a JSON array")
    result: list[Any] = []
    removed = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            raise HooksConfigError("hook groups must contain a hooks array")
        kept_handlers = []
        group_removed = 0
        for handler in entry["hooks"]:
            if _handler_is_monitor_hook(handler):
                removed += 1
                group_removed += 1
            else:
                kept_handlers.append(handler)
        if kept_handlers or group_removed == 0:
            updated = copy.deepcopy(entry)
            updated["hooks"] = kept_handlers
            result.append(updated)
    return result, removed


def _handler_is_monitor_hook(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command") or "")
    return (
        MONITOR_MARKER in command
        or "codex_cli_monitor.hooks" in command
        or "codex-monitor-hook" in command
    )


def _monitor_commands(entries: object) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [
        str(handler.get("command") or "")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
        for handler in entry["hooks"]
        if _handler_is_monitor_hook(handler)
    ]


def _write_if_changed(
    path: Path,
    original: dict[str, Any],
    updated: dict[str, Any],
    existed: bool,
) -> tuple[bool, Path | None]:
    if original == updated and existed:
        return False, None
    data = (json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if existed:
        backup_path = path.with_name(path.name + ".bak")
        _backup_file(path, backup_path)
    mode = stat.S_IMODE(path.stat().st_mode) if existed else 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
    return True, backup_path


def _backup_file(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hooks_explicitly_disabled(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    features = payload.get("features")
    return isinstance(features, dict) and features.get("hooks") is False


def _hook_command(repo_root: Path, event: str) -> str:
    module_path = repo_root / "src"
    return (
        f"{MONITOR_MARKER} PYTHONPATH={_shell_quote(str(module_path))} "
        f"python3 -S -m codex_cli_monitor.hooks {_shell_quote(event)} "
        '--ppid "$PPID" || true'
    )


def _hook_command_windows(repo_root: Path, event: str) -> str:
    module_path = repo_root / "src"
    return (
        f'set "{MONITOR_MARKER}" && set "PYTHONPATH={module_path}" && '
        f'py -3 -S -m codex_cli_monitor.hooks "{event}"'
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _print_check(result: CheckResult, hooks_path: Path) -> None:
    print(f"Hook config: {hooks_path}")
    print(f"Status: {result.detail}")
    print("Installed events: " + (", ".join(result.installed_events) or "none"))
    if result.missing_events:
        print("Missing events: " + ", ".join(result.missing_events))
    if result.unexpected_events:
        print("Unexpected monitor events: " + ", ".join(result.unexpected_events))
    if result.stale_events:
        print("Stale commands: " + ", ".join(result.stale_events))
    if not result.command_path_valid:
        print("Monitor hook module path does not exist")
    if result.hooks_disabled:
        print("Codex hooks are explicitly disabled by [features].hooks = false")
    if result.current and not result.hooks_disabled:
        print("Open /hooks in Codex to confirm these commands are trusted.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
