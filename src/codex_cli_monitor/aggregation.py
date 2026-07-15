from __future__ import annotations

import math
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .models import CodexSession


SNAPSHOT_SCHEMA_VERSION = 1
VALID_DISPLAY_STATUSES = {"运行中", "成功", "失败"}
MAX_REMOTE_SESSIONS = 1024
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SnapshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ServerIdentity:
    server_id: str
    server_name: str
    boot_id: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.server_id,
            "name": self.server_name,
            "boot_id": self.boot_id,
        }


@dataclass(frozen=True)
class RemoteSnapshot:
    identity: ServerIdentity
    observed_at: float
    received_at: float
    sessions: tuple[dict, ...]


def resolve_server_identity(
    server_id: str | None = None,
    server_name: str | None = None,
    proc_root: Path = Path("/proc"),
) -> ServerIdentity:
    hostname = socket.gethostname() or "unknown-host"
    resolved_id = server_id or hostname
    if not SERVER_ID_RE.fullmatch(resolved_id):
        raise ValueError(
            "server id must contain only letters, digits, '.', '_', ':', or '-' "
            "and be at most 128 characters"
        )
    resolved_name = (server_name or hostname).strip()
    if not resolved_name or len(resolved_name) > 128:
        raise ValueError("server name must be between 1 and 128 characters")
    boot_id = _read_optional_text(proc_root / "sys" / "kernel" / "random" / "boot_id")
    return ServerIdentity(resolved_id, resolved_name, boot_id)


def build_collector_snapshot(
    sessions: tuple[CodexSession, ...],
    identity: ServerIdentity,
    observed_at: float | None = None,
) -> dict:
    observed_at = time.time() if observed_at is None else observed_at
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "server": identity.to_dict(),
        "observed_at": observed_at,
        "observed_at_iso": _timestamp_iso(observed_at),
        "session_count": len(sessions),
        "sessions": [_collector_session_payload(session) for session in sessions],
    }


def build_sessions_payload(
    local_sessions: tuple[CodexSession, ...],
    identity: ServerIdentity,
    remote_snapshots: Iterable[RemoteSnapshot] = (),
    observed_at: float | None = None,
) -> dict:
    observed_at = time.time() if observed_at is None else observed_at
    sessions = [
        _local_session_payload(
            session,
            identity,
            observed_at,
        )
        for session in local_sessions
    ]
    servers = [
        {
            **identity.to_dict(),
            "local": True,
            "online": True,
            "observed_at": observed_at,
            "observed_at_iso": _timestamp_iso(observed_at),
            "received_at": observed_at,
            "age_seconds": 0.0,
            "session_count": len(local_sessions),
        }
    ]
    for snapshot in remote_snapshots:
        sessions.extend(snapshot.sessions)
        servers.append(
            {
                **snapshot.identity.to_dict(),
                "local": False,
                "online": True,
                "observed_at": snapshot.observed_at,
                "observed_at_iso": _timestamp_iso(snapshot.observed_at),
                "received_at": snapshot.received_at,
                "age_seconds": max(0.0, observed_at - snapshot.received_at),
                "session_count": len(snapshot.sessions),
            }
        )
    sessions.sort(
        key=lambda item: (
            str(item.get("server_name") or item.get("server_id") or ""),
            _optional_float(item.get("started_at")) or float("inf"),
            _optional_int(item.get("pid")) or 0,
        )
    )
    servers.sort(key=lambda item: (not item["local"], item["name"], item["id"]))
    return {
        "observed_at": observed_at,
        "observed_at_iso": _timestamp_iso(observed_at),
        "server_count": len(servers),
        "servers": servers,
        "session_count": len(sessions),
        "sessions": sessions,
    }


class RemoteSnapshotStore:
    def __init__(self, ttl_seconds: float = 5.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("snapshot TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._snapshots: dict[str, RemoteSnapshot] = {}

    def ingest(
        self,
        payload: Mapping[str, object],
        received_at: float | None = None,
    ) -> RemoteSnapshot:
        snapshot = validate_snapshot(
            payload,
            received_at=time.time() if received_at is None else received_at,
        )
        with self._lock:
            self._snapshots[snapshot.identity.server_id] = snapshot
        return snapshot

    def active(self, now: float | None = None) -> tuple[RemoteSnapshot, ...]:
        now = time.time() if now is None else now
        with self._lock:
            snapshots = tuple(self._snapshots.values())
        return tuple(
            sorted(
                (
                    snapshot
                    for snapshot in snapshots
                    if now - snapshot.received_at <= self.ttl_seconds
                ),
                key=lambda item: (
                    item.identity.server_name,
                    item.identity.server_id,
                ),
            )
        )


def snapshot_server_id(payload: Mapping[str, object]) -> str:
    server = payload.get("server")
    if not isinstance(server, Mapping):
        raise SnapshotValidationError("snapshot server must be an object")
    server_id = server.get("id")
    if not isinstance(server_id, str) or not SERVER_ID_RE.fullmatch(server_id):
        raise SnapshotValidationError("snapshot server id is invalid")
    return server_id


def validate_snapshot(
    payload: Mapping[str, object],
    received_at: float,
) -> RemoteSnapshot:
    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotValidationError("unsupported snapshot schema version")
    server_id = snapshot_server_id(payload)
    server = payload["server"]
    assert isinstance(server, Mapping)
    server_name = server.get("name")
    if (
        not isinstance(server_name, str)
        or not server_name.strip()
        or len(server_name) > 128
    ):
        raise SnapshotValidationError("snapshot server name is invalid")
    boot_id = server.get("boot_id")
    if boot_id is not None and (not isinstance(boot_id, str) or len(boot_id) > 128):
        raise SnapshotValidationError("snapshot boot id is invalid")
    observed_at = _required_float(payload.get("observed_at"), "observed_at")
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list):
        raise SnapshotValidationError("snapshot sessions must be an array")
    if len(raw_sessions) > MAX_REMOTE_SESSIONS:
        raise SnapshotValidationError("snapshot contains too many sessions")
    identity = ServerIdentity(server_id, server_name.strip(), boot_id)
    sessions = tuple(
        _remote_session_payload(item, identity, observed_at)
        for item in raw_sessions
    )
    return RemoteSnapshot(identity, observed_at, received_at, sessions)


def _local_session_payload(
    session: CodexSession,
    identity: ServerIdentity,
    observed_at: float,
) -> dict:
    root = session.root
    result = {
        "server_id": identity.server_id,
        "server_name": identity.server_name,
        "server_boot_id": identity.boot_id,
        "server_observed_at": observed_at,
        "session_key": _session_key(identity, root.pid, root.started_at),
        "pid": root.pid,
        "ppid": root.ppid,
        "status": session.display_status,
        "directory": root.cwd,
        "started_at": root.started_at,
        "started_at_iso": _timestamp_iso(root.started_at),
        "elapsed_seconds": root.elapsed_seconds,
        "tty": root.tty,
        "command": root.command_name,
    }
    result.update(
        {
            "inferred_status": session.inference.to_dict(),
            "state_activity": session.state_activity.to_dict()
            if session.state_activity is not None
            else None,
            "hook_state": session.hook_state.to_dict()
            if session.hook_state is not None
            else None,
        }
    )
    return result


def _collector_session_payload(session: CodexSession) -> dict:
    root = session.root
    return {
        "pid": root.pid,
        "status": session.display_status,
        "directory": root.cwd,
        "started_at": root.started_at,
    }


def _remote_session_payload(
    item: object,
    identity: ServerIdentity,
    observed_at: float,
) -> dict:
    if not isinstance(item, Mapping):
        raise SnapshotValidationError("snapshot session must be an object")
    pid = _required_int(item.get("pid"), "session pid")
    if pid <= 0 or pid > 2_147_483_647:
        raise SnapshotValidationError("session pid must be positive")
    status = item.get("status")
    if status not in VALID_DISPLAY_STATUSES:
        raise SnapshotValidationError("session status is invalid")
    directory = _optional_limited_str(
        item.get("directory"),
        "session directory",
        4096,
    )
    started_at = _optional_float(item.get("started_at"))
    return {
        "server_id": identity.server_id,
        "server_name": identity.server_name,
        "server_boot_id": identity.boot_id,
        "server_observed_at": observed_at,
        "session_key": _session_key(identity, pid, started_at),
        "pid": pid,
        "ppid": _optional_int(item.get("ppid")),
        "status": status,
        "directory": directory,
        "started_at": started_at,
        "started_at_iso": _timestamp_iso(started_at),
        "elapsed_seconds": _optional_float(item.get("elapsed_seconds")),
        "tty": _optional_limited_str(item.get("tty"), "session tty", 256),
        "command": _optional_limited_str(item.get("command"), "session command", 256),
        "inferred_status": None,
        "state_activity": None,
        "hook_state": None,
    }


def _session_key(
    identity: ServerIdentity,
    pid: int,
    started_at: float | None,
) -> str:
    boot_id = identity.boot_id or "unknown-boot"
    start = "unknown-start" if started_at is None else f"{started_at:.6f}"
    return f"{identity.server_id}:{boot_id}:{pid}:{start}"


def _read_optional_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _required_float(value: object, label: str) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        raise SnapshotValidationError(f"{label} must be a number")
    return parsed


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _required_int(value: object, label: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise SnapshotValidationError(f"{label} must be an integer")
    return parsed


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_limited_str(value: object, label: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise SnapshotValidationError(f"{label} is invalid")
    return value


def _timestamp_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        value = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
