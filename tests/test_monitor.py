from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
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

    def test_node_codex_wrapper_without_native_binary_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(
                proc,
                100,
                "MainThread",
                "S",
                1,
                [
                    "node",
                    "/home/coder/.nvm/versions/node/v24.14.0/lib/node_modules/@openai/codex/bin/codex",
                ],
                "/work/a",
            )

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(sessions, ())

    def test_codex_upgrade_install_tree_is_not_reported_without_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(
                proc,
                101,
                "npm",
                "S",
                100,
                ["npm", "install", "-g", "@openai/codex@latest"],
                "/work/a",
            )

            sessions = discover_sessions(proc, sample_window=0)

        self.assertEqual(sessions, ())

    def test_codex_package_install_during_active_turn_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(
                proc,
                101,
                "npm",
                "S",
                100,
                ["npm", "install", "-g", "@openai/codex@latest"],
                "/work/a",
            )
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=hook_log)

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root.pid, 100)
        self.assertEqual(sessions[0].display_status, "运行中")

    def test_codex_package_install_with_user_turn_state_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(
                proc,
                101,
                "npm",
                "S",
                100,
                ["npm", "install", "-g", "@openai/codex@latest"],
                "/work/a",
            )
            _write_session(
                home,
                "/work/a",
                '{"type":"response_item","payload":{"type":"message","role":"user"}}',
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root.pid, 100)
        self.assertEqual(sessions[0].display_status, "运行中")

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

    def test_hook_stop_with_assistant_stream_diagnostic_marks_failure(self) -> None:
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
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"■ stream disconnected before completion: Transport error: network error: error decoding response body"}}\n'
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

    def test_hook_stop_with_empty_terminal_turn_marks_failure(self) -> None:
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
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"user_message","message":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"token_count"}}\n'
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

    def test_commentary_error_discussion_does_not_mark_session_failed(self) -> None:
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
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"explain"}}\n'
                '{"type":"event_msg","payload":{"type":"agent_message","phase":"commentary","message":"I saw ■ unexpected status 503 Service Unavailable."}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"done"}}\n'
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
        self.assertEqual(sessions[0].display_status, "成功")
        self.assertFalse(sessions[0].state_activity.failed_event)

    def test_failed_session_event_without_terminal_event_keeps_open_hook_turn_running(self) -> None:
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
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_missing_stop_hook_fresh_failed_terminal_event_remains_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 2
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            session = _write_session_records(
                home,
                "fresh-failure.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "fresh", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base),
                        "payload": {"turn_id": "fresh-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 0.5),
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 1),
                        "payload": {
                            "type": "agent_message",
                            "message": "■ exceeded retry limit, last status: 429 Too Many Requests",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 1),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            os.utime(session, (base + 1, base + 1))
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base,
                path=hook_log,
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

    def test_missing_stop_hook_stale_failed_terminal_event_remains_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            session = _write_session_records(
                home,
                "stale-failure.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "stale", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 1),
                        "payload": {"turn_id": "stale-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 2),
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 3),
                        "payload": {
                            "type": "turn_aborted",
                            "reason": "interrupted",
                        },
                    },
                ],
            )
            os.utime(session, (base + 3, base + 3))
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 1,
                path=hook_log,
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

    def test_missing_stop_hook_stale_empty_terminal_event_becomes_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            session = _write_session_records(
                home,
                "empty-reset.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "empty", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 5),
                        "payload": {"turn_id": "empty-turn"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 6),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            os.utime(session, (base + 6, base + 6))
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 1,
                path=hook_log,
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "未运行")
        self.assertFalse(sessions[0].state_activity.failed_event)

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

    def test_same_cwd_active_hook_gets_new_activity_before_old_stopped_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(proc, 200, "codex", "S", 1, ["codex"], "/work/a")
            old_session = _write_session_records(
                home,
                "old-success.jsonl",
                [
                    {
                        "type": "session_meta",
                        "timestamp": _iso(base + 1),
                        "payload": {"session_id": "old", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 2),
                        "payload": {"turn_id": "old-turn"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 3),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            new_session = _write_session_records(
                home,
                "new-active.jsonl",
                [
                    {
                        "type": "session_meta",
                        "timestamp": _iso(base + 20),
                        "payload": {"session_id": "new", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 21),
                        "payload": {"turn_id": "new-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 22),
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 23),
                        "payload": {"type": "token_count"},
                    },
                ],
            )
            os.utime(old_session, (base + 3, base + 3))
            os.utime(new_session, (base + 23, base + 23))
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 2,
                path=hook_log,
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 4,
                path=hook_log,
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=200,
                timestamp=base + 20,
                path=hook_log,
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        sessions_by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(
            sessions_by_pid[100].state_activity.relative_path,
            "sessions/2026/06/26/old-success.jsonl",
        )
        self.assertEqual(
            sessions_by_pid[200].state_activity.relative_path,
            "sessions/2026/06/26/new-active.jsonl",
        )
        self.assertEqual(sessions_by_pid[200].display_status, "运行中")

    def test_same_pid_session_start_ignores_old_success_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_session_records(
                home,
                "old-success.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "s", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 1),
                        "payload": {"turn_id": "old"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 3),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 1,
                path=hook_log,
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 4,
                path=hook_log,
            )
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 10,
                path=hook_log,
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "未运行")
        self.assertIsNone(sessions[0].state_activity)

    def test_new_empty_session_after_stop_marks_same_process_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            old_session = _write_session_records(
                home,
                "old-success.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "old", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 1),
                        "payload": {"turn_id": "old"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 2),
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "old",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 3),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            new_session = _write_session_records(
                home,
                "new-empty.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "new", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 5),
                        "payload": {"turn_id": "new"},
                    },
                ],
            )
            os.utime(old_session, (base + 4, base + 4))
            os.utime(new_session, (base + 5, base + 5))
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 1,
                path=hook_log,
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 4,
                path=hook_log,
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "未运行")
        self.assertIsNotNone(sessions[0].state_activity)
        self.assertEqual(
            sessions[0].state_activity.relative_path,
            "sessions/2026/06/26/new-empty.jsonl",
        )
        self.assertFalse(sessions[0].state_activity.latest_turn_has_user)

    def test_same_cwd_interruption_only_marks_matching_process_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(proc, 200, "codex", "S", 1, ["codex"], "/work/a")
            _write_session_records(
                home,
                "old-success.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "old", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 1),
                        "payload": {"turn_id": "old-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 2),
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 3),
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 3),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            _write_session_records(
                home,
                "new-failure.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "new", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base + 20),
                        "payload": {"turn_id": "new-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base + 21),
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base + 25),
                        "payload": {
                            "type": "turn_aborted",
                            "reason": "interrupted",
                        },
                    },
                ],
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 1,
                path=hook_log,
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 4,
                path=hook_log,
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=200,
                timestamp=base + 20,
                path=hook_log,
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=200,
                timestamp=base + 26,
                path=hook_log,
            )

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=0,
                codex_home=home,
                hook_log=hook_log,
            )

        sessions_by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(sessions_by_pid[100].display_status, "成功")
        self.assertEqual(sessions_by_pid[200].display_status, "失败")
        self.assertEqual(
            sessions_by_pid[100].state_activity.relative_path,
            "sessions/2026/06/26/old-success.jsonl",
        )
        self.assertEqual(
            sessions_by_pid[200].state_activity.relative_path,
            "sessions/2026/06/26/new-failure.jsonl",
        )

    def test_same_cwd_sessions_bind_distinct_files_without_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time()
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(
                proc,
                100,
                "codex",
                "S",
                1,
                ["codex"],
                "/work/a",
                start_ticks=1000,
            )
            _write_process(
                proc,
                200,
                "codex",
                "S",
                1,
                ["codex"],
                "/work/a",
                start_ticks=5000,
            )
            first_session = _write_session_records(
                home,
                "first-success.jsonl",
                [
                    {
                        "type": "session_meta",
                        "timestamp": _iso(base - 189),
                        "payload": {"session_id": "first", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": _iso(base - 188),
                        "payload": {"turn_id": "first-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base - 187),
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": _iso(base - 186),
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": _iso(base - 185),
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            second_records = [
                {
                    "type": "session_meta",
                    "timestamp": _iso(base - 149),
                    "payload": {"session_id": "second", "cwd": "/work/a"},
                },
                {
                    "type": "turn_context",
                    "timestamp": _iso(base - 148),
                    "payload": {"turn_id": "second-turn"},
                },
                {
                    "type": "response_item",
                    "timestamp": _iso(base - 147),
                    "payload": {"type": "message", "role": "user"},
                },
            ]
            second_session = _write_session_records(
                home,
                "second-running.jsonl",
                second_records,
            )
            os.utime(first_session, (base - 185, base - 185))
            os.utime(second_session, (base - 147, base - 147))

            def mutate_second_session(_: float) -> None:
                _write_session_records(
                    home,
                    "second-running.jsonl",
                    [
                        *second_records,
                        {
                            "type": "response_item",
                            "timestamp": _iso(base - 146),
                            "payload": {
                                "type": "function_call",
                                "name": "exec_command",
                            },
                        },
                    ],
                )
                os.utime(second_session, (base + 1, base + 1))

            sessions = discover_sessions(
                proc_root=proc,
                sample_window=1,
                codex_home=home,
                sleep=mutate_second_session,
            )

        sessions_by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(sessions_by_pid[100].display_status, "成功")
        self.assertEqual(sessions_by_pid[200].display_status, "运行中")
        self.assertEqual(
            sessions_by_pid[100].state_activity.relative_path,
            "sessions/2026/06/26/first-success.jsonl",
        )
        self.assertEqual(
            sessions_by_pid[200].state_activity.relative_path,
            "sessions/2026/06/26/second-running.jsonl",
        )

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


def _write_session_records(home: Path, name: str, records: list[dict]) -> Path:
    session = home / "sessions" / "2026" / "06" / "26" / name
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    return session


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


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
