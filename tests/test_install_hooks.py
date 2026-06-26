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


if __name__ == "__main__":
    unittest.main()
