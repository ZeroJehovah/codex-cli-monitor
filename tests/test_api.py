from __future__ import annotations

import unittest

from codex_cli_monitor.api import build_sessions_payload
from codex_cli_monitor.models import CodexSession, Inference, ProcessInfo


class ApiTests(unittest.TestCase):
    def test_sessions_payload_contains_frontend_status_fields(self) -> None:
        session = CodexSession(
            root=ProcessInfo(
                pid=100,
                ppid=1,
                comm="codex",
                state="S",
                cmdline=("codex",),
                cwd="/work/a",
                exe="/usr/bin/codex",
                tty="/dev/pts/3",
                tty_nr=34816,
                elapsed_seconds=10.0,
                cpu_seconds=1.0,
                started_at=1_782_453_600.0,
            ),
            descendants=(),
            connections=(),
            inference=Inference(
                status="waiting_user_likely",
                confidence=0.9,
                evidence=(),
            ),
            display_status="未运行",
        )

        payload = build_sessions_payload((session,), observed_at=1_782_454_000.0)

        self.assertEqual(payload["session_count"], 1)
        item = payload["sessions"][0]
        self.assertEqual(item["status"], "未运行")
        self.assertEqual(item["directory"], "/work/a")
        self.assertEqual(item["started_at"], 1_782_453_600.0)
        self.assertEqual(item["started_at_iso"], "2026-06-26T06:00:00Z")
        self.assertEqual(item["pid"], 100)
        self.assertEqual(item["inferred_status"]["status"], "waiting_user_likely")


if __name__ == "__main__":
    unittest.main()
