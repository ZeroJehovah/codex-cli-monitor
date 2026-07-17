from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hook_state import HookSessionState


@dataclass(frozen=True)
class Evidence:
    signal: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"signal": self.signal, "detail": self.detail}


@dataclass(frozen=True)
class NetworkConnection:
    protocol: str
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int
    state: str
    inode: str

    def is_established_remote(self) -> bool:
        if self.state != "ESTABLISHED":
            return False
        if self.remote_address in {"0.0.0.0", "::", "::1", "127.0.0.1"}:
            return False
        if self.remote_address.startswith("127."):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "local_address": self.local_address,
            "local_port": self.local_port,
            "remote_address": self.remote_address,
            "remote_port": self.remote_port,
            "state": self.state,
            "inode": self.inode,
        }


@dataclass(frozen=True)
class StateFile:
    relative_path: str
    size_bytes: int
    modified_at: float
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class CodexStateSummary:
    codex_home: str
    newest_files: tuple[StateFile, ...]
    scan_errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "codex_home": self.codex_home,
            "newest_files": [state_file.to_dict() for state_file in self.newest_files],
            "scan_errors": list(self.scan_errors),
        }


@dataclass(frozen=True)
class SessionActivity:
    relative_path: str
    session_id: str | None
    turn_id: str | None
    cwd: str | None
    size_bytes: int
    modified_at: float
    observed_at: float
    session_started_at: float | None = None
    last_record_at: float | None = None
    turn_started_at: float | None = None
    terminal_event_at: float | None = None
    changed_during_sample: bool = False
    last_record_type: str | None = None
    last_payload_type: str | None = None
    last_payload_role: str | None = None
    last_payload_reason: str | None = None
    terminal_event: bool = False
    terminal_agent_message_missing: bool = False
    failed_event: bool = False
    latest_turn_has_user: bool = False
    latest_turn_has_visible_response: bool = False

    @property
    def modified_age_seconds(self) -> float:
        return max(0.0, self.observed_at - self.modified_at)

    def event_label(self) -> str:
        if self.last_payload_type:
            return f"{self.last_record_type}:{self.last_payload_type}"
        return self.last_record_type or "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "cwd": self.cwd,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "modified_age_seconds": self.modified_age_seconds,
            "session_started_at": self.session_started_at,
            "last_record_at": self.last_record_at,
            "turn_started_at": self.turn_started_at,
            "terminal_event_at": self.terminal_event_at,
            "changed_during_sample": self.changed_during_sample,
            "last_record_type": self.last_record_type,
            "last_payload_type": self.last_payload_type,
            "last_payload_role": self.last_payload_role,
            "last_payload_reason": self.last_payload_reason,
            "terminal_event": self.terminal_event,
            "terminal_agent_message_missing": self.terminal_agent_message_missing,
            "failed_event": self.failed_event,
            "latest_turn_has_user": self.latest_turn_has_user,
            "latest_turn_has_visible_response": self.latest_turn_has_visible_response,
        }


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int | None
    comm: str | None
    state: str | None
    cmdline: tuple[str, ...]
    cwd: str | None
    exe: str | None
    tty: str | None
    tty_nr: int | None
    elapsed_seconds: float | None
    cpu_seconds: float | None
    started_at: float | None = None
    process_group_id: int | None = None
    session_id: int | None = None
    foreground_process_group_id: int | None = None
    cpu_delta_seconds: float | None = None
    children: tuple[int, ...] = field(default_factory=tuple)

    @property
    def command_name(self) -> str:
        if self.cmdline:
            return Path(self.cmdline[0]).name
        if self.exe:
            return Path(self.exe).name
        return self.comm or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "comm": self.comm,
            "state": self.state,
            "cmdline": list(self.cmdline),
            "cwd": self.cwd,
            "exe": self.exe,
            "tty": self.tty,
            "tty_nr": self.tty_nr,
            "elapsed_seconds": self.elapsed_seconds,
            "cpu_seconds": self.cpu_seconds,
            "started_at": self.started_at,
            "process_group_id": self.process_group_id,
            "session_id": self.session_id,
            "foreground_process_group_id": self.foreground_process_group_id,
            "cpu_delta_seconds": self.cpu_delta_seconds,
            "children": list(self.children),
        }


@dataclass(frozen=True)
class LaunchRecord:
    pid: int
    ppid: int | None
    cwd: str | None
    argv: tuple[str, ...]
    real_codex: str | None
    timestamp: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "cwd": self.cwd,
            "argv": list(self.argv),
            "real_codex": self.real_codex,
            "timestamp": self.timestamp,
            "source": self.source,
        }


@dataclass(frozen=True)
class Inference:
    status: str
    confidence: float
    evidence: tuple[Evidence, ...]
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class CodexSession:
    root: ProcessInfo
    descendants: tuple[ProcessInfo, ...]
    connections: tuple[NetworkConnection, ...]
    inference: Inference
    state_activity: SessionActivity | None = None
    hook_state: HookSessionState | None = None
    launch_record: LaunchRecord | None = None
    display_status: str = "成功"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.display_status,
            "inferred_status": self.inference.to_dict(),
            "root": self.root.to_dict(),
            "descendants": [process.to_dict() for process in self.descendants],
            "connections": [connection.to_dict() for connection in self.connections],
            "state_activity": self.state_activity.to_dict()
            if self.state_activity is not None
            else None,
            "hook_state": self.hook_state.to_dict()
            if self.hook_state is not None
            else None,
            "launch_record": self.launch_record.to_dict()
            if self.launch_record is not None
            else None,
        }
