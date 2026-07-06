from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .classify import infer_status, is_native_codex_process
from .codex_state import (
    scan_codex_state,
    scan_session_activities,
)
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
SESSION_ACTIVITY_MAX_FILES = 80
SESSION_ACTIVITY_METADATA_MAX_FILES = 2000
SESSION_ACTIVITY_CANDIDATE_LIMIT_PER_ROOT = 24
INACTIVE_ROOT_STATES = {"T", "t", "Z", "X", "x"}
SESSION_BINDING_UNKNOWN_DELTA_SECONDS = 365 * 24 * 3600.0
CODEX_MAINTENANCE_ARGS = {
    "--self-update",
    "--self_update",
    "--update",
    "--upgrade",
    "add",
    "install",
    "self-update",
    "self_update",
    "update",
    "upgrade",
}
PACKAGE_MANAGER_COMMANDS = {
    "corepack",
    "npm",
    "npx",
    "pnpm",
    "yarn",
}


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
    first_activities = (
        scan_session_activities(
            codex_home,
            max_files=SESSION_ACTIVITY_METADATA_MAX_FILES,
            metadata_only=True,
        )
        if sample_window > 0
        else ()
    )
    first_activities_by_path = {
        activity.relative_path: activity for activity in first_activities
    }
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
    hook_states = summarize_hook_events(load_hook_events(hook_log))
    hook_states_by_pid = {
        root.pid: _hook_state_for_root(root, hook_states) for root in codex_roots
    }
    metadata_activities = scan_session_activities(
        codex_home,
        max_files=SESSION_ACTIVITY_METADATA_MAX_FILES,
        previous=first_activities_by_path,
        metadata_only=True,
    )
    candidate_paths = _activity_candidate_paths_for_roots(
        codex_roots,
        metadata_activities,
        hook_states_by_pid,
    )
    session_activities = scan_session_activities(
        codex_home,
        max_files=SESSION_ACTIVITY_MAX_FILES,
        previous=first_activities_by_path,
        include_relative_paths=candidate_paths,
    )
    state_activities_by_pid = _state_activities_for_roots(
        codex_roots,
        session_activities,
        hook_states_by_pid,
    )

    sessions = []
    for root in codex_roots:
        descendants = descendants_by_pid[root.pid]
        connections = _connections_for((root, *descendants), connections_by_pid)
        hook_state = hook_states_by_pid[root.pid]
        state_activity = state_activities_by_pid.get(root.pid)
        if _should_ignore_maintenance_root(
            root,
            descendants,
            hook_state,
            state_activity,
        ):
            continue
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
                display_status=_display_status(
                    inference,
                    root,
                    descendants,
                    hook_state,
                    state_activity,
                ),
            )
        )

    return tuple(sorted(sessions, key=lambda session: session.root.pid))


def _activity_candidate_paths_for_roots(
    roots: tuple[ProcessInfo, ...],
    activities: tuple[SessionActivity, ...],
    hook_states_by_pid: dict[int, HookSessionState | None],
) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for root in roots:
        hook_state = hook_states_by_pid.get(root.pid)
        candidates = sorted(
            _activity_candidates_for_root(root, activities, hook_state),
            key=lambda activity: _activity_sort_key_for_root(
                root,
                activity,
                hook_state,
            ),
        )[:SESSION_ACTIVITY_CANDIDATE_LIMIT_PER_ROOT]
        for activity in candidates:
            if activity.relative_path in seen:
                continue
            seen.add(activity.relative_path)
            paths.append(activity.relative_path)
    return tuple(paths)


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
    codex_pids = {
        pid for pid, process in processes.items() if is_native_codex_process(process)
    }
    visible_codex_pids = {
        pid
        for pid in codex_pids
        if not _is_inactive_root_state(processes[pid])
    }
    roots = [
        processes[pid]
        for pid in visible_codex_pids
        if processes[pid].ppid not in visible_codex_pids
    ]
    return tuple(sorted(roots, key=lambda process: process.pid))


def _is_inactive_root_state(process: ProcessInfo) -> bool:
    return process.state in INACTIVE_ROOT_STATES


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


def _state_activities_for_roots(
    roots: tuple[ProcessInfo, ...],
    activities: tuple[SessionActivity, ...],
    hook_states_by_pid: dict[int, HookSessionState | None],
) -> dict[int, SessionActivity]:
    candidate_pairs: list[
        tuple[tuple[float, float, float, float, float, float], int, str, SessionActivity]
    ] = []
    for root in roots:
        hook_state = hook_states_by_pid.get(root.pid)
        for activity in _activity_candidates_for_root(root, activities, hook_state):
            candidate_pairs.append(
                (
                    _activity_sort_key_for_root(root, activity, hook_state),
                    root.pid,
                    activity.relative_path,
                    activity,
                )
            )

    assigned_roots: set[int] = set()
    assigned_activities: set[str] = set()
    result: dict[int, SessionActivity] = {}
    for _, pid, relative_path, activity in sorted(candidate_pairs):
        if pid in assigned_roots or relative_path in assigned_activities:
            continue
        result[pid] = activity
        assigned_roots.add(pid)
        assigned_activities.add(relative_path)
    return result


def _activity_candidates_for_root(
    root: ProcessInfo,
    activities: tuple[SessionActivity, ...],
    hook_state: HookSessionState | None = None,
) -> tuple[SessionActivity, ...]:
    root_cwd = _normalize_path(root.cwd)
    candidates = []
    for activity in activities:
        activity_cwd = _normalize_path(activity.cwd)
        if root_cwd is None or activity_cwd is None:
            continue
        if root_cwd == activity_cwd:
            if _activity_is_before_process_start(activity, root):
                continue
            if hook_state is not None and not _activity_matches_hook(
                activity,
                hook_state,
            ):
                continue
            candidates.append(activity)
    return tuple(candidates)


def _activity_sort_key_for_root(
    root: ProcessInfo,
    activity: SessionActivity,
    hook_state: HookSessionState | None,
) -> tuple[float, float, float, float]:
    hook_rank = 0.0 if hook_state is not None else 1.0
    hook_lifecycle_rank = _hook_lifecycle_sort_rank(hook_state)
    delta = _activity_hook_delta(activity, hook_state)
    if delta is None:
        delta = _activity_process_start_delta(activity, root)
    if delta is None:
        delta = SESSION_BINDING_UNKNOWN_DELTA_SECONDS
    event_at = _activity_event_time(activity)
    recency = -(event_at or activity.modified_at)
    return (
        hook_rank,
        hook_lifecycle_rank,
        delta,
        recency,
    )


def _hook_lifecycle_sort_rank(hook_state: HookSessionState | None) -> float:
    if hook_state is None:
        return 2.0
    if hook_state.in_turn or hook_state.active_tool_count > 0:
        return 0.0
    return 1.0


def _activity_hook_delta(
    activity: SessionActivity,
    hook_state: HookSessionState | None,
) -> float | None:
    if hook_state is None:
        return None
    if hook_state.turn_started_at is not None and activity.turn_started_at is not None:
        return abs(activity.turn_started_at - hook_state.turn_started_at)
    if (
        hook_state.last_stopped_at is not None
        and activity.terminal_event_at is not None
    ):
        return abs(activity.terminal_event_at - hook_state.last_stopped_at)
    event_at = _activity_event_time(activity)
    if event_at is not None:
        return abs(event_at - hook_state.updated_at)
    return None


def _activity_process_start_delta(
    activity: SessionActivity,
    root: ProcessInfo,
) -> float | None:
    if root.started_at is None:
        return None
    activity_started_at = (
        activity.session_started_at
        or activity.turn_started_at
        or activity.last_record_at
        or activity.modified_at
    )
    return abs(activity_started_at - root.started_at)


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


def _should_ignore_maintenance_root(
    root: ProcessInfo,
    descendants: tuple[ProcessInfo, ...],
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> bool:
    if not _looks_like_codex_maintenance_tree(root, descendants):
        return False
    if hook_state is not None and (
        hook_state.in_turn or hook_state.active_tool_count > 0
    ):
        return False
    if (
        state_activity is not None
        and state_activity.latest_turn_has_user
        and not state_activity.terminal_event
    ):
        return False
    return True


def _looks_like_codex_maintenance_tree(
    root: ProcessInfo,
    descendants: tuple[ProcessInfo, ...],
) -> bool:
    if _process_has_codex_maintenance_args(root):
        return True
    return any(_process_is_codex_package_manager(process) for process in descendants)


def _process_has_codex_maintenance_args(process: ProcessInfo) -> bool:
    args = tuple(_normalized_arg(arg) for arg in process.cmdline[1:])
    return any(arg in CODEX_MAINTENANCE_ARGS for arg in args)


def _process_is_codex_package_manager(process: ProcessInfo) -> bool:
    command = _normalized_arg(process.command_name)
    if command not in PACKAGE_MANAGER_COMMANDS:
        return False
    args = tuple(_normalized_arg(arg) for arg in process.cmdline[1:])
    if not any(arg in CODEX_MAINTENANCE_ARGS for arg in args):
        return False
    command_text = "\0".join(process.cmdline).lower()
    return "@openai/codex" in command_text or "openai/codex" in command_text


def _normalized_arg(value: str) -> str:
    return Path(value).name.lower()


def _display_status(
    inference: Inference,
    root: ProcessInfo,
    descendants: tuple[ProcessInfo, ...],
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> str:
    if hook_state is not None:
        if hook_state.in_turn or hook_state.active_tool_count > 0:
            if (
                _activity_is_current_for_hook(state_activity, hook_state)
                and _activity_terminal_event_is_for_open_hook_turn(
                    state_activity,
                    hook_state,
                )
            ):
                if state_activity is not None and (
                    state_activity.failed_event
                    or _activity_is_missing_response_failure(
                        state_activity,
                        hook_state,
                    )
                ):
                    return "失败"
                return "成功"
            return "运行中"
        if _activity_is_current_for_hook(state_activity, hook_state):
            if state_activity is not None and state_activity.terminal_event:
                if state_activity.failed_event or _activity_is_missing_response_failure(
                    state_activity,
                    hook_state,
                ):
                    return "失败"
                return "成功"
        if state_activity is not None and state_activity.failed_event:
            return "失败"
        if state_activity is not None and state_activity.terminal_event:
            return "成功"
        if hook_state.last_event == "stop":
            return "成功"
        return "成功"

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
    return "成功"


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


def _activity_terminal_event_is_for_open_hook_turn(
    state_activity: SessionActivity | None,
    hook_state: HookSessionState,
) -> bool:
    if state_activity is None or not state_activity.terminal_event:
        return False
    started_at = hook_state.turn_started_at or hook_state.updated_at
    return _activity_event_time(state_activity) >= started_at


def _activity_matches_hook(
    activity: SessionActivity,
    hook_state: HookSessionState,
) -> bool:
    event_at = _activity_event_time(activity)
    turn_started_at = hook_state.turn_started_at
    if (
        turn_started_at is not None
        and event_at + ACTIVITY_TIMESTAMP_GRACE_SECONDS < turn_started_at
    ):
        return False

    stop_at = hook_state.last_stopped_at
    if stop_at is None and hook_state.last_event == "stop":
        stop_at = hook_state.updated_at
    if stop_at is not None and event_at > stop_at + ACTIVITY_TIMESTAMP_GRACE_SECONDS:
        return False

    if hook_state.in_turn or hook_state.active_tool_count > 0:
        started_at = hook_state.turn_started_at or hook_state.updated_at
        return event_at + ACTIVITY_TIMESTAMP_GRACE_SECONDS >= started_at

    return True


def _activity_is_missing_response_failure(
    activity: SessionActivity | None,
    hook_state: HookSessionState,
) -> bool:
    if activity is None:
        return False
    if not activity.terminal_event or not activity.terminal_agent_message_missing:
        return False
    if not activity.latest_turn_has_user:
        return False
    if activity.latest_turn_has_visible_response:
        return False
    if activity.changed_during_sample:
        return False
    if hook_state.last_stopped_at is not None or hook_state.last_event == "stop":
        return False
    if not (hook_state.in_turn or hook_state.active_tool_count > 0):
        return False
    return _activity_is_current_for_hook(activity, hook_state)


def _activity_event_time(activity: SessionActivity) -> float:
    return (
        activity.terminal_event_at
        or activity.last_record_at
        or activity.modified_at
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


def _activity_is_before_process_start(
    activity: SessionActivity,
    process: ProcessInfo,
) -> bool:
    if process.started_at is None:
        return False
    if activity.modified_at < process.started_at:
        return True
    return (
        activity.session_started_at is not None
        and activity.session_started_at + ACTIVITY_TIMESTAMP_GRACE_SECONDS
        < process.started_at
    )
