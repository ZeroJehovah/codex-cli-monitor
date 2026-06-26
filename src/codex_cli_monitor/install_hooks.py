from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


HOOK_EVENTS = {
    "SessionStart": {
        "matcher": "*",
        "command": "session_start",
    },
    "UserPromptSubmit": {
        "command": "user_prompt_submit",
    },
    "PreToolUse": {
        "matcher": "*",
        "command": "pre_tool_use",
    },
    "PostToolUse": {
        "matcher": "*",
        "command": "post_tool_use",
    },
    "Stop": {
        "command": "stop",
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-monitor-install-hooks",
        description="Install codex-cli-monitor hooks into ~/.codex/hooks.json.",
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
    args = parser.parse_args(argv)
    hooks_path = args.codex_home.expanduser() / "hooks.json"
    install_hooks(hooks_path, args.repo_root.expanduser().resolve())
    print(f"Installed codex-cli-monitor hooks in {hooks_path}")
    print("Open /hooks in each running Codex session to review and trust new hooks.")
    return 0


def install_hooks(hooks_path: Path, repo_root: Path) -> None:
    config = _read_hooks_config(hooks_path)
    hooks = config.setdefault("hooks", {})
    for event_name, spec in HOOK_EVENTS.items():
        hooks[event_name] = _without_existing_monitor_hooks(hooks.get(event_name, []))
        entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command(repo_root, spec["command"]),
                    "timeout": 5,
                    "statusMessage": f"Recording monitor event {spec['command']}",
                }
            ]
        }
        if spec.get("matcher"):
            entry["matcher"] = spec["matcher"]
        hooks[event_name].append(entry)

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_hooks_config(path: Path) -> dict:
    if not path.exists():
        return {"hooks": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"hooks": {}}
    if not isinstance(payload, dict):
        return {"hooks": {}}
    if not isinstance(payload.get("hooks"), dict):
        payload["hooks"] = {}
    return payload


def _without_existing_monitor_hooks(entries: object) -> list:
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if _entry_is_monitor_hook(entry):
            continue
        result.append(entry)
    return result


def _entry_is_monitor_hook(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command") or "")
        if "codex_cli_monitor.hooks" in command or "codex-monitor-hook" in command:
            return True
    return False


def _hook_command(repo_root: Path, event: str) -> str:
    module_path = repo_root / "src"
    return (
        f"PYTHONPATH={_shell_quote(str(module_path))} "
        f"python3 -m codex_cli_monitor.hooks {_shell_quote(event)}"
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
