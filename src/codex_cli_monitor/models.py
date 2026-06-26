from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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

    def is_remote_tls_like(self) -> bool:
        if self.state != "ESTABLISHED":
            return False
        if self.remote_port not in {443, 8443}:
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
    launch_record: LaunchRecord | None = None
    confirmed_status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed_status": self.confirmed_status,
            "inferred_status": self.inference.to_dict(),
            "root": self.root.to_dict(),
            "descendants": [process.to_dict() for process in self.descendants],
            "connections": [connection.to_dict() for connection in self.connections],
            "launch_record": self.launch_record.to_dict()
            if self.launch_record is not None
            else None,
        }
