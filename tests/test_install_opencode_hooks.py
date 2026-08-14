from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.install_opencode_hooks import (
    OpenCodeHooksConfigError,
    check_hooks,
    install_hooks,
    uninstall_hooks,
)


class InstallOpencodeHooksTests(unittest.TestCase):
    def _repo(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "src" / "codex_cli_monitor").mkdir(parents=True)
        (root / "src" / "codex_cli_monitor" / "opencode_hooks.py").write_text("", encoding="utf-8")
        return root

    def test_install_writes_monitor_hooks_and_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src" / "codex_cli_monitor").mkdir(parents=True)
            (repo / "src" / "codex_cli_monitor" / "opencode_hooks.py").write_text("", encoding="utf-8")
            config = Path(tmp) / "opencode.json"
            result = install_hooks(config, repo)
            self.assertTrue(result.changed)
            self.assertEqual(set(result.installed_events), {"UserPromptSubmit", "Stop"})
            payload = json.loads(config.read_text(encoding="utf-8"))
            self.assertIn("hooks", payload)
            hook_commands = []
            for event_name in ("UserPromptSubmit", "Stop"):
                entries = payload["hooks"][event_name]
                self.assertEqual(len(entries), 1)
                hook_commands.append(entries[0]["hooks"][0]["command"])
            self.assertTrue(all("OPENCODE_CLI_MONITOR_HOOK=1" in c for c in hook_commands))

            check = check_hooks(config, repo)
            self.assertTrue(check.valid)
            self.assertTrue(check.installed)
            self.assertTrue(check.current)

            result2 = install_hooks(config, repo)
            self.assertFalse(result2.changed)

    def test_install_preserves_unrelated_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src" / "codex_cli_monitor").mkdir(parents=True)
            (repo / "src" / "codex_cli_monitor" / "opencode_hooks.py").write_text("", encoding="utf-8")
            config = Path(tmp) / "opencode.json"
            config.write_text(
                json.dumps({"model": "custom/model", "hooks": {}}, sort_keys=True),
                encoding="utf-8",
            )
            before = json.loads(config.read_text(encoding="utf-8"))
            install_hooks(config, repo)
            after = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(after["model"], "custom/model")
            self.assertIn("UserPromptSubmit", after["hooks"])

    def test_install_refuses_malformed_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src" / "codex_cli_monitor").mkdir(parents=True)
            (repo / "src" / "codex_cli_monitor" / "opencode_hooks.py").write_text("", encoding="utf-8")
            config = Path(tmp) / "opencode.json"
            config.write_text("{ broken json ", encoding="utf-8")
            with self.assertRaises(OpenCodeHooksConfigError):
                install_hooks(config, repo)
            self.assertEqual(config.read_text(encoding="utf-8"), "{ broken json ")

    def test_uninstall_removes_only_monitor_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src" / "codex_cli_monitor").mkdir(parents=True)
            (repo / "src" / "codex_cli_monitor" / "opencode_hooks.py").write_text("", encoding="utf-8")
            config = Path(tmp) / "opencode.json"
            config.write_text(
                json.dumps(
                    {
                        "hook": {"UserPromptSubmit": "custom"},
                        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "custom"}]}]},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            result = uninstall_hooks(config)
            after = json.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("UserPromptSubmit", after.get("hooks", {}))
            self.assertIn("hook", after)


if __name__ == "__main__":
    unittest.main()