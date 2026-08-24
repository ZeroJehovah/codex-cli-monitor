"""Read-only observer of local Claude Code CLI sessions.

Claude Code registers every live interactive session in a small JSON file at
``~/.claude/sessions/<pid>.json``.  That file is written by Claude Code itself
and carries exactly the minimal structural facts this monitor needs:

* ``pid`` and ``procStart`` (the ``/proc/<pid>/stat`` start-time tick count),
  which together bind a registration to one exact process and make PID reuse
  detectable.
* ``sessionId`` and ``cwd``, the stable conversation identity and directory.
* ``kind`` (``interactive``/``bg``/``daemon``/``daemon-worker``) and
  ``entrypoint``.
* ``status``, one of ``busy``/``shell``/``waiting``/``idle``, rewritten by
  Claude Code on every transition together with ``statusUpdatedAt``.

The registration alone decides ``运行中``: a session whose status is ``busy``
(model or tool work in flight), ``shell`` (a shell command is running), or
``waiting`` (an opened turn is blocked on a user decision) still has an open
turn.  ``idle`` means no turn is in flight, so the outcome of the most recent
turn is read from the bound transcript instead.

The transcript lives at
``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl``.  Only a bounded tail is
read, and only structural fields are inspected: the record ``type`` plus the
``isApiErrorMessage`` and ``isAbortedMidStream`` flags Claude Code writes on an
assistant record when a turn ended in an API error or was interrupted.  Prompt
text, assistant text, tool inputs, and tool outputs are never read or
classified.

Nothing in this module writes to, locks, or otherwise mutates Claude Code
state; the CLI and its installed package stay untouched.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .models import ProcessInfo

CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_SESSIONS_DIR = "sessions"
CLAUDE_PROJECTS_DIR = "projects"

STATUS_RUNNING = "运行中"
STATUS_SUCCESS = "成功"
STATUS_FAILURE = "失败"

# Status values Claude Code writes while a submitted turn is still open.  Any
# other value (``idle`` or an unrecognized future status) falls back to the
# structured transcript outcome so an unknown status can never pin a row to
# ``运行中`` forever.
WORKING_STATUSES = frozenset({"busy", "shell", "waiting"})

# Only interactive sessions represent a user Codex-style session.  Background,
# daemon, and daemon-worker registrations are maintenance or automation
# process trees and stay hidden, mirroring the ``codex exec`` exclusion.
DISPLAY_KINDS = frozenset({"interactive"})

MAX_REGISTRATION_BYTES = 64 * 1024
MAX_REGISTRATION_FILES = 512
TRANSCRIPT_TAIL_BYTES = 1024 * 1024
MAX_PROJECT_DIRS = 512
MAX_TRANSCRIPT_CACHE_ENTRIES = 256

_PROJECT_DIR_UNSAFE = re.compile(r"[^a-zA-Z0-9]")

_CACHE_LOCK = threading.Lock()
_TRANSCRIPT_CACHE: dict[Path, tuple[tuple[object, ...], "TranscriptOutcome"]] = {}
_TRANSCRIPT_PATHS: dict[tuple[str, str], Path] = {}


@dataclass(frozen=True)
class TranscriptOutcome:
    """Structural lifecycle facts read from a bounded transcript tail."""

    assistant_seen: bool = False
    terminal_event: bool = False
    failed_event: bool = False
    last_activity_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "assistant_seen": self.assistant_seen,
            "terminal_event": self.terminal_event,
            "failed_event": self.failed_event,
            "last_activity_at": self.last_activity_at,
        }


@dataclass(frozen=True)
class ClaudeSessionState:
    pid: int
    session_id: str
    cwd: str | None
    kind: str | None
    entrypoint: str | None
    version: str | None
    registered_status: str | None
    started_at: float | None
    updated_at: float | None
    status_updated_at: float | None
    last_activity_at: float | None
    turn_active: bool
    terminal_event: bool
    failed_event: bool
    status: str
    transcript_path: str | None

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "version": self.version,
            "registered_status": self.registered_status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "status_updated_at": self.status_updated_at,
            "last_activity_at": self.last_activity_at,
            "turn_active": self.turn_active,
            "terminal_event": self.terminal_event,
            "failed_event": self.failed_event,
            "status": self.status,
            "transcript_path": self.transcript_path,
        }


def default_claude_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the Claude Code configuration directory (``~/.claude``)."""
    env = env or os.environ
    value = env.get(CLAUDE_CONFIG_DIR_ENV)
    if value:
        return Path(value).expanduser()
    return Path.home() / ".claude"


def claude_sessions_dir(home: Path | None = None) -> Path:
    return (home or default_claude_home()) / CLAUDE_SESSIONS_DIR


def claude_projects_dir(home: Path | None = None) -> Path:
    return (home or default_claude_home()) / CLAUDE_PROJECTS_DIR


def encode_project_dir(cwd: str) -> str:
    """Encode a working directory the way Claude Code names project folders."""
    return _PROJECT_DIR_UNSAFE.sub("-", cwd)


def read_session_registration(pid: int, home: Path | None = None) -> dict | None:
    """Read ``~/.claude/sessions/<pid>.json`` without ever writing to it."""
    path = claude_sessions_dir(home) / f"{pid}.json"
    try:
        info = path.stat()
    except OSError:
        return None
    if not info.st_size or info.st_size > MAX_REGISTRATION_BYTES:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def registration_matches_process(data: Mapping, process: ProcessInfo) -> bool:
    """Confirm a registration belongs to this exact process, not a reused PID.

    ``procStart`` is the kernel start-time tick count from
    ``/proc/<pid>/stat``; when both sides expose it the comparison is exact.
    Older Claude Code builds only wrote ``startedAt`` (milliseconds), so that
    is accepted as a coarse fallback.  A registration that offers neither
    temporal proof is rejected, keeping PID reuse from resurrecting a row.
    """
    if _optional_int(data.get("pid")) != process.pid:
        return False

    proc_start = _optional_int(data.get("procStart"))
    if proc_start is not None and process.start_ticks is not None:
        return proc_start == process.start_ticks

    started_at = _ms_to_seconds(_optional_int(data.get("startedAt")))
    if started_at is not None and process.started_at is not None:
        return abs(started_at - process.started_at) <= 300.0
    return False


def claude_session_state(
    process: ProcessInfo,
    home: Path | None = None,
) -> ClaudeSessionState | None:
    """Return the displayable state for one live Claude Code process.

    Returns ``None`` when the process has no matching registration, when the
    registration is not an interactive session, or when no turn has ever been
    submitted, so a freshly opened Claude Code process stays hidden until it
    actually starts working.
    """
    home = home or default_claude_home()
    data = read_session_registration(process.pid, home)
    if data is None or not registration_matches_process(data, process):
        return None

    session_id = _optional_str(data.get("sessionId"))
    if not session_id:
        return None

    kind = _optional_str(data.get("kind"))
    if kind is not None and kind not in DISPLAY_KINDS:
        return None

    cwd = _optional_str(data.get("cwd")) or process.cwd
    registered_status = _optional_str(data.get("status"))
    started_at = _ms_to_seconds(_optional_int(data.get("startedAt")))
    updated_at = _ms_to_seconds(_optional_int(data.get("updatedAt")))
    status_updated_at = _ms_to_seconds(_optional_int(data.get("statusUpdatedAt")))

    transcript = resolve_transcript_path(session_id, cwd, home)

    if registered_status in WORKING_STATUSES:
        return ClaudeSessionState(
            pid=process.pid,
            session_id=session_id,
            cwd=cwd,
            kind=kind,
            entrypoint=_optional_str(data.get("entrypoint")),
            version=_optional_str(data.get("version")),
            registered_status=registered_status,
            started_at=started_at,
            updated_at=updated_at,
            status_updated_at=status_updated_at,
            last_activity_at=status_updated_at or updated_at,
            turn_active=True,
            terminal_event=False,
            failed_event=False,
            status=STATUS_RUNNING,
            transcript_path=str(transcript) if transcript is not None else None,
        )

    outcome = read_transcript_outcome(transcript)
    if not outcome.assistant_seen:
        # No turn has produced a structured assistant record yet, so this
        # session is monitored for liveness but not displayed.
        return None

    return ClaudeSessionState(
        pid=process.pid,
        session_id=session_id,
        cwd=cwd,
        kind=kind,
        entrypoint=_optional_str(data.get("entrypoint")),
        version=_optional_str(data.get("version")),
        registered_status=registered_status,
        started_at=started_at,
        updated_at=updated_at,
        status_updated_at=status_updated_at,
        last_activity_at=outcome.last_activity_at or status_updated_at or updated_at,
        turn_active=False,
        terminal_event=True,
        failed_event=outcome.failed_event,
        status=STATUS_FAILURE if outcome.failed_event else STATUS_SUCCESS,
        transcript_path=str(transcript) if transcript is not None else None,
    )


def resolve_transcript_path(
    session_id: str,
    cwd: str | None,
    home: Path | None = None,
) -> Path | None:
    """Locate the transcript for a session id without scanning unbounded trees.

    The encoded working directory is tried first because it is an exact
    O(1) lookup.  Claude Code shortens very long encoded names, so a bounded
    scan of the project folders is used as a fallback and the result is cached
    per session id.
    """
    home = home or default_claude_home()
    projects = claude_projects_dir(home)
    cache_key = (str(projects), session_id)

    with _CACHE_LOCK:
        cached = _TRANSCRIPT_PATHS.get(cache_key)
    if cached is not None:
        try:
            if cached.is_file():
                return cached
        except OSError:
            pass
        with _CACHE_LOCK:
            _TRANSCRIPT_PATHS.pop(cache_key, None)

    name = f"{session_id}.jsonl"
    candidates = []
    if cwd:
        candidates.append(projects / encode_project_dir(cwd) / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                _remember_transcript(cache_key, candidate)
                return candidate
        except OSError:
            continue

    try:
        entries = sorted(projects.iterdir())[:MAX_PROJECT_DIRS]
    except OSError:
        return None
    for entry in entries:
        candidate = entry / name
        try:
            if candidate.is_file():
                _remember_transcript(cache_key, candidate)
                return candidate
        except OSError:
            continue
    return None


def read_transcript_outcome(path: Path | None) -> TranscriptOutcome:
    """Read structural turn outcome facts from a bounded transcript tail."""
    if path is None:
        return TranscriptOutcome()
    try:
        info = path.stat()
    except OSError:
        return TranscriptOutcome()

    signature = (info.st_size, info.st_mtime_ns, info.st_ino)
    with _CACHE_LOCK:
        cached = _TRANSCRIPT_CACHE.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]

    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            offset = max(0, size - TRANSCRIPT_TAIL_BYTES)
            handle.seek(offset)
            raw = handle.read(TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return TranscriptOutcome()

    lines = raw.split(b"\n")
    if offset > 0 and lines:
        # Drop the partial record the tail window sliced in half.
        lines = lines[1:]

    outcome = _outcome_from_lines(lines)
    with _CACHE_LOCK:
        if len(_TRANSCRIPT_CACHE) >= MAX_TRANSCRIPT_CACHE_ENTRIES:
            _TRANSCRIPT_CACHE.clear()
        _TRANSCRIPT_CACHE[path] = (signature, outcome)
    return outcome


def _outcome_from_lines(lines: list[bytes]) -> TranscriptOutcome:
    """Derive the last turn's outcome by walking the tail backwards.

    The first main-conversation record encountered decides the turn:

    * an ``assistant`` record ends a turn, and its ``isApiErrorMessage`` /
      ``isAbortedMidStream`` flags mark an API error or a mid-stream abort;
    * a submitted human prompt found *after* the newest assistant record means
      the turn was opened and then interrupted before the model produced any
      record at all, which is the Ctrl+C / Escape path.

    Only record types, structural flags, and the ``origin.kind`` marker are
    inspected; no prompt, assistant, or tool text is read.
    """
    assistant_seen = False
    failed_event = False
    unanswered_prompt = False
    last_activity_at: float | None = None

    for raw in reversed(lines):
        if not raw or b"\x00" in raw:
            continue
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if last_activity_at is None:
            last_activity_at = _iso_to_seconds(record.get("timestamp"))
        # A sidechain record belongs to a subagent, not the main turn.
        if record.get("isSidechain") is True:
            continue
        record_type = record.get("type")
        if record_type == "assistant":
            assistant_seen = True
            failed_event = (
                unanswered_prompt
                or bool(record.get("isApiErrorMessage"))
                or bool(record.get("isAbortedMidStream"))
            )
            break
        if record_type == "user" and _is_submitted_prompt(record):
            unanswered_prompt = True

    if not assistant_seen and unanswered_prompt:
        # The very first turn of this session was interrupted before the model
        # produced any record; the session is still displayable.
        return TranscriptOutcome(
            assistant_seen=True,
            terminal_event=True,
            failed_event=True,
            last_activity_at=last_activity_at,
        )

    return TranscriptOutcome(
        assistant_seen=assistant_seen,
        terminal_event=assistant_seen,
        failed_event=failed_event,
        last_activity_at=last_activity_at,
    )


def _is_submitted_prompt(record: Mapping) -> bool:
    """True for a record Claude Code marks as a prompt submitted by the user.

    Tool results and injected system notices are also stored as ``user``
    records, so the explicit ``origin.kind`` marker is required.  Builds that
    do not write ``origin`` simply never match, which keeps the interrupt
    detection fail-conservative instead of inventing a ``失败``.
    """
    if record.get("isMeta") is True:
        return False
    origin = record.get("origin")
    return isinstance(origin, Mapping) and origin.get("kind") == "human"


def claude_state_health(home: Path | None = None) -> dict[str, object]:
    """Expose non-secret diagnostics about the Claude Code observation path."""
    home = home or default_claude_home()
    sessions = claude_sessions_dir(home)
    projects = claude_projects_dir(home)
    registrations = 0
    try:
        for index, entry in enumerate(sessions.iterdir()):
            if index >= MAX_REGISTRATION_FILES:
                break
            if entry.name.endswith(".json") and entry.name[:-5].isdecimal():
                registrations += 1
    except OSError:
        registrations = 0
    return {
        "home": str(home),
        "home_exists": home.is_dir(),
        "sessions_dir_exists": sessions.is_dir(),
        "projects_dir_exists": projects.is_dir(),
        "registered_sessions": registrations,
    }


def _remember_transcript(cache_key: tuple[str, str], path: Path) -> None:
    with _CACHE_LOCK:
        if len(_TRANSCRIPT_PATHS) >= MAX_TRANSCRIPT_CACHE_ENTRIES:
            _TRANSCRIPT_PATHS.clear()
        _TRANSCRIPT_PATHS[cache_key] = path


def _iso_to_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _ms_to_seconds(value: int | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def reset_caches() -> None:
    """Clear cached transcript lookups (used by tests)."""
    with _CACHE_LOCK:
        _TRANSCRIPT_CACHE.clear()
        _TRANSCRIPT_PATHS.clear()
