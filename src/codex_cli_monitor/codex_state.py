from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
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
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HTTP_ERROR_STATUS_RE = re.compile(
    r"\b(?:unexpected\s+status|last\s+status:?|status:?)\s*(4\d\d|5\d\d)\b"
)
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

    def completed_without_visible_response(
        self,
        terminal_payload_type: str | None,
    ) -> bool:
        return (
            terminal_payload_type in TERMINAL_PAYLOAD_TYPES
            and self.saw_user
            and self.saw_token_count
            and not self.saw_visible_assistant_or_tool
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
    failed_event = latest_turn.failed_event or latest_turn.completed_without_visible_response(
        terminal_payload_type
    )

    return SessionActivity(
        relative_path=relative_path,
        session_id=session_id,
        turn_id=latest_turn.turn_id,
        cwd=cwd,
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        observed_at=observed_at,
        last_record_at=_timestamp_from_record(last_record),
        turn_started_at=latest_turn.turn_started_at,
        terminal_event_at=_timestamp_from_record(latest_terminal_record),
        last_record_type=last_record_type,
        last_payload_type=last_payload_type,
        last_payload_role=_optional_str(last_payload.get("role")),
        last_payload_reason=last_payload_reason,
        terminal_event=latest_terminal_record is not None,
        failed_event=failed_event,
        latest_turn_has_user=latest_turn.saw_user,
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
