from __future__ import annotations

import time
from pathlib import Path

from .hook_state import HookSessionState
from .models import Evidence, Inference, NetworkConnection, ProcessInfo, SessionActivity


CPU_ACTIVE_SECONDS = 0.02
RECENT_SESSION_ACTIVITY_SECONDS = 15.0
RECENT_FUNCTION_CALL_SECONDS = 90.0
SUPPORT_PROCESS_MARKERS = (
    "chrome-devtools-mcp",
    "/mcp/",
    "modelcontextprotocol",
    "telemetry/watchdog",
)
INACTIVE_PROCESS_STATES = {"T", "t", "Z", "X", "x"}
TOOL_COMMAND_NAMES = {
    "bash",
    "dash",
    "fish",
    "make",
    "node",
    "npm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "sh",
    "yarn",
}
STATUSES = {
    "waiting_user_likely",
    "api_inflight_likely",
    "tool_running_likely",
    "active_likely",
    "unknown",
}


def infer_status(
    root: ProcessInfo,
    descendants: tuple[ProcessInfo, ...],
    connections: tuple[NetworkConnection, ...],
    sample_window: float,
    state_activity: SessionActivity | None = None,
    hook_state: HookSessionState | None = None,
) -> Inference:
    hook_inference = _infer_from_hook_state(hook_state)
    if hook_inference is not None:
        return hook_inference

    tool_children = tuple(
        process
        for process in descendants
        if not is_codex_process(process) and _looks_like_active_tool_process(process)
    )
    if tool_children:
        command = _display_process(tool_children[0])
        return Inference(
            status="tool_running_likely",
            confidence=0.86,
            evidence=(
                Evidence(
                    "process_tree",
                    f"Observed descendant process {tool_children[0].pid}: {command}",
                ),
            ),
            limitations=(
                "Child process purpose is inferred from the process tree, not from Codex internals.",
            ),
        )

    total_cpu_delta = _total_cpu_delta((root, *descendants))
    if state_activity is not None and state_activity.changed_during_sample:
        return Inference(
            status="active_likely",
            confidence=0.78,
            evidence=(
                Evidence(
                    "codex_state",
                    "Associated session file changed during the sample: "
                    f"{state_activity.relative_path}",
                ),
                Evidence(
                    "codex_state",
                    f"Last structured event is {state_activity.event_label()}.",
                ),
            ),
            limitations=(
                "Session file activity shows Codex is recording events, but not the exact internal state.",
            ),
        )

    remote_connections = tuple(
        connection for connection in connections if connection.is_established_remote()
    )
    if (
        remote_connections
        and _has_recent_nonterminal_state_activity(state_activity)
        and (total_cpu_delta is None or total_cpu_delta < CPU_ACTIVE_SECONDS)
    ):
        connection = remote_connections[0]
        return Inference(
            status="api_inflight_likely",
            confidence=0.64,
            evidence=(
                Evidence(
                    "network",
                    "Observed established remote connection "
                    f"{connection.remote_address}:{connection.remote_port}",
                ),
                Evidence(
                    "process_tree",
                    "No local tool descendant was observed.",
                ),
                Evidence(
                    "codex_state",
                    "Associated session file has recent non-terminal activity "
                    f"{state_activity.modified_age_seconds:.1f}s ago.",
                ),
            ),
            limitations=(
                "Remote API waiting is inferred from recent Codex state plus a live connection; keep-alive sockets can still confuse this signal.",
            ),
        )

    if (
        state_activity is not None
        and not state_activity.terminal_event
        and state_activity.modified_age_seconds <= RECENT_SESSION_ACTIVITY_SECONDS
    ):
        return Inference(
            status="active_likely",
            confidence=0.66,
            evidence=(
                Evidence(
                    "codex_state",
                    "Associated session file was recently updated "
                    f"{state_activity.modified_age_seconds:.1f}s ago.",
                ),
                Evidence(
                    "codex_state",
                    f"Last structured event is {state_activity.event_label()}.",
                ),
            ),
            limitations=(
                "Recent session metadata is inferred from Codex local state, not a definitive live event stream.",
            ),
        )

    if total_cpu_delta is not None and total_cpu_delta >= CPU_ACTIVE_SECONDS:
        return Inference(
            status="active_likely",
            confidence=0.72,
            evidence=(
                Evidence(
                    "cpu_delta",
                    f"Observed {total_cpu_delta:.3f}s CPU time over {sample_window:.3f}s sample.",
                ),
            ),
            limitations=(
                "CPU activity does not reveal whether work is local, remote, or UI-related.",
            ),
        )

    support_descendants = tuple(
        process
        for process in descendants
        if _looks_like_support_process(process)
        and not _looks_like_active_tool_process(process)
    )
    if _looks_idle_at_tty(root, descendants, support_descendants):
        evidence = [
            Evidence("process_state", f"Root process state is {root.state or 'unknown'}."),
            Evidence("process_tree", "No local tool descendant was observed."),
        ]
        if support_descendants:
            evidence.append(
                Evidence(
                    "process_tree",
                    "Only long-lived support descendants were observed.",
                )
            )
        if root.tty:
            evidence.append(Evidence("tty", f"TTY appears to be {root.tty}."))
        if total_cpu_delta is not None:
            evidence.append(
                Evidence(
                    "cpu_delta",
                    f"Observed {total_cpu_delta:.3f}s CPU time over {sample_window:.3f}s sample.",
                )
            )
        if state_activity is not None:
            evidence.append(
                Evidence(
                    "codex_state",
                    "Associated session file is not changing; "
                    f"last event {state_activity.event_label()} "
                    f"{state_activity.modified_age_seconds:.1f}s ago.",
                )
            )
        return Inference(
            status="waiting_user_likely",
            confidence=0.58 if total_cpu_delta is not None else 0.46,
            evidence=tuple(evidence),
            limitations=(
                "Readiness for user input is inferred without scraping the terminal.",
            ),
        )

    return Inference(
        status="unknown",
        confidence=0.2,
        evidence=(
            Evidence(
                "signals",
                "Available process, CPU, child, TTY, and network signals are insufficient or contradictory.",
            ),
        ),
    )


def is_codex_process(process: ProcessInfo) -> bool:
    names = {
        _clean_process_name(Path(value).name)
        for value in (process.command_name, process.comm or "", process.exe or "")
        if value
    }
    if names.intersection({"codex", "codex.exe"}):
        return True

    cmdline_text = "\0".join(process.cmdline)
    if "@openai/codex" in cmdline_text:
        return True
    if "/codex/bin/" in cmdline_text and "node" in names:
        return True
    return False


def _clean_process_name(value: str) -> str:
    return value.removesuffix(" (deleted)")


def _looks_idle_at_tty(
    root: ProcessInfo,
    descendants: tuple[ProcessInfo, ...],
    support_descendants: tuple[ProcessInfo, ...],
) -> bool:
    if len(descendants) != len(support_descendants):
        return False
    if root.state not in {"S", "I", "T", None}:
        return False
    return bool(root.tty or root.cwd)


def _looks_like_active_tool_process(process: ProcessInfo) -> bool:
    if process.state in INACTIVE_PROCESS_STATES:
        return False
    if _looks_like_support_process(process):
        return False
    if process.cpu_delta_seconds is not None and process.cpu_delta_seconds >= CPU_ACTIVE_SECONDS:
        return True
    if process.state in {"R", "D"}:
        return True
    return process.command_name in TOOL_COMMAND_NAMES


def _looks_like_support_process(process: ProcessInfo) -> bool:
    command = _display_process(process)
    command_lower = command.lower()
    return any(marker in command_lower for marker in SUPPORT_PROCESS_MARKERS)


def _has_recent_nonterminal_state_activity(
    state_activity: SessionActivity | None,
) -> bool:
    if state_activity is None or state_activity.terminal_event:
        return False
    return state_activity.modified_age_seconds <= RECENT_SESSION_ACTIVITY_SECONDS


def _infer_from_hook_state(hook_state: HookSessionState | None) -> Inference | None:
    if hook_state is None:
        return None
    age = max(0.0, time.time() - hook_state.updated_at)
    evidence = (
        Evidence(
            "codex_hook",
            f"Last hook event {hook_state.last_event} observed {age:.1f}s ago.",
        ),
    )
    if hook_state.active_tool_count > 0:
        return Inference(
            status="tool_running_likely",
            confidence=0.92,
            evidence=evidence,
            limitations=("Hook events are trusted local lifecycle signals, not Codex internals.",),
        )
    if hook_state.in_turn:
        return Inference(
            status="api_inflight_likely",
            confidence=0.86,
            evidence=evidence,
            limitations=(
                "Hook events show a turn is open and no tool is currently running; this may include reasoning or remote API waiting.",
            ),
        )
    return Inference(
        status="waiting_user_likely",
        confidence=0.9,
        evidence=evidence,
        limitations=("Hook Stop/UserPromptSubmit events define this state for the monitored lifecycle.",),
    )


def _total_cpu_delta(processes: tuple[ProcessInfo, ...]) -> float | None:
    deltas = [process.cpu_delta_seconds for process in processes]
    known = [delta for delta in deltas if delta is not None]
    if not known:
        return None
    return sum(known)


def _display_process(process: ProcessInfo) -> str:
    if process.cmdline:
        return " ".join(process.cmdline[:4])
    return process.command_name or f"pid {process.pid}"
