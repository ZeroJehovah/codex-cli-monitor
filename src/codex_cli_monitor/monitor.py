from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .classify import is_native_codex_process
from .codex_state import default_codex_home
from .hook_state import HookSessionState, load_hook_events, summarize_hook_events
from .models import (
    CodexSession,
    CodexStateSummary,
    Evidence,
    Inference,
    ProcessInfo,
    SessionActivity,
)
from .procfs import read_processes
from .shim import default_log_path, load_launch_records
from .terminal_state import scan_process_terminal_activities, scan_terminal_activity


INACTIVE_ROOT_STATES = {"T", "t", "Z", "X", "x"}


@dataclass(frozen=True)
class _LifecycleCandidate:
    hook_state: HookSessionState | None
    state_activity: SessionActivity | None
    display_status: str
    updated_at: float
    binding_method: str
    binding_confidence: float
    binding_evidence: tuple[str, ...]


def inspect_runtime(
    proc_root: Path = Path("/proc"),
    sample_window: float = 0.0,
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
    state_home = (codex_home or default_codex_home()).expanduser()
    state_summary = CodexStateSummary(codex_home=str(state_home), newest_files=())
    return sessions, state_summary


def discover_sessions(
    proc_root: Path = Path("/proc"),
    sample_window: float = 0.0,
    shim_log: Path | None = None,
    codex_home: Path | None = None,
    hook_log: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CodexSession, ...]:
    # Retain these arguments for compatibility with existing service templates.
    # Runtime status refreshes intentionally never wait for CPU-delta sampling.
    del sample_window, sleep
    processes = read_processes(proc_root)
    codex_roots = _find_codex_roots(processes)
    if not codex_roots:
        return ()

    state_home = (codex_home or default_codex_home()).expanduser()
    grouped_hook_states = summarize_hook_events(load_hook_events(hook_log))
    hook_states_by_pid = {
        root.pid: _hook_states_for_root(root, grouped_hook_states)
        for root in codex_roots
    }
    visible_roots = tuple(
        root
        for root in codex_roots
        if any(state.has_turn_activity for state in hook_states_by_pid[root.pid])
    )
    if not visible_roots:
        return ()

    launch_records = load_launch_records(shim_log or default_log_path())
    sessions = []
    for root in visible_roots:
        candidates = _lifecycle_candidates_for_root(
            root=root,
            hook_states=hook_states_by_pid[root.pid],
            proc_root=proc_root,
            codex_home=state_home,
        )
        if not candidates:
            continue
        selected = _select_lifecycle_candidate(candidates)
        sessions.append(
            CodexSession(
                root=root,
                descendants=tuple(_collect_descendants(root.pid, processes)),
                connections=(),
                inference=_lifecycle_inference(
                    selected.display_status,
                    selected.hook_state,
                    selected.state_activity,
                ),
                state_activity=selected.state_activity,
                hook_state=selected.hook_state,
                launch_record=launch_records.get(root.pid),
                display_status=selected.display_status,
                binding_method=selected.binding_method,
                binding_confidence=selected.binding_confidence,
                binding_ambiguous=False,
                binding_candidate_count=len(candidates),
                binding_evidence=selected.binding_evidence,
            )
        )
    return tuple(sorted(sessions, key=lambda session: session.root.pid))


def _lifecycle_candidates_for_root(
    *,
    root: ProcessInfo,
    hook_states: tuple[HookSessionState, ...],
    proc_root: Path,
    codex_home: Path,
) -> tuple[_LifecycleCandidate, ...]:
    displayable_hook_states = tuple(
        state for state in hook_states if state.has_turn_activity
    )
    candidates = [
        _hook_lifecycle_candidate(state, scan_terminal_activity(state, codex_home))
        for state in displayable_hook_states
    ]

    for activity in scan_process_terminal_activities(
        root.pid,
        proc_root=proc_root,
        codex_home=codex_home,
        cwd=root.cwd,
    ):
        if not _is_new_fd_lifecycle(activity, displayable_hook_states, root):
            continue
        candidates.append(
            _LifecycleCandidate(
                hook_state=None,
                state_activity=activity,
                display_status=_lifecycle_display_status(None, activity),
                updated_at=activity.last_record_at or 0.0,
                binding_method="process_fd_session_id",
                binding_confidence=1.0,
                binding_evidence=(
                    "session file bound by an open file descriptor on the Codex PID",
                    "lifecycle event bound by the file session_id and structured turn_id",
                ),
            )
        )
    return tuple(candidates)


def _hook_lifecycle_candidate(
    hook_state: HookSessionState,
    state_activity: SessionActivity | None,
) -> _LifecycleCandidate:
    if state_activity is not None:
        binding_method = "session_id"
        binding_confidence = 1.0
        binding_evidence = (
            "process bound by hook parent PID",
            "terminal file bound by hook session_id",
        )
    else:
        binding_method = "hook_pid"
        binding_confidence = 0.98
        binding_evidence = (
            "process bound by hook parent PID",
            "no matching terminal file was available",
        )
    return _LifecycleCandidate(
        hook_state=hook_state,
        state_activity=state_activity,
        display_status=_lifecycle_display_status(hook_state, state_activity),
        updated_at=max(
            hook_state.updated_at,
            state_activity.last_record_at
            if state_activity is not None and state_activity.last_record_at is not None
            else 0.0,
        ),
        binding_method=binding_method,
        binding_confidence=binding_confidence,
        binding_evidence=binding_evidence,
    )


def _is_new_fd_lifecycle(
    activity: SessionActivity,
    hook_states: tuple[HookSessionState, ...],
    root: ProcessInfo,
) -> bool:
    event_at = activity.last_record_at
    if event_at is None or _is_before_process_start(event_at, root):
        return False
    same_session = tuple(
        state for state in hook_states if state.session_id == activity.session_id
    )
    if not same_session:
        return True
    if activity.turn_id and any(
        activity.turn_id in {state.turn_id, state.last_stopped_turn_id}
        for state in same_session
    ):
        return False
    return event_at > max(state.updated_at for state in same_session)


def _select_lifecycle_candidate(
    candidates: tuple[_LifecycleCandidate, ...],
) -> _LifecycleCandidate:
    running = tuple(
        candidate for candidate in candidates if candidate.display_status == "运行中"
    )
    return max(running or candidates, key=lambda candidate: candidate.updated_at)


def _lifecycle_display_status(
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> str:
    if state_activity is not None and state_activity.terminal_event:
        return "失败" if state_activity.failed_event else "成功"
    if hook_state is not None:
        return "运行中" if hook_state.in_turn else "成功"
    if state_activity is not None and state_activity.turn_active:
        return "运行中"
    return "成功"


def _lifecycle_inference(
    display_status: str,
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> Inference:
    if display_status == "失败":
        event_type = (
            state_activity.last_payload_type if state_activity is not None else "terminal"
        )
        return Inference(
            status="failure_terminal",
            confidence=1.0,
            evidence=(
                Evidence(
                    "codex_terminal_event",
                    f"Structured {event_type} event reported failure.",
                ),
            ),
        )
    if display_status == "运行中":
        if hook_state is None:
            event_type = (
                state_activity.last_payload_type
                if state_activity is not None
                else "task_started"
            )
            return Inference(
                status="running_terminal",
                confidence=1.0,
                evidence=(
                    Evidence(
                        "codex_terminal_event",
                        f"Structured {event_type} opened a PID-bound turn.",
                    ),
                ),
            )
        return Inference(
            status="running_hook",
            confidence=1.0,
            evidence=(
                Evidence(
                    "codex_hook",
                    f"UserPromptSubmit opened turn {hook_state.turn_id or 'unknown'}.",
                ),
            ),
        )
    if state_activity is not None and state_activity.terminal_event:
        signal = "codex_terminal_event"
        status = "success_terminal"
        detail = (
            f"Structured {state_activity.last_payload_type} event completed the turn."
        )
    else:
        signal = "codex_hook"
        status = "success_hook"
        detail = "Stop completed the hook-managed turn."
    return Inference(
        status=status,
        confidence=1.0,
        evidence=(Evidence(signal, detail),),
    )


def _find_codex_roots(processes: dict[int, ProcessInfo]) -> tuple[ProcessInfo, ...]:
    codex_pids = {
        pid for pid, process in processes.items() if is_native_codex_process(process)
    }
    visible_codex_pids = {
        pid
        for pid in codex_pids
        if processes[pid].state not in INACTIVE_ROOT_STATES
        and not _is_confirmed_detached_terminal_root(processes[pid], processes)
    }
    roots = (
        processes[pid]
        for pid in visible_codex_pids
        if processes[pid].ppid not in visible_codex_pids
    )
    return tuple(sorted(roots, key=lambda process: process.pid))


def _is_confirmed_detached_terminal_root(
    process: ProcessInfo,
    processes: dict[int, ProcessInfo],
) -> bool:
    if process.tty_nr is None or process.tty_nr <= 0:
        return False
    if process.tty is not None and process.tty.endswith(" (deleted)"):
        return True
    if (
        process.foreground_process_group_id is not None
        and process.foreground_process_group_id < 0
    ):
        return True
    return (
        process.session_id is not None
        and process.session_id > 1
        and process.session_id not in processes
    )


def _collect_descendants(
    root_pid: int,
    processes: dict[int, ProcessInfo],
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


def _hook_states_for_root(
    root: ProcessInfo,
    states: dict[str, tuple[HookSessionState, ...]],
) -> tuple[HookSessionState, ...]:
    root_cwd = _normalize_path(root.cwd)
    if root_cwd is None:
        return ()
    return tuple(
        state
        for state in states.get(root_cwd, ())
        if (
            state.codex_pid == root.pid
            and not _is_before_process_start(state.updated_at, root)
        )
    )


def _normalize_path(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(Path(value).absolute())


def _is_before_process_start(timestamp: float, process: ProcessInfo) -> bool:
    return process.started_at is not None and timestamp < process.started_at
