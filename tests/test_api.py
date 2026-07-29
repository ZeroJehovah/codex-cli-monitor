from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from codex_cli_monitor.api import build_hook_health, build_sessions_payload
from codex_cli_monitor.install_hooks import install_hooks
from codex_cli_monitor.models import CodexSession, Inference, ProcessInfo


class ApiTests(unittest.TestCase):
    def test_hook_health_distinguishes_explicit_disable_and_tool_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir()
            repo_root = Path(__file__).resolve().parents[1]
            install_hooks(home / "hooks.json", repo_root, include_tool_events=True)
            (home / "config.toml").write_text(
                "[features]\nhooks = false\n",
                encoding="utf-8",
            )
            health = build_hook_health(home, Path(tmp) / "missing-hooks.jsonl")

        self.assertEqual(health["configured_mode"], "tool_diagnostics")
        self.assertEqual(health["signal_state"], "explicitly_disabled")
        self.assertTrue(health["installation"]["hooks_disabled"])

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
            display_status="成功",
        )

        payload = build_sessions_payload((session,), observed_at=1_782_454_000.0)

        self.assertEqual(payload["session_count"], 1)
        item = payload["sessions"][0]
        self.assertEqual(item["status"], "成功")
        self.assertEqual(item["directory"], "/work/a")
        self.assertEqual(item["started_at"], 1_782_453_600.0)
        self.assertEqual(item["started_at_iso"], "2026-06-26T06:00:00Z")
        self.assertEqual(item["pid"], 100)
        self.assertEqual(item["inferred_status"]["status"], "waiting_user_likely")
        full = session.to_dict()
        self.assertIn("binding_method", full)
        self.assertIn("binding_evidence", full)


if __name__ == "__main__":
    unittest.main()
