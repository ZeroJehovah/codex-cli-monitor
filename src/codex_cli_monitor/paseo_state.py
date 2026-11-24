"""Read active Paseo agent state from its local structured agent records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Mapping

from .classify import is_native_codex_process
from .models import (
    STATUS_RUNNING,
    CodexSession,
    Evidence,
    Inference,
    ProcessInfo,
)

PASEO_CACHE_SECONDS = 2.0
PASEO_MAX_AGENT_DIRECTORIES = 256
PASEO_MAX_ACTIVE_AGENTS = 128
PASEO_MAX_RECORD_BYTES = 64 * 1024
PASEO_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_CACHE_LOCK = threading.Lock()
_cached_sessions: tuple[CodexSession, ...] = ()
_cache_updated_at = 0.0
_cache_identity: tuple = ()


def has_paseo_daemon(processes: Mapping[int, ProcessInfo]) -> bool:
    """Only inspect Paseo's records while its named daemon is running."""
    return any(
        _is_paseo_daemon(process)
        for process in processes.values()
    )


def _is_paseo_daemon(process: ProcessInfo) -> bool:
    return (
        process.comm == "Paseo Daemon"
        and process.state not in {"Z", "X", "x"}
    )


def _has_paseo_parent(process: ProcessInfo, processes: Mapping[int, ProcessInfo]) -> bool:
    parent = processes.get(process.ppid)
    return parent is not None and _is_paseo_daemon(parent)


def paseo_sessions(
    processes: Mapping[int, ProcessInfo],
    proc_root: Path = Path("/proc"),
) -> tuple[CodexSession, ...]:
    """Return a bounded, short-lived cache of active Paseo tasks."""
    global _cached_sessions, _cache_updated_at, _cache_identity
    now = time.monotonic()
    candidates = tuple(
        process for process in processes.values()
        if is_native_codex_process(process)
        and "app-server" in process.cmdline
        and process.state not in {"T", "t", "Z", "X", "x"}
        and _has_paseo_parent(process, processes)
    )
    identity = (
        str(proc_root), str(_paseo_home()),
        tuple(sorted((p.pid, p.start_ticks, p.ppid) for p in candidates)),
    )
    with _CACHE_LOCK:
        if identity == _cache_identity and now - _cache_updated_at < PASEO_CACHE_SECONDS:
            return _cached_sessions
        try:
            _cached_sessions = _discover_paseo_sessions(candidates, proc_root)
        except OSError:
            _cached_sessions = ()
        _cache_identity = identity
        _cache_updated_at = time.monotonic()
        return _cached_sessions


def clear_paseo_cache() -> None:
    """Drop Paseo rows immediately when its daemon is no longer running."""
    global _cached_sessions, _cache_updated_at
    with _CACHE_LOCK:
        _cached_sessions = ()
        _cache_updated_at = time.monotonic()


def reset_paseo_cache() -> None:
    """Reset the module cache for tests and service initialization."""
    global _cached_sessions, _cache_updated_at
    with _CACHE_LOCK:
        _cached_sessions = ()
        _cache_updated_at = 0.0


def _discover_paseo_sessions(
    candidates: tuple[ProcessInfo, ...],
    proc_root: Path,
) -> tuple[CodexSession, ...]:
    paseo_home = _paseo_home()
    agents_root = paseo_home / "agents"
    if not agents_root.is_dir():
        return ()

    sessions = []
    seen_agent_ids: set[str] = set()
    for process in sorted(candidates, key=lambda process: process.pid)[:PASEO_MAX_ACTIVE_AGENTS]:
        agent_id = _read_proc_env_value(
            proc_root / str(process.pid) / "environ",
            "PASEO_AGENT_ID",
        )
        agent_cwd = _read_proc_env_value(
            proc_root / str(process.pid) / "environ",
            "PASEO_AGENT_CWD",
        )
        if (
            not agent_id
            or not PASEO_AGENT_ID_RE.fullmatch(agent_id)
            or not agent_cwd
            or not agent_cwd.startswith("/")
        ):
            continue
        if agent_id in seen_agent_ids:
            continue
        seen_agent_ids.add(agent_id)
        record = _read_agent_record(agents_root, agent_id)
        session = _session_from_record(record, process, agent_cwd)
        if session is not None:
            sessions.append(session)
    return tuple(sessions)


def _read_agent_record(root: Path, agent_id: str) -> dict | None:
    try:
        directories = islice(root.iterdir(), PASEO_MAX_AGENT_DIRECTORIES)
    except OSError:
        return None
    for directory in directories:
        path = directory / f"{agent_id}.json"
        try:
            size = path.stat().st_size
            if size <= 0 or size > PASEO_MAX_RECORD_BYTES:
                continue
            with path.open("rb") as stream:
                raw = stream.read(PASEO_MAX_RECORD_BYTES + 1)
            if len(raw) > PASEO_MAX_RECORD_BYTES:
                continue
        except OSError:
            continue
        record = _read_selected_json_fields(
            raw,
            {"id", "cwd", "createdAt", "lastStatus"},
        )
        if record is not None and record.get("id") == agent_id:
            return record
    return None


def _session_from_record(
    item: dict | None,
    process: ProcessInfo,
    agent_cwd: str,
) -> CodexSession | None:
    if item is None or item.get("lastStatus") != "running":
        return None
    agent_id = item.get("id")
    directory = item.get("cwd")
    started_at = _parse_timestamp(item.get("createdAt"))
    if (
        not isinstance(agent_id, str)
        or not agent_id
        or len(agent_id) > 128
        or not isinstance(directory, str)
        or len(directory) > 4096
        or started_at is None
        or os.path.normpath(directory) != os.path.normpath(agent_cwd)
    ):
        return None

    status = STATUS_RUNNING
    waiting_reason = None
    source_status = "running_paseo"

    inference = Inference(
        status=source_status,
        confidence=1.0,
        evidence=(
            Evidence(
                "paseo_agent_status",
                "The exact Codex app-server PID is linked to a Paseo task whose structured record is running.",
            ),
        ),
        limitations=(),
    )
    root = replace(
        process,
        cwd=os.path.normpath(directory),
    )
    identity = f"{process.pid}:{process.start_ticks}:{agent_id}"
    instance_id = "paseo-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return CodexSession(
        root=root,
        descendants=(),
        connections=(),
        inference=inference,
        display_status=status,
        binding_method="paseo_agent_id",
        binding_confidence=1.0,
        binding_ambiguous=False,
        binding_candidate_count=1,
        binding_evidence=(
            "Paseo task id came from the exact Codex app-server process environment",
            "status and workspace came from Paseo's local structured agent record",
            "task identity hashes the engine process identity and Paseo agent id",
        ),
        cli_type="paseo",
        waiting_reason=waiting_reason,
        instance_id=instance_id,
    )


def _paseo_home() -> Path:
    explicit = os.environ.get("PASEO_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".paseo"


def _read_proc_env_value(path: Path, wanted_key: str) -> str | None:
    prefix = wanted_key.encode("ascii") + b"="
    pending = bytearray()
    token_oversized = False
    total = 0

    def accept(token: bytes) -> str | None:
        if token.startswith(prefix):
            try:
                return token[len(prefix):].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return None
        return None

    try:
        with path.open("rb") as stream:
            while total < 1024 * 1024:
                chunk = stream.read(4096)
                if not chunk:
                    break
                total += len(chunk)
                parts = chunk.split(b"\0")
                first = parts[0]
                if not token_oversized:
                    if len(pending) + len(first) <= 16384:
                        pending.extend(first)
                    else:
                        token_oversized = True
                if len(parts) > 1:
                    if not token_oversized:
                        value = accept(bytes(pending))
                        if value is not None:
                            return value
                    pending.clear()
                    token_oversized = False
                    for part in parts[1:-1]:
                        value = accept(part)
                        if value is not None:
                            return value
                    if len(parts[-1]) <= 16384:
                        pending.extend(parts[-1])
                    else:
                        token_oversized = True
            if not token_oversized and pending.startswith(prefix):
                return pending[len(prefix):].decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _read_selected_json_fields(raw: bytes, wanted: set[str]) -> dict | None:
    """Decode selected values while skipping all other field bytes untouched."""
    position = _skip_json_whitespace(raw, 0)
    if position >= len(raw) or raw[position] != ord("{"):
        return None
    position += 1
    selected = {}
    while position < len(raw):
        position = _skip_json_whitespace(raw, position)
        if position >= len(raw):
            return None
        if raw[position] == ord("}"):
            return selected
        if raw[position] != ord('"'):
            return None
        end = _skip_json_string(raw, position)
        if end < 0:
            return None
        try:
            key = json.loads(raw[position:end])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(key, str):
            return None
        position = _skip_json_whitespace(raw, end)
        if position >= len(raw) or raw[position] != ord(":"):
            return None
        position = _skip_json_whitespace(raw, position + 1)
        value_start = position
        value_end = _skip_json_value(raw, position)
        if value_end < 0:
            return None
        if key in wanted:
            if key in selected or raw[value_start] != ord('"') or value_end - value_start > 8192:
                return None
            try:
                value = json.loads(raw[value_start:value_end])
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            selected[key] = value
        position = _skip_json_whitespace(raw, value_end)
        if position < len(raw) and raw[position] == ord(","):
            position += 1
            continue
        if position < len(raw) and raw[position] == ord("}"):
            return selected
        return None
    return None


def _skip_json_whitespace(source: bytes, position: int) -> int:
    while position < len(source) and source[position] in b" \t\r\n":
        position += 1
    return position


def _skip_json_value(source: bytes, position: int) -> int:
    position = _skip_json_whitespace(source, position)
    if position >= len(source):
        return -1
    if source[position] == ord('"'):
        return _skip_json_string(source, position)
    if source[position] not in (ord("["), ord("{")):
        end = position
        while end < len(source) and source[end] not in b",}] \t\r\n":
            end += 1
        return end if end > position else -1

    closers = [ord("]") if source[position] == ord("[") else ord("}")]
    position += 1
    while position < len(source):
        character = source[position]
        if character == ord('"'):
            position = _skip_json_string(source, position)
            if position < 0:
                return -1
            continue
        if character == ord("["):
            closers.append(ord("]"))
        elif character == ord("{"):
            closers.append(ord("}"))
        elif character in (ord("]"), ord("}")):
            if not closers or character != closers[-1]:
                return -1
            closers.pop()
            if not closers:
                return position + 1
        position += 1
    return -1


def _skip_json_string(source: bytes, position: int) -> int:
    position += 1
    while position < len(source):
        character = source[position]
        if character == ord("\\"):
            position += 2
            continue
        if character == ord('"'):
            return position + 1
        position += 1
    return -1


def _parse_timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if not math.isfinite(timestamp) or timestamp <= 0:
            return None
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
