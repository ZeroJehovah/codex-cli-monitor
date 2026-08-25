from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor import hooks


def _stdin(payload: bytes) -> TextIOWrapper:
    return TextIOWrapper(BytesIO(payload), encoding="utf-8")


class HooksTests(unittest.TestCase):
    def test_schema_v2_captures_only_stable_whitelisted_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            incoming = {
                "hook_event_name": "PreToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "cwd": "/work/a",
                "tool_input": {"secret": "do not store"},
                "prompt": "do not store",
                "transcript_path": "/secret/transcript",
            }
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                "sys.stdin", _stdin(json.dumps(incoming).encode())
            ):
                self.assertEqual(hooks.main(["pre_tool_use", "--ppid", "1234"]), 0)
            payload = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["turn_id"], "turn-1")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertEqual(payload["tool_use_id"], "tool-1")
        self.assertEqual(payload["ppid"], 1234)
        serialized = json.dumps(payload)
        self.assertNotIn("do not store", serialized)
        self.assertNotIn("transcript", serialized)

    def test_object_values_in_whitelisted_slots_are_not_stringified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            incoming = {
                "hook_event_name": "SessionStart",
                "session_id": {"prompt": "secret"},
                "source": {"assistant": "secret"},
                "cwd": "/work/a",
            }
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                "sys.stdin", _stdin(json.dumps(incoming).encode())
            ):
                hooks.main(["session_start"])
            payload = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertIsNone(payload["session_id"])
        self.assertIsNone(payload["hook_source"])
        self.assertNotIn("secret", json.dumps(payload))

    def test_event_name_mismatch_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            incoming = b'{"hook_event_name":"Stop","session_id":"s"}'
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                "sys.stdin", _stdin(incoming)
            ):
                self.assertEqual(hooks.main(["user_prompt_submit"]), 0)
            log_exists = log_path.exists()
            diagnostic_exists = log_path.with_name("hooks.jsonl.diagnostics.jsonl").exists()

        self.assertFalse(log_exists)
        self.assertTrue(diagnostic_exists)

    def test_invalid_and_oversized_stdin_fail_open(self) -> None:
        for incoming in (b"not-json", b"{" + b"x" * (256 * 1024 + 10)):
            with self.subTest(length=len(incoming)), tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "hooks.jsonl"
                with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                    "sys.stdin", _stdin(incoming)
                ):
                    self.assertEqual(hooks.main(["stop"]), 0)
                self.assertFalse(log_path.exists())
                self.assertTrue(
                    log_path.with_name("hooks.jsonl.diagnostics.jsonl").exists()
                )

    def test_any_internal_error_returns_zero_without_stdout(self) -> None:
        stdout = StringIO()
        with patch("codex_cli_monitor.hooks.read_hook_payload_stdin", side_effect=OSError("nope")):
            with redirect_stdout(stdout):
                self.assertEqual(hooks.main(["stop"]), 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_timestamp_override_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                "sys.stdin", _stdin(b'{"hook_event_name":"Stop"}')
            ):
                hooks.main(["stop", "--timestamp", "42.5"])
            payload = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["timestamp"], 42.5)

    def test_permission_request_records_only_the_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            incoming = {
                "hook_event_name": "PermissionRequest",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "cwd": "/work/a",
                "tool_input": {"command": "rm -rf /secret"},
                "permission_request": {"reason": "do not store"},
            }
            stdout = StringIO()
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                "sys.stdin", _stdin(json.dumps(incoming).encode())
            ):
                with redirect_stdout(stdout):
                    self.assertEqual(hooks.main(["permission_request"]), 0)
            payload = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "permission_request")
        self.assertEqual(payload["tool_name"], "Bash")
        serialized = json.dumps(payload)
        self.assertNotIn("do not store", serialized)
        self.assertNotIn("rm -rf", serialized)
        # Empty stdout is how Codex reads "no decision", so the approval prompt
        # is left exactly as it would be without the hook installed.
        self.assertEqual(stdout.getvalue(), "")

    def test_permission_request_name_mismatch_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}), patch(
                "sys.stdin", _stdin(b'{"hook_event_name":"PreToolUse"}')
            ):
                self.assertEqual(hooks.main(["permission_request"]), 0)
            self.assertFalse(log_path.exists())

    def test_low_frequency_hook_handler_p95_is_under_twenty_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hooks.jsonl"
            durations = []
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG": str(log_path)}):
                for index in range(100):
                    incoming = json.dumps(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": "s",
                            "turn_id": f"t-{index}",
                            "cwd": "/work/a",
                        }
                    ).encode()
                    started = time.perf_counter()
                    with patch("sys.stdin", _stdin(incoming)):
                        hooks.main(["user_prompt_submit", "--ppid", "100"])
                    durations.append(time.perf_counter() - started)
        self.assertLess(sorted(durations)[94], 0.020)


if __name__ == "__main__":
    unittest.main()
