from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor import hooks


class HooksTests(unittest.TestCase):
    def test_post_tool_use_discards_stdin_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}):
                with patch(
                    "codex_cli_monitor.hooks.discard_hook_payload_stdin",
                ) as discard:
                    self.assertEqual(hooks.main(["post_tool_use"]), 0)

            payload = json.loads(log_path.read_text(encoding="utf-8"))

        discard.assert_called_once_with()
        self.assertEqual(payload["event"], "post_tool_use")
        self.assertIsNone(payload["session_id"])
        self.assertIsNone(payload["hook_source"])

    def test_session_start_still_reads_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}):
                with patch(
                    "codex_cli_monitor.hooks.read_hook_payload_stdin",
                    return_value={"source": "startup", "session_id": "019f-test"},
                ):
                    self.assertEqual(hooks.main(["session_start"]), 0)

            payload = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "session_start")
        self.assertEqual(payload["hook_source"], "startup")
        self.assertEqual(payload["session_id"], "019f-test")

    def test_hook_accepts_parent_pid_and_timestamp_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}):
                self.assertEqual(
                    hooks.main(["post_tool_use", "--ppid", "1234", "--timestamp", "42.5"]),
                    0,
                )

            payload = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "post_tool_use")
        self.assertEqual(payload["ppid"], 1234)
        self.assertEqual(payload["timestamp"], 42.5)


if __name__ == "__main__":
    unittest.main()
