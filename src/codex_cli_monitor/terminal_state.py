from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .codex_state import default_codex_home
from .models import SessionActivity

if TYPE_CHECKING:
    from .hook_state import HookSessionState


MAX_INITIAL_TAIL_BYTES = 4 * 1024 * 1024
MAX_INCREMENTAL_READ_BYTES = 1024 * 1024
MAX_LIFECYCLE_PREFIX_BYTES = 1024 * 1024
MAX_TERMINAL_EVENTS_PER_FILE = 64
MAX_CACHE_ENTRIES = 256
MAX_OPEN_FDS_PER_PROCESS = 4096
MAX_BOUND_SESSION_FILES = 32
TERMINAL_ACTIVE_TYPES = {"task_started"}
TERMINAL_SUCCESS_TYPES = {"task_complete", "turn_complete", "turn_completed"}
TERMINAL_FAILURE_TYPES = {
    "thread_rolled_back",
    "turn_aborted",
    "turn_failed",
}
TIMESTAMP_GRACE_SECONDS = 5.0
SESSION_ID_SUFFIX = re.compile(
    r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl$"
)


@dataclass(frozen=True)
class _TerminalEvent:
    event_type: str
    turn_id: str | None
    timestamp: float | None
    active: bool
    terminal: bool
    failed: bool


@dataclass(frozen=True)
class _TailCacheEntry:
    device: int
    inode: int
    offset: int
    carry: bytes
    events: tuple[_TerminalEvent, ...]


_TAIL_CACHE: dict[str, _TailCacheEntry] = {}
_SESSION_PATH_CACHE: dict[tuple[str, str], Path] = {}
_CACHE_LOCK = threading.Lock()


def scan_terminal_activity(
    hook_state: HookSessionState,
    codex_home: Path | None = None,
) -> SessionActivity | None:
    """Read only structured terminal events for one hook-bound Codex turn."""
    home = (codex_home or default_codex_home()).expanduser()
    path = _session_path(home, hook_state.session_id)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None

    events = _terminal_events(path, stat.st_dev, stat.st_ino, stat.st_size)
    event = _event_for_hook(events, hook_state)
    return _session_activity(
        home=home,
        path=path,
        stat=stat,
        session_id=hook_state.session_id,
        cwd=hook_state.cwd,
        event=event,
        fallback_turn_id=hook_state.turn_id,
        fallback_turn_started_at=hook_state.turn_started_at,
    )


def scan_process_terminal_activities(
    pid: int,
    proc_root: Path = Path("/proc"),
    codex_home: Path | None = None,
    cwd: str | None = None,
) -> tuple[SessionActivity, ...]:
    """Read lifecycle markers from session files opened by one exact Codex PID."""
    home = (codex_home or default_codex_home()).expanduser()
    activities = []
    for path in _open_session_paths(proc_root, pid, home):
        session_id = _session_id_from_path(path)
        if session_id is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        events = _terminal_events(path, stat.st_dev, stat.st_ino, stat.st_size)
        if not events:
            continue
        activities.append(
            _session_activity(
                home=home,
                path=path,
                stat=stat,
                session_id=session_id,
                cwd=cwd,
                event=events[-1],
            )
        )
    return tuple(
        sorted(
            activities,
            key=lambda activity: activity.last_record_at or 0.0,
            reverse=True,
        )
    )


def _session_activity(
    *,
    home: Path,
    path: Path,
    stat: os.stat_result,
    session_id: str | None,
    cwd: str | None,
    event: _TerminalEvent | None,
    fallback_turn_id: str | None = None,
    fallback_turn_started_at: float | None = None,
) -> SessionActivity:
    try:
        relative_path = path.relative_to(home).as_posix()
    except ValueError:
        relative_path = path.as_posix()
    observed_at = time.time()
    return SessionActivity(
        relative_path=relative_path,
        session_id=session_id,
        turn_id=(event.turn_id if event is not None else fallback_turn_id),
        cwd=cwd,
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        observed_at=observed_at,
        turn_started_at=(
            event.timestamp
            if event is not None and event.active
            else fallback_turn_started_at
        ),
        terminal_event_at=(
            event.timestamp
            if event is not None and event.terminal
            else None
        ),
        last_record_at=(event.timestamp if event is not None else None),
        last_record_type=("event_msg" if event is not None else None),
        last_payload_type=(event.event_type if event is not None else None),
        last_payload_reason=None,
        turn_active=(event.active if event is not None else False),
        terminal_event=(event.terminal if event is not None else False),
        failed_event=(event.failed if event is not None else False),
    )


def _open_session_paths(proc_root: Path, pid: int, home: Path) -> tuple[Path, ...]:
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return ()
    try:
        resolved_sessions_root = sessions_root.resolve()
        entries = tuple((proc_root / str(pid) / "fd").iterdir())[
            :MAX_OPEN_FDS_PER_PROCESS
        ]
    except OSError:
        return ()

    paths: set[Path] = set()
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if not target.endswith(".jsonl") or target.endswith(" (deleted)"):
            continue
        target_path = Path(target)
        if not target_path.is_absolute():
            continue
        try:
            resolved = target_path.resolve(strict=True)
            resolved.relative_to(resolved_sessions_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and _session_id_from_path(resolved) is not None:
            paths.add(resolved)
    return tuple(
        sorted(paths, key=_mtime_or_zero, reverse=True)[:MAX_BOUND_SESSION_FILES]
    )


def _session_id_from_path(path: Path) -> str | None:
    match = SESSION_ID_SUFFIX.search(path.name)
    return match.group(1) if match is not None else None


def _session_path(home: Path, session_id: str | None) -> Path | None:
    if not _safe_session_id(session_id) or not home.is_dir():
        return None
    key = (str(home), session_id)
    with _CACHE_LOCK:
        cached = _SESSION_PATH_CACHE.get(key)
    if cached is not None and cached.is_file():
        return cached

    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return None
    try:
        matches = tuple(sessions_root.glob(f"**/*{session_id}.jsonl"))
    except OSError:
        return None
    files = tuple(path for path in matches if path.is_file())
    if not files:
        return None
    selected = max(files, key=_mtime_or_zero)
    with _CACHE_LOCK:
        _SESSION_PATH_CACHE[key] = selected
        _prune_cache(_SESSION_PATH_CACHE)
    return selected


def _terminal_events(
    path: Path,
    device: int,
    inode: int,
    size: int,
) -> tuple[_TerminalEvent, ...]:
    key = str(path)
    with _CACHE_LOCK:
        cached = _TAIL_CACHE.get(key)

    reset = (
        cached is None
        or cached.device != device
        or cached.inode != inode
        or size < cached.offset
    )
    if reset:
        offset = max(0, size - MAX_INITIAL_TAIL_BYTES)
        carry = b""
        prior_events: tuple[_TerminalEvent, ...] = ()
        discard_first_partial_line = offset > 0
        recover_prefix = offset > 0
    else:
        offset = cached.offset
        carry = cached.carry
        prior_events = cached.events
        discard_first_partial_line = False
        recover_prefix = False

    if size == offset:
        return prior_events
    read_limit = MAX_INITIAL_TAIL_BYTES if reset else MAX_INCREMENTAL_READ_BYTES
    if size - offset > read_limit:
        offset = max(0, size - read_limit)
        carry = b""
        prior_events = ()
        discard_first_partial_line = offset > 0
        recover_prefix = offset > 0

    if recover_prefix:
        prior_events = _lifecycle_prefix_events(path, offset)

    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(read_limit)
    except OSError:
        return prior_events

    new_offset = offset + len(chunk)
    data = carry + chunk
    lines = data.split(b"\n")
    new_carry = lines.pop() if lines else b""
    if discard_first_partial_line and lines:
        lines.pop(0)

    parsed = list(prior_events)
    for line in lines:
        event = _terminal_event_from_line(line)
        if event is not None:
            parsed.append(event)
    events = tuple(parsed[-MAX_TERMINAL_EVENTS_PER_FILE:])
    entry = _TailCacheEntry(device, inode, new_offset, new_carry, events)
    with _CACHE_LOCK:
        _TAIL_CACHE[key] = entry
        _prune_cache(_TAIL_CACHE)
    return events


def _lifecycle_prefix_events(
    path: Path,
    tail_offset: int,
) -> tuple[_TerminalEvent, ...]:
    read_limit = min(tail_offset, MAX_LIFECYCLE_PREFIX_BYTES)
    if read_limit <= 0:
        return ()
    try:
        with path.open("rb") as handle:
            chunk = handle.read(read_limit)
    except OSError:
        return ()
    lines = chunk.split(b"\n")
    if chunk and not chunk.endswith(b"\n"):
        lines.pop()
    events = []
    for line in lines:
        event = _terminal_event_from_line(line)
        if event is not None:
            events.append(event)
    return tuple(events[-MAX_TERMINAL_EVENTS_PER_FILE:])


def _terminal_event_from_line(line: bytes) -> _TerminalEvent | None:
    if not line or b"\x00" in line:
        return None
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type in TERMINAL_ACTIVE_TYPES:
        active = True
        terminal = False
        failed = False
    elif event_type in TERMINAL_SUCCESS_TYPES:
        active = False
        terminal = True
        failed = payload.get("error") is not None
    elif event_type in TERMINAL_FAILURE_TYPES:
        active = False
        terminal = True
        failed = True
    else:
        return None
    return _TerminalEvent(
        event_type=event_type,
        turn_id=_string(payload.get("turn_id") or record.get("turn_id")),
        timestamp=_timestamp(record.get("timestamp")),
        active=active,
        terminal=terminal,
        failed=failed,
    )


def _event_for_hook(
    events: tuple[_TerminalEvent, ...],
    hook_state: HookSessionState,
) -> _TerminalEvent | None:
    expected_turn_id = hook_state.turn_id or hook_state.last_stopped_turn_id
    if expected_turn_id:
        exact = tuple(event for event in events if event.turn_id == expected_turn_id)
        if exact:
            return exact[-1]
        known_conflict = any(event.turn_id is not None for event in events)
        if known_conflict:
            return None

    started_at = hook_state.turn_started_at
    if started_at is None:
        started_at = hook_state.last_stopped_at
    candidates = tuple(
        event
        for event in events
        if event.timestamp is None
        or started_at is None
        or event.timestamp + TIMESTAMP_GRACE_SECONDS >= started_at
    )
    return candidates[-1] if candidates else None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_session_id(value: str | None) -> bool:
    return bool(
        value
        and len(value) <= 128
        and not any(character in value for character in "/\\*?[]")
    )


def _prune_cache(cache: dict) -> None:
    overflow = len(cache) - MAX_CACHE_ENTRIES
    for key in tuple(cache)[: max(0, overflow)]:
        cache.pop(key, None)


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
