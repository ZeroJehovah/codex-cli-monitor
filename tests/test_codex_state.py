from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
