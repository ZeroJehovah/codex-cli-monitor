"""OpenCode lifecycle hook command for codex-cli-monitor.

OpenCode invokes configured hooks by passing a JSON document on the hook
command's stdin (the same transport Codex uses).  This command reads that
payload, extracts only stable lifecycle identifiers (event name, session id,
turn/session metadata, working directory, parent pid), and appends a small,
bounded record to the local OpenCode hook log.

All failures fail open: the hook must never fail, block, or steer the
OpenCode turn.  Prompt, assistant, tool-input, and tool-output bodies are
never written to the log.
"""

from __future__ import annotations

import argparse
import sys

from .opencode_state import (
    OFFICIAL_EVENT_NAMES,
    default_opencode_hook_log_path,
)
from .opencode_hook_state import append_opencode_hook_event, read_hook_payload_stdin


def main(argv: list[str] | None = None) -> int:
    try:
        _run(argv)
    except BaseException:
        # Monitoring must never change the OpenCode hook result.
        return 0
    return 0


def _run(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(
        prog="opencode-monitor-hook",
        description="Record OpenCode lifecycle hook events for codex-cli-monitor.",
    )
    parser.add_argument("event", choices=tuple(OFFICIAL_EVENT_NAMES))
    parser.add_argument("--tool", default=None)
    parser.add_argument("--ppid", type=int, default=None)
    parser.add_argument("--timestamp", type=float, default=None)
    parser.add_argument(
        "--hook-payload",
        default=None,
        help="inline JSON hook payload (used when the payload is passed as an argument)",
    )
    args = parser.parse_args(argv)

    if args.hook_payload:
        import json

        try:
            hook_payload = json.loads(args.hook_payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            hook_payload = None
    else:
        hook_payload = read_hook_payload_stdin()

    if hook_payload is None:
        return
    payload_event = hook_payload.get("hook_event_name") or hook_payload.get("event")
    if payload_event not in OFFICIAL_EVENT_NAMES.values():
        if isinstance(payload_event, str) and payload_event.startswith("session"):
            # OpenCode emits session.* events; accept the normalized mapping.
            pass
        return
    append_opencode_hook_event(
        args.event,
        tool=args.tool,
        ppid=args.ppid,
        timestamp=args.timestamp,
        hook_payload=hook_payload,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))