from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.install_hooks import (
    DEFAULT_HOOK_EVENTS,
    HooksConfigError,
    _hook_command_windows,
    check_hooks,
    install_hooks,
    main,
    uninstall_hooks,
)


class InstallHooksTests(unittest.TestCase):
    def test_default_install_is_low_frequency_and_preserves_same_group_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "custom": {"keep": True},
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "echo keep"},
                                        {
                                            "type": "command",
                                            "command": "python -m codex_cli_monitor.hooks stop",
                                        },
                                    ]
                                }
                            ],
                            "PreToolUse": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "codex-monitor-hook pre_tool_use",
                                        }
                                    ]
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = install_hooks(hooks_path, repo_root)
            payload = json.loads(hooks_path.read_text(encoding="utf-8"))

        self.assertTrue(result.changed)
        self.assertEqual(set(payload["hooks"]), set(DEFAULT_HOOK_EVENTS))
        self.assertTrue(payload["custom"]["keep"])
        stop_commands = [
            handler["command"]
            for group in payload["hooks"]["Stop"]
            for handler in group["hooks"]
        ]
        self.assertIn("echo keep", stop_commands)
        self.assertEqual(sum("CODEX_CLI_MONITOR_HOOK=1" in item for item in stop_commands), 1)
        monitor_command = next(item for item in stop_commands if "CODEX_CLI_MONITOR_HOOK=1" in item)
        self.assertIn('--ppid "$PPID"', monitor_command)

    def test_tool_events_require_explicit_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            install_hooks(hooks_path, repo_root, include_tool_events=True)
            payload = json.loads(hooks_path.read_text(encoding="utf-8"))

        self.assertIn("PreToolUse", payload["hooks"])
        self.assertIn("PostToolUse", payload["hooks"])
        command = payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertNotIn("</dev/null", command)
        self.assertNotIn("&", command)

    def test_invalid_config_is_never_overwritten(self) -> None:
        for raw in (
            b"{broken",
            b"[]",
            b'{"hooks": []}',
            b'{"hooks":{"Stop":{}}}',
            b'{"hooks":{"Stop":[{"hooks":["bad"]}]}}',
        ):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                hooks_path = root / "hooks.json"
                repo_root = root / "repo"
                (repo_root / "src").mkdir(parents=True)
                hooks_path.write_bytes(raw)
                with self.assertRaises(HooksConfigError):
                    install_hooks(hooks_path, repo_root)
                self.assertEqual(hooks_path.read_bytes(), raw)

    def test_install_creates_backup_and_does_not_rewrite_unchanged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            original = b'{"other":true,"hooks":{}}\n'
            hooks_path.write_bytes(original)
            first = install_hooks(hooks_path, repo_root)
            backup_bytes = first.backup_path.read_bytes()
            first_mtime = hooks_path.stat().st_mtime_ns
            time.sleep(0.002)
            second = install_hooks(hooks_path, repo_root)
            second_mtime = hooks_path.stat().st_mtime_ns

        self.assertTrue(first.changed)
        self.assertEqual(backup_bytes, original)
        self.assertFalse(second.changed)
        self.assertEqual(second_mtime, first_mtime)

    def test_failure_before_replace_keeps_original_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            hooks_path.write_text('{"hooks":{},"keep":1}\n', encoding="utf-8")
            original = hooks_path.read_bytes()
            real_fdopen = os.fdopen

            def failing_fdopen(fd: int, *args: object, **kwargs: object):
                os.close(fd)
                raise OSError("simulated write failure")

            with patch("codex_cli_monitor.install_hooks.os.fdopen", side_effect=failing_fdopen):
                with self.assertRaises(OSError):
                    install_hooks(hooks_path, repo_root)
            after = hooks_path.read_bytes()

        self.assertEqual(after, original)
        self.assertEqual(json.loads(original), {"hooks": {}, "keep": 1})
        self.assertIs(real_fdopen, os.fdopen)

    def test_backup_failure_aborts_without_changing_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            hooks_path.write_text('{"hooks":{},"keep":true}\n', encoding="utf-8")
            original = hooks_path.read_bytes()
            with patch(
                "codex_cli_monitor.install_hooks._backup_file",
                side_effect=OSError("backup unavailable"),
            ):
                with self.assertRaises(OSError):
                    install_hooks(hooks_path, repo_root)
            after = hooks_path.read_bytes()
        self.assertEqual(after, original)

    def test_uninstall_removes_only_monitor_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            install_hooks(hooks_path, repo_root, include_tool_events=True)
            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            payload["hooks"]["Stop"][0]["hooks"].insert(
                0, {"type": "command", "command": "echo third-party"}
            )
            hooks_path.write_text(json.dumps(payload), encoding="utf-8")
            result = uninstall_hooks(hooks_path)
            after = json.loads(hooks_path.read_text(encoding="utf-8"))

        self.assertTrue(result.changed)
        self.assertEqual(after["hooks"]["Stop"][0]["hooks"][0]["command"], "echo third-party")
        self.assertNotIn("SessionStart", after["hooks"])

    def test_empty_third_party_group_is_preserved_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src").mkdir(parents=True)
            hooks_path.write_text(
                '{"hooks":{"Stop":[{"matcher":"third-party","hooks":[]}]}}',
                encoding="utf-8",
            )
            install_hooks(hooks_path, repo_root)
            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["hooks"]["Stop"][0]["matcher"], "third-party")
        self.assertEqual(payload["hooks"]["Stop"][0]["hooks"], [])

    def test_check_reports_stale_disabled_and_invalid_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            moved_root = root / "moved"
            (repo_root / "src").mkdir(parents=True)
            (moved_root / "src").mkdir(parents=True)
            install_hooks(hooks_path, repo_root)
            (root / "config.toml").write_text("[features]\nhooks = false\n", encoding="utf-8")
            result = check_hooks(hooks_path, moved_root, config_path=root / "config.toml")
            hooks_path.write_text("broken", encoding="utf-8")
            invalid = check_hooks(hooks_path, repo_root)

        self.assertFalse(result.current)
        self.assertTrue(result.hooks_disabled)
        self.assertEqual(set(result.stale_events), set(DEFAULT_HOOK_EVENTS))
        self.assertFalse(invalid.valid)

    def test_check_cli_has_nonzero_status_when_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = main(["--codex-home", tmp, "--repo-root", tmp, "--check"])
        self.assertEqual(result, 1)

    def test_check_rejects_installed_command_when_module_path_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "hooks.json"
            repo_root = root / "repo"
            (repo_root / "src" / "codex_cli_monitor").mkdir(parents=True)
            (repo_root / "src" / "codex_cli_monitor" / "hooks.py").touch()
            install_hooks(hooks_path, repo_root)
            (repo_root / "src" / "codex_cli_monitor" / "hooks.py").unlink()
            result = check_hooks(hooks_path, repo_root)

        self.assertFalse(result.current)
        self.assertFalse(result.command_path_valid)
        self.assertEqual(result.detail, "command path missing")

    def test_windows_command_generation_uses_supported_override_shape(self) -> None:
        command = _hook_command_windows(Path(r"C:\monitor"), "stop")
        self.assertIn("set \"CODEX_CLI_MONITOR_HOOK=1\"", command)
        self.assertIn("py -3 -S -m codex_cli_monitor.hooks", command)


if __name__ == "__main__":
    unittest.main()
