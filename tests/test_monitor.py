from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from codex_cli_monitor.hook_state import append_hook_event
from codex_cli_monitor.monitor import discover_sessions, inspect_runtime


class MonitorTests(unittest.TestCase):
    def test_discovers_waiting_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].confirmed_status, "open")
        self.assertEqual(sessions[0].inference.status, "waiting_user_likely")
        self.assertEqual(sessions[0].display_status, "未运行")

    def test_classifies_descendant_as_tool_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(proc, 101, "bash", "S", 100, ["bash", "-lc", "pytest"], "/work/a")

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].inference.status, "tool_running_likely")
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].descendants[0].pid, 101)

    def test_stopped_codex_root_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "T", 1, ["codex"], "/work/a")

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(sessions, ())

    def test_new_codex_run_does_not_count_stopped_same_directory_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "T", 1, ["codex"], "/work/a")
            _write_process(proc, 200, "codex", "S", 1, ["codex"], "/work/a")

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root.pid, 200)
        self.assertEqual(sessions[0].display_status, "未运行")

    def test_network_alone_does_not_classify_api_inflight_likely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            (proc / "100" / "fd" / "9").symlink_to("socket:[12345]")
            (proc / "net").mkdir()
            (proc / "net" / "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                "   0: 0100007F:C350 5DB8D822:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 20 4 30 10 -1\n",
                encoding="utf-8",
            )
            (proc / "net" / "tcp6").write_text(
                "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n",
                encoding="utf-8",
            )

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].inference.status, "waiting_user_likely")
        self.assertEqual(sessions[0].display_status, "未运行")

    def test_changing_associated_session_file_marks_session_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user"}}\n',
                encoding="utf-8",
            )

            def mutate_session_file(_: float) -> None:
                session.write_text(
                    '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                    '{"type":"response_item","payload":{"type":"function_call","name":"exec_command"}}\n',
                    encoding="utf-8",
                )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=1,
                codex_home=home,
                sleep=mutate_session_file,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].inference.status, "active_likely")
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertIsNotNone(sessions[0].state_activity)

    def test_hook_session_start_marks_session_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            append_hook_event("session_start", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "未运行")
        self.assertIsNotNone(sessions[0].hook_state)

    def test_hook_state_overrides_waiting_sidecar_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].inference.status, "api_inflight_likely")
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertIsNotNone(sessions[0].hook_state)

    def test_hook_tool_lifecycle_marks_session_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("pre_tool_use", tool="Bash", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].inference.status, "tool_running_likely")
        self.assertEqual(sessions[0].display_status, "运行中")

    def test_hook_stop_with_successful_terminal_event_marks_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_session(
                home,
                "/work/a",
                '{"type":"response_item","payload":{"type":"task_complete"}}',
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "成功")
        self.assertFalse(sessions[0].state_activity.failed_event)

    def test_hook_stop_with_interrupted_terminal_event_marks_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_session(
                home,
                "/work/a",
                '{"type":"response_item","payload":{"type":"turn_aborted","reason":"interrupted"}}',
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "失败")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_hook_stop_with_retry_limit_message_marks_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_session(
                home,
                "/work/a",
                '{"type":"event_msg","payload":{"type":"agent_message","message":"■ exceeded retry limit, last status: 429 Too Many Requests"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}',
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "失败")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_hook_stop_with_stream_disconnect_message_marks_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_session(
                home,
                "/work/a",
                '{"type":"event_msg","payload":{"type":"agent_message","message":"■ stream disconnected before completion: stream closed before response.completed"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}',
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "失败")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_hook_stop_with_unexpected_http_status_message_marks_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_session(
                home,
                "/work/a",
                '{"type":"event_msg","payload":{"type":"agent_message","message":"■ unexpected status 503 Service Unavailable: auth_unavailable: no auth available (providers=codex)"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}',
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "失败")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_failed_session_event_overrides_open_hook_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            _write_session(
                home,
                "/work/a",
                '{"type":"response_item","payload":{"type":"error","message":"blocked"}}',
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "失败")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_same_cwd_new_session_does_not_inherit_old_success_hook_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(proc, 200, "codex", "S", 1, ["codex"], "/work/a")
            _write_session(
                home,
                "/work/a",
                '{"type":"response_item","payload":{"type":"task_complete"}}',
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=hook_log)
            append_hook_event("session_start", cwd="/work/a", ppid=200, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        sessions_by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(sessions_by_pid[100].display_status, "成功")
        self.assertEqual(sessions_by_pid[200].display_status, "未运行")

    def test_new_process_ignores_session_activity_from_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a", start_ticks=15000)
            session = _write_session(
                home,
                "/work/a",
                '{"type":"response_item","payload":{"type":"task_complete"}}',
            )
            old_mtime = time.time() - 120
            os.utime(session, (old_mtime, old_mtime))

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
            )

        self.assertEqual(len(sessions), 1)
        self.assertIsNone(sessions[0].state_activity)
        self.assertEqual(sessions[0].display_status, "未运行")

    def test_inspect_runtime_returns_sessions_and_state_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("{}\n", encoding="utf-8")

            sessions, state_summary = inspect_runtime(
                proc,
                sample_window=0,
                codex_home=home,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(state_summary.codex_home, str(home))
        self.assertEqual(state_summary.newest_files[0].kind, "session_jsonl")


def _write_common_proc(proc: Path) -> None:
    (proc / "uptime").write_text("200.00 0.00\n", encoding="utf-8")


def _write_process(
    proc: Path,
    pid: int,
    comm: str,
    state: str,
    ppid: int,
    cmdline: list[str],
    cwd: str,
    start_ticks: int = 100,
) -> None:
    pid_dir = proc / str(pid)
    (pid_dir / "fd").mkdir(parents=True)
    (pid_dir / "stat").write_text(
        _stat_line(pid, comm, state, ppid, start_ticks=start_ticks),
        encoding="utf-8",
    )
    (pid_dir / "cmdline").write_bytes(b"\0".join(item.encode() for item in cmdline) + b"\0")
    (pid_dir / "cwd").symlink_to(cwd)
    (pid_dir / "exe").symlink_to(f"/usr/bin/{cmdline[0]}")
    (pid_dir / "fd" / "0").symlink_to("/dev/pts/3")


def _write_session(home: Path, cwd: str, last_record: str) -> Path:
    session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        '{"type":"session_meta","payload":{"session_id":"s","cwd":"'
        + cwd
        + '"}}\n'
        + last_record
        + "\n",
        encoding="utf-8",
    )
    return session


def _stat_line(
    pid: int,
    comm: str,
    state: str,
    ppid: int,
    start_ticks: int = 100,
) -> str:
    fields = [
        state,
        str(ppid),
        "0",
        "0",
        "34816",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "5",
        "7",
        "0",
        "0",
        "20",
        "0",
        "1",
        "0",
        str(start_ticks),
    ]
    return f"{pid} ({comm}) {' '.join(fields)}\n"


if __name__ == "__main__":
    unittest.main()
