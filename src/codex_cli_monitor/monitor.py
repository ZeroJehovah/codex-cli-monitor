from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Callable, Mapping

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
from .terminal_state import (
    scan_process_terminal_activities,
    scan_terminal_activity,
    session_creation_time,
)
from .models import CodexSession, CodexStateSummary


INACTIVE_ROOT_STATES = {"T", "t", "Z", "X", "x"}

# Label used when Codex reports an approval prompt without naming the tool.
DEFAULT_CODEX_WAITING_REASON = "approval prompt"

# A Codex TUI client creates its session (thread) right after it starts.  When
# several live Codex terminals share one directory, a session owned by the
# shared managed app-server is attributed to the client that started shortly
# before the session was created, and never to one that started later.
SHARED_SESSION_START_WINDOW_SECONDS = 15.0

# Older Codex builds record session metadata with whole-second precision, which
# can land a moment before the millisecond process start it belongs to.
SHARED_SESSION_CREATED_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class _LifecycleCandidate:
    hook_state: HookSessionState | None
    state_activity: SessionActivity | None
    display_status: str
    updated_at: float
    binding_method: str
    binding_confidence: float
    binding_evidence: tuple[str, ...]


@dataclass(frozen=True)
class _SharedLifecycleSource:
    """Managed app-server daemons allowed to supply lifecycle for one root.

    ``session_ids`` is ``None`` when the directory holds a single live Codex
    root, which is the unambiguous case where the cwd alone may bind the shared
    daemon's sessions.  With several live roots the set names the sessions that
    provably belong to this root, so a plain same-directory neighbour can never
    inherit another client's running, success, or failure state.
    """

    processes: tuple[ProcessInfo, ...] = ()
    session_ids: frozenset[str] | None = None


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
        lifecycle_processes_by_pid = {
            root.pid: _lifecycle_processes_for_root(root, processes)
            for root in codex_roots
        }
        shared_lifecycle_sources_by_pid = {
            root.pid: _shared_lifecycle_source_for_root(
                root,
                codex_roots,
                processes,
                codex_home=state_home,
                hook_states_by_cwd=grouped_hook_states,
            )
            for root in codex_roots
        }
        hook_states_by_pid = {
            root.pid: _hook_states_for_root(
                root,
                grouped_hook_states,
                lifecycle_processes_by_pid[root.pid],
                shared_lifecycle_processes=(
                    shared_lifecycle_sources_by_pid[root.pid].processes
                ),
                shared_session_ids=(
                    shared_lifecycle_sources_by_pid[root.pid].session_ids
                ),
            )
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
                lifecycle_processes=lifecycle_processes_by_pid[root.pid],
                shared_lifecycle_processes=(
                    shared_lifecycle_sources_by_pid[root.pid].processes
                ),
                shared_session_ids=(
                    shared_lifecycle_sources_by_pid[root.pid].session_ids
                ),
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
    hook_events = opencode_hook_events(default_opencode_hook_log_path())
    directories = tuple(
        sorted({root.cwd for root in roots if root.cwd})
    )
    anchored_ids = tuple(
        sorted(
            {
                session_id
                for root in roots
                for session_id in (
                    _opencode_hook_session_id(root, hook_events),
                    _opencode_command_session_id(root.cmdline),
                )
                if session_id
            }
        )
    )
    states = scan_opencode_state(
        data_dir,
        ids=anchored_ids,
        directories=directories,
    )
    by_cwd: dict[str, list[OpenCodeSessionState]] = {}
    by_id: dict[str, OpenCodeSessionState] = {}
    for state in states:
        if state.cwd:
            by_cwd.setdefault(state.cwd, []).append(state)
        by_id[state.session_id] = state
    by_cwd = {path: tuple(items) for path, items in by_cwd.items()}

    decisions = pending_decisions(default_opencode_decision_log_path())
    sessions: list[CodexSession] = []

    def anchor_session(root: ProcessInfo) -> str | None:
        session_id = _opencode_hook_session_id(root, hook_events)
        if session_id is None:
            session_id = _opencode_command_session_id(root.cmdline)
        return session_id

    def open_anchor(root: ProcessInfo) -> bool:
        state = by_id.get(anchor_session(root))
        if state is None:
            return False
        return state.turn_active or state.status in OPEN_TURN_STATUSES

    used_session_ids: set[str] = set()
    ordered = sorted(
        roots,
        key=lambda process: (
            1 if open_anchor(process) else 0,
            process.started_at is None,
            process.started_at or 0.0,
            process.pid,
        ),
        reverse=True,
    )
    for root in ordered:
        state = _opencode_state_for_root(
            root, by_cwd, by_id, hook_events, used_session_ids
        )
        if state is None:
            continue
        used_session_ids.add(state.session_id)
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
    used_session_ids: AbstractSet[str],
) -> OpenCodeSessionState | None:
    session_id = _opencode_hook_session_id(root, hook_events)
    if session_id is None:
        session_id = _opencode_command_session_id(root.cmdline)

    bound = by_id.get(session_id) if session_id else None
    if bound is not None and bound.session_id in used_session_ids:
        bound = None
    if bound is not None:
        if bound.turn_active or bound.status in OPEN_TURN_STATUSES:
            return bound
    owned: list[OpenCodeSessionState] = []
    if bound is not None:
        owned.append(bound)
    cwd_candidates: list[OpenCodeSessionState] = []
    for state in by_cwd.get(root.cwd, ()):
        if state.session_id in used_session_ids:
            continue
        if state is bound:
            continue
        # A directory can contain abandoned open turns from previous processes.
        # Only activity during this process's lifetime supports a cwd binding.
        # An explicit resume/hook anchor above remains valid for an idle row.
        if (
            root.started_at is None
            or state.last_activity_at is None
            or state.last_activity_at < root.started_at - 2.0
        ):
            continue
        cwd_candidates.append(state)

    # A process may resume an existing conversation without exposing its
    # session id in argv (and without the optional lifecycle hook).  In that
    # case the session can predate the process by hours, so requiring a
    # post-start creation timestamp would incorrectly hide an otherwise
    # active row. Its message/tool activity can instead prove that it belongs
    # to the current process lifetime. Prefer newly created rows only within
    # the same lifecycle class, preserving resumed active conversations.
    fresh_cwd_candidates = [
        state
        for state in cwd_candidates
        if state.created_at is not None
        and root.started_at is not None
        and state.created_at >= root.started_at - 2.0
    ]
    open_cwd_candidates = [
        state
        for state in cwd_candidates
        if state.turn_active or state.status in OPEN_TURN_STATUSES
    ]
    if open_cwd_candidates:
        # A still-open row is stronger ownership evidence than the creation
        # time of a completed row.  This matters when several unanchored
        # OpenCode processes share a cwd: one process may be working in a
        # resumed session created long before it started, while a different,
        # recently-created row has already completed.  Filtering to fresh rows
        # first would bind the live process to that completed row and leave its
        # actual open row unused.
        owned.extend(open_cwd_candidates)
    elif fresh_cwd_candidates:
        owned.extend(fresh_cwd_candidates)
    elif bound is None:
        owned.extend(cwd_candidates)
    if not owned:
        return None
    candidates = tuple(owned)

    def activity_key(state) -> tuple[float, float, float]:
        """Rank rows by the most recent structural activity they contain.

        Creation-time proximity is not enough once one process has started
        several conversations in the same directory: a newer conversation
        may be the one currently shown by OpenCode even when an older row was
        created closer to the process start.  The latest message/tool/session
        activity is the strongest available unanchored ownership signal.
        """
        return tuple(
            value if value is not None else float("-inf")
            for value in (state.last_activity_at, state.updated_at, state.created_at)
        )
    open_turns = [
        state for state in candidates
        if state.turn_active or state.status in OPEN_TURN_STATUSES
    ]
    if open_turns:
        return max(open_turns, key=activity_key)
    return max(candidates, key=activity_key)

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
    lifecycle_processes: tuple[ProcessInfo, ...] | None = None,
    shared_lifecycle_processes: tuple[ProcessInfo, ...] | None = None,
    shared_session_ids: frozenset[str] | None = None,
) -> tuple[_LifecycleCandidate, ...]:
    lifecycle_processes = lifecycle_processes or (root,)
    shared_lifecycle_processes = shared_lifecycle_processes or ()
    displayable_hook_states = tuple(
        state for state in hook_states if state.has_turn_activity
    )
    shared_pids = {process.pid for process in shared_lifecycle_processes}
    candidates = [
        _hook_lifecycle_candidate(
            state,
            scan_terminal_activity(state, codex_home),
            shared=state.codex_pid in shared_pids,
        )
        for state in displayable_hook_states
    ]

    for lifecycle_process in lifecycle_processes:
        for activity in scan_process_terminal_activities(
            lifecycle_process.pid,
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
                "session file bound by an open file descriptor on the exact Codex lifecycle PID",
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

    # New Codex CLI versions can put the interactive TTY client in front of a
    # long-lived, shared managed app-server. In that layout the app-server
    # owns the rollout file and emits the Hook, while the TTY process is not
    # its child. The caller only supplies this fallback with the sessions it
    # proved belong to this root, so a concurrent same-directory client is
    # never given another client's lifecycle by cwd alone.
    for lifecycle_process in shared_lifecycle_processes:
        for activity in scan_process_terminal_activities(
            lifecycle_process.pid,
            proc_root=proc_root,
            codex_home=codex_home,
            cwd=None,
        ):
            if (
                shared_session_ids is not None
                and activity.session_id not in shared_session_ids
            ):
                continue
            if not _shared_activity_matches_root(
                activity,
                root,
                displayable_hook_states,
            ):
                continue
            if not _is_new_fd_lifecycle(
                activity,
                displayable_hook_states,
                root,
                allow_preexisting=allow_preexisting_fd_lifecycle,
            ):
                continue
            candidates.append(
                _LifecycleCandidate(
                    hook_state=None,
                    state_activity=activity,
                    display_status=_lifecycle_display_status(None, activity),
                    updated_at=activity.last_record_at or 0.0,
                    binding_method="shared_app_server_session",
                    binding_confidence=0.85,
                    binding_evidence=(
                        "session file bound by the shared managed Codex app-server",
                        "session cwd, creation time, and lifecycle time matched this Codex terminal",
                    ),
                )
            )
    return tuple(candidates)


def _hook_lifecycle_candidate(
    hook_state: HookSessionState,
    state_activity: SessionActivity | None,
    *,
    shared: bool = False,
) -> _LifecycleCandidate:
    if shared:
        binding_method = "shared_app_server_hook"
        binding_confidence = 0.85
        binding_evidence = (
            "Hook was emitted by the shared managed Codex app-server",
            "Hook cwd, session creation time, and lifecycle time matched this Codex terminal",
        )
    elif state_activity is not None:
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
        if is_native_codex_process(process)
        and not is_codex_exec_process(process)
        and not _is_codex_app_server_daemon(process)
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


def _shared_lifecycle_source_for_root(
    root: ProcessInfo,
    roots: tuple[ProcessInfo, ...],
    processes: dict[int, ProcessInfo],
    *,
    codex_home: Path,
    hook_states_by_cwd: Mapping[str, tuple[HookSessionState, ...]],
) -> _SharedLifecycleSource:
    """Return the shared managed daemons usable as lifecycle sources for a root.

    A managed app-server normally appears as a child of the interactive Codex
    process. Recent CLI builds may instead reuse one daemon for several TTY
    clients, so its parent is unrelated to the client that owns a Hook. There
    is no PID field in the rollout records, so the daemon's sessions are bound
    by directory and creation time: with one live root in the directory the cwd
    already identifies the client, and with several live roots in the directory
    only the sessions that were created right after this process started are
    attributed to it. Anything else stays hidden instead of guessing.
    """
    root_cwd = _normalize_path(root.cwd)
    if root_cwd is None:
        return _SharedLifecycleSource()
    same_cwd_roots = tuple(
        candidate
        for candidate in roots
        if _normalize_path(candidate.cwd) == root_cwd
    )
    root_pids = {candidate.pid for candidate in roots}
    shared_processes = tuple(
        process
        for process in processes.values()
        if _is_managed_codex_app_server(process)
        and process.ppid not in root_pids
    )
    if not shared_processes:
        return _SharedLifecycleSource()
    if len(same_cwd_roots) == 1:
        return _SharedLifecycleSource(shared_processes, None)
    session_ids = _shared_session_ids_for_root(
        root,
        same_cwd_roots,
        hook_states_by_cwd.get(root_cwd, ()),
        codex_home,
    )
    if not session_ids:
        return _SharedLifecycleSource()
    return _SharedLifecycleSource(shared_processes, session_ids)


def _shared_session_ids_for_root(
    root: ProcessInfo,
    same_cwd_roots: tuple[ProcessInfo, ...],
    hook_states: tuple[HookSessionState, ...],
    codex_home: Path,
) -> frozenset[str]:
    """Return the shared-daemon sessions that provably belong to this process.

    A Codex TUI client creates its session immediately after it starts.  With
    several live Codex roots in one directory, a session therefore belongs to
    the root that started most recently before the session was created, and
    only when that root started within a bounded window before it.  A session
    created before every root, or long after all of them, stays unattributed
    and keeps the row hidden.
    """
    owned: set[str] = set()
    for state in hook_states:
        created_at = session_creation_time(codex_home, state.session_id)
        if created_at is None:
            continue
        candidates = tuple(
            candidate
            for candidate in same_cwd_roots
            if candidate.started_at is not None
            and candidate.started_at
            <= created_at + SHARED_SESSION_CREATED_GRACE_SECONDS
            and created_at - candidate.started_at
            <= SHARED_SESSION_START_WINDOW_SECONDS
        )
        if not candidates:
            continue
        owner = max(candidates, key=lambda candidate: candidate.started_at or 0.0)
        if owner.pid == root.pid:
            owned.add(state.session_id)
    return frozenset(owned)


def _lifecycle_processes_for_root(
    root: ProcessInfo,
    processes: dict[int, ProcessInfo],
) -> tuple[ProcessInfo, ...]:
    """Include exact managed app-server children as lifecycle signal owners.

    Recent Codex CLI versions keep the interactive TTY process as the user-facing
    process, while a managed app-server child emits its Hooks and holds the
    rollout file open. The child is not a separate displayed session: its exact
    process identity and parent relationship bind those signals back to this
    live TTY root.
    """
    root_cwd = _normalize_path(root.cwd)
    if root_cwd is None:
        return (root,)

    lifecycle_processes = [root]
    for child_pid in root.children:
        child = processes.get(child_pid)
        if (
            child is None
            or not _is_managed_codex_app_server(child)
            or _normalize_path(child.cwd) != root_cwd
        ):
            continue
        lifecycle_processes.append(child)
    return tuple(lifecycle_processes)


def _is_managed_codex_app_server(process: ProcessInfo) -> bool:
    if not is_native_codex_process(process):
        return False
    try:
        app_server_index = process.cmdline.index("app-server")
    except ValueError:
        return False
    return "--managed-daemon" in process.cmdline[app_server_index + 1 :]


def _is_codex_app_server_daemon(process: ProcessInfo) -> bool:
    """Return true for Codex's non-interactive app-server daemon processes."""
    if not is_native_codex_process(process):
        return False
    try:
        app_server_index = process.cmdline.index("app-server")
    except ValueError:
        return False
    arguments = process.cmdline[app_server_index + 1 :]
    return "--managed-daemon" in arguments or (
        bool(arguments) and arguments[0] == "daemon"
    )


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
    lifecycle_processes: tuple[ProcessInfo, ...] | None = None,
    *,
    shared_lifecycle_processes: tuple[ProcessInfo, ...] = (),
    shared_session_ids: frozenset[str] | None = None,
) -> tuple[HookSessionState, ...]:
    root_cwd = _normalize_path(root.cwd)
    if root_cwd is None:
        return ()
    lifecycle_pids = {
        process.pid: process
        for process in (lifecycle_processes or (root,))
        if _normalize_path(process.cwd) == root_cwd
    }
    shared_pids = {
        process.pid: process for process in shared_lifecycle_processes
    }
    return tuple(
        state
        for state in states.get(root_cwd, ())
        if _hook_state_matches_root(
            state,
            root,
            lifecycle_pids,
            shared_pids,
            shared_session_ids,
        )
    )


def _hook_state_matches_root(
    state: HookSessionState,
    root: ProcessInfo,
    lifecycle_pids: dict[int, ProcessInfo],
    shared_pids: dict[int, ProcessInfo],
    shared_session_ids: frozenset[str] | None,
) -> bool:
    if state.codex_pid in lifecycle_pids:
        return not _is_before_process_start(
            state.updated_at,
            lifecycle_pids[state.codex_pid],
        )
    if state.codex_pid in shared_pids:
        if (
            shared_session_ids is not None
            and state.session_id not in shared_session_ids
        ):
            return False
        # The shared daemon predates the terminal client. Its event must be
        # newer than the client itself, otherwise a new process could inherit
        # a completed turn from an older process in the same directory.
        return not _is_before_process_start(state.updated_at, root)
    return False


def _shared_activity_matches_root(
    activity: SessionActivity,
    root: ProcessInfo,
    hook_states: tuple[HookSessionState, ...],
) -> bool:
    activity_cwd = _normalize_path(activity.cwd)
    root_cwd = _normalize_path(root.cwd)
    if activity_cwd is not None:
        return activity_cwd == root_cwd
    return any(
        state.session_id == activity.session_id
        and _normalize_path(state.cwd) == root_cwd
        for state in hook_states
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
