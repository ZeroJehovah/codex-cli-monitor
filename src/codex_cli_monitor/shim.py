from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Mapping

from .models import LaunchRecord


LOG_ENV = "CODEX_MONITOR_SHIM_LOG"
REAL_CODEX_ENV = "CODEX_MONITOR_REAL_CODEX"


def default_log_path(env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    if env.get(LOG_ENV):
        return Path(env[LOG_ENV]).expanduser()
    if env.get("XDG_STATE_HOME"):
        state_home = Path(env["XDG_STATE_HOME"]).expanduser()
    else:
        state_home = Path.home() / ".local" / "state"
    return state_home / "codex-cli-monitor" / "launches.jsonl"


def find_real_codex(
    shim_argv0: str | None = None,
    env: Mapping[str, str] | None = None,
    shim_path: Path | None = None,
) -> Path | None:
    env = env or os.environ
    configured = env.get(REAL_CODEX_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        if _is_executable_file(candidate):
            return candidate

    shim_path = shim_path or Path(shim_argv0 or sys.argv[0]).expanduser()
    try:
        resolved_shim = shim_path.resolve()
    except OSError:
        resolved_shim = shim_path.absolute()

    path_value = env.get("PATH", "")
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry).expanduser() / "codex"
        if not _is_executable_file(candidate):
            continue
        try:
            resolved_candidate = candidate.resolve()
        except OSError:
            resolved_candidate = candidate.absolute()
        if resolved_candidate == resolved_shim:
            continue
        return candidate
    return None


def write_launch_record(
    path: Path,
    argv: tuple[str, ...],
    real_codex: Path,
    env: Mapping[str, str] | None = None,
    shim_path: Path | None = None,
) -> None:
    env = env or os.environ
    record = {
        "schema_version": 1,
        "timestamp": time.time(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "argv": list(argv),
        "real_codex": str(real_codex),
        "shim": str((shim_path or Path(sys.argv[0])).resolve()),
        "source": str(path),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def write_launch_record_best_effort(
    path: Path,
    argv: tuple[str, ...],
    real_codex: Path,
    env: Mapping[str, str] | None = None,
    shim_path: Path | None = None,
) -> bool:
    try:
        write_launch_record(path, argv, real_codex, env, shim_path)
    except OSError as error:
        print(f"codex shim: could not write launch metadata: {error}", file=sys.stderr)
        return False
    return True


def load_launch_records(path: Path, max_age_seconds: float = 7 * 24 * 3600) -> dict[int, LaunchRecord]:
    if not path.exists():
        return {}
    min_timestamp = time.time() - max_age_seconds
    records: dict[int, LaunchRecord] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    for line in lines:
        try:
            payload = json.loads(line)
            timestamp = float(payload["timestamp"])
            pid = int(payload["pid"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp < min_timestamp:
            continue
        records[pid] = LaunchRecord(
            pid=pid,
            ppid=_optional_int(payload.get("ppid")),
            cwd=_optional_str(payload.get("cwd")),
            argv=tuple(str(item) for item in payload.get("argv", [])),
            real_codex=_optional_str(payload.get("real_codex")),
            timestamp=timestamp,
            source=str(payload.get("source") or path),
        )
    return records


def main(argv: list[str] | None = None, shim_path: Path | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    real_codex = find_real_codex(argv[0], shim_path=shim_path)
    if real_codex is None:
        print("codex shim: could not find the real codex executable in PATH", file=sys.stderr)
        return 127

    log_path = default_log_path()
    write_launch_record_best_effort(
        log_path,
        tuple(["codex", *argv[1:]]),
        real_codex,
        shim_path=shim_path,
    )

    env = os.environ.copy()
    env["CODEX_MONITOR_SHIM_ACTIVE"] = "1"
    env["CODEX_MONITOR_SHIM_PATH"] = str((shim_path or Path(argv[0])).resolve())
    os.execvpe(str(real_codex), [str(real_codex), *argv[1:]], env)
    return 127


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
