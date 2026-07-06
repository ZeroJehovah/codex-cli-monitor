from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.install_hooks import install_hooks


class InstallHooksTests(unittest.TestCase):
    def test_install_hooks_preserves_unrelated_hooks_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            repo_root = Path(tmp) / "repo"
            (repo_root / "src").mkdir(parents=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo keep",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            install_hooks(hooks_path, repo_root)
            install_hooks(hooks_path, repo_root)

            payload = json.loads(hooks_path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["hooks"]["Stop"]), 2)
        commands = [
            hook["command"]
            for entry in payload["hooks"]["Stop"]
            for hook in entry["hooks"]
        ]
        self.assertIn("echo keep", commands)
        self.assertEqual(
            sum("codex_cli_monitor.hooks" in command for command in commands),
            1,
        )
        monitor_command = next(
            command for command in commands if "codex_cli_monitor.hooks" in command
        )
        self.assertIn("python3 -S -m codex_cli_monitor.hooks", monitor_command)
        monitor_hooks = [
            hook
            for entry in payload["hooks"]["Stop"]
            for hook in entry["hooks"]
            if "codex_cli_monitor.hooks" in hook["command"]
        ]
        self.assertNotIn("statusMessage", monitor_hooks[0])

    def test_install_hooks_removes_existing_monitor_status_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            repo_root = Path(tmp) / "repo"
            (repo_root / "src").mkdir(parents=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "PYTHONPATH=/old python3 -S -m "
                                                "codex_cli_monitor.hooks pre_tool_use"
                                            ),
                                            "timeout": 5,
                                            "statusMessage": (
                                                "Recording monitor event pre_tool_use"
                                            ),
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            install_hooks(hooks_path, repo_root)

            payload = json.loads(hooks_path.read_text(encoding="utf-8"))

        monitor_hooks = [
            hook
            for entry in payload["hooks"]["PreToolUse"]
            for hook in entry["hooks"]
            if "codex_cli_monitor.hooks" in hook["command"]
        ]
        self.assertEqual(len(monitor_hooks), 1)
        self.assertNotIn("statusMessage", monitor_hooks[0])

    def test_tool_hooks_are_installed_as_background_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            repo_root = Path(tmp) / "repo"
            (repo_root / "src").mkdir(parents=True)

            install_hooks(hooks_path, repo_root)

            payload = json.loads(hooks_path.read_text(encoding="utf-8"))

        pre_tool_command = payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        post_tool_command = payload["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        stop_command = payload["hooks"]["Stop"][0]["hooks"][0]["command"]

        for command in (pre_tool_command, post_tool_command):
            self.assertIn('__codex_monitor_ts="$(date +%s.%N)"', command)
            self.assertIn(
                "if [ -t 0 ]; then :; else cat >/dev/null 2>/dev/null || true; fi",
                command,
            )
            self.assertIn('--ppid "$PPID"', command)
            self.assertIn('--timestamp "$__codex_monitor_ts"', command)
            self.assertIn("</dev/null >/dev/null 2>&1 &", command)

        self.assertNotIn("</dev/null", stop_command)
        self.assertNotIn("--timestamp", stop_command)


if __name__ == "__main__":
    unittest.main()
