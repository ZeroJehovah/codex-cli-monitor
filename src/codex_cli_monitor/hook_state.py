from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


HOOK_LOG_ENV = "CODEX_MONITOR_HOOK_LOG"
DEFAULT_MAX_EVENTS = 2000


@dataclass(frozen=True)
class HookEvent:
    event: str
    timestamp: float
    pid: int | None
    ppid: int | None
    cwd: str | None
    tool: str | None = None
    hook_source: str | None = None
    session_id: str | None = None
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "ppid": self.ppid,
            "cwd": self.cwd,
            "tool": self.tool,
            "hook_source": self.hook_source,
            "session_id": self.session_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class HookSessionState:
    cwd: str
    updated_at: float
    last_event: str
    in_turn: bool
    turn_started_at: float | None = None
    last_stopped_at: float | None = None
    session_started_at: float | None = None
    session_start_source: str | None = None
    session_id: str | None = None
    active_tool_count: int = 0
    last_tool: str | None = None
    codex_pid: int | None = None
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            "cwd": self.cwd,
            "updated_at": self.updated_at,
            "age_seconds": max(0.0, time.time() - self.updated_at),
            "last_event": self.last_event,
            "in_turn": self.in_turn,
            "turn_started_at": self.turn_started_at,
            "last_stopped_at": self.last_stopped_at,
            "session_started_at": self.session_started_at,
            "session_start_source": self.session_start_source,
            "session_id": self.session_id,
            "active_tool_count": self.active_tool_count,
            "last_tool": self.last_tool,
            "codex_pid": self.codex_pid,
            "source": self.source,
        }


def default_hook_log_path(env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    if env.get(HOOK_LOG_ENV):
        return Path(env[HOOK_LOG_ENV]).expanduser()
    if env.get("XDG_STATE_HOME"):
        state_home = Path(env["XDG_STATE_HOME"]).expanduser()
    else:
        state_home = Path.home() / ".local" / "state"
    return state_home / "codex-cli-monitor" / "hooks.jsonl"


def append_hook_event(
    event: str,
    tool: str | None = None,
    cwd: str | None = None,
    ppid: int | None = None,
    timestamp: float | None = None,
    path: Path | None = None,
    hook_payload: Mapping[str, object] | None = None,
) -> None:
    log_path = path or default_hook_log_path()
    hook_source = _hook_payload_source(hook_payload)
    session_id = _hook_payload_session_id(hook_payload)
    payload = {
        "schema_version": 1,
        "event": event,
        "timestamp": time.time() if timestamp is None else timestamp,
        "pid": os.getpid(),
        "ppid": os.getppid() if ppid is None else ppid,
        "cwd": cwd or os.getcwd(),
        "tool": tool,
        "hook_source": hook_source,
        "session_id": session_id,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def read_hook_payload_stdin() -> dict | None:
    if sys.stdin.isatty():
        return None
    try:
        text = sys.stdin.read()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def load_hook_events(
    path: Path | None = None,
    max_age_seconds: float = 24 * 3600,
) -> tuple[HookEvent, ...]:
    log_path = path or default_hook_log_path()
    if not log_path.exists():
        return ()
    min_timestamp = time.time() - max_age_seconds
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ()

    events = []
    for line in lines[-DEFAULT_MAX_EVENTS:]:
        try:
            payload = json.loads(line)
            timestamp = float(payload["timestamp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp < min_timestamp:
            continue
        event = _optional_str(payload.get("event"))
        cwd = _optional_str(payload.get("cwd"))
        if not event or not cwd:
            continue
        events.append(
            HookEvent(
                event=event,
                timestamp=timestamp,
                pid=_optional_int(payload.get("pid")),
                ppid=_optional_int(payload.get("ppid")),
                cwd=cwd,
                tool=_optional_str(payload.get("tool")),
                hook_source=_optional_str(payload.get("hook_source")),
                session_id=_optional_str(payload.get("session_id")),
                source=str(log_path),
            )
        )
    return tuple(events)


def summarize_hook_events(
    events: Iterable[HookEvent],
) -> dict[str, tuple[HookSessionState, ...]]:
    states: dict[tuple[str, int | None], HookSessionState] = {}
    active_tools: dict[tuple[str, int | None], int] = {}
    in_turn: dict[tuple[str, int | None], bool] = {}
    turn_started_at: dict[tuple[str, int | None], float | None] = {}
    last_stopped_at: dict[tuple[str, int | None], float | None] = {}
    session_started_at: dict[tuple[str, int | None], float | None] = {}
    session_start_source: dict[tuple[str, int | None], str | None] = {}
    session_id: dict[tuple[str, int | None], str | None] = {}

    for event in sorted(events, key=lambda item: item.timestamp):
        cwd = _normalize_path(event.cwd)
        if cwd is None:
            continue
        key = (cwd, event.ppid)
        if event.event == "session_start":
            session_id[key] = event.session_id
        elif event.session_id is not None:
            session_id[key] = event.session_id
        if event.event in {"session_start", "user_prompt_submit"}:
            in_turn[key] = event.event == "user_prompt_submit"
            active_tools[key] = 0
            if event.event == "session_start":
                turn_started_at[key] = None
                last_stopped_at[key] = None
                session_started_at[key] = event.timestamp
                session_start_source[key] = event.hook_source
            else:
                turn_started_at[key] = event.timestamp
                last_stopped_at[key] = None
        elif event.event == "pre_tool_use":
            was_in_turn = in_turn.get(key, False)
            in_turn[key] = True
            active_tools[key] = active_tools.get(key, 0) + 1
            if not was_in_turn or turn_started_at.get(key) is None:
                turn_started_at[key] = event.timestamp
                last_stopped_at[key] = None
        elif event.event == "post_tool_use":
            active_tools[key] = max(0, active_tools.get(key, 0) - 1)
            in_turn[key] = True
            if turn_started_at.get(key) is None:
                turn_started_at[key] = event.timestamp
                last_stopped_at[key] = None
        elif event.event == "stop":
            in_turn[key] = False
            active_tools[key] = 0
            last_stopped_at[key] = event.timestamp

        states[key] = HookSessionState(
            cwd=cwd,
            updated_at=event.timestamp,
            last_event=event.event,
            in_turn=in_turn.get(key, False),
            turn_started_at=turn_started_at.get(key),
            last_stopped_at=last_stopped_at.get(key),
            session_started_at=session_started_at.get(key),
            session_start_source=session_start_source.get(key),
            session_id=session_id.get(key),
            active_tool_count=active_tools.get(key, 0),
            last_tool=event.tool,
            codex_pid=event.ppid,
            source=event.source,
        )
    grouped: dict[str, list[HookSessionState]] = {}
    for state in states.values():
        grouped.setdefault(state.cwd, []).append(state)
    return {
        cwd: tuple(sorted(items, key=lambda item: item.updated_at, reverse=True))
        for cwd, items in grouped.items()
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _hook_payload_source(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    return _optional_str(
        payload.get("source")
        or payload.get("session_start_source")
        or payload.get("start_source")
        or payload.get("trigger")
    )


def _hook_payload_session_id(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    return _optional_str(
        payload.get("session_id")
        or payload.get("thread_id")
        or payload.get("conversation_id")
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_path(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(Path(value).absolute())
