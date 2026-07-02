from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor import hooks


class HooksTests(unittest.TestCase):
    def test_post_tool_use_does_not_read_stdin_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}):
                with patch(
                    "codex_cli_monitor.hooks.read_hook_payload_stdin",
                    side_effect=AssertionError("stdin should not be read"),
                ):
                    self.assertEqual(hooks.main(["post_tool_use"]), 0)

            payload = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "post_tool_use")
        self.assertIsNone(payload["session_id"])
        self.assertIsNone(payload["hook_source"])

    def test_session_start_still_reads_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}):
                with patch(
                    "codex_cli_monitor.hooks.read_hook_payload_stdin",
                    return_value={"source": "resume", "session_id": "019f-test"},
                ):
                    self.assertEqual(hooks.main(["session_start"]), 0)

            payload = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "session_start")
        self.assertEqual(payload["hook_source"], "resume")
        self.assertEqual(payload["session_id"], "019f-test")


if __name__ == "__main__":
    unittest.main()
