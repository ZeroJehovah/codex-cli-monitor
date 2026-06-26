from __future__ import annotations

import argparse
import sys

from .hook_state import append_hook_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-monitor-hook",
        description="Record Codex lifecycle hook events for codex-cli-monitor.",
    )
    parser.add_argument("event")
    parser.add_argument("--tool", default=None)
    args = parser.parse_args(argv)
    append_hook_event(args.event, tool=args.tool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
