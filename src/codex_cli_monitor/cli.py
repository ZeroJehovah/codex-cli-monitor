from __future__ import annotations

import argparse
import json
import os
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .api import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    DEFAULT_COLLECTOR_INTERVAL_SECONDS,
    DEFAULT_LOCAL_CACHE_SECONDS,
    DEFAULT_REMOTE_TTL_SECONDS,
    ApiConfig,
    serve_api,
)
from .monitor import inspect_runtime


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    if args.stop:
        return _stop_daemon(args.pid_file)

    if args.daemon:
        return _start_daemon(args)

    if args.serve:
        return _serve(args)

    while True:
        sessions, codex_state = inspect_runtime(
            proc_root=args.proc_root,
            sample_window=args.sample_window,
            shim_log=args.shim_log,
            codex_home=args.codex_home,
            hook_log=args.hook_log,
        )
        if args.json:
            payload = {
                "session_count": len(sessions),
                "sessions": [session.to_dict() for session in sessions],
                "codex_state": codex_state.to_dict(),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_table(sessions, codex_state)

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
        "--codex-home",
        type=Path,
        default=None,
        help="Codex home directory to scan for local state metadata",
    )
    parser.add_argument(
        "--hook-log",
        type=Path,
        default=None,
        help="path to Codex hook lifecycle JSONL metadata",
    )
    parser.add_argument(
        "--watch",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="refresh repeatedly at the given interval",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run a resident HTTP API server",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="run the HTTP API server in the background",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop the background HTTP API server recorded in the pid file",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_API_HOST,
        help=f"HTTP API host, defaults to {DEFAULT_API_HOST}",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_API_PORT,
        help=f"HTTP API port, defaults to {DEFAULT_API_PORT}",
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=None,
        help="daemon pid file path",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="daemon stdout/stderr log file path",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="receive remote collector snapshots and merge them with local sessions",
    )
    parser.add_argument(
        "--server-id",
        default=os.environ.get("CODEX_MONITOR_SERVER_ID"),
        help="stable unique server id; defaults to CODEX_MONITOR_SERVER_ID or hostname",
    )
    parser.add_argument(
        "--server-name",
        default=os.environ.get("CODEX_MONITOR_SERVER_NAME"),
        help="display name; defaults to CODEX_MONITOR_SERVER_NAME or hostname",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CODEX_MONITOR_API_TOKEN"),
        help="Bearer token for read APIs; prefer CODEX_MONITOR_API_TOKEN",
    )
    parser.add_argument(
        "--ingest-token",
        default=os.environ.get("CODEX_MONITOR_INGEST_TOKEN"),
        help="Bearer token accepted from collectors; prefer CODEX_MONITOR_INGEST_TOKEN",
    )
    parser.add_argument(
        "--ingest-token-file",
        type=Path,
        default=Path(os.environ["CODEX_MONITOR_INGEST_TOKEN_FILE"])
        if os.environ.get("CODEX_MONITOR_INGEST_TOKEN_FILE")
        else None,
        help="JSON object mapping server ids to collector tokens",
    )
    parser.add_argument(
        "--collector-url",
        default=os.environ.get("CODEX_MONITOR_AGGREGATOR_URL"),
        help="aggregator base URL or /api/collector/snapshot endpoint",
    )
    parser.add_argument(
        "--collector-token",
        default=os.environ.get("CODEX_MONITOR_COLLECTOR_TOKEN"),
        help="collector Bearer token; prefer CODEX_MONITOR_COLLECTOR_TOKEN",
    )
    parser.add_argument(
        "--collector-interval",
        type=_positive_float,
        default=DEFAULT_COLLECTOR_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=(
            "seconds between collector snapshots; defaults to "
            f"{DEFAULT_COLLECTOR_INTERVAL_SECONDS}"
        ),
    )
    parser.add_argument(
        "--remote-ttl",
        type=_positive_float,
        default=DEFAULT_REMOTE_TTL_SECONDS,
        metavar="SECONDS",
        help=f"remote snapshot TTL; defaults to {DEFAULT_REMOTE_TTL_SECONDS}",
    )
    parser.add_argument(
        "--cache-seconds",
        type=_non_negative_float,
        default=DEFAULT_LOCAL_CACHE_SECONDS,
        metavar="SECONDS",
        help=f"local scan cache lifetime; defaults to {DEFAULT_LOCAL_CACHE_SECONDS}",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not (args.serve or args.daemon):
        return
    args.ingest_tokens = {}
    if args.ingest_token_file is not None:
        try:
            args.ingest_tokens = _load_ingest_tokens(args.ingest_token_file)
        except ValueError as error:
            parser.error(str(error))
    if args.aggregate and not args.ingest_token and not args.ingest_tokens:
        parser.error("--aggregate requires an ingest token or ingest token file")
    if args.collector_url and not args.collector_token:
        parser.error(
            "--collector-url requires --collector-token or "
            "CODEX_MONITOR_COLLECTOR_TOKEN"
        )


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


def _port(value: str) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _serve(args: argparse.Namespace) -> int:
    pid_file = _pid_file(args.pid_file)
    current_pid = os.getpid()
    running_pid = _running_monitor_pid(pid_file)
    if running_pid is not None and running_pid != current_pid:
        print(
            f"codex-monitor API is already running with PID {running_pid} "
            f"({pid_file})",
            file=sys.stderr,
        )
        return 1
    _write_pid_file(pid_file)
    try:
        serve_api(
            host=args.host,
            port=args.port,
            config=ApiConfig(
                proc_root=args.proc_root,
                sample_window=args.sample_window,
                shim_log=args.shim_log,
                codex_home=args.codex_home,
                hook_log=args.hook_log,
                aggregate=args.aggregate,
                server_id=args.server_id,
                server_name=args.server_name,
                api_token=args.api_token,
                ingest_token=args.ingest_token,
                ingest_tokens=args.ingest_tokens,
                remote_ttl_seconds=args.remote_ttl,
                local_cache_seconds=args.cache_seconds,
                collector_url=args.collector_url,
                collector_token=args.collector_token,
                collector_interval_seconds=args.collector_interval,
            ),
        )
    finally:
        _remove_pid_file(pid_file, expected_pid=current_pid)
    return 0


def _start_daemon(args: argparse.Namespace) -> int:
    pid_file = _pid_file(args.pid_file)
    running_pid = _running_monitor_pid(pid_file)
    if running_pid is not None:
        print(
            f"codex-monitor API is already running with PID {running_pid} "
            f"({pid_file})"
        )
        return 0

    log_file = _log_file(args.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "codex_cli_monitor",
        "--serve",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--sample-window",
        str(args.sample_window),
        "--proc-root",
        str(args.proc_root),
        "--pid-file",
        str(pid_file),
        "--remote-ttl",
        str(args.remote_ttl),
        "--cache-seconds",
        str(args.cache_seconds),
        "--collector-interval",
        str(args.collector_interval),
    ]
    if args.aggregate:
        command.append("--aggregate")
    if args.server_id is not None:
        command.extend(["--server-id", args.server_id])
    if args.server_name is not None:
        command.extend(["--server-name", args.server_name])
    if args.collector_url is not None:
        command.extend(["--collector-url", args.collector_url])
    if args.ingest_token_file is not None:
        command.extend(["--ingest-token-file", str(args.ingest_token_file)])
    if args.shim_log is not None:
        command.extend(["--shim-log", str(args.shim_log)])
    if args.codex_home is not None:
        command.extend(["--codex-home", str(args.codex_home)])
    if args.hook_log is not None:
        command.extend(["--hook-log", str(args.hook_log)])

    child_env = os.environ.copy()
    _set_secret_env(child_env, "CODEX_MONITOR_API_TOKEN", args.api_token)
    _set_secret_env(child_env, "CODEX_MONITOR_INGEST_TOKEN", args.ingest_token)
    _set_secret_env(child_env, "CODEX_MONITOR_COLLECTOR_TOKEN", args.collector_token)

    with log_file.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            env=child_env,
        )
    time.sleep(0.2)
    if process.poll() is not None:
        print(
            f"codex-monitor API failed to start; see {log_file}",
            file=sys.stderr,
        )
        return process.returncode or 1

    print(
        f"codex-monitor API started with PID {process.pid} at "
        f"http://{args.host}:{args.port}"
    )
    print(f"mode: {'aggregator' if args.aggregate else 'collector'}")
    print(f"pid file: {pid_file}")
    print(f"log file: {log_file}")
    return 0


def _set_secret_env(env: dict[str, str], name: str, value: str | None) -> None:
    if value:
        env[name] = value
    else:
        env.pop(name, None)


def _load_ingest_tokens(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read ingest token file: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"ingest token file is not valid JSON: {error}") from error
    if not isinstance(payload, dict) or not payload:
        raise ValueError("ingest token file must be a non-empty JSON object")
    tokens = {}
    for server_id, token in payload.items():
        if not isinstance(server_id, str) or not isinstance(token, str) or not token:
            raise ValueError("ingest token file entries must map server ids to tokens")
        tokens[server_id] = token
    return tokens


def _stop_daemon(path: Path | None) -> int:
    pid_file = _pid_file(path)
    pid = _read_pid_file(pid_file)
    if pid is None:
        print(f"codex-monitor API is not running ({pid_file} not found)")
        return 0
    if not _process_exists(pid):
        _remove_pid_file(pid_file)
        print(f"removed stale pid file for PID {pid}")
        return 0
    if _is_monitor_api_process(pid) is False:
        _remove_pid_file(pid_file)
        print(f"removed stale pid file for non-monitor PID {pid}")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        print(f"could not stop PID {pid}: {error}", file=sys.stderr)
        return 1

    for _ in range(50):
        if not _process_exists(pid):
            _remove_pid_file(pid_file)
            print(f"stopped codex-monitor API PID {pid}")
            return 0
        time.sleep(0.1)

    print(f"sent SIGTERM to codex-monitor API PID {pid}")
    return 0


def _state_dir() -> Path:
    if os.environ.get("XDG_STATE_HOME"):
        return Path(os.environ["XDG_STATE_HOME"]).expanduser() / "codex-cli-monitor"
    return Path.home() / ".local" / "state" / "codex-cli-monitor"


def _pid_file(path: Path | None) -> Path:
    return (path or (_state_dir() / "codex-monitor.pid")).expanduser()


def _log_file(path: Path | None) -> Path:
    return (path or (_state_dir() / "codex-monitor.log")).expanduser()


def _write_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _read_pid_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _remove_pid_file(path: Path, expected_pid: int | None = None) -> None:
    if expected_pid is not None and _read_pid_file(path) != expected_pid:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_monitor_pid(pid_file: Path) -> int | None:
    pid = _read_pid_file(pid_file)
    if pid is None:
        return None
    if not _process_exists(pid):
        _remove_pid_file(pid_file)
        return None
    if _is_monitor_api_process(pid) is False:
        _remove_pid_file(pid_file)
        return None
    return pid


def _is_monitor_api_process(pid: int) -> bool | None:
    try:
        raw_cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    parts = [part.decode("utf-8", errors="replace") for part in raw_cmdline.split(b"\0") if part]
    if not parts:
        return None
    joined = "\0".join(parts)
    return "codex_cli_monitor" in joined and "--serve" in parts


def _print_table(sessions: tuple, codex_state) -> None:
    _print_codex_state(codex_state)
    print(f"Open Codex sessions: {len(sessions)}")
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
                "STATUS": session.display_status,
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


def _print_codex_state(codex_state) -> None:
    session_files = [
        state_file
        for state_file in codex_state.newest_files
        if state_file.kind == "session_jsonl"
    ]
    if session_files:
        newest = session_files[0]
        print(
            "Codex state: "
            f"{codex_state.codex_home} latest session metadata "
            f"{newest.relative_path} modified {_format_age(newest.modified_at)} ago"
        )
    elif codex_state.scan_errors:
        print(f"Codex state: unavailable ({codex_state.scan_errors[0]})")
    else:
        print(f"Codex state: {codex_state.codex_home} no session metadata found")


def _first_evidence(session) -> str:
    if session.inference.evidence:
        return session.inference.evidence[0].detail
    if session.root.cmdline:
        return shlex.join(session.root.cmdline)
    return session.root.command_name


def _format_age(timestamp: float) -> str:
    return _format_duration(max(0, time.time() - timestamp))


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
