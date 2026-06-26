from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .classify import infer_status, is_codex_process
from .codex_state import scan_codex_state, scan_session_activities
from .models import (
    CodexSession,
    CodexStateSummary,
    Inference,
    NetworkConnection,
    ProcessInfo,
    SessionActivity,
)
from .hook_state import HookSessionState, load_hook_events, summarize_hook_events
from .procfs import read_network_connections, read_processes
from .shim import default_log_path, load_launch_records


ACTIVITY_TIMESTAMP_GRACE_SECONDS = 5.0


def inspect_runtime(
    proc_root: Path = Path("/proc"),
    sample_window: float = 0.25,
    shim_log: Path | None = None,
    codex_home: Path | None = None,
    hook_log: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[tuple[CodexSession, ...], CodexStateSummary]:
    sessions = discover_sessions(
        proc_root=proc_root,
        sample_window=sample_window,
        shim_log=shim_log,
        codex_home=codex_home,
        hook_log=hook_log,
        sleep=sleep,
    )
    state_summary = scan_codex_state(codex_home)
    return sessions, state_summary


def discover_sessions(
    proc_root: Path = Path("/proc"),
    sample_window: float = 0.25,
    shim_log: Path | None = None,
    codex_home: Path | None = None,
    hook_log: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CodexSession, ...]:
    first_snapshot = read_processes(proc_root)
    first_activities = scan_session_activities(codex_home) if sample_window > 0 else ()
    first_activities_by_path = {
        activity.relative_path: activity for activity in first_activities
    }
    if sample_window > 0:
        sleep(sample_window)
        second_snapshot = read_processes(proc_root)
        session_activities = scan_session_activities(
            codex_home,
            previous=first_activities_by_path,
        )
    else:
        second_snapshot = first_snapshot
        session_activities = scan_session_activities(codex_home)

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
    hook_states = summarize_hook_events(load_hook_events(hook_log))

    sessions = []
    for root in codex_roots:
        descendants = descendants_by_pid[root.pid]
        connections = _connections_for((root, *descendants), connections_by_pid)
        state_activity = _state_activity_for_root(root, session_activities)
        hook_state = _hook_state_for_root(root, hook_states)
        inference = infer_status(
            root,
            descendants,
            connections,
            sample_window,
            state_activity,
            hook_state,
        )
        sessions.append(
            CodexSession(
                root=root,
                descendants=descendants,
                connections=connections,
                inference=inference,
                state_activity=state_activity,
                hook_state=hook_state,
                launch_record=launch_records.get(root.pid),
                display_status=_display_status(inference, hook_state, state_activity),
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


def _state_activity_for_root(
    root: ProcessInfo,
    activities: tuple[SessionActivity, ...],
) -> SessionActivity | None:
    root_cwd = _normalize_path(root.cwd)
    candidates = []
    for activity in activities:
        activity_cwd = _normalize_path(activity.cwd)
        if root_cwd is None or activity_cwd is None:
            continue
        if root_cwd == activity_cwd:
            if _is_before_process_start(activity.modified_at, root):
                continue
            candidates.append(activity)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.modified_at, reverse=True)[0]


def _hook_state_for_root(
    root: ProcessInfo,
    states: dict[str, tuple[HookSessionState, ...]],
) -> HookSessionState | None:
    root_cwd = _normalize_path(root.cwd)
    if root_cwd is None:
        return None
    candidates = states.get(root_cwd, ())
    if not candidates:
        return None
    for state in candidates:
        if state.codex_pid == root.pid:
            return state
    for state in candidates:
        if state.codex_pid is None and not _is_before_process_start(state.updated_at, root):
            return state
    return None


def _display_status(
    inference: Inference,
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> str:
    if hook_state is not None:
        if hook_state.last_event == "session_start":
            return "未运行"
        if _activity_is_current_for_hook(state_activity, hook_state):
            if state_activity is not None and state_activity.failed_event:
                return "失败"
            if state_activity is not None and state_activity.terminal_event:
                return "成功"
        if hook_state.in_turn or hook_state.active_tool_count > 0:
            return "运行中"
        if state_activity is not None and state_activity.failed_event:
            return "失败"
        if state_activity is not None and state_activity.terminal_event:
            return "成功"
        if hook_state.last_event == "stop":
            return "成功"
        return "未运行"

    if state_activity is not None:
        if state_activity.failed_event:
            return "失败"
        if state_activity.terminal_event:
            return "成功"
        if state_activity.changed_during_sample:
            return "运行中"

    if inference.status in {
        "api_inflight_likely",
        "tool_running_likely",
        "active_likely",
    }:
        return "运行中"
    return "未运行"


def _activity_is_current_for_hook(
    state_activity: SessionActivity | None,
    hook_state: HookSessionState,
) -> bool:
    if state_activity is None:
        return False
    return (
        state_activity.modified_at + ACTIVITY_TIMESTAMP_GRACE_SECONDS
        >= hook_state.updated_at
    )


def _normalize_path(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(Path(value).absolute())


def _is_before_process_start(timestamp: float, process: ProcessInfo) -> bool:
    if process.started_at is None:
        return False
    return timestamp < process.started_at
