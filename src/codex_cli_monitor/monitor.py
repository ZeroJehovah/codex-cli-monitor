from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .classify import infer_status, is_codex_process
from .codex_state import scan_codex_state
from .models import CodexSession, CodexStateSummary, NetworkConnection, ProcessInfo
from .procfs import read_network_connections, read_processes
from .shim import default_log_path, load_launch_records


def inspect_runtime(
    proc_root: Path = Path("/proc"),
    sample_window: float = 0.25,
    shim_log: Path | None = None,
    codex_home: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[tuple[CodexSession, ...], CodexStateSummary]:
    sessions = discover_sessions(proc_root, sample_window, shim_log, sleep)
    state_summary = scan_codex_state(codex_home)
    return sessions, state_summary


def discover_sessions(
    proc_root: Path = Path("/proc"),
    sample_window: float = 0.25,
    shim_log: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CodexSession, ...]:
    first_snapshot = read_processes(proc_root)
    if sample_window > 0:
        sleep(sample_window)
        second_snapshot = read_processes(proc_root)
    else:
        second_snapshot = first_snapshot

    processes = _with_cpu_deltas(first_snapshot, second_snapshot, sample_window)
    codex_roots = _find_codex_roots(processes)
    if not codex_roots:
        return ()

    relevant_pids = set()
    descendants_by_pid: dict[int, tuple[ProcessInfo, ...]] = {}
    for root in codex_roots:
        descendants = tuple(_collect_descendants(root.pid, processes))
        descendants_by_pid[root.pid] = descendants
        relevant_pids.add(root.pid)
        relevant_pids.update(process.pid for process in descendants)

    connections_by_pid = read_network_connections(proc_root, relevant_pids)
    launch_records = load_launch_records(shim_log or default_log_path())

    sessions = []
    for root in codex_roots:
        descendants = descendants_by_pid[root.pid]
        connections = _connections_for((root, *descendants), connections_by_pid)
        inference = infer_status(root, descendants, connections, sample_window)
        sessions.append(
            CodexSession(
                root=root,
                descendants=descendants,
                connections=connections,
                inference=inference,
                launch_record=launch_records.get(root.pid),
            )
        )

    return tuple(sorted(sessions, key=lambda session: session.root.pid))


def _with_cpu_deltas(
    first: dict[int, ProcessInfo],
    second: dict[int, ProcessInfo],
    sample_window: float,
) -> dict[int, ProcessInfo]:
    if sample_window <= 0:
        return second

    result = {}
    for pid, process in second.items():
        previous = first.get(pid)
        delta = None
        if (
            previous is not None
            and previous.cpu_seconds is not None
            and process.cpu_seconds is not None
        ):
            delta = max(0.0, process.cpu_seconds - previous.cpu_seconds)
        result[pid] = replace(process, cpu_delta_seconds=delta)
    return result


def _find_codex_roots(processes: dict[int, ProcessInfo]) -> tuple[ProcessInfo, ...]:
    codex_pids = {pid for pid, process in processes.items() if is_codex_process(process)}
    roots = [
        processes[pid]
        for pid in codex_pids
        if processes[pid].ppid not in codex_pids
    ]
    return tuple(sorted(roots, key=lambda process: process.pid))


def _collect_descendants(
    root_pid: int, processes: dict[int, ProcessInfo]
) -> tuple[ProcessInfo, ...]:
    descendants = []
    stack = list(processes[root_pid].children)
    while stack:
        pid = stack.pop(0)
        child = processes.get(pid)
        if child is None:
            continue
        descendants.append(child)
        stack.extend(child.children)
    return tuple(descendants)


def _connections_for(
    processes: tuple[ProcessInfo, ...],
    connections_by_pid: dict[int, tuple[NetworkConnection, ...]],
) -> tuple[NetworkConnection, ...]:
    seen = set()
    result = []
    for process in processes:
        for connection in connections_by_pid.get(process.pid, ()):
            key = (
                connection.protocol,
                connection.local_address,
                connection.local_port,
                connection.remote_address,
                connection.remote_port,
                connection.state,
                connection.inode,
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(connection)
    return tuple(result)
