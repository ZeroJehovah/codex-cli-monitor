from __future__ import annotations

import argparse
import sys

from .hook_state import append_hook_event, read_hook_payload_stdin


SKIP_STDIN_PAYLOAD_EVENTS = {"pre_tool_use", "post_tool_use"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-monitor-hook",
        description="Record Codex lifecycle hook events for codex-cli-monitor.",
    )
    parser.add_argument("event")
    parser.add_argument("--tool", default=None)
    parser.add_argument("--ppid", type=int, default=None)
    parser.add_argument("--timestamp", type=float, default=None)
    args = parser.parse_args(argv)
    append_hook_event(
        args.event,
        tool=args.tool,
        ppid=args.ppid,
        timestamp=args.timestamp,
        hook_payload=_hook_payload_for_event(args.event),
    )
    return 0


def _hook_payload_for_event(event: str) -> dict | None:
    if event in SKIP_STDIN_PAYLOAD_EVENTS:
        return None
    return read_hook_payload_stdin()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
