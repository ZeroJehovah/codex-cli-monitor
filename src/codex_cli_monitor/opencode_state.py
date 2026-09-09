"""Read-only observer of local OpenCode CLI sessions.

OpenCode stores its conversations in a local SQLite database
(``~/.local/share/opencode/opencode.db``).  This module opens that database
strictly read-only, never writes to it, and infers a small user-facing
lifecycle (running / success / failure) from minimal structural records:

* ``session`` rows give the stable session id, working directory, and
  creation/update times.
* ``message`` rows carry ``role`` (user/assistant), ``time.created`` and
  ``time.completed``, plus ``finish`` and the presence of a structured
  ``error`` for assistant messages. Completion closes one model step;
  ``tool-calls``, ``unknown``, and a missing finish keep the turn open.
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
SESSIONS_PER_DIRECTORY_LIMIT = SESSION_LIMIT
ACTIVE_SESSION_GRACE_SECONDS = 60.0
_terminated_cached: dict[Path, tuple[object, ...]] = {}

_CACHE_LOCK = threading.Lock()
_STATE_CACHE: dict[
    Path,
    tuple[
        tuple[object, ...],
        tuple[tuple[str, ...], tuple[str, ...], int],
        tuple["OpenCodeSessionState", ...],
    ],
] = {}

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
    directories: tuple[str, ...] = (),
) -> tuple[OpenCodeSessionState, ...]:
    """Read session lifecycle states from the OpenCode SQLite database.

    ``ids`` optionally restricts the scan to a fixed set of stable session
    ids. ``directories`` limits the fallback scan to sessions in the supplied
    working directories. Both filters are intentionally scoped to live
    OpenCode processes; the monitor never needs to load the complete session
    history just to find the current rows.
    """
    db = opencode_db_path(data_dir)
    if not db.is_file():
        return ()
    try:
        info = db.stat()
    except OSError:
        return ()

    signature = _db_signature(db, info)
    query_key = (
        tuple(sorted({item for item in ids if item})),
        tuple(sorted({item for item in directories if item})),
        SESSIONS_PER_DIRECTORY_LIMIT,
    )
    with _CACHE_LOCK:
        cached = _STATE_CACHE.get(db)
        if cached is not None:
            cached_signature, cached_query_key, states = cached
            if cached_signature == signature and cached_query_key == query_key:
                return states

    states = _read_session_states(
        db,
        ids=query_key[0],
        directories=query_key[1],
        directory_limit=SESSIONS_PER_DIRECTORY_LIMIT,
    )
    with _CACHE_LOCK:
        _STATE_CACHE[db] = (signature, query_key, states)
    return states


def _db_signature(db: Path, db_stat: os.stat_result) -> tuple[object, ...]:
    """Build a cache-invalidation signature for an OpenCode SQLite database.

    OpenCode runs its database in WAL journal mode.  In WAL mode, new writes
    land in ``opencode.db-wal`` while the main ``opencode.db`` file's mtime
    and size may not change until a checkpoint runs.  A signature that only
    inspects the main file would therefore serve stale cached state long
    after a session transitioned from ``运行中`` to ``成功`` or ``失败``.
    Including the WAL file (size + mtime) ensures the cache invalidates on
    every write.
    """
    signature: tuple[object, ...] = (db_stat.st_size, db_stat.st_mtime_ns, db_stat.st_ino)
    wal = db.with_name(db.name + "-wal")
    try:
        wal_stat = wal.stat()
    except OSError:
        return signature
    return signature + (wal_stat.st_size, wal_stat.st_mtime_ns)


def _read_session_states(
    db: Path,
    *,
    ids: tuple[str, ...],
    directories: tuple[str, ...],
    directory_limit: int,
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
        sessions = _query_sessions(
            connection,
            ids=ids,
            directories=directories,
            directory_limit=directory_limit,
        )
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
    *,
    ids: tuple[str, ...],
    directories: tuple[str, ...],
    directory_limit: int,
) -> tuple[dict, ...]:
    columns = "id, directory, title, time_created, time_updated"
    result: list[dict] = []
    known: set[str] = set()

    def add_rows(rows: list[sqlite3.Row] | list[tuple]) -> None:
        for row in rows:
            session_id = str(row[0])
            if session_id in known:
                continue
            known.add(session_id)
            result.append(
                {
                    "id": session_id,
                    "directory": _optional_str(row[1]),
                    "title": _optional_str(row[2]),
                    "time_created": _optional_int(row[3]),
                    "time_updated": _optional_int(row[4]),
                }
            )

    if directories:
        query = (
            f"SELECT {columns} FROM session WHERE directory = ? "
            "ORDER BY time_updated DESC, id DESC LIMIT ?"
        )
        for directory in directories:
            add_rows(connection.execute(query, (directory, directory_limit)).fetchall())
    else:
        query = (
            f"SELECT {columns} FROM session "
            "ORDER BY time_updated DESC, id DESC LIMIT ?"
        )
        add_rows(connection.execute(query, (SESSION_LIMIT,)).fetchall())

    if ids:
        placeholders = ",".join("?" for _ in ids)
        query = f"SELECT {columns} FROM session WHERE id IN ({placeholders})"
        add_rows(connection.execute(query, ids).fetchall())
    return tuple(result)


def _query_messages(
    connection: sqlite3.Connection,
    sessions: tuple[dict, ...],
) -> dict[str, tuple[dict, ...]]:
    if not sessions:
        return {}
    result: dict[str, list[dict]] = {session["id"]: [] for session in sessions}
    safe_data = "CASE WHEN json_valid(data) THEN data ELSE '{}' END"
    query = (
        "SELECT json_object("
        f"'role', json_extract({safe_data}, '$.role'), "
        f"'time', json_object('created', json_extract({safe_data}, '$.time.created'), "
        f"'completed', json_extract({safe_data}, '$.time.completed')), "
        f"'finish', json_extract({safe_data}, '$.finish'), "
        f"'failed', json_type({safe_data}, '$.error') IS NOT NULL AND "
        f"json_type({safe_data}, '$.error') != 'null'), time_created FROM message "
        "WHERE session_id = ? AND "
        f"json_extract({safe_data}, '$.role') = ? "
        "ORDER BY time_created DESC, id DESC LIMIT 1"
    )
    for session in sessions:
        session_id = session["id"]
        for role in ("user", "assistant"):
            row = connection.execute(query, (session_id, role)).fetchone()
            if row is None:
                continue
            parsed = _parse_message_data(str(row[0]), row[1])
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
        "SELECT session_id, MAX(time_updated) FROM part "
        "WHERE session_id IN ({}) AND "
        "json_extract(CASE WHEN json_valid(data) THEN data ELSE '{{}}' END, "
        "'$.state.status') = 'running' "
        "GROUP BY session_id"
    ).format(placeholders)
    rows = connection.execute(query, ids).fetchall()
    result: dict[str, list[str]] = {session["id"]: [] for session in sessions}
    for session_id, time_updated in rows:
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
        "failed": bool(data.get("failed")),
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
    current_failed = bool(current_assistant and current_assistant["failed"])
    last_running_tool = running_tools[0] if running_tools else None

    # OpenCode completes each model step before starting the next one. There
    # need not be a running tool during that handoff, even in a healthy turn.
    step_continues = not current_finish or current_finish in ("tool-calls", "unknown")
    if current_completed is None or (
        not current_failed and (last_running_tool is not None or step_continues)
    ):
        turn_active = True
        status = STATUS_RUNNING
        terminal_event = False
        failed_event = False
    else:
        turn_active = False
        terminal_event = True
        failed_event = current_failed or current_finish != "stop"
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
