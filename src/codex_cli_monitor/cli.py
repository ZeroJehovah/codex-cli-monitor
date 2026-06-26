from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from .monitor import discover_sessions


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    while True:
        sessions = discover_sessions(
            proc_root=args.proc_root,
            sample_window=args.sample_window,
            shim_log=args.shim_log,
        )
        if args.json:
            payload = {
                "session_count": len(sessions),
                "sessions": [session.to_dict() for session in sessions],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_table(sessions)

        if args.watch is None:
            return 0
        time.sleep(args.watch)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-monitor",
        description="Observe local Codex CLI sessions without modifying Codex.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    parser.add_argument(
        "--proc-root",
        type=Path,
        default=Path("/proc"),
        help="procfs root to scan, defaults to /proc",
    )
    parser.add_argument(
        "--sample-window",
        type=_non_negative_float,
        default=0.25,
        help="seconds between CPU samples; use 0 for a single snapshot",
    )
    parser.add_argument(
        "--shim-log",
        type=Path,
        default=None,
        help="path to shim launch JSONL metadata",
    )
    parser.add_argument(
        "--watch",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="refresh repeatedly at the given interval",
    )
    return parser


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _print_table(sessions: tuple) -> None:
    if not sessions:
        print("No open Codex CLI sessions found.")
        return

    rows = []
    for session in sessions:
        root = session.root
        inference = session.inference
        rows.append(
            {
                "PID": str(root.pid),
                "STATUS": inference.status,
                "CONF": f"{inference.confidence:.2f}",
                "ELAPSED": _format_duration(root.elapsed_seconds),
                "TTY": root.tty or "-",
                "CWD": root.cwd or "-",
                "EVIDENCE": _first_evidence(session),
            }
        )

    headers = ["PID", "STATUS", "CONF", "ELAPSED", "TTY", "CWD", "EVIDENCE"]
    widths = {
        header: max(len(header), *(len(row[header]) for row in rows))
        for header in headers
    }
    print(" ".join(header.ljust(widths[header]) for header in headers))
    print(" ".join("-" * widths[header] for header in headers))
    for row in rows:
        print(" ".join(row[header].ljust(widths[header]) for header in headers))


def _first_evidence(session) -> str:
    if session.inference.evidence:
        return session.inference.evidence[0].detail
    if session.root.cmdline:
        return shlex.join(session.root.cmdline)
    return session.root.command_name


def _format_duration(value: float | None) -> str:
    if value is None:
        return "-"
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
