from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.claude_state import (
    encode_project_dir,
    reset_caches as reset_claude_caches,
)
from codex_cli_monitor.hook_state import append_hook_event
from codex_cli_monitor.monitor import discover_sessions, inspect_runtime
from codex_cli_monitor.terminal_state import MAX_INITIAL_TAIL_BYTES


CLAUDE_SESSION_ID = "c578535a-e73e-4f74-86dd-af2273c5375b"
CLAUDE_CWD = "/work/claude"


class MonitorTests(unittest.TestCase):
    def test_new_process_is_monitored_but_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")

            sessions = discover_sessions(proc, sample_window=1, sleep=_fail_sleep)

        self.assertEqual(sessions, ())

    def test_session_start_alone_is_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, _home, hook_log = _runtime(tmp)
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                path=hook_log,
                hook_payload={"session_id": "session-a"},
            )

            sessions = discover_sessions(proc, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_prompt_hook_displays_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_hook")
        self.assertEqual(sessions[0].connections, ())

    def test_stop_hook_displays_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _hook(hook_log, "stop", "session-a", "turn-a")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "成功")
        self.assertEqual(sessions[0].inference.status, "success_hook")

    def test_codex_exec_process_is_excluded_even_with_prompt_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            # 增加一个 exec 形态的 codex 进程（pid 101），并给它提交提示词 hook
            _write_process(
                proc,
                101,
                "codex",
                "S",
                1,
                ["codex", "exec", "--model", "test-model"],
                "/work/exec",
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/exec",
                ppid=101,
                path=hook_log,
                hook_payload={"session_id": "session-exec", "turn_id": "turn-exec"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        # exec codex(101) 即使有匹配的提示词 hook 也被排除；默认 codex(100) 无 hook 不显示
        self.assertEqual(sessions, ())

    def test_normal_codex_process_still_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")

    def test_tmux_resumed_goal_started_before_process_launch_is_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _tmux_runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(
                home,
                session_id,
                "turn-resumed",
                "task_started",
                timestamp=time.time() - 300,
            )
            _bind_open_session(proc, 102, path, 35)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root.pid, 102)
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_terminal")
        self.assertEqual(sessions[0].binding_method, "process_fd_session_id")
        self.assertIn("tmux", " ".join(sessions[0].binding_evidence))

    def test_tmux_terminal_event_after_launch_keeps_resumed_goal_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _tmux_runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(
                home,
                session_id,
                "turn-resumed",
                "task_started",
                timestamp=time.time() - 300,
            )
            _append_terminal(path, "turn-completed", "task_complete", error=None)
            _bind_open_session(proc, 102, path, 35)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root.pid, 102)
        self.assertEqual(sessions[0].display_status, "成功")
        self.assertEqual(sessions[0].inference.status, "success_terminal")

    def test_tmux_terminal_event_before_launch_does_not_create_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _tmux_runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(
                home,
                session_id,
                "turn-old",
                "task_complete",
                error=None,
                timestamp=time.time() - 300,
            )
            _bind_open_session(proc, 102, path, 35)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_turn_complete_error_displays_failure_without_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(
                home,
                "session-a",
                "turn-a",
                "task_complete",
                error={"message": "request failed"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "失败")
        self.assertEqual(sessions[0].inference.status, "failure_terminal")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_turn_aborted_displays_failure_without_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(home, "session-a", "turn-a", "turn_aborted")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "失败")

    def test_turn_complete_without_error_displays_success_when_stop_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(home, "session-a", "turn-a", "task_complete", error=None)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "成功")

    def test_diagnostic_error_text_is_not_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            session = _session_path(home, "session-a")
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"timestamp":"2026-07-29T00:00:00Z","type":"event_msg",'
                '"payload":{"type":"agent_message","message":"API error 500"}}\n',
                encoding="utf-8",
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")

    def test_new_prompt_is_not_overridden_by_previous_turn_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-old")
            _write_terminal(home, "session-a", "turn-old", "turn_aborted")
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-new")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")

    def test_same_pid_active_session_beats_later_old_session_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            base = time.time() - 10
            _hook(
                hook_log,
                "user_prompt_submit",
                "session-old",
                "turn-old",
                timestamp=base,
            )
            _hook(
                hook_log,
                "user_prompt_submit",
                "session-new",
                "turn-new",
                timestamp=base + 1,
            )
            _hook(
                hook_log,
                "stop",
                "session-old",
                "turn-old",
                timestamp=base + 2,
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].hook_state.session_id, "session-new")

    def test_goal_continuation_task_started_on_open_session_fd_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            old_session = "019fb158-36bb-7440-9128-2e4e0f7c6168"
            new_session = "019fb176-333f-7071-aa87-1d1837579794"
            _hook(hook_log, "user_prompt_submit", old_session, "turn-old")
            _hook(hook_log, "stop", old_session, "turn-old")
            old_path = _write_terminal(
                home, old_session, "turn-old", "task_complete", error=None
            )
            new_path = _write_terminal(
                home, new_session, "turn-auto", "task_started"
            )
            _extend_with_sparse_gap(
                new_path,
                MAX_INITIAL_TAIL_BYTES + 64 * 1024,
            )
            _bind_open_session(proc, 100, old_path, 14)
            _bind_open_session(proc, 100, new_path, 25)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_terminal")
        self.assertEqual(sessions[0].state_activity.session_id, new_session)
        self.assertIsNone(sessions[0].hook_state)
        self.assertEqual(sessions[0].binding_method, "process_fd_session_id")

    def test_direct_goal_task_started_displays_without_prompt_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(home, session_id, "turn-auto", "task_started")
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_terminal")
        self.assertIsNone(sessions[0].hook_state)
        self.assertEqual(sessions[0].binding_method, "process_fd_session_id")

    def test_direct_goal_completion_remains_displayed_without_prompt_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(home, session_id, "turn-auto", "task_started")
            _append_terminal(path, "turn-auto", "task_complete", error=None)
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "成功")
        self.assertEqual(sessions[0].inference.status, "success_terminal")

    def test_process_does_not_inherit_goal_started_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(
                home,
                session_id,
                "turn-old",
                "task_started",
                timestamp=time.time() - 300,
            )
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_direct_goal_requires_known_process_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            (proc / "uptime").unlink()
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(home, session_id, "turn-auto", "task_started")
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_same_directory_processes_bind_by_pid_and_session_id(self) -> None:
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
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", ppid=100)
            _hook(hook_log, "user_prompt_submit", "session-b", "turn-b", ppid=200)
            _write_terminal(home, "session-a", "turn-a", "task_complete", error=None)
            _write_terminal(home, "session-b", "turn-b", "turn_aborted")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(by_pid[100].display_status, "成功")
        self.assertEqual(by_pid[200].display_status, "失败")
        self.assertNotEqual(
            by_pid[100].state_activity.relative_path,
            by_pid[200].state_activity.relative_path,
        )

    def test_process_exit_removes_stale_open_hook_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            self.assertEqual(
                len(discover_sessions(proc, codex_home=home, hook_log=hook_log)),
                1,
            )
            for child in tuple(proc.iterdir()):
                if child.name.isdigit():
                    _remove_fake_process(child)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_detached_or_stopped_process_is_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "T", 1, ["codex"], "/work/a")
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")

            sessions = discover_sessions(proc, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_runtime_scan_does_not_sample_cpu_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            with patch(
                "codex_cli_monitor.procfs.read_network_connections",
                side_effect=AssertionError("network scan must not run"),
            ):
                sessions = discover_sessions(
                    proc,
                    sample_window=200,
                    codex_home=home,
                    hook_log=hook_log,
                    sleep=_fail_sleep,
                )

        self.assertEqual(len(sessions), 1)

    def test_inspect_runtime_keeps_state_summary_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(home, "session-a", "turn-a", "task_complete", error=None)

            sessions, summary = inspect_runtime(
                proc,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(summary.codex_home, str(home))


class ClaudeMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_claude_caches()

    def test_unregistered_claude_process_is_not_displayed(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            (claude_home / "sessions").mkdir(parents=True)

            sessions = discover_sessions(proc)

        self.assertEqual(sessions, ())

    def test_busy_claude_session_displays_running(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(claude_home, status="busy")
            _write_claude_transcript(
                claude_home,
                [{"type": "user", "origin": {"kind": "human"}}],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.root.pid, 300)
        self.assertEqual(session.cli_type, "claude")
        self.assertEqual(session.display_status, "运行中")
        self.assertEqual(session.inference.status, "running_terminal")
        self.assertEqual(session.binding_method, "claude_session_registration")
        self.assertEqual(session.binding_confidence, 1.0)
        self.assertFalse(session.binding_ambiguous)
        self.assertEqual(session.inference.evidence[0].signal, "claude_session")

    def test_idle_claude_session_without_a_turn_is_not_displayed(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(claude_home, status="idle")

            sessions = discover_sessions(proc)

        self.assertEqual(sessions, ())

    def test_completed_claude_turn_displays_success(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(claude_home, status="idle")
            _write_claude_transcript(
                claude_home,
                [
                    {"type": "user", "origin": {"kind": "human"}},
                    {"type": "assistant"},
                ],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "成功")
        self.assertEqual(sessions[0].inference.status, "success_terminal")
        self.assertEqual(
            sessions[0].inference.evidence[0].signal,
            "claude_transcript",
        )

    def test_failed_claude_turn_displays_failure(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(claude_home, status="idle")
            _write_claude_transcript(
                claude_home,
                [
                    {"type": "user", "origin": {"kind": "human"}},
                    {"type": "assistant", "isApiErrorMessage": True},
                ],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "失败")
        self.assertEqual(sessions[0].inference.status, "failure_terminal")

    def test_reused_pid_registration_is_not_displayed(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(claude_home, status="busy", proc_start=999999)

            sessions = discover_sessions(proc)

        self.assertEqual(sessions, ())

    def test_claude_session_on_a_launch_dialog_is_not_displayed(self) -> None:
        # A `claude` another CLI spawned into a pty nobody is watching parks on
        # its onboarding dialog and reports `waiting` indefinitely without ever
        # writing a transcript.  It must not show up as a session.
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(
                claude_home,
                status="waiting",
                waiting_for="dialog open",
            )

            sessions = discover_sessions(proc)

        self.assertEqual(sessions, ())

    def test_claude_and_codex_sessions_coexist(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            hook_log = claude_home.parent / "hooks.jsonl"
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_claude_registration(claude_home, status="busy")
            _write_claude_transcript(
                claude_home,
                [{"type": "user", "origin": {"kind": "human"}}],
            )

            sessions = discover_sessions(proc, hook_log=hook_log)

        self.assertEqual([session.root.pid for session in sessions], [100, 300])
        self.assertEqual(
            [session.cli_type for session in sessions],
            ["codex", "claude"],
        )


class WaitingDecisionTests(unittest.TestCase):
    """待确认: turns that stopped and cannot advance without the user."""

    def setUp(self) -> None:
        reset_claude_caches()

    def test_codex_permission_request_displays_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            now = time.time()
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", timestamp=now - 60)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 30,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.display_status, "待确认")
        self.assertEqual(session.waiting_reason, "Bash")
        self.assertEqual(session.inference.status, "waiting_decision_hook")
        self.assertTrue(session.inference.limitations)

    def test_codex_waiting_reason_falls_back_when_no_tool_is_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            now = time.time()
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", timestamp=now - 60)
            append_hook_event(
                "permission_request",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 30,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "待确认")
        self.assertEqual(sessions[0].waiting_reason, "approval prompt")

    def test_codex_answered_prompt_returns_to_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            now = time.time()
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", timestamp=now - 60)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 30,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )
            append_hook_event(
                "post_tool_use",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 10,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertIsNone(sessions[0].waiting_reason)

    def test_codex_rollout_activity_after_the_prompt_releases_waiting(self) -> None:
        # An approved long-running command keeps PostToolUse pending, but any
        # rollout record written after the prompt proves Codex moved on.
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            now = time.time()
            _hook(
                hook_log,
                "user_prompt_submit",
                session_id,
                "turn-a",
                timestamp=now - 90,
            )
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 60,
                path=hook_log,
                hook_payload={"session_id": session_id, "turn_id": "turn-a"},
            )
            _write_terminal(
                home,
                session_id,
                "turn-a",
                "task_started",
                timestamp=now - 20,
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertIsNone(sessions[0].waiting_reason)

    def test_codex_stop_clears_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            now = time.time()
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", timestamp=now - 60)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 30,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )
            _hook(hook_log, "stop", "session-a", "turn-a", timestamp=now - 5)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "成功")
        self.assertIsNone(sessions[0].waiting_reason)

    def test_waiting_turn_beats_a_newer_finished_session(self) -> None:
        # A row must never be reported as finished while another session on the
        # same process is still holding an unanswered prompt.
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            now = time.time()
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", timestamp=now - 90)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 80,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )
            _hook(hook_log, "user_prompt_submit", "session-b", "turn-b", timestamp=now - 40)
            _hook(hook_log, "stop", "session-b", "turn-b", timestamp=now - 20)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "待确认")

    def test_waiting_turn_beats_a_newer_running_session(self) -> None:
        # Both sessions have an open turn, so recency alone would pick the newer
        # running one and hide the prompt that is actually blocking the process.
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            now = time.time()
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", timestamp=now - 90)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                timestamp=now - 80,
                path=hook_log,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )
            _hook(hook_log, "user_prompt_submit", "session-b", "turn-b", timestamp=now - 20)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "待确认")
        self.assertEqual(sessions[0].waiting_reason, "Bash")

    def test_claude_waiting_registration_displays_waiting(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(
                claude_home,
                status="waiting",
                waiting_for="goal proposal",
            )
            _write_claude_transcript(
                claude_home,
                [{"type": "user", "origin": {"kind": "human"}}],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.cli_type, "claude")
        self.assertEqual(session.display_status, "待确认")
        self.assertEqual(session.waiting_reason, "goal proposal")
        self.assertEqual(session.inference.status, "waiting_decision_registration")
        self.assertTrue(session.inference.limitations)

    def test_claude_waiting_without_a_label_still_displays_waiting(self) -> None:
        with _claude_runtime() as (proc, claude_home):
            _write_claude_registration(claude_home, status="waiting")
            _write_claude_transcript(
                claude_home,
                [{"type": "user", "origin": {"kind": "human"}}],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(sessions[0].display_status, "待确认")
        self.assertEqual(sessions[0].waiting_reason, "permission prompt")

    def test_opencode_pending_prompt_displays_waiting(self) -> None:
        with _opencode_runtime(status="running") as (proc, decision_log):
            _write_decision_log(decision_log, [_opencode_ask(category="bash")])

            sessions = discover_sessions(proc)

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.cli_type, "opencode")
        self.assertEqual(session.display_status, "待确认")
        self.assertEqual(session.waiting_reason, "bash")
        self.assertEqual(session.inference.status, "waiting_decision_plugin")
        self.assertIn(
            "open prompt reported by the OpenCode decision plugin",
            session.binding_evidence,
        )

    def test_opencode_answered_prompt_stays_running(self) -> None:
        with _opencode_runtime(status="running") as (proc, decision_log):
            _write_decision_log(
                decision_log,
                [_opencode_ask(), _opencode_reply()],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertIsNone(sessions[0].waiting_reason)

    def test_opencode_marker_cannot_resurrect_a_finished_session(self) -> None:
        # A prompt abandoned by killing OpenCode leaves an unanswered marker
        # behind; the database status is the only thing that may open a turn.
        with _opencode_runtime(status="success") as (proc, decision_log):
            _write_decision_log(decision_log, [_opencode_ask()])

            sessions = discover_sessions(proc)

        self.assertEqual(sessions[0].display_status, "成功")
        self.assertIsNone(sessions[0].waiting_reason)

    def test_opencode_without_the_plugin_keeps_the_database_status(self) -> None:
        with _opencode_runtime(status="running") as (proc, _decision_log):
            sessions = discover_sessions(proc)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_terminal")

    def test_same_directory_opencode_processes_bind_by_resume_session(self) -> None:
        # Two OpenCode processes in one directory must not collapse onto the
        # same session: each resumes a different session with `-s`, so the
        # running one stays 运行中 and the finished one stays 成功.
        with _opencode_two_session_runtime() as (proc, decision_log):
            sessions = discover_sessions(proc)

        by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(set(by_pid), {400, 401})
        self.assertEqual(by_pid[400].display_status, "运行中")
        self.assertEqual(by_pid[401].display_status, "成功")

    def test_opencode_decision_from_another_session_is_not_inherited(self) -> None:
        # A fresh OpenCode row in the same directory as an unanswered marker
        # from a different (older) session keeps its database status instead of
        # showing 待确认.
        with _opencode_runtime(status="running") as (proc, decision_log):
            _write_decision_log(
                decision_log,
                [
                    _opencode_ask(session_id="ses_other", pid=9999),
                ],
            )

            sessions = discover_sessions(proc)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertIsNone(sessions[0].waiting_reason)

    def test_opencode_decision_from_the_same_process_still_displays_waiting(self) -> None:
        # The exact session/process that opened the prompt keeps 待确认; only
        # cross-session markers are spurned.
        with _opencode_runtime(status="running") as (proc, decision_log):
            _write_decision_log(decision_log, [_opencode_ask(pid=400, category="bash")])

            sessions = discover_sessions(proc)

        self.assertEqual(sessions[0].display_status, "待确认")
        self.assertEqual(sessions[0].waiting_reason, "bash")


OPENCODE_CWD = "/work/opencode"
OPENCODE_SESSION_ID = "ses_monitor_waiting"


@contextmanager
def _opencode_runtime(status: str):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proc = root / "proc"
        data_dir = root / "opencode-data"
        decision_log = root / "decisions.jsonl"
        proc.mkdir()
        data_dir.mkdir()
        _write_common_proc(proc)
        _write_process(proc, 400, "opencode", "S", 1, ["opencode"], OPENCODE_CWD)
        _write_opencode_db(data_dir / "opencode.db", status)
        with patch.dict(
            os.environ,
            {
                "OPENCODE_DATA": str(data_dir),
                "OPENCODE_MONITOR_DECISION_LOG": str(decision_log),
            },
        ):
            yield proc, decision_log


@contextmanager
def _opencode_two_session_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proc = root / "proc"
        data_dir = root / "opencode-data"
        decision_log = root / "decisions.jsonl"
        proc.mkdir()
        data_dir.mkdir()
        _write_common_proc(proc)
        _write_process(
            proc,
            400,
            "opencode",
            "S",
            1,
            ["opencode", "-s", "ses_monitor_waiting"],
            OPENCODE_CWD,
        )
        _write_process(
            proc,
            401,
            "opencode",
            "S",
            1,
            ["opencode", "-s", "ses_other_finished"],
            OPENCODE_CWD,
        )
        _write_two_session_opencode_db(data_dir / "opencode.db")
        with patch.dict(
            os.environ,
            {
                "OPENCODE_DATA": str(data_dir),
                "OPENCODE_MONITOR_DECISION_LOG": str(decision_log),
            },
        ):
            yield proc, decision_log


def _write_opencode_db(path: Path, status: str) -> None:
    import sqlite3

    now_ms = int(time.time() * 1000)
    assistant: dict[str, object] = {
        "role": "assistant",
        "time": {"created": now_ms - 50_000},
    }
    if status == "success":
        assistant["time"] = {"created": now_ms - 50_000, "completed": now_ms - 10_000}
        assistant["finish"] = "stop"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, "
            "slug TEXT, directory TEXT, title TEXT, version TEXT, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        connection.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
            "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (
                OPENCODE_SESSION_ID,
                "global",
                "s",
                OPENCODE_CWD,
                "t",
                "1.0.0",
                now_ms - 120_000,
                now_ms - 1_000,
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (
                "m1",
                OPENCODE_SESSION_ID,
                now_ms - 120_000,
                now_ms - 120_000,
                json.dumps({"role": "user", "time": {"created": now_ms - 120_000}}),
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (
                "m2",
                OPENCODE_SESSION_ID,
                now_ms - 50_000,
                now_ms - 1_000,
                json.dumps(assistant),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _write_two_session_opencode_db(path: Path) -> None:
    import sqlite3

    now_ms = int(time.time() * 1000)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, "
            "slug TEXT, directory TEXT, title TEXT, version TEXT, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        connection.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
            "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (
                "ses_monitor_waiting",
                "global",
                "s",
                OPENCODE_CWD,
                "t",
                "1.0.0",
                now_ms - 120_000,
                now_ms - 5_000,
            ),
        )
        connection.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (
                "ses_other_finished",
                "global",
                "s",
                OPENCODE_CWD,
                "t",
                "1.0.0",
                now_ms - 220_000,
                now_ms - 1_000,
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (
                "m1",
                "ses_monitor_waiting",
                now_ms - 120_000,
                now_ms - 120_000,
                json.dumps({"role": "user", "time": {"created": now_ms - 120_000}}),
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (
                "m2",
                "ses_monitor_waiting",
                now_ms - 60_000,
                now_ms - 5_000,
                json.dumps(
                    {"role": "assistant", "time": {"created": now_ms - 60_000}}
                ),
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (
                "m3",
                "ses_other_finished",
                now_ms - 220_000,
                now_ms - 220_000,
                json.dumps(
                    {"role": "user", "time": {"created": now_ms - 220_000}}
                ),
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (
                "m4",
                "ses_other_finished",
                now_ms - 150_000,
                now_ms - 1_000,
                json.dumps(
                    {
                        "role": "assistant",
                        "time": {
                            "created": now_ms - 150_000,
                            "completed": now_ms - 10_000,
                        },
                        "finish": "stop",
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _opencode_ask(
    category: str | None = None,
    session_id: str | None = None,
    pid: int | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "event": "permission.asked",
        "kind": "permission",
        "timestamp": time.time() - 20.0,
        "pid": 400 if pid is None else pid,
        "directory": OPENCODE_CWD,
        "session_id": OPENCODE_SESSION_ID if session_id is None else session_id,
        "request_id": "per_1",
        "category": category,
    }


def _opencode_reply() -> dict:
    return {
        "schema_version": 1,
        "event": "permission.replied",
        "kind": None,
        "timestamp": time.time() - 5.0,
        "pid": 400,
        "directory": OPENCODE_CWD,
        "session_id": OPENCODE_SESSION_ID,
        "request_id": "per_1",
        "category": None,
    }


def _write_decision_log(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


@contextmanager
def _claude_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proc = root / "proc"
        claude_home = root / "claude-home"
        proc.mkdir()
        _write_common_proc(proc)
        _write_process(proc, 300, "claude", "S", 1, ["claude"], CLAUDE_CWD)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
            yield proc, claude_home


def _write_claude_registration(
    claude_home: Path,
    status: str,
    pid: int = 300,
    proc_start: int = 100,
    waiting_for: str | None = None,
) -> None:
    sessions = claude_home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "pid": pid,
        "procStart": proc_start,
        "sessionId": CLAUDE_SESSION_ID,
        "cwd": CLAUDE_CWD,
        "kind": "interactive",
        "entrypoint": "cli",
        "status": status,
        "startedAt": 1_700_000_000_000,
        "updatedAt": 1_700_000_050_000,
        "statusUpdatedAt": 1_700_000_050_000,
    }
    if waiting_for is not None:
        payload["waitingFor"] = waiting_for
    (sessions / f"{pid}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_claude_transcript(claude_home: Path, records: list[dict]) -> None:
    directory = claude_home / "projects" / encode_project_dir(CLAUDE_CWD)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{CLAUDE_SESSION_ID}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _runtime(tmp: str) -> tuple[Path, Path, Path, Path]:
    root = Path(tmp)
    proc = root / "proc"
    home = root / "codex-home"
    hook_log = root / "hooks.jsonl"
    proc.mkdir()
    home.mkdir()
    _write_common_proc(proc)
    _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
    return root, proc, home, hook_log


def _tmux_runtime(tmp: str) -> tuple[Path, Path, Path, Path]:
    root = Path(tmp)
    proc = root / "proc"
    home = root / "codex-home"
    hook_log = root / "hooks.jsonl"
    proc.mkdir()
    home.mkdir()
    _write_common_proc(proc)
    _write_process(
        proc,
        90,
        "tmux: server",
        "S",
        1,
        ["tmux", "new", "-s", "codex"],
        "/work/a",
    )
    _write_process(proc, 100, "bash", "S", 90, ["bash"], "/work/a")
    _write_process(proc, 101, "node", "S", 100, ["node", "codex"], "/work/a")
    _write_process(
        proc,
        102,
        "codex",
        "S",
        101,
        ["/opt/codex/vendor/aarch64-unknown-linux-musl/bin/codex"],
        "/work/a",
    )
    return root, proc, home, hook_log


def _hook(
    path: Path,
    event: str,
    session_id: str,
    turn_id: str,
    *,
    ppid: int = 100,
    timestamp: float | None = None,
) -> None:
    append_hook_event(
        event,
        cwd="/work/a",
        ppid=ppid,
        timestamp=timestamp,
        path=path,
        hook_payload={"session_id": session_id, "turn_id": turn_id},
    )


def _write_terminal(
    home: Path,
    session_id: str,
    turn_id: str,
    event_type: str,
    *,
    error: object = "absent",
    timestamp: float | None = None,
) -> Path:
    payload: dict[str, object] = {"type": event_type, "turn_id": turn_id}
    if error != "absent":
        payload["error"] = error
    path = _session_path(home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(timestamp),
        )
        if timestamp is not None
        else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "event_msg",
        "payload": payload,
    }
    import json

    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _append_terminal(
    path: Path,
    turn_id: str,
    event_type: str,
    *,
    error: object = "absent",
) -> None:
    payload: dict[str, object] = {"type": event_type, "turn_id": turn_id}
    if error != "absent":
        payload["error"] = error
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "event_msg",
        "payload": payload,
    }
    import json

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _session_path(home: Path, session_id: str) -> Path:
    return home / "sessions" / "2026" / "07" / "29" / f"rollout-{session_id}.jsonl"


def _bind_open_session(
    proc: Path,
    pid: int,
    session_path: Path,
    fd: int,
) -> None:
    (proc / str(pid) / "fd" / str(fd)).symlink_to(session_path)


def _extend_with_sparse_gap(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.seek(0, 2)
        handle.seek(size, 1)
        handle.write(b"\n{}\n")


def _fail_sleep(_: float) -> None:
    raise AssertionError("CPU sample sleep must not run")


def _remove_fake_process(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


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
) -> None:
    pid_dir = proc / str(pid)
    (pid_dir / "fd").mkdir(parents=True, exist_ok=True)
    (pid_dir / "stat").write_text(_stat_line(pid, comm, state, ppid), encoding="utf-8")
    (pid_dir / "cmdline").write_bytes(b"\0".join(item.encode() for item in cmdline) + b"\0")
    (pid_dir / "cwd").symlink_to(cwd)
    (pid_dir / "exe").symlink_to(f"/usr/bin/{cmdline[0]}")
    (pid_dir / "fd" / "0").symlink_to("/dev/pts/3")


def _stat_line(pid: int, comm: str, state: str, ppid: int) -> str:
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
        "100",
    ]
    return f"{pid} ({comm}) {' '.join(fields)}\n"


if __name__ == "__main__":
    unittest.main()
