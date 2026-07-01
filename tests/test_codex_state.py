from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.codex_state import scan_codex_state, scan_session_activities


class CodexStateTests(unittest.TestCase):
    def test_scan_codex_state_reads_metadata_without_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            shell_snapshot = home / "shell_snapshots" / "abc.sh"
            session.parent.mkdir(parents=True)
            shell_snapshot.parent.mkdir(parents=True)
            session.write_text('{"secret":"not returned"}\n', encoding="utf-8")
            shell_snapshot.write_text("echo hi\n", encoding="utf-8")
            (home / "state_5.sqlite-wal").write_bytes(b"sqlite")

            summary = scan_codex_state(home, max_files=10)

        payload = summary.to_dict()
        self.assertEqual(summary.codex_home, tmp)
        self.assertEqual(summary.scan_errors, ())
        self.assertIn("session_jsonl", {item.kind for item in summary.newest_files})
        self.assertIn("shell_snapshot", {item.kind for item in summary.newest_files})
        self.assertNotIn("secret", repr(payload))

    def test_missing_codex_home_reports_conservative_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            summary = scan_codex_state(missing)

        self.assertEqual(summary.newest_files, ())
        self.assertIn("does not exist", summary.scan_errors[0])

    def test_scan_session_activities_reads_head_and_tail_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / (
                "rollout-2026-06-26T15-42-25-019f02e1-4585-7693-84dd-684e3da64778.jsonl"
            )
            session.parent.mkdir(parents=True)
            session.write_text(
                "\n".join(
                    [
                        '{"type":"session_meta","payload":{"session_id":"019f02e1-4585-7693-84dd-684e3da64778","cwd":"/work/a"}}',
                        '{"type":"response_item","payload":{"type":"message","role":"user","content":"secret"}}',
                        '{"type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"secret"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].session_id, "019f02e1-4585-7693-84dd-684e3da64778")
        self.assertEqual(activities[0].cwd, "/work/a")
        self.assertEqual(activities[0].last_payload_type, "function_call")
        self.assertNotIn("secret", repr(activities[0].to_dict()))

    def test_scan_session_activities_marks_successful_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_keeps_missing_final_agent_message_diagnostic_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": "s", "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": "t"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": None,
                        },
                    },
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].terminal_agent_message_missing)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_keeps_final_agent_message_successful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": "s", "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": "t"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": "done",
                        },
                    },
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].terminal_agent_message_missing)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_marks_interrupted_turn_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"turn_aborted","reason":"interrupted"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)
        self.assertEqual(activities[0].last_payload_reason, "interrupted")

    def test_scan_session_activities_marks_retry_limit_message_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"agent_message","message":"■ exceeded retry limit, last status: 429 Too Many Requests"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_marks_stream_disconnect_message_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"agent_message","message":"■ stream disconnected before completion: stream closed before response.completed"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_marks_unexpected_http_status_message_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"agent_message","message":"■ unexpected status 503 Service Unavailable: auth_unavailable: no auth available (providers=codex)"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_keeps_empty_terminal_turn_successful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"user_message","message":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"token_count"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_keeps_empty_terminal_turn_without_token_count_successful(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"event_msg","payload":{"type":"task_started"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_ignores_commentary_error_discussion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"explain"}}\n'
                '{"type":"event_msg","payload":{"type":"agent_message","phase":"commentary","message":"I saw ■ unexpected status 503 Service Unavailable."}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"done"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_marks_red_terminal_error_message_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"event_msg","payload":{"type":"agent_message","message":"\\u001b[31mService Unavailable\\u001b[0m"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_ignores_error_text_from_user_or_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"■ exceeded retry limit, last status: 429 Too Many Requests"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"■ unexpected status 503 Service Unavailable: auth_unavailable"}}\n'
                '{"type":"response_item","payload":{"type":"function_call_output","output":"ERROR: exceeded retry limit, last status: 429 Too Many Requests"}}\n'
                '{"type":"response_item","payload":{"type":"function_call_output","output":"■ unexpected status 503 Service Unavailable: auth_unavailable"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_ignores_plain_assistant_error_discussion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"explain errors"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"A service unavailable response usually means the upstream service is down."}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_ignores_assistant_quoted_error_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"what happened?"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"This is only an example:\\n\\n`■ unexpected status 503 Service Unavailable: auth_unavailable`\\n\\n■ unexpected status 503 Service Unavailable: auth_unavailable"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_marks_red_assistant_terminal_error_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"\\u001b[31munexpected status 503 Service Unavailable\\u001b[0m"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_marks_assistant_terminal_diagnostic_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"session_meta","payload":{"session_id":"s","cwd":"/work/a"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"user","content":"go"}}\n'
                '{"type":"response_item","payload":{"type":"message","role":"assistant","content":"■ stream disconnected before completion: Transport error: network error: error decoding response body"}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_marks_latest_turn_failure_after_later_task_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "s", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": "2026-06-29T08:00:00Z",
                        "payload": {"turn_id": "turn-1"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-29T08:00:01Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "go",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-29T08:00:02Z",
                        "payload": {
                            "type": "agent_message",
                            "message": "■ exceeded retry limit, last status: 429 Too Many Requests",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-29T08:00:03Z",
                        "payload": {"type": "token_count"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-29T08:00:04Z",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].turn_id, "turn-1")
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)
        self.assertIsNotNone(activities[0].turn_started_at)
        self.assertIsNotNone(activities[0].terminal_event_at)

    def test_scan_session_activities_uses_latest_turn_not_previous_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "s", "cwd": "/work/a"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": "2026-06-29T08:00:00Z",
                        "payload": {"turn_id": "old-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-29T08:00:01Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "old",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-29T08:00:02Z",
                        "payload": {"type": "task_complete"},
                    },
                    {
                        "type": "turn_context",
                        "timestamp": "2026-06-29T08:01:00Z",
                        "payload": {"turn_id": "new-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-29T08:01:01Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "new",
                        },
                    },
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].turn_id, "new-turn")
        self.assertFalse(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_scans_latest_turn_beyond_tail_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {"session_id": "s", "cwd": "/work/a"},
                },
                {
                    "type": "turn_context",
                    "timestamp": "2026-06-29T08:00:00Z",
                    "payload": {"turn_id": "old-turn"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-06-29T08:00:01Z",
                    "payload": {"type": "task_complete"},
                },
            ]
            records.extend(
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "old_count": index},
                }
                for index in range(10)
            )
            records.extend(
                [
                    {
                        "type": "turn_context",
                        "timestamp": "2026-06-29T08:01:00Z",
                        "payload": {"turn_id": "new-turn"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-29T08:01:01Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "new",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-29T08:01:02Z",
                        "payload": {
                            "type": "agent_message",
                            "message": "■ exceeded retry limit, last status: 429 Too Many Requests",
                        },
                    },
                ]
            )
            records.extend(
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "new_count": index},
                }
                for index in range(220)
            )
            records.append(
                {
                    "type": "event_msg",
                    "timestamp": "2026-06-29T08:01:03Z",
                    "payload": {"type": "task_complete"},
                }
            )
            _write_jsonl(session, records)

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].turn_id, "new-turn")
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_marks_matching_runtime_turn_error_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16aa-1111-7111-8111-111111111111"
            turn_id = "019f16aa-2222-7222-8222-222222222222"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "go",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02.500Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            _write_runtime_log_db(
                home,
                [
                    (
                        1782788402,
                        session_id,
                        _runtime_turn_error_body(session_id, turn_id),
                    )
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_ignores_runtime_error_from_other_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16aa-3333-7333-8333-333333333333"
            current_turn_id = "019f16aa-4444-7444-8444-444444444444"
            old_turn_id = "019f16aa-5555-7555-8555-555555555555"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": current_turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "go",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02.500Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            _write_runtime_log_db(
                home,
                [
                    (
                        1782788300,
                        session_id,
                        _runtime_turn_error_body(session_id, old_turn_id),
                    )
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_marks_runtime_turn_error_with_log_target_as_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16aa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
            turn_id = "019f16aa-bbbb-7bbb-8bbb-bbbbbbbbbbbb"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": "done",
                        },
                    },
                ],
            )
            _write_runtime_log_db(
                home,
                [
                    (
                        1782788402,
                        session_id,
                        _runtime_turn_error_body(session_id, turn_id),
                        "log",
                    )
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_waits_for_terminal_event_before_runtime_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16aa-6666-7666-8666-666666666666"
            turn_id = "019f16aa-7777-7777-8777-777777777777"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "go",
                        },
                    },
                ],
            )
            _write_runtime_log_db(
                home,
                [
                    (
                        1782788402,
                        session_id,
                        _runtime_turn_error_body(session_id, turn_id),
                    )
                ],
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertFalse(activities[0].terminal_event)
        self.assertFalse(activities[0].failed_event)

    def test_scan_session_activities_marks_runtime_turn_error_from_wal_tail_as_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16aa-8888-7888-8888-888888888888"
            turn_id = "019f16aa-9999-7999-8999-999999999999"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "go",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02.500Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            (home / "logs_2.sqlite-wal").write_bytes(
                (
                    "\0INFOcodex_core::session::turn"
                    + _runtime_turn_error_body(session_id, turn_id)
                    + "codex_core::session::turncore/src/session/turn.rs\0"
                ).encode("utf-8")
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_marks_runtime_turn_error_from_wal_without_start_regex(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16ac-1111-7111-8111-111111111111"
            turn_id = "019f16ac-2222-7222-8222-222222222222"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": "done",
                        },
                    },
                ],
            )
            (home / "logs_2.sqlite-wal").write_bytes(
                (
                    "INFOlog"
                    + _runtime_turn_error_body(session_id, turn_id)
                    + "codex_core::session::turncore/src/session/turn.rs"
                ).encode("utf-8")
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertTrue(activities[0].failed_event)

    def test_scan_session_activities_ignores_quoted_runtime_error_in_wal_tail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "019f16ab-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
            turn_id = "019f16ab-bbbb-7bbb-8bbb-bbbbbbbbbbbb"
            session = home / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-06-30T03:00:00Z",
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": "/work/a"},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": turn_id},
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "go",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:02.500Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T03:00:03Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )
            (home / "logs_2.sqlite-wal").write_text(
                "TRACEcodex_client::transport: POST body quotes "
                f"INFOcodex_core::session::turn{_runtime_turn_error_body(session_id, turn_id)}",
                encoding="utf-8",
            )

            activities = scan_session_activities(home)

        self.assertEqual(len(activities), 1)
        self.assertFalse(activities[0].failed_event)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _write_runtime_log_db(
    home: Path,
    rows: list[tuple[int, str, str] | tuple[int, str, str, str]],
) -> None:
    connection = sqlite3.connect(home / "logs_2.sqlite")
    try:
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL DEFAULT 0,
                level TEXT NOT NULL DEFAULT 'INFO',
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO logs (
                ts,
                ts_nanos,
                level,
                target,
                feedback_log_body,
                thread_id
            )
            VALUES (?, 0, 'INFO', 'codex_core::session::turn', ?, ?)
            """,
            [
                (timestamp, body, session_id)
                for timestamp, session_id, body, _target in (
                    _runtime_log_row(row) for row in rows
                )
            ],
        )
        for timestamp, session_id, body, target in (
            _runtime_log_row(row) for row in rows
        ):
            if target == "codex_core::session::turn":
                continue
            connection.execute(
                """
                UPDATE logs
                SET target = ?
                WHERE ts = ? AND thread_id = ? AND feedback_log_body = ?
                """,
                (target, timestamp, session_id, body),
            )
        connection.commit()
    finally:
        connection.close()


def _runtime_log_row(
    row: tuple[int, str, str] | tuple[int, str, str, str],
) -> tuple[int, str, str, str]:
    if len(row) == 3:
        timestamp, session_id, body = row
        return timestamp, session_id, body, "codex_core::session::turn"
    return row


def _runtime_turn_error_body(session_id: str, turn_id: str) -> str:
    return (
        f"session_loop{{thread_id={session_id}}}:"
        f"submission_dispatch{{otel.name=\"op.dispatch.user_input\" submission.id=\"{turn_id}\" codex.op=\"user_input\"}}:"
        f"turn{{otel.name=\"session_task.turn\" thread.id={session_id} turn.id={turn_id} model=yuecheng/gpt-5.5}}:"
        "session_task.run:run_turn: Turn error: unexpected status 503 Service Unavailable: "
        "auth_unavailable: no auth available (providers=codex, model=yuecheng/gpt-5.5)"
    )


if __name__ == "__main__":
    unittest.main()
