from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .models import CodexStateSummary, SessionActivity, StateFile


DEFAULT_MAX_FILES = 12
FAILED_RECORD_TYPES = {"error"}
FAILED_PAYLOAD_TYPES = {
    "error",
    "thread_rolled_back",
    "turn_aborted",
    "turn_failed",
}
FAILED_PAYLOAD_REASONS = {
    "cancelled",
    "canceled",
    "error",
    "failed",
    "interrupted",
}
TERMINAL_PAYLOAD_TYPES = {
    "task_complete",
    "thread_rolled_back",
    "turn_aborted",
    "turn_completed",
    "turn_failed",
}
STATE_PATTERNS = (
    "sessions/**/*.jsonl",
    "shell_snapshots/*.sh",
    "history.jsonl",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
)


def default_codex_home(env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    if env.get("CODEX_HOME"):
        return Path(env["CODEX_HOME"]).expanduser()
    return Path.home() / ".codex"


def scan_codex_state(
    codex_home: Path | None = None,
    max_files: int = DEFAULT_MAX_FILES,
) -> CodexStateSummary:
    home = (codex_home or default_codex_home()).expanduser()
    newest: list[StateFile] = []
    errors: list[str] = []

    if not home.exists():
        return CodexStateSummary(
            codex_home=str(home),
            newest_files=(),
            scan_errors=(f"{home} does not exist",),
        )
    if not home.is_dir():
        return CodexStateSummary(
            codex_home=str(home),
            newest_files=(),
            scan_errors=(f"{home} is not a directory",),
        )

    seen: set[Path] = set()
    for pattern in STATE_PATTERNS:
        try:
            paths = tuple(home.glob(pattern))
        except OSError as error:
            errors.append(f"{pattern}: {error}")
            continue
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            state_file = _state_file(home, path)
            if state_file is not None:
                newest.append(state_file)

    newest.sort(key=lambda item: item.modified_at, reverse=True)
    return CodexStateSummary(
        codex_home=str(home),
        newest_files=tuple(newest[:max_files]),
        scan_errors=tuple(errors),
    )


def scan_session_activities(
    codex_home: Path | None = None,
    max_files: int = 80,
    previous: Mapping[str, SessionActivity] | None = None,
) -> tuple[SessionActivity, ...]:
    home = (codex_home or default_codex_home()).expanduser()
    if not home.is_dir():
        return ()

    observed_at = time.time()
    previous = previous or {}
    activities: list[SessionActivity] = []
    try:
        paths = sorted(
            home.glob("sessions/**/*.jsonl"),
            key=lambda path: _mtime_or_zero(path),
            reverse=True,
        )[:max_files]
    except OSError:
        return ()

    for path in paths:
        activity = _session_activity(home, path, observed_at)
        if activity is None:
            continue
        old = previous.get(activity.relative_path)
        if old is not None:
            activity = replace(
                activity,
                changed_during_sample=(
                    activity.size_bytes != old.size_bytes
                    or activity.modified_at != old.modified_at
                ),
            )
        activities.append(activity)
    return tuple(activities)


def _state_file(home: Path, path: Path) -> StateFile | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    try:
        relative_path = path.relative_to(home)
    except ValueError:
        relative_path = path
    return StateFile(
        relative_path=relative_path.as_posix(),
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        kind=_classify_state_path(relative_path),
    )


def _session_activity(
    home: Path,
    path: Path,
    observed_at: float,
) -> SessionActivity | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None

    try:
        relative_path = path.relative_to(home).as_posix()
    except ValueError:
        relative_path = path.as_posix()

    first_record = None
    last_record = None
    for record in _iter_jsonl_records(path, head_limit=8, tail_limit=24):
        if first_record is None:
            first_record = record
        last_record = record

    session_id = _session_id_from_record(first_record) or _session_id_from_name(path.name)
    cwd = _cwd_from_record(first_record)
    last_payload = last_record.get("payload") if isinstance(last_record, dict) else None
    last_payload = last_payload if isinstance(last_payload, dict) else {}
    last_payload_type = _optional_str(last_payload.get("type"))
    last_record_type = _optional_str(last_record.get("type")) if isinstance(last_record, dict) else None
    last_payload_reason = _optional_str(last_payload.get("reason"))
    failed_event = _is_failed_event(
        last_record_type,
        last_payload_type,
        last_payload_reason,
    )

    return SessionActivity(
        relative_path=relative_path,
        session_id=session_id,
        cwd=cwd,
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        observed_at=observed_at,
        last_record_type=last_record_type,
        last_payload_type=last_payload_type,
        last_payload_role=_optional_str(last_payload.get("role")),
        last_payload_reason=last_payload_reason,
        terminal_event=last_payload_type in TERMINAL_PAYLOAD_TYPES,
        failed_event=failed_event,
    )


def _iter_jsonl_records(
    path: Path,
    head_limit: int,
    tail_limit: int,
) -> tuple[dict, ...]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = []
            tail = []
            for index, line in enumerate(handle):
                if index < head_limit:
                    head.append(line)
                tail.append(line)
                if len(tail) > tail_limit:
                    tail.pop(0)
    except OSError:
        return ()

    records = []
    seen_lines = set()
    for line in [*head, *tail]:
        if line in seen_lines:
            continue
        seen_lines.add(line)
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return tuple(records)


def _session_id_from_record(record: dict | None) -> str | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    return _optional_str(payload.get("session_id") or payload.get("id"))


def _session_id_from_name(name: str) -> str | None:
    if not name.endswith(".jsonl"):
        return None
    stem = name.removesuffix(".jsonl")
    parts = stem.split("-")
    if len(parts) < 7:
        return None
    return "-".join(parts[-5:])


def _cwd_from_record(record: dict | None) -> str | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    return _optional_str(payload.get("cwd"))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _is_failed_event(
    record_type: str | None,
    payload_type: str | None,
    payload_reason: str | None,
) -> bool:
    reason = payload_reason.lower() if payload_reason is not None else None
    return (
        record_type in FAILED_RECORD_TYPES
        or payload_type in FAILED_PAYLOAD_TYPES
        or reason in FAILED_PAYLOAD_REASONS
    )


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _classify_state_path(relative_path: Path) -> str:
    parts = relative_path.parts
    name = relative_path.name
    if parts and parts[0] == "sessions" and name.endswith(".jsonl"):
        return "session_jsonl"
    if parts and parts[0] == "shell_snapshots" and name.endswith(".sh"):
        return "shell_snapshot"
    if name == "history.jsonl":
        return "history"
    if name.endswith(".sqlite-wal"):
        return "sqlite_wal"
    if name.endswith(".sqlite-shm"):
        return "sqlite_shm"
    if name.endswith(".sqlite"):
        return "sqlite"
    return "other"
