from __future__ import annotations

import argparse
import sys

from .hook_state import append_hook_event, default_hook_log_path, read_hook_payload_stdin


OFFICIAL_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "stop": "Stop",
}


def main(argv: list[str] | None = None) -> int:
    try:
        _run(argv)
    except BaseException:
        # Monitoring must never change the Codex hook result.
        return 0
    return 0


def _run(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(
        prog="codex-monitor-hook",
        description="Record Codex lifecycle hook events for codex-cli-monitor.",
    )
    parser.add_argument("event", choices=tuple(OFFICIAL_EVENT_NAMES))
    parser.add_argument("--tool", default=None)
    parser.add_argument("--ppid", type=int, default=None)
    parser.add_argument("--timestamp", type=float, default=None)
    args = parser.parse_args(argv)
    hook_payload = read_hook_payload_stdin()
    if hook_payload is None:
        return
    payload_event = hook_payload.get("hook_event_name")
    if payload_event != OFFICIAL_EVENT_NAMES[args.event]:
        from .hook_state import _record_hook_diagnostic

        _record_hook_diagnostic(default_hook_log_path(), "event_name_mismatch")
        return
    append_hook_event(
        args.event,
        tool=args.tool,
        ppid=args.ppid,
        timestamp=args.timestamp,
        hook_payload=hook_payload,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
