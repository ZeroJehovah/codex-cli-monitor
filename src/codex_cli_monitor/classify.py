from __future__ import annotations

from pathlib import Path

from .models import Evidence, Inference, NetworkConnection, ProcessInfo


CPU_ACTIVE_SECONDS = 0.02
SUPPORT_PROCESS_MARKERS = (
    "chrome-devtools-mcp",
    "/mcp/",
    "modelcontextprotocol",
    "telemetry/watchdog",
)
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
) -> Inference:
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
    remote_tls = tuple(connection for connection in connections if connection.is_remote_tls_like())
    if remote_tls and (total_cpu_delta is None or total_cpu_delta < CPU_ACTIVE_SECONDS):
        connection = remote_tls[0]
        return Inference(
            status="api_inflight_likely",
            confidence=0.62,
            evidence=(
                Evidence(
                    "network",
                    "Observed established remote TLS-like connection "
                    f"{connection.remote_address}:{connection.remote_port}",
                ),
                Evidence(
                    "process_tree",
                    "No local tool descendant was observed.",
                ),
            ),
            limitations=(
                "Network connections alone are not definitive because keep-alive sockets may remain open.",
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
