"""Read-only view of OpenCode prompts that are waiting for a human answer.

OpenCode keeps pending permission and question prompts in memory only: the
``permission`` table stores approvals that were already granted, and the
persisted event table carries message/session records but no permission
events.  Nothing on disk distinguishes "the model is working" from "OpenCode
stopped and is waiting for the user to choose", so the optional OpenCode plugin
(``assets/opencode/codex-monitor-decisions.js``) appends tiny JSONL markers
whenever a prompt opens or is answered, and this module folds those markers
into the set of decisions that are still open.

The log is read exactly like every other monitor log: a bounded tail, one
rotated generation, corrupt lines skipped, and nothing but structural
identifiers plus the short permission category.  When the plugin is not
installed the log simply does not exist and OpenCode sessions keep their
read-only SQLite status.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .models import normalize_waiting_reason


DECISION_LOG_ENV = "OPENCODE_MONITOR_DECISION_LOG"
DECISION_LOG_DIR = "opencode-cli-monitor"
DECISION_LOG_NAME = "decisions.jsonl"
DECISION_LOG_GENERATIONS = 1
MAX_TAIL_BYTES_PER_FILE = 1024 * 1024
MAX_DECISION_RECORDS = 2000

# Markers older than this are ignored.  A prompt the user abandoned by killing
# OpenCode never gets a reply marker, so an unbounded history could otherwise
# keep a directory pinned to ``待确认`` forever.
DEFAULT_MAX_AGE_SECONDS = 6 * 3600

ASK_EVENTS = {
    "permission.asked": "permission",
    "question.asked": "question",
}
REPLY_EVENTS = frozenset(
    {"permission.replied", "question.replied", "question.rejected"}
)

# Fallback labels used when the plugin could not read a permission category.
DEFAULT_REASONS = {
    "permission": "permission prompt",
    "question": "question prompt",
}


@dataclass(frozen=True)
class PendingDecision:
    """One OpenCode prompt that has opened and has not been answered."""

    kind: str
    request_id: str | None
    session_id: str | None
    directory: str | None
    category: str | None
    asked_at: float

    @property
    def reason(self) -> str:
        """Return the short, body-free label shown next to a waiting row."""
        return (
            normalize_waiting_reason(self.category)
            or DEFAULT_REASONS.get(self.kind)
            or "pending decision"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "directory": self.directory,
            "category": self.category,
            "asked_at": self.asked_at,
            "reason": self.reason,
        }


def default_opencode_decision_log_path(env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    if env.get(DECISION_LOG_ENV):
        return Path(env[DECISION_LOG_ENV]).expanduser()
    if env.get("XDG_STATE_HOME"):
        state_home = Path(env["XDG_STATE_HOME"]).expanduser()
    else:
        state_home = Path.home() / ".local" / "state"
    return state_home / DECISION_LOG_DIR / DECISION_LOG_NAME


def load_decision_records(
    path: Path | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> tuple[dict, ...]:
    """Read bounded ask/reply markers, oldest first."""
    log_path = path or default_opencode_decision_log_path()
    minimum = time.time() - max_age_seconds
    records: list[dict] = []
    for file_path in reversed(_decision_log_files(log_path)):
        for raw in _read_tail(file_path).split(b"\n"):
            if not raw or b"\x00" in raw:
                continue
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            timestamp = _optional_float(payload.get("timestamp"))
            if not isinstance(event, str) or timestamp is None:
                continue
            if event not in ASK_EVENTS and event not in REPLY_EVENTS:
                continue
            if timestamp < minimum:
                continue
            records.append(payload)
    records.sort(key=lambda item: float(item["timestamp"]))
    return tuple(records[-MAX_DECISION_RECORDS:])


def pending_decisions(
    path: Path | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> tuple[PendingDecision, ...]:
    """Return the still-open decisions, newest first."""
    return _fold_decisions(load_decision_records(path, max_age_seconds))


def _fold_decisions(records: Iterable[Mapping[str, object]]) -> tuple[PendingDecision, ...]:
    """Fold ask/reply markers into the decisions that are still open.

    A reply clears its own request id *and* every open decision for the same
    session.  Clearing by session as well is deliberate: the two payloads name
    the request differently (``id`` on the ask, ``requestID`` on the reply), so
    an identifier that stops lining up must not be able to pin a row to
    ``待确认`` for the rest of a turn.  The cost is that two prompts open at
    once in one session are both released by the first answer, which only ever
    understates waiting - the status the monitor reported before this existed.
    """
    open_decisions: dict[str, PendingDecision] = {}
    for payload in records:
        event = str(payload.get("event"))
        session_id = _optional_str(payload.get("session_id"))
        request_id = _optional_str(payload.get("request_id"))
        if event in ASK_EVENTS:
            timestamp = _optional_float(payload.get("timestamp"))
            if timestamp is None or (request_id is None and session_id is None):
                # A marker with no identifier could never be answered.
                continue
            key = request_id or f"session:{session_id}"
            open_decisions[key] = PendingDecision(
                kind=ASK_EVENTS[event],
                request_id=request_id,
                session_id=session_id,
                directory=_optional_str(payload.get("directory")),
                category=_optional_str(payload.get("category")),
                asked_at=timestamp,
            )
            continue
        if request_id is not None:
            open_decisions.pop(request_id, None)
        for key, decision in tuple(open_decisions.items()):
            if session_id is not None and decision.session_id == session_id:
                open_decisions.pop(key, None)
    return tuple(
        sorted(
            open_decisions.values(),
            key=lambda decision: decision.asked_at,
            reverse=True,
        )
    )


def find_pending_decision(
    decisions: Iterable[PendingDecision],
    *,
    session_id: str | None,
    directory: str | None,
) -> PendingDecision | None:
    """Pick the decision belonging to one monitored OpenCode row.

    The session id is an exact match and is preferred.  A decision recorded for
    the same working directory is accepted as a fallback because the plugin sees
    the directory OpenCode was started in, which is how the monitor already
    binds OpenCode processes to sessions.
    """
    candidates = tuple(decisions)
    if session_id:
        for decision in candidates:
            if decision.session_id == session_id:
                return decision
    normalized = _normalize_path(directory)
    if normalized is None:
        return None
    for decision in candidates:
        if _normalize_path(decision.directory) == normalized:
            return decision
    return None


def opencode_decision_log_health(path: Path | None = None) -> dict[str, object]:
    """Expose non-secret diagnostics about the OpenCode decision plugin log."""
    log_path = path or default_opencode_decision_log_path()
    files = _decision_log_files(log_path)
    records = load_decision_records(log_path)
    open_decisions = _fold_decisions(records)
    size = 0
    for file_path in files:
        try:
            size += file_path.stat().st_size
        except OSError:
            continue
    return {
        "path": str(log_path),
        "exists": bool(files),
        "size_bytes": size,
        "rotation_generations": max(0, len(files) - 1),
        "latest_event_at": max(
            (float(record["timestamp"]) for record in records),
            default=None,
        ),
        "record_count": len(records),
        "pending_decisions": len(open_decisions),
    }


def _decision_log_files(path: Path) -> tuple[Path, ...]:
    candidates = (
        path,
        *(
            path.with_name(f"{path.name}.{generation}")
            for generation in range(1, DECISION_LOG_GENERATIONS + 1)
        ),
    )
    return tuple(candidate for candidate in candidates if candidate.is_file())


def _read_tail(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            offset = max(0, size - MAX_TAIL_BYTES_PER_FILE)
            handle.seek(offset)
            data = handle.read(MAX_TAIL_BYTES_PER_FILE)
    except OSError:
        return b""
    if offset > 0:
        # Drop the record the tail window sliced in half.
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return data


def _normalize_path(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(Path(value).absolute())


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None
