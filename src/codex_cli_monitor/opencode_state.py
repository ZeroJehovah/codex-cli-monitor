"""Read-only observer of local OpenCode CLI sessions.

OpenCode stores its conversations in a local SQLite database
(``~/.local/share/opencode/opencode.db``).  This module opens that database
strictly read-only, never writes to it, and infers a small user-facing
lifecycle (running / success / failure) from minimal structural records:

* ``session`` rows give the stable session id, working directory, and
  creation/update times.
* ``message`` rows carry ``role`` (user/assistant), ``time.created`` and
  ``time.completed``, plus ``finish`` (``stop``/``tool-calls``) for assistant
  messages.
* ``part`` rows carry tool-call state, including ``running`` obstacles that
  prove a turn is still in flight.

A session is displayed for a live ``opencode`` process only when that process
holds the database open (or the directory matches and a hook marker exists).
Only minimal structured fields are used; prompt/assistant text and tool
outputs are never read or classified.

An optional, separately installed lifecycle hook writes small JSONL markers
(``UserPromptSubmit`` / ``Stop`` equivalents) into a local bounded log; those
records confirm process-to-session binding and expose an unambiguous
exit edge even before the database flushes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

OPENCODE_DATA_DIR_ENV = "OPENCODE_DATA"
OPENCODE_HOME_ENV = "OPENCODE_HOME"
OPENCODE_STATE_DIR = "storage"
OPENCODE_DB_FILE = "opencode.db"
OPENCODE_HOOK_LOG_ENV = "OPENCODE_MONITOR_HOOK_LOG"
DEFAULT_HOOK_LOG_DIR = "opencode-cli-monitor"
DEFAULT_HOOK_LOG_NAME = "hooks.jsonl"

OFFICIAL_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "stop": "Stop",
}

DB_OPEN_TIMEOUT_SECONDS = 0.05
DB_READ_ONLY_URI = "file:{path}?mode=ro&immutable=0"
SESSION_LIMIT = 256
ACTIVE_SESSION_GRACE_SECONDS = 60.0
MAX_DB_SIZE_BYTES = 512 * 1024 * 1024
MAX_SESSIONS_PER_CACHE = 64
MAX_STATE_CACHE_ENTRIES = 256
DB_SIGNATURE_KEY = ("size", "mtime_ns", "inode")
_terminated_cached: dict[Path, tuple[object, ...]] = {}

_CACHE_LOCK = threading.Lock()
_STATE_CACHE: dict[Path, object] = {}
_STATE_SIGNATURE: dict[Path, object] = {}

STATUS_RUNNING = "运行中"
STATUS_SUCCESS = "成功"
STATUS_FAILURE = "失败"


@dataclass(frozen=True)
class OpenCodeSessionState:
    session_id: str
    cwd: str | None
    title: str | None
    created_at: float | None
    updated_at: float | None
    last_activity_at: float | None
    turn_started_at: float | None
    turn_active: bool
    terminal_event: bool
    failed_event: bool
    status: str

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "turn_started_at": self.turn_started_at,
            "turn_active": self.turn_active,
            "terminal_event": self.terminal_event,
            "failed_event": self.failed_event,
            "status": self.status,
        }


class OpenCodeDBError(OSError):
    """Raised when the OpenCode database cannot be opened read-only safely."""




def default_opencode_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the OpenCode data directory holding ``opencode.db``.

    The database sits directly in the data directory (for example
    ``~/.local/share/opencode/opencode.db``).  Honors ``OPENCODE_DATA`` and
    ``OPENCODE_HOME`` (used by newer OpenCode builds), falling back to the
    XDG data location.
    """
    env = env or os.environ
    for key in (OPENCODE_DATA_DIR_ENV, OPENCODE_HOME_ENV):
        value = env.get(key)
        if value:
            return Path(value).expanduser()
    xdg = Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return xdg / "opencode"


def opencode_db_path(data_dir: Path | None = None) -> Path:
    data_dir = data_dir or default_opencode_data_dir()
    return data_dir / OPENCODE_DB_FILE


def default_opencode_hook_log_path(env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    if env.get(OPENCODE_HOOK_LOG_ENV):
        return Path(env[OPENCODE_HOOK_LOG_ENV]).expanduser()
    if env.get("XDG_STATE_HOME"):
        state_home = Path(env["XDG_STATE_HOME"]).expanduser()
    else:
        state_home = Path.home() / ".local" / "state"
    return state_home / DEFAULT_HOOK_LOG_DIR / DEFAULT_HOOK_LOG_NAME


def opencode_hook_events(
    path: Path | None = None,
    max_age_seconds: float = 24 * 3600,
) -> tuple[dict, ...]:
    """Read bounded OpenCode hook JSONL markers (event, timestamp, pid, cwd)."""
    log_path = path or default_opencode_hook_log_path()
    try:
        with log_path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 4 * 1024 * 1024))
            lines = handle.read()
    except OSError:
        return ()
    events: list[dict] = []
    minimum = time.time() - max_age_seconds
    for raw in lines.split(b"\n"):
        if not raw or b"\x00" in raw:
            continue
        try:
            item = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        ts = _optional_float(item.get("timestamp"))
        if ts is None or ts < minimum:
            continue
        events.append(item)
    return tuple(events)


def scan_opencode_state(
    data_dir: Path | None = None,
    ids: tuple[str, ...] = (),
) -> tuple[OpenCodeSessionState, ...]:
    """Read session lifecycle states from the OpenCode SQLite database.

    ``ids`` optionally restricts the scan to a fixed set of stable session
    ids (used to keep a PID-open session visible even when the database
    becomes large).
    """
    db = opencode_db_path(data_dir)
    if not db.is_file():
        return ()
    try:
        info = db.stat()
    except OSError:
        return ()
    if info.st_size > MAX_DB_SIZE_BYTES:
        return ()

    signature = (info.st_size, info.st_mtime_ns, info.st_ino)
    with _CACHE_LOCK:
        if _STATE_SIGNATURE.get(db) == signature and _STATE_CACHE.get(db) is not None:
            cached = _STATE_CACHE[db]
            return tuple(cached) if isinstance(cached, tuple) else _states_from_cached(cached)

    states = _read_session_states(db, ids)
    cache_value: object
    if len(states) > MAX_STATE_CACHE_ENTRIES:
        cache_value = (states[:MAX_STATE_CACHE_ENTRIES],)
    else:
        cache_value = states
    with _CACHE_LOCK:
        _STATE_CACHE[db] = cache_value
        _STATE_SIGNATURE[db] = signature
    return states


def _states_from_cached(cached: object) -> tuple[OpenCodeSessionState, ...]:
    if isinstance(cached, tuple) and cached and isinstance(cached[0], OpenCodeSessionState):
        return cached
    return ()


def _read_session_states(
    db: Path,
    ids: tuple[str, ...],
) -> tuple[OpenCodeSessionState, ...]:
    try:
        connection = sqlite3.connect(
            DB_READ_ONLY_URI.format(path=db),
            uri=True,
            timeout=DB_OPEN_TIMEOUT_SECONDS,
        )
    except sqlite3.Error as error:
        return ()
    try:
        try:
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        sessions = _query_sessions(connection, ids)
        messages = _query_messages(connection, sessions)
        tools = _query_running_tools(connection, sessions)
    except sqlite3.Error:
        return ()
    finally:
        connection.close()

    states = []
    for session in sessions:
        state, last_tools = _build_state(
            session,
            messages.get(session["id"], ()),
            tools.get(session["id"], ()),
        )
        if state is not None:
            states.append(state)
    return tuple(states)


def _query_sessions(
    connection: sqlite3.Connection,
    ids: tuple[str, ...],
) -> tuple[dict, ...]:
    query = (
        "SELECT id, directory, title, time_created, time_updated "
        "FROM session ORDER BY time_updated DESC LIMIT ?"
    )
    rows = connection.execute(query, (SESSION_LIMIT,)).fetchall()
    result = [
        {
            "id": str(row[0]),
            "directory": _optional_str(row[1]),
            "title": _optional_str(row[2]),
            "time_created": _optional_int(row[3]),
            "time_updated": _optional_int(row[4]),
        }
        for row in rows
    ]
    known = {session["id"] for session in result}
    for session_id in ids:
        if session_id in known:
            continue
        row = connection.execute(
            "SELECT id, directory, title, time_created, time_updated "
            "FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            continue
        result.append(
            {
                "id": str(row[0]),
                "directory": _optional_str(row[1]),
                "title": _optional_str(row[2]),
                "time_created": _optional_int(row[3]),
                "time_updated": _optional_int(row[4]),
            }
        )
    return tuple(result)


def _query_messages(
    connection: sqlite3.Connection,
    sessions: tuple[dict, ...],
) -> dict[str, tuple[dict, ...]]:
    if not sessions:
        return {}
    ids = tuple(session["id"] for session in sessions)
    placeholders = ",".join("?" for _ in ids)
    query = (
        "SELECT id AS message_id, session_id, data, time_created "
        "FROM message WHERE session_id IN ({}) ORDER BY time_created ASC"
    ).format(placeholders)
    rows = connection.execute(query, ids).fetchall()
    result: dict[str, list[dict]] = {session["id"]: [] for session in sessions}
    for message_id, session_id, data, time_created in rows:
        parsed = _parse_message_data(str(data), time_created)
        if parsed is not None:
            result[session_id].append(parsed)
    return {session_id: tuple(items) for session_id, items in result.items()}


def _query_running_tools(
    connection: sqlite3.Connection,
    sessions: tuple[dict, ...],
) -> dict[str, tuple[str, ...]]:
    if not sessions:
        return {}
    ids = tuple(session["id"] for session in sessions)
    placeholders = ",".join("?" for _ in ids)
    query = (
        "SELECT session_id, data, time_updated FROM part "
        "WHERE session_id IN ({}) AND json_extract(data, '$.state.status') = 'running'"
    ).format(placeholders)
    rows = connection.execute(query, ids).fetchall()
    result: dict[str, list[str]] = {session["id"]: [] for session in sessions}
    for session_id, data, time_updated in rows:
        result[session_id].append(str(time_updated))
    return {session_id: tuple(sorted(items, reverse=True)) for session_id, items in result.items()}


def _parse_message_data(raw: str, time_created: int) -> dict | None:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    role = data.get("role")
    msg_time = data.get("time")
    if not isinstance(role, str) or not isinstance(msg_time, dict):
        return None
    return {
        "role": role,
        "created": _optional_positive_int(msg_time.get("created")),
        "completed": _optional_positive_int(msg_time.get("completed")),
        "finish": _optional_str(data.get("finish")),
        "message_time_created": _optional_positive_int(time_created),
    }


def _build_state(
    session: dict,
    messages: tuple[dict, ...],
    running_tools: tuple[str, ...],
) -> tuple[OpenCodeSessionState | None, tuple[dict, ...]]:
    created = _ms_to_seconds(session["time_created"])
    updated = _ms_to_seconds(session["time_updated"])
    assistant_messages = [item for item in messages if item["role"] == "assistant"]
    user_messages = [item for item in messages if item["role"] == "user"]

    last_activity = updated
    for item in messages:
        candidate = item["completed"] or item["created"] or item["message_time_created"]
        if candidate is not None:
            last_activity = max(last_activity, candidate)

    turn_started = None
    if user_messages:
        turn_started = user_messages[-1]["created"]
    if last_activity is None:
        last_activity = created

    current_assistant = assistant_messages[-1] if assistant_messages else None
    current_completed = current_assistant["completed"] if current_assistant else None
    current_finish = current_assistant["finish"] if current_assistant else None
    last_running_tool = running_tools[0] if running_tools else None

    if current_completed is None:
        turn_active = True
        status = STATUS_RUNNING
        terminal_event = False
        failed_event = False
    elif last_running_tool is not None:
        turn_active = True
        status = STATUS_RUNNING
        terminal_event = False
        failed_event = False
    else:
        turn_active = False
        terminal_event = True
        failed_event = bool(current_finish and current_finish != "stop")
        status = STATUS_FAILURE if failed_event else STATUS_SUCCESS

    state = OpenCodeSessionState(
        session_id=session["id"],
        cwd=session["directory"],
        title=session["title"],
        created_at=created,
        updated_at=updated,
        last_activity_at=last_activity,
        turn_started_at=turn_started,
        turn_active=turn_active,
        terminal_event=terminal_event,
        failed_event=failed_event,
        status=status,
    )
    return state, ()


def _ms_to_seconds(value: int | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(obj: object) -> float | None:
    if obj is None:
        return None
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_positive_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None