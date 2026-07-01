from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .models import CodexStateSummary, SessionActivity, StateFile


DEFAULT_MAX_FILES = 12
RUNTIME_LOG_DATABASE = "logs_2.sqlite"
RUNTIME_LOG_WAL = f"{RUNTIME_LOG_DATABASE}-wal"
RUNTIME_FAILURE_MATCH_GRACE_SECONDS = 30.0
RUNTIME_FAILURE_SQLITE_LIMIT = 2000
RUNTIME_FAILURE_SQLITE_TIMEOUT_SECONDS = 0.05
RUNTIME_FAILURE_WAL_MAX_BYTES = 8 * 1024 * 1024
RUNTIME_FAILURE_WAL_SNIPPET_BYTES = 6000
RUNTIME_FAILURE_WAL_SOURCE_MARKER = "codex_core::session::turncore/src/session/turn.rs"
RUNTIME_FAILURE_SQLITE_LIMIT_PER_THREAD = 20
RUNTIME_FAILURE_WAL_CONTEXT_BYTES = 2000
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
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HTTP_ERROR_STATUS_RE = re.compile(
    r"\b(?:unexpected\s+status|last\s+status:?|status:?)\s*(4\d\d|5\d\d)\b"
)
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
THREAD_ID_RE = re.compile(rf"\b(?:thread_id|thread\.id)=({UUID_RE})\b")
TURN_ID_RE = re.compile(rf"\b(?:turn_id|turn\.id)=({UUID_RE})\b")
CWD_RE = re.compile(r"\bcwd=([^}:]+)")
TERMINAL_ERROR_PREFIXES = (
    "error:",
    "unexpected status ",
)
TERMINAL_ERROR_PHRASES = (
    "api error",
    "auth_unavailable",
    "bad gateway",
    "connection refused",
    "connection reset",
    "exceeded retry limit",
    "gateway timeout",
    "internal server error",
    "model error",
    "network error",
    "no auth available",
    "overloaded",
    "rate limit",
    "rate limited",
    "response.completed",
    "server error",
    "service unavailable",
    "stream closed before response.completed",
    "stream disconnected before completion",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "too many requests",
    "transport error",
    "error decoding response body",
)
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


@dataclass(frozen=True)
class _RuntimeFailure:
    thread_id: str | None
    turn_id: str | None
    cwd: str | None
    timestamp: float | None
    source: str


class _TurnRecordSummary:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.seen = False
        self.turn_id: str | None = None
        self.turn_started_at: float | None = None
        self.latest_terminal_record: dict | None = None
        self.failed_event = False
        self.saw_user = False
        self.saw_token_count = False
        self.saw_visible_assistant_or_tool = False

    def add(self, record: dict) -> None:
        self.seen = True
        if self.turn_id is None:
            self.turn_id = _turn_id_from_record(record)
        if self.turn_started_at is None and (
            _is_turn_start_record(record) or _is_user_message_record(record)
        ):
            self.turn_started_at = _timestamp_from_record(record)

        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if _optional_str(payload.get("type")) in TERMINAL_PAYLOAD_TYPES:
            self.latest_terminal_record = record
        if _optional_str(payload.get("type")) == "token_count":
            self.saw_token_count = True
        if _is_user_message_record(record):
            self.saw_user = True
        if _is_visible_assistant_or_tool_record(record):
            self.saw_visible_assistant_or_tool = True
        if _record_is_failed_event(record):
            self.failed_event = True


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
    metadata_only: bool = False,
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
        activity = (
            _session_activity_metadata(home, path, observed_at)
            if metadata_only
            else _session_activity(home, path, observed_at)
        )
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

    if activities and not metadata_only:
        activities = _merge_runtime_failures(home, activities)
    return tuple(activities)


def scan_new_session_markers(
    codex_home: Path | None = None,
    max_files: int = 80,
) -> tuple[SessionActivity, ...]:
    home = (codex_home or default_codex_home()).expanduser()
    if not home.is_dir():
        return ()

    observed_at = time.time()
    session_ids = _session_ids_for_home(home)
    try:
        paths = sorted(
            home.glob("shell_snapshots/*.sh"),
            key=lambda path: _mtime_or_zero(path),
            reverse=True,
        )[:max_files]
    except OSError:
        return ()

    markers = []
    for path in paths:
        marker = _new_session_marker(home, path, observed_at, session_ids)
        if marker is not None:
            markers.append(marker)
    return tuple(markers)


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

    scanned = _scan_session_records(path)
    if scanned is None:
        return None
    first_record, last_record, latest_turn = scanned

    session_id = _session_id_from_record(first_record) or _session_id_from_name(path.name)
    cwd = _cwd_from_record(first_record)
    last_payload = last_record.get("payload") if isinstance(last_record, dict) else None
    last_payload = last_payload if isinstance(last_payload, dict) else {}
    last_payload_type = _optional_str(last_payload.get("type"))
    last_record_type = _optional_str(last_record.get("type")) if isinstance(last_record, dict) else None
    last_payload_reason = _optional_str(last_payload.get("reason"))
    latest_terminal_record = latest_turn.latest_terminal_record
    latest_terminal_payload = (
        latest_terminal_record.get("payload")
        if isinstance(latest_terminal_record, dict)
        else None
    )
    latest_terminal_payload = (
        latest_terminal_payload if isinstance(latest_terminal_payload, dict) else {}
    )
    terminal_payload_type = _optional_str(latest_terminal_payload.get("type"))
    terminal_agent_message_missing = (
        terminal_payload_type == "task_complete"
        and "last_agent_message" in latest_terminal_payload
        and latest_terminal_payload.get("last_agent_message") is None
    )
    failed_event = latest_turn.failed_event

    return SessionActivity(
        relative_path=relative_path,
        session_id=session_id,
        turn_id=latest_turn.turn_id,
        cwd=cwd,
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        observed_at=observed_at,
        session_started_at=_timestamp_from_record(first_record),
        last_record_at=_timestamp_from_record(last_record),
        turn_started_at=latest_turn.turn_started_at,
        terminal_event_at=_timestamp_from_record(latest_terminal_record),
        last_record_type=last_record_type,
        last_payload_type=last_payload_type,
        last_payload_role=_optional_str(last_payload.get("role")),
        last_payload_reason=last_payload_reason,
        terminal_event=latest_terminal_record is not None,
        terminal_agent_message_missing=terminal_agent_message_missing,
        failed_event=failed_event,
        latest_turn_has_user=latest_turn.saw_user,
    )


def _session_activity_metadata(
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

    return SessionActivity(
        relative_path=relative_path,
        session_id=None,
        turn_id=None,
        cwd=None,
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        observed_at=observed_at,
    )


def _new_session_marker(
    home: Path,
    path: Path,
    observed_at: float,
    session_ids: set[str],
) -> SessionActivity | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None

    session_id = _session_id_from_shell_snapshot_name(path.name)
    if session_id is None or session_id in session_ids:
        return None

    try:
        relative_path = path.relative_to(home).as_posix()
    except ValueError:
        relative_path = path.as_posix()

    return SessionActivity(
        relative_path=relative_path,
        session_id=session_id,
        turn_id=None,
        cwd=_cwd_from_shell_snapshot(path),
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        observed_at=observed_at,
        session_started_at=stat.st_mtime,
        last_record_at=stat.st_mtime,
        last_record_type="shell_snapshot",
        last_payload_type="new_session",
    )


def _scan_session_records(
    path: Path,
) -> tuple[dict | None, dict | None, _TurnRecordSummary] | None:
    first_record = None
    last_record = None
    all_records = _TurnRecordSummary()
    explicit_turn = _TurnRecordSummary()
    user_turn = _TurnRecordSummary()

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                if first_record is None:
                    first_record = record
                last_record = record
                all_records.add(record)

                if _is_turn_start_record(record):
                    explicit_turn.reset()
                    explicit_turn.add(record)
                elif explicit_turn.seen:
                    explicit_turn.add(record)

                if _is_user_message_record(record):
                    user_turn.reset()
                    user_turn.add(record)
                elif user_turn.seen:
                    user_turn.add(record)
    except OSError:
        return None

    if explicit_turn.seen:
        latest_turn = explicit_turn
    elif user_turn.seen:
        latest_turn = user_turn
    else:
        latest_turn = all_records
    return first_record, last_record, latest_turn


def _merge_runtime_failures(
    home: Path,
    activities: list[SessionActivity],
) -> list[SessionActivity]:
    failures = _scan_runtime_failures(home, activities)
    if not failures:
        return activities

    merged = []
    for activity in activities:
        if activity.failed_event or not activity.terminal_event:
            merged.append(activity)
            continue
        if any(_runtime_failure_matches_activity(failure, activity) for failure in failures):
            merged.append(replace(activity, failed_event=True))
        else:
            merged.append(activity)
    return merged


def _scan_runtime_failures(
    home: Path,
    activities: list[SessionActivity],
) -> tuple[_RuntimeFailure, ...]:
    candidates = [
        activity
        for activity in activities
        if activity.terminal_event and not activity.failed_event
    ]
    session_ids = {activity.session_id for activity in candidates if activity.session_id}
    turn_ids = {activity.turn_id for activity in candidates if activity.turn_id}
    if not session_ids and not turn_ids:
        return ()

    failures = []
    failures.extend(
        _scan_runtime_failure_sqlite(
            home / RUNTIME_LOG_DATABASE,
            activities=candidates,
        )
    )
    failures.extend(
        _scan_runtime_failure_wal(
            home / RUNTIME_LOG_WAL,
            session_ids=session_ids,
            turn_ids=turn_ids,
        )
    )
    return tuple(_dedupe_runtime_failures(failures))


def _scan_runtime_failure_sqlite(
    path: Path,
    *,
    activities: list[SessionActivity],
) -> tuple[_RuntimeFailure, ...]:
    session_windows = _runtime_failure_session_windows(activities)
    session_ids = set(session_windows)
    if not path.is_file() or not session_ids:
        return ()

    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=RUNTIME_FAILURE_SQLITE_TIMEOUT_SECONDS,
        )
    except sqlite3.Error:
        return ()

    try:
        try:
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass

        columns = _sqlite_table_columns(connection, "logs")
        if "logs" not in _sqlite_table_names(connection) or not columns:
            return ()
        if "feedback_log_body" not in columns or "ts" not in columns:
            return ()

        select_thread_id = "thread_id" if "thread_id" in columns else "NULL"
        target_column = "target" if "target" in columns else "NULL"
        failures = []
        if "thread_id" in columns:
            thread_query = (
                f"SELECT ts, {select_thread_id}, {target_column}, feedback_log_body "
                "FROM logs "
                "WHERE thread_id = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT ?"
            )
            for session_id in sorted(session_ids):
                rows = connection.execute(
                    thread_query,
                    (
                        session_id,
                        int(session_windows[session_id]),
                        RUNTIME_FAILURE_SQLITE_LIMIT_PER_THREAD,
                    ),
                )
                for timestamp, thread_id, target, body in rows:
                    failure = _runtime_failure_from_log_body(
                        timestamp=_float_or_none(timestamp),
                        thread_id=_optional_str(thread_id),
                        target=_optional_str(target),
                        body=_optional_str(body) or "",
                        source=RUNTIME_LOG_DATABASE,
                    )
                    if failure is not None:
                        failures.append(failure)

        min_timestamp = min(session_windows.values())
        query = (
            f"SELECT ts, {select_thread_id}, {target_column}, feedback_log_body "
            "FROM logs "
            "WHERE ts >= ? AND feedback_log_body LIKE '%Turn error:%' "
            "ORDER BY ts DESC LIMIT ?"
        )
        rows = connection.execute(query, (int(min_timestamp), RUNTIME_FAILURE_SQLITE_LIMIT))
        for timestamp, thread_id, target, body in rows:
            failure = _runtime_failure_from_log_body(
                timestamp=_float_or_none(timestamp),
                thread_id=_optional_str(thread_id),
                target=_optional_str(target),
                body=_optional_str(body) or "",
                source=RUNTIME_LOG_DATABASE,
            )
            if failure is not None:
                failures.append(failure)
        return tuple(failures)
    except sqlite3.Error:
        return ()
    finally:
        connection.close()


def _runtime_failure_session_windows(
    activities: list[SessionActivity],
) -> dict[str, float]:
    windows: dict[str, float] = {}
    for activity in activities:
        if activity.session_id is None:
            continue
        timestamp = (
            activity.turn_started_at
            or activity.terminal_event_at
            or activity.last_record_at
            or activity.modified_at
            or time.time()
        )
        start = max(0.0, timestamp - RUNTIME_FAILURE_MATCH_GRACE_SECONDS)
        previous = windows.get(activity.session_id)
        if previous is None or start < previous:
            windows[activity.session_id] = start
    return windows


def _sqlite_table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _sqlite_table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _scan_runtime_failure_wal(
    path: Path,
    *,
    session_ids: set[str],
    turn_ids: set[str],
) -> tuple[_RuntimeFailure, ...]:
    if not path.is_file():
        return ()

    try:
        size = path.stat().st_size
    except OSError:
        return ()
    offset = max(0, size - RUNTIME_FAILURE_WAL_MAX_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(RUNTIME_FAILURE_WAL_MAX_BYTES)
    except OSError:
        return ()

    text = data.decode("utf-8", errors="ignore")
    failures = []
    for error_match in re.finditer(r"Turn error:", text):
        start = max(0, error_match.start() - RUNTIME_FAILURE_WAL_CONTEXT_BYTES)
        end = min(len(text), error_match.start() + RUNTIME_FAILURE_WAL_SNIPPET_BYTES)
        snippet = text[start:end]
        if RUNTIME_FAILURE_WAL_SOURCE_MARKER not in snippet:
            continue
        failure = _runtime_failure_from_log_body(
            timestamp=None,
            thread_id=None,
            target=None,
            body=snippet,
            source=RUNTIME_LOG_WAL,
        )
        if failure is None:
            continue
        if failure.thread_id not in session_ids and failure.turn_id not in turn_ids:
            continue
        failures.append(failure)
    return tuple(failures)


def _dedupe_runtime_failures(
    failures: list[_RuntimeFailure],
) -> tuple[_RuntimeFailure, ...]:
    seen = set()
    result = []
    for failure in failures:
        key = (
            failure.thread_id,
            failure.turn_id,
            failure.cwd,
            failure.timestamp,
            failure.source,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(failure)
    return tuple(result)


def _runtime_failure_from_log_body(
    *,
    timestamp: float | None,
    thread_id: str | None,
    target: str | None,
    body: str,
    source: str,
) -> _RuntimeFailure | None:
    if "Turn error:" not in body:
        return None
    if not _runtime_log_body_is_codex_turn_error(target, body):
        return None

    parsed_thread_id = thread_id or _regex_first(THREAD_ID_RE, body)
    return _RuntimeFailure(
        thread_id=parsed_thread_id,
        turn_id=_regex_first(TURN_ID_RE, body),
        cwd=_regex_first(CWD_RE, body),
        timestamp=timestamp,
        source=source,
    )


def _runtime_log_body_is_codex_turn_error(target: str | None, body: str) -> bool:
    if target == "codex_core::session::turn":
        return True
    return (
        "codex_core::session::turn" in body
        or "session_task.run:run_turn: Turn error:" in body
    )


def _runtime_failure_matches_activity(
    failure: _RuntimeFailure,
    activity: SessionActivity,
) -> bool:
    if failure.thread_id is not None and activity.session_id is not None:
        if failure.thread_id != activity.session_id:
            return False
    elif failure.thread_id is not None:
        return False

    if failure.turn_id is not None and activity.turn_id is not None:
        return failure.turn_id == activity.turn_id
    if failure.turn_id is not None:
        return False

    if failure.cwd is not None and activity.cwd is not None:
        if _normalize_runtime_path(failure.cwd) != _normalize_runtime_path(activity.cwd):
            return False

    if failure.timestamp is not None:
        if (
            activity.turn_started_at is not None
            and failure.timestamp + RUNTIME_FAILURE_MATCH_GRACE_SECONDS
            < activity.turn_started_at
        ):
            return False
        if (
            activity.terminal_event_at is not None
            and failure.timestamp - RUNTIME_FAILURE_MATCH_GRACE_SECONDS
            > activity.terminal_event_at
        ):
            return False

    return failure.thread_id is not None or (
        failure.cwd is not None and failure.timestamp is not None
    )


def _regex_first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(1)


def _normalize_runtime_path(value: str) -> str:
    return str(Path(value).expanduser())


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


def _session_id_from_shell_snapshot_name(name: str) -> str | None:
    if not name.endswith(".sh"):
        return None
    prefix = name.removesuffix(".sh").split(".", 1)[0]
    return prefix if re.fullmatch(UUID_RE, prefix) else None


def _session_ids_for_home(home: Path) -> set[str]:
    session_ids: set[str] = set()
    try:
        paths = tuple(home.glob("sessions/**/*.jsonl"))
    except OSError:
        return session_ids
    for path in paths:
        session_id = _session_id_from_name(path.name)
        if session_id is not None:
            session_ids.add(session_id)
    return session_ids


def _cwd_from_record(record: dict | None) -> str | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    return _optional_str(payload.get("cwd"))


def _cwd_from_shell_snapshot(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        raw = _shell_assignment_value(line, "PWD")
        if raw is None:
            continue
        try:
            parts = shlex.split(raw, posix=True)
        except ValueError:
            continue
        if len(parts) == 1 and parts[0]:
            return parts[0]
    return None


def _shell_assignment_value(line: str, name: str) -> str | None:
    stripped = line.strip()
    prefixes = (
        f"declare -x {name}=",
        f"export {name}=",
        f"{name}=",
    )
    for prefix in prefixes:
        if stripped.startswith(prefix):
            return stripped.removeprefix(prefix)
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _is_turn_start_record(record: dict) -> bool:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    record_type = _optional_str(record.get("type"))
    payload_type = _optional_str(payload.get("type"))
    return record_type == "turn_context" or (
        record_type == "event_msg" and payload_type == "task_started"
    )


def _turn_id_from_record(record: dict) -> str | None:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    metadata = metadata if isinstance(metadata, dict) else {}
    return _optional_str(
        payload.get("turn_id")
        or metadata.get("turn_id")
        or record.get("turn_id")
    )


def _timestamp_from_record(record: dict | None) -> float | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return (
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def _record_is_failed_event(record: dict) -> bool:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    record_type = _optional_str(record.get("type"))
    payload_type = _optional_str(payload.get("type"))
    payload_reason = _optional_str(payload.get("reason"))
    return _is_failed_event(
        record_type,
        payload_type,
        payload_reason,
    ) or _is_failed_message_record(record_type, payload)


def _is_visible_assistant_or_tool_record(record: dict) -> bool:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    record_type = _optional_str(record.get("type"))
    payload_type = _optional_str(payload.get("type"))
    payload_role = _optional_str(payload.get("role"))
    if record_type == "event_msg" and payload_type == "agent_message":
        return _optional_str(payload.get("phase")) is None
    if record_type == "response_item" and payload_role == "assistant":
        return True
    return payload_type in {
        "custom_tool_call",
        "function_call",
        "function_call_output",
    }


def _is_user_message_record(record: dict) -> bool:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    record_type = _optional_str(record.get("type"))
    payload_type = _optional_str(payload.get("type"))
    payload_role = _optional_str(payload.get("role"))
    return (
        record_type == "event_msg"
        and payload_type == "user_message"
    ) or (
        record_type == "response_item"
        and payload_type == "message"
        and payload_role == "user"
    )


def _is_failed_message_record(record_type: str | None, payload: dict) -> bool:
    payload_type = _optional_str(payload.get("type"))
    payload_role = _optional_str(payload.get("role"))
    if record_type == "event_msg" and payload_type == "agent_message":
        if _optional_str(payload.get("phase")) is not None:
            return False
        text = _optional_str(payload.get("message")) or ""
        return _message_text_has_terminal_error(text, require_red_ansi=False)
    elif (
        record_type == "response_item"
        and payload_type == "message"
        and payload_role == "assistant"
    ):
        text = _message_content_text(payload.get("content"))
        return _message_text_has_terminal_error(
            text,
            require_red_ansi=True,
        ) or _message_text_is_terminal_diagnostic(text)
    else:
        return False


def _message_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _message_text_has_terminal_error(text: str, *, require_red_ansi: bool) -> bool:
    for line in text.splitlines():
        has_error_marker = _line_has_terminal_error_marker(line)
        has_red_ansi = _line_has_red_ansi(line)
        if require_red_ansi and not has_red_ansi:
            continue
        normalized = _normalize_terminal_message_line(line)
        if not normalized:
            continue
        if normalized.startswith(TERMINAL_ERROR_PREFIXES):
            return True
        if HTTP_ERROR_STATUS_RE.search(normalized):
            return True
        if has_error_marker and any(
            phrase in normalized for phrase in TERMINAL_ERROR_PHRASES
        ):
            return True
    return False


def _message_text_is_terminal_diagnostic(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    normalized_first = _normalize_terminal_message_line(lines[0])
    if not normalized_first:
        return False
    if not (
        _line_has_terminal_error_marker(lines[0])
        or normalized_first.startswith(TERMINAL_ERROR_PREFIXES)
    ):
        return False
    normalized_text = _normalize_terminal_message_line("\n".join(lines))
    return (
        normalized_text.startswith(TERMINAL_ERROR_PREFIXES)
        or HTTP_ERROR_STATUS_RE.search(normalized_text) is not None
        or any(phrase in normalized_text for phrase in TERMINAL_ERROR_PHRASES)
    )


def _line_has_terminal_error_marker(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("■") or _line_has_red_ansi(line)


def _line_has_red_ansi(line: str) -> bool:
    return "\x1b[31" in line or "\x1b[91" in line


def _normalize_terminal_message_line(line: str) -> str:
    return ANSI_ESCAPE_RE.sub("", line).strip().lstrip("■ ").lower()


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
