from __future__ import annotations

import unittest

from codex_cli_monitor.classify import infer_status, is_codex_process
from codex_cli_monitor.hook_state import HookSessionState
from codex_cli_monitor.models import NetworkConnection, ProcessInfo, SessionActivity


class ClassifyTests(unittest.TestCase):
    def test_long_lived_mcp_support_process_can_still_look_waiting(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        mcp = _process(
            101,
            "node",
            cmdline=("chrome-devtools-mcp",),
            ppid=100,
            state="S",
        )

        inference = infer_status(root, (mcp,), (), sample_window=0)

        self.assertEqual(inference.status, "waiting_user_likely")

    def test_shell_child_is_tool_running(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        shell = _process(
            101,
            "sh",
            cmdline=("sh", "-c", "pytest"),
            ppid=100,
            state="S",
        )

        inference = infer_status(root, (shell,), (), sample_window=0)

        self.assertEqual(inference.status, "tool_running_likely")

    def test_stopped_shell_child_is_not_tool_running(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        shell = _process(
            101,
            "sh",
            cmdline=("sh", "-c", "pytest"),
            ppid=100,
            state="T",
        )

        inference = infer_status(root, (shell,), (), sample_window=0)

        self.assertNotEqual(inference.status, "tool_running_likely")

    def test_hook_open_turn_overrides_waiting_sidecar_signals(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        hook_state = HookSessionState(
            cwd="/work/a",
            updated_at=1000.0,
            last_event="user_prompt_submit",
            in_turn=True,
        )

        inference = infer_status(
            root,
            (),
            (),
            sample_window=0,
            hook_state=hook_state,
        )

        self.assertEqual(inference.status, "api_inflight_likely")
        self.assertGreater(inference.confidence, 0.8)

    def test_hook_stop_overrides_stale_network_signals(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        connection = NetworkConnection(
            protocol="tcp4",
            local_address="172.24.3.172",
            local_port=50000,
            remote_address="172.24.0.1",
            remote_port=7890,
            state="ESTABLISHED",
            inode="12345",
        )
        hook_state = HookSessionState(
            cwd="/work/a",
            updated_at=1000.0,
            last_event="stop",
            in_turn=False,
        )

        inference = infer_status(
            root,
            (),
            (connection,),
            sample_window=0,
            hook_state=hook_state,
        )

        self.assertEqual(inference.status, "waiting_user_likely")

    def test_changing_session_file_is_active(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        activity = _activity(changed=True, age=1, last_payload_type="function_call")

        inference = infer_status(root, (), (), sample_window=1, state_activity=activity)

        self.assertEqual(inference.status, "active_likely")

    def test_network_without_recent_state_does_not_force_api_inflight(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        connection = NetworkConnection(
            protocol="tcp4",
            local_address="127.0.0.1",
            local_port=50000,
            remote_address="34.216.184.93",
            remote_port=443,
            state="ESTABLISHED",
            inode="12345",
        )
        activity = _activity(changed=False, age=300, last_payload_type="turn_aborted")

        inference = infer_status(
            root,
            (),
            (connection,),
            sample_window=1,
            state_activity=activity,
        )

        self.assertEqual(inference.status, "waiting_user_likely")

    def test_recent_function_call_state_can_indicate_api_inflight(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        connection = NetworkConnection(
            protocol="tcp4",
            local_address="127.0.0.1",
            local_port=50000,
            remote_address="34.216.184.93",
            remote_port=443,
            state="ESTABLISHED",
            inode="12345",
        )
        activity = _activity(changed=False, age=3, last_payload_type="function_call")

        inference = infer_status(
            root,
            (),
            (connection,),
            sample_window=1,
            state_activity=activity,
        )

        self.assertEqual(inference.status, "api_inflight_likely")

    def test_recent_state_with_proxy_connection_can_indicate_api_inflight(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        connection = NetworkConnection(
            protocol="tcp4",
            local_address="172.24.3.172",
            local_port=50000,
            remote_address="172.24.0.1",
            remote_port=7890,
            state="ESTABLISHED",
            inode="12345",
        )
        activity = _activity(changed=False, age=3, last_payload_type="reasoning")

        inference = infer_status(
            root,
            (),
            (connection,),
            sample_window=1,
            state_activity=activity,
        )

        self.assertEqual(inference.status, "api_inflight_likely")

    def test_codex_process_allows_deleted_exe_suffix(self) -> None:
        process = _process(
            100,
            "MainThread",
            cmdline=(),
            ppid=1,
            state="S",
            tty="/dev/pts/1",
        )
        process = ProcessInfo(
            pid=process.pid,
            ppid=process.ppid,
            comm=process.comm,
            state=process.state,
            cmdline=process.cmdline,
            cwd=process.cwd,
            exe="/tmp/.codex/bin/codex (deleted)",
            tty=process.tty,
            tty_nr=process.tty_nr,
            elapsed_seconds=process.elapsed_seconds,
            cpu_seconds=process.cpu_seconds,
        )

        self.assertTrue(is_codex_process(process))


def _process(
    pid: int,
    comm: str,
    cmdline: tuple[str, ...] | None = None,
    ppid: int = 1,
    state: str = "S",
    tty: str | None = None,
) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        comm=comm,
        state=state,
        cmdline=cmdline or (comm,),
        cwd="/work/a",
        exe=f"/usr/bin/{comm}",
        tty=tty,
        tty_nr=34816 if tty else 0,
        elapsed_seconds=10,
        cpu_seconds=1,
    )


def _activity(
    changed: bool,
    age: float,
    last_payload_type: str,
) -> SessionActivity:
    observed_at = 1000.0
    return SessionActivity(
        relative_path="sessions/2026/06/26/rollout.jsonl",
        session_id="session",
        turn_id="turn",
        cwd="/work/a",
        size_bytes=100,
        modified_at=observed_at - age,
        observed_at=observed_at,
        changed_during_sample=changed,
        last_record_type="response_item",
        last_payload_type=last_payload_type,
        terminal_event=last_payload_type in {"turn_aborted", "thread_rolled_back"},
    )


if __name__ == "__main__":
    unittest.main()
