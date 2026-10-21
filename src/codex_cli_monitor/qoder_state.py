"""Read-only observer of local Qoder CLI sessions.

Qoder CLI writes an append-only structured event stream for every interactive
run.  The file lives under the Qoder configuration directory at
``logs/sessions/<encoded-cwd>/<session-id>/segments/<run-id>.jsonl`` and is
held open by the exact CLI process for the lifetime of the run.  That open file
descriptor is the binding this monitor uses: a displayed row is tied to one
literal Qoder PID, so two Qoder processes in the same directory can never
inherit each other's turn state.

Only structural lifecycle records are inspected:

* ``turn.started`` opens a main-conversation turn (``data.is_subagent`` marks
  subagent turns, which are ignored).
* ``turn.finished`` closes it and carries ``data.reason``.  Qoder writes
  ``end_turn`` for a normal completion and ``error`` for API/model failures,
  refusals, malformed tool use, and interrupted requests.
* ``permission.requested`` / ``permission.resolved`` mark a tool authorization
  prompt that is waiting on the user.  The pair is matched by ``tool_call_id``;
  an unresolved request means the turn is blocked.
* ``tool.requested`` for ``AskUserQuestion`` or ``ExitPlanMode`` means the turn
  is waiting for an answer or a plan approval until the matching tool finishes
  or the model resumes.

Only structural keys are inspected; no prompt text, assistant text, tool
input, tool output, or error message is retained or emitted.  The wait label is
the Qoder tool name, clamped to a short printable string.  The transcript under
``projects/`` is not used at all, and nothing in Qoder's home is written,
locked, or mutated.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .models import (
    STATUS_FAILURE,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WAITING,
    ProcessInfo,
    normalize_waiting_reason,
)

QODER_CONFIG_DIR_ENV = "QODER_CONFIG_DIR"
DEFAULT_HOME_NAMES = (".qoder-cn", ".qoder")
SESSIONS_SUBDIR = Path("logs") / "sessions"

__all__ = [
    "STATUS_FAILURE",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_WAITING",
    "QoderSegmentState",
    "QoderSessionState",
    "default_qoder_home",
    "find_open_segments",
    "qoder_home_candidates",
    "qoder_is_interactive",
    "qoder_session_state",
    "qoder_state_health",
    "read_qoder_segment_state",
    "reset_caches",
]

# Qoder tools that stop the turn until a person answers.  Ordinary tools are
# still working, and a permission request is handled separately.
WAITING_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Qoder print mode is a one-shot non-interactive invocation, equivalent to
# ``claude -p`` / ``codex exec`` and excluded from the floating session table.
PRINT_FLAGS = frozenset({"-p", "--print"})
INTERACTIVE_FLAGS = frozenset({"-i", "--prompt-interactive"})
HEADLESS_FLAGS = frozenset({"--remote-control"})

MAX_OPEN_FDS_PER_PROCESS = 4096
MAX_BOUND_SEGMENTS = 32
MAX_INITIAL_SCAN_BYTES = 8 * 1024 * 1024
MAX_INCREMENTAL_READ_BYTES = 1024 * 1024
MAX_EVENT_LINE_BYTES = 4 * 1024 * 1024
MAX_PENDING_PERMISSIONS = 64
MAX_CACHE_ENTRIES = 256
MAX_HEALTH_PROJECT_DIRS = 512
MAX_HEALTH_SESSIONS = 4096

DEFAULT_PERMISSION_WAITING_REASON = "approval prompt"

_CACHE_LOCK = threading.Lock()
_SEGMENT_CACHE: dict[str, "_SegmentCacheEntry"] = {}


@dataclass(frozen=True)
class QoderSegmentState:
    """Structural turn facts from one Qoder run segment."""

    session_id: str | None
    turn_id: str | None
    work_submitted: bool
    turn_active: bool
    terminal_event: bool
    failed_event: bool
    status: str | None
    waiting_reason: str | None
    last_activity_at: float | None
    last_event_type: str | None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "work_submitted": self.work_submitted,
            "turn_active": self.turn_active,
            "terminal_event": self.terminal_event,
            "failed_event": self.failed_event,
            "status": self.status,
            "waiting_reason": self.waiting_reason,
            "last_activity_at": self.last_activity_at,
            "last_event_type": self.last_event_type,
        }


@dataclass(frozen=True)
class QoderSessionState:
    pid: int
    session_id: str
    segment_path: str
    cwd: str | None
    started_at: float | None
    turn_id: str | None
    turn_active: bool
    terminal_event: bool
    failed_event: bool
    status: str
    last_activity_at: float | None
    waiting_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "session_id": self.session_id,
            "segment_path": self.segment_path,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "turn_id": self.turn_id,
            "turn_active": self.turn_active,
            "terminal_event": self.terminal_event,
            "failed_event": self.failed_event,
            "status": self.status,
            "last_activity_at": self.last_activity_at,
            "waiting_reason": self.waiting_reason,
        }


@dataclass(frozen=True)
class _OpenSegment:
    path: Path
    session_id: str
    modified_at: float


@dataclass
class _ParseState:
    work_submitted: bool = False
    turn_id: str | None = None
    turn_active: bool = False
    terminal_event: bool = False
    failed_event: bool = False
    last_activity_at: float | None = None
    last_event_type: str | None = None
    pending_permissions: dict[str, str] = field(default_factory=dict)
    last_permission_key: str | None = None
    pending_ask: str | None = None
    pending_ask_id: str | None = None
    synthetic_key: int = 0
    subagent_turns: set[str] = field(default_factory=set)


@dataclass
class _SegmentCacheEntry:
    device: int
    inode: int
    offset: int
    carry: bytes
    parse: _ParseState


def default_qoder_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the Qoder configuration directory used for diagnostics."""
    env = env or os.environ
    value = env.get(QODER_CONFIG_DIR_ENV)
    if value:
        return Path(value).expanduser()
    home = Path.home()
    for name in DEFAULT_HOME_NAMES:
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / DEFAULT_HOME_NAMES[0]


def qoder_home_candidates(
    process: ProcessInfo | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return the bounded set of Qoder homes this process may use.

    An explicit ``--config-dir`` wins, then ``QODER_CONFIG_DIR``.  Without
    either, both the CN (``~/.qoder-cn``) and international (``~/.qoder``)
    defaults are considered, because one monitor may serve both builds.
    """
    env = env or os.environ
    config_dir = _config_dir_from_cmdline(process.cmdline) if process else None
    if config_dir:
        return (Path(config_dir).expanduser(),)
    value = env.get(QODER_CONFIG_DIR_ENV)
    if value:
        return (Path(value).expanduser(),)
    home = Path.home()
    return tuple(home / name for name in DEFAULT_HOME_NAMES)


def qoder_is_interactive(process: ProcessInfo) -> bool:
    """True for an interactive Qoder CLI process with a live terminal.

    Qoder runs headless workers and print-mode one-shots without a controlling
    TTY, and a terminal-less process cannot be a user-facing session.
    ``--prompt-interactive`` explicitly keeps the session interactive even
    though it starts with a prompt; a bare ``-p``/``--print`` is excluded.
    """
    if process.tty_nr is None or process.tty_nr <= 0:
        return False
    if process.tty is not None and process.tty.endswith(" (deleted)"):
        return False
    args = set(process.cmdline)
    if args.intersection(HEADLESS_FLAGS):
        return False
    if args.intersection(INTERACTIVE_FLAGS):
        return True
    if args.intersection(PRINT_FLAGS):
        return False
    return True


def find_open_segments(
    process: ProcessInfo,
    proc_root: Path = Path("/proc"),
    homes: tuple[Path, ...] | None = None,
) -> tuple[_OpenSegment, ...]:
    """Find segment JSONL files held open for writing by one exact Qoder PID.

    The segment writer descriptor is opened write-only by Qoder for the run, so
    read-only descriptors inherited for history are ignored.  Bound ownership
    plus the newest modification time make the current run unambiguous even in
    a resumed session whose older segment files are also present.
    """
    search_homes = homes or qoder_home_candidates(process)
    roots: list[tuple[Path, Path]] = []
    for home in search_homes:
        sessions_root = (home / SESSIONS_SUBDIR).resolve()
        try:
            if not sessions_root.is_dir():
                continue
        except OSError:
            continue
        roots.append((home, sessions_root))
    if not roots:
        return ()

    fd_dir = proc_root / str(process.pid) / "fd"
    try:
        entries = tuple(fd_dir.iterdir())[:MAX_OPEN_FDS_PER_PROCESS]
    except OSError:
        return ()

    found: dict[Path, _OpenSegment] = {}
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if not target.endswith(".jsonl") or target.endswith(" (deleted)"):
            continue
        if not _is_write_descriptor(proc_root, process.pid, entry.name):
            continue
        try:
            resolved = Path(target).resolve(strict=True)
        except OSError:
            continue
        for _home, sessions_root in roots:
            try:
                relative = resolved.relative_to(sessions_root)
            except ValueError:
                continue
            session_id = _session_id_from_relative(relative)
            if session_id is None:
                continue
            try:
                modified_at = resolved.stat().st_mtime
            except OSError:
                continue
            found[resolved] = _OpenSegment(
                path=resolved,
                session_id=session_id,
                modified_at=modified_at,
            )
            break
    return tuple(
        sorted(found.values(), key=lambda item: item.modified_at, reverse=True)
    )[:MAX_BOUND_SEGMENTS]


def qoder_session_state(
    process: ProcessInfo,
    home: Path | None = None,
    proc_root: Path = Path("/proc"),
    homes: tuple[Path, ...] | None = None,
) -> QoderSessionState | None:
    """Return displayable state for one live, interactive Qoder CLI process.

    Returns ``None`` when the process is headless or one-shot, holds no segment
    writer, or has not submitted a main-conversation turn, so a freshly opened
    Qoder CLI stays hidden until it actually starts working.
    """
    if not qoder_is_interactive(process):
        return None
    candidates = (home,) if home is not None else homes
    segments = find_open_segments(process, proc_root=proc_root, homes=candidates)
    if not segments:
        return None
    segment = segments[0]
    parsed = read_qoder_segment_state(segment.path, session_id=segment.session_id)
    if parsed.status is None:
        return None
    return QoderSessionState(
        pid=process.pid,
        session_id=segment.session_id,
        segment_path=str(segment.path),
        cwd=process.cwd,
        started_at=process.started_at,
        turn_id=parsed.turn_id,
        turn_active=parsed.turn_active,
        terminal_event=parsed.terminal_event,
        failed_event=parsed.failed_event,
        status=parsed.status,
        last_activity_at=parsed.last_activity_at,
        waiting_reason=parsed.waiting_reason,
    )


def read_qoder_segment_state(
    path: Path,
    session_id: str | None = None,
) -> QoderSegmentState:
    """Incrementally parse one segment file and return the latest turn facts.

    Only complete JSONL records are parsed.  A partial trailing record is
    carried into the next refresh, and a corrupt record is skipped exactly like
    an incomplete one.  The read window is bounded on every refresh so one
    enormous segment cannot stall the resident scan.
    """
    try:
        info = path.stat()
    except OSError:
        return _snapshot(_ParseState(), session_id)

    key = str(path)
    with _CACHE_LOCK:
        entry = _SEGMENT_CACHE.get(key)
        if (
            entry is None
            or entry.device != info.st_dev
            or entry.inode != info.st_ino
            or info.st_size < entry.offset
        ):
            entry = _SegmentCacheEntry(
                device=info.st_dev,
                inode=info.st_ino,
                offset=0,
                carry=b"",
                parse=_ParseState(),
            )
            _SEGMENT_CACHE[key] = entry
            first_read = True
        else:
            first_read = False

        if info.st_size > entry.offset:
            limit = (
                MAX_INITIAL_SCAN_BYTES if first_read
                else MAX_INCREMENTAL_READ_BYTES
            )
            _read_append(entry, path, min(limit, info.st_size - entry.offset))

        if len(_SEGMENT_CACHE) > MAX_CACHE_ENTRIES:
            _SEGMENT_CACHE.clear()
            _SEGMENT_CACHE[key] = entry

        return _snapshot(entry.parse, session_id)


def qoder_state_health(home: Path | None = None) -> dict[str, object]:
    """Expose non-secret diagnostics about the Qoder observation path."""
    home = (home or default_qoder_home()).expanduser()
    sessions = home / SESSIONS_SUBDIR
    session_directories = 0
    segments = 0
    try:
        projects = list(sessions.iterdir())[:MAX_HEALTH_PROJECT_DIRS]
    except OSError:
        projects = []
        sessions_exists = False
    else:
        sessions_exists = sessions.is_dir()
    for project in projects:
        try:
            session_entries = list(project.iterdir())
        except OSError:
            continue
        for session in session_entries:
            if session_directories >= MAX_HEALTH_SESSIONS:
                break
            segment_dir = session / "segments"
            try:
                if not segment_dir.is_dir():
                    continue
                file_count = sum(
                    1
                    for item in segment_dir.iterdir()
                    if item.name.endswith(".jsonl")
                )
            except OSError:
                continue
            session_directories += 1
            segments += file_count
    return {
        "home": str(home),
        "home_exists": home.is_dir(),
        "logs_dir_exists": (home / "logs").is_dir(),
        "sessions_dir_exists": sessions_exists,
        "session_directories": session_directories,
        "segments": segments,
    }


def reset_caches() -> None:
    """Clear cached segment parsers (used by tests)."""
    with _CACHE_LOCK:
        _SEGMENT_CACHE.clear()


def _read_append(entry: _SegmentCacheEntry, path: Path, length: int) -> None:
    try:
        with path.open("rb") as handle:
            handle.seek(entry.offset)
            chunk = handle.read(length)
    except OSError:
        return
    if not chunk:
        return
    entry.offset += len(chunk)
    data = entry.carry + chunk
    lines = data.split(b"\n")
    entry.carry = lines.pop()
    if len(entry.carry) > MAX_EVENT_LINE_BYTES:
        # A single pathological record is not a lifecycle signal this monitor
        # can use, so drop it instead of carrying unbounded bytes.
        entry.carry = b""
    for raw in lines:
        record = _record_from_line(raw)
        if record is not None:
            _apply_record(entry.parse, record)


def _apply_record(state: _ParseState, record: Mapping) -> None:
    event_type = record.get("type")
    if not isinstance(event_type, str):
        return
    data = record.get("data")
    if not isinstance(data, Mapping):
        data = {}
    timestamp = _iso_to_seconds(record.get("ts"))
    if timestamp is not None:
        state.last_activity_at = timestamp
    state.last_event_type = event_type

    turn_id = _optional_str(record.get("turn_id"))
    if turn_id is not None and turn_id in state.subagent_turns:
        return

    if event_type == "turn.started":
        if data.get("is_subagent") is True:
            if turn_id is not None and len(state.subagent_turns) < MAX_PENDING_PERMISSIONS:
                state.subagent_turns.add(turn_id)
            return
        state.work_submitted = True
        state.turn_id = turn_id
        state.turn_active = True
        state.terminal_event = False
        state.failed_event = False
        state.pending_permissions.clear()
        state.last_permission_key = None
        state.pending_ask = None
        state.pending_ask_id = None
        return

    if not state.turn_active:
        # A terminal event without an observed start still proves a turn was
        # submitted; only accept it when no other turn is open.
        if event_type == "turn.finished":
            state.work_submitted = True
            state.terminal_event = True
            state.turn_id = turn_id
            state.failed_event = _turn_failed(data)
        return

    if turn_id is not None and state.turn_id is not None and turn_id != state.turn_id:
        # Events belonging to another (for example subagent) turn never affect
        # the current main-conversation outcome.
        return

    if event_type == "turn.finished":
        state.turn_active = False
        state.terminal_event = True
        state.failed_event = _turn_failed(data)
        state.pending_permissions.clear()
        state.last_permission_key = None
        state.pending_ask = None
        state.pending_ask_id = None
        return

    if event_type == "permission.requested":
        tool_name = (
            normalize_waiting_reason(data.get("tool_name"))
            or DEFAULT_PERMISSION_WAITING_REASON
        )
        key = _optional_str(record.get("tool_call_id"))
        if key is None:
            state.synthetic_key += 1
            key = f"permission-{state.synthetic_key}"
        if len(state.pending_permissions) >= MAX_PENDING_PERMISSIONS:
            oldest = next(iter(state.pending_permissions), None)
            if oldest is not None:
                state.pending_permissions.pop(oldest, None)
        state.pending_permissions[key] = tool_name
        state.last_permission_key = key
        return

    if event_type == "permission.resolved":
        key = _optional_str(record.get("tool_call_id"))
        if key is not None and key in state.pending_permissions:
            state.pending_permissions.pop(key, None)
        elif state.pending_permissions:
            # A resolved request that no longer has a matching id (for example
            # when the first scan began mid-prompt) releases the oldest one;
            # keeping it forever would pin the row to ``待确认``.
            state.pending_permissions.pop(next(iter(state.pending_permissions)))
        state.last_permission_key = next(
            reversed(tuple(state.pending_permissions)),
            None,
        )
        return

    if event_type == "tool.requested":
        tool_name = normalize_waiting_reason(data.get("tool_name"))
        if tool_name in WAITING_TOOL_NAMES:
            state.pending_ask = tool_name
            state.pending_ask_id = _optional_str(record.get("tool_call_id"))
        return

    if event_type in {"tool.execution.finished", "tool.shell.finished"}:
        tool_call_id = _optional_str(record.get("tool_call_id"))
        if state.pending_ask is not None and (
            state.pending_ask_id is None
            or tool_call_id is None
            or tool_call_id == state.pending_ask_id
        ):
            state.pending_ask = None
            state.pending_ask_id = None
        return

    if event_type == "model.request.started":
        # A new model request can only follow an answered question or an
        # approved plan, so any outstanding ask-tool wait is over.
        state.pending_ask = None
        state.pending_ask_id = None


def _turn_failed(data: Mapping) -> bool:
    if data.get("error") is not None:
        return True
    return data.get("reason") == "error"


def _snapshot(state: _ParseState, session_id: str | None) -> QoderSegmentState:
    status, waiting_reason = _derive_status(state)
    return QoderSegmentState(
        session_id=session_id,
        turn_id=state.turn_id,
        work_submitted=state.work_submitted,
        turn_active=state.turn_active,
        terminal_event=state.terminal_event,
        failed_event=state.failed_event,
        status=status,
        waiting_reason=waiting_reason,
        last_activity_at=state.last_activity_at,
        last_event_type=state.last_event_type,
    )


def _derive_status(state: _ParseState) -> tuple[str | None, str | None]:
    if state.turn_active:
        if state.pending_permissions:
            key = state.last_permission_key
            reason = state.pending_permissions.get(key) if key else None
            if reason is None and state.pending_permissions:
                reason = next(reversed(tuple(state.pending_permissions.values())))
            return STATUS_WAITING, reason or DEFAULT_PERMISSION_WAITING_REASON
        if state.pending_ask is not None:
            return STATUS_WAITING, state.pending_ask
        return STATUS_RUNNING, None
    if state.work_submitted and state.terminal_event:
        return (STATUS_FAILURE if state.failed_event else STATUS_SUCCESS), None
    return None, None


def _record_from_line(raw: bytes) -> Mapping | None:
    if not raw or b"\x00" in raw:
        return None
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _config_dir_from_cmdline(cmdline: tuple[str, ...]) -> str | None:
    for index, argument in enumerate(cmdline):
        if argument == "--config-dir" and index + 1 < len(cmdline):
            return cmdline[index + 1]
        if argument.startswith("--config-dir="):
            return argument.split("=", 1)[1]
    return None


def _is_write_descriptor(proc_root: Path, pid: int, fd_name: str) -> bool:
    path = proc_root / str(pid) / "fdinfo" / fd_name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Test proc trees and restricted environments expose no fdinfo; the
        # resolved open descriptor is still an exact PID binding.
        return True
    for line in text.splitlines():
        if line.startswith("flags:"):
            try:
                flags = int(line.split()[1], 8)
            except (IndexError, ValueError):
                return True
            return (flags & os.O_ACCMODE) != os.O_RDONLY
    return True


def _session_id_from_relative(relative: Path) -> str | None:
    parts = relative.parts
    if len(parts) != 4:
        return None
    project, session_id, segments, filename = parts
    if (
        not project
        or not session_id
        or segments != "segments"
        or not filename.endswith(".jsonl")
    ):
        return None
    return session_id


def _iso_to_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
