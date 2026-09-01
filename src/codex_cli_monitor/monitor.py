from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .classify import (
    is_claude_process,
    is_codex_exec_process,
    is_native_codex_process,
    is_opencode_process,
)
from .claude_state import ClaudeSessionState, claude_session_state
from .codex_state import default_codex_home
from .hook_state import HookSessionState, load_hook_events, summarize_hook_events
from .models import (
    OPEN_TURN_STATUSES,
    STATUS_FAILURE,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WAITING,
    Evidence,
    Inference,
    ProcessInfo,
    SessionActivity,
    normalize_waiting_reason,
)
from .opencode_decisions import (
    PendingDecision,
    default_opencode_decision_log_path,
    find_pending_decision,
    pending_decisions,
)
from .opencode_state import (
    OpenCodeSessionState,
    default_opencode_data_dir,
    default_opencode_hook_log_path,
    opencode_hook_events,
    scan_opencode_state,
)
from .procfs import read_processes
from .shim import default_log_path, load_launch_records
from .terminal_state import scan_process_terminal_activities, scan_terminal_activity
from .models import CodexSession, CodexStateSummary


INACTIVE_ROOT_STATES = {"T", "t", "Z", "X", "x"}

# Label used when Codex reports an approval prompt without naming the tool.
DEFAULT_CODEX_WAITING_REASON = "approval prompt"


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
    opencode_roots = _find_opencode_roots(processes)
    claude_roots = _find_claude_roots(processes)
    if not codex_roots and not opencode_roots and not claude_roots:
        return ()

    sessions: list[CodexSession] = []
    if codex_roots:
        state_home = (codex_home or default_codex_home()).expanduser()
        grouped_hook_states = summarize_hook_events(load_hook_events(hook_log))
        hook_states_by_pid = {
            root.pid: _hook_states_for_root(root, grouped_hook_states)
            for root in codex_roots
        }
        launch_records = load_launch_records(shim_log or default_log_path())
        for root in codex_roots:
            candidates = _lifecycle_candidates_for_root(
                root=root,
                hook_states=hook_states_by_pid[root.pid],
                proc_root=proc_root,
                codex_home=state_home,
                allow_preexisting_fd_lifecycle=_is_tmux_hosted(root, processes),
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
                    cli_type="codex",
                    waiting_reason=_codex_waiting_reason(selected),
                )
            )
    if opencode_roots:
        sessions.extend(
            _discover_opencode_sessions(opencode_roots, processes)
        )
    if claude_roots:
        sessions.extend(_discover_claude_sessions(claude_roots, processes))
    return tuple(sorted(sessions, key=lambda session: session.root.pid))


def _discover_claude_sessions(
    roots: tuple[ProcessInfo, ...],
    processes: dict[int, ProcessInfo],
) -> tuple[CodexSession, ...]:
    sessions: list[CodexSession] = []
    for root in roots:
        state = claude_session_state(root)
        if state is None:
            continue
        sessions.append(
            CodexSession(
                root=root,
                descendants=tuple(_collect_descendants(root.pid, processes)),
                connections=(),
                inference=_claude_inference(state),
                state_activity=None,
                hook_state=None,
                launch_record=None,
                display_status=state.status,
                binding_method="claude_session_registration",
                binding_confidence=1.0,
                binding_ambiguous=False,
                binding_candidate_count=1,
                binding_evidence=(
                    "process bound by the Claude Code PID registration and its "
                    "recorded process start time",
                    "lifecycle status read read-only from the registration and "
                    "the bound session transcript",
                ),
                cli_type="claude",
                waiting_reason=state.waiting_for,
            )
        )
    return tuple(sessions)


def _claude_inference(state: ClaudeSessionState) -> Inference:
    if state.status == STATUS_WAITING:
        return Inference(
            status="waiting_decision_registration",
            confidence=1.0,
            evidence=(
                Evidence(
                    "claude_session",
                    f"Claude Code session {state.session_id} reported status "
                    f"{state.registered_status!r}: the turn is open but blocked "
                    f"on {state.waiting_for or 'a user decision'}.",
                ),
            ),
            limitations=(
                "the session cannot advance until the prompt is answered in the "
                "Claude Code terminal",
            ),
        )
    if state.status == STATUS_RUNNING:
        return Inference(
            status="running_terminal",
            confidence=1.0,
            evidence=(
                Evidence(
                    "claude_session",
                    f"Claude Code session {state.session_id} reported status "
                    f"{state.registered_status!r} with an open turn.",
                ),
            ),
        )
    if state.status == STATUS_FAILURE:
        return Inference(
            status="failure_terminal",
            confidence=1.0,
            evidence=(
                Evidence(
                    "claude_transcript",
                    f"Claude Code session {state.session_id} ended its last turn "
                    "with a structured API error or mid-stream abort.",
                ),
            ),
        )
    return Inference(
        status="success_terminal",
        confidence=1.0,
        evidence=(
            Evidence(
                "claude_transcript",
                f"Claude Code session {state.session_id} completed its last turn; "
                f"last activity {_age_description(state.last_activity_at)}.",
            ),
        ),
    )


def _discover_opencode_sessions(
    roots: tuple[ProcessInfo, ...],
    processes: dict[int, ProcessInfo],
) -> tuple[CodexSession, ...]:
    data_dir = default_opencode_data_dir()
    states = scan_opencode_state(
        data_dir,
        ids=tuple(),
    )
    by_cwd: dict[str, list[OpenCodeSessionState]] = {}
    by_id: dict[str, OpenCodeSessionState] = {}
    for state in states:
        if state.cwd:
            by_cwd.setdefault(state.cwd, []).append(state)
        by_id[state.session_id] = state
    by_cwd = {path: tuple(items) for path, items in by_cwd.items()}

    hook_events = opencode_hook_events(default_opencode_hook_log_path())
    decisions = pending_decisions(default_opencode_decision_log_path())
    sessions: list[CodexSession] = []
    for root in roots:
        state = _opencode_state_for_root(root, by_cwd, by_id, hook_events)
        if state is None:
            continue
        binding_method = "opencode_hook_session_id" if _opencode_hook_confirms_root(
            root, hook_events
        ) else "opencode_sqlite_cwd"
        decision = _opencode_pending_decision(state, root, decisions)
        display_status = STATUS_WAITING if decision is not None else state.status
        binding_evidence = [
            "OpenCode process bound to session by current working directory",
            "lifecycle status read read-only from the OpenCode SQLite database",
        ]
        if decision is not None:
            binding_evidence.append(
                "open prompt reported by the OpenCode decision plugin"
            )
        sessions.append(
            CodexSession(
                root=root,
                descendants=tuple(_collect_descendants(root.pid, processes)),
                connections=(),
                inference=_opencode_inference(state, decision),
                state_activity=None,
                hook_state=None,
                launch_record=None,
                display_status=display_status,
                binding_method=binding_method,
                binding_confidence=1.0,
                binding_ambiguous=False,
                binding_candidate_count=1,
                binding_evidence=tuple(binding_evidence),
                cli_type="opencode",
                waiting_reason=decision.reason if decision is not None else None,
            )
        )
    return tuple(sessions)


def _opencode_pending_decision(
    state: OpenCodeSessionState,
    root: ProcessInfo,
    decisions: tuple[PendingDecision, ...],
) -> PendingDecision | None:
    """Return the open prompt blocking this OpenCode row, if there is one.

    The overlay only ever relabels a turn the database already reports as open.

    A decision marker left behind by a session that was killed at its prompt can
    therefore never resurrect a finished row, and the monitor can never invent an
    open turn that OpenCode does not have.  A marker recorded by a different
    OpenCode process or for a different session is never inherited by a new row
    in the same working directory: each pending decision is bound to the exact
    process (and session) that opened it.
    """
    if not decisions or state.status != STATUS_RUNNING:
        return None
    return find_pending_decision(
        decisions,
        session_id=state.session_id,
        directory=state.cwd or root.cwd,
        pid=root.pid,
    )


def _opencode_inference(
    state: OpenCodeSessionState,
    decision: PendingDecision | None,
) -> Inference:
    if decision is not None:
        return Inference(
            status="waiting_decision_plugin",
            confidence=1.0,
            evidence=(
                Evidence(
                    "opencode_decision_plugin",
                    f"OpenCode session {state.session_id} opened a "
                    f"{decision.kind} prompt ({decision.reason}) "
                    f"{_age_description(decision.asked_at)} and it is unanswered.",
                ),
            ),
            limitations=(
                "the session cannot advance until the prompt is answered in the "
                "OpenCode terminal",
            ),
        )
    return Inference(
        status=_opencode_inference_status(state.status),
        confidence=1.0,
        evidence=(
            Evidence(
                "opencode_sqlite",
                f"OpenCode session {state.session_id} "
                f"({state.status}); last activity "
                f"{_age_description(state.last_activity_at)}.",
            ),
        ),
    )


def _opencode_state_for_root(
    root: ProcessInfo,
    by_cwd: Mapping[str, tuple[OpenCodeSessionState,...]],
    by_id: Mapping[str, OpenCodeSessionState],
    hook_events: tuple[dict, ...],
) -> OpenCodeSessionState | None:
    session_id = _opencode_hook_session_id(root, hook_events)
    if session_id is None:
        session_id = _opencode_command_session_id(root.cmdline)
    if session_id and session_id in by_id:
        return by_id[session_id]
    if root.cwd is None:
        return None
    candidates = by_cwd.get(root.cwd, ())
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    owned: tuple[OpenCodeSessionState, ...] = tuple(
        state
        for state in candidates
        if state.created_at is not None
        and root.started_at is not None
        and state.created_at >= root.started_at - 2.0
    )
    if len(owned) == 1:
        return owned[0]
    if len(owned) > 1:
        candidates = owned
    return candidates[0]


def _opencode_hook_session_id(
    root: ProcessInfo,
    hook_events: tuple[dict, ...],
) -> str | None:
    """Return the session id bound to this exact process by hook markers.

    A hook marker is spawned by OpenCode, so its recorded ``ppid`` is the
    process pid that owns the session(this is also how Codex hooks bind).  The
    recorded ``pid`` (the hook process itself) is accepted as well in case a
    future OpenCode build invokes hooks without a shell intermediate.
    """
    latest_at = -1.0
    session_id: str | None = None
    for event in hook_events:
        if event.get("ppid") == root.pid or event.get("pid") == root.pid:
            candidate = event.get("session_id")
            timestamp = _optional_float(event.get("timestamp"))
            if (
                isinstance(candidate, str)
                and candidate
                and timestamp is not None
                and timestamp >= latest_at
            ):
                latest_at = timestamp
                session_id = candidate
    return session_id


def _opencode_command_session_id(cmdline: tuple[str, ...]) -> str | None:
    """Return an explicit ``opencode -s <session-id>`` resume identifier."""
    tokens = tuple(cmdline)
    for index, token in enumerate(tokens):
        if token in ("-s", "--session", "--session-id"):
            if index + 1 < len(tokens):
                value = tokens[index + 1]
                if value and not value.startswith("-"):
                    return value
            continue
        if token.startswith("--session="):
            return token.split("=", 1)[1] or None
        if token.startswith("--session-id="):
            return token.split("=", 1)[1] or None
    return None


def _opencode_hook_confirms_root(
    root: ProcessInfo,
    hook_events: tuple[dict, ...],
) -> bool:
    for event in hook_events:
        if event.get("ppid") == root.pid or event.get("pid") == root.pid:
            return True
        cwd = event.get("cwd")
        if cwd and root.cwd and _normalize_path(cwd) == _normalize_path(root.cwd):
            return True
    return False


def _opencode_inference_status(status: str) -> str:
    if status == STATUS_RUNNING:
        return "running_terminal"
    if status == STATUS_FAILURE:
        return "failure_terminal"
    return "success_terminal"


def _age_description(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown"
    age = max(0.0, time.time() - timestamp)
    return f"{age:.0f}s ago"


def _lifecycle_candidates_for_root(
    *,
    root: ProcessInfo,
    hook_states: tuple[HookSessionState, ...],
    proc_root: Path,
    codex_home: Path,
    allow_preexisting_fd_lifecycle: bool = False,
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
        if not _is_new_fd_lifecycle(
            activity,
            displayable_hook_states,
            root,
            allow_preexisting=allow_preexisting_fd_lifecycle,
        ):
            continue
        binding_evidence = [
            "session file bound by an open file descriptor on the Codex PID",
            "lifecycle event bound by the file session_id and structured turn_id",
        ]
        if (
            allow_preexisting_fd_lifecycle
            and activity.turn_started_at is not None
            and _is_before_process_start(activity.turn_started_at, root)
        ):
            binding_evidence.append(
                "live tmux ancestry permits the exact open resumed lifecycle"
            )
        candidates.append(
            _LifecycleCandidate(
                hook_state=None,
                state_activity=activity,
                display_status=_lifecycle_display_status(None, activity),
                updated_at=activity.last_record_at or 0.0,
                binding_method="process_fd_session_id",
                binding_confidence=1.0,
                binding_evidence=tuple(binding_evidence),
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
    *,
    allow_preexisting: bool = False,
) -> bool:
    lifecycle_at = activity.turn_started_at
    if allow_preexisting and activity.terminal_event:
        lifecycle_at = activity.terminal_event_at
    preexisting_active = (
        allow_preexisting
        and activity.turn_active
        and activity.turn_started_at is not None
    )
    if (
        lifecycle_at is None
        or root.started_at is None
        or (
            not preexisting_active
            and _is_before_process_start(lifecycle_at, root)
        )
    ):
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
    return lifecycle_at > max(state.updated_at for state in same_session)


def _select_lifecycle_candidate(
    candidates: tuple[_LifecycleCandidate, ...],
) -> _LifecycleCandidate:
    # An open turn always wins over a finished one, whether it is advancing on
    # its own or blocked on an approval prompt.  Among open turns the blocked one
    # wins: this PID cannot finish until the user answers, and showing the
    # neighbouring running session instead would hide exactly that.
    open_turns = tuple(
        candidate
        for candidate in candidates
        if candidate.display_status in OPEN_TURN_STATUSES
    )
    waiting = tuple(
        candidate
        for candidate in open_turns
        if candidate.display_status == STATUS_WAITING
    )
    return max(
        waiting or open_turns or candidates,
        key=lambda candidate: candidate.updated_at,
    )


def _lifecycle_display_status(
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> str:
    if state_activity is not None and state_activity.terminal_event:
        return STATUS_FAILURE if state_activity.failed_event else STATUS_SUCCESS
    if hook_state is not None:
        if not hook_state.in_turn:
            return STATUS_SUCCESS
        return (
            STATUS_WAITING
            if _codex_awaiting_decision(hook_state, state_activity)
            else STATUS_RUNNING
        )
    if state_activity is not None and state_activity.turn_active:
        return STATUS_RUNNING
    return STATUS_SUCCESS


def _codex_awaiting_decision(
    hook_state: HookSessionState,
    state_activity: SessionActivity | None,
) -> bool:
    """True when Codex stopped at an approval prompt and has not moved on.

    ``PermissionRequest`` is the only signal that Codex is waiting: the rollout
    file writes the tool call before asking and its output only after the tool
    finishes, so nothing there separates "waiting for approval" from "running".
    A rollout record written *after* the prompt opened proves Codex resumed,
    which releases the row even when the approved command runs long enough that
    its ``PostToolUse`` edge is still pending.
    """
    pending_at = hook_state.permission_pending_at
    if not hook_state.awaiting_decision or pending_at is None:
        return False
    if state_activity is None or state_activity.last_record_at is None:
        return True
    return state_activity.last_record_at <= pending_at


def _codex_waiting_reason(candidate: _LifecycleCandidate) -> str | None:
    if candidate.display_status != STATUS_WAITING or candidate.hook_state is None:
        return None
    return (
        normalize_waiting_reason(candidate.hook_state.permission_tool)
        or DEFAULT_CODEX_WAITING_REASON
    )


def _lifecycle_inference(
    display_status: str,
    hook_state: HookSessionState | None,
    state_activity: SessionActivity | None,
) -> Inference:
    if display_status == STATUS_FAILURE:
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
    if display_status == STATUS_WAITING:
        tool = hook_state.permission_tool if hook_state is not None else None
        return Inference(
            status="waiting_decision_hook",
            confidence=1.0,
            evidence=(
                Evidence(
                    "codex_hook",
                    "PermissionRequest opened an approval prompt for "
                    f"{tool or 'a tool call'} and no later activity was recorded.",
                ),
            ),
            limitations=(
                "the turn cannot advance until the prompt is answered in the "
                "Codex terminal",
            ),
        )
    if display_status == STATUS_RUNNING:
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
        pid
        for pid, process in processes.items()
        if is_native_codex_process(process) and not is_codex_exec_process(process)
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


def _is_tmux_hosted(
    process: ProcessInfo,
    processes: dict[int, ProcessInfo],
) -> bool:
    visited: set[int] = set()
    pid = process.ppid
    while pid is not None and pid > 0 and pid not in visited:
        visited.add(pid)
        ancestor = processes.get(pid)
        if ancestor is None:
            return False
        if _is_tmux_process(ancestor):
            return True
        pid = ancestor.ppid
    return False


def _is_tmux_process(process: ProcessInfo) -> bool:
    command = process.command_name.lower()
    comm = (process.comm or "").lower()
    return command in {"tmux", "tmux.exe"} or comm.startswith("tmux:")


def _find_opencode_roots(processes: dict[int, ProcessInfo]) -> tuple[ProcessInfo, ...]:
    opencode_pids = {
        pid for pid, process in processes.items() if is_opencode_process(process)
    }
    visible_opencode_pids = {
        pid
        for pid in opencode_pids
        if processes[pid].state not in INACTIVE_ROOT_STATES
        and not _is_confirmed_detached_terminal_root(processes[pid], processes)
    }
    roots = (
        processes[pid]
        for pid in visible_opencode_pids
        if processes[pid].ppid not in visible_opencode_pids
    )
    return tuple(sorted(roots, key=lambda process: process.pid))


def _find_claude_roots(processes: dict[int, ProcessInfo]) -> tuple[ProcessInfo, ...]:
    claude_pids = {
        pid for pid, process in processes.items() if is_claude_process(process)
    }
    visible_claude_pids = {
        pid
        for pid in claude_pids
        if processes[pid].state not in INACTIVE_ROOT_STATES
        and not _is_confirmed_detached_terminal_root(processes[pid], processes)
    }
    roots = (
        processes[pid]
        for pid in visible_claude_pids
        if processes[pid].ppid not in visible_claude_pids
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
