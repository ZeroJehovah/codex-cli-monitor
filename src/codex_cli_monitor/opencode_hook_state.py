"""Concurrency-safe, bounded local storage for OpenCode hook events.

The OpenCode lifecycle hook appends small JSONL markers (event, timestamp,
session id, working directory, parent pid) here.  The resident monitor reads
a bounded tail to bind a live OpenCode process to its session and to observe
unambiguous exit edges even before the SQLite database flushes.

Storage rules mirror the Codex hook log:

* bounded (rotating) and concurrency-safe via ``flock``
* tolerant of corrupt/truncated lines
* read incrementally from the tail, never loading unbounded history
* never contains prompt, assistant, tool-input, or tool-output bodies
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - reserved for a future native Windows collector
    fcntl = None  # type: ignore[assignment]


HOOK_LOG_MAX_BYTES_ENV = "OPENCODE_MONITOR_HOOK_LOG_MAX_BYTES"
DEFAULT_LOG_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_LOG_GENERATIONS = 2
MAX_TAIL_BYTES_PER_FILE = 4 * 1024 * 1024
MAX_HOOK_PAYLOAD_BYTES = 256 * 1024
HOOK_LOCK_TIMEOUT_SECONDS = 0.015
HOOK_EVENT_CACHE_MAX_ENTRIES = 32


@dataclass(frozen=True)
class OpencodeHookEvent:
    event: str
    timestamp: float
    pid: int | None
    ppid: int | None
    cwd: str | None
    session_id: str | None
    source: str | None

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "ppid": self.ppid,
            "cwd": self.cwd,
            "session_id": self.session_id,
            "source": self.source,
        }


_CACHE_ENTRIES: dict[str, tuple[tuple[tuple[int, int, int, int], ...], tuple[OpencodeHookEvent, ...]]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _HookLogCacheEntry:
    signature: tuple[tuple[int, int, int, int], ...]
    events: tuple[OpencodeHookEvent, ...]


def default_opencode_hook_log_path(env: Mapping[str, str] | None = None) -> Path:
    from .opencode_state import default_opencode_hook_log_path as _impl

    return _impl(env)


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
        _record_hook_diagnostic(path or default_opencode_hook_log_path(), "stdin_read_error")
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        _record_hook_diagnostic(path or default_opencode_hook_log_path(), "stdin_oversized")
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _record_hook_diagnostic(path or default_opencode_hook_log_path(), "stdin_invalid")
        return None
    if not isinstance(payload, dict):
        _record_hook_diagnostic(path or default_opencode_hook_log_path(), "stdin_invalid")
        return None
    return payload


def append_opencode_hook_event(
    event: str,
    tool: str | None = None,
    cwd: str | None = None,
    ppid: int | None = None,
    timestamp: float | None = None,
    path: Path | None = None,
    hook_payload: Mapping[str, object] | None = None,
) -> bool:
    log_path = path or default_opencode_hook_log_path()
    payload = {
        "schema_version": 2,
        "event": event,
        "timestamp": time.time() if timestamp is None else timestamp,
        "pid": os.getpid(),
        "ppid": os.getppid() if ppid is None else ppid,
        "cwd": cwd or _hook_payload_string(hook_payload, "cwd")
        or _hook_payload_string(hook_payload, "directory")
        or os.getcwd(),
        "session_id": _hook_payload_session_id(hook_payload),
        "session_title": _hook_payload_string(hook_payload, "session_title")
        or _hook_payload_string(hook_payload, "title"),
        "tool_name": _hook_payload_string(hook_payload, "tool") or tool,
        "hook_source": _hook_payload_string(hook_payload, "hook_event_name")
        or _hook_payload_string(hook_payload, "event"),
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        _append_encoded_event(log_path, encoded)
    except BaseException as error:
        _record_hook_diagnostic(log_path, "write_error", type(error).__name__)
        return False
    return True


def load_opencode_hook_events(
    path: Path | None = None,
    max_age_seconds: float = 24 * 3600,
) -> tuple[OpencodeHookEvent, ...]:
    log_path = path or default_opencode_hook_log_path()
    files = _hook_log_files(log_path)
    signature = _file_signature(files)
    if not signature:
        return ()
    cache_key = str(log_path)
    with _CACHE_LOCK:
        cached = _CACHE_ENTRIES.get(cache_key)
        if cached is not None and cached[0] == signature:
            return _gather_events(cached[1], max_age_seconds)
    events: list[OpencodeHookEvent] = []
    for file_path in reversed(files):
        lines, _ = _read_tail_lines(file_path, 800)
        for line in lines:
            if not line or b"\x00" in line:
                continue
            try:
                payload = json.loads(line)
                timestamp = float(payload["timestamp"])
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            event = payload.get("event")
            cwd = payload.get("cwd")
            if not isinstance(event, str) or not event or not isinstance(cwd, str) or not cwd:
                continue
            events.append(
                OpencodeHookEvent(
                    event=event,
                    timestamp=timestamp,
                    pid=_optional_int(payload.get("pid")),
                    ppid=_optional_int(payload.get("ppid")),
                    cwd=cwd,
                    session_id=_optional_str(payload.get("session_id")),
                    source=str(file_path),
                )
            )
    parsed = tuple(sorted(events, key=lambda item: item.timestamp)[-2000:])
    with _CACHE_LOCK:
        _CACHE_ENTRIES[cache_key] = (signature, parsed)
        overflow = len(_CACHE_ENTRIES) - HOOK_EVENT_CACHE_MAX_ENTRIES
        if overflow > 0:
            for stale_key in tuple(_CACHE_ENTRIES)[:overflow]:
                _CACHE_ENTRIES.pop(stale_key, None)
    return _gather_events(parsed, max_age_seconds)


def _gather_events(
    events: tuple[OpencodeHookEvent, ...],
    max_age_seconds: float,
) -> tuple[OpencodeHookEvent, ...]:
    minimum = time.time() - max_age_seconds
    return tuple(event for event in events if event.timestamp >= minimum)


def opencode_hook_log_health(path: Path | None = None) -> dict[str, object]:
    log_path = path or default_opencode_hook_log_path()
    events = load_opencode_hook_events(log_path)
    files = _hook_log_files(log_path)
    latest = max((event.timestamp for event in events), default=None)
    return {
        "path": str(log_path),
        "exists": bool(files),
        "size_bytes": sum(item[2] for item in _file_signature(files)),
        "rotation_generations": max(0, len(files) - 1),
        "latest_event_at": latest,
        "event_count": len(events),
    }


def _append_encoded_event(log_path: Path, encoded: bytes) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = log_path.with_name(log_path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
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


def _configured_log_max_bytes() -> int:
    try:
        return max(
            64 * 1024,
            int(os.environ.get(HOOK_LOG_MAX_BYTES_ENV, DEFAULT_LOG_MAX_BYTES)),
        )
    except ValueError:
        return DEFAULT_LOG_MAX_BYTES


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


def _hook_log_files(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (
            path,
            *(_generation_path(path, number) for number in range(1, DEFAULT_LOG_GENERATIONS + 1)),
        )
        if candidate.is_file()
    )


def _generation_path(path: Path, generation: int) -> Path:
    return path.with_name(f"{path.name}.{generation}")


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


def _rotate_hook_log(log_path: Path) -> None:
    oldest = _generation_path(log_path, DEFAULT_LOG_GENERATIONS)
    oldest.unlink(missing_ok=True)
    for generation in range(DEFAULT_LOG_GENERATIONS - 1, 0, -1):
        source = _generation_path(log_path, generation)
        if source.exists():
            os.replace(source, _generation_path(log_path, generation + 1))
    if log_path.exists():
        os.replace(log_path, _generation_path(log_path, 1))


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int)) else None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hook_payload_string(payload: Mapping[str, object] | None, key: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(key)
    return str(value) if isinstance(value, (str, int)) else None


def _hook_payload_session_id(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    for key in ("session_id", "sessionID", "id", "session"):
        value = _hook_payload_string(payload, key)
        if value is not None:
            return value
    return None