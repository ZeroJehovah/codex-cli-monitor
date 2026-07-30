from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - reserved for a future native Windows collector
    fcntl = None  # type: ignore[assignment]


HOOK_LOG_ENV = "CODEX_MONITOR_HOOK_LOG"
HOOK_LOG_MAX_BYTES_ENV = "CODEX_MONITOR_HOOK_LOG_MAX_BYTES"
DEFAULT_MAX_EVENTS = 2000
DEFAULT_LOG_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_LOG_GENERATIONS = 2
MAX_TAIL_BYTES_PER_FILE = 4 * 1024 * 1024
MAX_HOOK_PAYLOAD_BYTES = 256 * 1024
HOOK_LOCK_TIMEOUT_SECONDS = 0.015
HOOK_EVENT_CACHE_MAX_ENTRIES = 32


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
    turn_id: str | None = None
    tool_use_id: str | None = None
    schema_version: int = 1
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "ppid": self.ppid,
            "cwd": self.cwd,
            "tool_name": self.tool,
            "hook_source": self.hook_source,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "tool_use_id": self.tool_use_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class HookSessionState:
    cwd: str
    updated_at: float
    last_event: str
    in_turn: bool
    has_turn_activity: bool = False
    turn_started_at: float | None = None
    last_stopped_at: float | None = None
    session_started_at: float | None = None
    session_start_source: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    last_stopped_turn_id: str | None = None
    active_tool_count: int = 0
    active_tool_use_ids: tuple[str, ...] = ()
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
            "has_turn_activity": self.has_turn_activity,
            "turn_started_at": self.turn_started_at,
            "last_stopped_at": self.last_stopped_at,
            "session_started_at": self.session_started_at,
            "session_start_source": self.session_start_source,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "last_stopped_turn_id": self.last_stopped_turn_id,
            "active_tool_count": self.active_tool_count,
            "active_tool_use_ids": list(self.active_tool_use_ids),
            "last_tool": self.last_tool,
            "codex_pid": self.codex_pid,
            "source": self.source,
        }


@dataclass(frozen=True)
class _HookEventCacheEntry:
    signature: tuple[tuple[int, int, int, int], ...]
    events: tuple[HookEvent, ...]
    diagnostics: dict[str, object]


_HOOK_EVENT_CACHE: dict[str, _HookEventCacheEntry] = {}
_HOOK_EVENT_CACHE_LOCK = threading.Lock()


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
) -> bool:
    log_path = path or default_hook_log_path()
    payload = {
        "schema_version": 2,
        "event": event,
        "timestamp": time.time() if timestamp is None else timestamp,
        "pid": os.getpid(),
        "ppid": os.getppid() if ppid is None else ppid,
        "cwd": cwd or _hook_payload_string(hook_payload, "cwd") or os.getcwd(),
        "session_id": _hook_payload_session_id(hook_payload),
        "turn_id": _hook_payload_string(hook_payload, "turn_id"),
        "tool_name": _hook_payload_string(hook_payload, "tool_name") or tool,
        "tool_use_id": _hook_payload_string(hook_payload, "tool_use_id"),
        "hook_source": _hook_payload_source(hook_payload),
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        _append_encoded_event(log_path, encoded)
    except BaseException as error:
        _record_hook_diagnostic(log_path, "write_error", type(error).__name__)
        return False
    return True


def read_hook_payload_stdin(
    max_bytes: int = MAX_HOOK_PAYLOAD_BYTES,
    path: Path | None = None,
) -> dict | None:
    if sys.stdin.isatty():
        return None
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        raw = stream.read(max_bytes + 1)
    except (OSError, UnicodeError):
        _record_hook_diagnostic(path or default_hook_log_path(), "stdin_read_error")
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        _record_hook_diagnostic(path or default_hook_log_path(), "stdin_oversized")
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _record_hook_diagnostic(path or default_hook_log_path(), "stdin_invalid")
        return None
    if not isinstance(payload, dict):
        _record_hook_diagnostic(path or default_hook_log_path(), "stdin_invalid")
        return None
    return payload


def discard_hook_payload_stdin() -> None:
    read_hook_payload_stdin()


def load_hook_events(
    path: Path | None = None,
    max_age_seconds: float = 24 * 3600,
) -> tuple[HookEvent, ...]:
    log_path = path or default_hook_log_path()
    files = _hook_log_files(log_path)
    signature = _file_signature(files)
    if not signature:
        return ()
    cache_key = str(log_path)
    with _HOOK_EVENT_CACHE_LOCK:
        cached = _HOOK_EVENT_CACHE.get(cache_key)
        if cached is not None and cached.signature == signature:
            return _recent_events(cached.events, max_age_seconds)

    events: list[HookEvent] = []
    valid_lines = 0
    corrupt_lines = 0
    bytes_read = 0
    schema_versions: set[int] = set()
    for file_path in reversed(files):
        lines, read_count = _read_tail_lines(file_path, DEFAULT_MAX_EVENTS)
        bytes_read += read_count
        for line in lines:
            if not line or b"\x00" in line:
                corrupt_lines += 1
                continue
            try:
                payload = json.loads(line)
                timestamp = float(payload["timestamp"])
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                corrupt_lines += 1
                continue
            event = _optional_str(payload.get("event"))
            cwd = _optional_str(payload.get("cwd"))
            if not event or not cwd:
                corrupt_lines += 1
                continue
            schema_version = _optional_int(payload.get("schema_version")) or 1
            schema_versions.add(schema_version)
            valid_lines += 1
            events.append(
                HookEvent(
                    event=event,
                    timestamp=timestamp,
                    pid=_optional_int(payload.get("pid")),
                    ppid=_optional_int(payload.get("ppid")),
                    cwd=cwd,
                    tool=_optional_str(payload.get("tool_name") or payload.get("tool")),
                    hook_source=_optional_str(payload.get("hook_source")),
                    session_id=_optional_str(payload.get("session_id")),
                    turn_id=_optional_str(payload.get("turn_id")),
                    tool_use_id=_optional_str(payload.get("tool_use_id")),
                    schema_version=schema_version,
                    source=str(file_path),
                )
            )
    cached_events = tuple(sorted(events, key=lambda item: item.timestamp)[-DEFAULT_MAX_EVENTS:])
    diagnostics: dict[str, object] = {
        "valid_lines": valid_lines,
        "corrupt_lines": corrupt_lines,
        "bytes_read": bytes_read,
        "schema_versions": sorted(schema_versions),
    }
    with _HOOK_EVENT_CACHE_LOCK:
        _HOOK_EVENT_CACHE[cache_key] = _HookEventCacheEntry(signature, cached_events, diagnostics)
        overflow = len(_HOOK_EVENT_CACHE) - HOOK_EVENT_CACHE_MAX_ENTRIES
        if overflow > 0:
            for stale_key in tuple(_HOOK_EVENT_CACHE)[:overflow]:
                _HOOK_EVENT_CACHE.pop(stale_key, None)
    return _recent_events(cached_events, max_age_seconds)


def hook_log_health(path: Path | None = None) -> dict[str, object]:
    log_path = path or default_hook_log_path()
    events = load_hook_events(log_path)
    files = _hook_log_files(log_path)
    signature = _file_signature(files)
    with _HOOK_EVENT_CACHE_LOCK:
        cached = _HOOK_EVENT_CACHE.get(str(log_path))
        diagnostics = dict(cached.diagnostics) if cached and cached.signature == signature else {}
    diagnostic_summary = _read_hook_diagnostics(log_path)
    latest = max((event.timestamp for event in events), default=None)
    mode = "tool_diagnostics" if any(
        event.event in {"pre_tool_use", "post_tool_use"} for event in events
    ) else "default"
    return {
        "path": str(log_path),
        "exists": bool(files),
        "size_bytes": sum(item[2] for item in signature),
        "rotation_generations": max(0, len(files) - 1),
        "latest_event_at": latest,
        "schema_versions": diagnostics.get("schema_versions", []),
        "valid_lines": diagnostics.get("valid_lines", 0),
        "corrupt_lines": diagnostics.get("corrupt_lines", 0),
        "tail_bytes_read": diagnostics.get("bytes_read", 0),
        "write_error_count": diagnostic_summary.get("write_error", 0),
        "stdin_invalid_count": diagnostic_summary.get("stdin_invalid", 0),
        "stdin_oversized_count": diagnostic_summary.get("stdin_oversized", 0),
        "last_diagnostic_at": diagnostic_summary.get("last_diagnostic_at"),
        "last_diagnostic_kind": diagnostic_summary.get("last_diagnostic_kind"),
        "last_error": diagnostic_summary.get("last_error"),
        "event_mode": mode,
    }


def summarize_hook_events(
    events: Iterable[HookEvent],
) -> dict[str, tuple[HookSessionState, ...]]:
    states: dict[tuple[str, int | None, str | None], HookSessionState] = {}
    active_tool_ids: dict[tuple[str, int | None, str | None], set[str]] = {}
    anonymous_tools: dict[tuple[str, int | None, str | None], int] = {}
    normalized_cwds: dict[str, str | None] = {}

    for event in sorted(events, key=lambda item: item.timestamp):
        if event.cwd not in normalized_cwds:
            normalized_cwds[event.cwd] = _normalize_path(event.cwd)
        cwd = normalized_cwds[event.cwd]
        if cwd is None:
            continue
        key = (cwd, event.ppid, event.session_id)
        previous = states.get(key)
        if previous is None:
            previous = HookSessionState(cwd, event.timestamp, event.event, False, codex_pid=event.ppid)
        session_id = event.session_id or previous.session_id
        turn_id = previous.turn_id
        in_turn = previous.in_turn
        has_turn_activity = previous.has_turn_activity
        turn_started_at = previous.turn_started_at
        last_stopped_at = previous.last_stopped_at
        last_stopped_turn_id = previous.last_stopped_turn_id
        session_started_at = previous.session_started_at
        session_start_source = previous.session_start_source
        tools = active_tool_ids.setdefault(key, set())
        anonymous = anonymous_tools.get(key, 0)

        if event.event == "session_start":
            session_started_at = event.timestamp
            session_start_source = event.hook_source
        elif event.event == "user_prompt_submit":
            in_turn = True
            has_turn_activity = True
            turn_id = event.turn_id or turn_id
            turn_started_at = event.timestamp
            last_stopped_at = None
            tools.clear()
            anonymous = 0
        elif event.event == "pre_tool_use":
            in_turn = True
            turn_id = event.turn_id or turn_id
            if event.tool_use_id:
                tools.add(event.tool_use_id)
            else:
                anonymous += 1
            if turn_started_at is None:
                turn_started_at = event.timestamp
        elif event.event == "post_tool_use":
            in_turn = True
            turn_id = event.turn_id or turn_id
            if event.tool_use_id:
                tools.discard(event.tool_use_id)
            else:
                anonymous = max(0, anonymous - 1)
            if turn_started_at is None:
                turn_started_at = event.timestamp
        elif event.event == "stop":
            has_turn_activity = True
            last_stopped_at = event.timestamp
            last_stopped_turn_id = event.turn_id or turn_id
            if not (event.turn_id and turn_id and event.turn_id != turn_id):
                in_turn = False
                turn_id = event.turn_id or turn_id
                tools.clear()
                anonymous = 0

        anonymous_tools[key] = anonymous
        states[key] = HookSessionState(
            cwd=cwd,
            updated_at=event.timestamp,
            last_event=event.event,
            in_turn=in_turn,
            has_turn_activity=has_turn_activity,
            turn_started_at=turn_started_at,
            last_stopped_at=last_stopped_at,
            session_started_at=session_started_at,
            session_start_source=session_start_source,
            session_id=session_id,
            turn_id=turn_id,
            last_stopped_turn_id=last_stopped_turn_id,
            active_tool_count=len(tools) + anonymous,
            active_tool_use_ids=tuple(sorted(tools)),
            last_tool=event.tool or previous.last_tool,
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


def _append_encoded_event(log_path: Path, encoded: bytes) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = log_path.with_name(log_path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        _acquire_lock(lock_fd)
        max_bytes = _configured_log_max_bytes()
        try:
            size = log_path.stat().st_size
        except OSError:
            size = 0
        if size and size + len(encoded) > max_bytes:
            _rotate_hook_log(log_path)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(log_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError("short hook log write")
        finally:
            os.close(descriptor)
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(lock_fd)


def _acquire_lock(descriptor: int) -> None:
    if fcntl is None:
        return
    deadline = time.monotonic() + HOOK_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("hook log lock timeout")
            time.sleep(0.001)


def _rotate_hook_log(log_path: Path) -> None:
    oldest = _generation_path(log_path, DEFAULT_LOG_GENERATIONS)
    oldest.unlink(missing_ok=True)
    for generation in range(DEFAULT_LOG_GENERATIONS - 1, 0, -1):
        source = _generation_path(log_path, generation)
        if source.exists():
            os.replace(source, _generation_path(log_path, generation + 1))
    if log_path.exists():
        os.replace(log_path, _generation_path(log_path, 1))


def _configured_log_max_bytes() -> int:
    try:
        return max(64 * 1024, int(os.environ.get(HOOK_LOG_MAX_BYTES_ENV, DEFAULT_LOG_MAX_BYTES)))
    except ValueError:
        return DEFAULT_LOG_MAX_BYTES


def _generation_path(path: Path, generation: int) -> Path:
    return path.with_name(f"{path.name}.{generation}")


def _hook_log_files(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (
            path,
            *(_generation_path(path, number) for number in range(1, DEFAULT_LOG_GENERATIONS + 1)),
        )
        if candidate.is_file()
    )


def _file_signature(paths: tuple[Path, ...]) -> tuple[tuple[int, int, int, int], ...]:
    result = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(result)


def _read_tail_lines(path: Path, max_lines: int) -> tuple[list[bytes], int]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            remaining = min(size, MAX_TAIL_BYTES_PER_FILE)
            chunks: list[bytes] = []
            line_count = 0
            while remaining > 0 and line_count <= max_lines:
                chunk_size = min(64 * 1024, remaining)
                handle.seek(-chunk_size, os.SEEK_CUR)
                chunk = handle.read(chunk_size)
                handle.seek(-chunk_size, os.SEEK_CUR)
                chunks.append(chunk)
                line_count += chunk.count(b"\n")
                remaining -= chunk_size
    except OSError:
        return [], 0
    data = b"".join(reversed(chunks))
    if size > len(data):
        first_newline = data.find(b"\n")
        data = data[first_newline + 1 :] if first_newline >= 0 else b""
    return data.splitlines()[-max_lines:], len(b"".join(chunks))


def _recent_events(events: tuple[HookEvent, ...], max_age_seconds: float) -> tuple[HookEvent, ...]:
    minimum = time.time() - max_age_seconds
    return tuple(event for event in events if event.timestamp >= minimum)


def _record_hook_diagnostic(path: Path, kind: str, detail: str | None = None) -> None:
    diagnostic_path = path.with_name(path.name + ".diagnostics.jsonl")
    payload = {"timestamp": time.time(), "kind": kind}
    if detail:
        payload["detail"] = detail[:80]
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    try:
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(diagnostic_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.fstat(descriptor).st_size < 256 * 1024:
                os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
    except BaseException:
        return


def _read_hook_diagnostics(path: Path) -> dict[str, object]:
    diagnostic_path = path.with_name(path.name + ".diagnostics.jsonl")
    lines, _ = _read_tail_lines(diagnostic_path, 2000)
    summary: dict[str, object] = {}
    for line in lines:
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if isinstance(kind, str):
            summary[kind] = int(summary.get(kind, 0)) + 1
            timestamp = payload.get("timestamp")
            if isinstance(timestamp, (int, float)):
                summary["last_diagnostic_at"] = float(timestamp)
            summary["last_diagnostic_kind"] = kind
            detail = payload.get("detail")
            if kind == "write_error" and isinstance(detail, str):
                summary["last_error"] = detail
    return summary


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int)) else None


def _hook_payload_string(payload: Mapping[str, object] | None, key: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(key)
    return str(value) if isinstance(value, (str, int)) else None


def _hook_payload_source(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    for key in ("source", "session_start_source", "start_source", "trigger"):
        value = _hook_payload_string(payload, key)
        if value is not None:
            return value
    return None


def _hook_payload_session_id(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    for key in ("session_id", "thread_id", "conversation_id"):
        value = _hook_payload_string(payload, key)
        if value is not None:
            return value
    return None


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
